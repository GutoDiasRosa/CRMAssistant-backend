import logging
from datetime import datetime
from uuid import UUID, uuid4

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.agents.crm_tools import TOOLS, executar_ferramenta
from app.core.agents.intent_router_crm import classify_intent
from app.db.models import ConversaChatbot, Mensagem, Usuario
from app.dependencies import pode_ver_todos_oportunidades
from app.domain.repositories.crm import OportunidadeRepository

logger = logging.getLogger(__name__)

# Mensagens anteriores da conversa enviadas ao modelo como contexto.
MAX_HISTORICO = 20
# Limite de rodadas de ferramentas por pergunta (evita laços longos).
MAX_RODADAS_FERRAMENTAS = 6

PERFIL_LABEL = {
    "SDR": "Prospector (SDR)",
    "CLOSER": "Closer",
    "GERENTE": "Gerente comercial",
    "DIRETOR": "Diretor",
    "ANALISTA": "Analista de CRM",
    "ADMIN": "Administrador",
}

_client: anthropic.AsyncAnthropic | None = None


def _get_client(api_key: str) -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


class CrmAssistantOrchestratorService:
    def __init__(self, session: AsyncSession, usuario: Usuario):
        self.session = session
        self.usuario = usuario

    async def _get_or_create_conversa(self, session_id: str | None) -> ConversaChatbot:
        if session_id:
            try:
                cid = UUID(session_id)
            except ValueError:
                cid = None
            if cid:
                r = await self.session.execute(
                    select(ConversaChatbot).where(
                        ConversaChatbot.id == cid,
                        ConversaChatbot.usuario_id == self.usuario.id,
                    )
                )
                found = r.scalar_one_or_none()
                if found:
                    return found
        conv = ConversaChatbot(id=uuid4(), usuario_id=self.usuario.id, titulo=None)
        self.session.add(conv)
        await self.session.flush()
        return conv

    async def _historico(self, conversa_id: UUID) -> list[dict]:
        """Últimas mensagens da conversa no formato da API (a primeira precisa ser do usuário)."""
        r = await self.session.execute(
            select(Mensagem)
            .where(Mensagem.conversa_id == conversa_id)
            .order_by(Mensagem.created_at.desc())
            .limit(MAX_HISTORICO)
        )
        mensagens = [
            {"role": m.papel, "content": m.conteudo}
            for m in reversed(r.scalars().all())
            if m.papel in ("user", "assistant") and m.conteudo
        ]
        while mensagens and mensagens[0]["role"] != "user":
            mensagens.pop(0)
        return mensagens

    def _system_prompt(self) -> str:
        visao = (
            "Este usuário vê os dados de toda a equipe."
            if pode_ver_todos_oportunidades(self.usuario)
            else "Este usuário vê apenas a própria carteira; as ferramentas já retornam só os dados dele."
        )
        return (
            "Você é o assistente do CRM Assist, usado pela equipe comercial de uma distribuidora "
            "B2B de equipamentos para construção civil. Você responde em português do Brasil, em "
            "linguagem simples e direta, perguntas sobre funil de vendas, leads, oportunidades, "
            "desempenho do time e relatórios. Muitas pessoas da equipe não dominam o CRM, então "
            "explique números de forma clara, sem jargão técnico.\n\n"
            f"Usuário atual: {self.usuario.nome}, perfil {PERFIL_LABEL.get(self.usuario.perfil, self.usuario.perfil)}. "
            f"{visao}\n"
            f"Data de hoje: {datetime.now().strftime('%d/%m/%Y')}.\n\n"
            "Os dados vêm do banco sincronizado com o RD Station e só estão disponíveis pelas "
            "ferramentas: consulte-as antes de responder sobre números, leads ou oportunidades. "
            "Nunca invente valores, nomes ou métricas; se algo não estiver nos dados, diga que a "
            "informação não está disponível. Escreva valores como R$ 45.000.\n\n"
            "O chat mostra texto simples: não use Markdown (negrito, títulos ou tabelas). Listas "
            "com hífen no início da linha funcionam bem. Seja breve.\n\n"
            "Se a pergunta não tiver relação com vendas ou com o CRM, explique em uma frase que "
            "você só ajuda com esses assuntos."
        )

    async def _responder_com_claude(self, historico: list[dict], mensagem: str) -> str:
        settings = get_settings()
        client = _get_client(settings.anthropic_api_key)
        messages: list = [*historico, {"role": "user", "content": mensagem}]

        for _ in range(MAX_RODADAS_FERRAMENTAS):
            response = await client.beta.messages.create(
                model=settings.anthropic_model,
                max_tokens=16000,
                system=self._system_prompt(),
                tools=TOOLS,
                messages=messages,
                output_config={"effort": settings.anthropic_effort},
                # Se o modelo recusar por política de segurança, a própria API refaz o
                # pedido no modelo recomendado para aquela categoria.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )

            if response.stop_reason == "refusal":
                return "Não consigo ajudar com esse pedido. Posso responder sobre o funil, leads e desempenho do time."

            if response.stop_reason in ("tool_use", "pause_turn"):
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason == "pause_turn":
                    continue
                resultados = []
                for bloco in response.content:
                    if bloco.type != "tool_use":
                        continue
                    try:
                        conteudo = await executar_ferramenta(
                            bloco.name, bloco.input, self.session, self.usuario
                        )
                        resultados.append(
                            {"type": "tool_result", "tool_use_id": bloco.id, "content": conteudo}
                        )
                    except Exception as e:
                        logger.exception("Falha na ferramenta %s", bloco.name)
                        resultados.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": bloco.id,
                                "content": f"Erro ao consultar os dados: {e}",
                                "is_error": True,
                            }
                        )
                # Todos os resultados vão juntos numa única mensagem do usuário.
                messages.append({"role": "user", "content": resultados})
                continue

            texto = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens" and not texto:
                return "A resposta ficou longa demais. Tente uma pergunta mais específica."
            return texto or "Não encontrei uma resposta para isso. Pode reformular a pergunta?"

        return "Essa pergunta exigiu consultas demais. Tente dividir em perguntas menores."

    async def _fallback_sem_ia(self, intent: str) -> str:
        """Resposta simples quando ANTHROPIC_API_KEY não está configurada."""
        if intent == "cumprimento":
            return "Olá! Sou o assistente do CRM. Posso ajudar com funil, leads, performance do time e relatórios."
        if intent == "fora_de_escopo":
            return "Esse assunto está fora do escopo do assistente de vendas e CRM."
        m = await OportunidadeRepository(self.session, self.usuario).metricas_resumo()
        return (
            f"(Modo sem IA) Você tem {m['total_leads']} leads e {m['total_oportunidades']} "
            "oportunidades. Configure ANTHROPIC_API_KEY no backend para respostas completas."
        )

    async def handle_chat(self, mensagem: str, session_id: str | None) -> dict:
        conv = await self._get_or_create_conversa(session_id)
        historico = await self._historico(conv.id)
        self.session.add(Mensagem(conversa_id=conv.id, papel="user", conteudo=mensagem))
        if not conv.titulo:
            conv.titulo = mensagem[:120]
        await self.session.flush()

        intent = classify_intent(mensagem)
        settings = get_settings()
        if not settings.anthropic_api_key:
            reply = await self._fallback_sem_ia(intent)
        else:
            try:
                reply = await self._responder_com_claude(historico, mensagem)
            except anthropic.AuthenticationError:
                logger.error("ANTHROPIC_API_KEY inválida")
                reply = "O assistente está com a chave de IA inválida. Avise o administrador do sistema."
            except anthropic.RateLimitError:
                reply = "O assistente recebeu muitas perguntas agora. Tente de novo em alguns segundos."
            except anthropic.APIStatusError as e:
                logger.error("Erro da API Anthropic %s: %s", e.status_code, e.message)
                reply = "O assistente está indisponível no momento. Tente novamente em instantes."
            except anthropic.APIConnectionError:
                logger.error("Sem conexão com a API Anthropic")
                reply = "Não consegui falar com o serviço de IA. Verifique a conexão e tente de novo."

        self.session.add(Mensagem(conversa_id=conv.id, papel="assistant", conteudo=reply))
        await self.session.commit()
        return {
            "detected_intent": intent,
            "response_text": reply,
            "sessionId": str(conv.id),
        }
