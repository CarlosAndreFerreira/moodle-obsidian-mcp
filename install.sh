#!/usr/bin/env bash
# Instalação permanente do moodle-mcp a partir deste clone (Linux e macOS).
# Em Windows, os mesmos passos em PowerShell:
#   uv tool install --force ".[pdf]"
#   moodle-mcp --set obsidian_vault_dir=C:\caminho\para\o\Vault
#   moodle-mcp --install-handler
#   moodle-mcp --login
#
# Uso: ./install.sh [caminho-do-vault]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_DIR="${1:-}"

# 1. Instalação permanente, fora do repositório. O handler moodlemobile:// fica
#    registado com o caminho do executável, pelo que não pode viver numa cache
#    efémera (uvx) nem num venv que se apague com o clone.
if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$REPO_DIR[pdf]"
    BIN_DIR="$(uv tool dir --bin)"
elif command -v pipx >/dev/null 2>&1; then
    pipx install --force "$REPO_DIR[pdf]"
    BIN_DIR="${PIPX_BIN_DIR:-$HOME/.local/bin}"
else
    echo "Instala primeiro o uv (https://docs.astral.sh/uv/) ou o pipx." >&2
    exit 1
fi
MOODLE_MCP="$BIN_DIR/moodle-mcp"

# 2. Vault do Obsidian (só se for indicado; senão fica o que já estiver configurado)
if [[ -n "$VAULT_DIR" ]]; then
    mkdir -p "$VAULT_DIR"
    "$MOODLE_MCP" --set "obsidian_vault_dir=$VAULT_DIR"
fi

# 3. Handler moodlemobile:// para o login por SSO
"$MOODLE_MCP" --install-handler

echo
"$MOODLE_MCP" --show-config
echo
echo "Próximos passos:"
echo "  1. Se ainda não está, acrescenta $BIN_DIR ao PATH (uv: 'uv tool update-shell')."
echo "  2. Configura a instância (se ainda não estiver): moodle-mcp --show-config"
echo "  3. Faz login no Moodle no navegador e corre: moodle-mcp --login"
