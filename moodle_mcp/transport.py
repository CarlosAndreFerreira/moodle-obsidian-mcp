"""Transporte HTTP para o Moodle: sessões, novas tentativas e downloads autenticados.

Duas garantias atravessam o módulo: o token nunca aparece no texto de uma
exceção (que chega ao modelo), e só é juntado a URLs de ficheiros da instância
configurada.
"""

from __future__ import annotations

import functools
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from urllib3.util import Retry, parse_url

from moodle_mcp import __version__, config


class MoodleRequestError(RuntimeError):
    """Um pedido HTTP ao Moodle falhou.

    A mensagem tem só o método, o caminho e o código HTTP. O `requests` põe o
    URL completo, com a query, no texto de `HTTPError` e `ConnectionError`, e o
    token vai na query dos downloads; como o texto das exceções chega ao modelo
    (e ao histórico da sessão), nunca pode transportar o URL.
    """


#: Base do backoff exponencial entre tentativas, em segundos.
BACKOFF = 0.5

USER_AGENT = f"moodle-mcp/{__version__} (+https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp)"


@functools.cache
def session(retry: bool) -> requests.Session:
    """Sessão HTTP partilhada (keep-alive), com ou sem novas tentativas.

    Uma sincronização faz centenas de pedidos ao mesmo servidor: reutilizar a
    ligação TLS poupa um handshake por pedido, e uma falha transitória (502 de
    um proxy, 503 de manutenção curta, 429 por excesso de pedidos) deixa de
    deitar abaixo a sincronização inteira.

    Só pedidos idempotentes repetem depois de o servidor os ter recebido. Uma
    escrita (enviar mensagem, publicar no fórum) só repete se a ligação nem
    chegou a ser estabelecida: repetir depois de um 502 podia publicar duas vezes.

    Args:
        retry: True para leituras; False para escritas.

    Returns:
        A sessão configurada.

    """
    if retry:
        policy = Retry(
            total=4,
            connect=3,
            read=2,
            status=3,
            backoff_factor=BACKOFF,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
    else:
        policy = Retry(total=2, connect=2, read=0, status=0, other=0, backoff_factor=BACKOFF)
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=policy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = USER_AGENT
    return session


def request(method: str, url: str, *, retry: bool = True, **kwargs: Any) -> requests.Response:
    """Faz um pedido HTTP e converte qualquer falha numa exceção sem segredos.

    Args:
        method: Método HTTP.
        url: URL sem segredos na query (os segredos vão em `params`/`data`).
        retry: Se repete em falhas transitórias (só para pedidos idempotentes).
        **kwargs: Passados a `Session.request`.

    Returns:
        A resposta, já com `raise_for_status()` verificado.

    Raises:
        MoodleRequestError: Em qualquer falha de rede ou código HTTP de erro.

    """
    try:
        response = session(retry).request(method, url, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        # `from None`: a exceção original tem o URL com o token no texto.
        raise MoodleRequestError(f"{method.upper()} {urlparse(url).path} falhou: {detail}") from None
    return response


def json_body(response: requests.Response) -> Any:
    """Lê o JSON de uma resposta, com uma mensagem útil quando o Moodle devolve HTML."""
    try:
        return response.json()
    except ValueError:
        raise MoodleRequestError(
            f"{urlparse(response.url).path}: a resposta não é JSON (HTTP {response.status_code}). "
            "O Moodle pode estar em manutenção ou a pedir login no navegador."
        ) from None


#: Caminhos de ficheiro do Moodle aos quais o token pode ser acrescentado.
_FILE_ENDPOINTS = ("/webservice/pluginfile.php/", "/tokenpluginfile.php/", "/pluginfile.php/")

_DEFAULT_PORTS = {"https": 443, "http": 80}


def validate_file_url(file_url: str, base_url: str) -> str:
    r"""Garante que um URL de ficheiro pertence à instância Moodle configurada.

    O URL é interpretado depois de preparado pelo `requests`, ou seja, pelo
    mesmo parser que vai abrir a ligação. Comparar com o `urlparse` não chega:
    para `https://evil.example\@moodle/x` o `urlparse` lê o anfitrião
    `moodle`, mas o `requests` liga-se a `evil.example`.

    Args:
        file_url: O `fileurl` recebido (da API do Moodle ou do modelo).
        base_url: URL base da instância.

    Returns:
        O URL tal como o `requests` o vai enviar.

    Raises:
        ValueError: Se o esquema, o anfitrião, a porta ou o caminho não forem
            os de um ficheiro desta instância.

    """
    refused = ValueError(
        "Recusado: o URL não é um ficheiro (pluginfile.php) da instância Moodle configurada "
        f"({base_url or 'sem URL'}); o token só é enviado para essa instância. Se o Moodle usa "
        "outro endereço (http/https, www.), corrige o URL da instância na configuração."
    )
    try:
        prepared = requests.Request("GET", file_url.strip()).prepare().url or ""
        target, base = parse_url(prepared), parse_url(base_url)
    except (requests.RequestException, ValueError):
        raise refused from None

    def host(parsed: Any) -> str:
        return (parsed.host or "").rstrip(".").lower()

    def port(parsed: Any) -> int | None:
        return parsed.port or _DEFAULT_PORTS.get(parsed.scheme or "")

    if (
        not host(base)
        or target.auth
        or target.scheme != base.scheme
        or host(target) != host(base)
        or port(target) != port(base)
    ):
        raise refused

    path = target.path or ""
    if ".." in unquote(path).split("/"):
        raise refused
    base_path = (base.path or "").rstrip("/")
    if not any(path.startswith(base_path + endpoint) for endpoint in _FILE_ENDPOINTS):
        raise refused
    return prepared


def authed_get(file_url: str, instance: str, **kwargs: Any) -> requests.Response:
    """Descarrega um ficheiro autenticado do Moodle — único sítio onde o token vai num URL.

    Valida o URL contra a instância antes de juntar o token, e passa-o em
    `params` (nunca concatenado). Um redirecionamento não leva o token: o
    `requests` constrói o pedido seguinte a partir do `Location`.

    Args:
        file_url: O `fileurl` devolvido pela API do Moodle.
        instance: Nome da instância.
        **kwargs: Passados a `requests.request` (`stream`, `timeout`, ...).

    Returns:
        A resposta HTTP.

    Raises:
        ValueError: Se o URL não pertencer à instância ou não houver token.
        MoodleRequestError: Se o pedido falhar.

    """
    target = config.resolve_instance(instance)
    base_url, token = target.url, target.token
    if not token:
        raise ValueError(f"Token para '{instance}' não está configurado.")
    url = validate_file_url(file_url, base_url)
    return request("GET", url, params={"token": token}, **kwargs)
