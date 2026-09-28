"""
Popula o banco local com dados de demonstração do CRM Assist.

Cenário: distribuidora B2B de equipamentos para construção civil (ver README).
Pode ser executado várias vezes: usuários e registros já existentes são mantidos.

Uso (dentro de backend/, com o .venv ativo):
    python -m scripts.seed_dev

Credenciais de desenvolvimento criadas (NÃO use em produção):
    admin@crmassist.dev    / demo1234  -> perfil ADMIN   (vê tudo e gerencia usuários)
    gerente@crmassist.dev  / demo1234  -> perfil GERENTE (vê todo o time)
    sdr@crmassist.dev      / demo1234  -> perfil SDR     (vê só a própria carteira)
    closer@crmassist.dev   / demo1234  -> perfil CLOSER  (vê só a própria carteira)
"""

import asyncio

from sqlalchemy import select

from app.core.rd_station_sync.upsert import log_sync, upsert_lead, upsert_oportunidade
from app.db.database import async_session_factory, engine
from app.db.models import Usuario
from app.security import hash_password

SENHA_DEMO = "demo1234"

USUARIOS = [
    ("admin@crmassist.dev", "Administrador", "ADMIN"),
    ("gerente@crmassist.dev", "Rafael Andrade", "GERENTE"),
    ("sdr@crmassist.dev", "Carlos Eduardo Mendes", "SDR"),
    ("closer@crmassist.dev", "Tatiane Rocha", "CLOSER"),
]

# (id RD, empresa, contato, e-mail, etapa, valor, responsável)
OPORTUNIDADES = [
    ("demo-01", "Construtora Horizonte", "João Silva", "joao@horizonte.dev", "Prospecção", 45000, "sdr"),
    ("demo-02", "Obras & Cia", "Maria Santos", "maria@obrasecia.dev", "Prospecção", 32000, "sdr"),
    ("demo-03", "BuildTech Engenharia", "Pedro Costa", "pedro@buildtech.dev", "Qualificado", 78000, "sdr"),
    ("demo-04", "Forte Construções", "Ana Lima", "ana@forte.dev", "Qualificado", 54000, "closer"),
    ("demo-05", "MegaObras", "Carlos Dias", "carlos@megaobras.dev", "Proposta enviada", 120000, "closer"),
    ("demo-06", "Urbana Engenharia", "Beatriz Rocha", "beatriz@urbana.dev", "Proposta enviada", 95000, "closer"),
    ("demo-07", "Estrutural S.A.", "Roberto Alves", "roberto@estrutural.dev", "Negociação", 180000, "closer"),
    ("demo-08", "TopEdifícios", "Laura Mendes", "laura@topedificios.dev", "Negociação", 210000, "closer"),
]


async def main() -> None:
    async with async_session_factory() as session:
        usuarios: dict[str, Usuario] = {}
        for email, nome, perfil in USUARIOS:
            user = (
                await session.execute(select(Usuario).where(Usuario.email == email))
            ).scalar_one_or_none()
            if user is None:
                user = Usuario(
                    email=email, nome=nome, perfil=perfil, hashed_password=hash_password(SENHA_DEMO)
                )
                session.add(user)
                await session.flush()
                print(f"Usuário criado: {email} ({perfil})")
            usuarios[email.split("@")[0]] = user

        for rd_id, empresa, contato, email, etapa, valor, dono in OPORTUNIDADES:
            responsavel = usuarios[dono].id
            lead = await upsert_lead(
                session,
                rd_lead_id=f"lead-{rd_id}",
                nome=contato,
                email=email,
                telefone=None,
                status="Lead",
                dados_extras=None,
                usuario_id=responsavel,
            )
            await upsert_oportunidade(
                session,
                rd_deal_id=f"deal-{rd_id}",
                nome=empresa,
                etapa_funil=etapa,
                valor=valor,
                status="open",
                dados_extras=None,
                lead_id=lead.id,
                usuario_id=responsavel,
            )
        await log_sync(
            session,
            fonte="seed_dev",
            event_type="carga_demonstracao",
            rd_entity_id=None,
            status="success",
            detalhe={"oportunidades": len(OPORTUNIDADES)},
        )
        await session.commit()
        print(f"{len(OPORTUNIDADES)} oportunidades de demonstração prontas.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
