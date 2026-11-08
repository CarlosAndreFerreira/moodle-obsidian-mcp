"""Handler moodlemobile://: o token não fica visível na lista de processos."""

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from moodle_mcp import config, login, token_catcher

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="execve e /proc só em POSIX")
FAKE_TOKEN = "f" * 32


def _url(token=FAKE_TOKEN, signature="0" * 32):
    return "moodlemobile://token=" + base64.b64encode(f"{signature}:::{token}:::priv".encode()).decode()


# ------------------------------------------------------------------------------
# O URL sai dos argumentos antes de qualquer trabalho
# ------------------------------------------------------------------------------


@posix_only
def test_cli_reexecs_without_the_url_in_argv(monkeypatch):
    seen = {}

    def fake_execve(path, argv, env):
        seen.update(path=path, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setattr(token_catcher.os, "execve", fake_execve)
    monkeypatch.delenv(token_catcher.ENV_URL, raising=False)
    monkeypatch.setattr(sys, "argv", ["moodle-mcp-token-catcher", _url()])

    with pytest.raises(SystemExit):
        token_catcher.cli()

    assert seen["argv"] == [sys.executable, "-m", "moodle_mcp.token_catcher"]
    assert not any(FAKE_TOKEN in arg or "token=" in arg for arg in seen["argv"])
    assert seen["env"][token_catcher.ENV_URL] == _url()


def test_cli_reads_the_url_from_the_environment_and_removes_it(monkeypatch):
    handled = []
    monkeypatch.setattr(token_catcher, "handle_url", handled.append)
    monkeypatch.setenv(token_catcher.ENV_URL, _url())
    monkeypatch.setattr(sys, "argv", ["python"])

    assert token_catcher.cli() == 0
    assert handled == [_url()]
    assert token_catcher.ENV_URL not in os.environ  # não passa para subprocessos


def test_cli_falls_back_when_execve_fails(monkeypatch):
    handled = []

    def broken_execve(*args):
        raise OSError("sem execve")

    monkeypatch.setattr(token_catcher.os, "execve", broken_execve)
    monkeypatch.setattr(token_catcher, "handle_url", handled.append)
    monkeypatch.delenv(token_catcher.ENV_URL, raising=False)
    monkeypatch.setattr(sys, "argv", ["moodle-mcp-token-catcher", _url()])

    assert token_catcher.cli() == 0
    assert handled == [_url()]


def test_importing_the_catcher_does_not_load_the_server():
    """Os imports pesados só acontecem depois do execve (janela curta)."""
    code = (
        "import sys, moodle_mcp.token_catcher; "
        "print('moodle_mcp.server' in sys.modules, 'mcp' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "False"]


@posix_only
@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="precisa de /proc")
def test_real_process_hides_the_token_from_other_users(tmp_path):
    """Lê o /proc/<pid>/cmdline do handler real enquanto corre, como `ps` faria."""
    env = {**os.environ, "MOODLE_MCP_CONFIG_DIR": str(tmp_path), "MOODLE_TEST_URL": "https://m.example"}
    env.pop(token_catcher.ENV_URL, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "moodle_mcp.token_catcher", _url()],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cmdlines = []
    started = time.monotonic()
    while proc.poll() is None and time.monotonic() - started < 20:
        try:
            cmdlines.append((time.monotonic() - started, Path(f"/proc/{proc.pid}/cmdline").read_bytes()))
        except OSError:
            break
        time.sleep(0.005)
    proc.wait(timeout=20)

    with_token = [
        t for t, c in cmdlines if FAKE_TOKEN.encode() in base64.b64decode(c.split(b"token=")[-1] + b"==")
    ]
    without = [t for t, c in cmdlines if b"token=" not in c]
    assert without, "o processo nunca chegou a correr sem o URL nos argumentos"
    # O URL só pode aparecer no arranque, antes do execve (milissegundos), nunca depois.
    assert not with_token or max(with_token) < min(without)
    assert not with_token or max(with_token) < 0.5


# ------------------------------------------------------------------------------
# Chamadas duplicadas do navegador
# ------------------------------------------------------------------------------


def test_repeat_of_an_accepted_login_is_ignored_without_logging(cfg_dir, monkeypatch):
    monkeypatch.setenv("MOODLE_UAB_URL", "https://uab.example")
    config.save_instance_token("uab", FAKE_TOKEN)  # o token acabado de aceitar
    log = token_catcher.log_file()
    log.write_text("[t] OK\n", encoding="utf-8")

    token_catcher.handle_url(_url())

    assert log.read_text(encoding="utf-8") == "[t] OK\n"  # o resultado bom fica


def test_unknown_token_without_pending_login_is_still_refused(cfg_dir):
    token_catcher.handle_url(_url(token="e" * 32))
    assert "ERRO" in token_catcher.log_file().read_text(encoding="utf-8")


def test_known_token_with_a_pending_login_is_not_treated_as_repeat(cfg_dir, monkeypatch):
    monkeypatch.setenv("MOODLE_UAB_URL", "https://uab.example")
    config.save_instance_token("uab", FAKE_TOKEN)
    login.start_login("uab")
    assert token_catcher._is_repeat_of_accepted_login(_url()) is False


def test_payload_parsing_is_shared_with_the_server():
    assert login.parse_launch_payload(_url())[1] == FAKE_TOKEN
    assert login.parse_launch_payload("moodlemobile://token=!!!") == [""]
