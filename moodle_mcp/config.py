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

As instâncias Moodle são um registo aberto, não uma lista fixa. Declaram-se em
`config.toml`::

    default_instance = "uab"

    [instances.uab]
    url = "https://elearning.uab.pt"

    [instances.utad]
    url = "https://e-learning.utad.pt"

ou por ambiente, com `MOODLE_<NOME>_URL` e `MOODLE_<NOME>_TOKEN`. `MOODLE_URL` e
`MOODLE_TOKEN` sem nome configuram a instância predefinida, o que mantém as
instalações antigas a funcionar sem alterações.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

APP_NAME = "moodle-mcp"

logger = logging.getLogger("moodle-mcp.config")

#: Opções não secretas: nome da chave -> (variável de ambiente, omissão).
SETTINGS: dict[str, tuple[str, str]] = {
    "obsidian_vault_dir": ("OBSIDIAN_VAULT_DIR", "~/Doutoramento"),
    "tools": ("MOODLE_MCP_TOOLS", ""),
    "default_instance": ("MOODLE_DEFAULT_INSTANCE", ""),
}

#: Opções secretas, guardadas em `credentials.json` em vez de `config.toml`.
#: Os tokens das instâncias não entram aqui: vivem em `credentials.json` sob
#: `instances.<nome>.token`, geridos por `save_instance_token`.
SECRETS: dict[str, str] = {}

#: Nome usado quando há um `MOODLE_URL` sem nome de instância associado.
FALLBACK_INSTANCE = "default"

#: Prefixos de ambiente que configuram o servidor e não uma instância.
_RESERVED_ENV_PREFIXES = ("MOODLE_MCP_", "MOODLE_DEFAULT_")

_ENV_INSTANCE = re.compile(r"^MOODLE_([A-Z0-9][A-Z0-9_]*)_(URL|TOKEN)$")


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


def _read_credentials_raw() -> dict[str, Any]:
    """Lê o `credentials.json` tal como está, incluindo a secção `instances`."""
    path = credentials_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignorado %s por não ser legível: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _read_credentials() -> dict[str, str]:
    """Devolve só as entradas escalares do `credentials.json`."""
    return {k: str(v) for k, v in _read_credentials_raw().items() if not isinstance(v, (dict, list))}


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
    existing = {
        str(k).lower(): str(v.get("url", ""))
        for k, v in (_read_toml().get("instances") or {}).items()
        if isinstance(v, dict)
    }
    return _write_toml(current, existing)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_toml(settings: dict[str, str], instance_urls: dict[str, str]) -> Path:
    """Reescreve o `config.toml` por completo a partir dos valores dados.

    Comentários e chaves desconhecidas não sobrevivem — troca deliberada para
    não precisar de uma dependência de escrita de TOML.

    Args:
        settings: Opções de topo (chaves de `SETTINGS`).
        instance_urls: Mapa `nome -> url` das instâncias.

    Returns:
        O caminho do ficheiro escrito.

    """
    lines = [
        f"# Configuração do {APP_NAME}. Os tokens ficam em credentials.json.",
        "# As variáveis de ambiente têm precedência sobre este ficheiro.",
        "",
    ]
    for name, item in sorted(settings.items()):
        if item:
            lines.append(f'{name} = "{_toml_escape(item)}"')
    for name, url in sorted(instance_urls.items()):
        if url:
            lines += ["", f"[instances.{name}]", f'url = "{_toml_escape(url)}"']

    path = config_file()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class Instance:
    """Uma instância Moodle configurada.

    Attributes:
        name: O nome pelo qual as ferramentas a identificam.
        url: URL base, sem barra final.
        token: O `wstoken`, ou `""` se ainda não estiver configurado.

    """

    name: str
    url: str
    token: str


def _instances_from_toml() -> dict[str, dict[str, str]]:
    raw = _read_toml().get("instances", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(name).lower(): {"url": str(body.get("url", "")).strip()}
        for name, body in raw.items()
        if isinstance(body, dict)
    }


def _instance_tokens_from_credentials() -> dict[str, str]:
    raw = _read_credentials_raw().get("instances", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(name).lower(): str(body.get("token", "")).strip()
        for name, body in raw.items()
        if isinstance(body, dict)
    }


def _instances_from_env(source: dict[str, str]) -> dict[str, dict[str, str]]:
    """Extrai instâncias de um mapa de variáveis `MOODLE_<NOME>_URL|TOKEN`."""
    found: dict[str, dict[str, str]] = {}
    for key, value in source.items():
        if not value.strip() or key.startswith(_RESERVED_ENV_PREFIXES):
            continue
        match = _ENV_INSTANCE.match(key)
        if not match:
            continue
        name, field = match.group(1).lower(), match.group(2).lower()
        found.setdefault(name, {})[field] = value.strip()
    return found


def instances() -> dict[str, Instance]:
    """Devolve todas as instâncias configuradas, por nome.

    Combina, por ordem crescente de precedência, o `config.toml`, o
    `credentials.json`, o `.env` legado e as variáveis de ambiente. Um
    `MOODLE_URL` ou `MOODLE_TOKEN` sem nome é atribuído à instância
    predefinida, o que mantém as instalações anteriores a funcionar.

    Returns:
        Mapa `nome -> Instance`. Pode vir vazio se nada estiver configurado.

    """
    merged: dict[str, dict[str, str]] = {}

    def absorb(found: dict[str, dict[str, str]]) -> None:
        for name, body in found.items():
            slot = merged.setdefault(name, {})
            slot.update({k: v for k, v in body.items() if v})

    absorb(_instances_from_toml())
    absorb({name: {"token": tok} for name, tok in _instance_tokens_from_credentials().items() if tok})
    absorb(_instances_from_env(_read_legacy_env()))
    absorb(_instances_from_env(dict(os.environ)))

    # `MOODLE_URL` / `MOODLE_TOKEN` sem nome formam a sua própria instância, e
    # SÓ quando não há nenhuma instância nomeada. Atribuí-los à instância
    # predefinida quando existem nomes faria um token de uma universidade ser
    # enviado para o servidor de outra, pelo que é preferível ignorá-los e
    # dizê-lo.
    legacy = _read_legacy_env()
    bare_url = (os.getenv("MOODLE_URL") or legacy.get("MOODLE_URL") or "").strip().rstrip("/")
    bare_token = (os.getenv("MOODLE_TOKEN") or legacy.get("MOODLE_TOKEN") or "").strip()
    if bare_url or bare_token:
        if merged:
            logger.debug(
                "MOODLE_URL/MOODLE_TOKEN sem nome ignorados: há instâncias nomeadas (%s). "
                "Usa MOODLE_<NOME>_URL / MOODLE_<NOME>_TOKEN.",
                ", ".join(sorted(merged)),
            )
        else:
            target = (get("default_instance") or "").strip().lower() or FALLBACK_INSTANCE
            merged[target] = {"url": bare_url, "token": bare_token}

    return {
        name: Instance(name=name, url=body.get("url", "").rstrip("/"), token=body.get("token", ""))
        for name, body in sorted(merged.items())
        if body.get("url") or body.get("token")
    }


def default_instance() -> str:
    """Devolve o nome da instância usada quando nenhuma é indicada.

    Usa `default_instance` da configuração se existir e for válido; senão, a
    única instância configurada; senão, `FALLBACK_INSTANCE`.
    """
    available = instances()
    configured = (get("default_instance") or "").strip().lower()
    if configured and configured in available:
        return configured
    if configured:
        logger.warning(
            "default_instance='%s' não está configurada; instâncias conhecidas: %s",
            configured,
            ", ".join(available) or "(nenhuma)",
        )
    if len(available) == 1:
        return next(iter(available))
    return FALLBACK_INSTANCE


def resolve_instance(name: str = "") -> Instance:
    """Resolve um nome de instância para a sua URL e token.

    Args:
        name: Nome da instância; vazio usa a predefinida.

    Returns:
        A `Instance` correspondente. O `token` pode vir vazio, o que as camadas
        de cima reportam como falta de configuração.

    Raises:
        ValueError: Se o nome não existir e não houver uma única instância para
            onde o encaminhar.

    """
    available = instances()
    wanted = (name or default_instance()).strip().lower()

    if wanted in available:
        return available[wanted]

    if len(available) == 1:
        # Nome desconhecido mas só há uma instância: encaminha em vez de falhar,
        # o que mantém a funcionar quem passe o nome antigo.
        only = next(iter(available.values()))
        logger.info("Instância '%s' desconhecida; a usar a única configurada ('%s').", wanted, only.name)
        return only

    if not available:
        return Instance(name=wanted, url="", token="")

    raise ValueError(
        f"Instância '{wanted}' não está configurada. Conhecidas: {', '.join(available)}. "
        "Ver `moodle-mcp --show-config`."
    )


def instance_names() -> list[str]:
    """Nomes das instâncias configuradas, para a schema das ferramentas."""
    names = list(instances())
    return names or [default_instance()]


def save_instance_token(name: str, token: str) -> Path:
    """Grava o token de uma instância em `credentials.json`.

    Args:
        name: Nome da instância.
        token: O `wstoken` a guardar.

    Returns:
        O caminho do ficheiro escrito.

    """
    data = _read_credentials_raw()
    bucket = data.setdefault("instances", {})
    if not isinstance(bucket, dict):
        bucket = data["instances"] = {}
    bucket.setdefault(name.strip().lower(), {})["token"] = token

    path = credentials_file()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _restrict_permissions(path)
    return path


def save_instance_url(name: str, url: str) -> Path:
    """Grava a URL de uma instância em `config.toml`.

    Args:
        name: Nome da instância.
        url: URL base, com ou sem barra final.

    Returns:
        O caminho do ficheiro escrito.

    """
    toml = _read_toml()
    bucket = toml.get("instances")
    existing = {
        str(k).lower(): str(v.get("url", "")) for k, v in (bucket or {}).items() if isinstance(v, dict)
    }
    existing[name.strip().lower()] = url.strip().rstrip("/")
    return _write_toml(
        {k: str(v) for k, v in toml.items() if k in SETTINGS},
        existing,
    )


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

    found = instances()
    summary["default_instance_efetiva"] = default_instance()
    if not found:
        summary["instances"] = "(nenhuma configurada)"
    for name, inst in found.items():
        token_state = "token definido" if inst.token else "SEM TOKEN"
        summary[f"instance[{name}]"] = f"{inst.url or '(sem url)'} — {token_state}"
    return summary
