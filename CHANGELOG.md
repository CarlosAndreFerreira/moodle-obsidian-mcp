# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-PT/1.1.0/);
o projeto segue [versionamento semântico](https://semver.org/lang/pt/) (antes da
1.0, uma versão menor pode mudar comportamento).

## [Por publicar]

### Adicionado
- **O que mudou:** cada sincronização guarda um retrato de cada UC (notas,
  trabalhos e estado da submissão, módulos, tópicos dos fóruns, testes) e
  compara-o com o anterior. O resultado vai para a nota «O que mudou no Moodle»
  e para a ferramenta nova `get_recent_changes` (só de leitura, não faz pedidos
  ao Moodle; no toolset `obsidian`). A sincronização devolve `changes_count`.
- **Calendário `.ics`** com os prazos (trabalhos, fóruns avaliados, testes) ao
  lado do painel, com alarme na véspera (RFC 5545: escapes, dobragem a 75
  octetos, CRLF).
- **Tarefas do Obsidian Tasks** no painel (`- [ ] … 📅 AAAA-MM-DD`), marcadas
  como feitas quando o trabalho está entregue.
- **Notas com bloco gerido:** o conteúdo gerado fica entre os marcadores
  `%% moodle-mcp:start %%` e `%% moodle-mcp:end %%` (comentários do Obsidian,
  invisíveis na leitura). O que o estudante escreve fora do bloco, e as
  propriedades que acrescenta ao frontmatter, sobrevivem às sincronizações.
  Aplica-se às notas das UCs, ao painel e aos fóruns exportados. Uma nota sem
  marcadores, de uma versão anterior, é copiada para `.moodle-mcp/backup/`
  antes da primeira reescrita.
- **Downloads incrementais:** os materiais que não mudaram no Moodle
  (`timemodified` e tamanho iguais) e ainda estão no Vault não voltam a ser
  descarregados. O estado fica em `.moodle-mcp/state-<instância>.json`, no
  Vault. O resultado ganha `unchanged_count`.

### Segurança
- O estado «esta sessão já leu conteúdo de terceiros», usado pelo modo
  `MOODLE_MCP_CONFIRM_WRITES=untrusted`, passa a ser por sessão MCP e não por
  processo: os posts lidos por um cliente não obrigam outro cliente do mesmo
  servidor a confirmar ações, e vice-versa.

### Alterado
- O extra `[pdf]` fica limitado a `pymupdf<2`. A 2.x passou a depender do
  `onnxruntime`, do `numpy` e do `pymupdf4llm`, que o projeto não usa: a
  instalação desce de cerca de 280 MB para cerca de 90 MB. A série 1.x continua
  mantida.

## [0.7.2] — 2026-11-08

### Segurança
- O handler `moodlemobile://` recebia o URL, que contém o token, como argumento
  de linha de comandos e ficava vivo durante toda a sincronização. Em Linux e
  macOS os argumentos de qualquer processo são visíveis a todos os utilizadores
  da máquina (`ps`, `/proc/<pid>/cmdline`). O handler substitui-se agora a si
  próprio (`execve`) logo no arranque, sem o URL nos argumentos, e passa-o por
  uma variável de ambiente, que só o dono do processo consegue ler. Os imports
  pesados só acontecem depois dessa troca. **Quem fez login com uma versão
  anterior numa máquina partilhada deve revogar o token e voltar a fazer login.**

### Corrigido
- Uma segunda chamada do navegador ao handler para o mesmo login deixa de
  escrever um erro no `last_auth.log` por cima do resultado bom.

## [0.7.1] — 2026-11-08

### Alterado
- Documentação: instalação a partir do PyPI (`uv tool install "moodle-obsidian-mcp[pdf]"`),
  atualização com `uv tool upgrade`, e links absolutos para o GitHub (os relativos
  não funcionavam na página do PyPI).
- Metadados do pacote: descrição em inglês e sem referências a universidades
  específicas, classifiers, keywords e links para o repositório, issues,
  changelog e política de segurança.

## [0.7.0] — 2026-11-08

### Adicionado
- `moodle-mcp --set CHAVE=VALOR` grava opções no `config.toml`, incluindo
  `instances.<nome>.url` (só `https`).

### Alterado
- Todas as ferramentas síncronas correm numa thread quando chamadas por um
  cliente MCP: um Moodle lento já não deixa o servidor sem responder.
- `install.sh` faz uma instalação permanente com `uv tool install` (ou `pipx`)
  em vez de um venv editável no repositório, só define o Vault se for indicado,
  e termina com o `moodle-mcp --login`.

### Corrigido
- `extract_token_from_session_cookie` experimenta primeiro o token do serviço da
  app móvel e só grava um token que o Moodle aceite (antes usava o primeiro
  hexadecimal da página).
- Links com nomes vindos do Moodle já não partem com `]`, `)` ou espaços.

### Segurança
- `set_moodle_token` só cria uma instância nova numa instalação sem nenhuma
  configurada. Com `auth` e `write` ligados, uma injeção já não consegue
  registar um servidor seu para lhe enviar ficheiros do Vault.

## [0.6.0] — 2026-11-07

### Adicionado
- Registo de auditoria (`audit.log`, 0600) de cada ação no Moodle, com a decisão:
  confirmada, recusada ou executada sem confirmação.
- `SECURITY.md` com o canal privado para reportar vulnerabilidades.
- Workflow de release para o PyPI com *trusted publishing* e atestações.
- Testes de propriedades (Hypothesis) para nomes de ficheiro, validação de URLs
  e limpeza de conteúdo; `mypy` em modo estrito nos módulos de base.

### Alterado
- O pacote passa a chamar-se `moodle-obsidian-mcp` (o nome `moodle-mcp` está
  ocupado no PyPI). Os comandos, o módulo Python e a configuração não mudam.
- `server.py` dividido: `transport.py` (HTTP), `vault.py` (ficheiros e notas) e
  `confirm.py` (confirmação humana).
- `get_course_participants` devolve só id, nome, papéis e grupos (sem emails nem
  dados de perfil dos colegas).

### Segurança
- O sdist passa a ter uma lista explícita de ficheiros: um `uv build` local já
  não inclui notas locais nem caches.

## [0.5.0] — 2026-11-07

### Adicionado
- `moodle-mcp --login`: login pelo navegador com *passport* aleatório e de uso
  único; o handler só aceita a resposta assinada `md5(wwwroot . passport)`.
- `MOODLE_MCP_SYNC_SUBDIR` e `MOODLE_MCP_DASHBOARD_NAME`; um painel por instância.
- Progresso por UC durante a sincronização.

### Alterado
- Sincronização, download de materiais e exportação de fóruns correm numa thread
  (antes bloqueavam o servidor).
- Sessões HTTP partilhadas, `User-Agent` próprio e novas tentativas para leituras.
- macOS: o handler é um applet AppleScript que recebe o URL por Apple Event.

### Removido
- O link fixo `passport=12345`.

### Segurança
- Fecha a troca do token por qualquer página web (CSRF de login) e a gravação do
  token na instância errada.

## [0.4.0] — 2026-11-07

### Adicionado
- CI: ruff, mypy, pytest (Linux, Windows, macOS; Python 3.11–3.13) e `pip-audit`.
- `MOODLE_MCP_MAX_DOWNLOAD_MB` e avisos de ficheiros de segredos legíveis.

### Alterado
- Erros do Moodle chegam como erros (`isError`), com a causa e o passo seguinte.
- A sincronização isola cada UC e reporta `status: "parcial"` e `errors`.
- Nomes de ficheiro únicos e válidos em todos os sistemas; páginas com o nome do módulo.

### Corrigido
- O token do login tem precedência sobre um token antigo no `.env` legado.
- Escrita atómica, com 0600, de credenciais e registos; downloads atómicos.

## [0.3.0] — 2026-11-07

### Adicionado
- Confirmação humana, pedida pelo servidor (elicitation), de cada ação no Moodle.
- Limpeza de conteúdo escondido e caracteres invisíveis nas respostas do Moodle.
- Desativação de conteúdo ativo do Obsidian (Dataview, Templater, imagens remotas).

## [0.2.1] — 2026-11-07

### Segurança
- O token só é enviado para ficheiros da instância configurada.
- O token deixa de aparecer no texto das exceções.
- `set_moodle_token` valida antes de gravar e não muda o URL de uma instância.
- As ferramentas que gravam credenciais (`auth`) ficam desligadas por omissão.

[0.7.2]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.2.0...v0.2.1
