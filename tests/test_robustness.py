"""Robustez: segredos, erros do Moodle, sincronização e downloads."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import responses
from conftest import BASE, TOKEN

from moodle_mcp import config, server
from moodle_mcp import vault as vaultlib

REST = f"{BASE}/webservice/rest/server.php"
FILE = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/aula.pdf"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="permissões POSIX")


# ------------------------------------------------------------------------------
# Segredos
# ------------------------------------------------------------------------------


def test_credentials_take_precedence_over_legacy_env(cfg_dir, tmp_path, monkeypatch):
    legacy = tmp_path / ".env"
    legacy.write_text("MOODLE_UAB_URL=https://uab.example\nMOODLE_UAB_TOKEN=antigo\n", encoding="utf-8")
    monkeypatch.setattr(config, "legacy_env_file", lambda: legacy)
    config.save_instance_token("uab", "novo")
    assert config.resolve_instance("uab").token == "novo"
    assert config.resolve_instance("uab").url == "https://uab.example"


def test_environment_still_wins_over_credentials(cfg_dir, monkeypatch):
    config.save_instance_token("test", "do-ficheiro")
    assert config.resolve_instance("test").token == TOKEN


@posix_only
def test_secrets_are_private_from_creation(cfg_dir):
    path = config.save_instance_token("uab", "segredo")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    fresh = cfg_dir / "novo"
    os.environ["MOODLE_MCP_CONFIG_DIR"] = str(fresh)
    try:
        assert stat.S_IMODE(config.config_dir().stat().st_mode) == 0o700
    finally:
        os.environ["MOODLE_MCP_CONFIG_DIR"] = str(cfg_dir)


def test_crash_mid_write_keeps_the_old_file(cfg_dir, monkeypatch):
    config.save_instance_token("uab", "bom")

    def boom(*args, **kwargs):
        raise OSError("disco cheio")

    original = config.os.replace
    monkeypatch.setattr(config.os, "replace", boom)
    with pytest.raises(OSError):
        config.save_instance_token("uab", "novo")
    monkeypatch.setattr(config.os, "replace", original)
    assert json.loads(config.credentials_file().read_text())["instances"]["uab"]["token"] == "bom"
    assert [p.name for p in cfg_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_corrupt_credentials_are_backed_up_not_silently_dropped(cfg_dir):
    config.credentials_file().write_text('{"instances": {"utad": {"token": "x"', encoding="utf-8")
    config.save_instance_token("uab", "novo")
    backups = list(cfg_dir.glob("credentials.json.corrompido-*"))
    assert len(backups) == 1 and '"utad"' in backups[0].read_text()
    assert config.resolve_instance("uab").token == "novo"


@posix_only
def test_security_warnings(cfg_dir, tmp_path, monkeypatch):
    creds = config.save_instance_token("uab", "x")
    creds.chmod(0o644)
    legacy = tmp_path / ".env"
    legacy.write_text("MOODLE_TOKEN=velho\n", encoding="utf-8")
    legacy.chmod(0o600)
    monkeypatch.setattr(config, "legacy_env_file", lambda: legacy)
    warnings = config.security_warnings()
    assert any("chmod 600" in w and "credentials.json" in w for w in warnings)
    assert any("MOODLE_TOKEN" in w for w in warnings)


def test_auth_log_is_private(cfg_dir, monkeypatch):
    from moodle_mcp import token_catcher

    token_catcher.handle_url("moodlemobile://token=lixo")
    log = token_catcher.log_file()
    assert "ERRO" in log.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert stat.S_IMODE(log.stat().st_mode) == 0o600


# ------------------------------------------------------------------------------
# Erros do Moodle chegam como erros acionáveis
# ------------------------------------------------------------------------------


@responses.activate
def test_invalid_token_is_an_actionable_error():
    responses.post(REST, json={"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "x"})
    with pytest.raises(server.MoodleAPIError) as excinfo:
        server.get_site_info(instance="test")
    assert excinfo.value.errorcode == "invalidtoken"
    assert "Faz login de novo" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


@responses.activate
def test_missing_function_explains_the_cause():
    responses.post(REST, json={"exception": "webservice_access_exception", "errorcode": "accessexception"})
    with pytest.raises(server.MoodleAPIError, match="não está disponível para a tua conta"):
        server.get_course_quizzes(1, instance="test")


def test_missing_token(monkeypatch):
    monkeypatch.delenv("MOODLE_TEST_TOKEN")
    with pytest.raises(server.MoodleAPIError) as excinfo:
        server.get_site_info(instance="test")
    assert excinfo.value.errorcode == "notoken"


@responses.activate
def test_enrolled_courses_falls_back_on_old_moodle_but_not_on_bad_token():
    def reply(request):
        fn = request.body.split("wsfunction=")[1].split("&")[0]
        if fn == "core_course_get_enrolled_courses_by_timeline_classification":
            return 200, {}, json.dumps({"exception": "x", "errorcode": "invalidparameter"})
        if fn == "core_webservice_get_site_info":
            return 200, {}, json.dumps({"userid": 3})
        return 200, {}, json.dumps([{"id": 9, "fullname": "UC"}])

    responses.add_callback(responses.POST, REST, callback=reply)
    assert server.get_enrolled_courses(instance="test") == [{"id": 9, "fullname": "UC"}]

    responses.replace(responses.POST, REST, json={"exception": "x", "errorcode": "invalidtoken"})
    with pytest.raises(server.MoodleAPIError):
        server.get_enrolled_courses(instance="test")


# ------------------------------------------------------------------------------
# Nomes de ficheiro
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("..", "_"),
        (".", "_"),
        ("", "_"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("COM1.pdf", "_COM1.pdf"),
        ("Relatório final. ", "Relatório final"),
        ("a/b\\c:d*e?f", "a-b-c-d-e-f"),
        ("v1.2 final", "v1.2 final"),
        ("aula\x00\x1f.pdf", "aula-.pdf"),
    ],
)
def test_safe_filename(name, expected):
    assert vaultlib.safe_filename(name) == expected


def test_safe_filename_truncates_the_stem_and_keeps_the_extension():
    name = vaultlib.safe_filename("x" * 300 + ".pdf")
    assert len(name) == 120 and name.endswith(".pdf")


def test_unique_path():
    used: set[Path] = set()
    names = [vaultlib.unique_path(Path("d/index.md"), used).name for _ in range(3)]
    assert names == ["index.md", "index (2).md", "index (3).md"]


# ------------------------------------------------------------------------------
# Downloads
# ------------------------------------------------------------------------------


@responses.activate
def test_failed_download_keeps_previous_file(vault):
    (vault / "aula.pdf").write_bytes(b"versao boa")
    responses.get(FILE, status=500)
    with pytest.raises(server.MoodleRequestError):
        server.download_moodle_file(FILE, "aula.pdf", False, instance="test")
    assert (vault / "aula.pdf").read_bytes() == b"versao boa"
    assert sorted(p.name for p in vault.iterdir()) == ["aula.pdf"]


@responses.activate
def test_declared_size_over_limit_is_refused(vault, monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_MAX_DOWNLOAD_MB", "0.001")  # ~1 KB
    responses.get(FILE, body=b"x" * 5000)
    with pytest.raises(ValueError, match="limite"):
        server.download_moodle_file(FILE, "grande.pdf", False, instance="test")
    assert list(vault.iterdir()) == []


@responses.activate
def test_streamed_size_over_limit_is_refused(vault, monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_MAX_DOWNLOAD_MB", "0.001")
    responses.get(FILE, body=b"x" * 5000, auto_calculate_content_length=False)
    with pytest.raises(ValueError, match="limite"):
        server.download_moodle_file(FILE, "grande.pdf", False, instance="test")
    assert list(vault.iterdir()) == []


@responses.activate
def test_course_pages_no_longer_overwrite_each_other(vault, monkeypatch):
    page = f"{BASE}/webservice/pluginfile.php/{{}}/mod_page/content/1/index.html"
    contents = [
        {
            "name": "Tema 1",
            "modules": [
                {
                    "name": "Guia",
                    "contents": [{"type": "file", "filename": "index.html", "fileurl": page.format(1)}],
                },
                {
                    "name": "Plano",
                    "contents": [{"type": "file", "filename": "index.html", "fileurl": page.format(2)}],
                },
                {
                    "name": "Plano",
                    "contents": [{"type": "file", "filename": "index.html", "fileurl": page.format(3)}],
                },
                {"name": "A", "contents": [{"type": "file", "filename": "aula.pdf", "fileurl": FILE}]},
                {"name": "B", "contents": [{"type": "file", "filename": "aula.pdf", "fileurl": FILE}]},
            ],
        }
    ]
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: contents)
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})
    for n in (1, 2, 3):
        responses.get(page.format(n), body=f"<p>página {n}</p>")
    responses.get(FILE, body=b"%PDF")

    result = server.download_course_materials(5, extract_pdfs=False, instance="test")

    folder = vault / "Unidades Curriculares" / "UC" / "Materiais" / "Tema 1"
    assert sorted(p.name for p in folder.iterdir()) == [
        "Guia.md",
        "Plano (2).md",
        "Plano.md",
        "aula (2).pdf",
        "aula.pdf",
    ]
    assert result["errors"] == []


# ------------------------------------------------------------------------------
# Sincronização: uma UC com problemas não pára as outras
# ------------------------------------------------------------------------------


@pytest.fixture
def fake_moodle(monkeypatch, vault):
    """Moodle simulado ao nível das funções, com falhas injetáveis por UC."""
    failures: dict[str, Exception] = {}

    def call(wsfunction, params=None, method="POST", instance="test"):
        if wsfunction == "core_webservice_get_site_info":
            return {"userid": 1, "fullname": "Aluno"}
        if wsfunction in failures:
            raise failures[wsfunction]
        return {}

    def contents(cid, instance="test"):
        if cid == 1:
            raise RuntimeError("dados inesperados")
        return []

    monkeypatch.setattr(server, "_moodle_call", call)
    monkeypatch.setattr(
        server,
        "get_enrolled_courses",
        lambda *a, **k: {
            "courses": [
                {"id": 1, "fullname": "Avariada"},
                {"id": 2, "fullname": "Ética"},
                {"id": 3, "fullname": "Ética"},
            ]
        },
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})
    monkeypatch.setattr(server, "get_course_grades", lambda *a, **k: {})
    monkeypatch.setattr(server, "get_course_forums", lambda *a, **k: [])
    monkeypatch.setattr(server, "get_course_contents", contents)
    return failures


def test_sync_isolates_failing_courses_and_reports_them(fake_moodle, vault):
    fake_moodle["mod_quiz_get_quizzes_by_courses"] = server.MoodleAPIError("accessexception", "sem acesso")

    result = server.sync_dashboard(instance="test")

    assert result["status"] == "parcial"
    assert result["courses_synced"] == 2 and result["courses_total"] == 3
    items = [e["item"] for e in result["errors"]]
    assert "Avariada" in items
    assert any(i.endswith(": testes") for i in items)
    notes = sorted(p.name for p in (vault / "Unidades Curriculares").iterdir())
    assert notes == ["Ética (2).md", "Ética.md"]  # nomes iguais já não se sobrescrevem
    dashboard = (vault / "Painel Moodle.md").read_text(encoding="utf-8")
    assert "Ética (2)\\|Abrir UC" in dashboard and "Avariada" not in dashboard


def test_sync_stops_on_fatal_errors(fake_moodle):
    fake_moodle["mod_choice_get_choices_by_courses"] = server.MoodleAPIError("invalidtoken", "token")
    with pytest.raises(server.MoodleAPIError):
        server.sync_dashboard(instance="test")
