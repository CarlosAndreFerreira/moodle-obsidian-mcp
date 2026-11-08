"""O `server.json` do MCP Registry tem de bater certo com o pacote publicado."""

import json
import re
import tomllib
from pathlib import Path

import moodle_mcp

ROOT = Path(__file__).resolve().parent.parent


def _server():
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_versions_match_the_package():
    server = _server()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert server["version"] == project["version"] == moodle_mcp.__version__
    assert [p["version"] for p in server["packages"]] == [project["version"]]
    assert server["packages"][0]["identifier"] == project["name"]


def test_readme_carries_the_ownership_marker():
    # O Registry procura `mcp-name: <nome>` na descrição do PyPI (o README), seguido de um limite.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"mcp-name: {re.escape(_server()['name'])}(\s|-->)", readme)


def test_uvx_entry_point_has_the_package_name():
    # Os clientes do Registry correm `uvx <identifier>`.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"][project["name"]] == "moodle_mcp.server:main"


def test_description_fits_the_registry_limit():
    assert len(_server()["description"]) <= 100


def _plugin():
    manifest = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    servers = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    return manifest, servers


def test_claude_plugin_pins_the_released_version():
    # A validação do diretório da Anthropic recusa um `uvx` sem versão exata, e o
    # plugin tem de correr a versão que acabou de ser publicada.
    manifest, servers = _plugin()
    version = moodle_mcp.__version__
    assert manifest["version"] == version
    assert servers["moodle"]["command"] == "uvx"
    assert servers["moodle"]["args"] == [f"moodle-obsidian-mcp[pdf]=={version}"]


def test_claude_plugin_only_references_declared_options():
    manifest, servers = _plugin()
    used = set(re.findall(r"\$\{user_config\.(\w+)\}", json.dumps(servers)))
    assert used <= set(manifest["userConfig"])
    assert manifest["userConfig"]["moodle_token"].get("sensitive") is True


def test_claude_plugin_readme_is_long_enough():
    readme = (ROOT / "plugin" / "README.md").read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", readme, flags=re.DOTALL)
    assert len(prose.split()) >= 40 and (ROOT / "plugin" / "LICENSE").exists()
