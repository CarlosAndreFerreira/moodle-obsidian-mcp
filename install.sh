#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_DIR="${1:-$HOME/Doutoramento}"

echo "🚀 A instalar Moodle-Obsidian MCP Suite em: $REPO_DIR"
mkdir -p "$VAULT_DIR"

# 1. Criar ambiente virtual Python (via uv ou python3 -m venv)
if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$REPO_DIR/.venv"
    uv pip install --python "$REPO_DIR/.venv/bin/python" -e "$REPO_DIR[pdf]"
else
    python3 -m venv "$REPO_DIR/.venv"
    "$REPO_DIR/.venv/bin/pip" install --upgrade pip
    "$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR[pdf]"
fi

# 2. Escrever a configuração no diretório do utilizador (não junto ao código:
#    com uvx/pipx o pacote corre de uma cache efémera e possivelmente só de leitura)
"$REPO_DIR/.venv/bin/python" - "$VAULT_DIR" <<'PYEOF'
import sys
from moodle_mcp import config
config.save_setting("obsidian_vault_dir", sys.argv[1])
print(f"✅ Configuração em {config.config_file()}")
PYEOF

# 3. Registar o handler moodlemobile:// (o próprio programa sabe fazê-lo em
#    Linux, Windows e macOS; aqui só o invocamos)
"$REPO_DIR/.venv/bin/moodle-mcp" --install-handler
