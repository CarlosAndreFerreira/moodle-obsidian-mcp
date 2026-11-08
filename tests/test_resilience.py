"""Bloco 2: cliente HTTP, login com passport, handler, organização do Vault e progresso."""

import asyncio
import base64
import json
import logging
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import responses
from conftest import BASE

from moodle_mcp import config, handler, login, server, token_catcher, transport
from moodle_mcp import vault as vaultlib

REST = f"{BASE}/webservice/rest/server.php"


# ------------------------------------------------------------------------------
# Cliente HTTP
# ------------------------------------------------------------------------------


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(transport, "BACKOFF", 0)
    transport.session.cache_clear()
    yield
    transport.session.cache_clear()


@responses.activate
def test_reads_retry_transient_failures(fast_retries):
    responses.post(REST, status=503)
    responses.post(REST, status=502)
    responses.post(REST, json={"userid": 1})
    assert server._moodle_call("core_webservice_get_site_info", instance="test") == {"userid": 1}
    assert len(responses.calls) == 3


@responses.activate
def test_writes_are_not_repeated_after_the_server_saw_them(fast_retries, monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_CONFIRM_WRITES", "client")
    responses.post(REST, status=502)
    responses.post(REST, json=[{"msgid": 1}])
    with pytest.raises(server.MoodleRequestError, match="HTTP 502"):
        server._moodle_call("core_message_send_instant_messages", {"messages[0][text]": "x"}, instance="test")
    assert len(responses.calls) == 1  # uma segunda tentativa podia enviar a mensagem duas vezes


@responses.activate
def test_requests_identify_the_client(fast_retries):
    responses.post(REST, json={"userid": 1})
    server._moodle_call("core_webservice_get_site_info", instance="test")
    assert responses.calls[0].request.headers["User-Agent"].startswith("moodle-mcp/")


@responses.activate
def test_html_instead_of_json_is_explained(fast_retries):
    responses.post(REST, body="<html>Site em manutenção</html>", content_type="text/html")
    with pytest.raises(server.MoodleRequestError, match="não é JSON"):
        server._moodle_call("core_webservice_get_site_info", instance="test")


# ------------------------------------------------------------------------------
# Edge cases de Markdown, nomes, toolsets e logging
# ------------------------------------------------------------------------------


def test_yaml_values_survive_quotes_and_newlines():
    value = 'Ética: "A" #1\nlinha'
    assert json.loads(vaultlib.yaml_scalar(value)) == value


def test_table_cells_escape_pipes_and_newlines():
    assert vaultlib.table_cell("a|b\n  c") == "a\\|b c"


def test_filenames_do_not_break_obsidian_links():
    assert vaultlib.safe_filename("UC [2026] #1 ^x") == "UC -2026- -1 -x"


def test_toolset_removal_fails_closed(monkeypatch):
    def broken(name):
        raise KeyError(name)

    monkeypatch.setattr(server.mcp, "remove_tool", broken)
    with pytest.raises(RuntimeError, match="Não foi possível retirar"):
        server.apply_toolsets({"core"})


@pytest.mark.parametrize(("value", "expected"), [("DEBUG", logging.DEBUG), ("bogus", logging.INFO)])
def test_log_level_is_applied(monkeypatch, capsys, value, expected):
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    monkeypatch.setenv("MOODLE_MCP_LOG_LEVEL", value)
    try:
        server.main(["--list-toolsets"])
        assert root.level == expected
    finally:
        root.handlers[:] = saved[1]
        root.setLevel(saved[0])


# ------------------------------------------------------------------------------
# Login com passport de uso único
# ------------------------------------------------------------------------------


def _passport(pending):
    return pending["url"].rsplit("passport=", 1)[1]


def test_start_login_stores_a_private_random_passport(cfg_dir):
    first, second = login.start_login("test"), login.start_login("test")
    assert first["url"].startswith(f"{BASE}/admin/tool/mobile/launch.php?")
    assert _passport(first) != _passport(second) and len(_passport(second)) >= 18
    if sys.platform != "win32":
        assert stat.S_IMODE((cfg_dir / "pending_login.json").stat().st_mode) == 0o600


def test_verify_accepts_the_moodle_signature_once(cfg_dir):
    pending = login.start_login("test")
    signed = login.signature(BASE, _passport(pending))
    assert login.verify(signed) == "test"
    with pytest.raises(login.LoginError, match="Não há nenhum login"):
        login.verify(signed)  # uso único: uma repetição é recusada


def test_forged_signature_is_refused_without_spoiling_the_real_login(cfg_dir):
    pending = login.start_login("test")
    with pytest.raises(login.LoginError, match="não corresponde"):
        login.verify(login.signature(BASE, "12345"))
    assert login.verify(login.signature(BASE, _passport(pending))) == "test"


def test_expired_login_is_refused(cfg_dir, monkeypatch):
    pending = login.start_login("test")
    later = time.time() + login.LOGIN_TTL_SECONDS + 1
    monkeypatch.setattr(login.time, "time", lambda: later)
    with pytest.raises(login.LoginError, match="expirou"):
        login.verify(login.signature(BASE, _passport(pending)))


def test_unsolicited_moodlemobile_url_is_refused(cfg_dir):
    payload = base64.b64encode(b"x:::TOKEN_DO_ATACANTE").decode()
    result = server.decode_mobile_launch_token(f"moodlemobile://token={payload}")
    assert result["saved"] is False and "--login" in result["message"]
    assert "test" not in config._instance_tokens_from_credentials()


@responses.activate
def test_catcher_saves_and_syncs_the_instance_that_was_logged_into(cfg_dir, monkeypatch):
    other = "https://outro.example"
    monkeypatch.setenv("MOODLE_OUTRO_URL", other)
    pending = login.start_login("outro")
    payload = base64.b64encode(f"{login.signature(other, _passport(pending))}:::novo-token".encode()).decode()
    responses.post(f"{other}/webservice/rest/server.php", json={"userid": 7, "fullname": "Aluno"})
    synced = {}
    monkeypatch.setattr(server, "sync_dashboard", lambda **kw: synced.update(kw) or {"courses_synced": 2})

    token_catcher.handle_url(f"moodlemobile://token={payload}")

    log = token_catcher.log_file().read_text(encoding="utf-8")
    assert "OK" in log and "Instância: outro" in log
    assert synced["instance"] == "outro"
    assert config.resolve_instance("outro").token == "novo-token"


# ------------------------------------------------------------------------------
# Handler do sistema operativo
# ------------------------------------------------------------------------------


def test_desktop_exec_escaping_follows_the_xdg_spec():
    line = handler.desktop_entry(["/home/a b/$x/100%/bin/catcher", "-m"]).splitlines()[3]
    assert line == 'Exec="/home/a b/\\\\$x/100%%/bin/catcher" -m %u'


def test_applescript_receives_the_url_by_apple_event():
    source = handler.applescript_source(['/Users/o"neil/my app/catcher'])
    assert source.startswith("on open location theURL")
    assert "quoted form of theURL" in source
    assert "'/Users/o\\\"neil/my app/catcher'" in source


def test_plist_declares_the_scheme_and_hides_from_dock():
    entries = handler.macos_plist_entries()
    assert entries["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["moodlemobile"]
    assert entries["LSUIElement"] is True


@pytest.mark.parametrize(
    ("path", "ephemeral"),
    [
        ("/home/u/.cache/uv/archive-v0/abc/bin/moodle-mcp-token-catcher", True),
        ("C:\\Users\\u\\AppData\\Local\\uv\\cache\\archive-v0\\x\\Scripts\\c.exe", True),
        ("/Users/u/Library/Caches/uv/archive-v0/x/bin/c", True),
        ("/home/u/.local/bin/moodle-mcp-token-catcher", False),
    ],
)
def test_ephemeral_install_detection(path, ephemeral):
    assert handler.is_ephemeral(path) is ephemeral


def test_install_warns_about_uvx_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "catcher_command", lambda: ["/home/u/.cache/uv/archive-v0/a/bin/c"])
    monkeypatch.setattr(handler.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(handler.shutil, "which", lambda name: None)  # não mexer no sistema real
    result = handler.install()
    assert any("cache temporária" in step for step in result["steps"])
    assert (tmp_path / "applications" / handler.DESKTOP_FILE).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="só em macOS")
def test_macos_applet_is_built_with_url_scheme(monkeypatch, tmp_path):
    import plistlib

    monkeypatch.setenv("HOME", str(tmp_path))
    result = handler.install()
    bundle = Path(result["path"])
    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["moodlemobile"]
    assert (bundle / "Contents" / "Resources" / "Scripts" / "main.scpt").exists()
    assert handler.uninstall()["removed"] == [str(bundle)]


# ------------------------------------------------------------------------------
# Organização do Vault e sincronização assíncrona
# ------------------------------------------------------------------------------


@pytest.fixture
def small_moodle(monkeypatch, vault):
    calls = []

    def call(wsfunction, params=None, method="POST", instance="test"):
        calls.append(wsfunction)
        if wsfunction == "core_webservice_get_site_info":
            return {"userid": 1, "fullname": "Aluno"}
        return {}

    monkeypatch.setattr(server, "_moodle_call", call)
    monkeypatch.setattr(
        server,
        "get_enrolled_courses",
        lambda *a, **k: {"courses": [{"id": 1, "fullname": "Ética"}, {"id": 2, "fullname": "Lógica"}]},
    )
    for name, value in (
        ("get_assignments", {"courses": []}),
        ("get_course_grades", {}),
        ("get_course_forums", []),
        ("get_course_contents", []),
    ):
        monkeypatch.setattr(server, name, lambda *a, _v=value, **k: _v)
    return calls


def test_sync_subdir_moves_everything_and_links_follow(small_moodle, vault, monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_SYNC_SUBDIR", "Moodle")
    server.sync_dashboard(instance="test")
    dashboard = (vault / "Moodle" / "Doutoramento - Painel Moodle.md").read_text(encoding="utf-8")
    assert "[[Moodle/Unidades Curriculares/Ética\\|Abrir UC]]" in dashboard
    note = (vault / "Moodle" / "Unidades Curriculares" / "Ética.md").read_text(encoding="utf-8")
    assert "[[Moodle/Doutoramento - Painel Moodle\\|" in note
    assert not (vault / "Unidades Curriculares").exists()


def test_each_instance_gets_its_own_dashboard(small_moodle, vault, monkeypatch):
    monkeypatch.setenv("MOODLE_OUTRO_URL", "https://outro.example")
    monkeypatch.setenv("MOODLE_MCP_DASHBOARD_NAME", "Painel")
    server.sync_dashboard(instance="test")
    assert (vault / "Painel (test).md").exists()


@responses.activate
def test_materials_respect_vault_dir(vault, monkeypatch):
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: [])
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})
    result = server.download_course_materials(5, instance="test", vault_dir="Sub")
    assert Path(result["materials_dir"]) == vault / "Sub" / "Unidades Curriculares" / "UC" / "Materiais"


def test_vault_dir_cannot_escape_the_vault(vault):
    with pytest.raises(ValueError, match="fora do Vault"):
        server._sync_root("../fora")


def test_async_sync_reports_progress(small_moodle):
    reported = []

    async def report_progress(done, total, message):
        reported.append((done, total, message))

    ctx = SimpleNamespace(report_progress=report_progress)
    result = asyncio.run(server.sync_doctoral_dashboard_to_obsidian(instance="test", ctx=ctx))
    assert result.courses_synced == 2
    assert reported == [(0, 2, "Ética"), (1, 2, "Lógica"), (2, 2, "painel")]


def test_long_sync_does_not_block_the_server(small_moodle, monkeypatch):
    """Enquanto a sincronização corre, o servidor continua a responder a outros pedidos."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    slow = server.sync_dashboard

    def slow_sync(*args, **kwargs):
        time.sleep(0.6)
        return slow(*args, **kwargs)

    monkeypatch.setattr(server, "sync_dashboard", slow_sync)

    async def scenario():
        async with connect(server.mcp._mcp_server) as client:
            sync = asyncio.create_task(
                client.call_tool("sync_doctoral_dashboard_to_obsidian", {"instance": "test"})
            )
            await asyncio.sleep(0.1)
            started = time.monotonic()
            await client.list_tools()
            answered_in = time.monotonic() - started
            result = await sync
            return answered_in, result

    answered_in, result = asyncio.run(scenario())
    assert answered_in < 0.4
    assert not result.isError
