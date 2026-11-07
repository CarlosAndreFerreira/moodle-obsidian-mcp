"""O Vault do Obsidian: confinamento, nomes de ficheiro e escrita de notas.

Tudo o que o servidor escreve em disco passa por aqui. `resolve_in_vault`
recusa qualquer caminho fora do Vault, `safe_filename` produz nomes válidos em
todos os sistemas e que não partem as ligações do Obsidian, e `write_note`
escreve de forma atómica com o conteúdo ativo do Obsidian desativado.
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

from markdownify import markdownify as md_convert

from moodle_mcp import config, sanitize

OBSIDIAN_VAULT_DIR = os.path.expanduser(config.get("obsidian_vault_dir"))


def vault_root() -> Path:
    """Devolve a raiz canónica do Vault do Obsidian (único diretório gravável)."""
    return Path(OBSIDIAN_VAULT_DIR).expanduser().resolve()


def resolve_in_vault(candidate: Any) -> Path:
    """Resolve um caminho e recusa qualquer um que escape ao Vault do Obsidian.

    Os nomes de ficheiro e os `fileurl` vêm do servidor Moodle e os caminhos de
    destino são escolhidos pelo modelo, pelo que uma página de curso manipulada
    poderia de outra forma dirigir uma escrita para `~/.ssh/authorized_keys` ou
    `~/.config/autostart/`. `resolve()` segue symlinks, pelo que um link dentro do
    Vault a apontar para fora também é apanhado.

    Args:
        candidate: Caminho absoluto ou relativo (relativo resolve a partir da raiz
            do Vault).

    Returns:
        O caminho absoluto canónico, garantidamente dentro do Vault.

    Raises:
        ValueError: Se o caminho resolvido ficar fora do Vault.

    """
    root = vault_root()
    raw = Path(str(candidate)).expanduser()
    if not raw.is_absolute():
        raw = root / raw
    target = raw.resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError(
            f"Acesso recusado: '{target}' está fora do Vault do Obsidian ('{root}'). "
            "Por segurança, o servidor só lê e escreve ficheiros dentro do Vault; "
            "para mudar de Vault, altera OBSIDIAN_VAULT_DIR na configuração."
        )
    return target


def html_to_markdown(html_text: str) -> str:
    """Transforma HTML do Moodle em Markdown, depois de retirar o conteúdo escondido."""
    if not html_text:
        return ""
    html_text = sanitize.clean_text(html_text)
    try:
        converted = md_convert(html_text, heading_style="ATX").strip()
        return re.sub(r"\n{3,}", "\n\n", converted)
    except Exception:
        clean = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
        clean = re.sub(r"</p>", "\n\n", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", "", clean)
        return html.unescape(clean).strip()


def yaml_scalar(value: Any) -> str:
    """Valor escalar seguro para frontmatter YAML.

    Uma string JSON é YAML válido: aspas, `:`, `#` e quebras de linha num nome
    de UC ou de fórum deixam de partir o frontmatter (e as propriedades do
    Obsidian e as consultas Dataview que dele dependem).
    """
    return json.dumps(str(value), ensure_ascii=False)


def table_cell(value: Any) -> str:
    """Texto seguro para uma célula de tabela Markdown (sem `|` nem quebras de linha)."""
    return re.sub(r"\s*\n\s*", " ", str(value)).replace("|", "\\|")


def write_note(path: Path, markdown: str) -> None:
    """Escreve uma nota no Vault com o conteúdo ativo do Obsidian desativado.

    Todas as notas geradas a partir do Moodle passam por aqui: incluem nomes,
    posts e descrições escritos por terceiros, e um bloco `dataviewjs` ou uma
    imagem remota num post não pode executar código nem chamar um servidor
    quando o estudante abre a nota.
    """
    config.atomic_write(path, sanitize.neutralize_markdown(markdown), 0o644)


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_MAX_NAME = 120


def safe_filename(name: str) -> str:
    """Torna um nome do Moodle num nome de ficheiro válido em todos os sistemas.

    * Retira separadores, caracteres proibidos no Windows e de controlo.
    * Recusa `.`/`..`/vazio, que mudariam de diretório ou não teriam nome.
    * Retira pontos e espaços finais (o Windows descarta-os em silêncio).
    * Prefixa nomes reservados do Windows (`CON`, `NUL`, `COM1`…), com ou sem extensão.
    * Trunca o nome, não a extensão: `aula.pdf` com 200 caracteres continua a
      acabar em `.pdf` e a ter o texto extraído.
    """
    # `#^[]` são válidos em ficheiros mas partem as ligações [[...]] do Obsidian.
    cleaned = re.sub(r'[\\/*?:"<>|#^\[\]\x00-\x1f\x7f]+', "-", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    if not cleaned.strip("."):
        return "_"
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot or not stem or len(suffix) > 10 or " " in suffix:
        stem, suffix = cleaned, ""
    if len(cleaned) > _MAX_NAME:
        keep = _MAX_NAME - (len(suffix) + 1 if suffix else 0)
        stem = stem[:keep].rstrip(". ")
        cleaned = f"{stem}.{suffix}" if suffix else stem
    if (stem if suffix else cleaned).split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def unique_path(path: Path, used: set[Path]) -> Path:
    """Devolve um caminho ainda não usado nesta execução, com ` (2)`, ` (3)`… se preciso.

    Dois módulos com o mesmo nome (ou todas as páginas `index.html` de uma
    secção) escreviam no mesmo ficheiro e só o último sobrevivia.
    """
    candidate, counter = path, 2
    while candidate in used:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    used.add(candidate)
    return candidate
