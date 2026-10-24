"""Configuração em camadas, independente do diretório de instalação.

Precedência, da maior para a menor:

1. Variáveis de ambiente — é por aqui que os clientes MCP injetam configuração
   (`env` no Claude Code, `environment` no opencode).
2. `config.toml` no diretório de configuração do utilizador — editado à mão.
3. `credentials.json` no mesmo diretório — escrito pelo servidor, só para segredos.
4. O `.env` legado ao lado do código — lido para retrocompatibilidade e migrado
   na primeira escrita.
5. Omissões.

O diretório é o do sistema operativo, via `platformdirs`:
`~/.config/moodle-mcp` (Linux), `~/Library/Application Support/moodle-mcp`
(macOS), `%LOCALAPPDATA%/moodle-mcp` (Windows). O motivo de não ficar junto ao
código é que, instalado com `uvx` ou `pipx`, o pacote corre de uma cache efémera
e possivelmente só de leitura: escrever o token lá falharia ou perder-se-ia.

Os segredos ficam separados das restantes opções para que o `config.toml` possa
ser partilhado ou versionado sem arrastar credenciais.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

APP_NAME = "moodle-mcp"

logger = logging.getLogger("moodle-mcp.config")

#: Opções não secretas: nome da chave -> (variável de ambiente, omissão).
SETTINGS: dict[str, tuple[str, str]] = {
    "moodle_url": ("MOODLE_URL", "https://elearning.uab.pt"),
    "moodle_utad_url": ("MOODLE_UTAD_URL", "https://e-learning.utad.pt"),
    "obsidian_vault_dir": ("OBSIDIAN_VAULT_DIR", "~/Doutoramento"),
    "tools": ("MOODLE_MCP_TOOLS", ""),
}

#: Opções secretas, guardadas em `credentials.json` em vez de `config.toml`.
SECRETS: dict[str, str] = {
    "moodle_token": "MOODLE_TOKEN",
    "moodle_utad_token": "MOODLE_UTAD_TOKEN",
}


def config_dir() -> Path:
    """Devolve o diretório de configuração, criando-o se necessário.

    `MOODLE_MCP_CONFIG_DIR` tem precedência, o que serve para testes e para
    instalações portáteis.
    """
    override = os.getenv("MOODLE_MCP_CONFIG_DIR", "").strip()
    path = Path(override).expanduser() if override else Path(user_config_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    """Caminho do `config.toml` (pode não existir: tudo tem omissão)."""
    return config_dir() / "config.toml"


def credentials_file() -> Path:
    """Caminho do `credentials.json`, onde ficam os tokens."""
    return config_dir() / "credentials.json"


def legacy_env_file() -> Path:
    """Caminho do `.env` legado, ao lado do código."""
    return Path(__file__).resolve().parent.parent / ".env"


def _read_toml() -> dict[str, Any]:
    path = config_file()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Ignorado %s por não ser legível: %s", path, exc)
        return {}


def _read_credentials() -> dict[str, str]:
    path = credentials_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignorado %s por não ser legível: %s", path, exc)
        return {}
    return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _read_legacy_env() -> dict[str, str]:
    """Lê o `.env` legado sem depender do python-dotenv.

    Aceita apenas `CHAVE=valor`, ignora comentários e linhas vazias, e remove
    aspas envolventes.
    """
    path = legacy_env_file()
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def get(key: str) -> str:
    """Devolve o valor de uma opção aplicando a ordem de precedência.

    Args:
        key: Nome da opção, de `SETTINGS` ou de `SECRETS`.

    Returns:
        O valor como texto, ou a omissão (`""` para segredos).

    Raises:
        KeyError: Se `key` não for uma opção conhecida.

    """
    if key in SECRETS:
        env_name, default = SECRETS[key], ""
    elif key in SETTINGS:
        env_name, default = SETTINGS[key]
    else:
        raise KeyError(f"Opção desconhecida: {key}")

    from_env = os.getenv(env_name, "").strip()
    if from_env:
        return from_env

    if key in SECRETS:
        stored = _read_credentials().get(key, "").strip()
    else:
        stored = str(_read_toml().get(key, "")).strip()
    if stored:
        return stored

    legacy = _read_legacy_env().get(env_name, "").strip()
    if legacy:
        logger.debug("'%s' veio do .env legado; será migrado na próxima escrita.", key)
        return legacy

    return default


def save_secret(key: str, value: str) -> Path:
    """Grava um segredo em `credentials.json` com permissões restritas.

    Args:
        key: Nome da opção secreta (chave de `SECRETS`).
        value: O valor a guardar.

    Returns:
        O caminho do ficheiro escrito.

    Raises:
        KeyError: Se `key` não for uma opção secreta conhecida.

    """
    if key not in SECRETS:
        raise KeyError(f"'{key}' não é uma opção secreta. Secretas: {', '.join(SECRETS)}")

    data = _read_credentials()
    # Traz consigo o que ainda estiver só no .env legado, para que a migração
    # não deixe metade dos tokens atrás.
    legacy = _read_legacy_env()
    for secret_key, env_name in SECRETS.items():
        if secret_key not in data and legacy.get(env_name, "").strip():
            data[secret_key] = legacy[env_name].strip()
    data[key] = value

    path = credentials_file()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _restrict_permissions(path)
    return path


def save_setting(key: str, value: str) -> Path:
    """Grava uma opção não secreta em `config.toml`.

    O ficheiro é reescrito por completo a partir dos valores conhecidos, pelo que
    comentários e chaves desconhecidas não sobrevivem — é uma troca deliberada
    para não precisar de uma dependência de escrita de TOML.

    Args:
        key: Nome da opção (chave de `SETTINGS`).
        value: O valor a guardar.

    Returns:
        O caminho do ficheiro escrito.

    Raises:
        KeyError: Se `key` não for uma opção conhecida.

    """
    if key not in SETTINGS:
        raise KeyError(f"'{key}' não é uma opção de config. Conhecidas: {', '.join(SETTINGS)}")

    current = {k: str(v) for k, v in _read_toml().items() if k in SETTINGS}
    current[key] = value

    lines = [
        f"# Configuração do {APP_NAME}. Os tokens ficam em credentials.json.",
        "# As variáveis de ambiente têm precedência sobre este ficheiro.",
        "",
    ]
    for name, item in sorted(current.items()):
        if item:
            escaped = item.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{name} = "{escaped}"')

    path = config_file()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _restrict_permissions(path: Path) -> None:
    """Restringe o ficheiro ao proprietário, onde o sistema o suporte.

    Em POSIX aplica 0600. No Windows o `chmod` não tem este efeito, pelo que a
    proteção fica a cargo das ACL do perfil do utilizador — ficam registadas em
    DEBUG para não dar a ideia de uma garantia que não existe.
    """
    if sys.platform == "win32":
        logger.debug("Em Windows, %s fica protegido pelas ACL do perfil.", path)
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover
        logger.warning("Não foi possível restringir as permissões de %s: %s", path, exc)


def _exists(path: Path) -> str:
    """Devolve "existe" ou "ausente", para o resumo de `describe()`."""
    return "existe" if path.exists() else "ausente"


def describe() -> dict[str, str]:
    """Resume a configuração efetiva para diagnóstico, sem revelar segredos.

    Returns:
        Dicionário com os caminhos em uso, os valores não secretos e, para cada
        segredo, apenas `"definido"` ou `"ausente"`.

    """
    summary: dict[str, str] = {
        "config_dir": str(config_dir()),
        "config_file": f"{config_file()} ({_exists(config_file())})",
        "credentials_file": f"{credentials_file()} ({_exists(credentials_file())})",
        "legacy_env": f"{legacy_env_file()} ({_exists(legacy_env_file())})",
    }
    summary.update({key: get(key) or "(omissão vazia)" for key in SETTINGS})
    summary.update({key: "definido" if get(key) else "ausente" for key in SECRETS})
    return summary
