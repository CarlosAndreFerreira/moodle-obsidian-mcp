"""Isolamento dos testes: configuração temporária e nenhum segredo real.

O `moodle_mcp.server` lê a configuração na importação (Vault e instância
predefinida), pelo que o ambiente tem de ser preparado aqui, antes de qualquer
teste o importar. O `.env` legado do repositório é desligado para que um token
verdadeiro que lá esteja nunca entre num teste.
"""

import os
import tempfile
from pathlib import Path

import pytest

BASE = "https://moodle.example.edu"
TOKEN = "0123456789abcdef0123456789abcdef"

_TMP = Path(tempfile.mkdtemp(prefix="moodle-mcp-tests-"))

for _key in [k for k in os.environ if k.startswith(("MOODLE_", "OBSIDIAN_"))]:
    del os.environ[_key]
os.environ.update(
    {
        "MOODLE_MCP_CONFIG_DIR": str(_TMP / "cfg"),
        "OBSIDIAN_VAULT_DIR": str(_TMP / "vault"),
        "MOODLE_DEFAULT_INSTANCE": "test",
        "MOODLE_TEST_URL": BASE,
        "MOODLE_TEST_TOKEN": TOKEN,
    }
)

from moodle_mcp import config  # noqa: E402

config.legacy_env_file = lambda: _TMP / "inexistente.env"  # type: ignore[assignment]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Vault vazio e exclusivo de cada teste."""
    from moodle_mcp import server

    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(server, "OBSIDIAN_VAULT_DIR", str(root))
    return root


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """Diretório de configuração vazio e exclusivo de cada teste."""
    path = tmp_path / "cfg"
    path.mkdir()
    monkeypatch.setenv("MOODLE_MCP_CONFIG_DIR", str(path))
    return path
