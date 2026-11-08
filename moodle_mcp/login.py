"""Login pelo fluxo da app móvel com um *passport* de uso único.

O Moodle responde a `/admin/tool/mobile/launch.php?passport=<P>` com
`moodlemobile://token=<base64>`, onde o payload é
`md5(wwwroot . P):::token[:::privatetoken]` (`admin/tool/mobile/launch.php`).
O primeiro campo é a assinatura que permite ao recetor confirmar que a resposta
corresponde a um pedido seu.

Com um passport fixo (o `12345` que a documentação usava), essa verificação não
servia de nada: qualquer página web podia redirecionar o navegador para
`moodlemobile://token=<base64 de "x:::TOKEN_DO_ATACANTE">`, e o handler
substituía o token do estudante pelo do atacante (CSRF de login) e escrevia no
Vault o conteúdo da conta do atacante. Também não havia forma de saber de que
instância vinha o token, que era sempre gravado na predefinida.

Agora `moodle-mcp --login` gera um passport aleatório, guarda-o com a instância
e um prazo, e só aceita uma resposta assinada com ele, uma única vez.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import unquote

from moodle_mcp import config

#: Tempo para concluir o login no navegador.
LOGIN_TTL_SECONDS = 15 * 60


class LoginError(ValueError):
    """A resposta `moodlemobile://` não corresponde a nenhum login pendente válido."""


def parse_launch_payload(launch_url_or_base64: str) -> list[str]:
    """Descodifica o payload de `moodlemobile://token=<base64>` sem o validar.

    Args:
        launch_url_or_base64: O URL completo ou só a parte em Base64.

    Returns:
        Os campos `[assinatura, token, privatetoken?]`; uma lista com um único
        campo (possivelmente vazio) se o payload não tiver o formato esperado.

    """
    raw = launch_url_or_base64.strip()
    if "token=" in raw:
        raw = raw.split("token=", 1)[1].split("&", 1)[0]
    # Alguns navegadores entregam o payload com percent-encoding (%2B, %2F, %3D).
    raw = unquote(raw).strip()
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", errors="ignore")
    except (binascii.Error, ValueError):
        decoded = ""
    return decoded.split(":::")


def has_pending() -> bool:
    """Diz se há um login iniciado e ainda dentro do prazo."""
    pending = _load_pending()
    if not pending:
        return False
    return time.time() - float(pending.get("created", 0)) <= LOGIN_TTL_SECONDS


def _pending_file() -> Any:
    return config.config_dir() / "pending_login.json"


def launch_url(base_url: str, passport: str) -> str:
    """URL que inicia o fluxo da app móvel com um passport."""
    return f"{base_url}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport={passport}"


def start_login(instance: str = "") -> dict[str, Any]:
    """Prepara um login: gera e guarda um passport de uso único.

    Args:
        instance: Nome da instância; vazio usa a predefinida.

    Returns:
        Dicionário com `instance`, `url` (a abrir no navegador) e `expires_in`.

    Raises:
        LoginError: Se a instância não tiver URL configurado.

    """
    target = config.resolve_instance(instance)
    if not target.url:
        raise LoginError(
            f"A instância '{target.name}' não tem URL configurado. Define-o em config.toml "
            f"ou com MOODLE_{target.name.upper()}_URL."
        )
    passport = str(secrets.randbelow(10**18)).zfill(18)
    record = {"instance": target.name, "url": target.url, "passport": passport, "created": time.time()}
    config.write_private(_pending_file(), json.dumps(record) + "\n")
    return {"instance": target.name, "url": launch_url(target.url, passport), "expires_in": LOGIN_TTL_SECONDS}


def _load_pending() -> dict[str, Any] | None:
    try:
        data = json.loads(_pending_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def signature(base_url: str, passport: str) -> str:
    """Calcula a assinatura do Moodle: `md5(wwwroot . passport)`."""
    return hashlib.md5(f"{base_url}{passport}".encode(), usedforsecurity=False).hexdigest()


def verify(site_signature: str) -> str:
    """Confirma uma resposta `moodlemobile://` e consome o login pendente.

    Args:
        site_signature: O primeiro campo do payload descodificado.

    Returns:
        O nome da instância para a qual o login foi pedido.

    Raises:
        LoginError: Se não houver login pendente, se tiver expirado ou se a
            assinatura não corresponder. O login pendente só é consumido em
            caso de sucesso, para que uma resposta forjada não estrague um
            login legítimo em curso.

    """
    pending = _load_pending()
    if not pending:
        raise LoginError(
            "Não há nenhum login à espera de resposta. Começa com `moodle-mcp --login` "
            "(o link fixo com passport=12345 deixou de ser aceite, por segurança)."
        )
    if time.time() - float(pending.get("created", 0)) > LOGIN_TTL_SECONDS:
        _pending_file().unlink(missing_ok=True)
        raise LoginError("O login expirou. Volta a correr `moodle-mcp --login`.")

    expected = signature(str(pending["url"]), str(pending["passport"]))
    if not hmac.compare_digest(expected, site_signature.strip().lower()):
        raise LoginError(
            "A resposta não corresponde ao login pedido: ou não veio do Moodle configurado "
            f"({pending['url']}), ou o URL configurado difere do endereço do site (http/https, www). "
            "Nada foi gravado."
        )
    _pending_file().unlink(missing_ok=True)
    return str(pending["instance"])
