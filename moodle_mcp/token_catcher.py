#!/usr/bin/env python3
"""
Capturador automático de Token Mobile do Moodle (moodlemobile://token=...).
Quando o navegador Brave/Chrome/Firefox redireciona para moodlemobile://token=...,
este script descodifica o token, guarda-o no .env (sem expor segredos em logs)
e sincroniza o Painel do Doutoramento no Obsidian!
"""
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from moodle_mcp import server as moodle_mcp


def handle_url(url: str):
    log_file = BASE_DIR / "last_auth.log"
    try:
        res = moodle_mcp.decode_mobile_launch_token(url, instance="uab")
        site_info = res.get("validation", {}).get("site_info", {})
        user = site_info.get("fullname", "Utilizador")
        sync_res = moodle_mcp.sync_doctoral_dashboard_to_obsidian(
            download_materials=False,
            export_forums=True,
            instance="uab",
        )
        # Never write raw tokens to log files
        log_file.write_text(
            f"[{datetime.now().isoformat()}] SUCCESS!\n"
            f"User: {user}\n"
            f"Courses synced: {sync_res.get('courses_synced', 0)}\n",
            encoding="utf-8",
        )
    except Exception as e:
        log_file.write_text(
            f"[{datetime.now().isoformat()}] ERROR: {e}\n",
            encoding="utf-8",
        )


def cli() -> int:
    """Ponto de entrada do handler `moodlemobile://`.

    Recebe o URL como primeiro argumento, que é o que o sistema operativo passa
    ao handler registado para o esquema.

    Returns:
        0 em caso de sucesso, 2 se não vier URL.
    """
    if len(sys.argv) < 2:
        print("Uso: moodle-mcp-token-catcher moodlemobile://token=...", file=sys.stderr)
        return 2
    handle_url(sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
