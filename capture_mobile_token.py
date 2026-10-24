#!/usr/bin/env python3
"""Shim de compatibilidade: o capturador mudou para `moodle_mcp/token_catcher.py`.

Mantido porque o handler `moodlemobile://` registado pelo install.sh aponta para
este caminho. O comando equivalente é `moodle-mcp-token-catcher`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from moodle_mcp.token_catcher import cli  # noqa: E402

if __name__ == "__main__":
    cli()
