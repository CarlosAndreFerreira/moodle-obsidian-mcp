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

#: Módulos que não são conteúdo (texto solto na página da UC).
_IGNORED_MODULES = frozenset({"label"})
_EMPTY_GRADES = frozenset({"", "-", "—"})


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if int(ts) else "sem prazo"
    except (TypeError, ValueError, OSError):
        return "sem prazo"


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
            add(name, "uc", "UC nova na tua lista de disciplinas")
            continue

        for item, grade in now.get("grades", {}).items():
            old = before.get("grades", {}).get(item)
            if grade not in _EMPTY_GRADES and grade != old:
                add(
                    name,
                    "nota",
                    f"{item}: {grade}" + (f" (antes: {old})" if old not in (None, *_EMPTY_GRADES) else ""),
                )

        for aid, task in now.get("assignments", {}).items():
            old = before.get("assignments", {}).get(aid)
            if not old:
                add(name, "trabalho", f"Trabalho novo: {task.get('name')} (prazo {_when(task.get('due'))})")
                continue
            if task.get("due") != old.get("due"):
                moved = f"{_when(old.get('due'))} → {_when(task.get('due'))}"
                add(name, "prazo", f"Prazo de «{task.get('name')}» mudou: {moved}")
            if task.get("status") != old.get("status"):
                add(name, "submissão", f"«{task.get('name')}»: {old.get('status')} → {task.get('status')}")

        for cmid, module in now.get("modules", {}).items():
            if module.get("type") in _IGNORED_MODULES or cmid in before.get("modules", {}):
                continue
            add(name, "material", f"Novo na página da UC: {module.get('name')} ({module.get('type')})")

        for did, disc in now.get("discussions", {}).items():
            old = before.get("discussions", {}).get(did)
            where = f"{disc.get('forum')}: «{disc.get('name')}»"
            if not old:
                author = f" de {disc['author']}" if disc.get("author") else ""
                add(name, "fórum", f"Tópico novo{author} em {where}")
            elif int(disc.get("modified") or 0) > int(old.get("modified") or 0):
                add(name, "fórum", f"Respostas novas em {where}")

        for cmid, quiz in now.get("quizzes", {}).items():
            old = before.get("quizzes", {}).get(cmid)
            if not old:
                add(name, "teste", f"Teste novo: {quiz.get('name')} (fecha {_when(quiz.get('close'))})")
            elif quiz.get("close") != old.get("close"):
                moved = f"{_when(old.get('close'))} → {_when(quiz.get('close'))}"
                add(name, "prazo", f"Fecho de «{quiz.get('name')}» mudou: {moved}")

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
        "# 🔔 O que mudou no Moodle",
        "",
    ]
    if changes is None:
        lines.append(f"Primeira sincronização ({now}): a partir da próxima, aparece aqui o que mudou.")
        return "\n".join(lines) + "\n"
    lines.append(f"Desde {since} até {now}.")
    lines.append("")
    if not changes:
        lines.append("_Nada de novo._")
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
