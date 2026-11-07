"""Servidor MCP para Moodle com sincronização para o Obsidian.

Expõe 32 ferramentas agrupadas em toolsets (ver `TOOLSETS` no fim do ficheiro)
para consultar disciplinas, notas, trabalhos e fóruns, e para sincronizar tudo
isso em Markdown dentro de um Vault do Obsidian.

Decisões de segurança que atravessam o módulo:

* `_redact_secrets` corre em `_moodle_call`, o único ponto de passagem HTTP, para
  que nenhuma credencial devolvida pelo Moodle chegue ao modelo.
* `_resolve_in_vault` confina toda a leitura e escrita de ficheiros ao Vault.
* Nenhum parâmetro usa `Optional[...]`, por compatibilidade de schema com os
  vários providers de LLM.

Arranque: `moodle-mcp` (ver `main`), `python -m moodle_mcp.server`, ou o shim
`main.py` na raiz do repositório.
"""

import argparse
import base64
import binascii
import hashlib
import html
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert
from mcp import types as mcp_types
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from urllib3.util import parse_url

from moodle_mcp import __version__, config, handler, sanitize

try:  # O PyMuPDF é AGPL-3.0 e fica no extra `[pdf]`; sem ele tudo o resto funciona.
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore[assignment]

mcp = FastMCP("Moodle & Obsidian MCP Server")

OBSIDIAN_VAULT_DIR = os.path.expanduser(config.get("obsidian_vault_dir"))

#: Nome da instância usada quando a ferramenta não recebe outra. Resolvido no
#: arranque para que apareça como omissão na schema que o cliente recebe.
DEFAULT_INSTANCE = config.default_instance()


_SECRET_KEYS = frozenset(
    {
        "wstoken",
        "token",
        "privatetoken",
        "userprivateaccesskey",
        "privateaccesskey",
        "sesskey",
        "password",
    }
)


def _redact_secrets(payload: Any) -> Any:
    """Remove recursivamente chaves que transportam credenciais de uma resposta Moodle.

    Tudo o que uma ferramenta devolve é serializado para o prompt do provider de LLM
    configurado no cliente e persistido no histórico da sessão em disco. O
    `core_webservice_get_site_info` devolve `userprivateaccesskey`, pelo que a
    redação é feita no único ponto de passagem HTTP em vez de ferramenta a ferramenta.

    Args:
        payload: Qualquer estrutura desserializada de JSON (dict, list ou escalar).

    Returns:
        A mesma estrutura, com os valores das chaves sensíveis substituídos por
        `"[REDACTED]"`.
    """
    if isinstance(payload, dict):
        return {
            key: ("[REDACTED]" if key.lower() in _SECRET_KEYS and value else _redact_secrets(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_secrets(item) for item in payload]
    return payload


def _token_fingerprint(token: str) -> str:
    """Devolve um identificador não reversível de um token, seguro para mostrar.

    Permite confirmar *qual* token está configurado (e comparar entre execuções)
    sem expor o segredo ao modelo.

    Args:
        token: O token em claro.

    Returns:
        `"sha256:<12 hex>"`, ou `""` se não houver token.
    """
    if not token:
        return ""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _vault_root() -> Path:
    """Devolve a raiz canónica do Vault do Obsidian (único diretório gravável)."""
    return Path(OBSIDIAN_VAULT_DIR).expanduser().resolve()


def _resolve_in_vault(candidate: Any) -> Path:
    """Resolve um caminho e recusa qualquer um que escape ao Vault do Obsidian.

    Os nomes de ficheiro e os `fileurl` vêm do servidor Moodle e os caminhos de
    destino são escolhidos pelo modelo, pelo que uma página de curso manipulada
    poderia de outra forma dirigir uma escrita para `~/.ssh/authorized_keys` ou
    `~/.config/autostart/`. `resolve()` segue symlinks, pelo que um link dentro do
    Vault a apontar para fora também é apanhado.

    Args:
        candidate: Caminho absoluto ou relativo (relativo resolve a partir da raiz
            do Vault).

    Returns:
        O caminho absoluto canónico, garantidamente dentro do Vault.

    Raises:
        ValueError: Se o caminho resolvido ficar fora do Vault.
    """
    root = _vault_root()
    raw = Path(str(candidate)).expanduser()
    if not raw.is_absolute():
        raw = root / raw
    target = raw.resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError(
            f"Acesso recusado: '{target}' está fora do Vault do Obsidian ('{root}'). "
            "Por segurança, o servidor só lê e escreve ficheiros dentro do Vault; "
            "para mudar de Vault, altera OBSIDIAN_VAULT_DIR na configuração."
        )
    return target


# ==============================================================================
# TIPOS DE PARÂMETRO REUTILIZÁVEIS
#
# Cada parâmetro leva uma descrição porque é ela — e não o nome — que o modelo lê
# ao decidir o que passar. Em Moodle a confusão entre `cmid` (id do módulo de
# curso) e o `id` da instância da atividade é a fonte de erro mais comum.
#
# Nenhum parâmetro usa `Optional[...]`: uma união `anyOf: [T, null]` na schema é
# rejeitada ou tratada de forma inconsistente por parte dos providers de LLM.
# Em vez disso, cada parâmetro opcional tem uma sentinela do mesmo tipo
# (`0` para ids, `""` para texto), documentada na descrição.
# ==============================================================================


def _instance_field() -> Any:
    """Constrói o campo `instance` a partir das instâncias configuradas.

    O enum é gerado da configuração, não fixo no código: o cliente recebe na
    schema os nomes que *esta* instalação conhece, em vez de um texto livre que
    o modelo tem de adivinhar.
    """
    names = config.instance_names()
    listed = ", ".join(f"'{name}'" for name in names)
    return Field(
        description=(
            f"Instância Moodle a usar. Configuradas nesta instalação: {listed}. "
            f"Omissão: '{DEFAULT_INSTANCE}'."
        ),
        json_schema_extra=cast(Any, {"enum": names}) if len(names) > 1 else None,
    )


Inst = Annotated[str, _instance_field()]
CourseId = Annotated[
    int,
    Field(description="ID numérico da disciplina/UC (campo `id` de get_enrolled_courses)."),
]
UserId = Annotated[
    int,
    Field(description="ID do utilizador no Moodle; 0 (omissão) usa o aluno autenticado."),
]


def _ann(
    title: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = True,
) -> ToolAnnotations:
    """Constrói as anotações de uma ferramenta para os clientes MCP.

    Os clientes usam estes sinais para decidir o que podem auto-aprovar e o que
    exige confirmação humana. São interpretados pelo cliente, não pelo provider
    de LLM, pelo que funcionam igualmente em qualquer modelo.

    Args:
        title: Nome legível para a interface do cliente.
        read_only: True se a ferramenta não altera nada (nem no Moodle nem em disco).
        destructive: True se pode sobrescrever ou tornar irreversível algo existente.
        idempotent: False se repetir a chamada cria duplicados.

    Returns:
        O objeto `ToolAnnotations` a passar a `@mcp.tool(annotations=...)`.
        `openWorldHint` é sempre True: todas as ferramentas falam com um servidor
        Moodle remoto.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=True,
    )


def _resolve_target(instance: str = "") -> tuple[str, str]:
    """Resolve a URL base e o token de uma instância.

    Args:
        instance: Nome da instância; vazio usa a predefinida.

    Returns:
        Par `(url_base, token)`. O token vem vazio se a instância existir mas
        não tiver token configurado.

    Raises:
        ValueError: Se o nome não existir e houver várias instâncias.
    """
    target = config.resolve_instance(instance)
    return target.url, target.token


class MoodleRequestError(RuntimeError):
    """Um pedido HTTP ao Moodle falhou.

    A mensagem tem só o método, o caminho e o código HTTP. O `requests` põe o
    URL completo, com a query, no texto de `HTTPError` e `ConnectionError`, e o
    token vai na query dos downloads; como o texto das exceções chega ao modelo
    (e ao histórico da sessão), nunca pode transportar o URL.
    """


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Faz um pedido HTTP e converte qualquer falha numa exceção sem segredos.

    Args:
        method: Método HTTP.
        url: URL sem segredos na query (os segredos vão em `params`/`data`).
        **kwargs: Passados a `requests.request`.

    Returns:
        A resposta, já com `raise_for_status()` verificado.

    Raises:
        MoodleRequestError: Em qualquer falha de rede ou código HTTP de erro.
    """
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        # `from None`: a exceção original tem o URL com o token no texto.
        raise MoodleRequestError(f"{method.upper()} {urlparse(url).path} falhou: {detail}") from None
    return response


#: Caminhos de ficheiro do Moodle aos quais o token pode ser acrescentado.
_FILE_ENDPOINTS = ("/webservice/pluginfile.php/", "/tokenpluginfile.php/", "/pluginfile.php/")

_DEFAULT_PORTS = {"https": 443, "http": 80}


def _validate_file_url(file_url: str, base_url: str) -> str:
    """Garante que um URL de ficheiro pertence à instância Moodle configurada.

    O URL é interpretado depois de preparado pelo `requests`, ou seja, pelo
    mesmo parser que vai abrir a ligação. Comparar com o `urlparse` não chega:
    para `https://evil.example\\@moodle/x` o `urlparse` lê o anfitrião
    `moodle`, mas o `requests` liga-se a `evil.example`.

    Args:
        file_url: O `fileurl` recebido (da API do Moodle ou do modelo).
        base_url: URL base da instância.

    Returns:
        O URL tal como o `requests` o vai enviar.

    Raises:
        ValueError: Se o esquema, o anfitrião, a porta ou o caminho não forem
            os de um ficheiro desta instância.
    """
    refused = ValueError(
        "Recusado: o URL não é um ficheiro (pluginfile.php) da instância Moodle configurada "
        f"({base_url or 'sem URL'}); o token só é enviado para essa instância. Se o Moodle usa "
        "outro endereço (http/https, www.), corrige o URL da instância na configuração."
    )
    try:
        prepared = requests.Request("GET", file_url.strip()).prepare().url or ""
        target, base = parse_url(prepared), parse_url(base_url)
    except (requests.RequestException, ValueError):
        raise refused from None

    def host(parsed: Any) -> str:
        return (parsed.host or "").rstrip(".").lower()

    def port(parsed: Any) -> int | None:
        return parsed.port or _DEFAULT_PORTS.get(parsed.scheme or "")

    if (
        not host(base)
        or target.auth
        or target.scheme != base.scheme
        or host(target) != host(base)
        or port(target) != port(base)
    ):
        raise refused

    path = target.path or ""
    if ".." in unquote(path).split("/"):
        raise refused
    base_path = (base.path or "").rstrip("/")
    if not any(path.startswith(base_path + endpoint) for endpoint in _FILE_ENDPOINTS):
        raise refused
    return prepared


def _authed_get(file_url: str, instance: str, **kwargs: Any) -> requests.Response:
    """Descarrega um ficheiro autenticado do Moodle — único sítio onde o token vai num URL.

    Valida o URL contra a instância antes de juntar o token, e passa-o em
    `params` (nunca concatenado). Um redirecionamento não leva o token: o
    `requests` constrói o pedido seguinte a partir do `Location`.

    Args:
        file_url: O `fileurl` devolvido pela API do Moodle.
        instance: Nome da instância.
        **kwargs: Passados a `requests.request` (`stream`, `timeout`, ...).

    Returns:
        A resposta HTTP.

    Raises:
        ValueError: Se o URL não pertencer à instância ou não houver token.
        MoodleRequestError: Se o pedido falhar.
    """
    base_url, token = _resolve_target(instance)
    if not token:
        raise ValueError(f"Token para '{instance}' não está configurado.")
    url = _validate_file_url(file_url, base_url)
    return _request("GET", url, params={"token": token}, **kwargs)


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
_TRUSTED_WSFUNCTIONS = frozenset(
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
_untrusted_content_seen = False


def _mark_untrusted_content_seen() -> None:
    global _untrusted_content_seen
    _untrusted_content_seen = True


def _confirm_mode() -> str:
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


async def _require_confirmation(ctx: Context | None, action: str, details: dict[str, Any]) -> None:
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
    mode = _confirm_mode()
    if mode == "client" or (mode == "untrusted" and not _untrusted_content_seen):
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
        raise PermissionError(f"Ação recusada: o pedido de confirmação falhou ({exc}). {hint}") from None
    if result.action != "accept" or not getattr(result.data, "confirmar", False):
        raise PermissionError(
            "Ação cancelada: o utilizador não a confirmou. Não voltes a tentar sem um novo pedido explícito."
        )


#: Funções de leitura pelo nome (convenção da API do Moodle); as restantes podem escrever.
_READ_ONLY_WSFUNCTION = re.compile(r"_(get|search|list|count|check)_|_(get|search|list)$")


def _describe_user(user_id: int, instance: str) -> str:
    """Devolve `"<id> (<nome>)"` para o pedido de confirmação, ou só o id se falhar.

    Mostrar o nome é o que permite reparar num destinatário que o estudante não
    escolheu.
    """
    try:
        found = _moodle_call(
            "core_user_get_users_by_field", {"field": "id", "values[0]": user_id}, instance=instance
        )
        if isinstance(found, list) and found:
            return f"{user_id} ({found[0].get('fullname', '?')})"
    except Exception:  # o nome é informativo; a confirmação continua sem ele
        pass
    return f"{user_id} (nome desconhecido — confirma o id)"


def _ws_call(
    base_url: str,
    token: str,
    wsfunction: str,
    params: dict[str, Any] | None = None,
    method: str = "POST",
) -> Any:
    """Chama uma função REST do Moodle com um URL e um token explícitos."""
    payload: dict[str, Any] = {
        "wstoken": token,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
    }
    if params:
        payload.update(params)

    url = f"{base_url}/webservice/rest/server.php"
    if method.upper() == "GET":
        response = _request("GET", url, params=payload, timeout=30)
    else:
        response = _request("POST", url, data=payload, timeout=30)
    if wsfunction not in _TRUSTED_WSFUNCTIONS:
        _mark_untrusted_content_seen()
    # Redação de segredos e limpeza de conteúdo escondido no único ponto por
    # onde passam todas as respostas da API.
    return sanitize.clean_payload(_redact_secrets(response.json()))


def _moodle_call(
    wsfunction: str,
    params: dict[str, Any] | None = None,
    method: str = "POST",
    instance: str = DEFAULT_INSTANCE,
) -> Any:
    """Chama uma função REST do Moodle na instância indicada.

    Raises:
        MoodleAPIError: Se não houver token ou se o Moodle responder com uma
            exceção. O Moodle devolve os erros com HTTP 200 e um corpo
            `{"exception": ...}`; devolvê-los como dados faria o modelo tratá-los
            como resultado (e, por vezes, inventar um). Como exceção, chegam ao
            cliente como `isError`, que é o que a spec MCP recomenda.
        MoodleRequestError: Se o pedido HTTP falhar.
    """
    base_url, token = _resolve_target(instance)
    if not token:
        raise MoodleAPIError(
            "notoken",
            f"A instância '{instance}' não tem token configurado. {_login_hint(base_url)}",
        )
    result = _ws_call(base_url, token, wsfunction, params, method)
    if isinstance(result, dict) and "exception" in result:
        raise _api_error(result, wsfunction, instance, base_url)
    return result


class MoodleAPIError(RuntimeError):
    """O Moodle recusou o pedido (resposta `{"exception": ...}` ou falta de token).

    Attributes:
        errorcode: O `errorcode` do Moodle, p.ex. `invalidtoken`.
    """

    def __init__(self, errorcode: str, message: str) -> None:
        """Guarda o `errorcode` ao lado da mensagem acionável."""
        super().__init__(message)
        self.errorcode = errorcode


#: Erros que nenhuma outra chamada vai resolver: a sincronização pára logo.
_FATAL_ERRORCODES = frozenset({"notoken", "invalidtoken", "servicerequireslogin", "usernotfullysetup"})


def _login_hint(base_url: str) -> str:
    return (
        "Faz login de novo: com o handler instalado (`moodle-mcp --install-handler`), abre no "
        f"navegador {base_url or '<url-do-moodle>'}/admin/tool/mobile/launch.php"
        "?service=moodle_mobile_app&passport=12345"
    )


def _api_error(result: dict[str, Any], wsfunction: str, instance: str, base_url: str) -> MoodleAPIError:
    """Converte uma resposta de erro do Moodle numa mensagem acionável."""
    code = str(result.get("errorcode") or result.get("exception") or "desconhecido")
    detail = str(result.get("message") or "")
    if code == "invalidtoken":
        message = (
            f"O Moodle recusou o token de '{instance}' (expirou ou foi revogado). {_login_hint(base_url)}"
        )
    elif code in {"accessexception", "webservicefunctionnotavailable", "nopermissions", "requireloginerror"}:
        message = (
            f"A função `{wsfunction}` não está disponível para a tua conta em '{instance}' "
            f"({code}: {detail}). O administrador do Moodle não a ativou no serviço da app móvel."
        )
    else:
        message = f"O Moodle devolveu um erro em `{wsfunction}` ({code}): {detail}"
    return MoodleAPIError(code, message)


def _soft(
    call: Any,
    *args: Any,
    default: Any = None,
    errors: list[dict[str, str]] | None = None,
    what: str = "",
    **kwargs: Any,
) -> Any:
    """Corre uma chamada opcional; um erro dela não deve parar o resto.

    Erros fatais (token inválido ou em falta) voltam a ser levantados: repetir
    em cada disciplina só encheria o relatório com o mesmo problema.

    Args:
        call: Função a chamar com `*args` e `**kwargs`.
        *args: Argumentos posicionais de `call`.
        default: O valor a devolver se a chamada falhar.
        errors: Lista onde registar o erro, se dada.
        what: Descrição curta do que falhou, para o relatório.
        **kwargs: Argumentos nomeados de `call`.

    Returns:
        O resultado da chamada, ou `default`.
    """
    try:
        return call(*args, **kwargs)
    except MoodleAPIError as exc:
        if exc.errorcode in _FATAL_ERRORCODES:
            raise
        if errors is not None:
            errors.append({"item": what, "error": str(exc)})
    except (MoodleRequestError, ValueError) as exc:
        if errors is not None:
            errors.append({"item": what, "error": str(exc)})
    return default


def _get_current_user_id(instance: str = DEFAULT_INSTANCE) -> int:
    info = _moodle_call("core_webservice_get_site_info", instance=instance)
    if isinstance(info, dict) and "userid" in info:
        return int(info["userid"])
    raise MoodleAPIError("nouserid", f"O Moodle não devolveu o userid da sessão ({instance}).")


def _html_to_markdown(html_text: str) -> str:
    if not html_text:
        return ""
    html_text = sanitize.clean_text(html_text)
    try:
        converted = md_convert(html_text, heading_style="ATX").strip()
        return re.sub(r"\n{3,}", "\n\n", converted)
    except Exception:
        clean = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
        clean = re.sub(r"</p>", "\n\n", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", "", clean)
        return html.unescape(clean).strip()


def _write_note(path: Path, markdown: str) -> None:
    """Escreve uma nota no Vault com o conteúdo ativo do Obsidian desativado.

    Todas as notas geradas a partir do Moodle passam por aqui: incluem nomes,
    posts e descrições escritos por terceiros, e um bloco `dataviewjs` ou uma
    imagem remota num post não pode executar código nem chamar um servidor
    quando o estudante abre a nota.
    """
    config.atomic_write(path, sanitize.neutralize_markdown(markdown), 0o644)


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_MAX_NAME = 120


def _safe_filename(name: str) -> str:
    """Converte um nome vindo do Moodle num nome de ficheiro válido em todos os sistemas.

    * Retira separadores, caracteres proibidos no Windows e de controlo.
    * Recusa `.`/`..`/vazio, que mudariam de diretório ou não teriam nome.
    * Retira pontos e espaços finais (o Windows descarta-os em silêncio).
    * Prefixa nomes reservados do Windows (`CON`, `NUL`, `COM1`…), com ou sem extensão.
    * Trunca o nome, não a extensão: `aula.pdf` com 200 caracteres continua a
      acabar em `.pdf` e a ter o texto extraído.
    """
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f\x7f]+', "-", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    if not cleaned.strip("."):
        return "_"
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot or not stem or len(suffix) > 10 or " " in suffix:
        stem, suffix = cleaned, ""
    if len(cleaned) > _MAX_NAME:
        keep = _MAX_NAME - (len(suffix) + 1 if suffix else 0)
        stem = stem[:keep].rstrip(". ")
        cleaned = f"{stem}.{suffix}" if suffix else stem
    if (stem if suffix else cleaned).split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _unique_path(path: Path, used: set[Path]) -> Path:
    """Devolve um caminho ainda não usado nesta execução, com ` (2)`, ` (3)`… se preciso.

    Dois módulos com o mesmo nome (ou todas as páginas `index.html` de uma
    secção) escreviam no mesmo ficheiro e só o último sobrevivia.
    """
    candidate, counter = path, 2
    while candidate in used:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    used.add(candidate)
    return candidate


# ==============================================================================
# 1. AUTENTICAÇÃO, TOKENS & SSO FEDERADO
# ==============================================================================


@mcp.tool(annotations=_ann("Guardar token do Moodle", destructive=True))
def set_moodle_token(
    token: Annotated[
        str, Field(description="O `wstoken` do Web Service do Moodle (32 caracteres hexadecimais).")
    ],
    instance: Inst = DEFAULT_INSTANCE,
    moodle_url: Annotated[
        str,
        Field(
            description=(
                "URL base (https://) de uma instância que ainda não tenha URL configurado; "
                "vazio usa o atual. Não muda o URL de uma instância existente."
            )
        ),
    ] = "",
) -> dict[str, Any]:
    """Valida um token Moodle e, só se for válido, guarda-o em `credentials.json`.

    Aviso de segurança: o token entra por argumento, pelo que é enviado para o
    provider de LLM configurado no cliente e fica no histórico da sessão. Para
    configuração inicial, preferir o capturador `moodlemobile://`. A resposta
    nunca devolve o token, apenas a sua impressão digital.

    O URL de uma instância já configurada não pode ser mudado por aqui: se
    pudesse, uma instrução escondida num fórum bastaria para apontar a instância
    para outro servidor, e um token definido no ambiente (que tem precedência)
    passaria a ser enviado para lá em todas as chamadas.

    Args:
        token: O `wstoken` do Web Service do Moodle.
        instance: Nome da instância a configurar; vazio usa a predefinida.
        moodle_url: URL base de uma instância ainda sem URL.

    Returns:
        Dicionário com `saved`, `saved_to`, `instance`, `token_fingerprint` e o
        `site_info` devolvido pelo Moodle (já redigido), ou `error`/`message`
        se o token não for gravado.
    """
    clean_token = token.strip()
    name = (instance or DEFAULT_INSTANCE).strip().lower()
    current = config.instances().get(name)
    current_url = current.url if current else ""

    if moodle_url:
        new_url = _validate_instance_url(moodle_url)
        if current_url and current_url.rstrip("/").lower() != new_url.lower():
            return {
                "error": True,
                "saved": False,
                "instance": name,
                "message": (
                    f"Recusado: a instância '{name}' já tem URL ({current_url}). Mudar o URL de uma "
                    "instância existente só é possível na configuração (config.toml ou "
                    f"MOODLE_{name.upper()}_URL), não por uma ferramenta."
                ),
            }
        base_url = new_url
    elif current_url:
        base_url = current_url
    else:
        return {
            "error": True,
            "saved": False,
            "instance": name,
            "message": f"A instância '{name}' não tem URL configurado; indica `moodle_url` (https://).",
        }

    # Valida o token NOVO, contra o URL que vai ficar gravado, ANTES de gravar:
    # um token errado não pode substituir um que funciona.
    site_info = _ws_call(base_url, clean_token, "core_webservice_get_site_info")
    if not isinstance(site_info, dict) or "exception" in site_info or "userid" not in site_info:
        return {
            "error": True,
            "saved": False,
            "instance": name,
            "token_fingerprint": _token_fingerprint(clean_token),
            "message": "O Moodle recusou o token; nada foi gravado.",
            "details": site_info,
        }

    if moodle_url and not current_url:
        config.save_instance_url(name, base_url)
    saved_to = str(config.save_instance_token(name, clean_token))

    result: dict[str, Any] = {
        "saved": True,
        "saved_to": saved_to,
        "instance": name,
        "token_fingerprint": _token_fingerprint(clean_token),
        "site_info": site_info,
    }
    if config.resolve_instance(name).token != clean_token:
        result["warning"] = (
            "O token foi gravado, mas outro definido no ambiente ou no .env legado tem precedência "
            f"e continua a ser usado. Remove MOODLE_{name.upper()}_TOKEN de lá para usar o novo."
        )
    return result


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _validate_instance_url(url: str) -> str:
    """Normaliza o URL base de uma instância e exige HTTPS.

    HTTP só é aceite para `localhost`, para instâncias de desenvolvimento: o
    token vai em todos os pedidos e não pode circular em claro.

    Args:
        url: URL base proposto.

    Returns:
        O URL sem barra final.

    Raises:
        ValueError: Se o URL não for HTTPS, não tiver anfitrião ou trouxer
            credenciais, query ou fragmento.
    """
    clean = url.strip().rstrip("/")
    try:
        parsed = parse_url(requests.Request("GET", clean).prepare().url or "")
    except (requests.RequestException, ValueError):
        raise ValueError(f"URL de instância inválido: {clean!r}") from None
    host = (parsed.host or "").lower()
    secure = parsed.scheme == "https" or (parsed.scheme == "http" and host in _LOCAL_HOSTS)
    if not host or not secure or parsed.auth or parsed.query or parsed.fragment:
        raise ValueError(
            f"URL de instância recusado: {clean!r}. Tem de ser https://<anfitrião>[/caminho], "
            "sem credenciais, query ou fragmento."
        )
    return clean


@mcp.tool(annotations=_ann("Capturar token via moodlemobile://", destructive=True))
def decode_mobile_launch_token(
    launch_url_or_base64: Annotated[
        str, Field(description="O URL `moodlemobile://token=...` completo, ou apenas a parte em Base64.")
    ],
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """Descodifica um URL `moodlemobile://token=...` e guarda o `wstoken` no `.env`.

    O URL é obtido em
    `/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345`.

    A resposta nunca inclui o payload descodificado nem o token: ambos iriam para
    o provider de LLM configurado no cliente e ficariam no histórico da sessão.
    Devolve apenas a impressão digital do token e a validação da ligação.

    Args:
        launch_url_or_base64: O URL `moodlemobile://token=...` completo ou só a
            parte em Base64.
        instance: Nome da instância a configurar; vazio usa a predefinida.

    Returns:
        Dicionário com `saved`, `instance`, `token_fingerprint`, `has_privatetoken`
        e `validation` (o resultado de `set_moodle_token`).
    """
    raw = launch_url_or_base64.strip()
    if "token=" in raw:
        raw = raw.split("token=", 1)[1].split("&", 1)[0]
    # Alguns navegadores entregam o payload com percent-encoding (%2B, %2F, %3D).
    raw = unquote(raw).strip()
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", errors="ignore")
    except (binascii.Error, ValueError):
        decoded = ""
    parts = decoded.split(":::")
    if len(parts) < 2 or not parts[1].strip():
        return {
            "error": True,
            "saved": False,
            "message": (
                "O payload descodificado não tem o formato esperado "
                "`passport:::wstoken[:::privatetoken]`. Confirma que o URL veio de "
                "`/admin/tool/mobile/launch.php`. (O conteúdo descodificado não é "
                "mostrado por poder conter credenciais.)"
            ),
        }

    token = parts[1].strip()
    validation = set_moodle_token(token, instance=instance)
    return {
        "saved": bool(validation.get("saved")),
        "instance": instance,
        "token_fingerprint": _token_fingerprint(token),
        "has_privatetoken": len(parts) >= 3 and bool(parts[2].strip()),
        "validation": validation,
        **({"message": validation["message"]} if "message" in validation else {}),
    }


@mcp.tool(annotations=_ann("Extrair token de um cookie", destructive=True))
def extract_token_from_session_cookie(
    moodle_session_cookie: Annotated[
        str, Field(description="Valor do cookie `MoodleSession` de uma sessão de browser ativa.")
    ],
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """Extrai o token do Moodle a partir de um cookie `MoodleSession` ativo.

    Tenta `/user/managetoken.php` e, em alternativa, `/admin/tool/mobile/launch.php`.
    O cookie é visível em F12 -> Storage/Application -> Cookies -> MoodleSession.

    Aviso de segurança: o cookie de sessão entra por argumento, logo é enviado
    para o provider de LLM configurado no cliente e fica no histórico da sessão.
    Preferir o capturador `moodlemobile://`, que não passa pelo canal MCP. A
    resposta nunca devolve o token nem o cookie.

    Args:
        moodle_session_cookie: Valor do cookie `MoodleSession`.
        instance: Nome da instância a configurar; vazio usa a predefinida.

    Returns:
        O resultado de `set_moodle_token` (com impressão digital, sem o token), ou
        um dicionário de erro se a sessão já não estiver ativa.
    """
    base_url, _ = _resolve_target(instance)
    session = requests.Session()
    session.cookies.set("MoodleSession", moodle_session_cookie.strip(), domain=urlparse(base_url).hostname)

    # 1. Try /user/managetoken.php
    resp = session.get(f"{base_url}/user/managetoken.php", timeout=20)
    if resp.ok and "login" not in resp.url.lower():
        soup = BeautifulSoup(resp.text, "html.parser")
        # Find 32-char hex tokens in table cells
        candidates = re.findall(r"\b[a-f0-9]{32}\b", soup.get_text(" "))
        if candidates:
            token = candidates[0]
            return set_moodle_token(token, instance=instance)

    # 2. Try /admin/tool/mobile/launch.php
    launch_resp = session.get(
        f"{base_url}/admin/tool/mobile/launch.php",
        params={"service": "moodle_mobile_app", "passport": "12345"},
        allow_redirects=False,
        timeout=20,
    )
    loc = launch_resp.headers.get("Location", "")
    if "token=" in loc:
        return decode_mobile_launch_token(loc, instance=instance)

    return {
        "error": True,
        "status_code": resp.status_code,
        "redirected_to_login": "login" in resp.url.lower(),
        "message": (
            "Não foi possível extrair o token automaticamente com este MoodleSession. "
            "Verifica se a sessão ainda está ativa."
        ),
    }


@mcp.tool(annotations=_ann("Chamar função arbitrária da API", destructive=True, idempotent=False))
async def call_any_moodle_wsfunction(
    wsfunction: Annotated[
        str, Field(description="Nome da função de Web Service, ex.: `core_user_get_users_by_field`.")
    ],
    params_json: Annotated[
        str,
        Field(description="Parâmetros em JSON. Arrays usam a sintaxe do Moodle, ex.: {'courseids[0]': 42}."),
    ] = "{}",
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
) -> Any:
    """
    Ferramenta universal de baixo nível: permite chamar QUALQUER função da API Web Services do Moodle
    (ex.: `core_user_get_users_by_field`, `mod_glossary_get_entries_by_letter`, etc.) passando parâmetros em JSON.
    """
    params = json.loads(params_json) if params_json else {}
    if not _READ_ONLY_WSFUNCTION.search(wsfunction):
        await _require_confirmation(
            ctx,
            "Chamar uma função da API do Moodle que pode alterar dados",
            {
                "Instância": instance,
                "Função": wsfunction,
                "Parâmetros": json.dumps(params, ensure_ascii=False),
            },
        )
    return _moodle_call(wsfunction, params, instance=instance)


# ==============================================================================
# 2. DISCIPLINAS, CONTEÚDOS, PÁGINAS HTML, PASTAS E PROGRESSO
# ==============================================================================


@mcp.tool(annotations=_ann("Ver informação do site", read_only=True))
def get_site_info(
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém informação do site Moodle, versão, funções ativas na API e dados do aluno autenticado."""
    return _moodle_call("core_webservice_get_site_info", instance=instance)


@mcp.tool(annotations=_ann("Listar disciplinas inscritas", read_only=True))
def get_enrolled_courses(
    classification: Annotated[
        str, Field(description="Filtro temporal: 'all', 'inprogress', 'future', 'past' ou 'favourites'.")
    ] = "all",
    instance: Inst = DEFAULT_INSTANCE,
):
    """
    Lista todas as disciplinas/unidades curriculares em que o aluno está inscrito.
    `classification` pode ser: 'all', 'inprogress', 'future', 'past', 'favourites'.
    """
    try:
        return _moodle_call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {"classification": classification},
            instance=instance,
        )
    except MoodleAPIError as exc:
        if exc.errorcode in _FATAL_ERRORCODES:
            raise
        # Moodles antigos não têm a função por linha temporal.
        userid = _get_current_user_id(instance=instance)
        return _moodle_call("core_enrol_get_users_courses", {"userid": userid}, instance=instance)


@mcp.tool(annotations=_ann("Ver conteúdos da disciplina", read_only=True))
def get_course_contents(
    course_id: CourseId,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém todos os tópicos, módulos, recursos, ficheiros, pastas e URLs de uma unidade curricular."""
    return _moodle_call("core_course_get_contents", {"courseid": course_id}, instance=instance)


@mcp.tool(annotations=_ann("Ver páginas e livros", read_only=True))
def get_course_pages_and_books(
    course_id: CourseId,
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """
    Obtém todas as Páginas HTML (`mod_page`), Pastas (`mod_folder`), Recursos (`mod_resource`)
    e Livros (`mod_book`, muito usados em contratos de aprendizagem e guias) de uma disciplina.
    """
    pages = _moodle_call("mod_page_get_pages_by_courses", {"courseids[0]": course_id}, instance=instance)
    folders = _moodle_call(
        "mod_folder_get_folders_by_courses", {"courseids[0]": course_id}, instance=instance
    )
    resources = _moodle_call(
        "mod_resource_get_resources_by_courses", {"courseids[0]": course_id}, instance=instance
    )
    books = _moodle_call("mod_book_get_books_by_courses", {"courseids[0]": course_id}, instance=instance)
    return {
        "pages": pages,
        "folders": folders,
        "resources": resources,
        "books": books,
    }


@mcp.tool(annotations=_ann("Ver progresso de atividades", read_only=True))
def get_activities_completion_status(
    course_id: CourseId,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém o estado de conclusão (checkboxes de progresso) de todas as atividades de uma disciplina."""
    uid = _get_current_user_id(instance=instance)
    return _moodle_call(
        "core_completion_get_activities_completion_status",
        {"courseid": course_id, "userid": uid},
        instance=instance,
    )


@mcp.tool(annotations=_ann("Marcar atividade como concluída"))
async def toggle_activity_completion(
    cmid: Annotated[
        int,
        Field(
            description="ID do módulo de curso (cmid): o campo `id` dos módulos em\nget_course_contents, NÃO o `instance`."
        ),
    ],
    completed: Annotated[
        bool, Field(description="True marca a atividade como concluída; False desmarca.")
    ] = True,
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
):
    """
    Marca ou desmarca manualmente uma atividade/recurso da disciplina como concluída (`core_completion_update_activity_completion_status_manually`).
    `cmid` é o ID do módulo do curso (course module id).
    """
    await _require_confirmation(
        ctx,
        "Marcar atividade como concluída" if completed else "Desmarcar atividade como concluída",
        {"Instância": instance, "cmid": cmid},
    )
    return _moodle_call(
        "core_completion_update_activity_completion_status_manually",
        {"cmid": cmid, "completed": 1 if completed else 0},
        instance=instance,
    )


@mcp.tool(annotations=_ann("Listar participantes", read_only=True))
def get_course_participants(
    course_id: CourseId,
    limit: Annotated[int, Field(description="Número máximo de participantes a devolver.")] = 100,
    instance: Inst = DEFAULT_INSTANCE,
):
    """
    Lista os participantes de uma disciplina (professores, tutores e colegas de doutoramento).
    Útil para identificar o ID do professor ou colegas de grupo.
    """
    return _moodle_call(
        "core_enrol_get_enrolled_users",
        {
            "courseid": course_id,
            "options[0][name]": "limitnumber",
            "options[0][value]": limit,
        },
        instance=instance,
    )


# ==============================================================================
# 3. DOWNLOAD DE PDFS, SLIDES, ENUNCIADOS E CONVERSÃO PARA MARKDOWN
# ==============================================================================


@mcp.tool(annotations=_ann("Descarregar ficheiro para o Vault"))
def download_moodle_file(
    file_url: Annotated[
        str,
        Field(
            description=(
                "O `fileurl` (pluginfile.php) devolvido pela API do Moodle, sem token. "
                "Só são aceites ficheiros da instância configurada."
            )
        ),
    ],
    dest_path: Annotated[
        str,
        Field(
            description="Destino do ficheiro. Relativo resolve a partir da raiz do\nVault; tem de ficar dentro do Vault."
        ),
    ],
    extract_pdf_to_markdown: Annotated[
        bool, Field(description="Se True e o ficheiro for PDF, extrai o texto para um `.md` irmão.")
    ] = True,
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """Descarrega um ficheiro autenticado do Moodle para dentro do Vault.

    Se for um PDF e `extract_pdf_to_markdown=True`, extrai todo o texto com
    PyMuPDF para um `.md` ao lado do PDF, pesquisável no Obsidian.

    `dest_path` tem de ficar dentro de `OBSIDIAN_VAULT_DIR`: um caminho relativo
    resolve a partir da raiz do Vault e qualquer caminho que escape é recusado.

    Args:
        file_url: O `fileurl` devolvido pela API do Moodle (sem token).
        dest_path: Destino, absoluto dentro do Vault ou relativo à raiz do Vault.
        extract_pdf_to_markdown: Se extrai o texto de PDFs para um `.md` irmão.
        instance: Nome da instância Moodle; vazio usa a predefinida.

    Returns:
        Dicionário com `downloaded_file`, `size_bytes` e, para PDFs,
        `extracted_markdown` e `pdf_pages`.

    Raises:
        ValueError: Se `dest_path` cair fora do Vault ou se `file_url` não for um
            ficheiro da instância configurada (o token nunca sai para outro anfitrião).
        MoodleRequestError: Se o download falhar.
    """
    target = _resolve_in_vault(dest_path)
    _download_to(target, file_url, instance)

    result: dict[str, Any] = {
        "downloaded_file": str(target),
        "size_bytes": target.stat().st_size,
    }

    if extract_pdf_to_markdown and target.suffix.lower() == ".pdf" and fitz is None:
        result["pdf_extraction_warning"] = (
            "Extração de PDF indisponível: instala o extra com "
            "`pip install 'moodle-mcp[pdf]'` (PyMuPDF, AGPL-3.0)."
        )
    elif extract_pdf_to_markdown and target.suffix.lower() == ".pdf":
        try:
            doc = fitz.open(str(target))
            md_pages = [
                "---",
                f'source_pdf: "{target.name}"',
                f"pages: {len(doc)}",
                f"extracted_at: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                f"tags: [moodle, pdf-extraido, {instance}]",
                "---",
                "",
                f"# 📄 Texto Extraído: {target.stem}",
                "",
            ]
            for idx, page in enumerate(cast(Any, doc), start=1):
                text = sanitize.strip_invisible(page.get_text("text")).strip()
                if text:
                    md_pages.append(f"## Página {idx}\n\n{text}\n")
            n_pages = len(doc)
            doc.close()
            md_path = target.with_suffix(".pdf.md")
            _write_note(md_path, "\n".join(md_pages))
            result["extracted_markdown"] = str(md_path)
            result["pdf_pages"] = n_pages
        except Exception as e:
            result["pdf_extraction_warning"] = str(e)

    return result


def _max_download_bytes() -> int:
    try:
        return int(float(config.get("max_download_mb")) * 1024 * 1024)
    except ValueError:
        return 500 * 1024 * 1024


def _download_to(target: Path, file_url: str, instance: str) -> None:
    """Descarrega para um temporário e só no fim substitui o destino.

    Um download que falhe a meio (rede, timeout, limite de tamanho) deixa o
    ficheiro anterior intacto, em vez de um PDF truncado por cima do bom. O
    limite (`MOODLE_MCP_MAX_DOWNLOAD_MB`, 500 por omissão) impede que um
    ficheiro enorme, ou um servidor que nunca pare de enviar, encha o disco.

    Raises:
        ValueError: Se o ficheiro exceder o limite de tamanho.
        MoodleRequestError: Se o pedido falhar.
    """
    limit = _max_download_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.part")
    try:
        with _authed_get(file_url, instance, stream=True, timeout=60) as r:
            declared = int(r.headers.get("Content-Length") or 0)
            if declared > limit:
                raise ValueError(f"Ficheiro com {declared} bytes excede o limite de {limit} bytes.")
            written = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    written += len(chunk)
                    if written > limit:
                        raise ValueError(f"Ficheiro excede o limite de {limit} bytes.")
                    f.write(chunk)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


@mcp.tool(annotations=_ann("Descarregar materiais da UC"))
def download_all_course_materials_to_obsidian(
    course_id: CourseId,
    extract_pdfs: Annotated[
        bool, Field(description="Se converte cada PDF descarregado num `.md` pesquisável no Obsidian.")
    ] = True,
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """
    Descarrega TODOS os ficheiros (PDFs, enunciados de e-fólios, slides, textos de apoio e páginas HTML)
    de uma disciplina diretamente para a pasta da Unidade Curricular dentro do teu Vault do Obsidian
    (`<Vault>/Unidades Curriculares/<Nome da UC>/Materiais/`),
    convertendo também os PDFs e Páginas HTML em notas Markdown pesquisáveis!
    """
    contents = get_course_contents(course_id, instance=instance)
    if not isinstance(contents, list):
        return {"error": True, "details": contents}

    cname = _course_folder_name(course_id, instance)

    base_uc_dir = _resolve_in_vault(Path("Unidades Curriculares") / cname / "Materiais")
    base_uc_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    errors: list[dict[str, str]] = []
    used: set[Path] = set()

    for section in contents:
        sec_name = _safe_filename(section.get("name") or f"Topico_{section.get('section', 0)}")
        sec_dir = base_uc_dir / sec_name
        for mod in section.get("modules", []):
            mname = _safe_filename(mod.get("name", "Recurso"))
            for content_item in mod.get("contents", []):
                ctype = content_item.get("type")
                fname = _safe_filename(content_item.get("filename") or f"{mname}.bin")
                furl = content_item.get("fileurl")
                if ctype == "file" and furl:
                    try:
                        if fname.lower().endswith((".html", ".htm")):
                            r = _authed_get(furl, instance, timeout=30)
                            sec_dir.mkdir(parents=True, exist_ok=True)
                            # Cada página do Moodle chama-se `index.html`: o nome útil é o do módulo.
                            stem = mname if Path(fname).stem.lower() == "index" else Path(fname).stem
                            md_dest = _unique_path(sec_dir / f"{_safe_filename(stem)}.md", used)
                            _write_note(md_dest, f"# {mname}\n\n{_html_to_markdown(r.text)}\n")
                            downloaded.append(str(md_dest))
                        else:
                            res = download_moodle_file(
                                furl,
                                str(_unique_path(sec_dir / fname, used)),
                                extract_pdf_to_markdown=extract_pdfs,
                                instance=instance,
                            )
                            downloaded.append(res["downloaded_file"])
                    except Exception as e:
                        errors.append({"file": fname, "error": str(e)})

    # Enunciados anexados aos trabalhos (e-fólios)
    assign_resp = _soft(get_assignments, instance=instance, default={}, errors=errors, what="trabalhos")
    if isinstance(assign_resp, dict):
        for c in assign_resp.get("courses", []):
            if int(c.get("id", 0)) == int(course_id):
                efolios_dir = base_uc_dir / "Enunciados_Efolios"
                for a in c.get("assignments", []):
                    aname = _safe_filename(a.get("name", "Trabalho"))
                    for att in a.get("introattachments", []) + a.get("introfiles", []):
                        furl = att.get("fileurl")
                        fname = _safe_filename(att.get("filename", "enunciado.pdf"))
                        if furl:
                            try:
                                res = download_moodle_file(
                                    furl,
                                    str(_unique_path(efolios_dir / _safe_filename(f"{aname}_{fname}"), used)),
                                    extract_pdf_to_markdown=extract_pdfs,
                                    instance=instance,
                                )
                                downloaded.append(res["downloaded_file"])
                            except Exception as e:
                                errors.append({"file": fname, "error": str(e)})

    return {
        "course_id": course_id,
        "course_name": cname,
        "materials_dir": str(base_uc_dir),
        "downloaded_count": len(downloaded),
        "downloaded_files": downloaded,
        "errors": errors,
    }


# ==============================================================================
# 4. AVALIAÇÕES, NOTAS, E-FÓLIOS, TESTES/QUIZZES E SUBMISSÕES
# ==============================================================================


@mcp.tool(annotations=_ann("Listar trabalhos e e-fólios", read_only=True))
def get_assignments(
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém todos os trabalhos / e-fólios / p-fólios, descrições, ficheiros de enunciado e prazos de entrega."""
    return _moodle_call("mod_assign_get_assignments", instance=instance)


@mcp.tool(annotations=_ann("Ver estado de uma submissão", read_only=True))
def get_assignment_submission(
    assign_id: Annotated[
        int, Field(description="ID do trabalho — o campo `id` de get_assignments, NÃO o cmid.")
    ],
    user_id: UserId = 0,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém o estado detalhado da submissão, nota, rubrica e comentários de feedback de um trabalho/e-fólio."""
    uid = user_id or _get_current_user_id(instance=instance)
    return _moodle_call(
        "mod_assign_get_submission_status",
        {"assignid": assign_id, "userid": uid},
        instance=instance,
    )


@mcp.tool(annotations=_ann("Ver notas da disciplina", read_only=True))
def get_course_grades(
    course_id: CourseId,
    user_id: UserId = 0,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém todas as notas e itens de avaliação (e-fólios A/B/C, Global, p-fólio) de uma disciplina específica."""
    uid = user_id or _get_current_user_id(instance=instance)
    return _moodle_call(
        "gradereport_user_get_grade_items",
        {"courseid": course_id, "userid": uid},
        instance=instance,
    )


@mcp.tool(annotations=_ann("Ver resumo de todas as notas", read_only=True))
def get_all_grades_overview(
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém o resumo de notas finais/atuais de todas as disciplinas em que o aluno está inscrito."""
    return _moodle_call("gradereport_overview_get_course_grades", instance=instance)


@mcp.tool(annotations=_ann("Listar testes", read_only=True))
def get_course_quizzes(
    course_id: CourseId,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Lista todos os questionários / testes online (`mod_quiz`) de uma disciplina e as tuas tentativas/notas."""
    quizzes = _moodle_call("mod_quiz_get_quizzes_by_courses", {"courseids[0]": course_id}, instance=instance)
    return quizzes


@mcp.tool(annotations=_ann("Enviar ficheiro para rascunho", idempotent=False))
async def upload_file_to_draft(
    file_path: Annotated[
        str,
        Field(description="Ficheiro a enviar, dentro do Vault. Relativo resolve a\npartir da raiz do Vault."),
    ],
    item_id: Annotated[
        int, Field(description="`itemid` de um draft existente a reutilizar; 0 cria um novo.")
    ] = 0,
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
) -> Any:
    """Envia um ficheiro do Vault para a área de rascunho (draft area) do Moodle.

    `file_path` tem de estar dentro de `OBSIDIAN_VAULT_DIR`: o upload sai da
    máquina para o servidor da universidade, pelo que ler um caminho arbitrário
    seria um canal de exfiltração (p.ex. `~/.ssh/id_rsa`). Coloca o e-fólio no
    Vault antes de o submeter.

    Args:
        file_path: Ficheiro a enviar, dentro do Vault (absoluto ou relativo à raiz).
        item_id: `itemid` do draft a reutilizar; 0 cria um novo.
        instance: Nome da instância Moodle; vazio usa a predefinida.
        ctx: Contexto MCP, injetado pelo FastMCP (não aparece na schema).

    Returns:
        A resposta do `upload.php`, que inclui o `itemid` a passar a
        `save_assignment_submission` ou `reply_to_forum_post`.

    Raises:
        ValueError: Se `file_path` cair fora do Vault.
    """
    base_url, token = _resolve_target(instance)
    path = _resolve_in_vault(file_path)
    if not path.exists():
        return {"error": True, "message": f"Ficheiro não encontrado: {path}"}

    if not token:
        return {"error": True, "message": f"Token para '{instance}' não está configurado."}

    await _require_confirmation(
        ctx,
        "Enviar um ficheiro do Vault para o servidor do Moodle",
        {
            "Instância": f"{instance} ({base_url})",
            "Ficheiro": str(path.relative_to(_vault_root())),
            "Tamanho": f"{path.stat().st_size} bytes",
        },
    )
    upload_url = f"{base_url}/webservice/upload.php"
    # O token vai no corpo multipart e não na query: o `upload.php` lê-o com
    # `required_param`, que aceita ambos, e assim não aparece em URLs nem em erros.
    fields: dict[str, Any] = {
        "token": token,
        "filearea": "draft",
        "itemid": item_id,
    }
    with path.open("rb") as f:
        files = {"file_1": (path.name, f)}
        response = _request("POST", upload_url, data=fields, files=files, timeout=90)
    return sanitize.clean_payload(_redact_secrets(response.json()))


@mcp.tool(annotations=_ann("Submeter trabalho / e-fólio", destructive=True, idempotent=False))
async def save_assignment_submission(
    assign_id: Annotated[int, Field(description="ID do trabalho (campo `id` de get_assignments).")],
    online_text_html: Annotated[
        str, Field(description="Texto da submissão em HTML (plugin 'online text'); vazio não envia texto.")
    ] = "",
    files_draft_item_id: Annotated[
        int, Field(description="`itemid` devolvido por upload_file_to_draft; 0 submete sem ficheiros.")
    ] = 0,
    submit_for_grading: Annotated[
        bool,
        Field(
            description="True entrega definitivamente para avaliação e é\nIRREVERSÍVEL. False apenas guarda o rascunho."
        ),
    ] = False,
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
) -> dict[str, Any]:
    """
    Guarda um trabalho/e-fólio no Moodle (texto online e/ou ficheiros previamente carregados com `upload_file_to_draft`).
    Por segurança, `submit_for_grading=False` guarda em modo rascunho/submissão editável;
    se `submit_for_grading=True`, lacra e submete em definitivo para avaliação.
    """
    await _require_confirmation(
        ctx,
        "ENTREGAR DEFINITIVAMENTE o trabalho para avaliação (deixa de ser editável)"
        if submit_for_grading
        else "Guardar o rascunho da submissão de um trabalho",
        {
            "Instância": instance,
            "assign_id": assign_id,
            "Texto": online_text_html or "(nenhum)",
            "Ficheiros (itemid do rascunho)": files_draft_item_id or "(nenhum)",
        },
    )
    params: dict[str, Any] = {"assignmentid": assign_id}
    if online_text_html:
        params["plugindata[onlinetext_editor][text]"] = online_text_html
        params["plugindata[onlinetext_editor][format]"] = 1
        params["plugindata[onlinetext_editor][itemid]"] = 0
    if files_draft_item_id:
        params["plugindata[files_filemanager]"] = files_draft_item_id

    save_res = _moodle_call("mod_assign_save_submission", params, instance=instance)
    result: dict[str, Any] = {"save_submission": save_res}

    if submit_for_grading:
        submit_res = _moodle_call(
            "mod_assign_submit_for_grading",
            {"assignmentid": assign_id, "acceptsubmissionstatement": 1},
            instance=instance,
        )
        result["submit_for_grading"] = submit_res

    return result


@mcp.tool(annotations=_ann("Comentar numa submissão", idempotent=False))
async def add_submission_comment(
    context_id: Annotated[int, Field(description="ID de contexto do módulo do trabalho no Moodle.")],
    item_id: Annotated[int, Field(description="ID do item a comentar (normalmente o id da submissão).")],
    content: Annotated[str, Field(description="Texto do comentário.")],
    area: Annotated[
        str, Field(description="Área de comentários do Moodle; raramente precisa de ser alterada.")
    ] = "submission_comments",
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
):
    """
    Adiciona um comentário oficial num trabalho/submissão (ex.: observação para o professor na entrega de um e-fólio).
    `context_id`: contextid do módulo do trabalho (`cmid` context).
    `item_id`: ID da submissão (`submission.id` obtido em `get_assignment_submission`).
    """
    await _require_confirmation(
        ctx,
        "Publicar um comentário numa submissão",
        {"Instância": instance, "Contexto": context_id, "Item": item_id, "Comentário": content},
    )
    return _moodle_call(
        "core_comment_add_comments",
        {
            "comments[0][contextlevel]": "module",
            "comments[0][instanceid]": context_id,
            "comments[0][component]": "assignsubmission_comments",
            "comments[0][content]": content,
            "comments[0][itemid]": item_id,
            "comments[0][area]": area,
        },
        instance=instance,
    )


# ==============================================================================
# 5. FÓRUNS, DEBATES ASSÍNCRONOS E MENSAGENS DIRETAS A PROFESSORES
# ==============================================================================


@mcp.tool(annotations=_ann("Listar fóruns", read_only=True))
def get_course_forums(
    course_id: CourseId,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Lista todos os fóruns (Fórum de Notícias, Fórum Central, Fóruns de Tópicos/Grupos) de uma disciplina."""
    return _moodle_call("mod_forum_get_forums_by_courses", {"courseids[0]": course_id}, instance=instance)


@mcp.tool(annotations=_ann("Listar tópicos de um fórum", read_only=True))
def get_forum_discussions(
    forum_id: Annotated[int, Field(description="ID do fórum (campo `id` de get_course_forums).")],
    instance: Inst = DEFAULT_INSTANCE,
):
    """Lista todos os tópicos de discussão dentro de um fórum específico."""
    return _moodle_call("mod_forum_get_forum_discussions", {"forumid": forum_id}, instance=instance)


@mcp.tool(annotations=_ann("Ler posts de um tópico", read_only=True))
def get_discussion_posts(
    discussion_id: Annotated[
        int, Field(description="ID do tópico (campo `discussion` de get_forum_discussions).")
    ],
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém todas as mensagens/respostas dentro de um tópico de discussão de um fórum."""
    return _moodle_call("mod_forum_get_discussion_posts", {"discussionid": discussion_id}, instance=instance)


@mcp.tool(annotations=_ann("Procurar nos fóruns", read_only=True))
def search_all_course_forums(
    course_id: CourseId,
    query: Annotated[
        str,
        Field(description="Texto a procurar no assunto e no corpo dos posts, sem distinção de maiúsculas."),
    ],
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """
    Pesquisa uma palavra-chave ou tema em TODOS os fóruns e posts de uma disciplina da UAb!
    Ideal para encontrar o que o professor ou colegas já responderam sobre um e-fólio ou artigo.
    """
    forums = get_course_forums(course_id, instance=instance)
    if not isinstance(forums, list):
        return {"error": True, "details": forums}

    q = query.lower().strip()
    matches = []
    for f in forums:
        fid = f.get("id")
        fname = f.get("name", f"Fórum {fid}")
        discs = _soft(get_forum_discussions, fid, instance=instance, default={})
        for d in discs.get("discussions", []) if isinstance(discs, dict) else []:
            did = d.get("discussion") or d.get("id")
            dname = d.get("name", "")
            posts_resp = _soft(get_discussion_posts, did, instance=instance, default={})
            posts = posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []
            for p in posts:
                subj = p.get("subject", "")
                msg = _html_to_markdown(p.get("message", ""))
                author = p.get("author", {}).get("fullname", "")
                if q in subj.lower() or q in msg.lower() or q in dname.lower():
                    matches.append(
                        {
                            "forum": fname,
                            "discussion_id": did,
                            "discussion": dname,
                            "post_id": p.get("id"),
                            "author": author,
                            "subject": subj,
                            "excerpt": msg[:600],
                        }
                    )
    return {"query": query, "course_id": course_id, "matches_count": len(matches), "matches": matches}


@mcp.tool(annotations=_ann("Responder num fórum", idempotent=False))
async def reply_to_forum_post(
    post_id: Annotated[
        int, Field(description="ID do post a que se responde (campo `id` de get_discussion_posts).")
    ],
    subject: Annotated[str, Field(description="Assunto da resposta.")],
    message_html: Annotated[str, Field(description="Corpo da resposta em HTML.")],
    attachments_draft_itemid: Annotated[
        int, Field(description="`itemid` de upload_file_to_draft para anexos; 0 não anexa nada.")
    ] = 0,
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
):
    """
    Envia uma resposta/comentário para um post de fórum (como aluno), com suporte opcional para anexo
    (carregado previamente via `upload_file_to_draft`).
    """
    await _require_confirmation(
        ctx,
        "Publicar uma resposta num fórum (visível para a turma)",
        {
            "Instância": instance,
            "Em resposta ao post": post_id,
            "Assunto": subject,
            "Mensagem": message_html,
            "Anexos (itemid)": attachments_draft_itemid or "(nenhum)",
        },
    )
    params: dict[str, Any] = {
        "postid": post_id,
        "subject": subject,
        "message": message_html,
    }
    if attachments_draft_itemid:
        params["options[0][name]"] = "attachmentsid"
        params["options[0][value]"] = attachments_draft_itemid
    return _moodle_call("mod_forum_add_discussion_post", params, instance=instance)


@mcp.tool(annotations=_ann("Criar tópico num fórum", idempotent=False))
async def create_forum_discussion(
    forum_id: Annotated[
        int, Field(description="ID do fórum onde criar o tópico (campo `id` de get_course_forums).")
    ],
    subject: Annotated[str, Field(description="Assunto do novo tópico.")],
    message_html: Annotated[str, Field(description="Corpo da mensagem inicial em HTML.")],
    group_id: Annotated[int, Field(description="ID do grupo; -1 publica para todos os grupos.")] = -1,
    attachments_draft_itemid: Annotated[
        int, Field(description="`itemid` de upload_file_to_draft para anexos; 0 não anexa nada.")
    ] = 0,
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
):
    """Cria um novo tópico de discussão num fórum da disciplina (com suporte opcional a anexo e grupo)."""
    await _require_confirmation(
        ctx,
        "Criar um tópico num fórum (visível para a turma)",
        {
            "Instância": instance,
            "Fórum": forum_id,
            "Grupo": "todos" if group_id == -1 else group_id,
            "Assunto": subject,
            "Mensagem": message_html,
            "Anexos (itemid)": attachments_draft_itemid or "(nenhum)",
        },
    )
    params: dict[str, Any] = {
        "forumid": forum_id,
        "subject": subject,
        "message": message_html,
        "groupid": group_id,
    }
    if attachments_draft_itemid:
        params["options[0][name]"] = "attachmentsid"
        params["options[0][value]"] = attachments_draft_itemid
    return _moodle_call("mod_forum_add_discussion", params, instance=instance)


@mcp.tool(annotations=_ann("Enviar mensagem instantânea", idempotent=False))
async def send_instant_message(
    to_user_id: Annotated[
        int, Field(description="ID do destinatário (campo `id` de get_course_participants).")
    ],
    text: Annotated[str, Field(description="Texto da mensagem a enviar.")],
    instance: Inst = DEFAULT_INSTANCE,
    ctx: Context = None,  # type: ignore[assignment]  # injetado pelo FastMCP
):
    """Envia uma mensagem privada direta no Moodle para um professor ou colega de doutoramento (`core_message_send_instant_messages`)."""
    await _require_confirmation(
        ctx,
        "Enviar uma mensagem privada no Moodle",
        {"Instância": instance, "Destinatário": _describe_user(to_user_id, instance), "Mensagem": text},
    )
    return _moodle_call(
        "core_message_send_instant_messages",
        {
            "messages[0][touserid]": to_user_id,
            "messages[0][text]": text,
            "messages[0][textformat]": 1,
        },
        instance=instance,
    )


@mcp.tool(annotations=_ann("Ver eventos e prazos", read_only=True))
def get_calendar_events(
    course_id: Annotated[
        int,
        Field(
            description="Limitar a uma disciplina; 0 devolve os eventos de todas as disciplinas inscritas."
        ),
    ] = 0,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém os próximos eventos de calendário e prazos de entrega."""
    if course_id:
        current_timestamp = int(datetime.now().timestamp())
        return _moodle_call(
            "core_calendar_get_calendar_events",
            {
                "events[courseids][0]": course_id,
                "options[timestart]": current_timestamp,
            },
            instance=instance,
        )
    return _moodle_call("core_calendar_get_calendar_upcoming_view", instance=instance)


@mcp.tool(annotations=_ann("Ler mensagens", read_only=True))
def get_messages(
    read: Annotated[
        bool, Field(description="True devolve as mensagens já lidas; False devolve as não lidas.")
    ] = False,
    instance: Inst = DEFAULT_INSTANCE,
):
    """Obtém notificações e mensagens recebidas pelo aluno."""
    uid = _get_current_user_id(instance=instance)
    return _moodle_call(
        "core_message_get_messages",
        {
            "useridto": uid,
            "type": "notifications",
            "read": 1 if read else 0,
        },
        instance=instance,
    )


# ==============================================================================
# 6. SINCRONIZAÇÃO PROFUNDA COM O OBSIDIAN (DOUTORAMENTO)
# ==============================================================================


@mcp.tool(annotations=_ann("Exportar fóruns para o Vault"))
def export_forum_discussions_to_obsidian(
    course_id: CourseId,
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """
    Exporta todos os fóruns e debates de uma disciplina para ficheiros Markdown organizados no Obsidian
    (`<Vault>/Unidades Curriculares/<UC>/Foruns/`).
    Útil para teres as orientações do professor e as discussões assíncronas no Vault.
    """
    forums = get_course_forums(course_id, instance=instance)
    if not isinstance(forums, list):
        return {"error": True, "details": forums}

    cname = _course_folder_name(course_id, instance)

    foruns_dir = _resolve_in_vault(Path("Unidades Curriculares") / cname / "Foruns")
    foruns_dir.mkdir(parents=True, exist_ok=True)

    exported_files = []
    used: set[Path] = set()
    for f in forums:
        fid = f.get("id")
        fname = _safe_filename(f.get("name", f"Forum_{fid}"))
        discs_resp = _soft(get_forum_discussions, fid, instance=instance, default={})
        discussions = discs_resp.get("discussions", []) if isinstance(discs_resp, dict) else []
        if not discussions:
            continue

        lines = [
            "---",
            f"forum_id: {fid}",
            f"course_id: {course_id}",
            f'title: "{fname}"',
            f"tags: [moodle, forum, {instance}]",
            "---",
            "",
            f"# 💬 {fname} ({cname})",
            "",
        ]

        for d in discussions:
            did = d.get("discussion") or d.get("id")
            dname = d.get("name", f"Discussão {did}")
            lines.append(f"## 🧵 {dname} (`discussion_id: {did}`)\n")
            posts_resp = _soft(get_discussion_posts, did, instance=instance, default={})
            posts = posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []
            for p in reversed(posts):
                pid = p.get("id")
                author = p.get("author", {}).get("fullname", "Utilizador")
                created = p.get("timecreated", 0)
                dt_str = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else ""
                body = _html_to_markdown(p.get("message", ""))
                lines.extend(
                    [
                        f"### 👤 {author} — `{dt_str}` (`post_id: {pid}`)",
                        "",
                        body,
                        "",
                        "---",
                        "",
                    ]
                )

        fpath = _unique_path(foruns_dir / f"{fname}.md", used)
        _write_note(fpath, "\n".join(lines))
        exported_files.append(str(fpath))

    return {
        "course_id": course_id,
        "course_name": cname,
        "forums_exported": len(exported_files),
        "files": exported_files,
    }


def _course_folder_name(course_id: int, instance: str) -> str:
    """Nome da pasta de uma UC no Vault; `UC_<id>` se o nome não puder ser obtido."""
    courses_resp = _soft(get_enrolled_courses, "all", instance=instance, default=[])
    courses = courses_resp.get("courses", []) if isinstance(courses_resp, dict) else courses_resp
    for c in courses if isinstance(courses, list) else []:
        if int(c.get("id", 0)) == int(course_id):
            return _safe_filename(c.get("fullname") or c.get("shortname") or f"UC_{course_id}")
    return f"UC_{course_id}"


def _count_my_forum_posts(forum_id: int, userid: int, instance: str = DEFAULT_INSTANCE) -> tuple[int, int]:
    """Conta as discussões de um fórum e quantos posts (tópicos ou respostas) são do próprio aluno."""
    discussions: list[dict[str, Any]] = []
    page = 0
    while True:
        resp = _soft(
            _moodle_call,
            "mod_forum_get_forum_discussions",
            {"forumid": forum_id, "perpage": 50, "page": page},
            instance=instance,
            default={},
        )
        batch = resp.get("discussions", []) if isinstance(resp, dict) else []
        discussions.extend(batch)
        if len(batch) < 50:
            break
        page += 1

    mine = 0
    for disc in discussions:
        posts_resp = _soft(
            _moodle_call,
            "mod_forum_get_discussion_posts",
            {"discussionid": disc.get("discussion")},
            instance=instance,
            default={},
        )
        for post in posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []:
            if (post.get("author") or {}).get("id") == userid:
                mine += 1
    return len(discussions), mine


def _sync_course(
    course: dict[str, Any],
    note_name: str,
    *,
    userid: int,
    instance: str,
    base_url: str,
    uc_dir: Path,
    assign_by_course: dict[int, list[dict[str, Any]]],
    now_str: str,
    download_materials: bool,
    export_forums: bool,
    errors: list[dict[str, str]],
) -> tuple[list[str], list[tuple[int, str, str, str, str, Any, str]], list[tuple[str, str, str]], list[str]]:
    """Sincroniza uma UC e devolve o que entra no painel.

    Isolada numa função para que a falha de uma disciplina não aborte as
    outras: o chamador só junta ao painel o que esta devolve com sucesso, e as
    secções opcionais (notas, fóruns, escolhas, testes, conteúdos) degradam para
    vazio com o erro registado em `errors`.

    Returns:
        `(linhas_do_painel, prazos, escolhas_pendentes, ficheiros_escritos)`.
    """
    dashboard_lines: list[str] = []
    upcoming_items: list[tuple[int, str, str, str, str, Any, str]] = []
    pending_choices: list[tuple[str, str, str]] = []
    created_files: list[str] = []

    cid = int(course["id"])
    cname = course.get("fullname") or course.get("shortname") or f"Curso {cid}"
    safe_name = note_name
    progress = course.get("progress")
    prog_str = f"{int(progress)}%" if progress is not None else "—"

    grades_resp = _soft(
        get_course_grades, cid, userid, instance=instance, default={}, errors=errors, what=f"{cname}: notas"
    )
    grade_items = []
    course_total = "—"
    if isinstance(grades_resp, dict):
        usergrades = grades_resp.get("usergrades", [])
        if usergrades:
            grade_items = usergrades[0].get("gradeitems", [])
            for gi in grade_items:
                if gi.get("itemtype") == "course":
                    course_total = gi.get("gradeformatted") or "—"

    course_assigns = assign_by_course.get(cid, [])
    uc_note_name = f"{safe_name}.md"
    dashboard_lines.append(
        f"| `{cid}` | **{cname}** | {prog_str} | **{course_total}** | {len(course_assigns)} | [[Unidades Curriculares/{safe_name}\\|Abrir UC]] |"
    )

    uc_lines = [
        "---",
        f"course_id: {cid}",
        f'title: "{cname}"',
        f'progress: "{prog_str}"',
        f"updated: {now_str}",
        f"tags: [moodle, uc, {instance}]",
        "---",
        "",
        f"# 📘 {cname}",
        "",
        f"⬅️ Voltar a [[Doutoramento - Painel Moodle]] | 🔗 [Abrir no Moodle]({base_url}/course/view.php?id={cid})",
        "",
        "## 📊 Avaliações e Notas (`gradereport`)",
        "",
        "| Item de Avaliação | Nota | Intervalo | Feedback do Professor |",
        "|:---|:---:|:---:|:---|",
    ]

    for gi in grade_items:
        iname = gi.get("itemname") or ("Total da Disciplina" if gi.get("itemtype") == "course" else "Item")
        gval = gi.get("gradeformatted") or "—"
        gmin = gi.get("grademin", "")
        gmax = gi.get("grademax", "")
        fb = _html_to_markdown(gi.get("feedback", "")).replace("\n", " ")
        uc_lines.append(f"| {iname} | **{gval}** | {gmin}–{gmax} | {fb} |")

    uc_lines.extend(
        [
            "",
            "## 📝 Trabalhos, E-fólios e P-fólios (`mod_assign`)",
            "",
        ]
    )

    if not course_assigns:
        uc_lines.append("_Nenhum trabalho registado nesta disciplina._")
    else:
        for a in course_assigns:
            aid = a.get("id")
            aname = a.get("name", f"Trabalho {aid}")
            duedate = a.get("duedate", 0)
            due_str = datetime.fromtimestamp(duedate).strftime("%Y-%m-%d %H:%M") if duedate else "Sem prazo"
            intro = _html_to_markdown(a.get("intro", ""))

            # Check student's submission status
            sub_status = "—"
            grading_status = "—"
            try:
                sub_resp = get_assignment_submission(int(aid or 0), userid, instance=instance)
                if isinstance(sub_resp, dict) and "lastattempt" in sub_resp:
                    sub_obj = sub_resp["lastattempt"].get("submission", {})
                    sub_status = sub_obj.get("status", "—")
                    grading_status = sub_resp["lastattempt"].get("gradingstatus", "—")
            except Exception:
                pass

            uc_lines.extend(
                [
                    f"### 📌 {aname} (`assign_id: {aid}`)",
                    f"- **Prazo de entrega:** `{due_str}`",
                    f"- **Estado da Submissão:** `{sub_status}` | **Avaliação:** `{grading_status}`",
                ]
            )
            if intro:
                uc_lines.append(f"- **Enunciado / Descrição:**\n\n{intro}\n")
            uc_lines.append("")
            if duedate:
                upcoming_items.append((duedate, due_str, cname, aname, "trabalho", aid, sub_status))

    # Fóruns com prazo (frequentemente avaliados) — contam para a nota mas não são `mod_assign`
    forums_resp = _soft(
        get_course_forums, cid, instance=instance, default=[], errors=errors, what=f"{cname}: fóruns"
    )
    dated_forums = [f for f in (forums_resp if isinstance(forums_resp, list) else []) if f.get("duedate")]
    if dated_forums:
        uc_lines.extend(
            [
                "",
                "## 🏛 Fóruns com prazo (`mod_forum`)",
                "",
                "| Prazo | Fórum | Discussões | Posts meus | cmid |",
                "|:---|:---|:---:|:---:|---:|",
            ]
        )
        for f in dated_forums:
            fdue = int(f.get("duedate") or 0)
            fdue_str = datetime.fromtimestamp(fdue).strftime("%Y-%m-%d %H:%M")
            n_disc, n_mine = _count_my_forum_posts(int(f["id"]), userid, instance=instance)
            fstatus = f"{n_mine} post(s) meus" if n_mine else "sem participação"
            uc_lines.append(
                f"| `{fdue_str}` | [{f.get('name')}]({base_url}/mod/forum/view.php?id={f.get('cmid')}) "
                f"| {n_disc} | {n_mine} | `{f.get('cmid')}` |"
            )
            upcoming_items.append(
                (fdue, fdue_str, cname, f.get("name", "Fórum"), "fórum", f.get("cmid"), fstatus)
            )

    # Escolhas (`mod_choice`) ainda sem resposta — p.ex. opção de frequência de módulo
    choices_resp = _soft(
        _moodle_call,
        "mod_choice_get_choices_by_courses",
        {"courseids[0]": cid},
        instance=instance,
        default={},
        errors=errors,
        what=f"{cname}: escolhas",
    )
    for ch in choices_resp.get("choices", []) if isinstance(choices_resp, dict) else []:
        opts_resp = _soft(
            _moodle_call,
            "mod_choice_get_choice_options",
            {"choiceid": ch.get("id")},
            instance=instance,
            default={},
        )
        options = opts_resp.get("options", []) if isinstance(opts_resp, dict) else []
        if options and not any(o.get("checked") for o in options):
            ch_url = f"{base_url}/mod/choice/view.php?id={ch.get('coursemodule')}"
            pending_choices.append((cname, ch.get("name", "Escolha"), ch_url))
            uc_lines.extend(
                [
                    "",
                    f"> ⚠️ **Escolha por responder:** [{ch.get('name')}]({ch_url}) "
                    f"({len(options)} opções disponíveis)",
                ]
            )

    # Testes (`mod_quiz`) com data de fecho
    quizzes_resp = _soft(
        _moodle_call,
        "mod_quiz_get_quizzes_by_courses",
        {"courseids[0]": cid},
        instance=instance,
        default={},
        errors=errors,
        what=f"{cname}: testes",
    )
    for qz in quizzes_resp.get("quizzes", []) if isinstance(quizzes_resp, dict) else []:
        qclose = int(qz.get("timeclose") or 0)
        if qclose:
            qclose_str = datetime.fromtimestamp(qclose).strftime("%Y-%m-%d %H:%M")
            upcoming_items.append(
                (qclose, qclose_str, cname, qz.get("name", "Teste"), "teste", qz.get("coursemodule"), "—")
            )

    contents = _soft(
        get_course_contents, cid, instance=instance, default=None, errors=errors, what=f"{cname}: conteúdos"
    )
    uc_lines.extend(["", "## 📂 Conteúdos, Tópicos e Materiais da Disciplina", ""])
    if isinstance(contents, list):
        for section in contents:
            sec_name = section.get("name") or f"Secção {section.get('section', '')}"
            uc_lines.append(f"### {sec_name}")
            sec_summary = _html_to_markdown(section.get("summary", ""))
            if sec_summary:
                uc_lines.append(sec_summary)
            for mod in section.get("modules", []):
                mname = mod.get("name", "Recurso")
                mtype = mod.get("modname", "")
                murl = mod.get("url", "")
                mid = mod.get("instance", "")
                cmid = mod.get("id", "")
                comp = mod.get("completiondata", {}).get("state", 0)
                check = "✅" if comp == 1 else "⬜"
                if murl:
                    uc_lines.append(f"- {check} [{mname}]({murl}) (`{mtype}`, id: `{mid}`, cmid: `{cmid}`)")
                else:
                    uc_lines.append(f"- {check} {mname} (`{mtype}`, id: `{mid}`, cmid: `{cmid}`)")
            uc_lines.append("")

    uc_file = uc_dir / uc_note_name
    _write_note(uc_file, "\n".join(uc_lines))
    created_files.append(str(uc_file))

    for wanted, extra, label in (
        (download_materials, download_all_course_materials_to_obsidian, "materiais"),
        (export_forums, export_forum_discussions_to_obsidian, "fóruns exportados"),
    ):
        if wanted:
            res = _soft(extra, cid, instance=instance, default={}, errors=errors, what=f"{cname}: {label}")
            for err in res.get("errors", []) if isinstance(res, dict) else []:
                errors.append(
                    {"item": f"{cname}: {label}: {err.get('file', '')}", "error": err.get("error", "")}
                )

    return dashboard_lines, upcoming_items, pending_choices, created_files


@mcp.tool(annotations=_ann("Sincronizar painel no Obsidian"))
def sync_doctoral_dashboard_to_obsidian(
    vault_dir: Annotated[
        str, Field(description="Subdiretório alternativo dentro do Vault; vazio escreve na raiz do Vault.")
    ] = "",
    download_materials: Annotated[
        bool, Field(description="Se descarrega também todos os PDFs e materiais de cada UC.")
    ] = False,
    export_forums: Annotated[
        bool, Field(description="Se exporta também todos os tópicos dos fóruns para Markdown.")
    ] = False,
    instance: Inst = DEFAULT_INSTANCE,
) -> dict[str, Any]:
    """
    Sincroniza todas as disciplinas inscritas, notas detalhadas, estado de submissão dos e-fólios/p-fólios,
    progresso de atividades e prazos do Moodle para o teu Vault do Doutoramento no Obsidian.
    Se `download_materials=True`, descarrega também todos os PDFs e converte-os para Markdown!
    Se `export_forums=True`, exporta também todos os tópicos dos fóruns para o Obsidian!
    """
    base_url, _ = _resolve_target(instance)
    target_dir = _resolve_in_vault(vault_dir) if vault_dir else _vault_root()
    target_dir.mkdir(parents=True, exist_ok=True)
    uc_dir = target_dir / "Unidades Curriculares"
    uc_dir.mkdir(parents=True, exist_ok=True)

    site_info = _moodle_call("core_webservice_get_site_info", instance=instance)
    if isinstance(site_info, dict) and (site_info.get("error") or "exception" in site_info):
        return {"error": True, "details": site_info}

    userid = int(site_info["userid"])
    fullname = site_info.get("fullname", "Aluno")

    courses_resp = get_enrolled_courses("all", instance=instance)
    courses = courses_resp.get("courses", []) if isinstance(courses_resp, dict) else courses_resp
    if not isinstance(courses, list):
        courses = []

    assign_resp = _soft(get_assignments, instance=instance, default={})
    assign_by_course: dict[int, list[dict[str, Any]]] = {}
    if isinstance(assign_resp, dict):
        for c in assign_resp.get("courses", []):
            assign_by_course[int(c["id"])] = c.get("assignments", [])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    dashboard_lines = [
        "---",
        f"title: Painel Moodle ({instance})",
        f"updated: {now_str}",
        f"tags: [moodle, painel, {instance}]",
        "---",
        "",
        f"# 🎓 Doutoramento — Painel Académico ({base_url})",
        "",
        f"**Estudante:** {fullname} (`userid: {userid}`)  ",
        f"**Última sincronização:** {now_str}",
        "",
        "## 📚 Unidades Curriculares e Notas",
        "",
        "| ID | Unidade Curricular | Progresso | Nota Atual | E-fólios / Trabalhos | Página da UC |",
        "|---:|:---|:---:|:---:|:---:|:---|",
    ]

    upcoming_items: list[tuple[int, str, str, str, str, Any, str]] = []
    pending_choices: list[tuple[str, str, str]] = []
    created_files = []

    errors: list[dict[str, str]] = []
    used_notes: set[Path] = set()
    synced = 0
    for course in courses:
        cname = course.get("fullname") or course.get("shortname") or f"Curso {course.get('id')}"
        # UCs com o mesmo nome escreviam a mesma nota; o painel liga ao nome único.
        note_name = _unique_path(Path(_safe_filename(cname) + ".md"), used_notes).stem
        try:
            rows, upcoming, choices, files = _sync_course(
                course,
                note_name,
                userid=userid,
                instance=instance,
                base_url=base_url,
                uc_dir=uc_dir,
                assign_by_course=assign_by_course,
                now_str=now_str,
                download_materials=download_materials,
                export_forums=export_forums,
                errors=errors,
            )
        except MoodleAPIError as exc:
            if exc.errorcode in _FATAL_ERRORCODES:
                raise
            errors.append({"item": cname, "error": str(exc)})
            continue
        except Exception as exc:  # uma UC com dados inesperados não pode parar as outras
            errors.append({"item": cname, "error": f"{type(exc).__name__}: {exc}"})
            continue
        dashboard_lines.extend(rows)
        upcoming_items.extend(upcoming)
        pending_choices.extend(choices)
        created_files.extend(files)
        synced += 1

    now_ts = int(datetime.now().timestamp())
    pending_states = {"new", "draft", "reopened", "sem participação", "—"}
    dashboard_lines.extend(
        [
            "",
            "## ⏰ Calendário de Prazos (trabalhos, fóruns avaliados e testes)",
            "",
            "| Data Limite | Tipo | Unidade Curricular | Atividade | Estado | ID |",
            "|:---|:---:|:---|:---|:---:|---:|",
        ]
    )
    for due_ts, due_str, cname, aname, kind, ref_id, status in sorted(upcoming_items, key=lambda x: x[0]):
        # Só sinaliza o que continua em falta: um prazo passado já entregue não é um alerta.
        flag = " ⚠️" if due_ts < now_ts and status in pending_states else ""
        dashboard_lines.append(
            f"| `{due_str}`{flag} | {kind} | {cname} | **{aname}** | `{status}` | `{ref_id}` |"
        )

    if pending_choices:
        dashboard_lines.extend(
            [
                "",
                "## 🗳 Escolhas e inscrições por responder",
                "",
            ]
        )
        for cname, ch_name, ch_url in pending_choices:
            dashboard_lines.append(f"- {cname} — [{ch_name}]({ch_url})")
    dashboard_lines.append("")
    dashboard_lines.append("_⚠️ = prazo já ultrapassado e atividade ainda em falta._")

    dashboard_file = target_dir / "Doutoramento - Painel Moodle.md"
    _write_note(dashboard_file, "\n".join(dashboard_lines) + "\n")
    created_files.insert(0, str(dashboard_file))

    return {
        "status": "ok" if not errors else "parcial",
        "student": fullname,
        "courses_synced": synced,
        "courses_total": len(courses),
        "files_written": created_files,
        "errors": errors,
    }


# ==============================================================================
# TOOLSETS E ARRANQUE
# ==============================================================================

logger = logging.getLogger("moodle-mcp")

#: Agrupamento das ferramentas. Expor as 32 custa ~9 500 tokens de contexto em
#: cada pedido, o que degrada a escolha de ferramenta em modelos pequenos e locais.
#: Um perfil reduzido (`--tools core,obsidian`) corta esse custo para menos de
#: metade sem perder o fluxo principal de sincronização para o Obsidian.
TOOLSETS: dict[str, tuple[str, ...]] = {
    "core": (
        "get_site_info",
        "get_enrolled_courses",
        "get_course_contents",
        "get_course_pages_and_books",
        "get_activities_completion_status",
        "get_course_participants",
        "get_calendar_events",
        "get_messages",
    ),
    "grades": (
        "get_assignments",
        "get_assignment_submission",
        "get_course_grades",
        "get_all_grades_overview",
        "get_course_quizzes",
    ),
    "forums": (
        "get_course_forums",
        "get_forum_discussions",
        "get_discussion_posts",
        "search_all_course_forums",
    ),
    "obsidian": (
        "download_moodle_file",
        "download_all_course_materials_to_obsidian",
        "export_forum_discussions_to_obsidian",
        "sync_doctoral_dashboard_to_obsidian",
    ),
    "write": (
        "toggle_activity_completion",
        "upload_file_to_draft",
        "save_assignment_submission",
        "add_submission_comment",
        "reply_to_forum_post",
        "create_forum_discussion",
        "send_instant_message",
    ),
    "auth": (
        "set_moodle_token",
        "decode_mobile_launch_token",
        "extract_token_from_session_cookie",
    ),
    "raw": ("call_any_moodle_wsfunction",),
}

#: Ativos por omissão: só leitura do Moodle e escrita no Vault local.
#:
#: `write` fica de fora porque as suas ferramentas agem em nome do estudante do
#: lado da universidade — submeter um trabalho, publicar num fórum avaliado,
#: enviar uma mensagem — e nenhuma dessas ações tem como ser desfeita. Expor por
#: omissão o que é irreversível faz com que uma interpretação errada do modelo
#: baste para a provocar; exigir `--tools ...,write` torna-a uma decisão de quem
#: configura o servidor.
#:
#: `raw` fica de fora pela mesma razão vista de outro ângulo: o
#: `call_any_moodle_wsfunction` dá acesso total à API de Web Services, escrita
#: incluída, o que é bom para diagnóstico e mau para estar sempre ligado.
#:
#: `auth` fica de fora porque gravar credenciais não é algo que o modelo precise
#: de fazer: o handler `moodlemobile://` faz o login sem passar pelo canal MCP.
#: Exposto, um post de fórum com instruções escondidas poderia trocar o token
#: guardado pelo de outra conta.
DEFAULT_TOOLSETS = ("core", "grades", "forums", "obsidian")


def _iter_registered_tools() -> list[Any]:
    """Devolve os objetos `Tool` registados no servidor.

    O FastMCP 1.30 não expõe um acessor síncrono para as ferramentas registadas
    (`FastMCP.list_tools()` é assíncrono e devolve o tipo do protocolo, sem o
    dicionário de schema mutável). O acesso ao gestor interno fica contido nesta
    função para que uma mudança de API do SDK tenha um único ponto de correção.

    Returns:
        A lista de ferramentas, ou uma lista vazia se o SDK mudar de estrutura.
    """
    try:
        return list(mcp._tool_manager.list_tools())
    except AttributeError:  # pragma: no cover - depende da versão do SDK
        logger.warning("Não foi possível inspecionar as ferramentas registadas.")
        return []


def strip_schema_titles() -> int:
    """Remove os `title` que o Pydantic gera a partir dos nomes dos campos.

    O Pydantic emite `"title": "Cmid"` para um campo `cmid`, o que não acrescenta
    nada ao que o nome e a descrição já dizem e custa ~570 tokens no conjunto das
    32 ferramentas.

    Returns:
        O número de chaves `title` removidas.
    """
    removed = 0
    for tool in _iter_registered_tools():
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict):
            continue
        removed += 1 if schema.pop("title", None) is not None else 0
        for field_schema in schema.get("properties", {}).values():
            if isinstance(field_schema, dict):
                removed += 1 if field_schema.pop("title", None) is not None else 0
    return removed


def resolve_toolsets(spec: str) -> set[str]:
    """Interpreta uma especificação `--tools` e devolve os toolsets a ativar.

    Args:
        spec: Lista separada por vírgulas, `"all"` para todos, ou vazio para a
            seleção por omissão.

    Returns:
        O conjunto de nomes de toolset a ativar.

    Raises:
        ValueError: Se algum nome não existir em `TOOLSETS`.
    """
    spec = spec.strip()
    if not spec:
        return set(DEFAULT_TOOLSETS)
    if spec.lower() == "all":
        return set(TOOLSETS)

    requested = {part.strip().lower() for part in spec.split(",") if part.strip()}
    unknown = sorted(requested - set(TOOLSETS))
    if unknown:
        raise ValueError(
            f"Toolset(s) desconhecido(s): {', '.join(unknown)}. Disponíveis: {', '.join(TOOLSETS)}."
        )
    if "core" not in requested:
        # Sem `core` não há informação do site nem listagem de disciplinas, pelo que
        # nenhuma das outras ferramentas teria por onde começar.
        logger.info("A acrescentar o toolset 'core', necessário a todos os outros.")
        requested.add("core")
    return requested


def apply_toolsets(enabled: set[str]) -> list[str]:
    """Retira do servidor as ferramentas que não pertencem aos toolsets ativos.

    As funções Python continuam a existir e a ser chamáveis internamente — o
    `sync_doctoral_dashboard_to_obsidian` compõe várias delas — mas deixam de ser
    anunciadas ao cliente, e por isso de ocupar contexto.

    Args:
        enabled: Nomes dos toolsets a manter.

    Returns:
        Os nomes das ferramentas retiradas, por ordem alfabética.
    """
    removed: list[str] = []
    for toolset, tool_names in TOOLSETS.items():
        if toolset in enabled:
            continue
        for tool_name in tool_names:
            try:
                mcp.remove_tool(tool_name)
                removed.append(tool_name)
            except Exception as exc:  # pragma: no cover - nome inexistente
                logger.warning("Não foi possível retirar '%s': %s", tool_name, exc)
    return sorted(removed)


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada do servidor MCP.

    Args:
        argv: Argumentos de linha de comandos; `None` usa `sys.argv`.

    Returns:
        O código de saída do processo.
    """
    # stdout é o canal JSON-RPC do transporte stdio: qualquer escrita nele
    # corrompe o protocolo. Todo o diagnóstico vai para stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.getenv("MOODLE_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s [%(name)s] %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="moodle-mcp",
        description="Servidor MCP para Moodle com sincronização para o Obsidian.",
    )
    parser.add_argument(
        "--tools",
        default=config.get("tools"),
        metavar="A,B,C",
        help=(
            "Toolsets a expor, separados por vírgulas: "
            f"{', '.join(TOOLSETS)}. 'all' expõe todos. "
            f"Omissão: {','.join(DEFAULT_TOOLSETS)} "
            "(também configurável por MOODLE_MCP_TOOLS)."
        ),
    )
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="Lista os toolsets e as ferramentas de cada um, e termina.",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Mostra os caminhos e os valores de configuração em uso, e termina.",
    )
    parser.add_argument(
        "--install-handler",
        action="store_true",
        help=(
            f"Registra este programa como handler do esquema {handler.SCHEME}:// "
            "no sistema operativo, e termina. É o que permite capturar o token "
            "automaticamente a partir do navegador em logins com SSO federado."
        ),
    )
    parser.add_argument(
        "--uninstall-handler",
        action="store_true",
        help=f"Remove o registo do esquema {handler.SCHEME}://, e termina.",
    )
    parser.add_argument("--version", action="version", version=f"moodle-mcp {__version__}")
    args = parser.parse_args(argv)

    if args.install_handler or args.uninstall_handler:
        try:
            result = handler.install() if args.install_handler else handler.uninstall()
        except handler.HandlerError as exc:
            parser.error(str(exc))
        for key, value in result.items():
            if isinstance(value, list):
                print(f"{key}:")
                for item in value:
                    print(f"  - {item}")
            else:
                print(f"{key}: {value}")
        if args.install_handler:
            print(
                "\nPara autenticar: faz login no Moodle no navegador e abre "
                "<url-do-moodle>/admin/tool/mobile/launch.php"
                "?service=moodle_mobile_app&passport=12345"
            )
        return 0

    if args.show_config:
        for key, value in config.describe().items():
            print(f"{key:<20} {value}")
        return 0

    if args.list_toolsets:
        for toolset, tool_names in TOOLSETS.items():
            marca = "*" if toolset in DEFAULT_TOOLSETS else " "
            print(f"{marca} {toolset} ({len(tool_names)})")
            for tool_name in tool_names:
                print(f"      {tool_name}")
        print("\n* = ativo por omissão")
        return 0

    try:
        enabled = resolve_toolsets(args.tools)
    except ValueError as exc:
        parser.error(str(exc))

    for warning in config.security_warnings():
        logger.warning("Segurança: %s", warning)

    removed = apply_toolsets(enabled)
    titles = strip_schema_titles()
    for risky, nota in (
        ("write", "podem submeter trabalhos, publicar em fóruns e enviar mensagens"),
        ("raw", "dão acesso total à API de Web Services, escrita incluída"),
        ("auth", "podem substituir o token guardado"),
    ):
        if risky in enabled:
            logger.warning("Toolset '%s' ativo: as suas ferramentas %s.", risky, nota)
    if enabled & {"write", "raw"}:
        mode = _confirm_mode()
        if mode == "client":
            logger.warning(
                "MOODLE_MCP_CONFIRM_WRITES=client: as ações no Moodle não pedem confirmação ao "
                "utilizador; dependem só da aprovação do cliente MCP."
            )
        else:
            logger.info("Ações no Moodle pedem confirmação ao utilizador (modo '%s').", mode)
    logger.info(
        "Toolsets ativos: %s | %d ferramentas expostas (%d retiradas, %d titles removidos)",
        ",".join(sorted(enabled)),
        len(_iter_registered_tools()),
        len(removed),
        titles,
    )

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
