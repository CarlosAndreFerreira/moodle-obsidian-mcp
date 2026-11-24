"""Capítulos dos livros do Moodle (`mod_book`): nome do capítulo, ordem do livro e pasta do livro."""

import json

import responses
from conftest import BASE

from moodle_mcp import server, state

STRUCTURE = [
    {"title": "Contrato de Aprendizagem", "href": "11/index.html", "level": 0, "subitems": []},
    {"title": "Objetivos", "href": "12/index.html", "level": 0, "subitems": []},
    {
        "title": "Conteúdos: visão geral",
        "href": "13/index.html",
        "level": 0,
        "subitems": [{"title": "Tema 1", "href": "14/index.html", "level": 1}],
    },
]


def _chapter(cid, body):
    url = f"{BASE}/webservice/pluginfile.php/7/mod_book/chapter/{cid}/index.html"
    responses.get(url, body=body)
    return {
        "type": "file",
        "filename": "index.html",
        "filepath": f"/{cid}/",
        "fileurl": url,
        "timemodified": 1,
    }


def _course(monkeypatch, contents, name="Contrato DW"):
    monkeypatch.setattr(
        server,
        "get_course_contents",
        lambda *a, **k: [
            {"name": "Geral", "modules": [{"name": name, "modname": "book", "contents": contents}]}
        ],
    )
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})


def _geral(vault):
    return vault / "Unidades Curriculares" / "UC" / "Materiais" / "Geral"


@responses.activate
def test_chapters_are_named_and_numbered_inside_a_book_folder(vault, monkeypatch):
    image = f"{BASE}/webservice/pluginfile.php/7/mod_book/chapter/11/image.png"
    responses.get(image, body=b"PNG")
    contents = [
        {"type": "content", "filename": "structure", "filepath": "/", "content": json.dumps(STRUCTURE)},
        _chapter(11, "<p>Bem-vindos</p>"),
        {"type": "file", "filename": "image.png", "filepath": "/11/", "fileurl": image, "filesize": 3},
        _chapter(12, "<p>Objetivos da UC</p>"),
        _chapter(13, "<p>Visão</p>"),
        _chapter(14, "<p>Tema</p>"),
    ]
    _course(monkeypatch, contents)

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    book = _geral(vault) / "Contrato DW"
    assert sorted(p.name for p in book.iterdir()) == [
        "01 Contrato de Aprendizagem.md",
        "02 Objetivos.md",
        "03 Conteúdos- visão geral.md",  # `:` não é válido em nomes de ficheiro
        "03.1 Tema 1.md",
    ]
    text = (book / "02 Objetivos.md").read_text(encoding="utf-8")
    assert "# Objetivos" in text and "*Contrato DW*" in text and "Objetivos da UC" in text
    # As imagens dos capítulos ficam onde sempre ficaram, para não duplicar materiais já descarregados.
    assert (_geral(vault) / "image.png").read_bytes() == b"PNG"


@responses.activate
def test_without_a_readable_structure_chapters_keep_the_old_names(vault, monkeypatch):
    contents = [
        {"type": "content", "filename": "structure", "filepath": "/", "content": "{partido"},
        _chapter(11, "<p>a</p>"),
        _chapter(12, "<p>b</p>"),
    ]
    _course(monkeypatch, contents)

    server.download_course_materials(5, extract_pdfs=False, instance="test")

    assert sorted(p.name for p in _geral(vault).iterdir()) == ["Contrato DW (2).md", "Contrato DW.md"]


@responses.activate
def test_a_chapter_note_with_the_old_name_is_moved_with_the_students_text(vault, monkeypatch):
    contents = [
        {"type": "content", "filename": "structure", "filepath": "/", "content": "[]"},
        _chapter(11, "<p>versão 1</p>"),
    ]
    _course(monkeypatch, contents)
    server.download_course_materials(5, extract_pdfs=False, instance="test")
    old = _geral(vault) / "Contrato DW.md"
    old.write_text(old.read_text(encoding="utf-8") + "\nNOTA MINHA\n", encoding="utf-8")

    # Uma versão nova do servidor já sabe ler a estrutura do livro.
    contents[0]["content"] = json.dumps(STRUCTURE[:1])
    server.download_course_materials(5, extract_pdfs=False, instance="test")

    new = _geral(vault) / "Contrato DW" / "01 Contrato de Aprendizagem.md"
    assert not old.exists()
    assert "NOTA MINHA" in new.read_text(encoding="utf-8")
    entry = next(iter(state.load(vault, "test")["files"].values()))
    assert entry["path"].endswith("Contrato DW/01 Contrato de Aprendizagem.md")
    assert entry["paths"] == [entry["path"]]


def test_book_structure_parsing_is_defensive():
    assert server._book_chapters({"contents": []}) == {}
    assert server._book_chapters({"contents": [{"filename": "structure", "content": "null"}]}) == {}
    weird = [{"title": "", "href": "1/index.html"}, "x", {"title": "Ok", "href": "2/index.html"}]
    chapters = server._book_chapters({"contents": [{"filename": "structure", "content": json.dumps(weird)}]})
    assert chapters == {"2": ("03", "Ok")}
