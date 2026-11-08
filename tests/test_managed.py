"""Notas com bloco gerido (a sincronização não apaga o que o estudante escreve) e downloads incrementais."""

import responses
from conftest import BASE

from moodle_mcp import server, state
from moodle_mcp import vault as vaultlib


def _generated(version, extra=""):
    return f'---\ncourse_id: 1\ntitle: "UC"\nupdated: {version}\n---\n\n# UC\n\nConteúdo {version}{extra}\n'


# ------------------------------------------------------------------------------
# Bloco gerido
# ------------------------------------------------------------------------------


def test_user_text_and_properties_survive_resyncs(vault):
    note = vault / "UC.md"
    vaultlib.write_managed_note(note, _generated("v1"))
    text = note.read_text(encoding="utf-8")
    text = text.replace("updated: v1\n", "updated: v1\naliases:\n  - minha UC\n  - outra\nestado: a ler\n")
    text += "\n## As minhas notas\n\nIdeia para o e-fólio.\n"
    note.write_text(text, encoding="utf-8")

    for version in ("v2", "v3"):
        vaultlib.write_managed_note(note, _generated(version))

    out = note.read_text(encoding="utf-8")
    assert out.startswith("---\n")  # o frontmatter continua a ser a primeira coisa da nota
    assert "Conteúdo v3" in out and "Conteúdo v2" not in out and "Conteúdo v1" not in out
    assert "updated: v3" in out
    assert "aliases:\n  - minha UC\n  - outra" in out and "estado: a ler" in out
    assert "## As minhas notas\n\nIdeia para o e-fólio." in out
    assert out.count(vaultlib.MANAGED_START) == 1 and out.count(vaultlib.MANAGED_END) == 1


def test_text_written_before_the_block_is_kept(vault):
    note = vault / "UC.md"
    vaultlib.write_managed_note(note, _generated("v1"))
    text = note.read_text(encoding="utf-8").replace(
        vaultlib.MANAGED_START, "> Lembrete meu\n\n" + vaultlib.MANAGED_START
    )
    note.write_text(text, encoding="utf-8")
    vaultlib.write_managed_note(note, _generated("v2"))
    assert "> Lembrete meu" in note.read_text(encoding="utf-8")


def test_rewriting_unchanged_content_is_stable(vault):
    note = vault / "UC.md"
    vaultlib.write_managed_note(note, _generated("v1"))
    first = note.read_text(encoding="utf-8")
    vaultlib.write_managed_note(note, _generated("v1"))
    assert note.read_text(encoding="utf-8") == first


def test_legacy_note_is_backed_up_before_the_first_rewrite(vault):
    note = vault / "Unidades Curriculares" / "UC.md"
    note.parent.mkdir(parents=True)
    note.write_text("# UC\n\nEditei isto à mão numa versão antiga.\n", encoding="utf-8")

    backup = vaultlib.write_managed_note(note, _generated("v1"))

    assert backup is not None and backup.is_relative_to(
        vault / ".moodle-mcp" / "backup" / "Unidades Curriculares"
    )
    assert "Editei isto à mão" in backup.read_text(encoding="utf-8")
    assert vaultlib.MANAGED_START in note.read_text(encoding="utf-8")
    # Uma segunda escrita já não precisa de cópia.
    assert vaultlib.write_managed_note(note, _generated("v2")) is None


def test_broken_markers_are_treated_as_legacy(vault):
    note = vault / "UC.md"
    vaultlib.write_managed_note(note, _generated("v1"))
    note.write_text(note.read_text(encoding="utf-8").replace(vaultlib.MANAGED_END, ""), encoding="utf-8")
    assert vaultlib.write_managed_note(note, _generated("v2")) is not None
    out = note.read_text(encoding="utf-8")
    assert out.count(vaultlib.MANAGED_START) == 1 and out.count(vaultlib.MANAGED_END) == 1


def test_users_own_active_blocks_are_not_touched(vault):
    note = vault / "UC.md"
    vaultlib.write_managed_note(note, _generated("v1", "\n```dataviewjs\nmoodle\n```"))
    note.write_text(note.read_text(encoding="utf-8") + "\n```dataviewjs\nmeu\n```\n", encoding="utf-8")
    vaultlib.write_managed_note(note, _generated("v2", "\n```dataviewjs\nmoodle\n```"))
    out = note.read_text(encoding="utf-8")
    assert "```dataviewjs\nmeu" in out  # do estudante: fica
    assert "```dataviewjs\nmoodle" not in out  # vindo do Moodle: desativado


def test_sync_keeps_what_the_student_wrote(vault, monkeypatch):
    def call(wsfunction, params=None, method="POST", instance="test"):
        return {"userid": 1, "fullname": "Aluno"} if wsfunction == "core_webservice_get_site_info" else {}

    monkeypatch.setattr(server, "_moodle_call", call)
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 1, "fullname": "UC"}]}
    )
    for name, value in (
        ("get_assignments", {"courses": []}),
        ("get_course_grades", {}),
        ("get_course_forums", []),
    ):
        monkeypatch.setattr(server, name, lambda *a, _v=value, **k: _v)
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: [])

    server.sync_dashboard(instance="test")
    for path in (vault / "Unidades Curriculares" / "UC.md", vault / "Painel Moodle.md"):
        path.write_text(path.read_text(encoding="utf-8") + "\nNOTA PESSOAL\n", encoding="utf-8")
    server.sync_dashboard(instance="test")

    for path in (vault / "Unidades Curriculares" / "UC.md", vault / "Painel Moodle.md"):
        assert "NOTA PESSOAL" in path.read_text(encoding="utf-8"), path.name


# ------------------------------------------------------------------------------
# Downloads incrementais
# ------------------------------------------------------------------------------

FILE = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/aula.pdf"
PAGE = f"{BASE}/webservice/pluginfile.php/2/mod_page/content/1/index.html"


def _contents(timemodified=100):
    return [
        {
            "name": "Tema",
            "modules": [
                {
                    "name": "Aula",
                    "contents": [
                        {
                            "type": "file",
                            "filename": "aula.pdf",
                            "fileurl": FILE,
                            "timemodified": timemodified,
                            "filesize": 4,
                        }
                    ],
                },
                {
                    "name": "Guia",
                    "contents": [
                        {
                            "type": "file",
                            "filename": "index.html",
                            "fileurl": PAGE,
                            "timemodified": 50,
                            "filesize": 9,
                        }
                    ],
                },
            ],
        }
    ]


def _fake_course(monkeypatch, timemodified):
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: _contents(timemodified))
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})


@responses.activate
def test_second_download_skips_unchanged_files(vault, monkeypatch):
    _fake_course(monkeypatch, 100)
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>guia</p>")

    first = server.download_course_materials(5, extract_pdfs=False, instance="test")
    calls_after_first = len(responses.calls)
    second = server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert first["downloaded_count"] == 2 and first["unchanged_count"] == 0
    assert second["downloaded_count"] == 0 and second["unchanged_count"] == 2
    assert len(responses.calls) == calls_after_first  # nenhum pedido novo
    assert state.state_file(vault, "test").exists()


@responses.activate
def test_changed_or_missing_files_are_downloaded_again(vault, monkeypatch):
    _fake_course(monkeypatch, 100)
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>guia</p>")
    server.download_course_materials(5, extract_pdfs=False, instance="test")

    _fake_course(monkeypatch, 200)  # o professor substituiu o PDF
    again = server.download_course_materials(5, extract_pdfs=False, instance="test")
    assert again["downloaded_count"] == 1 and again["unchanged_count"] == 1

    guide = next((vault / "Unidades Curriculares" / "UC" / "Materiais" / "Tema").glob("Guia*.md"))
    guide.unlink()  # o estudante apagou a nota
    third = server.download_course_materials(5, extract_pdfs=False, instance="test")
    assert third["downloaded_count"] == 1


@responses.activate
def test_unreadable_state_means_download_everything(vault, monkeypatch):
    _fake_course(monkeypatch, 100)
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>guia</p>")
    server.download_course_materials(5, extract_pdfs=False, instance="test")
    state.state_file(vault, "test").write_text("{corrompido", encoding="utf-8")
    assert server.download_course_materials(5, extract_pdfs=False, instance="test")["downloaded_count"] == 2


def test_switching_language_replaces_the_block_once(vault, monkeypatch):
    note = vault / "UC.md"
    vaultlib.write_managed_note(note, _generated("v1"))
    note.write_text(note.read_text(encoding="utf-8") + "\nNOTA PESSOAL\n", encoding="utf-8")
    monkeypatch.setenv("MOODLE_MCP_LANGUAGE", "en")
    vaultlib.write_managed_note(note, _generated("v2"))
    out = note.read_text(encoding="utf-8")
    assert out.count("moodle-mcp:start") == 1 and out.count(vaultlib.MANAGED_END) == 1
    assert "replaced on each sync" in out and "Conteúdo v2" in out and "NOTA PESSOAL" in out
