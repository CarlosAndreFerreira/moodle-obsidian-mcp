"""Registo de auditoria das ações no Moodle e minimização de dados pessoais."""

import asyncio
import json
import stat
import sys
from types import SimpleNamespace

import pytest
import responses
from conftest import BASE, TOKEN

from moodle_mcp import confirm, server

REST = f"{BASE}/webservice/rest/server.php"


class Ctx:
    def __init__(self, action="accept"):
        self.action = action
        self.session = SimpleNamespace(check_client_capability=lambda caps: True)

    async def elicit(self, message, schema):
        if self.action == "accept":
            return SimpleNamespace(action="accept", data=schema(confirmar=True))
        return SimpleNamespace(action=self.action, data=None)


def _records():
    path = confirm.audit_file()
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    )


@pytest.fixture(autouse=True)
def clean(cfg_dir, monkeypatch):
    monkeypatch.setattr(confirm, "untrusted_content_seen", False)
    monkeypatch.delenv("MOODLE_MCP_CONFIRM_WRITES", raising=False)


@responses.activate
def test_every_decision_is_audited():
    responses.post(REST, json=[{"id": 42, "fullname": "Prof. Ana"}])
    asyncio.run(server.send_instant_message(42, "olá", instance="test", ctx=Ctx()))
    with pytest.raises(PermissionError):
        asyncio.run(server.send_instant_message(42, "spam", instance="test", ctx=Ctx("decline")))
    with pytest.raises(PermissionError):
        asyncio.run(server.send_instant_message(42, "x", instance="test", ctx=None))

    decisions = [r["decisao"] for r in _records()]
    assert decisions == [
        "confirmada pelo utilizador",
        "recusada pelo utilizador (decline)",
        "recusada: cliente sem elicitation",
    ]
    assert _records()[0]["detalhes"]["Mensagem"] == "olá"


@responses.activate
def test_client_mode_is_audited_too(monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_CONFIRM_WRITES", "client")
    responses.post(REST, json=[{"id": 1}])
    asyncio.run(server.toggle_activity_completion(5, instance="test", ctx=None))
    assert _records()[-1]["decisao"] == "executada sem confirmação do servidor (modo client)"


def test_audit_log_is_private_has_no_token_and_rotates(monkeypatch):
    monkeypatch.setattr(confirm, "AUDIT_MAX_BYTES", 200)
    for n in range(10):
        confirm.audit("Ação", {"Texto": "x" * 50, "n": n}, "confirmada pelo utilizador")
    path = confirm.audit_file()
    assert path.with_name("audit.log.1").exists()
    assert path.stat().st_size <= 200 + 300
    assert TOKEN not in path.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_audit_failure_never_blocks(monkeypatch):
    def broken():
        raise OSError("disco só de leitura")

    monkeypatch.setattr(confirm, "audit_file", broken)
    confirm.audit("Ação", {}, "confirmada pelo utilizador")  # não levanta


@responses.activate
def test_participants_only_expose_what_is_needed():
    responses.post(
        REST,
        json=[
            {
                "id": 7,
                "fullname": "Colega",
                "email": "colega@exemplo.pt",
                "lastaccess": 1700000000,
                "city": "Lisboa",
                "profileimageurl": "https://x/p.png",
                "customfields": [{"name": "Telefone", "value": "912345678"}],
                "roles": [{"roleid": 5, "shortname": "student", "name": "Estudante"}],
                "groups": [{"id": 1, "name": "Grupo A", "description": ""}],
            }
        ],
    )
    result = server.get_course_participants(3, instance="test")
    assert result == [{"id": 7, "fullname": "Colega", "roles": ["student"], "groups": ["Grupo A"]}]
    assert "colega@exemplo.pt" not in json.dumps(result)


# ------------------------------------------------------------------------------
# Conteúdo não confiável é contado por sessão, não por processo
# ------------------------------------------------------------------------------


@responses.activate
def test_untrusted_content_is_tracked_per_session(monkeypatch):
    """Dois clientes no mesmo servidor: só quem leu posts tem de confirmar."""
    from mcp import types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    monkeypatch.setenv("MOODLE_MCP_CONFIRM_WRITES", "untrusted")
    server.offload_blocking_tools()

    def reply(request):
        if "core_user_get_users_by_field" in request.body:
            return 200, {}, json.dumps([{"id": 42, "fullname": "Prof. Ana"}])
        if "mod_forum_get_discussion_posts" in request.body:
            return 200, {}, json.dumps({"posts": [{"id": 1, "message": "olá"}]})
        return 200, {}, json.dumps([{"msgid": 1}])

    responses.add_callback(responses.POST, REST, callback=reply)
    prompts: dict[str, int] = {"leitor": 0, "outro": 0}

    def answering(name):
        async def callback(context, params):
            prompts[name] += 1
            return types.ElicitResult(action="accept", content={"confirmar": True})

        return callback

    send = {"to_user_id": 42, "text": "olá", "instance": "test"}

    async def scenario():
        async with connect(server.mcp._mcp_server, elicitation_callback=answering("leitor")) as reader:
            async with connect(server.mcp._mcp_server, elicitation_callback=answering("outro")) as other:
                await reader.call_tool("get_discussion_posts", {"discussion_id": 1, "instance": "test"})
                assert not (await other.call_tool("send_instant_message", send)).isError
                assert not (await reader.call_tool("send_instant_message", send)).isError

    # Ferramentas de escrita não estão no toolset por omissão do servidor de testes,
    # mas estão registadas; o servidor em memória expõe-nas todas.
    asyncio.run(scenario())
    assert prompts == {"leitor": 1, "outro": 0}
    assert confirm.untrusted_content_seen is False  # nada vazou para o estado global
