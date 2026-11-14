import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Numeric, String, Text, event, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Status de oportunidade encerrada (o RD Station CRM usa "won"/"lost"; aceitamos variações em português).
STATUS_GANHO = ("won", "ganho", "ganha")
STATUS_PERDIDO = ("lost", "perdido", "perdida")
STATUS_FECHADO = STATUS_GANHO + STATUS_PERDIDO


class PerfilUsuario(str, enum.Enum):
    SDR = "SDR"
    CLOSER = "CLOSER"
    GERENTE = "GERENTE"
    DIRETOR = "DIRETOR"
    ANALISTA = "ANALISTA"
    ADMIN = "ADMIN"


class Usuario(Base):
    __tablename__ = "usuario"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    nome: Mapped[str] = mapped_column(String(255))
    perfil: Mapped[str] = mapped_column(String(32), default=PerfilUsuario.SDR.value)
    ativo: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    leads: Mapped[list["Lead"]] = relationship(back_populates="usuario")
    oportunidades: Mapped[list["Oportunidade"]] = relationship(back_populates="usuario")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rd_lead_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=True
    )
    nome: Mapped[str] = mapped_column(String(512), default="")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telefone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dados_extras: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    usuario: Mapped["Usuario | None"] = relationship(back_populates="leads")
    oportunidades: Mapped[list["Oportunidade"]] = relationship(back_populates="lead")
    interacoes: Mapped[list["Interacao"]] = relationship(back_populates="lead")


class Oportunidade(Base):
    __tablename__ = "oportunidade"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rd_deal_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=True
    )
    nome: Mapped[str] = mapped_column(String(512), default="")
    etapa_funil: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Etapa correspondente no funil do RD Station CRM (usada ao enviar a negociação para lá).
    rd_stage_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valor: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Quando foi ganha ou perdida (vazio enquanto está em aberto). Relatórios de vendas por
    # período usam esta data; oportunidades em aberto usam created_at.
    fechada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    dados_extras: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lead: Mapped["Lead | None"] = relationship(back_populates="oportunidades")
    usuario: Mapped["Usuario | None"] = relationship(back_populates="oportunidades")
    interacoes: Mapped[list["Interacao"]] = relationship(back_populates="oportunidade")


def _fechado(status: Any) -> str | None:
    """"won"/"lost" normalizado se o status é de oportunidade encerrada; senão None."""
    s = status.lower() if isinstance(status, str) else ""
    return "won" if s in STATUS_GANHO else "lost" if s in STATUS_PERDIDO else None


@event.listens_for(Oportunidade.status, "set")
def _registrar_fechamento(op: Oportunidade, novo: Any, antigo: Any, _initiator: Any) -> None:
    """Mantém fechada_em: marca agora ao ganhar/perder (por tela, webhook ou script) e limpa ao reabrir.

    Repetir o mesmo status (o webhook do RD reenvia "won" a cada alteração) não muda a data.
    Para gravar outra data, atribua fechada_em depois do status.
    """
    if _fechado(novo) is None:
        op.fechada_em = None
    elif _fechado(novo) != _fechado(antigo) or op.fechada_em is None:
        op.fechada_em = datetime.now(UTC)


class Interacao(Base):
    """Anotação de um vendedor sobre um cliente (lead e/ou oportunidade): a agenda do time."""

    __tablename__ = "interacao"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Ligada à oportunidade, ao lead ou aos dois (pelo menos um deles).
    oportunidade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oportunidade.id"), index=True, nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), index=True, nullable=True
    )
    # Autor da anotação.
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuario.id"), nullable=True
    )
    tipo: Mapped[str] = mapped_column(String(64), default="nota")
    descricao: Mapped[str] = mapped_column(Text, default="")
    # Quando o contato aconteceu (pode ser registrado depois).
    data: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # Próximo contato combinado com o cliente (aparece na agenda).
    retorno_em: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    oportunidade: Mapped["Oportunidade | None"] = relationship(back_populates="interacoes")
    lead: Mapped["Lead | None"] = relationship(back_populates="interacoes")
    autor: Mapped["Usuario | None"] = relationship()


class ConversaChatbot(Base):
    __tablename__ = "conversa_chatbot"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("usuario.id"), index=True)
    titulo: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sumario: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    mensagens: Mapped[list["Mensagem"]] = relationship(
        back_populates="conversa", cascade="all, delete-orphan"
    )


class Mensagem(Base):
    __tablename__ = "mensagem"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversa_chatbot.id", ondelete="CASCADE"), index=True
    )
    papel: Mapped[str] = mapped_column(String(32), default="user")
    conteudo: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversa: Mapped["ConversaChatbot"] = relationship(back_populates="mensagens")


class Relatorio(Base):
    __tablename__ = "relatorio"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("usuario.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(128))
    parametros: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resultado: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Sincronizacao(Base):
    __tablename__ = "sincronizacao"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fonte: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rd_entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    detalhe: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RdStationToken(Base):
    """Tokens OAuth RD Station (um registro ativo por instalação demo)."""

    __tablename__ = "rd_station_token"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RdStationConfig(Base):
    """Configuração da integração com o RD Station cadastrada pela tela (registro único)."""

    __tablename__ = "rd_station_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Liga/desliga o recebimento de dados sem apagar a configuração.
    ativo: Mapped[bool] = mapped_column(default=True, server_default="true")
    # App criado na RD Station App Store (OAuth2).
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Responsável atribuído aos leads/oportunidades novos que chegam sem dono.
    usuario_integracao_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuario.id", ondelete="SET NULL"), nullable=True
    )
    # Vai na URL do webhook (?token=...) e autentica as chamadas do RD Station.
    webhook_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Webhooks que o próprio CRM Assist criou no RD Station: [{"uuid": ..., "event_type": ...}].
    webhooks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Motivo da última falha ao criar os webhooks (limpo quando a criação dá certo).
    webhook_erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    usuario_integracao: Mapped["Usuario | None"] = relationship()
