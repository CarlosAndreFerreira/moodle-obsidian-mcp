"""Regressões das fugas de token (relatório de segurança, itens C1–C4).

Nenhum teste faz pedidos reais: o `responses` intercepta tudo, e um pedido não
registado falha em vez de sair para a rede.
"""

import base64
import json
from urllib.parse import parse_qs, quote, urlparse

import pytest
import responses
from conftest import BASE, TOKEN

from moodle_mcp import config, server

FILE = f"{BASE}/webservice/pluginfile.php/12/mod_resource/content/0/aula.pdf"
REST = f"{BASE}/webservice/rest/server.php"


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


# ------------------------------------------------------------------------------
# C1 / C4 — o token só é juntado a ficheiros da instância configurada
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        FILE,
        f"{BASE}/pluginfile.php/1/mod_page/content/3/index.html?forcedownload=1",
        f"{BASE}/tokenpluginfile.php/abc/1/a.pdf",
        f"{BASE}:443/webservice/pluginfile.php/1/a.pdf",
        "https://MOODLE.example.edu./webservice/pluginfile.php/1/a.pdf",
    ],
)
def test_accepts_files_of_the_instance(url):
    assert server._validate_file_url(url, BASE)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/webservice/pluginfile.php/1/a.pdf",
        # O `urlparse` lê o anfitrião como moodle.example.edu; o `requests` liga-se a evil.example.
        "https://evil.example\\@moodle.example.edu/webservice/pluginfile.php/1/a.pdf",
        "https://moodle.example.edu@evil.example/webservice/pluginfile.php/1/a.pdf",
        "https://user:pw@moodle.example.edu/webservice/pluginfile.php/1/a.pdf",
        "https://moodle.example.edu.evil.example/webservice/pluginfile.php/1/a.pdf",
        "http://moodle.example.edu/webservice/pluginfile.php/1/a.pdf",
        "https://moodle.example.edu:8443/webservice/pluginfile.php/1/a.pdf",
        f"{BASE}/webservice/rest/server.php?wsfunction=core_user_update_users",
        f"{BASE}/webservice/pluginfile.php/../rest/server.php",
        f"{BASE}/webservice/pluginfile.php/%2e%2e/rest/server.php",
        f"{BASE}/login/index.php",
        "file:///etc/passwd",
        "",
    ],
)
def test_refuses_anything_else(url):
    with pytest.raises(ValueError, match="Recusado"):
        server._validate_file_url(url, BASE)


def test_respects_base_path():
    base = "https://escola.example/moodle"
    assert server._validate_file_url(f"{base}/webservice/pluginfile.php/1/a.pdf", base)
    with pytest.raises(ValueError):
        server._validate_file_url("https://escola.example/webservice/pluginfile.php/1/a.pdf", base)


@responses.activate
def test_download_sends_token_to_instance(vault):
    responses.get(FILE, body=b"%PDF-fake")
    result = server.download_moodle_file(FILE, "a.pdf", extract_pdf_to_markdown=False, instance="test")

    assert _query(responses.calls[0].request.url)["token"] == [TOKEN]
    assert (vault / "a.pdf").read_bytes() == b"%PDF-fake"
    assert result["size_bytes"] == 9


@responses.activate
def test_download_to_foreign_host_makes_no_request(vault):
    with pytest.raises(ValueError, match="Recusado"):
        server.download_moodle_file(
            "https://evil.example/webservice/pluginfile.php/1/x.pdf", "x.pdf", False, instance="test"
        )
    assert len(responses.calls) == 0
    assert not (vault / "x.pdf").exists()


@responses.activate
def test_token_substring_in_filename_no_longer_skips_auth(vault):
    url = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/notoken=1.pdf"
    responses.get(url, body=b"x")
    server.download_moodle_file(url, "n.pdf", False, instance="test")
    assert _query(responses.calls[0].request.url)["token"] == [TOKEN]


# ------------------------------------------------------------------------------
# C2 — o token nunca aparece no texto das exceções
# ------------------------------------------------------------------------------


@responses.activate
@pytest.mark.parametrize("status", [404, 500])
def test_http_error_text_has_no_token(vault, status):
    responses.get(FILE, status=status)
    with pytest.raises(server.MoodleRequestError) as excinfo:
        server.download_moodle_file(FILE, "a.pdf", False, instance="test")
    assert TOKEN not in str(excinfo.value)
    assert f"HTTP {status}" in str(excinfo.value)


@responses.activate
def test_connection_error_text_has_no_token(vault):
    # Sem nada registado, o `responses` levanta ConnectionError com o URL completo.
    with pytest.raises(server.MoodleRequestError) as excinfo:
        server.download_moodle_file(FILE, "a.pdf", False, instance="test")
    assert TOKEN not in str(excinfo.value)


@responses.activate
def test_course_materials_errors_have_no_token(vault, monkeypatch):
    page = f"{BASE}/webservice/pluginfile.php/7/mod_page/content/1/index.html"
    contents = [
        {
            "name": "Tema 1",
            "modules": [
                {
                    "name": "Mod",
                    "contents": [
                        {"type": "file", "filename": "aula.pdf", "fileurl": FILE},
                        {"type": "file", "filename": "index.html", "fileurl": page},
                        {"type": "file", "filename": "x.pdf", "fileurl": "https://evil.example/a.pdf"},
                    ],
                }
            ],
        }
    ]
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: contents)
    monkeypatch.setattr(server, "get_enrolled_courses", lambda *a, **k: {"courses": []})
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})
    responses.get(FILE, status=404)
    responses.get(page, status=503)

    result = server.download_all_course_materials_to_obsidian(42, extract_pdfs=False, instance="test")

    assert len(result["errors"]) == 3
    assert TOKEN not in json.dumps(result)
    assert all("evil.example" not in call.request.url for call in responses.calls)


@responses.activate
def test_upload_sends_token_in_body_not_in_url(vault):
    (vault / "efolio.pdf").write_bytes(b"conteudo")
    upload = f"{BASE}/webservice/upload.php"
    responses.post(upload, json=[{"itemid": 99, "filename": "efolio.pdf"}])

    result = server.upload_file_to_draft("efolio.pdf", instance="test")

    request = responses.calls[0].request
    assert TOKEN not in request.url
    assert TOKEN.encode() in request.body
    assert result[0]["itemid"] == 99


@responses.activate
def test_upload_error_text_has_no_token(vault):
    (vault / "efolio.pdf").write_bytes(b"conteudo")
    responses.post(f"{BASE}/webservice/upload.php", status=500)
    with pytest.raises(server.MoodleRequestError) as excinfo:
        server.upload_file_to_draft("efolio.pdf", instance="test")
    assert TOKEN not in str(excinfo.value)


@responses.activate
def test_rest_get_error_text_has_no_token():
    responses.get(REST, status=502)
    with pytest.raises(server.MoodleRequestError) as excinfo:
        server._moodle_call("core_webservice_get_site_info", method="GET", instance="test")
    assert TOKEN not in str(excinfo.value)


# ------------------------------------------------------------------------------
# C3 — set_moodle_token não muda URLs nem grava tokens inválidos
# ------------------------------------------------------------------------------

UAB = "https://elearning.uab.example"
OK_INFO = {"userid": 5, "fullname": "Aluno", "sitename": "UAb"}


def _write_instance(cfg_dir, url=UAB, token=None):
    (cfg_dir / "config.toml").write_text(f'[instances.uab]\nurl = "{url}"\n', encoding="utf-8")
    if token:
        creds = {"instances": {"uab": {"token": token}}}
        (cfg_dir / "credentials.json").write_text(json.dumps(creds), encoding="utf-8")


@responses.activate
def test_cannot_repoint_existing_instance(cfg_dir, monkeypatch):
    # Token real no ambiente (como o README recomenda) e URL no config.toml.
    _write_instance(cfg_dir)
    monkeypatch.setenv("MOODLE_UAB_TOKEN", "real" * 8)

    result = server.set_moodle_token("lixo", instance="uab", moodle_url="https://evil.example")

    assert result["saved"] is False
    assert "Recusado" in result["message"]
    assert len(responses.calls) == 0
    assert config.resolve_instance("uab").url == UAB


@pytest.mark.parametrize(
    "url",
    ["http://moodle.novo.example", "ftp://x.example", "https://user:pw@x.example", "https://x.example/?a=1"],
)
def test_new_instance_url_must_be_https(cfg_dir, url):
    with pytest.raises(ValueError, match="recusado"):
        server.set_moodle_token("t", instance="nova", moodle_url=url)


@responses.activate
def test_invalid_token_is_not_saved(cfg_dir):
    _write_instance(cfg_dir, token="bom" * 8)
    responses.post(f"{UAB}/webservice/rest/server.php", json={"exception": "x", "errorcode": "invalidtoken"})

    result = server.set_moodle_token("mau", instance="uab")

    assert result["saved"] is False
    assert config.resolve_instance("uab").token == "bom" * 8


@responses.activate
def test_network_failure_saves_nothing(cfg_dir):
    _write_instance(cfg_dir, token="bom" * 8)
    with pytest.raises(server.MoodleRequestError) as excinfo:
        server.set_moodle_token("novo-token", instance="uab")
    assert "novo-token" not in str(excinfo.value)
    assert config.resolve_instance("uab").token == "bom" * 8


@responses.activate
def test_valid_token_is_validated_then_saved(cfg_dir):
    _write_instance(cfg_dir, token="velho" * 4)
    responses.post(f"{UAB}/webservice/rest/server.php", json=OK_INFO)

    result = server.set_moodle_token("novo", instance="uab")

    assert parse_qs(responses.calls[0].request.body)["wstoken"] == ["novo"]
    assert result["saved"] is True and "warning" not in result
    assert config.resolve_instance("uab").token == "novo"


@responses.activate
def test_new_instance_with_url(cfg_dir):
    responses.post("https://nova.example/webservice/rest/server.php", json=OK_INFO)
    result = server.set_moodle_token("tok", instance="nova", moodle_url="https://nova.example/")
    assert result["saved"] is True
    assert config.resolve_instance("nova").url == "https://nova.example"


@responses.activate
def test_warns_when_environment_token_wins(cfg_dir, monkeypatch):
    _write_instance(cfg_dir)
    monkeypatch.setenv("MOODLE_UAB_TOKEN", "doambiente")
    responses.post(f"{UAB}/webservice/rest/server.php", json=OK_INFO)

    result = server.set_moodle_token("novo", instance="uab")

    assert result["saved"] is True
    assert "MOODLE_UAB_TOKEN" in result["warning"]


# ------------------------------------------------------------------------------
# moodlemobile:// — percent-encoding e propagação de `saved`
# ------------------------------------------------------------------------------


def _payload(token="abcdef0123456789abcdef0123456+/x"):
    return base64.b64encode(f"assinatura:::{token}:::privado".encode()).decode()


@responses.activate
@pytest.mark.parametrize("encode", [False, True])
def test_mobile_payload_plain_and_percent_encoded(cfg_dir, encode):
    _write_instance(cfg_dir)
    responses.post(f"{UAB}/webservice/rest/server.php", json=OK_INFO)
    payload = quote(_payload(), safe="") if encode else _payload()

    result = server.decode_mobile_launch_token(f"moodlemobile://token={payload}", instance="uab")

    assert result["saved"] is True
    assert parse_qs(responses.calls[0].request.body)["wstoken"] == ["abcdef0123456789abcdef0123456+/x"]


@pytest.mark.parametrize("garbage", ["moodlemobile://token=a", "moodlemobile://token=!!!", ""])
def test_mobile_payload_garbage_is_an_error_not_a_crash(garbage):
    result = server.decode_mobile_launch_token(garbage, instance="test")
    assert result["saved"] is False


@responses.activate
def test_mobile_payload_reports_unsaved_when_validation_fails(cfg_dir):
    _write_instance(cfg_dir, token="bom" * 8)
    responses.post(f"{UAB}/webservice/rest/server.php", json={"exception": "x"})
    result = server.decode_mobile_launch_token(f"moodlemobile://token={_payload()}", instance="uab")
    # O token_catcher decide se sincroniza com base neste campo.
    assert result["saved"] is False
    assert config.resolve_instance("uab").token == "bom" * 8


# ------------------------------------------------------------------------------
# Toolsets — as ferramentas que gravam credenciais não vêm por omissão
# ------------------------------------------------------------------------------


def test_auth_tools_are_not_exposed_by_default():
    defaults = {tool for ts in server.resolve_toolsets("") for tool in server.TOOLSETS[ts]}
    for tool in server.TOOLSETS["auth"]:
        assert tool not in defaults
    assert "set_moodle_token" in server.TOOLSETS["auth"]


def test_every_registered_tool_belongs_to_exactly_one_toolset():
    grouped = [tool for tools in server.TOOLSETS.values() for tool in tools]
    registered = {tool.name for tool in server._iter_registered_tools()}
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == registered
