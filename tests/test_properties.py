"""Testes de propriedades (Hypothesis) para o que recebe texto arbitrário do Moodle ou do modelo.

Em vez de exemplos escolhidos à mão, geram-se milhares de entradas e
verifica-se uma propriedade que tem de valer para todas: é assim que aparecem
os edge cases que ninguém se lembrou de escrever.
"""

import unicodedata

import requests
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from urllib3.util import parse_url

from moodle_mcp import sanitize, transport
from moodle_mcp import vault as vaultlib

SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
FORBIDDEN = set('\\/:*?"<>|#^[]') | {chr(c) for c in range(32)} | {"\x7f"}
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
BASE = "https://moodle.example.edu"


# ------------------------------------------------------------------------------
# Nomes de ficheiro
# ------------------------------------------------------------------------------


@SETTINGS
@given(st.text())
def test_safe_filename_is_always_valid(name):
    result = vaultlib.safe_filename(name)
    assert result and result not in {".", ".."}
    assert not FORBIDDEN & set(result)
    assert not result.endswith((".", " "))
    assert len(result) <= 120
    assert result.split(".")[0].upper() not in RESERVED


@SETTINGS
@given(st.text())
def test_safe_filename_is_idempotent(name):
    once = vaultlib.safe_filename(name)
    assert vaultlib.safe_filename(once) == once


@SETTINGS
@given(st.text(alphabet=st.characters(categories=["L", "N"]), min_size=1, max_size=400))
def test_safe_filename_keeps_the_extension(stem):
    assert vaultlib.safe_filename(f"{stem}.pdf").endswith(".pdf")


# ------------------------------------------------------------------------------
# O token só vai para a instância configurada, seja qual for o URL
# ------------------------------------------------------------------------------

TRICKY = st.text(alphabet="abcdefghijklmnopqrstuvwxyz.-_~@:/\\?#%[]!$&'()*+,;=0123456789 ", max_size=30)


@SETTINGS
@given(st.sampled_from(["https", "http", "HTTPS", "ftp"]), TRICKY, TRICKY, TRICKY)
def test_accepted_urls_always_connect_to_the_instance(scheme, before, middle, path):
    """Se `validate_file_url` aceita, o `requests` liga-se mesmo ao anfitrião da instância."""
    for url in (
        f"{scheme}://{before}moodle.example.edu{middle}/webservice/pluginfile.php/{path}",
        f"{scheme}://moodle.example.edu{before}@{middle}/webservice/pluginfile.php/{path}",
        f"{scheme}://{before}@moodle.example.edu/webservice/pluginfile.php/{path}",
    ):
        try:
            accepted = transport.validate_file_url(url, BASE)
        except ValueError:
            continue
        sent = parse_url(requests.Request("GET", accepted).prepare().url)
        assert sent.scheme == "https"
        assert (sent.host or "").rstrip(".").lower() == "moodle.example.edu"
        assert sent.port in (None, 443)
        assert not sent.auth
        assert sent.path.startswith(
            ("/webservice/pluginfile.php/", "/pluginfile.php/", "/tokenpluginfile.php/")
        )


# ------------------------------------------------------------------------------
# Limpeza de conteúdo de terceiros
# ------------------------------------------------------------------------------


@SETTINGS
@given(st.text())
def test_strip_invisible_leaves_no_format_characters(text):
    result = sanitize.strip_invisible(text)
    assert not any(unicodedata.category(char) == "Cf" for char in result)
    assert not any(0xE0000 <= ord(char) <= 0xE007F for char in result)


@SETTINGS
@given(st.text(alphabet=st.characters(exclude_categories=["Cf", "Mn"])))
def test_strip_invisible_is_identity_on_visible_text(text):
    assert sanitize.strip_invisible(text) == text


HTMLISH = st.text(alphabet="<>/=\"' abcdefghijklmnopqrstuvwxyz:;-!0123456789\n", max_size=200)


@SETTINGS
@given(HTMLISH)
def test_html_cleaning_and_conversion_never_crash(fragment):
    assert isinstance(sanitize.clean_text(fragment), str)
    assert isinstance(vaultlib.html_to_markdown(fragment), str)


@SETTINGS
@given(
    st.lists(
        st.sampled_from(
            [
                "```dataviewjs",
                "```dataview",
                "> ```tasks",
                "`$= dv.x`",
                "`= this`",
                "<% tp %>",
                "![a](https://x/y.png)",
                "![](//cdn/p.gif)",
                "texto",
                "\n",
                "```",
                "![[local.png]]",
            ]
        ),
        max_size=12,
    ).map(" ".join)
)
def test_neutralized_markdown_has_no_active_content_and_is_stable(markdown):
    once = sanitize.neutralize_markdown(markdown)
    assert sanitize.neutralize_markdown(once) == once
    assert "<%" not in once and "`$=" not in once
    assert "![a](http" not in once and "![](//" not in once
    for line in once.splitlines():
        stripped = line.lstrip(" \t>")
        assert not stripped.lower().startswith(("```dataviewjs", "```dataview", "```tasks"))
