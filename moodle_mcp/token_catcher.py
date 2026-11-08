#!/usr/bin/env python3
"""Capturador do token móvel do Moodle a partir de `moodlemobile://token=...`.

Quando a universidade usa SSO federado (SAML, Shibboleth, CAS, Entra ID) e
esconde a página `/user/managetoken.php`, o caminho praticável para obter um
token é o fluxo da aplicação móvel: o navegador acaba redirecionado para
`moodlemobile://token=<base64>`, e um handler registado no sistema operativo
para esse esquema recebe o URL.

Este módulo é esse handler. Verifica o payload, guarda o token através do
`moodle_mcp.config` e sincroniza o Vault de imediato. Nunca escreve o token em
ficheiros de registo.

O sistema operativo entrega o URL como argumento de linha de comandos, e em
Linux e macOS os argumentos de qualquer processo são visíveis a todos os
utilizadores da máquina (`ps`, `/proc/<pid>/cmdline`). Como o payload contém o
token e o handler fica vivo durante a sincronização, a primeira coisa que faz é
substituir-se a si próprio (`execve`) sem o URL nos argumentos, passando-o por
uma variável de ambiente, que só o dono do processo consegue ler. Por isso os
imports pesados (o servidor inteiro) só acontecem depois dessa troca.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from moodle_mcp import config, login

#: Variável de ambiente pela qual o URL passa depois do `execve`.
ENV_URL = "MOODLE_MCP_LAUNCH_URL"


def log_file() -> Path:
    """Devolve o caminho do registo da última autenticação.

    Fica no diretório de configuração do utilizador, e não junto ao código:
    instalado com `uvx` ou `pipx`, o pacote corre de uma cache efémera e
    possivelmente só de leitura.

    Returns:
        O caminho do ficheiro de registo.

    """
    return config.config_dir() / "last_auth.log"


def _is_repeat_of_accepted_login(url: str) -> bool:
    """Diz se o URL repete uma resposta já aceite.

    Alguns navegadores chamam o handler duas vezes para o mesmo redirecionamento.
    A segunda chamada já não encontra o login pendente (o passport é de uso
    único) e, sem esta verificação, escrevia um erro por cima do resultado bom.
    Só conta como repetição se o token for exatamente um dos já guardados e não
    houver nenhum login à espera; nesse caso não há nada a fazer nem a registar.
    """
    parts = login.parse_launch_payload(url)
    token = parts[1].strip() if len(parts) > 1 else ""
    if not token or login.has_pending():
        return False
    return any(instance.token == token for instance in config.instances().values())


def handle_url(url: str) -> None:
    """Processa um URL `moodlemobile://token=...` e sincroniza o Vault.

    Args:
        url: O URL completo tal como o sistema operativo o entrega ao handler.

    """
    if _is_repeat_of_accepted_login(url):
        return

    from moodle_mcp import server as moodle_mcp  # pesado: só depois do execve

    target = log_file()
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        res = moodle_mcp.decode_mobile_launch_token(url, instance=moodle_mcp.DEFAULT_INSTANCE)
        if not res.get("saved"):
            # Sem token novo não há nada a sincronizar: prosseguir usaria o token
            # que já estivesse guardado e faria uma sincronização completa a
            # partir de um URL inválido.
            config.write_private(
                target,
                f"[{stamp}] ERRO: não foi possível obter um token do URL recebido.\n"
                f"{res.get('message', '')}\n",
            )
            return
        site_info = res.get("validation", {}).get("site_info", {})
        user = site_info.get("fullname", "Utilizador")
        instance = str(res.get("instance") or moodle_mcp.DEFAULT_INSTANCE)
        sync_res = moodle_mcp.sync_dashboard(
            download_materials=False,
            export_forums=True,
            instance=instance,
        )
        # Nem o token nem a sua impressão digital entram no registo.
        config.write_private(
            target,
            f"[{stamp}] OK\n"
            f"Instância: {instance}\n"
            f"Utilizador: {user}\n"
            f"Disciplinas sincronizadas: {sync_res.get('courses_synced', 0)}\n",
        )
    except Exception as exc:
        config.write_private(target, f"[{stamp}] ERRO: {exc}\n")


def _reexec_without_url(url: str) -> None:
    """Substitui o processo por outro sem o URL nos argumentos (Linux e macOS).

    Só regressa se o `execve` falhar; nesse caso o handler continua no processo
    atual, como antes.
    """
    if sys.platform == "win32":
        return  # em Windows outros utilizadores não leem a linha de comandos
    try:
        os.execve(
            sys.executable,
            [sys.executable, "-m", "moodle_mcp.token_catcher"],
            {**os.environ, ENV_URL: url},
        )
    except OSError:
        return


def cli() -> int:
    """Ponto de entrada do handler `moodlemobile://`.

    Recebe o URL como primeiro argumento, que é o que o sistema operativo passa
    ao handler registado para o esquema, e passa-o de imediato para uma variável
    de ambiente (ver o docstring do módulo).

    Returns:
        0 em caso de sucesso, 2 se não vier URL.

    """
    url = os.environ.pop(ENV_URL, "")
    if not url:
        if len(sys.argv) < 2:
            print("Uso: moodle-mcp-token-catcher moodlemobile://token=...", file=sys.stderr)
            return 2
        url = sys.argv[1]
        _reexec_without_url(url)
    handle_url(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
