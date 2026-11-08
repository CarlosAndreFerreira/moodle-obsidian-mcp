"""Sincronização automática: `moodle-mcp --schedule` regista uma tarefa no sistema.

Usa o agendador nativo de cada sistema, sem processo residente:

* Linux: um timer do systemd de utilizador (`~/.config/systemd/user`), com
  `Persistent=true` para correr à primeira oportunidade depois de o computador
  ter estado desligado.
* macOS: um LaunchAgent (`~/Library/LaunchAgents`) com `StartInterval`.
* Windows: uma tarefa do Agendador de Tarefas (`schtasks`).

A tarefa corre `moodle-mcp --sync`, que lê a configuração e o token dos
ficheiros de sempre (`config.toml`, `credentials.json`). Nenhum token entra nos
ficheiros da tarefa nem na linha de comandos, onde qualquer utilizador da
máquina o veria. As opções não secretas que estejam definidas no ambiente de
quem corre `--schedule` (p.ex. `OBSIDIAN_VAULT_DIR`) são copiadas para a
tarefa, porque o agendador não herda o ambiente do terminal nem o do cliente
MCP.

`--schedule` e `--unschedule` são idempotentes: repetir substitui ou remove a
mesma tarefa.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from moodle_mcp import config, vault

NAME = "moodle-mcp-sync"
LAUNCHD_LABEL = "io.github.carlosandreferreira.moodle-mcp-sync"

Runner = Callable[[list[str]], Any]


class ScheduleError(RuntimeError):
    """O agendamento não pôde ser criado ou removido."""


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=60)


def sync_command(materials: bool = False) -> list[str]:
    """Comando que a tarefa executa, com caminho absoluto (o agendador não tem o PATH do terminal)."""
    name = "moodle-mcp" + (".exe" if sys.platform == "win32" else "")
    beside = Path(sys.executable).parent / name
    found = str(beside) if beside.exists() else shutil.which(name)
    argv = [found] if found else [sys.executable, "-m", "moodle_mcp.server"]
    return [*argv, "--sync", *(["--materials"] if materials else [])]


def _environment() -> dict[str, str]:
    """Opções não secretas definidas no ambiente atual (nunca tokens)."""
    names = [env for env, _default in config.SETTINGS.values()] + ["MOODLE_MCP_CONFIG_DIR"]
    return {name: os.environ[name] for name in names if os.environ.get(name)}


def _systemd_dir() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _launchd_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_quote(part: str) -> str:
    """Aspas para `ExecStart=` (o systemd separa por espaços e expande `%` e `$`)."""
    escaped = part.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _calendar(hours: int) -> str:
    """`OnCalendar` para correr a cada `hours` horas (24 = uma vez por dia, de manhã)."""
    return "*-*-* 07:15:00" if hours >= 24 else f"*-*-* 00/{hours}:15:00"


def systemd_units(hours: int, materials: bool = False) -> dict[str, str]:
    """Conteúdo do `.service` e do `.timer`."""
    env_lines = [f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in sorted(_environment().items())]
    service = "\n".join(
        [
            "[Unit]",
            "Description=Sincronizar o Moodle com o Obsidian (moodle-obsidian-mcp)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=" + " ".join(_systemd_quote(p) for p in sync_command(materials)),
            *env_lines,
            "Nice=10",
            "",
        ]
    )
    timer = "\n".join(
        [
            "[Unit]",
            "Description=Sincronização periódica do Moodle com o Obsidian",
            "",
            "[Timer]",
            f"OnCalendar={_calendar(hours)}",
            "Persistent=true",
            # Espalha os pedidos dos estudantes que usem a mesma configuração.
            "RandomizedDelaySec=10min",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return {f"{NAME}.service": service, f"{NAME}.timer": timer}


def launchd_plist(hours: int, materials: bool = False) -> bytes:
    """Conteúdo do LaunchAgent."""
    log = str(config.config_dir() / "schedule.log")
    data: dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": sync_command(materials),
        "StartInterval": hours * 3600,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    env = _environment()
    if env:
        data["EnvironmentVariables"] = env
    return plistlib.dumps(data)


def _schtasks_command(materials: bool) -> str:
    return " ".join(f'"{part}"' if " " in part or not part else part for part in sync_command(materials))


def preflight() -> tuple[Path, list[str]]:
    """Confirma que a tarefa agendada vai encontrar o Vault e pelo menos um token.

    O agendador não vê o ambiente do cliente MCP. Quem só tem o Vault ou as
    instâncias no `env` do cliente ficava com uma tarefa que escrevia numa
    pasta errada, ou que falhava em silêncio a cada execução.

    Returns:
        A raiz do Vault e os nomes das instâncias com token.

    Raises:
        ScheduleError: Se o Vault não existir ou nenhuma instância tiver token.

    """
    root = vault.vault_root()
    if not root.is_dir():
        raise ScheduleError(
            f"O Vault {root} não existe. Grava o caminho certo com "
            "`moodle-mcp --set obsidian_vault_dir=<pasta>` (o agendador não vê o env do cliente MCP)."
        )
    ready = sorted(name for name, inst in config.instances().items() if inst.token)
    if not ready:
        raise ScheduleError(
            "Nenhuma instância com token no config.toml/credentials.json. Faz `moodle-mcp --login` primeiro."
        )
    return root, ready


def install(
    hours: int = 6, materials: bool = False, platform: str = sys.platform, run: Runner = _run
) -> dict[str, Any]:
    """Cria (ou substitui) a tarefa de sincronização periódica.

    Args:
        hours: Intervalo entre sincronizações, de 1 a 24.
        materials: Se a tarefa também descarrega os materiais novos.
        platform: `sys.platform` (parâmetro para os testes).
        run: Executor de comandos (parâmetro para os testes).

    Returns:
        O que foi criado.

    Raises:
        ScheduleError: Intervalo inválido, ou o agendador recusou.

    """
    if not 1 <= int(hours) <= 24:
        raise ScheduleError("O intervalo tem de estar entre 1 e 24 horas.")
    hours = int(hours)
    root, ready = preflight()
    summary: dict[str, Any] = {
        "command": " ".join(sync_command(materials)),
        "every_hours": hours,
        "vault": str(root),
        "instances": ready,
    }
    try:
        if platform.startswith("linux"):
            if not shutil.which("systemctl"):
                raise ScheduleError("Sem systemd: agenda `moodle-mcp --sync` no cron, p.ex. `0 */6 * * *`.")
            folder = _systemd_dir()
            folder.mkdir(parents=True, exist_ok=True)
            written = []
            for name, text in systemd_units(hours, materials).items():
                (folder / name).write_text(text, encoding="utf-8")
                written.append(str(folder / name))
            run(["systemctl", "--user", "daemon-reload"])
            run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"])
            summary.update(files=written, check=f"systemctl --user list-timers {NAME}.timer")
        elif platform == "darwin":
            plist = _launchd_file()
            plist.parent.mkdir(parents=True, exist_ok=True)
            domain = f"gui/{os.getuid()}"  # type: ignore[attr-defined,unused-ignore]
            if plist.exists():  # pode já não estar carregado: um erro aqui não impede repetir
                _quiet(run, ["launchctl", "bootout", domain, str(plist)])
            plist.write_bytes(launchd_plist(hours, materials))
            run(["launchctl", "bootstrap", domain, str(plist)])
            summary.update(files=[str(plist)], check=f"launchctl print {domain}/{LAUNCHD_LABEL}")
        elif platform == "win32":
            # `/SC HOURLY` só aceita `/MO` de 1 a 23; 24 horas é uma vez por dia, de manhã.
            every = ["/SC", "DAILY", "/ST", "07:15"] if hours >= 24 else ["/SC", "HOURLY", "/MO", str(hours)]
            run(["schtasks", "/Create", "/F", "/TN", NAME, *every, "/TR", _schtasks_command(materials)])
            summary.update(files=[], check=f"schtasks /Query /TN {NAME}")
        else:
            raise ScheduleError(f"Sistema não suportado: {platform}. Agenda `moodle-mcp --sync` à mão.")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScheduleError(f"Não foi possível criar a tarefa: {exc}") from exc
    return summary


def uninstall(platform: str = sys.platform, run: Runner = _run) -> dict[str, Any]:
    """Remove a tarefa; não falha se ela não existir."""
    removed: list[str] = []
    if platform.startswith("linux"):
        folder = _systemd_dir()
        if shutil.which("systemctl") and (folder / f"{NAME}.timer").exists():
            _quiet(run, ["systemctl", "--user", "disable", "--now", f"{NAME}.timer"])
        for name in (f"{NAME}.timer", f"{NAME}.service"):
            if (folder / name).exists():
                (folder / name).unlink()
                removed.append(str(folder / name))
        if removed and shutil.which("systemctl"):
            _quiet(run, ["systemctl", "--user", "daemon-reload"])
    elif platform == "darwin":
        plist = _launchd_file()
        if plist.exists():
            _quiet(run, ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])  # type: ignore[attr-defined,unused-ignore]
            plist.unlink()
            removed.append(str(plist))
    elif platform == "win32":
        if _quiet(run, ["schtasks", "/Delete", "/F", "/TN", NAME]):
            removed.append(NAME)
    return {"removed": removed}


def _quiet(run: Runner, argv: list[str]) -> bool:
    try:
        run(argv)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
