# 🎓 Moodle-Obsidian Doctoral MCP & SSO Auto-Token Suite

🇬🇧 [English version](README.md)

Servidor **Model Context Protocol (MCP)** em Python (`FastMCP`) desenhado para estudantes de Doutoramento / Ensino Superior em plataformas **Moodle** com **Single Sign-On (SSO)** federado (como a **Universidade Aberta — `elearning.uab.pt`** com credenciais **UTAD / RCTSaai** e **`e-learning.utad.pt`**), integrado diretamente com o **Obsidian**.

---

## ✨ Funcionalidades Principais

1. **Auto-Descoberta de Token Mobile via Browser (`moodlemobile://`):**
   * Mesmo quando a universidade usa SSO federado (Microsoft Entra ID / SAML / Shibboleth / CAS) e oculta a página `/user/managetoken.php` aos estudantes, o capturador nativo (`capture_mobile_token.py` registado via `xdg-mime`) interceta o redirecionamento `moodlemobile://token=...` diretamente do navegador (Brave, Firefox, Chrome), descodifica o Base64, guarda o `wstoken` em `.env` e sincroniza o cofre do Obsidian automaticamente.
2. **Sincronização Completa para o Obsidian (`sync_doctoral_dashboard_to_obsidian`):**
   * Gera um painel central (`Painel Moodle.md`; um Vault que já tenha `Doutoramento - Painel Moodle.md` continua a usá-lo) com tabela de Unidades Curriculares, progresso, notas atuais e calendário ordenado de prazos de *e-fólios* / trabalhos.
   * Cria uma nota individual por cada Unidade Curricular (`Unidades Curriculares/<Nome da UC>.md`) com grelha de avaliação (`gradereport`), estado de submissão de cada *e-fólio* (`mod_assign`), enunciados convertidos para Markdown e árvore de recursos.
3. **Download de Materiais + Extração de PDFs para Markdown (`PyMuPDF`):**
   * `download_all_course_materials_to_obsidian`: Descarrega automaticamente todos os PDFs, slides, enunciados anexos e páginas HTML de uma cadeira para `Unidades Curriculares/<UC>/Materiais/` e converte cada PDF num ficheiro `.pdf.md` pesquisável no Obsidian e legível por agentes de IA.
4. **33 Ferramentas MCP para Estudantes (Leitura + Escrita):**
   * **Cursos, Livros e Progresso:** `get_enrolled_courses`, `get_course_contents`, `get_course_pages_and_books` (`mod_book`, `mod_page`, `mod_folder`, `mod_resource`), `get_activities_completion_status`, `toggle_activity_completion`, `get_course_participants`.
   * **Notas, Trabalhos e Submissões:** `get_course_grades`, `get_all_grades_overview`, `get_assignments`, `get_assignment_submission`, `get_course_quizzes`, `upload_file_to_draft`, `save_assignment_submission`, `add_submission_comment`.
   * **Fóruns Assíncronos e Mensagens:** `get_course_forums`, `get_forum_discussions`, `get_discussion_posts`, `search_all_course_forums`, `export_forum_discussions_to_obsidian`, `reply_to_forum_post`, `create_forum_discussion`, `send_instant_message`, `get_messages`, `get_calendar_events`.
   * **Suporte Multi-Instância:** Parâmetro `instance="uab"` ou `instance="utad"` em todas as ferramentas.

---

## 🚀 Instalação

O pacote está no PyPI como [`moodle-obsidian-mcp`](https://pypi.org/project/moodle-obsidian-mcp/)
e instala dois comandos, `moodle-mcp` e `moodle-mcp-token-catcher`, em Linux,
macOS e Windows.

```bash
uv tool install "moodle-obsidian-mcp[pdf]"     # ou: pipx install "moodle-obsidian-mcp[pdf]"
moodle-mcp --install-handler                    # uma vez: regista o handler moodlemobile://
moodle-mcp --login                              # login pelo navegador (SSO incluído)
```

Para atualizar: `uv tool upgrade moodle-obsidian-mcp` (ou `pipx upgrade moodle-obsidian-mcp`).

Usa uma instalação permanente (`uv tool` ou `pipx`) e não o `uvx`: o handler de
login fica registado com o caminho do executável, e o `uvx` corre de uma cache
que pode ser apagada. Para experimentar sem instalar:

```bash
uvx --from moodle-obsidian-mcp moodle-mcp --show-config
```

A partir de um clone, `./install.sh [caminho-do-vault]` faz a instalação
permanente, define o Vault, regista o handler e indica o `moodle-mcp --login`.
Para desenvolver: `git clone` e `uv pip install -e ".[dev,pdf]"`.

O extra `[pdf]` acrescenta a extração de texto de PDFs via PyMuPDF. Fica de fora
da instalação base porque o PyMuPDF é AGPL-3.0: sem ele, tudo o resto funciona e
os downloads de PDF apenas não geram o `.md` acompanhante.

Para registar o handler `moodlemobile://`, que é o que permite capturar o token
a partir do navegador quando a universidade usa SSO federado:

```bash
moodle-mcp --install-handler     # Linux, macOS e Windows
moodle-mcp --uninstall-handler
```

O handler retira logo o URL dos seus argumentos de linha de comandos (que em Linux e
macOS qualquer utilizador da máquina vê com `ps`) e passa-o por uma variável de
ambiente, que só o dono do processo consegue ler.

O mecanismo difere por sistema e o comando escolhe o certo: um ficheiro
`.desktop` com `xdg-mime` em Linux, chaves em `HKEY_CURRENT_USER` em Windows
(sem precisar de administrador), e um applet AppleScript com `CFBundleURLTypes`
registado no Launch Services em macOS (é o que recebe o URL por Apple Event). Não precisa de privilégios elevados em
nenhum deles.

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
moodle-mcp --set sync_subdir=Moodle --set instances.utad.url=https://e-learning.utad.pt
moodle-mcp --set language=en  # texto das notas em inglês (omissão: pt)
```

Opções das notas: `language` (`pt`/`en`), `dashboard_name`, `courses_dir`
(omissão `Unidades Curriculares`) e `sync_subdir`. Mudar de língua muda o texto
das notas, não as pastas.

### Sincronização agendada

```bash
moodle-mcp --sync                    # sincroniza já todas as instâncias com token
moodle-mcp --schedule 6              # a cada 6 horas (1–24); --materials descarrega também os ficheiros
moodle-mcp --unschedule
```

Usa o agendador do sistema (timer do systemd, LaunchAgent no macOS, Agendador
de Tarefas no Windows), sem cliente MCP aberto. A tarefa lê o mesmo
`config.toml` e `credentials.json`: nenhum token vai para os ficheiros da
tarefa nem para a linha de comandos. Como o agendador não vê o ambiente do
cliente MCP, grava o Vault no `config.toml` (`moodle-mcp --set
obsidian_vault_dir=…`). O resultado da última execução fica em
`last_sync.log`, no diretório de configuração.

O servidor expõe também os *resources* `moodle://changes`, `moodle://deadlines`
e `moodle://dashboard` (a última sincronização, sem pedidos ao Moodle) e os
*prompts* `weekly_review`, `prepare_assignment` e `forum_catch_up`.

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

As 33 ferramentas custam ~9 700 tokens de contexto em cada pedido. Expor só o
que usas corta isso para menos de metade:

| `--tools` | Tools | ~tokens | Escrita no Moodle |
|:---|--:|--:|:---|
| **(omissão)** | **22** | **~5 500** | **não** |
| `core,grades,forums,obsidian,write` | 29 | ~7 900 | sim |
| `all` | 33 | ~9 700 | sim, mais a API livre |
| `core,obsidian` | 13 | ~3 700 | não |
| `core` | 8 | ~1 750 | não |

`core` é sempre incluído.

**As ferramentas que gravam credenciais (`auth`) também não vêm ligadas.**
`set_moodle_token`, `decode_mobile_launch_token` e
`extract_token_from_session_cookie` permitiriam que um post de fórum com
instruções escondidas trocasse o token guardado. O login faz-se pelo handler
`moodlemobile://` (ver abaixo), que não passa pelo modelo; quem preferir colar o
token no chat ativa-as com `--tools ...,auth`. Mesmo assim, `set_moodle_token`
valida o token antes de o gravar, só aceita URLs `https://` e não muda o URL de
uma instância já configurada.

Os downloads só juntam o token a ficheiros (`pluginfile.php`) da instância
configurada: um URL de outro anfitrião é recusado antes de qualquer pedido.

**As ferramentas que escrevem no Moodle não vêm ligadas.** Submeter um trabalho,
publicar num fórum avaliado ou enviar uma mensagem age em nome do estudante do
lado da universidade, e nenhuma dessas ações se desfaz. Por omissão o servidor
lê do Moodle e escreve apenas no Vault local; para as activar é preciso
`--tools ...,write`, e o servidor registra um aviso no arranque quando estão
ligadas. O mesmo vale para `raw`, que expõe a API de Web Services por inteiro.

Das 22 ferramentas da omissão, 18 estão marcadas como `readOnlyHint`, pelo que
um cliente as pode auto-aprovar sem auto-aprovar nada irreversível.

### Fiabilidade

* **O que mudou:** cada sincronização compara-se com a anterior e escreve uma nota
  «O que mudou no Moodle» (a ferramenta `get_recent_changes` devolve o mesmo, sem
  pedir nada ao Moodle): notas lançadas, prazos alterados, trabalhos e testes
  novos, materiais novos, tópicos de fórum novos ou com respostas novas.
* **Prazos em todo o lado:** um calendário `.ics` ao lado do painel (importável ou
  subscritível no Google Calendar, Outlook, Thunderbird ou telemóvel, com alarme na
  véspera) e uma lista no formato do Obsidian Tasks (`- [ ] … 📅 AAAA-MM-DD`).
* **As tuas notas são tuas.** O conteúdo gerado fica entre `%% moodle-mcp:start %%`
  e `%% moodle-mcp:end %%`; o que escreves fora desse bloco, e as propriedades que
  acrescentas ao frontmatter, sobrevivem a todas as sincronizações. Uma nota de
  uma versão anterior (sem os marcadores) é copiada para `.moodle-mcp/backup/`,
  dentro do Vault, antes de ser reescrita pela primeira vez.
* **Downloads incrementais.** O `timemodified` e o tamanho de cada ficheiro ficam
  em `.moodle-mcp/state-<instância>.json`, no Vault; um ficheiro que não mudou no
  Moodle e ainda está no Vault não volta a ser descarregado.
* **A sincronização não pára por causa de uma UC.** Cada disciplina é
  sincronizada à parte; uma secção que o Moodle recuse (testes, escolhas, notas)
  fica vazia e o problema aparece em `errors`, com `status: "parcial"`. Só um
  token inválido ou em falta pára tudo, com instruções para voltar a fazer login.
* **Erros do Moodle chegam como erros** (`isError` no MCP), com a causa e o que
  fazer, em vez de virem disfarçados de resultado.
* **Nada se sobrescreve por acidente:** ficheiros, páginas, fóruns e UCs com o
  mesmo nome ganham ` (2)`, ` (3)`…; as páginas do Moodle (`index.html`) ficam com
  o nome do módulo. Os nomes são válidos em Windows, macOS e Linux.
* **Falhas transitórias não param nada:** as leituras repetem com backoff em
  429/5xx e falhas de ligação (respeitando `Retry-After`); as escritas nunca
  repetem depois de o servidor as ter recebido, para não publicar duas vezes.
  As ligações são reutilizadas (keep-alive).
* **O servidor não bloqueia:** sincronização, download de materiais e exportação
  de fóruns correm numa thread e reportam progresso por UC ao cliente.
* **Organização do Vault:** `MOODLE_MCP_SYNC_SUBDIR` põe o painel e
  `Unidades Curriculares/` numa subpasta (as ligações acompanham), e
  `MOODLE_MCP_DASHBOARD_NAME` muda o nome do painel. Com várias instâncias, cada
  uma tem o seu painel (`<nome> (<instância>)`).
* **Downloads atómicos e com limite:** um download que falhe deixa o ficheiro
  anterior intacto, e nenhum ficheiro passa de `MOODLE_MCP_MAX_DOWNLOAD_MB` (500).
* **Auditoria:** cada ação no Moodle fica registada em `audit.log` (0600, no
  diretório de configuração) com a decisão — confirmada, recusada ou executada
  sem confirmação —, sem tokens e com rotação a 1 MB.
* **Menos dados pessoais:** `get_course_participants` devolve só id, nome, papéis
  e grupos; emails e dados de perfil dos colegas não vão para o fornecedor do LLM.
* **Segredos:** `credentials.json` e `last_auth.log` são escritos de forma
  atómica e já com `0600`; um `credentials.json` ilegível é guardado à parte em
  vez de ser apagado; o token gravado pelo login tem precedência sobre um token
  antigo no `.env`. `moodle-mcp --show-config` (e o arranque) avisam de ficheiros
  legíveis por outros utilizadores.

### Proteção contra prompt injection

Posts de fórum, páginas e descrições são escritos por qualquer participante da
disciplina e chegam ao modelo. Nenhum modelo resiste de forma fiável a instruções
escondidas nesse conteúdo (OWASP LLM01), por isso as defesas são do lado do
servidor e não dependem do modelo:

* **Conteúdo invisível é retirado** de todas as respostas do Moodle: texto em
  elementos escondidos (`display:none`, `font-size:0`, `hidden`, comentários) e
  caracteres invisíveis (bloco Unicode «tags», largura zero, controlos
  bidirecionais). O modelo passa a ler o mesmo que o estudante vê.
* **As notas escritas no Vault não executam nada:** blocos `dataviewjs`/`dataview`,
  expressões inline do Dataview e comandos do Templater ficam desativados, e
  imagens remotas passam a links (não carregam sozinhas, logo não rastreiam).
* **Cada ação no Moodle pede confirmação ao utilizador**, pelo servidor
  (elicitation do MCP), com o destinatário e o texto exatos — também quando o
  cliente está configurado para aprovar tudo. Recusar, ou um cliente sem
  elicitation, bloqueia a ação.

O comportamento da confirmação escolhe-se com `MOODLE_MCP_CONFIRM_WRITES`:

| Valor | Quando pede confirmação |
|:---|:---|
| `always` (omissão) | antes de qualquer ação no Moodle |
| `untrusted` | só depois de a sessão ter lido conteúdo de terceiros (posts, páginas, mensagens) |
| `client` | nunca; depende só da aprovação do cliente MCP (para clientes sem elicitation — não recomendado) |

Limites: um cliente que responda sozinho aos pedidos de confirmação anula a
última defesa, e texto malicioso visível continua a chegar ao modelo. Por isso as
ferramentas de escrita continuam desligadas por omissão.

### Configuração nos clientes

**Plugin do Claude Code.** O repositório também é um *marketplace* de plugins. O plugin `moodle-obsidian` arranca o servidor com `uvx`, pede a pasta do Vault e a língua das notas, e acrescenta uma *skill* de revisão semanal:

```text
/plugin install moodle-obsidian --marketplace CarlosAndreFerreira/moodle-obsidian-mcp
```

O login no Moodle continua a ser feito uma vez com `moodle-mcp --login`.


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
2. Corre:
   ```bash
   moodle-mcp --login            # ou: moodle-mcp --login utad
   ```
   O comando abre no navegador um link de login com um *passport* aleatório,
   de uso único e válido durante 15 minutos (`--no-browser` só o mostra).
3. Aceita o pop-up **"Abrir Moodle Token Catcher"** — o token é verificado,
   guardado em `credentials.json` na instância certa e o Vault é sincronizado
   de imediato. O resultado fica em `last_auth.log`, no diretório de configuração.

O handler só aceita a resposta a um login iniciado assim: o Moodle assina-a
com `md5(wwwroot . passport)`, e o handler confirma essa assinatura. O link
antigo com `passport=12345` deixou de funcionar, porque permitia a qualquer
página web substituir o teu token pelo de outra conta.

---

## 📄 Licença

MIT — ver [LICENSE](LICENSE). O extra `[pdf]` instala o PyMuPDF, que é
**AGPL-3.0**: se o usares, essa dependência traz as suas próprias obrigações.
Sem o extra, o servidor funciona e apenas não gera o `.md` acompanhante dos PDFs.

## 🔒 Segurança e Privacidade

Para reportar uma vulnerabilidade, ver [SECURITY.md](SECURITY.md) (canal privado,
nunca issues públicas). As mudanças de cada versão estão no [CHANGELOG](CHANGELOG.md).

* O ficheiro `.gitignore` bloqueia estritamente `.env`, `*.log` e quaisquer ficheiros de credenciais/tokens.
* O capturador `moodlemobile://` nunca escreve tokens nos registos (`last_auth.log`, `audit.log`).
