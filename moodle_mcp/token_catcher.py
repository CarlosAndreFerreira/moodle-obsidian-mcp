#!/usr/bin/env python3
"""Capturador do token móvel do Moodle a partir de `moodlemobile://token=...`.

Quando a universidade usa SSO federado (SAML, Shibboleth, CAS, Entra ID) e
esconde a página `/user/managetoken.php`, o caminho praticável para obter um
token é o fluxo da aplicação móvel: o navegador acaba redirecionado para
`moodlemobile://token=<base64>`, e um handler registado no sistema operativo
para esse esquema recebe o URL.

Este módulo é esse handler. Descodifica o payload, guarda o token através do
`moodle_mcp.config` e sincroniza o Vault de imediato. Nunca escreve o token em
ficheiros de registo.
"""

import sys
from datetime import datetime
from pathlib import Path

from moodle_mcp import config
from moodle_mcp import server as moodle_mcp


def log_file() -> Path:
    """Devolve o caminho do registo da última autenticação.

    Fica no diretório de configuração do utilizador, e não junto ao código:
    instalado com `uvx` ou `pipx`, o pacote corre de uma cache efémera e
    possivelmente só de leitura.

    Returns:
        O caminho do ficheiro de registo.

    """
    return config.config_dir() / "last_auth.log"


def handle_url(url: str) -> None:
    """Processa um URL `moodlemobile://token=...` e sincroniza o Vault.

    Args:
        url: O URL completo tal como o sistema operativo o entrega ao handler.

    """
    target = log_file()
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        res = moodle_mcp.decode_mobile_launch_token(url, instance=moodle_mcp.DEFAULT_INSTANCE)
        if not res.get("saved"):
            # Sem token novo não há nada a sincronizar: prosseguir usaria o token
            # que já estivesse guardado e faria uma sincronização completa a
            # partir de um URL inválido.
            target.write_text(
                f"[{stamp}] ERRO: não foi possível obter um token do URL recebido.\n"
                f"{res.get('message', '')}\n",
                encoding="utf-8",
            )
            return
        site_info = res.get("validation", {}).get("site_info", {})
        user = site_info.get("fullname", "Utilizador")
        sync_res = moodle_mcp.sync_doctoral_dashboard_to_obsidian(
            download_materials=False,
            export_forums=True,
            instance=moodle_mcp.DEFAULT_INSTANCE,
        )
        # Nem o token nem a sua impressão digital entram no registo.
        target.write_text(
            f"[{stamp}] OK\n"
            f"Instância: {moodle_mcp.DEFAULT_INSTANCE}\n"
            f"Utilizador: {user}\n"
            f"Disciplinas sincronizadas: {sync_res.get('courses_synced', 0)}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        target.write_text(f"[{stamp}] ERRO: {exc}\n", encoding="utf-8")


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
