# 🎓 Moodle-Obsidian Doctoral MCP & SSO Auto-Token Suite

Servidor **Model Context Protocol (MCP)** em Python (`FastMCP`) desenhado para estudantes de Doutoramento / Ensino Superior em plataformas **Moodle** com **Single Sign-On (SSO)** federado (como a **Universidade Aberta — `elearning.uab.pt`** com credenciais **UTAD / RCTSaai** e **`e-learning.utad.pt`**), integrado diretamente com o **Obsidian**.

---

## ✨ Funcionalidades Principais

1. **Auto-Descoberta de Token Mobile via Browser (`moodlemobile://`):**
   * Mesmo quando a universidade usa SSO federado (Microsoft Entra ID / SAML / Shibboleth / CAS) e oculta a página `/user/managetoken.php` aos estudantes, o capturador nativo (`capture_mobile_token.py` registado via `xdg-mime`) interceta o redirecionamento `moodlemobile://token=...` diretamente do navegador (Brave, Firefox, Chrome), descodifica o Base64, guarda o `wstoken` em `.env` e sincroniza o cofre do Obsidian automaticamente.
2. **Sincronização Completa para o Obsidian (`sync_doctoral_dashboard_to_obsidian`):**
   * Gera um painel central (`Doutoramento - Painel Moodle.md`) com tabela de Unidades Curriculares, progresso, notas atuais e calendário ordenado de prazos de *e-fólios* / trabalhos.
   * Cria uma nota individual por cada Unidade Curricular (`Unidades Curriculares/<Nome da UC>.md`) com grelha de avaliação (`gradereport`), estado de submissão de cada *e-fólio* (`mod_assign`), enunciados convertidos para Markdown e árvore de recursos.
3. **Download de Materiais + Extração de PDFs para Markdown (`PyMuPDF`):**
   * `download_all_course_materials_to_obsidian`: Descarrega automaticamente todos os PDFs, slides, enunciados anexos e páginas HTML de uma cadeira para `Unidades Curriculares/<UC>/Materiais/` e converte cada PDF num ficheiro `.pdf.md` pesquisável no Obsidian e legível por agentes de IA.
4. **32 Ferramentas MCP para Estudantes (Leitura + Escrita):**
   * **Cursos, Livros e Progresso:** `get_enrolled_courses`, `get_course_contents`, `get_course_pages_and_books` (`mod_book`, `mod_page`, `mod_folder`, `mod_resource`), `get_activities_completion_status`, `toggle_activity_completion`, `get_course_participants`.
   * **Notas, Trabalhos e Submissões:** `get_course_grades`, `get_all_grades_overview`, `get_assignments`, `get_assignment_submission`, `get_course_quizzes`, `upload_file_to_draft`, `save_assignment_submission`, `add_submission_comment`.
   * **Fóruns Assíncronos e Mensagens:** `get_course_forums`, `get_forum_discussions`, `get_discussion_posts`, `search_all_course_forums`, `export_forum_discussions_to_obsidian`, `reply_to_forum_post`, `create_forum_discussion`, `send_instant_message`, `get_messages`, `get_calendar_events`.
   * **Suporte Multi-Instância:** Parâmetro `instance="uab"` ou `instance="utad"` em todas as ferramentas.

---

## 🚀 Instalação

O pacote tem um entrypoint (`moodle-mcp`), pelo que a mesma linha serve em Linux,
macOS e Windows e em qualquer cliente MCP.

```bash
uvx --from git+https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp moodle-mcp --show-config
```

Ou a partir de um clone:

```bash
git clone git@github.com:CarlosAndreFerreira/moodle-obsidian-mcp.git
cd moodle-obsidian-mcp
uv pip install -e ".[pdf]"
```

O extra `[pdf]` acrescenta a extração de texto de PDFs via PyMuPDF. Fica de fora
da instalação base porque o PyMuPDF é AGPL-3.0: sem ele, tudo o resto funciona e
os downloads de PDF apenas não geram o `.md` acompanhante.

Para registar o handler `moodlemobile://` em Linux (captura de token via SSO):

```bash
bash install.sh ~/Doutoramento
```

### Configuração

Nada é obrigatório: há omissões para tudo. A configuração é lida por camadas, da
maior precedência para a menor:

1. **Variáveis de ambiente** — é por aqui que os clientes MCP injetam opções
   (`env` no Claude Code, `environment` no opencode).
2. **`config.toml`** no diretório de configuração do utilizador.
3. **`credentials.json`** no mesmo diretório — só tokens, com permissões `0600`.
4. **`.env`** ao lado do código — legado, ainda lido.

O diretório é o do sistema operativo: `~/.config/moodle-mcp` (Linux),
`~/Library/Application Support/moodle-mcp` (macOS),
`%LOCALAPPDATA%\moodle-mcp` (Windows). Não fica junto ao código porque, instalado
com `uvx` ou `pipx`, o pacote corre de uma cache efémera e possivelmente só de
leitura.

```bash
moodle-mcp --show-config      # onde tudo é lido, sem revelar segredos
moodle-mcp --list-toolsets    # que ferramentas cada toolset expõe
```

### Instâncias Moodle

As instâncias são um registo aberto, não uma lista fixa: serve qualquer
universidade que corra Moodle com os Web Services e o serviço da app móvel
ativos. Declaram-se em `config.toml`:

```toml
default_instance = "uab"

[instances.uab]
url = "https://elearning.uab.pt"

[instances.utad]
url = "https://e-learning.utad.pt"
```

ou por ambiente, com `MOODLE_<NOME>_URL` e `MOODLE_<NOME>_TOKEN`:

```json
{
  "env": {
    "MOODLE_UAB_URL": "https://elearning.uab.pt",
    "MOODLE_UTAD_URL": "https://e-learning.utad.pt",
    "MOODLE_DEFAULT_INSTANCE": "uab"
  }
}
```

Os tokens vão para `credentials.json` sob `instances.<nome>.token`, gravados pelo
`set_moodle_token` ou pelo capturador `moodlemobile://`. Cada ferramenta aceita
`instance="<nome>"` e, quando há mais do que uma configurada, o nome aparece como
**enum na schema** — o cliente e o modelo recebem os nomes válidos desta
instalação em vez de texto livre.

`MOODLE_URL` e `MOODLE_TOKEN` sem nome continuam a funcionar e formam a instância
predefinida, mas só enquanto não houver nenhuma instância nomeada: atribuí-los a
uma instância nomeada faria o token de uma universidade ser enviado para o
servidor de outra.

### Toolsets

As 32 ferramentas custam ~8 500 tokens de contexto em cada pedido. Expor só o que
usas corta isso para menos de metade:

| `--tools` | Tools | ~tokens |
|:---|--:|--:|
| (omissão) | 31 | 8 225 |
| `core,obsidian` | 15 | 4 321 |
| `core` | 11 | 2 793 |

`core` é sempre incluído. `raw` (a ferramenta que chama qualquer função da API,
escrita incluída) fica de fora da omissão e tem de ser pedido explicitamente.

### Configuração nos clientes

**Claude Code / Claude Desktop** (`mcpServers`):

```json
{
  "mcpServers": {
    "moodle-uab": {
      "command": "moodle-mcp",
      "args": ["--tools", "core,obsidian"],
      "env": { "MOODLE_URL": "https://elearning.uab.pt" }
    }
  }
}
```

**opencode** (`opencode.json`; chave `mcp`, `command` como array, `environment`):

```json
{
  "mcp": {
    "moodle": {
      "type": "local",
      "command": ["moodle-mcp", "--tools", "core,obsidian"],
      "environment": { "MOODLE_URL": "https://elearning.uab.pt" },
      "enabled": true
    }
  }
}
```

### Autenticação via Browser (1 clique)
1. Faz login normalmente no Moodle da tua universidade no navegador.
2. Abre no mesmo navegador:
   ```text
   https://elearning.uab.pt/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345
   ```
3. Aceita o pop-up **"Abrir Moodle Mobile Token Catcher"** — o token é extraído,
   guardado em `credentials.json` e o Vault é sincronizado de imediato.

---

## 🔒 Segurança e Privacidade
* O ficheiro `.gitignore` bloqueia estritamente `.env`, `*.log` e quaisquer ficheiros de credenciais/tokens.
* O script `capture_mobile_token.py` nunca regista tokens em ficheiros de log.
