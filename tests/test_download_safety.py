"""Downloads que nunca apagam o que o estudante escreveu ou guardou no Vault.

As ferramentas de download substituem a versão anterior de um material quando o
professor o altera, e por isso declaram `destructiveHint=True`. O que estes
testes fixam é o limite dessa substituição: só o ficheiro que o mesmo URL lá
pôs, e nas notas geradas (texto de PDFs, páginas HTML) só o bloco gerido.
"""

import responses
from conftest import BASE

from moodle_mcp import server, state
from moodle_mcp import vault as vaultlib

FILE = f"{BASE}/webservice/pluginfile.php/1/mod_resource/content/0/aula.pdf"
PAGE = f"{BASE}/webservice/pluginfile.php/2/mod_page/content/1/index.html"
STUDENT = "\nAs minhas notas: rever a secção 2.\n"


def _course(monkeypatch, timemodified=100):
    contents = [
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
                            "timemodified": timemodified,
                            "filesize": 9,
                        }
                    ],
                },
            ],
        }
    ]
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: contents)
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 5, "fullname": "UC"}]}
    )
    monkeypatch.setattr(server, "get_assignments", lambda *a, **k: {"courses": []})


def _folder(vault):
    return vault / "Unidades Curriculares" / "UC" / "Materiais" / "Tema"


def _sync():
    return server.download_course_materials(5, extract_pdfs=False, instance="test")


# ------------------------------------------------------------------------------
# Ficheiros do estudante com o mesmo nome de um material
# ------------------------------------------------------------------------------


@responses.activate
def test_course_sync_never_overwrites_a_students_file_with_the_same_name(vault, monkeypatch):
    _course(monkeypatch)
    folder = _folder(vault)
    folder.mkdir(parents=True)
    (folder / "aula.pdf").write_bytes(b"o meu PDF anotado")
    (folder / "Guia.md").write_text("# O meu guia\n", encoding="utf-8")
    responses.get(FILE, body=b"%PDF v1")
    responses.get(PAGE, body="<p>guia v1</p>")

    _sync()

    assert (folder / "aula.pdf").read_bytes() == b"o meu PDF anotado"
    assert (folder / "Guia.md").read_text(encoding="utf-8") == "# O meu guia\n"
    assert (folder / "aula (2).pdf").read_bytes() == b"%PDF v1"
    assert "guia v1" in (folder / "Guia (2).md").read_text(encoding="utf-8")


@responses.activate
def test_the_side_copy_is_updated_in_place_on_later_syncs(vault, monkeypatch):
    _course(monkeypatch, 100)
    folder = _folder(vault)
    folder.mkdir(parents=True)
    (folder / "aula.pdf").write_bytes(b"o meu PDF anotado")
    responses.get(FILE, body=b"%PDF v1")
    responses.get(PAGE, body="<p>guia</p>")
    _sync()

    _course(monkeypatch, 200)  # o professor substituiu o PDF
    responses.replace(responses.GET, FILE, body=b"%PDF v2")
    again = _sync()

    assert again["errors"] == []
    assert (folder / "aula (2).pdf").read_bytes() == b"%PDF v2"
    assert not (folder / "aula (3).pdf").exists()  # sem uma cópia nova por sincronização
    assert (folder / "aula.pdf").read_bytes() == b"o meu PDF anotado"


@responses.activate
def test_the_same_file_in_two_modules_still_gets_two_names(vault, monkeypatch):
    # Reaproveitar o caminho da sincronização anterior não pode juntar os dois.
    _course(monkeypatch)
    contents = server.get_course_contents()
    contents[0]["modules"].append(
        {"name": "Outra", "contents": [dict(contents[0]["modules"][0]["contents"][0])]}
    )
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>guia</p>")
    _sync()
    _sync()
    names = sorted(p.name for p in _folder(vault).iterdir())
    assert names == ["Guia.md", "aula (2).pdf", "aula.pdf"]


# ------------------------------------------------------------------------------
# Notas geradas: só o bloco gerido muda
# ------------------------------------------------------------------------------


@responses.activate
def test_students_text_in_a_page_note_survives_a_changed_page(vault, monkeypatch):
    _course(monkeypatch, 100)
    responses.get(FILE, body=b"%PDF")
    responses.get(PAGE, body="<p>guia v1</p>")
    _sync()
    note = _folder(vault) / "Guia.md"
    assert vaultlib.MANAGED_END in note.read_text(encoding="utf-8")
    note.write_text(note.read_text(encoding="utf-8") + STUDENT, encoding="utf-8")

    _course(monkeypatch, 200)  # o professor alterou a página
    responses.replace(responses.GET, PAGE, body="<p>guia v2</p>")
    _sync()

    text = note.read_text(encoding="utf-8")
    assert "guia v2" in text and "guia v1" not in text
    assert STUDENT.strip() in text


class _FakePdf:
    def __init__(self, text):
        self.pages = [type("Page", (), {"get_text": lambda self, kind: text})()]

    def __len__(self):
        return len(self.pages)

    def __iter__(self):
        return iter(self.pages)

    def close(self):
        pass


@responses.activate
def test_students_text_in_the_extracted_pdf_note_survives_a_new_download(vault, monkeypatch):
    texts = iter(["Versão 1 do enunciado", "Versão 2 do enunciado"])
    monkeypatch.setattr(
        server, "fitz", type("Fitz", (), {"open": staticmethod(lambda p: _FakePdf(next(texts)))})
    )
    responses.get(FILE, body=b"%PDF v1")
    server.download_moodle_file(FILE, "Materiais/aula.pdf", True, instance="test")
    note = vault / "Materiais" / "aula.pdf.md"
    note.write_text(note.read_text(encoding="utf-8") + STUDENT, encoding="utf-8")

    responses.replace(responses.GET, FILE, body=b"%PDF v2")
    server.download_moodle_file(FILE, "Materiais/aula.pdf", True, instance="test")

    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\n")  # o frontmatter gerado continua a ser o primeiro
    assert "Versão 2" in text and "Versão 1" not in text
    assert STUDENT.strip() in text


@responses.activate
def test_a_students_note_named_like_the_extract_is_backed_up_first(vault, monkeypatch):
    monkeypatch.setattr(server, "fitz", type("Fitz", (), {"open": staticmethod(lambda p: _FakePdf("Texto"))}))
    (vault / "Materiais").mkdir()
    (vault / "Materiais" / "aula.pdf.md").write_text("Resumo que escrevi à mão.\n", encoding="utf-8")
    responses.get(FILE, body=b"%PDF")

    server.download_moodle_file(FILE, "Materiais/aula.pdf", True, instance="test")

    backups = list((vault / ".moodle-mcp" / "backup").rglob("aula.pdf*.md"))
    assert [b.read_text(encoding="utf-8") for b in backups] == ["Resumo que escrevi à mão.\n"]


# ------------------------------------------------------------------------------
# Estado: todos os sítios onde um URL foi gravado
# ------------------------------------------------------------------------------


def test_state_remembers_every_existing_copy_of_a_url(vault):
    files = {}
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (vault / name).write_bytes(b"x")
        state.remember(files, FILE, {"timemodified": 1}, vault / name, vault)
    (vault / "a.pdf").unlink()  # uma cópia que o estudante apagou deixa de contar
    state.remember(files, FILE, {"timemodified": 2}, vault / "c.pdf", vault)
    entry = files[state.file_key(FILE)]
    assert entry["path"] == "c.pdf"
    assert entry["paths"] == ["b.pdf", "c.pdf"]


def test_state_written_before_paths_existed_still_counts_as_ours():
    assert state.known_paths({"path": "x/aula.pdf", "timemodified": 1}) == ["x/aula.pdf"]
