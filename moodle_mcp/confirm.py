"""Confirmação humana, pedida pelo servidor, das ações no Moodle."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any

from mcp import types as mcp_types
from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from moodle_mcp import config

logger = logging.getLogger("moodle-mcp.confirm")

# ==============================================================================
# CONFIRMAÇÃO HUMANA DAS AÇÕES NO MOODLE
#
# Nenhum modelo resiste de forma fiável a instruções escondidas em conteúdo que
# lê (OWASP LLM01), e os posts de fórum são escritos por qualquer participante
# da disciplina. As anotações `destructiveHint` só sugerem ao cliente que
# pergunte, e muitos clientes estão configurados para aprovar tudo. Por isso é
# o servidor que pede a confirmação, por elicitation (spec MCP 2025-06-18),
# mostrando ao estudante o destinatário e o texto exatos — o que é preciso para
# reparar numa ação que não pediu.
# ==============================================================================

CONFIRM_MODES = ("always", "untrusted", "client")

#: Funções que só devolvem dados do próprio estudante ou do site, sem texto
#: escrito por terceiros. Qualquer outra marca a sessão como exposta a
#: conteúdo não confiável.
TRUSTED_WSFUNCTIONS = frozenset(
    {
        "core_webservice_get_site_info",
        "core_course_get_enrolled_courses_by_timeline_classification",
        "core_enrol_get_users_courses",
        "core_completion_get_activities_completion_status",
        "gradereport_overview_get_course_grades",
        "core_user_get_users_by_field",
    }
)

#: Se o processo já entregou ao modelo conteúdo escrito por terceiros. Com o
#: transporte stdio cada cliente tem o seu processo, pelo que equivale à sessão.
untrusted_content_seen = False


def mark_untrusted_content_seen() -> None:
    """Regista que a sessão já recebeu conteúdo escrito por terceiros."""
    global untrusted_content_seen
    untrusted_content_seen = True


def confirm_mode() -> str:
    """Devolve o modo de confirmação configurado (`always` se for inválido)."""
    mode = (config.get("confirm_writes") or "always").strip().lower()
    if mode not in CONFIRM_MODES:
        logger.warning("MOODLE_MCP_CONFIRM_WRITES='%s' inválido; a usar 'always'.", mode)
        return "always"
    return mode


class _Confirmation(BaseModel):
    """Resposta do utilizador ao pedido de confirmação."""

    confirmar: bool = Field(description="Marca para autorizar esta ação na tua conta do Moodle.")


_MAX_SHOWN = 4000


def _shown(value: Any) -> str:
    text = str(value)
    if len(text) <= _MAX_SHOWN:
        return text
    return f"{text[:_MAX_SHOWN]}… (+{len(text) - _MAX_SHOWN} caracteres não mostrados)"


async def require_confirmation(
    ctx: Context[Any, Any, Any] | None, action: str, details: dict[str, Any]
) -> None:
    """Pede ao utilizador, e não ao modelo, que confirme uma ação no Moodle.

    Modos (`MOODLE_MCP_CONFIRM_WRITES`):

    * `always` (omissão): pede sempre.
    * `untrusted`: pede só depois de a sessão ter lido conteúdo de terceiros
      (posts, páginas, mensagens), que é quando uma injeção pode ter entrado.
    * `client`: não pede; confia na aprovação do cliente MCP. Para clientes sem
      elicitation, e com aviso no arranque.

    Falha em fechado: se for preciso confirmar e o cliente não o suportar, a
    ação é recusada.

    Args:
        ctx: O contexto MCP injetado na ferramenta.
        action: Descrição curta da ação, mostrada no topo do pedido.
        details: Campos a mostrar, com os valores exatos que vão ser enviados.

    Raises:
        PermissionError: Se a confirmação não for possível ou for recusada.

    """
    mode = confirm_mode()
    if mode == "client" or (mode == "untrusted" and not untrusted_content_seen):
        audit(action, details, f"executada sem confirmação do servidor (modo {mode})")
        return

    hint = (
        "Para usar ferramentas de escrita num cliente sem elicitation, define "
        "MOODLE_MCP_CONFIRM_WRITES=client (a aprovação passa a depender só do cliente)."
    )
    try:
        session = ctx.session if ctx is not None else None
        supported = session is not None and session.check_client_capability(
            mcp_types.ClientCapabilities(elicitation=mcp_types.ElicitationCapability())
        )
    except Exception:  # sem pedido ativo (chamada interna) ou SDK diferente
        supported = False
    if not supported or ctx is None:
        audit(action, details, "recusada: cliente sem elicitation")
        raise PermissionError(
            "Ação recusada: esta ação no Moodle exige confirmação do utilizador e o cliente MCP "
            f"não suporta pedidos de confirmação (elicitation). {hint}"
        )

    lines = [f"⚠️ {action}", ""]
    lines += [f"• {name}: {_shown(value)}" for name, value in details.items()]
    lines += [
        "",
        "Esta ação é feita na tua conta do Moodle e não pode ser desfeita.",
        "Confirma só se foste tu a pedi-la — instruções escondidas em posts ou páginas "
        "podem levar o assistente a propor ações que não pediste.",
    ]
    try:
        result = await ctx.elicit(message="\n".join(lines), schema=_Confirmation)
    except Exception as exc:
        audit(action, details, f"recusada: pedido de confirmação falhou ({type(exc).__name__})")
        raise PermissionError(f"Ação recusada: o pedido de confirmação falhou ({exc}). {hint}") from None
    if result.action != "accept" or not getattr(result.data, "confirmar", False):
        audit(action, details, f"recusada pelo utilizador ({result.action})")
        raise PermissionError(
            "Ação cancelada: o utilizador não a confirmou. Não voltes a tentar sem um novo pedido explícito."
        )
    audit(action, details, "confirmada pelo utilizador")


#: Tamanho a partir do qual o registo de auditoria roda para `audit.log.1`.
AUDIT_MAX_BYTES = 1024 * 1024


def audit_file() -> Any:
    """Caminho do registo de auditoria das ações no Moodle."""
    return config.config_dir() / "audit.log"


def audit(action: str, details: dict[str, Any], decision: str) -> None:
    """Acrescenta uma linha JSON ao registo de auditoria das ações no Moodle.

    Cada ação que age em nome do estudante (mensagens, posts, submissões)
    fica registada com a decisão — confirmada, recusada, ou executada sem
    confirmação — para que se possa reconstituir o que aconteceu se um dia o
    assistente fizer algo inesperado (OWASP A09). O ficheiro é só do dono
    (0600), não tem tokens, e roda ao chegar a 1 MB. Uma falha a escrever nunca
    impede nem desbloqueia a ação.

    Args:
        action: A descrição da ação.
        details: Os valores mostrados ao utilizador (truncados a 500 caracteres).
        decision: O que aconteceu.

    """
    record = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "acao": action,
        "decisao": decision,
        "detalhes": {name: str(value)[:500] for name, value in details.items()},
    }
    try:
        path = audit_file()
        if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("Não foi possível escrever no registo de auditoria.", exc_info=True)


#: Funções de leitura pelo nome (convenção da API do Moodle); as restantes podem escrever.
READ_ONLY_WSFUNCTION = re.compile(r"_(get|search|list|count|check)_|_(get|search|list)$")
