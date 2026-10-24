import argparse
import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional
import sys
from urllib.parse import parse_qs, urlparse

import pymupdf as fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdownify import markdownify as md_convert
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

load_dotenv(Path(__file__).resolve().parent / ".env")

mcp = FastMCP("Moodle UAb/UTAD Doctoral Suite & Obsidian MCP Server")

MOODLE_URL = os.getenv("MOODLE_URL", "https://elearning.uab.pt").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "").strip()
MOODLE_UTAD_URL = os.getenv("MOODLE_UTAD_URL", "https://e-learning.utad.pt").rstrip("/")
MOODLE_UTAD_TOKEN = os.getenv("MOODLE_UTAD_TOKEN", "").strip()
OBSIDIAN_VAULT_DIR = os.path.expanduser(os.getenv("OBSIDIAN_VAULT_DIR", "~/Doutoramento"))


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

Inst = Annotated[
    str,
    Field(description="Instância Moodle a usar: 'uab' (Universidade Aberta) ou 'utad'."),
]
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


def _resolve_target(instance: str = "uab") -> tuple[str, str]:
    """Resolve URL and Token for 'uab' or 'utad'."""
    if instance.lower() == "utad":
        return MOODLE_UTAD_URL, MOODLE_UTAD_TOKEN
    return MOODLE_URL, MOODLE_TOKEN


def _moodle_call(
    wsfunction: str,
    params: Optional[dict[str, Any]] = None,
    method: str = "POST",
    instance: str = "uab",
) -> Any:
    """Helper to call Moodle REST Web Services API."""
    base_url, token = _resolve_target(instance)
    if not token:
        return {
            "error": True,
            "instance": instance,
            "message": (
                f"Token para '{instance}' não está configurado no .env. "
                f"Acede a {base_url}/user/managetoken.php (em Preferências -> Chaves de segurança) "
                "ou usa a ferramenta `extract_token_from_browser` / `set_moodle_token`."
            ),
        }

    payload: dict[str, Any] = {
        "wstoken": token,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
    }
    if params:
        payload.update(params)

    url = f"{base_url}/webservice/rest/server.php"
    if method.upper() == "GET":
        response = requests.get(url, params=payload, timeout=30)
    else:
        response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return _redact_secrets(response.json())


def _get_current_user_id(instance: str = "uab") -> int:
    info = _moodle_call("core_webservice_get_site_info", instance=instance)
    if isinstance(info, dict) and "userid" in info:
        return int(info["userid"])
    raise ValueError(f"Não foi possível obter userid da sessão Moodle ({instance}): {info}")


def _html_to_markdown(html_text: str) -> str:
    if not html_text:
        return ""
    try:
        converted = md_convert(html_text, heading_style="ATX").strip()
        return re.sub(r"\n{3,}", "\n\n", converted)
    except Exception:
        clean = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
        clean = re.sub(r"</p>", "\n\n", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", "", clean)
        return html.unescape(clean).strip()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\r\n]+', "-", name).strip()
    return re.sub(r"\s+", " ", cleaned)[:120]


def _append_token_to_fileurl(fileurl: str, token: str) -> str:
    if not fileurl:
        return ""
    sep = "&" if "?" in fileurl else "?"
    if "token=" in fileurl:
        return fileurl
    return f"{fileurl}{sep}token={token}"


def _update_env_var(key: str, value: str) -> str:
    env_path = Path(__file__).parent / ".env"
    lines = []
    if env_path.exists():
        lines = [
            line for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{key}=")
        ]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(env_path)


# ==============================================================================
# 1. AUTENTICAÇÃO, TOKENS & SSO (UAb / UTAD)
# ==============================================================================


@mcp.tool(annotations=_ann("Guardar token do Moodle", destructive=True))
def set_moodle_token(
    token: Annotated[str, Field(description="O `wstoken` do Web Service do Moodle (32 caracteres hexadecimais).")],
    instance: Inst = "uab",
    moodle_url: Annotated[str, Field(description="URL base alternativo a gravar para esta instância; vazio mantém o atual.")] = "",
) -> dict[str, Any]:
    """Guarda o token Moodle no `.env` e valida logo a ligação ao site.

    Aviso de segurança: o token entra por argumento, pelo que é enviado para o
    provider de LLM configurado no cliente e fica no histórico da sessão. Para
    configuração inicial, preferir escrever o `.env` à mão ou usar o capturador
    `moodlemobile://`. A resposta nunca devolve o token, apenas a sua impressão
    digital.

    Args:
        token: O `wstoken` do Web Service do Moodle.
        instance: Instância a configurar, `"uab"` ou `"utad"`.
        moodle_url: URL base alternativo a gravar para esta instância.

    Returns:
        Dicionário com `saved_to`, `instance`, `token_fingerprint` e o `site_info`
        devolvido pelo Moodle (já redigido).
    """
    global MOODLE_TOKEN, MOODLE_URL, MOODLE_UTAD_TOKEN, MOODLE_UTAD_URL
    clean_token = token.strip()
    if instance.lower() == "utad":
        if moodle_url:
            MOODLE_UTAD_URL = moodle_url.strip().rstrip("/")
            _update_env_var("MOODLE_UTAD_URL", MOODLE_UTAD_URL)
        MOODLE_UTAD_TOKEN = clean_token
        env_file = _update_env_var("MOODLE_UTAD_TOKEN", clean_token)
    else:
        if moodle_url:
            MOODLE_URL = moodle_url.strip().rstrip("/")
            _update_env_var("MOODLE_URL", MOODLE_URL)
        MOODLE_TOKEN = clean_token
        env_file = _update_env_var("MOODLE_TOKEN", clean_token)

    site_info = _moodle_call("core_webservice_get_site_info", instance=instance)
    return {
        "saved_to": env_file,
        "instance": instance,
        "token_fingerprint": _token_fingerprint(clean_token),
        "site_info": site_info,
    }


@mcp.tool(annotations=_ann("Capturar token via moodlemobile://", destructive=True))
def decode_mobile_launch_token(
    launch_url_or_base64: Annotated[str, Field(description="O URL `moodlemobile://token=...` completo, ou apenas a parte em Base64.")],
    instance: Inst = "uab",
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
        instance: Instância a configurar, `"uab"` ou `"utad"`.

    Returns:
        Dicionário com `saved`, `instance`, `token_fingerprint`, `has_privatetoken`
        e `validation` (o resultado de `set_moodle_token`).
    """
    raw = launch_url_or_base64.strip()
    if "token=" in raw:
        raw = raw.split("token=", 1)[1].split("&", 1)[0]
    decoded = base64.b64decode(raw + "===").decode("utf-8", errors="ignore")
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
    return {
        "saved": True,
        "instance": instance,
        "token_fingerprint": _token_fingerprint(token),
        "has_privatetoken": len(parts) >= 3 and bool(parts[2].strip()),
        "validation": set_moodle_token(token, instance=instance),
    }


@mcp.tool(annotations=_ann("Extrair token de um cookie", destructive=True))
def extract_token_from_session_cookie(
    moodle_session_cookie: Annotated[str, Field(description="Valor do cookie `MoodleSession` de uma sessão de browser ativa.")],
    instance: Inst = "uab",
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
        instance: Instância a configurar, `"uab"` ou `"utad"`.

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
def call_any_moodle_wsfunction(
    wsfunction: Annotated[str, Field(description="Nome da função de Web Service, ex.: `core_user_get_users_by_field`.")],
    params_json: Annotated[str, Field(description="Parâmetros em JSON. Arrays usam a sintaxe do Moodle, ex.: {'courseids[0]': 42}.")] = "{}",
    instance: Inst = "uab",
) -> Any:
    """
    Ferramenta universal de baixo nível: permite chamar QUALQUER função da API Web Services do Moodle
    (ex.: `core_user_get_users_by_field`, `mod_glossary_get_entries_by_letter`, etc.) passando parâmetros em JSON.
    """
    params = json.loads(params_json) if params_json else {}
    return _moodle_call(wsfunction, params, instance=instance)


# ==============================================================================
# 2. DISCIPLINAS, CONTEÚDOS, PÁGINAS HTML, PASTAS E PROGRESSO
# ==============================================================================


@mcp.tool(annotations=_ann("Ver informação do site", read_only=True))
def get_site_info(
    instance: Inst = "uab",
):
    """Obtém informação do site Moodle, versão, funções ativas na API e dados do aluno autenticado."""
    return _moodle_call("core_webservice_get_site_info", instance=instance)


@mcp.tool(annotations=_ann("Listar disciplinas inscritas", read_only=True))
def get_enrolled_courses(
    classification: Annotated[str, Field(description="Filtro temporal: 'all', 'inprogress', 'future', 'past' ou 'favourites'.")] = "all",
    instance: Inst = "uab",
):
    """
    Lista todas as disciplinas/unidades curriculares em que o aluno está inscrito.
    `classification` pode ser: 'all', 'inprogress', 'future', 'past', 'favourites'.
    """
    res = _moodle_call(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"classification": classification},
        instance=instance,
    )
    if isinstance(res, dict) and "exception" in res:
        userid = _get_current_user_id(instance=instance)
        return _moodle_call("core_enrol_get_users_courses", {"userid": userid}, instance=instance)
    return res


@mcp.tool(annotations=_ann("Ver conteúdos da disciplina", read_only=True))
def get_course_contents(
    course_id: CourseId,
    instance: Inst = "uab",
):
    """Obtém todos os tópicos, módulos, recursos, ficheiros, pastas e URLs de uma unidade curricular."""
    return _moodle_call("core_course_get_contents", {"courseid": course_id}, instance=instance)


@mcp.tool(annotations=_ann("Ver páginas e livros", read_only=True))
def get_course_pages_and_books(
    course_id: CourseId,
    instance: Inst = "uab",
) -> dict[str, Any]:
    """
    Obtém todas as Páginas HTML (`mod_page`), Pastas (`mod_folder`), Recursos (`mod_resource`)
    e Livros (`mod_book` — muito usados nos Contratos de Aprendizagem e Guias da UAb!) de uma disciplina.
    """
    pages = _moodle_call("mod_page_get_pages_by_courses", {"courseids[0]": course_id}, instance=instance)
    folders = _moodle_call("mod_folder_get_folders_by_courses", {"courseids[0]": course_id}, instance=instance)
    resources = _moodle_call("mod_resource_get_resources_by_courses", {"courseids[0]": course_id}, instance=instance)
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
    instance: Inst = "uab",
):
    """Obtém o estado de conclusão (checkboxes de progresso) de todas as atividades de uma disciplina."""
    uid = _get_current_user_id(instance=instance)
    return _moodle_call(
        "core_completion_get_activities_completion_status",
        {"courseid": course_id, "userid": uid},
        instance=instance,
    )


@mcp.tool(annotations=_ann("Marcar atividade como concluída"))
def toggle_activity_completion(
    cmid: Annotated[int, Field(description="ID do módulo de curso (cmid) — o campo `id` dos módulos em get_course_contents, NÃO o `instance`.")],
    completed: Annotated[bool, Field(description="True marca a atividade como concluída; False desmarca.")] = True,
    instance: Inst = "uab",
):
    """
    Marca ou desmarca manualmente uma atividade/recurso da disciplina como concluída (`core_completion_update_activity_completion_status_manually`).
    `cmid` é o ID do módulo do curso (course module id).
    """
    return _moodle_call(
        "core_completion_update_activity_completion_status_manually",
        {"cmid": cmid, "completed": 1 if completed else 0},
        instance=instance,
    )


@mcp.tool(annotations=_ann("Listar participantes", read_only=True))
def get_course_participants(
    course_id: CourseId,
    limit: Annotated[int, Field(description="Número máximo de participantes a devolver.")] = 100,
    instance: Inst = "uab",
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
    file_url: Annotated[str, Field(description="O `fileurl` devolvido pela API do Moodle, sem token (é acrescentado pelo servidor).")],
    dest_path: Annotated[str, Field(description="Destino do ficheiro. Um caminho relativo resolve a partir da raiz do Vault; tem de ficar dentro do Vault.")],
    extract_pdf_to_markdown: Annotated[bool, Field(description="Se True e o ficheiro for PDF, extrai o texto para um `.md` irmão.")] = True,
    instance: Inst = "uab",
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
        instance: Instância Moodle, `"uab"` ou `"utad"`.

    Returns:
        Dicionário com `downloaded_file`, `size_bytes` e, para PDFs,
        `extracted_markdown` e `pdf_pages`.

    Raises:
        ValueError: Se `dest_path` cair fora do Vault.
    """
    _, token = _resolve_target(instance)
    auth_url = _append_token_to_fileurl(file_url, token)
    target = _resolve_in_vault(dest_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with requests.get(auth_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with target.open("wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

    result: dict[str, Any] = {
        "downloaded_file": str(target),
        "size_bytes": target.stat().st_size,
    }

    if extract_pdf_to_markdown and target.suffix.lower() == ".pdf":
        try:
            doc = fitz.open(str(target))
            md_pages = [
                "---",
                f"source_pdf: \"{target.name}\"",
                f"pages: {len(doc)}",
                f"extracted_at: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "tags: [doutoramento, pdf-extraido, uab]",
                "---",
                "",
                f"# 📄 Texto Extraído: {target.stem}",
                "",
            ]
            for idx, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if text:
                    md_pages.append(f"## Página {idx}\n\n{text}\n")
            n_pages = len(doc)
            doc.close()
            md_path = target.with_suffix(".pdf.md")
            md_path.write_text("\n".join(md_pages), encoding="utf-8")
            result["extracted_markdown"] = str(md_path)
            result["pdf_pages"] = n_pages
        except Exception as e:
            result["pdf_extraction_warning"] = str(e)

    return result


@mcp.tool(annotations=_ann("Descarregar materiais da UC"))
def download_all_course_materials_to_obsidian(
    course_id: CourseId,
    extract_pdfs: Annotated[bool, Field(description="Se converte cada PDF descarregado num `.md` pesquisável no Obsidian.")] = True,
    instance: Inst = "uab",
) -> dict[str, Any]:
    """
    Descarrega TODOS os ficheiros (PDFs, enunciados de e-fólios, slides, textos de apoio e páginas HTML)
    de uma disciplina diretamente para a pasta da Unidade Curricular dentro do teu Vault do Obsidian
    (`<Vault>/Unidades Curriculares/<Nome da UC>/Materiais/`),
    convertendo também os PDFs e Páginas HTML em notas Markdown pesquisáveis!
    """
    _, token = _resolve_target(instance)
    contents = get_course_contents(course_id, instance=instance)
    if not isinstance(contents, list):
        return {"error": True, "details": contents}

    # Find course name
    cname = f"UC_{course_id}"
    courses_resp = get_enrolled_courses("all", instance=instance)
    courses = courses_resp.get("courses", []) if isinstance(courses_resp, dict) else courses_resp
    if isinstance(courses, list):
        for c in courses:
            if int(c.get("id", 0)) == int(course_id):
                cname = _safe_filename(c.get("fullname") or c.get("shortname") or cname)
                break

    base_uc_dir = _resolve_in_vault(Path("Unidades Curriculares") / cname / "Materiais")
    base_uc_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    errors = []

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
                    dest = sec_dir / fname
                    try:
                        if fname.lower().endswith((".html", ".htm")):
                            auth_url = _append_token_to_fileurl(furl, token)
                            r = requests.get(auth_url, timeout=30)
                            r.raise_for_status()
                            sec_dir.mkdir(parents=True, exist_ok=True)
                            md_dest = sec_dir / f"{Path(fname).stem}.md"
                            md_dest.write_text(
                                f"# {mname}\n\n{_html_to_markdown(r.text)}\n",
                                encoding="utf-8",
                            )
                            downloaded.append(str(md_dest))
                        else:
                            res = download_moodle_file(
                                furl,
                                str(dest),
                                extract_pdf_to_markdown=extract_pdfs,
                                instance=instance,
                            )
                            downloaded.append(res["downloaded_file"])
                    except Exception as e:
                        errors.append({"file": fname, "error": str(e)})

    # Also download assignment attachment files (enunciados de e-fólios)
    assign_resp = get_assignments(instance=instance)
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
                                    str(efolios_dir / f"{aname}_{fname}"),
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
    instance: Inst = "uab",
):
    """Obtém todos os trabalhos / e-fólios / p-fólios, descrições, ficheiros de enunciado e prazos de entrega."""
    return _moodle_call("mod_assign_get_assignments", instance=instance)


@mcp.tool(annotations=_ann("Ver estado de uma submissão", read_only=True))
def get_assignment_submission(
    assign_id: Annotated[int, Field(description="ID do trabalho — o campo `id` de get_assignments, NÃO o cmid.")],
    user_id: UserId = 0,
    instance: Inst = "uab",
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
    instance: Inst = "uab",
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
    instance: Inst = "uab",
):
    """Obtém o resumo de notas finais/atuais de todas as disciplinas em que o aluno está inscrito."""
    return _moodle_call("gradereport_overview_get_course_grades", instance=instance)


@mcp.tool(annotations=_ann("Listar testes", read_only=True))
def get_course_quizzes(
    course_id: CourseId,
    instance: Inst = "uab",
):
    """Lista todos os questionários / testes online (`mod_quiz`) de uma disciplina e as tuas tentativas/notas."""
    quizzes = _moodle_call("mod_quiz_get_quizzes_by_courses", {"courseids[0]": course_id}, instance=instance)
    return quizzes


@mcp.tool(annotations=_ann("Enviar ficheiro para rascunho", idempotent=False))
def upload_file_to_draft(
    file_path: Annotated[str, Field(description="Ficheiro a enviar, dentro do Vault. Um caminho relativo resolve a partir da raiz do Vault.")],
    item_id: Annotated[int, Field(description="`itemid` de um draft existente a reutilizar; 0 cria um novo.")] = 0,
    instance: Inst = "uab",
) -> Any:
    """Envia um ficheiro do Vault para a área de rascunho (draft area) do Moodle.

    `file_path` tem de estar dentro de `OBSIDIAN_VAULT_DIR`: o upload sai da
    máquina para o servidor da universidade, pelo que ler um caminho arbitrário
    seria um canal de exfiltração (p.ex. `~/.ssh/id_rsa`). Coloca o e-fólio no
    Vault antes de o submeter.

    Args:
        file_path: Ficheiro a enviar, dentro do Vault (absoluto ou relativo à raiz).
        item_id: `itemid` do draft a reutilizar; 0 cria um novo.
        instance: Instância Moodle, `"uab"` ou `"utad"`.

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

    upload_url = f"{base_url}/webservice/upload.php"
    params: dict[str, Any] = {
        "token": token,
        "filearea": "draft",
        "itemid": item_id,
    }
    with path.open("rb") as f:
        files = {"file_1": (path.name, f)}
        response = requests.post(upload_url, params=params, files=files, timeout=90)
    response.raise_for_status()
    return response.json()


@mcp.tool(annotations=_ann("Submeter trabalho / e-fólio", destructive=True, idempotent=False))
def save_assignment_submission(
    assign_id: Annotated[int, Field(description="ID do trabalho (campo `id` de get_assignments).")],
    online_text_html: Annotated[str, Field(description="Texto da submissão em HTML (plugin 'online text'); vazio não envia texto.")] = "",
    files_draft_item_id: Annotated[int, Field(description="`itemid` devolvido por upload_file_to_draft; 0 submete sem ficheiros.")] = 0,
    submit_for_grading: Annotated[bool, Field(description="True entrega definitivamente para avaliação e é IRREVERSÍVEL. False apenas guarda o rascunho.")] = False,
    instance: Inst = "uab",
) -> dict[str, Any]:
    """
    Guarda um trabalho/e-fólio no Moodle (texto online e/ou ficheiros previamente carregados com `upload_file_to_draft`).
    Por segurança, `submit_for_grading=False` guarda em modo rascunho/submissão editável;
    se `submit_for_grading=True`, lacra e submete em definitivo para avaliação.
    """
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
def add_submission_comment(
    context_id: Annotated[int, Field(description="ID de contexto do módulo do trabalho no Moodle.")],
    item_id: Annotated[int, Field(description="ID do item a comentar (normalmente o id da submissão).")],
    content: Annotated[str, Field(description="Texto do comentário.")],
    area: Annotated[str, Field(description="Área de comentários do Moodle; raramente precisa de ser alterada.")] = "submission_comments",
    instance: Inst = "uab",
):
    """
    Adiciona um comentário oficial num trabalho/submissão (ex.: observação para o professor na entrega de um e-fólio).
    `context_id`: contextid do módulo do trabalho (`cmid` context).
    `item_id`: ID da submissão (`submission.id` obtido em `get_assignment_submission`).
    """
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
# 5. FÓRUNS, DEBATES ASSÍNCRONOS (UAb) E MENSAGENS DIRETAS A PROFESSORES
# ==============================================================================


@mcp.tool(annotations=_ann("Listar fóruns", read_only=True))
def get_course_forums(
    course_id: CourseId,
    instance: Inst = "uab",
):
    """Lista todos os fóruns (Fórum de Notícias, Fórum Central, Fóruns de Tópicos/Grupos) de uma disciplina."""
    return _moodle_call("mod_forum_get_forums_by_courses", {"courseids[0]": course_id}, instance=instance)


@mcp.tool(annotations=_ann("Listar tópicos de um fórum", read_only=True))
def get_forum_discussions(
    forum_id: Annotated[int, Field(description="ID do fórum (campo `id` de get_course_forums).")],
    instance: Inst = "uab",
):
    """Lista todos os tópicos de discussão dentro de um fórum específico."""
    return _moodle_call("mod_forum_get_forum_discussions", {"forumid": forum_id}, instance=instance)


@mcp.tool(annotations=_ann("Ler posts de um tópico", read_only=True))
def get_discussion_posts(
    discussion_id: Annotated[int, Field(description="ID do tópico (campo `discussion` de get_forum_discussions).")],
    instance: Inst = "uab",
):
    """Obtém todas as mensagens/respostas dentro de um tópico de discussão de um fórum."""
    return _moodle_call("mod_forum_get_discussion_posts", {"discussionid": discussion_id}, instance=instance)


@mcp.tool(annotations=_ann("Procurar nos fóruns", read_only=True))
def search_all_course_forums(
    course_id: CourseId,
    query: Annotated[str, Field(description="Texto a procurar no assunto e no corpo dos posts, sem distinção de maiúsculas.")],
    instance: Inst = "uab",
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
        discs = get_forum_discussions(fid, instance=instance)
        for d in (discs.get("discussions", []) if isinstance(discs, dict) else []):
            did = d.get("discussion") or d.get("id")
            dname = d.get("name", "")
            posts_resp = get_discussion_posts(did, instance=instance)
            posts = posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []
            for p in posts:
                subj = p.get("subject", "")
                msg = _html_to_markdown(p.get("message", ""))
                author = p.get("author", {}).get("fullname", "")
                if q in subj.lower() or q in msg.lower() or q in dname.lower():
                    matches.append({
                        "forum": fname,
                        "discussion_id": did,
                        "discussion": dname,
                        "post_id": p.get("id"),
                        "author": author,
                        "subject": subj,
                        "excerpt": msg[:600],
                    })
    return {"query": query, "course_id": course_id, "matches_count": len(matches), "matches": matches}


@mcp.tool(annotations=_ann("Responder num fórum", idempotent=False))
def reply_to_forum_post(
    post_id: Annotated[int, Field(description="ID do post a que se responde (campo `id` de get_discussion_posts).")],
    subject: Annotated[str, Field(description="Assunto da resposta.")],
    message_html: Annotated[str, Field(description="Corpo da resposta em HTML.")],
    attachments_draft_itemid: Annotated[int, Field(description="`itemid` de upload_file_to_draft para anexos; 0 não anexa nada.")] = 0,
    instance: Inst = "uab",
):
    """
    Envia uma resposta/comentário para um post de fórum (como aluno), com suporte opcional para anexo
    (carregado previamente via `upload_file_to_draft`).
    """
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
def create_forum_discussion(
    forum_id: Annotated[int, Field(description="ID do fórum onde criar o tópico (campo `id` de get_course_forums).")],
    subject: Annotated[str, Field(description="Assunto do novo tópico.")],
    message_html: Annotated[str, Field(description="Corpo da mensagem inicial em HTML.")],
    group_id: Annotated[int, Field(description="ID do grupo; -1 publica para todos os grupos.")] = -1,
    attachments_draft_itemid: Annotated[int, Field(description="`itemid` de upload_file_to_draft para anexos; 0 não anexa nada.")] = 0,
    instance: Inst = "uab",
):
    """Cria um novo tópico de discussão num fórum da disciplina (com suporte opcional a anexo e grupo)."""
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
def send_instant_message(
    to_user_id: Annotated[int, Field(description="ID do destinatário (campo `id` de get_course_participants).")],
    text: Annotated[str, Field(description="Texto da mensagem a enviar.")],
    instance: Inst = "uab",
):
    """Envia uma mensagem privada direta no Moodle para um professor ou colega de doutoramento (`core_message_send_instant_messages`)."""
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
    course_id: Annotated[int, Field(description="Limitar a uma disciplina; 0 devolve os eventos de todas as disciplinas inscritas.")] = 0,
    instance: Inst = "uab",
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
    read: Annotated[bool, Field(description="True devolve as mensagens já lidas; False devolve as não lidas.")] = False,
    instance: Inst = "uab",
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
    instance: Inst = "uab",
) -> dict[str, Any]:
    """
    Exporta todos os fóruns e debates de uma disciplina para ficheiros Markdown organizados no Obsidian
    (`<Vault>/Unidades Curriculares/<UC>/Foruns/`).
    Excelente na UAb para teres todas as orientações do professor e discussões assíncronas no teu Vault!
    """
    forums = get_course_forums(course_id, instance=instance)
    if not isinstance(forums, list):
        return {"error": True, "details": forums}

    cname = f"UC_{course_id}"
    courses_resp = get_enrolled_courses("all", instance=instance)
    courses = courses_resp.get("courses", []) if isinstance(courses_resp, dict) else courses_resp
    if isinstance(courses, list):
        for c in courses:
            if int(c.get("id", 0)) == int(course_id):
                cname = _safe_filename(c.get("fullname") or c.get("shortname") or cname)
                break

    foruns_dir = _resolve_in_vault(Path("Unidades Curriculares") / cname / "Foruns")
    foruns_dir.mkdir(parents=True, exist_ok=True)

    exported_files = []
    for f in forums:
        fid = f.get("id")
        fname = _safe_filename(f.get("name", f"Forum_{fid}"))
        discs_resp = get_forum_discussions(fid, instance=instance)
        discussions = discs_resp.get("discussions", []) if isinstance(discs_resp, dict) else []
        if not discussions:
            continue

        lines = [
            "---",
            f"forum_id: {fid}",
            f"course_id: {course_id}",
            f"title: \"{fname}\"",
            "tags: [doutoramento, forum, uab]",
            "---",
            "",
            f"# 💬 {fname} ({cname})",
            "",
        ]

        for d in discussions:
            did = d.get("discussion") or d.get("id")
            dname = d.get("name", f"Discussão {did}")
            lines.append(f"## 🧵 {dname} (`discussion_id: {did}`)\n")
            posts_resp = get_discussion_posts(did, instance=instance)
            posts = posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []
            for p in reversed(posts):
                pid = p.get("id")
                author = p.get("author", {}).get("fullname", "Utilizador")
                created = p.get("timecreated", 0)
                dt_str = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else ""
                body = _html_to_markdown(p.get("message", ""))
                lines.extend([
                    f"### 👤 {author} — `{dt_str}` (`post_id: {pid}`)",
                    "",
                    body,
                    "",
                    "---",
                    "",
                ])

        fpath = foruns_dir / f"{fname}.md"
        fpath.write_text("\n".join(lines), encoding="utf-8")
        exported_files.append(str(fpath))

    return {
        "course_id": course_id,
        "course_name": cname,
        "forums_exported": len(exported_files),
        "files": exported_files,
    }


def _count_my_forum_posts(forum_id: int, userid: int, instance: str = "uab") -> tuple[int, int]:
    """Conta as discussões de um fórum e quantos posts (tópicos ou respostas) são do próprio aluno."""
    discussions: list[dict[str, Any]] = []
    page = 0
    while True:
        resp = _moodle_call(
            "mod_forum_get_forum_discussions",
            {"forumid": forum_id, "perpage": 50, "page": page},
            instance=instance,
        )
        batch = resp.get("discussions", []) if isinstance(resp, dict) else []
        discussions.extend(batch)
        if len(batch) < 50:
            break
        page += 1

    mine = 0
    for disc in discussions:
        posts_resp = _moodle_call(
            "mod_forum_get_discussion_posts",
            {"discussionid": disc.get("discussion")},
            instance=instance,
        )
        for post in (posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []):
            if (post.get("author") or {}).get("id") == userid:
                mine += 1
    return len(discussions), mine


@mcp.tool(annotations=_ann("Sincronizar painel no Obsidian"))
def sync_doctoral_dashboard_to_obsidian(
    vault_dir: Annotated[str, Field(description="Subdiretório alternativo dentro do Vault; vazio escreve na raiz do Vault.")] = "",
    download_materials: Annotated[bool, Field(description="Se descarrega também todos os PDFs e materiais de cada UC.")] = False,
    export_forums: Annotated[bool, Field(description="Se exporta também todos os tópicos dos fóruns para Markdown.")] = False,
    instance: Inst = "uab",
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

    assign_resp = get_assignments(instance=instance)
    assign_by_course: dict[int, list[dict[str, Any]]] = {}
    if isinstance(assign_resp, dict):
        for c in assign_resp.get("courses", []):
            assign_by_course[int(c["id"])] = c.get("assignments", [])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    dashboard_lines = [
        "---",
        "title: Doutoramento - Painel Moodle (UAb / UTAD)",
        f"updated: {now_str}",
        "tags: [doutoramento, uab, utad, moodle]",
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

    for course in courses:
        cid = int(course["id"])
        cname = course.get("fullname") or course.get("shortname") or f"Curso {cid}"
        safe_name = _safe_filename(cname)
        progress = course.get("progress")
        prog_str = f"{int(progress)}%" if progress is not None else "—"

        grades_resp = get_course_grades(cid, userid, instance=instance)
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
            f"title: \"{cname}\"",
            f"progress: \"{prog_str}\"",
            f"updated: {now_str}",
            "tags: [doutoramento, uc, uab]",
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

        uc_lines.extend([
            "",
            "## 📝 Trabalhos, E-fólios e P-fólios (`mod_assign`)",
            "",
        ])

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
                    sub_resp = get_assignment_submission(int(aid), userid, instance=instance)
                    if isinstance(sub_resp, dict) and "lastattempt" in sub_resp:
                        sub_obj = sub_resp["lastattempt"].get("submission", {})
                        sub_status = sub_obj.get("status", "—")
                        grading_status = sub_resp["lastattempt"].get("gradingstatus", "—")
                except Exception:
                    pass

                uc_lines.extend([
                    f"### 📌 {aname} (`assign_id: {aid}`)",
                    f"- **Prazo de entrega:** `{due_str}`",
                    f"- **Estado da Submissão:** `{sub_status}` | **Avaliação:** `{grading_status}`",
                ])
                if intro:
                    uc_lines.append(f"- **Enunciado / Descrição:**\n\n{intro}\n")
                uc_lines.append("")
                if duedate:
                    upcoming_items.append((duedate, due_str, cname, aname, "trabalho", aid, sub_status))

        # Fóruns com prazo (frequentemente avaliados) — contam para a nota mas não são `mod_assign`
        forums_resp = get_course_forums(cid, instance=instance)
        dated_forums = [
            f for f in (forums_resp if isinstance(forums_resp, list) else [])
            if f.get("duedate")
        ]
        if dated_forums:
            uc_lines.extend([
                "",
                "## 🏛 Fóruns com prazo (`mod_forum`)",
                "",
                "| Prazo | Fórum | Discussões | Posts meus | cmid |",
                "|:---|:---|:---:|:---:|---:|",
            ])
            for f in dated_forums:
                fdue = int(f.get("duedate") or 0)
                fdue_str = datetime.fromtimestamp(fdue).strftime("%Y-%m-%d %H:%M")
                n_disc, n_mine = _count_my_forum_posts(int(f["id"]), userid, instance=instance)
                fstatus = f"{n_mine} post(s) meus" if n_mine else "sem participação"
                uc_lines.append(
                    f"| `{fdue_str}` | [{f.get('name')}]({base_url}/mod/forum/view.php?id={f.get('cmid')}) "
                    f"| {n_disc} | {n_mine} | `{f.get('cmid')}` |"
                )
                upcoming_items.append((fdue, fdue_str, cname, f.get("name", "Fórum"), "fórum", f.get("cmid"), fstatus))

        # Escolhas (`mod_choice`) ainda sem resposta — p.ex. opção de frequência de módulo
        choices_resp = _moodle_call("mod_choice_get_choices_by_courses", {"courseids[0]": cid}, instance=instance)
        for ch in (choices_resp.get("choices", []) if isinstance(choices_resp, dict) else []):
            opts_resp = _moodle_call("mod_choice_get_choice_options", {"choiceid": ch.get("id")}, instance=instance)
            options = opts_resp.get("options", []) if isinstance(opts_resp, dict) else []
            if options and not any(o.get("checked") for o in options):
                ch_url = f"{base_url}/mod/choice/view.php?id={ch.get('coursemodule')}"
                pending_choices.append((cname, ch.get("name", "Escolha"), ch_url))
                uc_lines.extend([
                    "",
                    f"> ⚠️ **Escolha por responder:** [{ch.get('name')}]({ch_url}) "
                    f"({len(options)} opções disponíveis)",
                ])

        # Testes (`mod_quiz`) com data de fecho
        quizzes_resp = _moodle_call("mod_quiz_get_quizzes_by_courses", {"courseids[0]": cid}, instance=instance)
        for qz in (quizzes_resp.get("quizzes", []) if isinstance(quizzes_resp, dict) else []):
            qclose = int(qz.get("timeclose") or 0)
            if qclose:
                qclose_str = datetime.fromtimestamp(qclose).strftime("%Y-%m-%d %H:%M")
                upcoming_items.append((qclose, qclose_str, cname, qz.get("name", "Teste"), "teste", qz.get("coursemodule"), "—"))

        contents = get_course_contents(cid, instance=instance)
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
        uc_file.write_text("\n".join(uc_lines), encoding="utf-8")
        created_files.append(str(uc_file))

        if download_materials:
            download_all_course_materials_to_obsidian(cid, extract_pdfs=True, instance=instance)
        if export_forums:
            export_forum_discussions_to_obsidian(cid, instance=instance)

    now_ts = int(datetime.now().timestamp())
    pending_states = {"new", "draft", "reopened", "sem participação", "—"}
    dashboard_lines.extend([
        "",
        "## ⏰ Calendário de Prazos (trabalhos, fóruns avaliados e testes)",
        "",
        "| Data Limite | Tipo | Unidade Curricular | Atividade | Estado | ID |",
        "|:---|:---:|:---|:---|:---:|---:|",
    ])
    for due_ts, due_str, cname, aname, kind, ref_id, status in sorted(upcoming_items, key=lambda x: x[0]):
        # Só sinaliza o que continua em falta: um prazo passado já entregue não é um alerta.
        flag = " ⚠️" if due_ts < now_ts and status in pending_states else ""
        dashboard_lines.append(
            f"| `{due_str}`{flag} | {kind} | {cname} | **{aname}** | `{status}` | `{ref_id}` |"
        )

    if pending_choices:
        dashboard_lines.extend([
            "",
            "## 🗳 Escolhas e inscrições por responder",
            "",
        ])
        for cname, ch_name, ch_url in pending_choices:
            dashboard_lines.append(f"- {cname} — [{ch_name}]({ch_url})")
    dashboard_lines.append("")
    dashboard_lines.append("_⚠️ = prazo já ultrapassado e atividade ainda em falta._")

    dashboard_file = target_dir / "Doutoramento - Painel Moodle.md"
    dashboard_file.write_text("\n".join(dashboard_lines) + "\n", encoding="utf-8")
    created_files.insert(0, str(dashboard_file))

    return {
        "status": "ok",
        "student": fullname,
        "courses_synced": len(courses),
        "files_written": created_files,
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
        "set_moodle_token",
        "decode_mobile_launch_token",
        "extract_token_from_session_cookie",
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
    "raw": ("call_any_moodle_wsfunction",),
}

#: Ativos por omissão. `raw` fica de fora: o `call_any_moodle_wsfunction` dá
#: acesso total à API de Web Services, incluindo escrita, e é uma boa ferramenta
#: de diagnóstico mas má para estar sempre ligada.
DEFAULT_TOOLSETS = ("core", "grades", "forums", "obsidian", "write")


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
        # Sem `core` não há autenticação nem listagem de disciplinas, pelo que
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
        default=os.getenv("MOODLE_MCP_TOOLS", ""),
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
    args = parser.parse_args(argv)

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

    removed = apply_toolsets(enabled)
    titles = strip_schema_titles()
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
