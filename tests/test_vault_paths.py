"""Destinos de escrita no Vault: pastas escondidas, extensões e ficheiros do estudante.

Um anexo de fórum é um `pluginfile.php` válido da instância, que qualquer
participante pode carregar. Com instruções escondidas num post, o modelo podia
descarregá-lo para a configuração do Obsidian ou do Claude Code dentro do
Vault, que carrega código. Estes testes fixam as três regras que o impedem.
"""

import asyncio
import json

import pytest
import responses
from conftest import BASE
from hypothesis import given
from hypothesis import strategies as st

from moodle_mcp import server, state
from moodle_mcp import vault as vaultlib

FILE = f"{BASE}/webservice/pluginfile.php/9/mod_forum/attachment/3/anexo.bin"

HIDDEN_DESTINATIONS = [
    ".obsidian/plugins/x/main.js",
    ".obsidian/plugins/x/manifest.json",
    ".obsidian/community-plugins.json",
    ".claude/settings.json",
    ".git/hooks/post-checkout",
    ".vscode/settings.json",
    ".moodle-mcp/state-test.json",
    "Notas/.config/x.pdf",
]


@responses.activate
@pytest.mark.parametrize("dest", HIDDEN_DESTINATIONS)
def test_download_refuses_hidden_destinations(vault, dest):
    responses.get(FILE, body=b"CONTEUDO DO ATACANTE")
    with pytest.raises(ValueError, match="escondido"):
        server.download_moodle_file(FILE, dest, False, instance="test")
    assert not list(vault.rglob("*")), "nada pode ser escrito"
    assert not responses.calls, "nem sequer pedido ao Moodle"


@responses.activate
def test_download_never_overwrites_the_students_file(vault):
    (vault / "Notas").mkdir()
    (vault / "Notas" / "aula.pdf").write_bytes(b"meu")
    responses.get(FILE, body=b"%PDF novo")
    result = server.download_moodle_file(FILE, "Notas/aula.pdf", False, instance="test")
    assert (vault / "Notas" / "aula.pdf").read_bytes() == b"meu"
    assert result["saved_as"].endswith("aula (2).pdf")
    assert (vault / "Notas" / "aula (2).pdf").read_bytes() == b"%PDF novo"


@responses.activate
def test_redownloading_its_own_file_replaces_it(vault):
    responses.get(FILE, body=b"%PDF v1")
    server.download_moodle_file(FILE, "Materiais/aula.pdf", False, instance="test")
    responses.replace(responses.GET, FILE, body=b"%PDF v2")
    result = server.download_moodle_file(FILE, "Materiais/aula.pdf", False, instance="test")
    assert "saved_as" not in result
    assert (vault / "Materiais" / "aula.pdf").read_bytes() == b"%PDF v2"


@responses.activate
@pytest.mark.parametrize(
    ("dest", "saved"),
    [
        ("Scripts/main.js", "Scripts/main.js.txt"),  # p.ex. scripts do Templater
        ("Notas/nota.md", "Notas/nota.md.txt"),  # dataviewjs/Templater em bruto
        ("Notas/painel.canvas", "Notas/painel.canvas.txt"),
        ("x/run.sh", "x/run.sh.txt"),
        ("Materiais/aula.pdf", "Materiais/aula.pdf"),
        ("Materiais/dados.csv", "Materiais/dados.csv"),
    ],
)
def test_non_study_extensions_are_neutralised(vault, dest, saved):
    responses.get(FILE, body=b"conteudo")
    server.download_moodle_file(FILE, dest, False, instance="test")
    assert (vault / saved).read_bytes() == b"conteudo"


def test_hidden_paths_are_refused_for_every_model_chosen_folder(vault):
    for vault_dir in (".obsidian", ".claude", "Sub/.git"):
        with pytest.raises(ValueError, match="escondido"):
            server._sync_root(vault_dir)
    assert server._sync_root("Moodle") == vault / "Moodle"


def test_upload_refuses_hidden_files(vault):
    (vault / ".claude").mkdir()
    (vault / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="escondido"):
        asyncio.run(server.upload_file_to_draft(".claude/settings.local.json", instance="test"))


def test_dashboard_resource_refuses_a_hidden_path_in_the_state(vault):
    state.save(vault, "test", {"dashboard": ".claude/settings.json"})
    with pytest.raises(Exception, match="escondido"):
        asyncio.run(server.mcp.read_resource("moodle://dashboard/test"))


@responses.activate
def test_course_materials_with_hostile_names_stay_visible_and_inert(vault, monkeypatch):
    js = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/main.js"
    monkeypatch.setattr(
        server,
        "get_course_contents",
        lambda *a, **k: [
            {
                "name": ".obsidian",
                "modules": [
                    {
                        "name": "x",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "main.js",
                                "fileurl": js,
                                "timemodified": 1,
                                "filesize": 2,
                            }
                        ],
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": ".claude"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})
    responses.get(js, body=b"alert(1)")

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    written = [p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file()]
    materials = [p for p in written if not p.startswith(".moodle-mcp/")]
    assert materials == ["Unidades Curriculares/claude/Materiais/obsidian/main.js.txt"]
    files = json.loads(state.state_file(vault, "test").read_text(encoding="utf-8"))["files"]
    assert [entry["path"] for entry in files.values()] == materials


@given(st.text())
def test_safe_filename_never_hides_a_file(name):
    assert not vaultlib.safe_filename(name).startswith(".")


@given(st.text().map(vaultlib.safe_filename))
def test_neutral_name_is_idempotent_and_never_executable(name):
    once = vaultlib.neutral_download_name(name)
    assert vaultlib.neutral_download_name(once) == once
    assert once.rsplit(".", 1)[-1].lower() in vaultlib.DOWNLOAD_EXTENSIONS or once.endswith(".txt")


@responses.activate
@pytest.mark.parametrize("instance", ["t/../../../fora/evil", "../x", "a b", "UAB/..", ""])
def test_instance_name_cannot_move_the_state_file(vault, instance, monkeypatch):
    monkeypatch.setattr(server, "_download_to", lambda *a, **k: pytest.fail("não devia descarregar"))
    with pytest.raises(ValueError, match="instância"):
        server.download_moodle_file(FILE, "x.pdf", False, instance=instance or " ")
    assert not (vault.parent / "fora").exists()
    assert not list(vault.rglob("*"))


def test_state_file_normalises_valid_names(vault):
    assert state.state_file(vault, "UAB").name == "state-uab.json"
    with pytest.raises(ValueError):
        state.state_file(vault, "uab/../../x")


@responses.activate
def test_a_forum_attachment_cannot_replace_a_synced_course_file(vault):
    teacher = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/aula.pdf"
    responses.get(teacher, body=b"%PDF do professor")
    server.download_moodle_file(teacher, "Materiais/aula.pdf", False, instance="test")

    responses.get(FILE, body=b"%PDF do atacante")
    result = server.download_moodle_file(FILE, "Materiais/aula.pdf", False, instance="test")

    assert (vault / "Materiais" / "aula.pdf").read_bytes() == b"%PDF do professor"
    assert result["saved_as"].endswith("aula (2).pdf")


def test_generated_notes_neutralise_active_blocks(vault):
    # O texto extraído de um PDF e as páginas HTML convertidas são as únicas vias de
    # conteúdo do Moodle para notas; passam por write_note, que desativa estes blocos.
    note = vault / "x.pdf.md"
    vaultlib.write_note(note, "# T\n\n```dataviewjs\napp.vault.delete()\n```\n\n<% tp.system.exec() %>\n")
    text = note.read_text(encoding="utf-8")
    assert "```dataviewjs" not in text
    assert "<% tp." not in text
