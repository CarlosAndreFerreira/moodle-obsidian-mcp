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
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from moodle_mcp import config


def state_file(root: Path, instance: str) -> Path:
    """Caminho do ficheiro de estado de uma instância."""
    return root / ".moodle-mcp" / f"state-{instance}.json"


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


@contextlib.contextmanager
def locked(root: Path, instance: str) -> Iterator[None]:
    """Exclusão mútua entre processos à volta de um ler-alterar-gravar do estado.

    Uma sincronização agendada pode correr ao mesmo tempo que uma pedida no
    cliente MCP. Sem isto, as duas liam o estado, alteravam-no e gravavam, e a
    última apagava o que a outra acabara de registar. O lock só envolve a
    leitura e a gravação finais (segundos), nunca a sincronização inteira, e é
    um ficheiro criado com `O_EXCL`, o que funciona igual em Linux, macOS e
    Windows. Se não for possível obtê-lo em `LOCK_WAIT_SECONDS`, segue sem ele:
    perder um registo obriga, no pior caso, a descarregar um ficheiro outra vez.
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
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                break
            time.sleep(0.1)
        except OSError:
            break
    try:
        yield
    finally:
        if acquired:
            path.unlink(missing_ok=True)


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
