"""Contrato das anotações das ferramentas (`ToolAnnotations`).

Os clientes MCP decidem pelas hints o que auto-aprovam e o que pedem ao
estudante para confirmar. Uma hint em falta vale o que o cliente presumir (a
especificação presume o pior: escrita destrutiva e não idempotente), e uma hint
errada é pior do que nenhuma. Estes testes verificam as duas coisas: o que o
cliente recebe em `tools/list` e o que está escrito no código-fonte.
"""

import ast
import asyncio
from pathlib import Path

import pytest

from moodle_mcp import server

SERVER_PY = Path(server.__file__)
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

#: Ferramentas que não fazem pedidos ao Moodle: só leem o estado guardado no Vault.
LOCAL_ONLY = {"get_recent_changes"}


def _listed_tools():
    return asyncio.run(server.mcp.list_tools())


def _tool_names():
    return [tool.name for tool in _listed_tools()]


def test_every_tool_is_listed_with_a_title_and_all_four_boolean_hints():
    tools = _listed_tools()
    assert len(tools) == sum(len(names) for names in server.TOOLSETS.values())
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.title, tool.name
        for hint in HINTS:
            assert isinstance(getattr(tool.annotations, hint), bool), f"{tool.name}: {hint}"


def test_hints_are_written_literally_in_the_source():
    # Quem lê o código, ou um scanner estático, tem de ver as mesmas hints que o
    # cliente recebe, sem resolver valores por omissão de uma função auxiliar.
    found = {}
    for node in ast.parse(SERVER_PY.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and ast.unparse(deco.func) == "mcp.tool"):
                continue
            ann = next(k.value for k in deco.keywords if k.arg == "annotations")
            assert isinstance(ann, ast.Call) and ast.unparse(ann.func) == "ToolAnnotations", node.name
            values = {k.arg: k.value for k in ann.keywords}
            for hint in HINTS:
                assert hint in values, f"{node.name}: {hint} não está escrita"
                assert isinstance(values[hint], ast.Constant) and isinstance(values[hint].value, bool)
            found[node.name] = {hint: values[hint].value for hint in HINTS}
    assert sorted(found) == sorted(_tool_names())


@pytest.mark.parametrize("toolset", ["core", "grades", "forums"])
def test_read_toolsets_only_hold_read_only_tools(toolset):
    by_name = {tool.name: tool.annotations for tool in _listed_tools()}
    for name in server.TOOLSETS[toolset]:
        assert by_name[name].readOnlyHint is True, name
        assert by_name[name].destructiveHint is False, name


@pytest.mark.parametrize("toolset", ["write", "auth", "raw", "obsidian"])
def test_tools_that_write_never_claim_to_be_read_only(toolset):
    by_name = {tool.name: tool.annotations for tool in _listed_tools()}
    writers = [name for name in server.TOOLSETS[toolset] if name not in LOCAL_ONLY]
    assert writers
    for name in writers:
        assert by_name[name].readOnlyHint is False, name


def test_read_only_tools_are_not_destructive():
    for tool in _listed_tools():
        if tool.annotations.readOnlyHint:
            assert tool.annotations.destructiveHint is False, tool.name


def test_irreversible_actions_are_not_idempotent():
    # Repetir estas chamadas cria um segundo post, comentário, mensagem ou rascunho.
    by_name = {tool.name: tool.annotations for tool in _listed_tools()}
    for name in (
        "upload_file_to_draft",
        "save_assignment_submission",
        "add_submission_comment",
        "reply_to_forum_post",
        "create_forum_discussion",
        "send_instant_message",
        "call_any_moodle_wsfunction",
    ):
        assert by_name[name].idempotentHint is False, name


#: Podem fazer perder algo que não se volta a obter: submeter em definitivo, trocar
#: as credenciais guardadas, chamar qualquer função da API, ou substituir um
#: ficheiro do Vault pela nova versão do Moodle (a anterior pode já lá não estar).
DESTRUCTIVE = {
    "set_moodle_token",
    "decode_mobile_launch_token",
    "extract_token_from_session_cookie",
    "call_any_moodle_wsfunction",
    "save_assignment_submission",
    "download_moodle_file",
    "download_all_course_materials_to_obsidian",
    "sync_doctoral_dashboard_to_obsidian",
}


def test_exactly_the_tools_that_can_lose_content_are_destructive():
    # Uma ferramenta nova tem de ser classificada aqui de propósito, nos dois sentidos.
    flagged = {tool.name for tool in _listed_tools() if tool.annotations.destructiveHint}
    assert flagged == DESTRUCTIVE


def test_open_world_matches_whether_the_tool_talks_to_moodle():
    for tool in _listed_tools():
        assert tool.annotations.openWorldHint is (tool.name not in LOCAL_ONLY), tool.name
