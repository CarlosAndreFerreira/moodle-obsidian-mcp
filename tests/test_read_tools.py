"""Ferramentas de leitura: a função do Moodle chamada e os parâmetros enviados.

São invólucros finos sobre a API de Web Services, e é aí que um erro passa
despercebido: um parâmetro com o nome errado o Moodle ignora sem queixa, e a
ferramenta devolve dados de outra disciplina, de outro utilizador ou nenhuns.
"""

import json
from urllib.parse import parse_qsl

import pytest
import responses
from conftest import BASE

from moodle_mcp import server

REST = f"{BASE}/webservice/rest/server.php"
USERID = 42


@pytest.fixture
def moodle():
    """Moodle falso: responde por `wsfunction` e guarda os parâmetros de cada pedido."""
    answers: dict[str, object] = {"core_webservice_get_site_info": {"userid": USERID, "sitename": "Teste"}}
    calls: list[dict[str, str]] = []

    def reply(request):
        params = dict(parse_qsl(request.body))
        calls.append(params)
        return 200, {}, json.dumps(answers.get(params["wsfunction"], []))

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add_callback(responses.POST, REST, callback=reply, content_type="application/json")
        yield answers, calls


def _call(calls, wsfunction):
    matching = [c for c in calls if c["wsfunction"] == wsfunction]
    assert len(matching) == 1, [c["wsfunction"] for c in calls]
    return matching[0]


def test_get_course_pages_and_books_asks_the_four_module_types_of_that_course(moodle):
    answers, calls = moodle
    answers["mod_book_get_books_by_courses"] = {"books": [{"id": 1, "name": "Guia"}]}
    result = server.get_course_pages_and_books(7, instance="test")
    for wsfunction in (
        "mod_page_get_pages_by_courses",
        "mod_folder_get_folders_by_courses",
        "mod_resource_get_resources_by_courses",
        "mod_book_get_books_by_courses",
    ):
        assert _call(calls, wsfunction)["courseids[0]"] == "7"
    assert sorted(result) == ["books", "folders", "pages", "resources"]
    assert result["books"]["books"][0]["name"] == "Guia"


def test_get_activities_completion_status_is_for_the_authenticated_student(moodle):
    answers, calls = moodle
    answers["core_completion_get_activities_completion_status"] = {"statuses": [{"cmid": 3, "state": 1}]}
    result = server.get_activities_completion_status(7, instance="test")
    params = _call(calls, "core_completion_get_activities_completion_status")
    assert params["courseid"] == "7"
    assert params["userid"] == str(USERID)
    assert result["statuses"][0]["cmid"] == 3


def test_get_all_grades_overview_calls_the_overview_report(moodle):
    answers, calls = moodle
    answers["gradereport_overview_get_course_grades"] = {"grades": [{"courseid": 7, "grade": "15,00"}]}
    result = server.get_all_grades_overview(instance="test")
    _call(calls, "gradereport_overview_get_course_grades")
    assert result["grades"][0]["grade"] == "15,00"


def test_get_forum_discussions_passes_the_forum_id(moodle):
    answers, calls = moodle
    answers["mod_forum_get_forum_discussions"] = {"discussions": [{"discussion": 11, "name": "Dúvidas"}]}
    result = server.get_forum_discussions(5, instance="test")
    assert _call(calls, "mod_forum_get_forum_discussions")["forumid"] == "5"
    assert result["discussions"][0]["name"] == "Dúvidas"


def test_search_all_course_forums_matches_subject_body_and_discussion_name(moodle):
    answers, calls = moodle
    answers["mod_forum_get_forums_by_courses"] = [{"id": 5, "name": "Fórum Central", "course": 7}]
    answers["mod_forum_get_forum_discussions"] = {
        "discussions": [
            {"discussion": 11, "name": "Dúvidas sobre o E-FÓLIO A"},
            {"discussion": 12, "name": "Apresentações"},
        ]
    }
    answers["mod_forum_get_discussion_posts"] = {
        "posts": [
            {"id": 101, "subject": "Olá", "message": "<p>Bom dia</p>", "author": {"fullname": "Prof"}},
            {"id": 102, "subject": "Re", "message": "<p>O <b>e-fólio a</b> é individual</p>", "author": {}},
        ]
    }
    result = server.search_all_course_forums(7, "  e-Fólio A ", instance="test")

    # Discussão 11: o nome coincide, por isso entram os dois posts. Discussão 12: só o corpo do 102.
    found = [(m["discussion_id"], m["post_id"]) for m in result["matches"]]
    assert found == [(11, 101), (11, 102), (12, 102)]
    assert result["matches_count"] == 3
    assert result["matches"][0]["forum"] == "Fórum Central"
    assert "<b>" not in result["matches"][1]["excerpt"]


def test_search_all_course_forums_keeps_going_when_one_forum_fails(moodle):
    answers, calls = moodle
    answers["mod_forum_get_forums_by_courses"] = [{"id": 5, "name": "A"}, {"id": 6, "name": "B"}]
    answers["mod_forum_get_forum_discussions"] = {"exception": "x", "errorcode": "nopermissions"}
    result = server.search_all_course_forums(7, "qualquer", instance="test")
    assert result["matches_count"] == 0
    assert len([c for c in calls if c["wsfunction"] == "mod_forum_get_forum_discussions"]) == 2


def test_get_calendar_events_for_one_course_starts_now(moodle, monkeypatch):
    answers, calls = moodle

    class FixedNow:
        @staticmethod
        def now():
            from datetime import datetime

            return datetime.fromtimestamp(1_800_000_000)

    monkeypatch.setattr(server, "datetime", FixedNow)
    server.get_calendar_events(7, instance="test")
    params = _call(calls, "core_calendar_get_calendar_events")
    assert params["events[courseids][0]"] == "7"
    assert params["options[timestart]"] == "1800000000"


def test_get_calendar_events_without_course_uses_the_upcoming_view(moodle):
    answers, calls = moodle
    answers["core_calendar_get_calendar_upcoming_view"] = {"events": [{"name": "Entrega"}]}
    result = server.get_calendar_events(instance="test")
    _call(calls, "core_calendar_get_calendar_upcoming_view")
    assert result["events"][0]["name"] == "Entrega"


@pytest.mark.parametrize(("read", "expected"), [(False, "0"), (True, "1")])
def test_get_messages_reads_notifications_without_marking_them_read(moodle, read, expected):
    answers, calls = moodle
    server.get_messages(read=read, instance="test")
    params = _call(calls, "core_message_get_messages")
    assert params["useridto"] == str(USERID)
    assert params["type"] == "notifications"
    assert params["read"] == expected
    # A ferramenta declara readOnlyHint=True: não pode mudar o estado das notificações.
    assert "markasread" not in params
