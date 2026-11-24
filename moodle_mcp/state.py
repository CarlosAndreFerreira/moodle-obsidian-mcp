"""Estado da sincronização, guardado no Vault, para não repetir trabalho.

Para cada ficheiro do Moodle guarda o `timemodified` e o `filesize` com que foi
descarregado e onde ficou. Numa sincronização seguinte, um ficheiro com os
mesmos valores e que ainda existe no Vault não volta a ser descarregado: as
UCs de doutoramento têm dezenas de PDFs, e descarregar tudo de cada vez era
lento para o estudante e pesado para o servidor da universidade.

O estado vive em `<Vault>/.moodle-mcp/state-<instância>.json`, junto dos
ficheiros a que se refere, para continuar certo se o Vault for movido ou
sincronizado entre computadores.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from moodle_mcp import config

#: Nomes de instância aceites no nome do ficheiro de estado (os de `--set instances.<nome>.url`).
_INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def state_file(root: Path, instance: str) -> Path:
    """Caminho do ficheiro de estado de uma instância.

    O nome vem do parâmetro `instance` das ferramentas, que o modelo escolhe e
    que, com uma só instância configurada, é texto livre (um nome desconhecido
    é encaminhado para a única instância). Sem esta validação,
    `t/../../../.claude/settings` gravava o estado em `~/.claude/settings.json`,
    fora do Vault.

    Raises:
        ValueError: Se o nome não for um nome de instância válido.

    """
    name = (instance or "").strip().lower()
    if not _INSTANCE_RE.fullmatch(name):
        raise ValueError(f"Nome de instância inválido: {instance!r} (letras, dígitos, '_' e '-').")
    return root / ".moodle-mcp" / f"state-{name}.json"


def load(root: Path, instance: str) -> dict[str, Any]:
    """Lê o estado; um ficheiro ausente ou ilegível conta como vazio (tudo é descarregado)."""
    try:
        data = json.loads(state_file(root, instance).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(root: Path, instance: str, data: dict[str, Any]) -> None:
    """Grava o estado de forma atómica."""
    path = state_file(root, instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(path, json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", 0o644)


#: Um lock mais velho do que isto é de um processo que morreu a meio: é ignorado.
LOCK_STALE_SECONDS = 120
LOCK_WAIT_SECONDS = 30
_WINDOWS = os.name == "nt"


@contextlib.contextmanager
def locked(root: Path, instance: str) -> Iterator[None]:
    """Exclusão mútua entre processos à volta de um ler-alterar-gravar do estado.

    Uma sincronização agendada pode correr ao mesmo tempo que uma pedida no
    cliente MCP. Sem isto, as duas liam o estado, alteravam-no e gravavam, e a
    última apagava o que a outra acabara de registar. O lock só envolve a
    leitura e a gravação finais (segundos), nunca a sincronização inteira, e é
    um ficheiro criado com `O_EXCL`, o que funciona em Linux, macOS e Windows.
    Se não for possível obtê-lo em `LOCK_WAIT_SECONDS`, segue sem ele: perder um
    registo obriga, no pior caso, a descarregar um ficheiro outra vez.

    No Windows, criar o ficheiro no instante em que o dono do lock o está a
    apagar dá `PermissionError` e não `FileExistsError` (o nome só desaparece
    quando o último handle fecha). Isso é contenção, não falta de permissões:
    tratá-lo como erro definitivo fazia seguir sem lock e perder uma escrita.
    """
    path = state_file(root, instance).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    acquired = False
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            acquired = True
            break
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
            except FileNotFoundError:
                continue  # o dono acabou de o largar: tenta já
            except OSError:
                stale = False
            if stale:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
        except PermissionError:
            if not _WINDOWS:
                break  # em POSIX é mesmo falta de permissões na pasta
        except OSError:
            break
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            _release(path)


def _release(path: Path) -> None:
    """Apaga o ficheiro de lock, insistindo enquanto o Windows o der como em uso.

    Outro processo a ver se o lock está abandonado (`stat`) mantém um handle
    aberto por instantes; um `unlink` nesse momento falha com `PermissionError`.
    Desistir deixava o lock preso até ficar abandonado (`LOCK_STALE_SECONDS`).
    """
    for _ in range(100):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.01)


def file_key(fileurl: str) -> str:
    """Identificador estável de um ficheiro do Moodle (o URL sem query)."""
    return fileurl.split("?", 1)[0]


def is_unchanged(files: dict[str, Any], fileurl: str, item: dict[str, Any], target: Path) -> bool:
    """Diz se o ficheiro já foi descarregado com a mesma versão e ainda está no Vault."""
    known = files.get(file_key(fileurl))
    if not isinstance(known, dict) or not target.exists():
        return False
    return known.get("timemodified") == item.get("timemodified") and known.get("filesize") == item.get(
        "filesize"
    )


def remember(files: dict[str, Any], fileurl: str, item: dict[str, Any], target: Path, root: Path) -> None:
    """Regista a versão descarregada de um ficheiro."""
    try:
        where = target.relative_to(root).as_posix()
    except ValueError:
        where = str(target)
    files[file_key(fileurl)] = {
        "timemodified": item.get("timemodified"),
        "filesize": item.get("filesize"),
        "path": where,
    }
