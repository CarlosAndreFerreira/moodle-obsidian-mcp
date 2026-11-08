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
from datetime import datetime
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


def md_link(text: Any, url: str) -> str:
    """Link Markdown com texto e destino seguros, mesmo com nomes vindos do Moodle.

    Um `]` no nome de um módulo fechava o texto do link antes do tempo, e um
    `)` ou um espaço no URL cortava o destino. O texto fica com parênteses
    retos e barras invertidas escapados e sem quebras de linha; um destino com espaços, parênteses ou
    `<>` vai entre `<...>` (CommonMark), com `<` e `>` codificados.

    Args:
        text: Texto visível do link.
        url: Destino.

    Returns:
        O link `[texto](destino)`.

    """
    label = re.sub(r"\s*\n\s*", " ", str(text)).strip() or "link"
    label = re.sub(r"([\\\[\]])", r"\\\1", label)
    target = str(url).strip()
    if re.search(r"[\s()<>]", target):
        target = "<" + target.replace("<", "%3C").replace(">", "%3E") + ">"
    return f"[{label}]({target})"


def write_file(path: Path, text: str) -> None:
    """Escreve um ficheiro que não é Markdown (p.ex. o `.ics`), de forma atómica."""
    path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(path, text, 0o644)


def write_note(path: Path, markdown: str) -> None:
    """Escreve uma nota no Vault com o conteúdo ativo do Obsidian desativado.

    Todas as notas geradas a partir do Moodle passam por aqui: incluem nomes,
    posts e descrições escritos por terceiros, e um bloco `dataviewjs` ou uma
    imagem remota num post não pode executar código nem chamar um servidor
    quando o estudante abre a nota.
    """
    config.atomic_write(path, sanitize.neutralize_markdown(markdown), 0o644)


# ==============================================================================
# NOTAS COM BLOCO GERIDO
#
# A sincronização reescrevia as notas por inteiro: qualquer coisa que o
# estudante escrevesse numa nota de UC desaparecia na sincronização seguinte.
# Agora só o que está entre os marcadores é da sincronização; o resto da nota
# (e as propriedades do frontmatter que não são geradas) é do estudante.
# ==============================================================================

MANAGED_START = "%% moodle-mcp:start — o conteúdo até moodle-mcp:end é substituído em cada sincronização %%"
MANAGED_END = "%% moodle-mcp:end %%"
_START_RE = re.compile(r"^%% moodle-mcp:start\b.*%%[ \t]*$", re.MULTILINE)
_END_RE = re.compile(r"^%% moodle-mcp:end %%[ \t]*$", re.MULTILINE)
_KEY_RE = re.compile(r"^([A-Za-z0-9_][\w.-]*)\s*:")

#: Texto inicial da parte do estudante numa nota nova.
USER_SECTION = "\n%% Escreve à vontade abaixo desta linha: a sincronização não altera esta parte. %%\n"


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    """Separa o frontmatter YAML (linhas, sem os `---`) do resto da nota."""
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---\n", 4)
    if end == -1:
        return [], text
    return text[4:end].split("\n"), text[end + 5 :]


def _frontmatter_blocks(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """Agrupa as linhas por propriedade de topo (a linha da chave e as que a continuam)."""
    blocks: list[tuple[str | None, list[str]]] = []
    for line in lines:
        match = _KEY_RE.match(line)
        if match or not blocks:
            blocks.append((match.group(1) if match else None, [line]))
        else:
            blocks[-1][1].append(line)
    return blocks


def _merge_frontmatter(generated: list[str], existing: list[str]) -> list[str]:
    """Propriedades geradas atualizadas; as que o estudante acrescentou ficam."""
    ours = _frontmatter_blocks(generated)
    our_keys = {key for key, _ in ours if key}
    merged = [line for _, block in ours for line in block]
    for key, block in _frontmatter_blocks(existing):
        if key is None or key not in our_keys:
            merged.extend(block)
    return merged


def _backup(path: Path, text: str) -> Path:
    """Guarda uma cópia de uma nota antes da primeira reescrita com bloco gerido."""
    root = vault_root()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        relative = Path(path.name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = root / ".moodle-mcp" / "backup" / relative.parent / f"{relative.stem}.{stamp}{relative.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(target, text, 0o644)
    return target


def write_managed_note(path: Path, markdown: str) -> Path | None:
    """Escreve uma nota gerada sem apagar o que o estudante lá escreveu.

    O texto gerado (já sem conteúdo ativo do Obsidian) vai entre `MANAGED_START`
    e `MANAGED_END`. Numa nota que já tem os marcadores, só essa parte muda; o
    resto, incluindo propriedades do frontmatter que não são geradas, fica.
    Uma nota que existe mas não tem marcadores (gerada por uma versão anterior,
    eventualmente editada à mão) é primeiro copiada para
    `.moodle-mcp/backup/` dentro do Vault, e só depois reescrita.

    Args:
        path: Caminho da nota, dentro do Vault.
        markdown: O conteúdo gerado, com ou sem frontmatter.

    Returns:
        O caminho da cópia de segurança, se tiver sido feita.

    """
    generated_fm, generated_body = _split_frontmatter(sanitize.neutralize_markdown(markdown))
    block = f"{MANAGED_START}\n{generated_body.strip()}\n{MANAGED_END}\n"
    backup: Path | None = None
    before, after = "", USER_SECTION
    existing_fm: list[str] = []

    if path.exists():
        current = path.read_text(encoding="utf-8")
        existing_fm, existing_body = _split_frontmatter(current)
        start, end = _START_RE.search(existing_body), _END_RE.search(existing_body)
        if start and end and start.start() < end.start():
            before = existing_body[: start.start()]
            after = existing_body[end.end() :].lstrip("\n")
            after = "\n" + after if after else ""
        else:
            backup = _backup(path, current)
            existing_fm = []

    frontmatter = _merge_frontmatter(generated_fm, existing_fm)
    head = "---\n" + "\n".join(frontmatter) + "\n---\n" if frontmatter else ""
    config.atomic_write(path, head + before + block + after, 0o644)
    return backup


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
