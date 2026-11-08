"""O que mudou no Moodle desde a sincronização anterior.

Cada sincronização guarda um retrato de cada UC (notas, trabalhos e estado da
submissão, módulos, tópicos dos fóruns, testes) no estado do Vault. A seguinte
compara o retrato novo com esse e produz uma lista de mudanças: nota lançada,
prazo alterado, material novo, tópico novo ou com respostas novas. É o que dá
valor diário à ferramenta: em vez de percorrer cada UC, o estudante vê numa
nota o que aconteceu desde a última vez.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from moodle_mcp.i18n import t

#: Módulos que não são conteúdo (texto solto na página da UC).
_IGNORED_MODULES = frozenset({"label"})
_EMPTY_GRADES = frozenset({"", "-", "—"})


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if int(ts) else t("changes.no_due")
    except (TypeError, ValueError, OSError):
        return t("changes.no_due")


def diff(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, str]]:
    """Compara dois retratos e devolve as mudanças, por UC.

    Args:
        previous: O retrato da sincronização anterior (`{course_id: {...}}`).
        current: O retrato acabado de recolher.

    Returns:
        Uma lista de `{"course", "kind", "text"}`, pela ordem das UCs. Uma UC que
        não existia no retrato anterior conta como uma única mudança, em vez de
        listar todo o seu conteúdo como novo.

    """
    found: list[dict[str, str]] = []

    def add(course: str, kind: str, text: str) -> None:
        found.append({"course": course, "kind": kind, "text": text})

    for cid, now in current.items():
        name = str(now.get("name") or cid)
        before = previous.get(cid)
        if not isinstance(before, dict):
            add(name, "uc", t("changes.new_course"))
            continue

        for item, grade in now.get("grades", {}).items():
            old = before.get("grades", {}).get(item)
            if grade not in _EMPTY_GRADES and grade != old:
                before_text = t("changes.grade_before", old=old) if old not in (None, *_EMPTY_GRADES) else ""
                add(name, "nota", t("changes.grade", item=item, grade=grade) + before_text)

        for aid, task in now.get("assignments", {}).items():
            old = before.get("assignments", {}).get(aid)
            if not old:
                add(
                    name,
                    "trabalho",
                    t("changes.new_assignment", name=task.get("name"), due=_when(task.get("due"))),
                )
                continue
            if task.get("due") != old.get("due"):
                moved = f"{_when(old.get('due'))} → {_when(task.get('due'))}"
                add(name, "prazo", t("changes.due_moved", name=task.get("name"), moved=moved))
            if task.get("status") != old.get("status"):
                add(
                    name,
                    "submissão",
                    t("changes.status", name=task.get("name"), old=old.get("status"), new=task.get("status")),
                )

        for cmid, module in now.get("modules", {}).items():
            if module.get("type") in _IGNORED_MODULES or cmid in before.get("modules", {}):
                continue
            add(name, "material", t("changes.new_module", name=module.get("name"), type=module.get("type")))

        for did, disc in now.get("discussions", {}).items():
            old = before.get("discussions", {}).get(did)
            where = f"{disc.get('forum')}: «{disc.get('name')}»"
            if not old:
                author = t("changes.by", author=disc["author"]) if disc.get("author") else ""
                add(name, "fórum", t("changes.new_topic", author=author, where=where))
            elif int(disc.get("modified") or 0) > int(old.get("modified") or 0):
                add(name, "fórum", t("changes.replies", where=where))

        for cmid, quiz in now.get("quizzes", {}).items():
            old = before.get("quizzes", {}).get(cmid)
            if not old:
                add(
                    name,
                    "teste",
                    t("changes.new_quiz", name=quiz.get("name"), close=_when(quiz.get("close"))),
                )
            elif quiz.get("close") != old.get("close"):
                moved = f"{_when(old.get('close'))} → {_when(quiz.get('close'))}"
                add(name, "prazo", t("changes.close_moved", name=quiz.get("name"), moved=moved))

    return found


def render(changes: list[dict[str, str]] | None, since: str, now: str) -> str:
    """Escreve as mudanças como uma nota Markdown, agrupadas por UC.

    Args:
        changes: O resultado de `diff`, ou None na primeira sincronização.
        since: Quando foi a sincronização anterior.
        now: Quando foi esta.

    Returns:
        O texto da nota (com frontmatter).

    """
    lines = [
        "---",
        "tags: [moodle, novidades]",
        f"updated: {now}",
        "---",
        "",
        f"# {t('changes.heading')}",
        "",
    ]
    if changes is None:
        lines.append(t("changes.first_sync", now=now))
        return "\n".join(lines) + "\n"
    lines.append(t("changes.range", since=since, now=now))
    lines.append("")
    if not changes:
        lines.append(t("changes.nothing"))
        return "\n".join(lines) + "\n"
    icons = {
        "nota": "📊",
        "trabalho": "📝",
        "prazo": "⏰",
        "submissão": "📤",
        "material": "📂",
        "fórum": "💬",
        "teste": "🧪",
        "uc": "🎓",
    }
    course = None
    for change in changes:
        if change["course"] != course:
            course = change["course"]
            lines += ["", f"## {course}", ""]
        lines.append(f"- {icons.get(change['kind'], '•')} {change['text']}")
    return "\n".join(lines) + "\n"
