"""Sincronização sem cliente (`--sync`) e agendada (`--schedule`), e o lock do estado."""

import plistlib
import threading
import time

import pytest
from conftest import TOKEN

from moodle_mcp import schedule, server, state


@pytest.fixture
def sched(tmp_path, monkeypatch, cfg_dir):
    calls = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(schedule, "_launchd_file", lambda: tmp_path / "LaunchAgents" / "x.plist")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(schedule.sys, "executable", str(tmp_path / "sem-venv" / "python"))
    monkeypatch.setattr(schedule.os, "getuid", lambda: 1000, raising=False)
    return calls, tmp_path


def _recorder(calls):
    return lambda argv: calls.append(argv)


def test_linux_timer_is_written_and_enabled_without_secrets(sched, monkeypatch):
    calls, tmp = sched
    monkeypatch.setenv("OBSIDIAN_VAULT_DIR", "/home/eu/Vault 100%")
    summary = schedule.install(6, materials=True, platform="linux", run=_recorder(calls))

    folder = tmp / "xdg" / "systemd" / "user"
    service = (folder / "moodle-mcp-sync.service").read_text(encoding="utf-8")
    timer = (folder / "moodle-mcp-sync.timer").read_text(encoding="utf-8")
    exe = schedule.sync_command()[0]  # moodle-mcp.exe em Windows
    assert f'ExecStart="{exe}" "--sync" "--materials"' in service
    assert 'Environment="OBSIDIAN_VAULT_DIR=/home/eu/Vault 100%%"' in service  # % escapado
    assert "OnCalendar=*-*-* 00/6:15:00" in timer and "Persistent=true" in timer
    assert TOKEN not in service + timer and "MOODLE_TEST_TOKEN" not in service
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "moodle-mcp-sync.timer"]
    assert summary["every_hours"] == 6

    # Repetir substitui; remover limpa; remover outra vez não falha.
    schedule.install(24, platform="linux", run=_recorder(calls))
    assert "OnCalendar=*-*-* 07:15:00" in (folder / "moodle-mcp-sync.timer").read_text(encoding="utf-8")
    assert len(schedule.uninstall(platform="linux", run=_recorder(calls))["removed"]) == 2
    assert not list(folder.iterdir())
    assert schedule.uninstall(platform="linux", run=_recorder(calls))["removed"] == []


def test_macos_launch_agent(sched):
    calls, tmp = sched
    schedule.install(12, platform="darwin", run=_recorder(calls))
    data = plistlib.loads((tmp / "LaunchAgents" / "x.plist").read_bytes())
    assert data["ProgramArguments"] == [schedule.sync_command()[0], "--sync"]
    assert data["StartInterval"] == 12 * 3600
    assert TOKEN not in str(data)
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    assert schedule.uninstall(platform="darwin", run=_recorder(calls))["removed"]


def test_windows_task(sched):
    calls, _tmp = sched
    schedule.install(3, platform="win32", run=_recorder(calls))
    argv = calls[-1]
    assert argv[:5] == ["schtasks", "/Create", "/F", "/TN", "moodle-mcp-sync"]
    assert argv[argv.index("/MO") + 1] == "3" and argv[-1].endswith("--sync")
    assert schedule.uninstall(platform="win32", run=_recorder(calls))["removed"] == ["moodle-mcp-sync"]


@pytest.mark.parametrize("hours", [0, 25])
def test_invalid_interval(sched, hours):
    with pytest.raises(schedule.ScheduleError):
        schedule.install(hours, platform="linux", run=lambda argv: None)


def test_cli_rejects_bad_interval(sched):
    with pytest.raises(SystemExit) as info:
        server.main(["--schedule", "0"])
    assert info.value.code == 2


def test_run_sync_writes_a_log_and_returns_status(cfg_dir, monkeypatch, capsys):
    results = iter(
        [
            {"courses_synced": 2, "courses_total": 2, "changes_count": 3, "errors": []},
            {"courses_synced": 1, "courses_total": 2, "changes_count": 0, "errors": [{"item": "x"}]},
        ]
    )
    monkeypatch.setattr(server, "sync_dashboard", lambda **k: next(results))
    assert server.run_sync("test") == 0
    assert "2/2 UCs, 3 mudanças, 0 erros" in (cfg_dir / "last_sync.log").read_text(encoding="utf-8")
    assert server.run_sync("test") == 1


def test_run_sync_survives_an_exception(cfg_dir, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("Moodle em baixo")

    monkeypatch.setattr(server, "sync_dashboard", boom)
    assert server.run_sync("test") == 1
    assert "ERRO RuntimeError: Moodle em baixo" in (cfg_dir / "last_sync.log").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------
# Lock do estado
# ------------------------------------------------------------------------------


def test_lock_serialises_read_modify_write(vault):
    def worker(n):
        for _ in range(10):
            with state.locked(vault, "test"):
                data = state.load(vault, "test")
                data.setdefault("files", {})[f"{n}-{len(data['files'])}"] = 1
                time.sleep(0.001)
                state.save(vault, "test", data)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(state.load(vault, "test")["files"]) == 40  # nenhuma escrita perdida
    assert not state.state_file(vault, "test").with_suffix(".lock").exists()


def test_stale_lock_is_taken_over(vault, monkeypatch):
    lock = state.state_file(vault, "test").with_suffix(".lock")
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    old = time.time() - state.LOCK_STALE_SECONDS - 5
    import os

    os.utime(lock, (old, old))
    started = time.monotonic()
    with state.locked(vault, "test"):
        pass
    assert time.monotonic() - started < 1


def test_lock_gives_up_instead_of_hanging(vault, monkeypatch):
    monkeypatch.setattr(state, "LOCK_WAIT_SECONDS", 0.2)
    lock = state.state_file(vault, "test").with_suffix(".lock")
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    with state.locked(vault, "test"):
        pass
    assert lock.exists()  # o lock de outro processo não é apagado
