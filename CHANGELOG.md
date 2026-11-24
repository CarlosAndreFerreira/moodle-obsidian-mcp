# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-PT/1.1.0/);
o projeto segue [versionamento semântico](https://semver.org/lang/pt/) (antes da
1.0, uma versão menor pode mudar comportamento).

## [0.8.7] — 2026-11-24

### Alterado
- **Capítulos dos livros do Moodle com o nome do capítulo.** Cada capítulo de um
  livro (`mod_book`) chega como um `index.html`, e as notas ficavam todas com o
  nome do livro: `Contrato de Aprendizagem (2).md` … `(11).md`. Passam a ficar
  numa pasta com o nome do livro, numeradas pela ordem do livro e com o título
  de cada capítulo (`Contrato de Aprendizagem/02 Objetivos.md`; subcapítulos
  como `03.1 …`), e a nota começa pelo título do capítulo e pelo nome do livro.
  Os títulos e a ordem vêm do item `structure` que o Moodle envia com o livro;
  se não for legível, os capítulos ficam com os nomes de antes.
- Uma nota de capítulo que o servidor já tinha registado com o nome antigo é
  movida para o nome novo, com o que o estudante lá escreveu, em vez de ficar
  uma cópia órfã. As imagens dos capítulos ficam onde sempre ficaram, para não
  duplicar os materiais já descarregados.

## [0.8.6] — 2026-11-24

### Corrigido
- **A 0.8.5 duplicava os materiais descarregados por versões antigas.** Os
  materiais gravados antes de haver estado (antes da 0.8.0) não têm registo, e
  a regra de nunca substituir o que não é deste URL tratava cada um como do
  estudante: o download seguinte gravava `aula (2).pdf` ao lado de cada ficheiro.
  Num Vault real com 159 materiais antigos, 87 ficariam com uma segunda cópia.
  Um material sem registo passa a ser reconhecido quando:
  - é um ficheiro com exatamente o tamanho que o Moodle indica (fica igual e
    não é descarregado outra vez); procura-se também entre as cópias numeradas
    da mesma pasta (`image.png`, `image (2).png`…), porque as versões antigas
    numeravam os nomes repetidos por outra ordem;
  - é uma nota de página que começa pelo título que o servidor escreve, ou que
    já tem o bloco gerido (é reescrita no mesmo sítio, com backup).

  Na dúvida (sem tamanho do Moodle, tamanho diferente, outra nota com o mesmo
  nome), não reconhece, e a cópia vai para o lado. No mesmo Vault, todos os
  materiais que existiam passaram a ser reconhecidos, e não houve nenhuma cópia.
- Um material já guardado numa pasta é reaproveitado mesmo quando a numeração
  dos nomes repetidos muda entre sincronizações, em vez de ganhar outra cópia.

## [0.8.5] — 2026-11-24

### Corrigido
- **Notas do estudante apagadas ao descarregar outra vez.** O texto extraído
  de um PDF (`<ficheiro>.pdf.md`) e as notas das páginas HTML eram reescritos
  por inteiro sempre que o material mudava no Moodle: o que o estudante lá
  escrevesse perdia-se. Passam a ser notas com bloco gerido, como as das UCs
  e dos fóruns: só o bloco muda, o resto da nota fica, e uma nota antiga sem
  marcadores vai primeiro para `.moodle-mcp/backup/`.
- **`download_all_course_materials_to_obsidian` substituía ficheiros do
  estudante com o mesmo nome.** A regra do `download_moodle_file` (só se
  substitui o que o mesmo URL lá pôs; senão o novo fica com ` (2)`) passa a
  valer também nos materiais da UC e na sincronização. Nas sincronizações
  seguintes, a cópia ao lado é atualizada no mesmo sítio, sem criar uma nova.
  O estado guarda agora todos os sítios onde cada URL foi gravado (`paths`),
  para que o mesmo ficheiro em dois módulos continue a ser reconhecido. Sem
  estado (p.ex. apagado), fica-se pelo lado seguro e grava-se ao lado.

### Alterado
- **`destructiveHint=True` em `download_moodle_file`,
  `download_all_course_materials_to_obsidian` e
  `sync_doctoral_dashboard_to_obsidian`.** Substituem a versão anterior de um
  material pela nova, e a anterior pode já não existir no Moodle; pela
  especificação MCP, `destructiveHint=false` quer dizer que a ferramenta só
  faz atualizações aditivas. O `export_forum_discussions_to_obsidian` fica com
  `false`: só regenera o bloco gerido das notas. Os clientes MCP podem passar
  a pedir confirmação antes destas três ferramentas.

### Testes
- `tests/test_download_safety.py`: ficheiros e notas do estudante com o mesmo
  nome, atualização da cópia ao lado sem duplicar, o mesmo ficheiro em dois
  módulos, texto do estudante em notas de páginas e de PDFs, backup de uma
  nota sem marcadores, e o novo registo de caminhos no estado.
- `tests/test_annotations.py`: o conjunto exato de ferramentas destrutivas.

## [0.8.4] — 2026-11-24

### Corrigido
- **Lock do estado ignorado no Windows sob contenção.** Criar o ficheiro de
  lock no instante em que o dono o está a apagar dá `PermissionError` no
  Windows (o nome só desaparece quando o último handle fecha), e o
  `state.locked` tratava isso como erro definitivo e seguia sem lock: uma
  sincronização agendada e outra pedida no cliente podiam perder um registo do
  estado uma da outra. Num teste com 4 threads a criar e apagar o mesmo
  ficheiro, cerca de 5% das tentativas davam `PermissionError`. Passa a contar
  como contenção (espera e tenta de novo, até `LOCK_WAIT_SECONDS`); em POSIX
  continua a desistir logo. Libertar o lock também insiste enquanto o Windows
  der o ficheiro como em uso, em vez de o deixar preso até ficar abandonado.
  Apareceu como falha intermitente de `test_lock_serialises_read_modify_write`
  no CI (`windows-latest`).
- **`get_recent_changes` declarava `openWorldHint=True`.** A ferramenta não faz
  pedidos ao Moodle: lê o resultado guardado no Vault pela última
  sincronização. Passa a declarar `openWorldHint=False`. Era a única hint que
  não correspondia ao comportamento.
- **Descrição do `get_messages`.** Dizia devolver "notificações e mensagens",
  mas pede só `type=notifications`. A descrição diz agora o que devolve e que
  não marca nada como lido.

### Alterado
- **As quatro hints de cada ferramenta ficam escritas por extenso.** Cada
  `@mcp.tool` passa a ter um `ToolAnnotations(...)` com `readOnlyHint`,
  `destructiveHint`, `idempotentHint` e `openWorldHint` explícitos, em vez de
  uma função auxiliar com valores por omissão. O que os clientes recebiam em
  `tools/list` não muda (já vinham as quatro em todas as ferramentas), mas um
  scanner estático ou quem revê o código lia só os argumentos escritos e dava
  5 ferramentas como "sem anotações" e as 32 como incompletas.

### Testes
- `tests/test_annotations.py`: todas as ferramentas listadas têm título e as
  quatro hints em booleano; as hints estão escritas literalmente no código;
  os toolsets de leitura só têm ferramentas `readOnlyHint=True`; nenhuma
  ferramenta que escreve se declara só de leitura; as ações que criam
  duplicados não se declaram idempotentes; `openWorldHint` é `False` só para
  o que lê apenas o Vault.
- `tests/test_read_tools.py`: a função do Moodle e os parâmetros enviados por
  `get_course_pages_and_books`, `get_activities_completion_status`,
  `get_all_grades_overview`, `get_forum_discussions`,
  `search_all_course_forums` (incluindo um fórum que falha),
  `get_calendar_events` e `get_messages` (que não envia `markasread`). Todas
  as 33 ferramentas ficam referenciadas nos testes.

## [0.8.3] — 2026-11-16

### Segurança
- **Escrita em pastas de configuração do Vault.** O `download_moodle_file`
  só verificava que o destino ficava dentro do Vault, e aceitava
  `.obsidian/plugins/…`, `.obsidian/community-plugins.json`,
  `.claude/settings.json`, `.git/hooks/…` e `.moodle-mcp/`. Um anexo de fórum
  (um `pluginfile.php` válido, que qualquer participante pode carregar) e
  instruções escondidas num post podiam levar o modelo a instalar um plugin do
  Obsidian ou um hook do Claude Code, ou seja, a executar código na máquina do
  estudante. É a classe de falha do CVE-2025-53773 (Copilot) e do
  CVE-2025-54135 (Cursor). Reportado em privado; obrigado.
  - Todo o destino escolhido pelo modelo (`dest_path`, `vault_dir`) ou composto
    a partir de nomes do Moodle passa por `vault.resolve_visible`, que recusa
    qualquer componente escondido. O `upload_file_to_draft` também recusa
    enviar ficheiros escondidos para o Moodle.
  - `safe_filename` retira pontos iniciais: uma secção ou UC chamada
    `.obsidian` já não cria uma pasta escondida.
  - Um download só substitui um ficheiro que o mesmo URL descarregou para o
    mesmo sítio; senão, o novo fica com ` (2)` no nome (`saved_as` no
    resultado). Antes, uma nota do estudante era substituída sem aviso, e um
    anexo de fórum podia tomar o lugar do PDF de um professor, que a
    sincronização incremental depois já não voltava a descarregar.
  - Só as extensões de material de estudo se mantêm; as outras (`.js`, `.json`,
    `.css`, `.sh`, e também `.md`, `.canvas` e `.base`, que em bruto não passam
    pela neutralização de `dataviewjs`/Templater) ficam com `.txt` acrescentado,
    com o conteúdo intacto.
- **Estado do Vault escrito fora do Vault (desde a 0.8.0).** O nome da
  instância, que o modelo escolhe e que com uma só instância é texto livre,
  entrava sem validação no nome do ficheiro de estado:
  `t/../../../.claude/settings` gravava em `~/.claude/settings.json`. O nome
  passa a ser validado (`[a-z0-9][a-z0-9_-]*`) antes de qualquer escrita.

## [0.8.2] — 2026-11-08

### Adicionado
- **Plugin do Claude Code** (`plugin/`) e `marketplace.json` na raiz:
  `/plugin install moodle-obsidian --marketplace
  CarlosAndreFerreira/moodle-obsidian-mcp`. Arranca o servidor com
  `uvx` numa versão exata, como o diretório da Anthropic exige, pede o Vault
  e a língua, aceita um token opcional guardado como segredo e traz a *skill* `moodle-weekly-review`. Preparado para a
  submissão ao diretório do Claude; os testes obrigam a versão do plugin a
  acompanhar a do pacote.
- Ícone do plugin (`plugin/.claude-plugin/icon.png`), desenho original.

### Corrigido
- O plugin passa a validação do diretório do Claude: arranca o servidor com
  `uvx moodle-obsidian-mcp==<versão>` (o `[pdf]` era lido como um padrão de
  glob e bloqueava), a opção da língua deixa de ter `options`, e as opções
  chegam ao servidor como `CLAUDE_PLUGIN_OPTION_<CHAVE>`, os únicos nomes que o
  diretório aceita. O servidor traduz esses nomes para `OBSIDIAN_VAULT_DIR`,
  `MOODLE_MCP_LANGUAGE`, `MOODLE_URL` e `MOODLE_TOKEN`, sem se sobrepor a uma
  variável já definida. Sem o extra `[pdf]`, o plugin guarda os PDFs mas não os
  converte em notas.

## [0.8.1] — 2026-11-08

### Adicionado
- `server.json` para o [MCP Registry](https://registry.modelcontextprotocol.io)
  (`io.github.CarlosAndreFerreira/moodle-obsidian-mcp`) e a marca `mcp-name`
  no README, que o Registry usa para confirmar que o pacote do PyPI é deste
  repositório.
- Comando `moodle-obsidian-mcp`, igual a `moodle-mcp`: os clientes do Registry
  correm `uvx moodle-obsidian-mcp`, que antes falhava por não haver um comando
  com o nome do pacote.

## [0.8.0] — 2026-11-08

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
- **Sincronização agendada:** `moodle-mcp --sync` sincroniza sem cliente MCP
  (todas as instâncias com token, ou a indicada; `--materials` para descarregar
  também os materiais) e escreve `last_sync.log`. `moodle-mcp --schedule
  [HORAS]` regista-o no agendador do sistema: timer do systemd de utilizador
  (`Persistent=true`), LaunchAgent no macOS ou tarefa do Agendador de Tarefas
  no Windows. `--unschedule` remove-o. Nenhum token entra nos ficheiros da
  tarefa nem na linha de comandos.
- **Notas em inglês ou português:** opção `language` (`MOODLE_MCP_LANGUAGE`,
  `pt` por omissão). Muda o texto do painel, das notas das UCs, de «O que
  mudou», das tarefas, do calendário e dos prompts. As pastas e as chaves
  internas (tipo de mudança, UID dos eventos) não mudam, para que mudar de
  língua não volte a descarregar materiais nem duplique eventos.
- Opção `courses_dir` (`MOODLE_MCP_COURSES_DIR`) para a pasta das notas das UCs.
- **Resources MCP:** `moodle://changes`, `moodle://deadlines` e
  `moodle://dashboard` (e `…/{instance}`), com o resultado da última
  sincronização, sem pedidos ao Moodle. Cada leitura conta como conteúdo de
  terceiros para o modo `confirm_writes=untrusted`.
- **Prompts MCP:** `weekly_review`, `prepare_assignment` e `forum_catch_up`.
  Não levam conteúdo do Moodle (chegariam ao modelo com a autoridade do
  utilizador): só os passos, que leem esse conteúdo pelas ferramentas.
- **Respostas estruturadas:** a sincronização, `get_recent_changes`, o
  download de materiais e a exportação de fóruns declaram um `outputSchema`.
  Um erro do Moodle nestas ferramentas passa a ser um erro MCP (`isError`),
  com os segredos retirados.

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
- O painel deixa de se chamar «Doutoramento - Painel Moodle» por omissão: um
  Vault que já tenha essa nota continua a usá-la; um Vault novo recebe «Painel
  Moodle» (ou «Moodle Dashboard» em inglês). `dashboard_name` continua a
  mandar.
- Um lock entre processos protege a gravação do estado do Vault: uma
  sincronização agendada e outra pedida no cliente ao mesmo tempo já não
  apagam o registo uma da outra.

### Corrigido
- Um teste do handler podia falhar ao ler o `/proc` do processo entre o
  `fork` e o `exec`.

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

[0.8.7]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.6...v0.8.7
[0.8.6]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.5...v0.8.6
[0.8.5]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.4...v0.8.5
[0.8.4]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/compare/v0.2.0...v0.2.1
