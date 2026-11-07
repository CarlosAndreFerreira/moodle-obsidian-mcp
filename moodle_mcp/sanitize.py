"""Limpeza do conteúdo de terceiros antes de chegar ao modelo ou ao Vault.

Posts de fórum, descrições e páginas são escritos por qualquer participante da
disciplina e são a principal via de *prompt injection* (OWASP LLM01). Não há
limpeza que torne um texto seguro para um modelo, mas há duas classes de
conteúdo que não têm razão de existir no que o modelo lê:

* **O que um humano não vê.** Texto em elementos com `display:none` ou
  `font-size:0`, e caracteres invisíveis: o bloco Unicode «tags»
  (U+E0000–E007F, o chamado *ASCII smuggling*), espaços de largura zero,
  seletores de variação e controlos bidirecionais (*Trojan Source*,
  CVE-2021-42574). Quem lê o post no Moodle não vê nada disto; o modelo lê
  tudo. Retirá-lo faz com que o modelo e o estudante vejam o mesmo texto.
* **O que o Obsidian executa ou carrega sozinho.** Blocos `dataviewjs` e
  expressões inline do Dataview, comandos do Templater e imagens remotas (que
  funcionam como pixel de rastreio). Ficam desativados nas notas escritas no
  Vault (OWASP LLM05, tratamento inseguro do output).

Não é aplicada normalização NFKC: altera o significado de texto académico
(`m²` passaria a `m2`, `½` a `1⁄2`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from bs4 import BeautifulSoup, Comment

# ==============================================================================
# CARACTERES INVISÍVEIS
# ==============================================================================

#: Intervalos de caracteres sem representação visível que podem transportar texto.
_INVISIBLE_RANGES = (
    (0xE0000, 0xE007F),  # Tags: cada letra ASCII tem um gémeo invisível.
    (0xE0100, 0xE01EF),  # Seletores de variação suplementares (codificam bytes).
    (0xFE00, 0xFE0F),  # Seletores de variação.
)


def _is_invisible(char: str) -> bool:
    code = ord(char)
    if any(low <= code <= high for low, high in _INVISIBLE_RANGES):
        return True
    # Cf = formatação: largura zero, controlos bidirecionais, BOM, soft hyphen.
    return unicodedata.category(char) == "Cf"


def strip_invisible(text: str) -> str:
    """Remove caracteres invisíveis que podem esconder instruções.

    Args:
        text: Texto vindo do Moodle.

    Returns:
        O texto sem caracteres de formatação (categoria Unicode `Cf`) nem
        seletores de variação. Texto sem nada a remover é devolvido intacto.

    """
    if text.isascii():
        return text
    return "".join(char for char in text if not _is_invisible(char))


# ==============================================================================
# HTML ESCONDIDO
# ==============================================================================

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*(hidden|collapse)"
    r"|font-size\s*:\s*0(?![.\d]*[1-9])"
    r"|opacity\s*:\s*0(?![.\d]*[1-9])"
    r"|(?<![-\w])(max-)?(height|width)\s*:\s*0(px|em|rem|%)?\s*(;|$)",
    re.IGNORECASE,
)
_NON_CONTENT_TAGS = ("script", "style", "template", "noscript")


def _is_hidden(tag: Any) -> bool:
    attrs = getattr(tag, "attrs", None) or {}
    if "hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(str(attrs.get("style", ""))))


def strip_hidden_html(html: str) -> str:
    """Remove de um fragmento HTML o que o navegador não mostra.

    Retira elementos escondidos por estilo inline, pelos atributos `hidden` e
    `aria-hidden`, comentários, e `<script>`/`<style>`. Se não houver nada a
    retirar, devolve o texto original sem o reserializar, para não alterar
    valores que só por acaso tenham `<`.

    Args:
        html: Fragmento HTML vindo do Moodle.

    Returns:
        O HTML sem o conteúdo escondido.

    """
    if "<" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    doomed: list[Any] = [node for node in soup.find_all(string=lambda s: isinstance(s, Comment))]
    doomed += soup.find_all(_NON_CONTENT_TAGS)
    doomed += [tag for tag in soup.find_all(True) if _is_hidden(tag)]
    if not doomed:
        return html
    for node in doomed:
        node.extract()
    return str(soup)


def clean_text(value: str) -> str:
    """Aplica as duas limpezas a um valor de texto do Moodle."""
    return strip_invisible(strip_hidden_html(value))


def clean_payload(payload: Any) -> Any:
    """Aplica `clean_text` a todos os textos de uma resposta JSON do Moodle.

    Args:
        payload: Estrutura desserializada (dict, list ou escalar).

    Returns:
        A mesma estrutura com os textos limpos.

    """
    if isinstance(payload, str):
        return clean_text(payload)
    if isinstance(payload, dict):
        return {key: clean_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [clean_payload(item) for item in payload]
    return payload


# ==============================================================================
# CONTEÚDO ATIVO NO OBSIDIAN
# ==============================================================================

#: Linguagens de bloco que plugins do Obsidian executam ou avaliam.
_ACTIVE_FENCE = re.compile(
    r"^(?P<indent>[ \t>]*)(?P<fence>`{3,}|~{3,})[ \t]*"
    r"(?P<lang>dataviewjs|dataview|tasks|query|templater|js-engine|run-js|button|meta-bind[\w-]*)\b",
    re.IGNORECASE | re.MULTILINE,
)
#: Expressões inline do Dataview (`= …` e `$= …`, as prefixações por omissão).
_INLINE_QUERY = re.compile(r"`(\s*)(\$?=)")
#: Comandos do Templater.
_TEMPLATER = re.compile(r"<%")
#: Imagens Markdown cujo destino é remoto (carregadas sozinhas ao abrir a nota).
_REMOTE_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>\s*(?:https?:)?//[^)\s]+)(?P<rest>[^)]*)\)")
#: Imagens HTML que sobrevivam à conversão.
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def neutralize_markdown(markdown: str) -> str:
    """Desativa no Markdown o que o Obsidian executaria ou carregaria sozinho.

    * Blocos ` ```dataviewjs ` e semelhantes passam a ` ```text `, mantendo o
      código visível como texto.
    * Expressões inline `` `= …` `` e `` `$= …` `` ficam com `＝` (U+FF1D),
      que o Dataview não reconhece.
    * `<%` do Templater passa a `&lt;%`.
    * Imagens remotas passam a links, que só abrem quando se clica.

    Args:
        markdown: Markdown gerado a partir de conteúdo de terceiros.

    Returns:
        O Markdown com o conteúdo ativo desativado.

    """
    if not markdown:
        return markdown
    text = _ACTIVE_FENCE.sub(
        lambda m: f"{m['indent']}{m['fence']}text ({m['lang']} desativado pelo moodle-mcp)", markdown
    )
    text = _INLINE_QUERY.sub(lambda m: f"`{m[1]}{m[2].replace('=', '＝')}", text)
    text = _TEMPLATER.sub("&lt;%", text)
    text = _REMOTE_IMAGE.sub(lambda m: f"[🖼 {m['alt'] or 'imagem'}]({m['url'].strip()}{m['rest']})", text)
    return _HTML_IMG.sub("[imagem remota removida]", text)
