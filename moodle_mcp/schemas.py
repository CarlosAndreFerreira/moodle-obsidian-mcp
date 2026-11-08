"""Esquemas das respostas estruturadas das ferramentas principais.

O MCP permite que uma ferramenta declare um `outputSchema` e devolva
`structuredContent` validado contra ele. Um cliente (ou um agente que encadeia
ferramentas) passa a saber de antemão que campos vêm na resposta, em vez de
interpretar JSON livre.

Só as ferramentas do fluxo do Vault têm esquema próprio: cada esquema é
enviado em cada `tools/list` e custa contexto, e as restantes devolvem a
resposta do Moodle tal como vem, cuja forma depende da versão do Moodle.

`extra="allow"` deixa passar campos acrescentados no futuro sem quebrar
clientes antigos.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class ItemError(_Base):
    """Um passo que falhou sem parar a operação."""

    item: str = Field(default="", description="O que falhou (UC, secção, ficheiro).")
    error: str = Field(default="", description="A mensagem de erro.")


class FileError(_Base):
    """Um ficheiro que não foi possível descarregar."""

    file: str = ""
    error: str = ""


class SyncResult(_Base):
    """Resumo de `sync_doctoral_dashboard_to_obsidian`."""

    status: str = Field(description="`ok`, ou `parcial` se alguma UC ou secção falhou.")
    student: str
    courses_synced: int
    courses_total: int
    files_written: list[str] = Field(description="Caminhos escritos: painel, «O que mudou», UCs, calendário.")
    changes_count: int = Field(description="Mudanças desde a sincronização anterior (`get_recent_changes`).")
    errors: list[ItemError] = []


class Change(_Base):
    """Uma mudança detetada entre duas sincronizações."""

    course: str
    kind: str = Field(
        description="nota | trabalho | prazo | submissão | material | fórum | teste | uc (chaves estáveis)."
    )
    text: str


class RecentChanges(_Base):
    """Resultado de `get_recent_changes`."""

    changes: list[Change] = []
    since: str | None = Field(default=None, description="Data da sincronização anterior.")
    at: str | None = Field(default=None, description="Data da última sincronização.")
    first_sync: bool | None = Field(default=None, description="Se ainda não havia com que comparar.")
    message: str | None = None


class DownloadResult(_Base):
    """Resumo de `download_all_course_materials_to_obsidian`."""

    course_id: int
    course_name: str
    materials_dir: str
    downloaded_count: int
    downloaded_files: list[str] = []
    unchanged_count: int = Field(default=0, description="Ficheiros iguais, não descarregados.")
    errors: list[FileError] = []


class ExportResult(_Base):
    """Resumo de `export_forum_discussions_to_obsidian`."""

    course_id: int
    course_name: str
    forums_exported: int
    files: list[str] = []
