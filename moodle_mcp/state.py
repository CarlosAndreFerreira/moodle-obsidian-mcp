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

import json
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
