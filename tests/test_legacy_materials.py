"""Materiais descarregados por versões sem estado (antes da 0.8.0) são reconhecidos, não duplicados."""

import responses
from conftest import BASE

from moodle_mcp import server, state

FILE = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/aula.pdf"
PAGE = f"{BASE}/webservice/pluginfile.php/2/mod_page/content/1/index.html"


def _course(monkeypatch, filesize=4, with_page=False):
    modules = [
        {
            "name": "Aula",
            "contents": [
                {
                    "type": "file",
                    "filename": "aula.pdf",
                    "fileurl": FILE,
                    "timemodified": 7,
                    "filesize": filesize,
                }
            ],
        }
    ]
    if with_page:
        modules.append(
            {
                "name": "Guia",
                "contents": [
                    {
                        "type": "file",
                        "filename": "index.html",
                        "fileurl": PAGE,
                        "timemodified": 9,
                        "filesize": 5,
                    }
                ],
            }
        )
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: [{"name": "Tema", "modules": modules}])
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})


def _folder(vault):
    folder = vault / "Unidades Curriculares" / "UC" / "Materiais" / "Tema"
    folder.mkdir(parents=True)
    return folder


@responses.activate
def test_legacy_file_with_the_same_size_is_recognised(vault, monkeypatch):
    folder = _folder(vault)
    (folder / "aula.pdf").write_bytes(b"%PDF")  # 4 bytes, como o Moodle indica
    _course(monkeypatch)
    responses.get(FILE, body=b"%PDF")

    result = server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert sorted(p.name for p in folder.iterdir()) == ["aula.pdf"]
    assert result["downloaded_count"] == 0 and result["unchanged_count"] == 1
    assert not responses.calls, "reconhecido sem o descarregar outra vez"
    entry = state.load(vault, "test")["files"][state.file_key(FILE)]
    assert entry["path"].endswith("Tema/aula.pdf") and entry["timemodified"] == 7

    # Na sincronização seguinte já é um material conhecido.
    again = server.download_course_materials(5, extract_pdfs=False, instance="test")
    assert again["unchanged_count"] == 1 and not responses.calls


@responses.activate
def test_students_file_with_another_size_is_kept_and_the_copy_goes_beside(vault, monkeypatch):
    folder = _folder(vault)
    (folder / "aula.pdf").write_bytes(b"as minhas notas, mais compridas")
    _course(monkeypatch)
    responses.get(FILE, body=b"%PDF")

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert (folder / "aula.pdf").read_bytes() == b"as minhas notas, mais compridas"
    assert (folder / "aula (2).pdf").read_bytes() == b"%PDF"


@responses.activate
def test_without_a_size_from_moodle_nothing_is_assumed(vault, monkeypatch):
    folder = _folder(vault)
    (folder / "aula.pdf").write_bytes(b"%PDF")
    _course(monkeypatch, filesize=0)
    responses.get(FILE, body=b"%PDF")

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert sorted(p.name for p in folder.iterdir()) == ["aula (2).pdf", "aula.pdf"]


@responses.activate
def test_legacy_page_note_is_refreshed_in_place_and_backed_up(vault, monkeypatch):
    folder = _folder(vault)
    (folder / "aula.pdf").write_bytes(b"%PDF")
    (folder / "Guia.md").write_text("# Guia\n\nversão antiga da página\n", encoding="utf-8")
    _course(monkeypatch, with_page=True)
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>versão nova</p>")

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert sorted(p.name for p in folder.iterdir()) == ["Guia.md", "aula.pdf"]
    text = (folder / "Guia.md").read_text(encoding="utf-8")
    assert "versão nova" in text and "moodle-mcp:start" in text
    backups = list((vault / ".moodle-mcp" / "backup").rglob("Guia*.md"))
    assert backups and "versão antiga" in backups[0].read_text(encoding="utf-8")


@responses.activate
def test_students_own_note_with_the_page_name_is_not_taken(vault, monkeypatch):
    folder = _folder(vault)
    (folder / "aula.pdf").write_bytes(b"%PDF")
    (folder / "Guia.md").write_text("# O meu resumo do guia\n\ntexto meu\n", encoding="utf-8")
    _course(monkeypatch, with_page=True)
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>página</p>")

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert (folder / "Guia.md").read_text(encoding="utf-8") == "# O meu resumo do guia\n\ntexto meu\n"
    assert "página" in (folder / "Guia (2).md").read_text(encoding="utf-8")


@responses.activate
def test_same_named_legacy_files_numbered_in_another_order_are_matched(vault, monkeypatch):
    folder = _folder(vault)
    first = f"{BASE}/webservice/pluginfile.php/3/mod_label/intro/image.png"
    second = f"{BASE}/webservice/pluginfile.php/4/mod_label/intro/image.png"
    # Uma versão antiga gravou-as pela ordem inversa.
    (folder / "image.png").write_bytes(b"BBBBB")
    (folder / "image (2).png").write_bytes(b"AAA")
    monkeypatch.setattr(
        server,
        "get_course_contents",
        lambda *a, **k: [
            {
                "name": "Tema",
                "modules": [
                    {
                        "name": "A",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "image.png",
                                "fileurl": first,
                                "timemodified": 1,
                                "filesize": 3,
                            }
                        ],
                    },
                    {
                        "name": "B",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "image.png",
                                "fileurl": second,
                                "timemodified": 1,
                                "filesize": 5,
                            }
                        ],
                    },
                ],
            }
        ],
    )
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})

    result = server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert sorted(p.name for p in folder.iterdir()) == ["image (2).png", "image.png"]
    assert result["downloaded_count"] == 0 and result["unchanged_count"] == 2
    files = state.load(vault, "test")["files"]
    assert files[state.file_key(first)]["path"].endswith("image (2).png")
    assert files[state.file_key(second)]["path"].endswith("Tema/image.png")
