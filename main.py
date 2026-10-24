#!/usr/bin/env python3
"""Shim de compatibilidade: o servidor mudou para `moodle_mcp/server.py`.

Mantido porque as configurações de clientes MCP já existentes apontam para
`main.py` com um caminho absoluto. A forma recomendada passou a ser o comando
`moodle-mcp` (ver pyproject.toml) ou `python -m moodle_mcp.server`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from moodle_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
