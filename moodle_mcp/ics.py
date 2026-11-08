"""Prazos do Moodle num calendário iCalendar (RFC 5545).

O ficheiro `.ics` gerado em cada sincronização pode ser importado, ou subscrito
a partir do Vault, no Google Calendar, Outlook, Thunderbird ou no calendário
do telemóvel: os prazos de trabalhos, fóruns avaliados e testes passam a
aparecer onde o estudante já organiza o tempo, com um alarme na véspera.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from moodle_mcp.i18n import t

PRODID = "-//moodle-obsidian-mcp//prazos//PT"


def _escape(text: Any) -> str:
    """Escapa um valor TEXT (RFC 5545 §3.3.11)."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Dobra linhas com mais de 75 octetos (RFC 5545 §3.1), sem partir caracteres UTF-8."""
    out: list[str] = []
    current, size = "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        limit = 75 if not out else 74  # as continuações começam com um espaço
        if size + width > limit:
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return [out[0]] + [" " + part for part in out[1:]]


def _stamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def build(items: list[tuple[int, str, str, str, str, Any, str]], instance: str, base_url: str) -> str:
    """Constrói o calendário a partir dos prazos da sincronização.

    Args:
        items: `(timestamp, data, UC, atividade, tipo, id, estado)`, como no painel.
        instance: Nome da instância (entra no UID, para não colidir entre Moodles).
        base_url: URL do Moodle, para a descrição.

    Returns:
        O texto do `.ics`, com fins de linha CRLF.

    """
    now = _stamp(int(datetime.now(tz=UTC).timestamp()))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(t('ics.calname', instance=instance))}",
    ]
    for due_ts, _due_str, course, activity, kind, ref_id, status in sorted(items, key=lambda x: x[0]):
        if not due_ts:
            continue
        label = t("kind." + kind)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_escape(f'{instance}-{kind}-{ref_id}')}@moodle-obsidian-mcp",
            f"DTSTAMP:{now}",
            f"DTSTART:{_stamp(int(due_ts))}",
            "DURATION:PT0M",
            f"SUMMARY:{_escape(f'{activity} ({course})')}",
            f"DESCRIPTION:{_escape(t('ics.description', kind=label, status=status, base_url=base_url))}",
            f"CATEGORIES:{_escape(label)}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(t('ics.alarm', activity=activity))}",
            "TRIGGER:-P1D",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _fold(line)) + "\r\n"
