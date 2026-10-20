import base64
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import pymupdf as fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdownify import markdownify as md_convert
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("Moodle UAb/UTAD Doctoral Suite & Obsidian MCP Server")

MOODLE_URL = os.getenv("MOODLE_URL", "https://elearning.uab.pt").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "").strip()
MOODLE_UTAD_URL = os.getenv("MOODLE_UTAD_URL", "https://e-learning.utad.pt").rstrip("/")
MOODLE_UTAD_TOKEN = os.getenv("MOODLE_UTAD_TOKEN", "").strip()
OBSIDIAN_VAULT_DIR = os.path.expanduser(os.getenv("OBSIDIAN_VAULT_DIR", "~/Doutoramento"))


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
    return response.json()


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


@mcp.tool()
def set_moodle_token(
    token: str,
    instance: str = "uab",
    moodle_url: Optional[str] = None,
) -> dict[str, Any]:
    """
    Configura e guarda o token Moodle (para 'uab' ou 'utad') no ficheiro .env
    e valida de imediato a ligação com core_webservice_get_site_info.
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
        "site_info": site_info,
    }


@mcp.tool()
def decode_mobile_launch_token(launch_url_or_base64: str, instance: str = "uab") -> dict[str, Any]:
    """
    Descodifica um URL `moodlemobile://token=...` (obtido via `/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345`)
    e guarda automaticamente o `wstoken` e `privatetoken` no ficheiro `.env`.
    """
    raw = launch_url_or_base64.strip()
    if "token=" in raw:
        raw = raw.split("token=", 1)[1].split("&", 1)[0]
    decoded = base64.b64decode(raw + "===").decode("utf-8", errors="ignore")
    parts = decoded.split(":::")
    result: dict[str, Any] = {"raw_decoded": decoded}
    if len(parts) >= 2:
        token = parts[1].strip()
        result["wstoken"] = token
        if len(parts) >= 3:
            result["privatetoken"] = parts[2].strip()
        set_res = set_moodle_token(token, instance=instance)
        result["validation"] = set_res
    return result


@mcp.tool()
def extract_token_from_session_cookie(
    moodle_session_cookie: str,
    instance: str = "uab",
) -> dict[str, Any]:
    """
    Se já tiveres feito login no browser e passares o valor do cookie `MoodleSession`
    (visível em F12 -> Storage/Application -> Cookies -> MoodleSession),
    esta ferramenta extrai automaticamente a tua Chave de Segurança (Token) de `/user/managetoken.php`
    ou do endpoint `/admin/tool/mobile/launch.php` e guarda-a no `.env`!
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
        "redirected_url": resp.url,
        "message": "Não foi possível extrair o token automaticamente com este MoodleSession. Verifica se a sessão ainda está ativa.",
    }


@mcp.tool()
def call_any_moodle_wsfunction(
    wsfunction: str,
    params_json: str = "{}",
    instance: str = "uab",
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


@mcp.tool()
def get_site_info(instance: str = "uab"):
    """Obtém informação do site Moodle, versão, funções ativas na API e dados do aluno autenticado."""
    return _moodle_call("core_webservice_get_site_info", instance=instance)


@mcp.tool()
def get_enrolled_courses(classification: str = "all", instance: str = "uab"):
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


@mcp.tool()
def get_course_contents(course_id: int, instance: str = "uab"):
    """Obtém todos os tópicos, módulos, recursos, ficheiros, pastas e URLs de uma unidade curricular."""
    return _moodle_call("core_course_get_contents", {"courseid": course_id}, instance=instance)


@mcp.tool()
def get_course_pages_and_books(course_id: int, instance: str = "uab") -> dict[str, Any]:
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


@mcp.tool()
def get_activities_completion_status(course_id: int, instance: str = "uab"):
    """Obtém o estado de conclusão (checkboxes de progresso) de todas as atividades de uma disciplina."""
    uid = _get_current_user_id(instance=instance)
    return _moodle_call(
        "core_completion_get_activities_completion_status",
        {"courseid": course_id, "userid": uid},
        instance=instance,
    )


@mcp.tool()
def toggle_activity_completion(cmid: int, completed: bool = True, instance: str = "uab"):
    """
    Marca ou desmarca manualmente uma atividade/recurso da disciplina como concluída (`core_completion_update_activity_completion_status_manually`).
    `cmid` é o ID do módulo do curso (course module id).
    """
    return _moodle_call(
        "core_completion_update_activity_completion_status_manually",
        {"cmid": cmid, "completed": 1 if completed else 0},
        instance=instance,
    )


@mcp.tool()
def get_course_participants(course_id: int, limit: int = 100, instance: str = "uab"):
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


@mcp.tool()
def download_moodle_file(
    file_url: str,
    dest_path: str,
    extract_pdf_to_markdown: bool = True,
    instance: str = "uab",
) -> dict[str, Any]:
    """
    Descarrega um ficheiro autenticado do Moodle (PDF, DOCX, PPTX, ZIP, etc.) usando o teu token.
    Se for um PDF e `extract_pdf_to_markdown=True`, extrai automaticamente todo o texto com PyMuPDF
    para um ficheiro `.md` ao lado do PDF para que o Obsidian e o assistente de IA o possam ler e pesquisar!
    """
    _, token = _resolve_target(instance)
    auth_url = _append_token_to_fileurl(file_url, token)
    target = Path(dest_path).expanduser().resolve()
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
            doc.close()
            md_path = target.with_suffix(".pdf.md")
            md_path.write_text("\n".join(md_pages), encoding="utf-8")
            result["extracted_markdown"] = str(md_path)
            result["pdf_pages"] = idx
        except Exception as e:
            result["pdf_extraction_warning"] = str(e)

    return result


@mcp.tool()
def download_all_course_materials_to_obsidian(
    course_id: int,
    extract_pdfs: bool = True,
    instance: str = "uab",
) -> dict[str, Any]:
    """
    Descarrega TODOS os ficheiros (PDFs, enunciados de e-fólios, slides, textos de apoio e páginas HTML)
    de uma disciplina diretamente para a pasta da Unidade Curricular dentro do teu Vault do Obsidian
    (`/home/carlos-andr-ferreira/Doutoramento/Unidades Curriculares/<Nome da UC>/Materiais/`),
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

    base_uc_dir = Path(OBSIDIAN_VAULT_DIR).expanduser().resolve() / "Unidades Curriculares" / cname / "Materiais"
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


@mcp.tool()
def get_assignments(instance: str = "uab"):
    """Obtém todos os trabalhos / e-fólios / p-fólios, descrições, ficheiros de enunciado e prazos de entrega."""
    return _moodle_call("mod_assign_get_assignments", instance=instance)


@mcp.tool()
def get_assignment_submission(assign_id: int, user_id: Optional[int] = None, instance: str = "uab"):
    """Obtém o estado detalhado da submissão, nota, rubrica e comentários de feedback de um trabalho/e-fólio."""
    uid = user_id if user_id is not None else _get_current_user_id(instance=instance)
    return _moodle_call(
        "mod_assign_get_submission_status",
        {"assignid": assign_id, "userid": uid},
        instance=instance,
    )


@mcp.tool()
def get_course_grades(course_id: int, user_id: Optional[int] = None, instance: str = "uab"):
    """Obtém todas as notas e itens de avaliação (e-fólios A/B/C, Global, p-fólio) de uma disciplina específica."""
    uid = user_id if user_id is not None else _get_current_user_id(instance=instance)
    return _moodle_call(
        "gradereport_user_get_grade_items",
        {"courseid": course_id, "userid": uid},
        instance=instance,
    )


@mcp.tool()
def get_all_grades_overview(instance: str = "uab"):
    """Obtém o resumo de notas finais/atuais de todas as disciplinas em que o aluno está inscrito."""
    return _moodle_call("gradereport_overview_get_course_grades", instance=instance)


@mcp.tool()
def get_course_quizzes(course_id: int, instance: str = "uab"):
    """Lista todos os questionários / testes online (`mod_quiz`) de uma disciplina e as tuas tentativas/notas."""
    quizzes = _moodle_call("mod_quiz_get_quizzes_by_courses", {"courseids[0]": course_id}, instance=instance)
    return quizzes


@mcp.tool()
def upload_file_to_draft(file_path: str, item_id: int = 0, instance: str = "uab") -> Any:
    """
    Faz upload de um ficheiro local (ex.: PDF do teu e-fólio ou tese) para a área de rascunho (draft area) do Moodle.
    Retorna o `itemid` que pode ser passado a `save_assignment_submission` ou `reply_to_forum_post`.
    """
    base_url, token = _resolve_target(instance)
    path = Path(file_path).expanduser().resolve()
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


@mcp.tool()
def save_assignment_submission(
    assign_id: int,
    online_text_html: Optional[str] = None,
    files_draft_item_id: Optional[int] = None,
    submit_for_grading: bool = False,
    instance: str = "uab",
) -> dict[str, Any]:
    """
    Guarda um trabalho/e-fólio no Moodle (texto online e/ou ficheiros previamente carregados com `upload_file_to_draft`).
    Por segurança, `submit_for_grading=False` guarda em modo rascunho/submissão editável;
    se `submit_for_grading=True`, lacra e submete em definitivo para avaliação.
    """
    params: dict[str, Any] = {"assignmentid": assign_id}
    if online_text_html is not None:
        params["plugindata[onlinetext_editor][text]"] = online_text_html
        params["plugindata[onlinetext_editor][format]"] = 1
        params["plugindata[onlinetext_editor][itemid]"] = 0
    if files_draft_item_id is not None:
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


@mcp.tool()
def add_submission_comment(
    context_id: int,
    item_id: int,
    content: str,
    area: str = "submission_comments",
    instance: str = "uab",
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


@mcp.tool()
def get_course_forums(course_id: int, instance: str = "uab"):
    """Lista todos os fóruns (Fórum de Notícias, Fórum Central, Fóruns de Tópicos/Grupos) de uma disciplina."""
    return _moodle_call("mod_forum_get_forums_by_courses", {"courseids[0]": course_id}, instance=instance)


@mcp.tool()
def get_forum_discussions(forum_id: int, instance: str = "uab"):
    """Lista todos os tópicos de discussão dentro de um fórum específico."""
    return _moodle_call("mod_forum_get_forum_discussions", {"forumid": forum_id}, instance=instance)


@mcp.tool()
def get_discussion_posts(discussion_id: int, instance: str = "uab"):
    """Obtém todas as mensagens/respostas dentro de um tópico de discussão de um fórum."""
    return _moodle_call("mod_forum_get_discussion_posts", {"discussionid": discussion_id}, instance=instance)


@mcp.tool()
def search_all_course_forums(course_id: int, query: str, instance: str = "uab") -> dict[str, Any]:
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


@mcp.tool()
def reply_to_forum_post(
    post_id: int,
    subject: str,
    message_html: str,
    attachments_draft_itemid: Optional[int] = None,
    instance: str = "uab",
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
    if attachments_draft_itemid is not None:
        params["options[0][name]"] = "attachmentsid"
        params["options[0][value]"] = attachments_draft_itemid
    return _moodle_call("mod_forum_add_discussion_post", params, instance=instance)


@mcp.tool()
def create_forum_discussion(
    forum_id: int,
    subject: str,
    message_html: str,
    group_id: int = -1,
    attachments_draft_itemid: Optional[int] = None,
    instance: str = "uab",
):
    """Cria um novo tópico de discussão num fórum da disciplina (com suporte opcional a anexo e grupo)."""
    params: dict[str, Any] = {
        "forumid": forum_id,
        "subject": subject,
        "message": message_html,
        "groupid": group_id,
    }
    if attachments_draft_itemid is not None:
        params["options[0][name]"] = "attachmentsid"
        params["options[0][value]"] = attachments_draft_itemid
    return _moodle_call("mod_forum_add_discussion", params, instance=instance)


@mcp.tool()
def send_instant_message(to_user_id: int, text: str, instance: str = "uab"):
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


@mcp.tool()
def get_calendar_events(course_id: Optional[int] = None, instance: str = "uab"):
    """Obtém os próximos eventos de calendário e prazos de entrega."""
    if course_id is not None:
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


@mcp.tool()
def get_messages(read: bool = False, instance: str = "uab"):
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


@mcp.tool()
def export_forum_discussions_to_obsidian(course_id: int, instance: str = "uab") -> dict[str, Any]:
    """
    Exporta todos os fóruns e debates de uma disciplina para ficheiros Markdown organizados no Obsidian
    (`/home/carlos-andr-ferreira/Doutoramento/Unidades Curriculares/<UC>/Foruns/`).
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

    foruns_dir = Path(OBSIDIAN_VAULT_DIR).expanduser().resolve() / "Unidades Curriculares" / cname / "Foruns"
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


@mcp.tool()
def sync_doctoral_dashboard_to_obsidian(
    vault_dir: Optional[str] = None,
    download_materials: bool = False,
    export_forums: bool = False,
    instance: str = "uab",
) -> dict[str, Any]:
    """
    Sincroniza todas as disciplinas inscritas, notas detalhadas, estado de submissão dos e-fólios/p-fólios,
    progresso de atividades e prazos do Moodle para o teu Vault do Doutoramento no Obsidian.
    Se `download_materials=True`, descarrega também todos os PDFs e converte-os para Markdown!
    Se `export_forums=True`, exporta também todos os tópicos dos fóruns para o Obsidian!
    """
    base_url, _ = _resolve_target(instance)
    target_dir = Path(vault_dir or OBSIDIAN_VAULT_DIR).expanduser().resolve()
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

    upcoming_assignments = []
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
                    upcoming_assignments.append((duedate, due_str, cname, aname, aid, sub_status))

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

    dashboard_lines.extend([
        "",
        "## ⏰ Calendário de E-fólios e Trabalhos",
        "",
        "| Data Limite | Unidade Curricular | Trabalho / E-fólio | Estado | Assign ID |",
        "|:---|:---|:---|:---:|---:|",
    ])
    for _, due_str, cname, aname, aid, sub_status in sorted(upcoming_assignments, key=lambda x: x[0]):
        dashboard_lines.append(f"| `{due_str}` | {cname} | **{aname}** | `{sub_status}` | `{aid}` |")

    dashboard_file = target_dir / "Doutoramento - Painel Moodle.md"
    dashboard_file.write_text("\n".join(dashboard_lines) + "\n", encoding="utf-8")
    created_files.insert(0, str(dashboard_file))

    return {
        "status": "ok",
        "student": fullname,
        "courses_synced": len(courses),
        "files_written": created_files,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
