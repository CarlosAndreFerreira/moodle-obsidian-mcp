"""Lacunas fechadas depois dos três blocos: ferramentas em thread, --set, tokens e links."""

import asyncio
import json
import time

import pytest
import responses
from conftest import BASE

from moodle_mcp import config, server
from moodle_mcp import vault as vaultlib

REST = f"{BASE}/webservice/rest/server.php"


# ------------------------------------------------------------------------------
# Ferramentas síncronas correm numa thread
# ------------------------------------------------------------------------------


def test_all_tools_become_async_and_offload_is_idempotent():
    server.offload_blocking_tools()
    assert all(tool.is_async for tool in server._iter_registered_tools())
    assert server.offload_blocking_tools() == 0


def test_python_functions_stay_synchronous():
    # A sincronização e o capturador chamam estas funções diretamente.
    server.offload_blocking_tools()
    assert not asyncio.iscoroutinefunction(server.get_site_info)


def test_slow_read_tool_does_not_block_the_server(monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    def slow_call(wsfunction, params=None, method="POST", instance="test"):
        time.sleep(0.6)
        return {"userid": 1, "sitename": "lento"}

    monkeypatch.setattr(server, "_moodle_call", slow_call)
    server.offload_blocking_tools()

    async def scenario():
        async with connect(server.mcp._mcp_server) as client:
            slow = asyncio.create_task(client.call_tool("get_site_info", {"instance": "test"}))
            await asyncio.sleep(0.1)
            started = time.monotonic()
            await client.list_tools()
            answered_in = time.monotonic() - started
            return answered_in, await slow

    answered_in, result = asyncio.run(scenario())
    assert answered_in < 0.4
    assert not result.isError and "lento" in result.content[0].text


# ------------------------------------------------------------------------------
# moodle-mcp --set
# ------------------------------------------------------------------------------


def test_set_writes_settings_and_instance_urls(cfg_dir, capsys):
    assert server.main(["--set", "sync_subdir=Moodle", "--set", "instances.UTAD.url=https://u.example/"]) == 0
    assert config.get("sync_subdir") == "Moodle"
    assert config.instances()["utad"].url == "https://u.example"


@pytest.mark.parametrize("item", ["token=x", "instances.x.url=http://inseguro.example", "sem-igual"])
def test_set_refuses_unknown_keys_and_insecure_urls(cfg_dir, item):
    with pytest.raises(SystemExit):
        server.main(["--set", item])
    assert "x" not in config.instances()


# ------------------------------------------------------------------------------
# Token certo em /user/managetoken.php
# ------------------------------------------------------------------------------

OTHER, MOBILE = "a" * 32, "b" * 32
PAGE = (
    "<table>"
    f"<tr><td>Serviço de relatórios</td><td>{OTHER}</td></tr>"
    f"<tr><td>Moodle mobile web service</td><td>{MOBILE}</td></tr>"
    "</table>"
)


def test_mobile_service_tokens_come_first():
    assert server._managetoken_candidates(PAGE) == [MOBILE, OTHER]


@responses.activate
def test_cookie_extraction_saves_only_a_token_moodle_accepts(cfg_dir):
    uab = "https://uab.example"
    (cfg_dir / "config.toml").write_text(f'[instances.uab]\nurl = "{uab}"\n', encoding="utf-8")
    page = PAGE.replace("Moodle mobile web service", "Outro")  # nenhum marcado como móvel
    responses.get(f"{uab}/user/managetoken.php", body=page)

    def site_info(request):
        token = request.body.split("wstoken=")[1].split("&")[0]
        if token == OTHER:
            return 200, {}, json.dumps({"exception": "x", "errorcode": "invalidtoken"})
        return 200, {}, json.dumps({"userid": 3, "fullname": "Aluno"})

    responses.add_callback(responses.POST, f"{uab}/webservice/rest/server.php", callback=site_info)

    result = server.extract_token_from_session_cookie("cookie", instance="uab")

    assert result["saved"] is True
    assert config.resolve_instance("uab").token == MOBILE


# ------------------------------------------------------------------------------
# Links Markdown com nomes vindos do Moodle
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "url", "expected"),
    [
        ("Guia [v2]", "https://m/x?id=1", "[Guia \\[v2\\]](https://m/x?id=1)"),
        ("a\nb", "https://m/a b(c).pdf", "[a b](<https://m/a b(c).pdf>)"),
        ("", "https://m/<x>", "[link](<https://m/%3Cx%3E>)"),
    ],
)
def test_md_link(text, url, expected):
    assert vaultlib.md_link(text, url) == expected


def test_course_note_links_survive_odd_module_names(vault, monkeypatch):
    contents = [
        {"name": "Tema", "modules": [{"name": "Guia ](x) [v2]", "url": "https://m/view.php?id=1 (a)"}]}
    ]

    def call(wsfunction, params=None, method="POST", instance="test"):
        return {"userid": 1, "fullname": "Aluno"} if wsfunction == "core_webservice_get_site_info" else {}

    monkeypatch.setattr(server, "_moodle_call", call)
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 1, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})
    monkeypatch.setattr(server, "get_course_grades", lambda *a, **k: {})
    monkeypatch.setattr(server, "get_course_forums", lambda *a, **k: [])
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: contents)

    server.sync_dashboard(instance="test")

    note = (vault / "Unidades Curriculares" / "UC.md").read_text(encoding="utf-8")
    assert "[Guia \\](x) \\[v2\\]](<https://m/view.php?id=1 (a)>)" in note
