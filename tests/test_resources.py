"""Resources e prompts MCP: leem o estado da última sincronização e marcam a sessão."""

import asyncio
import json

import pytest

from moodle_mcp import confirm, server, state
from moodle_mcp import vault as vaultlib


def _read(uri):
    contents = asyncio.run(server.mcp.read_resource(uri))
    return contents[0].content


@pytest.fixture
def synced(vault):
    dashboard = vault / "Painel.md"
    dashboard.write_text("# Painel\n\nconteúdo", encoding="utf-8")
    state.save(
        vault,
        "test",
        {
            "synced_at": "2026-11-08 10:00",
            "last_changes": {
                "since": "a",
                "at": "b",
                "first_sync": False,
                "changes": [{"course": "Ética", "kind": "nota", "text": "e-fólio A: 3,5"}],
            },
            "deadlines": [
                {
                    "due": 4_000_000_000,
                    "date": "2096",
                    "course": "Ética",
                    "activity": "e-fólio B",
                    "kind": "trabalho",
                    "id": 1,
                    "status": "new",
                },
                {
                    "due": 1,
                    "date": "1970",
                    "course": "Ética",
                    "activity": "antigo",
                    "kind": "trabalho",
                    "id": 2,
                    "status": "new",
                },
            ],
            "dashboard": "Painel.md",
        },
    )
    return vault


def test_resources_serve_the_last_sync(synced):
    changes = json.loads(_read("moodle://changes/test"))
    assert changes["changes"][0]["text"] == "e-fólio A: 3,5"
    deadlines = json.loads(_read("moodle://deadlines"))
    assert [d["activity"] for d in deadlines["deadlines"]] == ["e-fólio B"]  # o antigo fica de fora
    assert _read("moodle://dashboard").startswith("# Painel")


def test_reading_a_resource_marks_untrusted_content(synced, monkeypatch):
    monkeypatch.setattr(confirm, "untrusted_content_seen", False)
    _read("moodle://changes")
    assert confirm.untrusted_content_seen is True


def test_resources_before_any_sync(vault):
    assert json.loads(_read("moodle://deadlines"))["deadlines"] == []
    assert "Ainda não houve" in _read("moodle://dashboard")


def test_dashboard_path_in_state_cannot_leave_the_vault(synced):
    data = state.load(synced, "test")
    data["dashboard"] = "../../etc/passwd"
    state.save(synced, "test", data)
    with pytest.raises(Exception, match="fora do Vault"):
        _read("moodle://dashboard")


def test_unknown_instance_is_rejected(synced, monkeypatch):
    monkeypatch.setattr(server.config, "instances", lambda: {"a": object(), "b": object()})
    with pytest.raises(ValueError):
        _read("moodle://changes/..%2F..%2Fsegredo")


def test_prompts_carry_no_moodle_content(synced):
    for name, args in (
        ("weekly_review", {}),
        ("prepare_assignment", {"course_id": "42"}),
        ("forum_catch_up", {"course_id": "42"}),
    ):
        result = asyncio.run(server.mcp.get_prompt(name, args))
        text = result.messages[0].content.text
        assert "e-fólio A" not in text and "Painel" not in text  # nada vindo do Vault ou do Moodle
        assert "instruções" in text and "Não submetas" in text


def test_sync_records_deadlines_and_dashboard(vault, monkeypatch):
    def call(wsfunction, params=None, method="POST", instance="test"):
        return {"userid": 1, "fullname": "Aluno"} if wsfunction == "core_webservice_get_site_info" else {}

    import time

    monkeypatch.setattr(server, "_moodle_call", call)
    monkeypatch.setattr(
        server, "get_enrolled_courses", lambda *a, **k: {"courses": [{"id": 1, "fullname": "UC"}]}
    )
    due = int(time.time()) + 86400
    monkeypatch.setattr(
        server,
        "get_assignments",
        lambda *a, **k: {"courses": [{"id": 1, "assignments": [{"id": 9, "name": "T1", "duedate": due}]}]},
    )
    monkeypatch.setattr(server, "get_course_grades", lambda *a, **k: {})
    monkeypatch.setattr(server, "get_course_forums", lambda *a, **k: [])
    monkeypatch.setattr(server, "get_course_contents", lambda *a, **k: [])
    monkeypatch.setattr(server, "get_assignment_submission", lambda *a, **k: {})
    server.sync_dashboard(instance="test")
    data = state.load(vault, "test")
    assert data["deadlines"][0]["activity"] == "T1"
    assert (vault / data["dashboard"]).is_file()
    assert vaultlib.MANAGED_START in _read("moodle://dashboard/test")
