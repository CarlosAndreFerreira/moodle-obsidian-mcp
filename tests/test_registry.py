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
