"""«O que mudou», calendário .ics e tarefas do Obsidian Tasks."""

import time

import pytest

from moodle_mcp import changes, ics, server, state

BASE_SNAPSHOT = {
    "1": {
        "name": "Ética",
        "grades": {"e-fólio A": "—", "Total": "—"},
        "assignments": {"10": {"name": "e-fólio A", "due": 1_800_000_000, "status": "new"}},
        "modules": {"100": {"name": "Guia", "type": "page"}},
        "discussions": {"500": {"name": "Dúvidas", "forum": "Fórum", "modified": 10, "author": "Prof. Ana"}},
        "quizzes": {"900": {"name": "Teste 1", "close": 0}},
    }
}


def _changed():
    import copy

    now = copy.deepcopy(BASE_SNAPSHOT)
    course = now["1"]
    course["grades"]["e-fólio A"] = "3,5"
    course["assignments"]["10"]["due"] = 1_800_086_400
    course["assignments"]["10"]["status"] = "submitted"
    course["assignments"]["11"] = {"name": "e-fólio B", "due": 1_810_000_000, "status": "new"}
    course["modules"]["101"] = {"name": "Slides semana 3", "type": "resource"}
    course["modules"]["102"] = {"name": "texto solto", "type": "label"}
    course["discussions"]["500"]["modified"] = 20
    course["discussions"]["501"] = {"name": "Grupo 2", "forum": "Fórum", "modified": 5, "author": "Rita"}
    course["quizzes"]["900"]["close"] = 1_800_000_000
    now["2"] = {"name": "Lógica"}
    return now


def test_diff_finds_every_kind_of_change():
    found = changes.diff(BASE_SNAPSHOT, _changed())
    kinds = [c["kind"] for c in found]
    texts = " | ".join(c["text"] for c in found)
    assert kinds.count("nota") == 1 and "e-fólio A: 3,5" in texts
    assert "Trabalho novo: e-fólio B" in texts
    assert any(k == "prazo" and "e-fólio A" in c["text"] for k, c in zip(kinds, found, strict=True))
    assert "new → submitted" in texts
    assert "Slides semana 3" in texts and "texto solto" not in texts  # etiquetas não contam
    assert "Respostas novas em Fórum: «Dúvidas»" in texts and "Tópico novo de Rita" in texts
    assert "Fecho de «Teste 1» mudou" in texts
    assert {"course": "Lógica", "kind": "uc", "text": "UC nova na tua lista de disciplinas"} in found


def test_no_change_means_no_entries():
    assert changes.diff(BASE_SNAPSHOT, BASE_SNAPSHOT) == []


def test_render_groups_by_course_and_handles_first_sync():
    text = changes.render(changes.diff(BASE_SNAPSHOT, _changed()), "2026-11-01 10:00", "2026-11-08 10:00")
    assert text.startswith("---\n") and "## Ética" in text and "## Lógica" in text
    assert "Desde 2026-11-01 10:00" in text
    assert "Primeira sincronização" in changes.render(None, "—", "agora")
    assert "Nada de novo" in changes.render([], "a", "b")


# ------------------------------------------------------------------------------
# Calendário
# ------------------------------------------------------------------------------

ITEMS = [
    (1_800_000_000, "x", "Ética, Moral; e Direito", "e-fólio A", "trabalho", 10, "new"),
    (1_800_100_000, "x", "Lógica", "Teste " + "longo " * 30 + "çãõ", "teste", 900, "—"),
    (0, "x", "Sem prazo", "nada", "trabalho", 11, "new"),
]


def test_ics_structure_and_escaping():
    text = ics.build(ITEMS, "uab", "https://m.example")
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")  # só CRLF
    assert text.count("BEGIN:VEVENT") == 2  # o item sem prazo fica de fora
    assert "UID:uab-trabalho-10@moodle-obsidian-mcp" in text
    assert "DTSTART:20270115T080000Z" in text  # 1 800 000 000 em UTC
    assert "Ética\\, Moral\\; e Direito" in text
    assert "TRIGGER:-P1D" in text


def test_ics_lines_are_folded_at_75_octets_without_breaking_utf8():
    text = ics.build(ITEMS, "uab", "https://m.example")
    physical = text.split("\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in physical)
    unfolded = text.replace("\r\n ", "")
    assert "Teste " + "longo " * 30 + "çãõ (Lógica)" in unfolded


def test_task_lines_format_and_window():
    now = int(time.time())
    items = [
        (now + 86400, "", "Ética", "e-fólio A", "trabalho", 1, "new"),
        (now + 2 * 86400, "", "Ética", "e-fólio B", "trabalho", 2, "submitted"),
        (now - 30 * 86400, "", "Ética", "antigo", "trabalho", 3, "new"),
        (now + 3 * 86400, "", "Ética", "Fórum avaliado", "fórum", 4, "2 post(s) meus"),
    ]
    lines = server._task_lines(items, now)
    assert any(line.startswith("- [ ] e-fólio A — Ética 📅 ") for line in lines)
    assert any(line.startswith("- [x] e-fólio B") for line in lines)
    assert any(line.startswith("- [x] Fórum avaliado") for line in lines)
    assert not any("antigo" in line for line in lines)
    assert server._task_lines([], now) == []


# ------------------------------------------------------------------------------
# Sincronização completa
# ------------------------------------------------------------------------------


@pytest.fixture
def evolving_moodle(monkeypatch, vault):
    data = {"grade": "—", "discussions": [{"discussion": 500, "name": "Dúvidas", "timemodified": 10}]}

    def call(wsfunction, params=None, method="POST", instance="test"):
        if wsfunction == "core_webservice_get_site_info":
            return {"userid": 1, "fullname": "Aluno"}
        if wsfunction == "mod_forum_get_forum_discussions":
            return {"discussions": data["discussions"]}
        return {}

    monkeypatch.setattr(server, "_moodle_call", call)
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 1, "fullname": "Ética"}]}
    )
    monkeypatch.setattr(
        server,
        "get_assignments",
        lambda *a, **k: {
            "courses": [
                {
                    "id": 1,
                    "assignments": [{"id": 10, "name": "e-fólio A", "duedate": int(time.time()) + 86400}],
                }
            ]
        },
    )
    monkeypatch.setattr(
        server,
        "get_course_grades",
        lambda *a, **k: {
            "usergrades": [{"gradeitems": [{"itemname": "e-fólio A", "gradeformatted": data["grade"]}]}]
        },
    )
    monkeypatch.setattr(server, "get_course_forums", lambda *a, **k: [{"id": 7, "name": "Fórum"}])
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: [])
    monkeypatch.setattr(server, "get_assignment_submission", lambda *a, **k: {})
    return data


def test_sync_reports_what_changed_and_writes_calendar(evolving_moodle, vault):
    first = server.sync_dashboard(instance="test")
    note = vault / "O que mudou no Moodle.md"
    assert "Primeira sincronização" in note.read_text(encoding="utf-8")
    assert server.get_recent_changes(instance="test")["first_sync"] is True
    assert first["changes_count"] == 0

    evolving_moodle["grade"] = "4,0"
    evolving_moodle["discussions"].append({"discussion": 501, "name": "Grupo 2", "timemodified": 30})
    second = server.sync_dashboard(instance="test")

    text = note.read_text(encoding="utf-8")
    assert "e-fólio A: 4,0" in text and "Tópico novo" in text and "Grupo 2" in text
    recent = server.get_recent_changes(instance="test")
    assert recent["first_sync"] is False and len(recent["changes"]) == second["changes_count"] == 2

    calendar = (vault / "Doutoramento - Painel Moodle.ics").read_text(encoding="utf-8")
    assert "SUMMARY:e-fólio A (Ética)" in calendar
    dashboard = (vault / "Doutoramento - Painel Moodle.md").read_text(encoding="utf-8")
    assert "[[O que mudou no Moodle\\|O que mudou]]" in dashboard and "{changes_link}" not in dashboard
    assert "- [ ] e-fólio A — Ética 📅 " in dashboard


def test_sync_keeps_the_materials_state(evolving_moodle, vault):
    state.save(
        vault, "test", {"files": {"https://m/x.pdf": {"timemodified": 1, "filesize": 2, "path": "x.pdf"}}}
    )
    server.sync_dashboard(instance="test")
    assert "https://m/x.pdf" in state.load(vault, "test")["files"]


def test_recent_changes_before_any_sync(vault):
    assert server.get_recent_changes(instance="test")["changes"] == []
