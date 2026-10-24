# Análise técnica — moodle-mcp

Data: 2026-10-24 · Alvo: `main.py` (1159 linhas, 47 KB), `capture_mobile_token.py`, `install.sh`

## Medições (não são estimativas)

Ambiente verificado no `.venv` do repo:

| Item | Valor |
|:---|:---|
| `mcp` SDK instalado | **1.30.0** (`requirements.txt` permite `>=1.0.0`) |
| Python | 3.12.13 |
| Ferramentas expostas | **32** |
| Tamanho das schemas (JSON) | **18 699 caracteres ≈ 5 300 tokens** |
| Parâmetros totais | **88** |
| Parâmetros **com** descrição | **0 de 88** |
| Parâmetros com `anyOf: [T, null]` | **9** |
| Ferramentas com `annotations` (readOnlyHint…) | **0 de 32** |
| Ferramentas com `outputSchema` | 10 de 32 |

O SDK 1.30.0 já suporta tudo o que é proposto abaixo:
`FastMCP.tool(name, title, description, annotations, icons, meta, structured_output)`,
`ToolAnnotations(title, readOnlyHint, destructiveHint, idempotentHint, openWorldHint)`,
`Context.report_progress / info / warning / error / elicit`,
`mcp.run(transport="stdio"|"sse"|"streamable-http")`.
Nada disto exige subir a versão mínima — basta usar.

Confirmado também que `@mcp.tool()` devolve a função original (`inspect.isfunction` → `True`),
pelo que o `capture_mobile_token.py` funciona hoje. É um detalhe de implementação, não contrato.

---

## P0 — Segurança — ✅ IMPLEMENTADO em 2026-10-24

### 1. Credenciais no canal MCP
As ferramentas de autenticação recebiam e devolviam material secreto. Tudo o que
entra ou sai de uma ferramenta vai no payload para o provider de LLM configurado no
cliente e fica no histórico da sessão em disco — e com o opencode o provider é à
escolha do utilizador.

**Proposta:** nunca devolver material secreto; devolver uma impressão digital não
reversível. Mover a captura de token para fora do canal MCP, ou usar `ctx.elicit()`
para que o valor venha do cliente e não do contexto do modelo.

### 2. Escrita de ficheiros sem fronteira definida
Os caminhos de destino eram aceites sem validação, e os nomes de ficheiro vêm do
servidor Moodle.

**Proposta:** confinar toda a leitura e escrita abaixo de `OBSIDIAN_VAULT_DIR`,
validando com `is_relative_to()` depois de `resolve()`.

### 3. `call_any_moodle_wsfunction` é acesso total à API
Permite qualquer `wsfunction` com qualquer parâmetro — inclui escrita. É uma boa
ferramenta de diagnóstico e uma má ferramenta para estar sempre ligada.

**Proposta:** desligada por omissão, ativada explicitamente.

### 4. Falta um modo leitura-apenas
`save_assignment_submission`, `reply_to_forum_post`, `create_forum_discussion`,
`send_instant_message` e `toggle_activity_completion` são irreversíveis do lado da
universidade. Submeter um e-fólio por erro de interpretação do modelo não tem `undo`.

**Proposta:** um modo leitura-apenas e `annotations` em todas as ferramentas — ver
P2, que é o que permite ao cliente distinguir o que pode auto-aprovar do que exige
confirmação humana.

---

## P1 — Setup multiplataforma e instalação

### 5. Não há `pyproject.toml` nem ponto de entrada — ✅ FEITO
Hoje a configuração de qualquer cliente tem de conter o caminho absoluto
`<repo>/.venv/bin/python`. Isso não é portável para
outra máquina, outro utilizador, nem para Windows (`.venv\Scripts\python.exe`).

**Proposta:** `pyproject.toml` com

```toml
[project.scripts]
moodle-mcp = "moodle_mcp.server:main"
```

o que permite `uvx --from git+https://github.com/…/moodle-mcp moodle-mcp` e,
depois de publicar no PyPI, `uvx moodle-mcp` — uma linha, igual nos três sistemas
operativos, sem clonar nem gerir venv. É o caminho padrão de instalação de servidores
MCP em Python. Acrescentar `main()` com `argparse` (`--transport`, `--readonly`,
`--version`, `--tools`), que hoje não existe: o ficheiro termina em
`mcp.run(transport="stdio")` sem CLI.

### 6. Configuração presa ao diretório de instalação — ✅ FEITO
`load_dotenv(Path(__file__).parent/".env")` e `_update_env_var` escrevem o `.env`
dentro do repo. Com `uvx` o pacote corre de uma cache efémera e possivelmente só de
leitura — o `set_moodle_token` falha ou escreve num local que desaparece.

**Proposta:** `platformdirs` →
`~/.config/moodle-mcp/config.toml` (Linux), `~/Library/Application Support/` (macOS),
`%APPDATA%\moodle-mcp\` (Windows). Precedência: variáveis de ambiente > ficheiro de
config > omissões. Mantém compatibilidade com o `env:` que os clientes injectam.
Melhor ainda: token no `keyring` do SO, com fallback para ficheiro. Resolve de passagem
o `chmod 600` do `install.sh`, que **não faz nada no Windows**.

### 7. `install.sh` é Linux-only
`bash`, `xdg-mime`, `.desktop` em `~/.local/share/applications`, `update-desktop-database`.
O registo do handler `moodlemobile://` — que é a melhor ideia do projeto — não existe
nos outros sistemas.

**Proposta:** `moodle-mcp install-handler` em Python, com ramificação por `sys.platform`:
- Linux: o `.desktop` atual
- Windows: `HKCU\Software\Classes\moodlemobile\shell\open\command` via `winreg` (não precisa de admin)
- macOS: um `.app` mínimo com `CFBundleURLTypes` no `Info.plist`

Um comando só, em vez de um script de shell que metade dos utilizadores não consegue correr.

### 8. Nomes de ficheiro quebram no Windows
`_safe_filename` remove `\ / * ? : " < > |` mas não trata:
nomes reservados (`CON`, `NUL`, `PRN`, `AUX`, `COM1`…), pontos/espaços finais, e o
limite de 260 caracteres do `MAX_PATH`. Um anexo chamado `NUL.pdf` ou uma UC chamada
`Aux` falha com `OSError` no Windows; o corte em 120 caracteres não impede exceder o
`MAX_PATH` quando somado a um vault profundo.

### 9. Só transporte stdio
`mcp.run(transport="stdio")` está fixo. O SDK 1.30 já tem `streamable-http`.
Expor `--transport` permite correr o servidor num sítio e usá-lo de outro
(ou do opencode em modo `type: "remote"`).

---

## P2 — Schemas: compatibilidade com providers de LLM

Esta secção é a que mais afeta "funcionar bem com o opencode e outros providers",
porque o opencode é agnóstico quanto ao modelo. As medições no topo são a prova.

### 10. 88 parâmetros, 0 descrições
A docstring da função vai como descrição da *tool*, mas os parâmetros ficam só com
`title: "Assign Id"` gerado do nome. O modelo tem de adivinhar o que é `cmid` (vs `id`
vs `instance`), `item_id`, `classification`. Em Moodle a distinção `cmid` / `instance id`
é precisamente onde se erra.

**Proposta:** `Annotated[int, Field(description="Course-module id (cmid), …")]`.
É a diferença entre um modelo pequeno acertar e não acertar na chamada.

### 11. `Optional[X] = None` gera `anyOf` — 9 ocorrências
`{"anyOf": [{"type":"integer"},{"type":"null"}]}` é o padrão que falha com
function-calling estrito (OpenAI strict mode) e com os schemas do Gemini, que não
suportam `anyOf` em todos os contextos. Afeta `user_id`, `course_id`, `vault_dir`,
`attachments_draft_itemid`, `online_text_html`, `files_draft_item_id`.

**Proposta:** sentinelas do mesmo tipo — `user_id: int = 0` ("0 = utilizador autenticado"),
`vault_dir: str = ""`. Schema de tipo único, descrito na descrição do campo.

### 12. 5 300 tokens de schema em todos os pedidos
32 tools ocupam ~5,3k tokens de contexto em **cada** chamada, antes de qualquer
conteúdo. Com modelos locais (Ollama/LM Studio via opencode) ou janelas pequenas isso
é caro e degrada a seleção de tools.

**Proposta:** toolsets por flag — `--tools core,grades,forums,obsidian,write`.
Nota de arquitetura: `call_any_moodle_wsfunction` torna ~15 das tools de leitura
redundantes; um perfil `minimal` (site_info + sync + call_any + download) cabe em
<1k tokens.

### 13. Nomes genéricos colidem
`get_messages`, `get_site_info`, `download_moodle_file` — num cliente com vários
servidores MCP ligados (e sem namespacing, como o opencode), `get_messages` é ambíguo
face a um servidor de email ou Slack.

**Proposta:** prefixo `moodle_` em todas as tools (`name=` no decorador, sem renomear
as funções Python).

### 14. Zero `annotations`
Nenhuma das 32 tools declara `readOnlyHint` / `destructiveHint`. Os clientes usam estes
hints para políticas de auto-aprovação. Sem eles, ou aprovas 32 tools à mão, ou aprovas
tudo — incluindo a submissão de e-fólios.

**Proposta:** `annotations=ToolAnnotations(readOnlyHint=True)` nas ~22 de leitura e
`destructiveHint=True` nas de escrita. Baixo esforço, efeito direto na segurança
de utilização em qualquer cliente.

### 15. Erros devolvidos como dados, não como erros
`_moodle_call` devolve `{"error": True, "message": …}` com HTTP 200 no protocolo. O
cliente vê sucesso; só o modelo é que percebe. Além disso, as exceções de Moodle
(`{"exception": …, "errorcode": …}`) passam sem tratamento em quase todos os sítios.

**Proposta:** levantar `ToolError`, que o FastMCP converte em `isError: true`.
Permite ao cliente distinguir "falhou" de "respondeu".

### 16. Descrições só em português
Funciona, mas o emparelhamento tool↔pedido é mais fiável em inglês na maioria dos
modelos, sobretudo nos menores e nos não-Anthropic.

**Proposta:** descrições de tools e campos em inglês; o conteúdo gerado nas notas
mantém-se em português (é o que o utilizador lê).

---

## P3 — Estrutura, qualidade, documentação

### 17. 1159 linhas num ficheiro, lógica acoplada ao registo de tools
`capture_mobile_token.py` faz `import main` e chama funções decoradas. Funciona por
acidente do SDK. Impede testar sem levantar o servidor.

**Proposta:** `moodle_mcp/{client.py, obsidian.py, tools/, server.py, cli.py}` —
`client.py` e `obsidian.py` puros, `server.py` só registo. A CLI e as tools chamam o
mesmo núcleo. É esta separação que destranca tudo o resto (testes, CI, toolsets).

### 18. Docstrings sem estrutura
Todas as 32 tools têm docstring — bom, e melhor do que a média. Mas nenhuma tem
`Args:` / `Returns:` / `Raises:`. Falta docstring de módulo no `main.py`.

**Proposta:** estilo Google consistente (`Args`, `Returns`, `Raises`), com a primeira
linha a ser uma frase imperativa curta — é essa que domina a seleção da tool.

### 19. Caminho absoluto pessoal nas docstrings
Duas docstrings contêm o caminho absoluto do Vault de quem desenvolveu. Está
**dentro da descrição enviada ao modelo** — expõe o nome de utilizador a qualquer
provider e é falso para qualquer outra pessoa.

### 20. Imports mortos
`shutil`, `sqlite3`, `tempfile` importados e nunca usados (0 ocorrências de cada).
O `sqlite3` sugere uma leitura de cookies do browser abandonada — vale confirmar que
não há aí uma intenção perdida.

### 21. Bug: `pdf_pages` num PDF de 0 páginas
`main.py` usa `idx` depois do `for idx, page in enumerate(doc, start=1)`. Se o PDF não
tiver páginas, `NameError` — apanhado pelo `except` e reportado como aviso de extração.
Usar `len(doc)`.

### 22. `_append_token_to_fileurl` faz uma verificação imprecisa
A deteção de um parâmetro já presente no URL usa uma comparação de substring, que dá
falsos positivos. Verificar o parâmetro com `parse_qs`. O token acaba na query
string, que é registada em logs de servidor — ponderar autenticação por cabeçalho.

### 23. `extract_token_from_session_cookie` é frágil
A extração usa uma heurística sobre o texto da página em vez de localizar a linha da
tabela do serviço `moodle_mobile_app`, pelo que pode devolver o valor errado.

### 24. Zero testes, zero CI
**Proposta:** `pytest` + `responses` (ou `respx`) a mockar o endpoint
`/webservice/rest/server.php` com fixtures reais anonimizadas; testes golden-file para
os geradores de Markdown (`sync_doctoral_dashboard_to_obsidian` produz texto —
é trivialmente testável e é a função mais complexa do projeto).
GitHub Actions em matriz `ubuntu / macos / windows` × `3.10–3.13`: é a única forma
honesta de afirmar que funciona no Windows. `ruff` + `mypy` + `pre-commit`.

### 25. Sem `LICENSE` — e o PyMuPDF é AGPL — ✅ FEITO
Não há ficheiro de licença, o que na prática impede qualquer terceiro de usar o código.
E `pymupdf` 2.0 é AGPL-3.0 (ou comercial): só te afeta **se distribuíres** o projeto,
mas a AGPL é viral e obrigaria o teu código a ser AGPL também.

**Proposta:** se publicares, tornar a extração de PDF um extra opcional
(`pip install moodle-mcp[pdf]`) ou trocar por `pypdf` (BSD). Para uso pessoal, não é
problema — mas decide antes de publicar, não depois.

### 26. `instance="uab"` / `"utad"` está codificado — ✅ FEITO
Os literais aparecem em todas as 32 assinaturas e em `_resolve_target`. Nenhuma outra
universidade pode usar isto sem editar o código — e o nome "uab" não significa nada
para um modelo a ler a schema.

**Proposta:** registo de instâncias na config (`[instances.uab] url=… `), `instance`
com omissão para a instância predefinida, e `Literal`/enum **gerado da config** para
que o cliente receba os valores válidos na schema em vez de um `string` livre.

### 27. Dashboard: chamadas sequenciais sem progresso
`sync_doctoral_dashboard_to_obsidian` faz O(cursos × trabalhos) pedidos HTTP
sequenciais, cada um com `timeout=30`, e cria uma conexão nova por chamada
(`requests.get/post` direto, sem `Session`). Pode bloquear o cliente minutos sem sinal
de vida.

**Proposta:** `requests.Session` com `HTTPAdapter(max_retries=Retry(backoff_factor=…))`
para reutilizar conexões e tolerar falhas transitórias; `ctx.report_progress()` e
`ctx.info()` (disponíveis no SDK 1.30) para dar feedback por UC.

### 28. `print()` em stdout quebraria o protocolo
Em stdio, stdout é o canal JSON-RPC. Hoje não há nenhum `print`, mas nada o impede.
**Proposta:** `logging.basicConfig(stream=sys.stderr)` explícito no arranque.

---

## Configuração em cada cliente (depois do P1)

Com `[project.scripts]` + `uvx`, a mesma linha serve todos os clientes.

**opencode** (`opencode.json` / `opencode.jsonc`, chave de topo `mcp`;
`type: "local"`, `command` como **array**, `environment` como objeto,
mais `cwd`, `enabled`, `timeout` em ms — omissão 30000):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "moodle": {
      "type": "local",
      "command": ["uvx", "moodle-mcp"],
      "environment": { "MOODLE_URL": "https://elearning.uab.pt" },
      "enabled": true
    }
  }
}
```

Nota: o opencode usa `mcp` + `type`/`command`-array/`environment`, **não** o
`mcpServers` + `command`/`args`/`env` do Claude Code. O README atual só documenta
Linux e nenhum cliente — vale um bloco por cliente (Claude Code, Claude Desktop,
opencode, Cursor, VS Code, Zed) e um `server.json` para o registo oficial.

Para distribuição, por esta ordem: `uvx` a partir do Git → PyPI (`uv build` +
`uv publish`) → `server.json` no registo oficial (versão tem de coincidir com a do
PyPI, senão a verificação falha) → opcionalmente um bundle `.mcpb` para instalação
num clique no Claude Desktop (tipo de servidor `uv`, que resolve dependências na
instalação em vez de as vendorizar).

---

## Ordem sugerida

1. P0.1 e P0.2 — fuga de tokens e contenção de escrita. Horas, não dias.
2. #17 (separar núcleo do registo) — destranca o resto.
3. P1.5 + P1.6 (`pyproject` + config em platformdirs) — é o "setup fácil".
4. P2.10/11/14 (descrições de campos, sentinelas, annotations) — ganho imediato
   em qualquer provider, baixo risco.
5. P1.7 (handler por SO) + #24 (CI em matriz) — só aqui se pode afirmar
   "funciona no Windows".
6. Resto.


---

# Anexo A — Estado do P0 (implementado)

Alterações em `main.py` (+218 / −38).

| # | Medida | Onde |
|:--|:---|:---|
| 1 | `_redact_secrets()` aplicado em `_moodle_call()`, que redige recursivamente as chaves que transportam credenciais | chokepoint único: cobre as 32 ferramentas, incluindo `call_any_moodle_wsfunction` |
| 2 | As ferramentas de autenticação deixam de devolver material secreto | devolvem `token_fingerprint` (`sha256:` + 12 hex) e indicadores booleanos |
| 3 | `set_moodle_token` devolve `token_fingerprint` em vez de nada verificável | permite confirmar *qual* token está ativo sem o expor |
| 4 | `extract_token_from_session_cookie` deixa de devolver o URL de redireção | substituído por `redirected_to_login: bool` |
| 5 | `_resolve_in_vault()` recusa qualquer caminho fora de `OBSIDIAN_VAULT_DIR`, com `resolve()` antes do teste, o que apanha symlinks | `download_moodle_file`, `upload_file_to_draft`, `sync_doctoral_dashboard_to_obsidian` |
| 6 | Diretórios derivados nascem da raiz validada | `base_uc_dir`, `foruns_dir`, `target_dir` |
| 7 | Caminho absoluto pessoal removido das docstrings (ia na schema para o provider) | duas docstrings, agora `<Vault>/…` |
| 8 | Bug `pdf_pages`: `idx` → `len(doc)` | `download_moodle_file` |

`upload_file_to_draft` passou a exigir que a origem esteja no Vault: o upload **sai**
da máquina para o servidor da universidade, pelo que uma origem arbitrária não é
apropriada. Coloca os e-fólios no Vault antes de submeter.

Testes executados (25 asserções, todas a passar): arranque do servidor e 32
ferramentas; redação em dict e em lista aninhada; impressão digital não reversível;
aceitação de caminhos relativos e absolutos dentro do Vault e recusa de caminhos
fora dele, incluindo via symlink e via segmentos relativos; ausência de material
secreto nas respostas das ferramentas de autenticação; preservação das chaves de que
o capturador de token depende; recusa de leitura e escrita fora do Vault **antes**
de qualquer pedido HTTP.

---

# Anexo B — Compatibilidade com providers de LLM (medido)

## Dados

| Verificação | Resultado |
|:---|:---|
| `additionalProperties: false` nas input schemas | **0 de 32** |
| Tools que **falham** o strict mode da OpenAI | **32 de 32** |
| Parâmetros com `anyOf: [T, null]` | 9 de 88 |
| Parâmetros com descrição | **0 de 88** |
| Descrições de tool em português | 30 de 32 |
| Tools com nome prefixado (`moodle_*`) | 0 de 32 |
| Tipos de parâmetro | string 49, integer 23, anyOf 9, boolean 7 |

## O que isto significa, por provider

**OpenAI (strict / Structured Outputs).** O strict mode exige
`additionalProperties: false` em cada objeto **e** todas as propriedades listadas em
`required` — campos opcionais não são permitidos, modelam-se como uniões nullable.
As 32 tools falham ambos os requisitos: nenhuma declara `additionalProperties` e
todas têm parâmetros opcionais fora de `required` (`instance` em todas, mais
`user_id`, `vault_dir`, etc.). Em modo não-estrito funcionam; com strict ligado, o
cliente tem de transformar a schema ou a chamada é rejeitada.

**Gemini.** Não confirmei nos documentos oficiais o subconjunto aceite em
`functionDeclaration`: a página de function calling da Google lista apenas `type`,
`properties` e `required`, e não se compromete quanto a `anyOf` ou a campos nullable.
Fontes de terceiros divergem. Trato isto como **não verificado**, não como
"não suporta". A mitigação é a mesma em qualquer caso e não tem custo.

**Anthropic / Claude.** Aceita as schemas como estão. É o caso onde os 9 `anyOf`
e a ausência de `additionalProperties` não incomodam — razão pela qual o problema
passa despercebido se só testares aqui.

**Modelos locais (Ollama / LM Studio via opencode).** O custo dominante são os
~5 300 tokens de schema em cada pedido e os 88 parâmetros sem descrição: a seleção
de tool degrada-se muito antes de qualquer questão de validação de schema.

## Mitigações, por ordem de retorno

1. **Sentinelas de tipo único** em vez de `Optional[X] = None`
   (`user_id: int = 0`, `vault_dir: str = ""`). Elimina os 9 `anyOf` e com eles toda
   a dependência do que cada provider aceita em uniões. Custo: trivial.
2. **`Annotated[T, Field(description=...)]`** nos 88 parâmetros. É o único item que
   melhora a precisão em *todos* os providers ao mesmo tempo, e é onde a distinção
   `cmid` vs `instance id` do Moodle deixa de ser adivinhada.
3. **`annotations=ToolAnnotations(readOnlyHint=True)`** nas ~22 de leitura e
   `destructiveHint=True` nas 5 de escrita. Agnóstico por construção: é o cliente,
   não o provider, que usa os hints para decidir o que auto-aprova.
4. **Toolsets** (`--tools core,grades,forums,obsidian,write`). Reduz os 5 300 tokens
   e é o que torna o servidor utilizável com modelos pequenos.
5. **Prefixo `moodle_`** via `name=` no decorador (sem renomear as funções Python).
   Evita a colisão de `get_messages` / `get_site_info` com outros servidores MCP num
   cliente sem namespacing.
6. **Descrições em inglês** nas tools e campos, mantendo o conteúdo das notas em
   português.
7. **`additionalProperties: false`**: o FastMCP não o emite. Se quiseres strict mode
   da OpenAI sem depender de transformação no cliente, é preciso declarar os
   parâmetros como modelos Pydantic com `model_config = ConfigDict(extra="forbid")`.
   Secundário — a maioria dos clientes chama em modo não-estrito.

Os pontos 1–3 são a definição prática de "agnóstico": removem as construções sobre
as quais os providers divergem, em vez de tentar acomodar cada um.

## Fontes consultadas

- OpenAI, guia de function calling: https://developers.openai.com/api/docs/guides/function-calling
- Google, Gemini function calling: https://ai.google.dev/gemini-api/docs/function-calling (inconclusivo quanto a `anyOf`)
- "Structured Outputs Across LLM Providers: 244 Models Tested (2026)": https://www.requesty.ai/blog/structured-outputs-across-llm-providers-the-compatibility-mess
- "Structured Outputs Across AI APIs: OpenAI, Claude, Gemini (2026)": https://www.developersdigest.tech/blog/structured-outputs-across-ai-apis-2026


---

# Anexo C — Mitigações 1–3 (implementado 2026-10-24)

## O que foi feito

**1. Sentinelas de tipo único.** Os 9 `Optional[X] = None` passaram a `int = 0` /
`str = ""`. `anyOf` na schema: **9 → 0**. Oito corpos adaptados
(`x is not None` → `x`, `user_id if ... else` → `user_id or ...`). A sentinela `0`
é segura porque nenhum id do Moodle é 0: `item_id=0` já significava "criar novo
draft" no código original.

**2. `Annotated[T, Field(description=...)]` nos 88 parâmetros.** Com descrição:
**0 → 88**. Três aliases reutilizáveis (`Inst`, `CourseId`, `UserId`) evitam
repetir a mesma frase 32 vezes. As descrições resolvem as ambiguidades reais do
Moodle: `cmid` vs `instance` em `toggle_activity_completion`, `assign_id` vs `cmid`
em `get_assignment_submission`, a sintaxe `courseids[0]` em
`call_any_moodle_wsfunction`, e o aviso de irreversibilidade em
`submit_for_grading`.

**3. `ToolAnnotations` nas 32 tools**, via helper `_ann(title, read_only=,
destructive=, idempotent=)`. Todas levam `openWorldHint=True` e um `title` legível.

| Classe | N | Flags |
|:---|--:|:---|
| Leitura pura | 17 | `readOnlyHint=True` |
| Escrita local no Vault | 4 | não-RO, não-destrutiva, idempotente |
| Credenciais (sobrescrevem o token) | 3 | **`destructiveHint=True`** |
| Escrita no Moodle, aditiva | 5 | não-RO, `idempotentHint=False` |
| Estado no Moodle, reversível | 1 | `toggle_activity_completion` |
| Entrega de e-fólio / API arbitrária | 2 | **`destructiveHint=True`**, `idempotentHint=False` |

As 17 de leitura passam a ser auto-aprováveis por qualquer cliente; as 15 restantes
pedem confirmação. `set_moodle_token`, `decode_mobile_launch_token` e
`extract_token_from_session_cookie` estão marcadas como destrutivas porque
sobrescrevem o token guardado sem retorno — foi exactamente esta a falha que
apagou o `MOODLE_TOKEN` durante o trabalho do P0.

Nota sobre as aditivas (`reply_to_forum_post`, `create_forum_discussion`,
`send_instant_message`, `add_submission_comment`, `upload_file_to_draft`): pela
especificação MCP, `destructiveHint` distingue *destrutivo* de *aditivo*, e um post
num fórum é aditivo. Ficam `destructiveHint=False` com `idempotentHint=False`
(repetir cria duplicados) e a irreversibilidade dita na descrição. Se preferires
que o cliente trate um post como destrutivo, é mudar um argumento em `_ann`.

## Custo: o contexto cresceu 77%

| | chars | ~tokens |
|:---|--:|--:|
| Antes | 18 699 | 5 342 |
| **Depois** | **33 128** | **9 465** |
| └ descrições de parâmetro | 7 168 | 2 048 |
| └ annotations | 4 258 | 1 217 |
| └ `title` autogerados (redundantes) | 1 982 | 566 |

Isto é o lado mau do trade-off e não estava quantificado no Anexo B: a precisão
por chamada melhora em todos os providers, mas o custo fixo de contexto quase
duplicou. **Torna a mitigação 4 (toolsets) necessária, não opcional**, sobretudo
para os modelos locais que eram o caso de uso mais frágil. Há ainda ~566 tokens de
`title` autogerados pelo Pydantic (`"title": "Cmid"`), puramente redundantes com o
nome do parâmetro — removê-los é a poda mais barata que resta.

## Verificação

18 asserções a passar. A redação de segredos é agora exercitada ponta a ponta
através do `_moodle_call` real (mock ao nível do `requests`), e não só na função
`_redact_secrets` isolada — foi assim que se confirmou que `get_site_info` já não
devolve `userprivateaccesskey`. Sentinelas testadas nos dois sentidos: `user_id=0`
resolve para o aluno autenticado e `user_id=99` é respeitado; `course_id=0` usa
`core_calendar_get_calendar_upcoming_view` e `course_id=7` filtra;
`online_text_html=""` não envia o plugin de texto. O `.env` foi protegido por mock
e não foi tocado.


---

# Anexo D — Mitigação 4: toolsets (implementado 2026-10-24)

## Resultado medido

| Perfil | Tools | ~tokens de schema |
|:---|--:|--:|
| Original (antes de qualquer P2) | 32 | 5 342 |
| Depois do P2, sem toolsets | 32 | 9 465 |
| `--tools all` (com titles removidos) | 32 | **8 488** |
| Omissão (sem `raw`) | 31 | **8 225** |
| `--tools core,obsidian` | 15 | **4 321** |
| `--tools core` | 11 | **2 793** |

O ponto que interessa: `core,obsidian` tem **4 321 tokens contra os 5 342 do
original**, e mantém as descrições em todos os parâmetros e as anotações em todas
as ferramentas. Ou seja, é possível ficar abaixo do ponto de partida *com* as
schemas melhores — bastou deixar de anunciar ao cliente ferramentas que o fluxo
principal não usa.

A remoção dos `title` autogerados valeu 977 tokens no perfil completo (120 chaves).

## Como funciona

`TOOLSETS` é uma tabela única no fim do `main.py` que mapeia nome → ferramentas:
`core` (11: autenticação, disciplinas, conteúdos, calendário, mensagens),
`grades` (5), `forums` (4), `obsidian` (4: downloads e sincronização),
`write` (7: tudo o que escreve no Moodle), `raw` (1).

`apply_toolsets()` usa `FastMCP.remove_tool()` — API pública — para retirar da
superfície MCP o que não está ativo. **As funções Python continuam a existir e a
ser chamáveis**, o que é essencial porque o
`sync_doctoral_dashboard_to_obsidian` compõe internamente `get_course_grades`,
`get_course_forums` e outras. Confirmado em teste: com `--tools core,obsidian`,
`get_course_forums` desaparece da lista do cliente mas a sincronização completa
continua a funcionar.

`strip_schema_titles()` remove as chaves `title` das input schemas. O
`annotations.title` (o nome legível para a interface do cliente) não é afetado.
Depende de `mcp._tool_manager`, porque o FastMCP 1.30 não tem acessor síncrono
para as ferramentas registadas; esse acesso privado está contido numa única
função, `_iter_registered_tools()`, com degradação graciosa se o SDK mudar.

`raw` fica **fora da omissão**: é o item 3 do P0 que faltava. O
`call_any_moodle_wsfunction` dá acesso total à API, escrita incluída.

## CLI (novo)

O ficheiro deixou de terminar em `mcp.run(transport="stdio")` e passou a ter um
`main(argv)` com `argparse` — o que também é o pré-requisito do
`[project.scripts]` do P1.5.

```
moodle-mcp --tools core,obsidian
moodle-mcp --list-toolsets
MOODLE_MCP_TOOLS=core,grades moodle-mcp
MOODLE_MCP_LOG_LEVEL=DEBUG moodle-mcp
```

`core` é acrescentado automaticamente a qualquer seleção (sem autenticação nem
listagem de disciplinas, nada mais tem por onde começar) e um nome inválido dá
erro com a lista dos válidos.

Resolvido de passagem o item 28: `logging.basicConfig(stream=sys.stderr)`. Em
stdio o stdout é o canal JSON-RPC, e o servidor agora escreve diagnóstico
(toolsets ativos, ferramentas retiradas) — tinha de ir para stderr.

## Verificação

20 asserções, mais um **handshake MCP real por stdio** com o cliente do SDK, que
é o que prova o comportamento do ponto de vista do cliente e não do processo:
`initialize` + `list_tools` devolvem 31 / 15 / 32 ferramentas conforme o perfil,
com 17 / 8 / 17 marcadas read-only. Verificado também que a tabela `TOOLSETS`
cobre exactamente as 32 ferramentas registadas, sem duplicados nem omissões —
um teste que falha se alguém acrescentar uma ferramenta e se esquecer da tabela.


---

# Anexo E — P1.5 e P1.6 (implementado 2026-10-24)

## Estrutura

```
moodle-mcp/
├── pyproject.toml              novo
├── moodle_mcp/
│   ├── __init__.py             novo (versão 0.2.0)
│   ├── config.py               novo — P1.6
│   ├── server.py               ← main.py (git mv, histórico preservado)
│   └── token_catcher.py        ← capture_mobile_token.py (git mv)
├── main.py                     shim de compatibilidade
└── capture_mobile_token.py     shim de compatibilidade
```

Os dois shims existem porque a tua configuração de cliente aponta para
`main.py` com caminho absoluto e o handler `.desktop` aponta para
`capture_mobile_token.py`. **Nada do que já funcionava deixou de funcionar.**
Verificado: as quatro formas de arrancar (`moodle-mcp`, `--tools core,obsidian`,
`main.py`, `python -m moodle_mcp.server`) passam o handshake MCP e devolvem
31 / 15 / 31 / 31 ferramentas.

Isto é o mínimo para o entrypoint ser real, **não** o split completo do item 17
(`client.py` / `obsidian.py` / `tools/`), que continua em aberto.

## P1.5 — pyproject.toml

`[project.scripts] moodle-mcp = "moodle_mcp.server:main"` mais
`moodle-mcp-token-catcher = "moodle_mcp.token_catcher:cli"`, build com hatchling.
Habilita `uvx --from git+… moodle-mcp` sem clonar nem gerir venv, igual nos três
sistemas operativos.

Duas decisões:

* **`requires-python = ">=3.11"`** porque o `config.toml` é lido com `tomllib`,
  que só é stdlib a partir do 3.11. Evita uma dependência (`tomli`) ao custo de
  não suportar 3.10.
* **PyMuPDF passou a extra `[pdf]`** — é o item 25. O import ficou condicional e,
  sem o extra, o download de PDF funciona e devolve
  `pdf_extraction_warning` em vez de falhar. Testado. Instalar o servidor deixa
  de impor a AGPL-3.0 a quem não queira a extração de texto.

A CLI ganhou `--show-config` e `--version`. O `requirements.txt` passou a
`-e .[pdf]`, com o pyproject como fonte única.

## P1.6 — configuração em camadas

`config.py`, com cinco camadas por ordem de precedência:

1. Variáveis de ambiente (como os clientes MCP injetam configuração)
2. `config.toml` — opções não secretas, editado à mão
3. `credentials.json` — só tokens, `0600` em POSIX
4. `.env` legado ao lado do código — ainda lido, migrado na primeira escrita
5. Omissões

Diretório via `platformdirs`: `~/.config/moodle-mcp`,
`~/Library/Application Support/moodle-mcp`, `%LOCALAPPDATA%/moodle-mcp`.
`MOODLE_MCP_CONFIG_DIR` força outro, o que serve para testes e instalações
portáteis.

Os segredos ficam **separados** das restantes opções por duas razões: o
`config.toml` pode ser partilhado ou versionado sem arrastar credenciais, e
escrever JSON não exige uma dependência de escrita de TOML. O `save_setting`
reescreve o `config.toml` por completo, pelo que comentários não sobrevivem —
troca deliberada.

O `_update_env_var` foi substituído por `_persist`, que encaminha cada chave para
o ficheiro certo. O `set_moodle_token` já escreve em `credentials.json`.

Resolvido de passagem o `chmod 600` do item 6: em Windows o `chmod` não tem este
efeito, e em vez de o chamar e fingir que protege, `_restrict_permissions`
registra em DEBUG que a proteção fica a cargo das ACL do perfil.

**A tua instalação atual não precisa de nada:** o `.env` com o token continua a
ser lido (camada 4) e é migrado para `credentials.json` na primeira vez que o
token for gravado.

## Qualidade

* `ruff check` **passa limpo** nos cinco ficheiros, com `ruff format` aplicado.
* Removidos os imports mortos `shutil`, `sqlite3` e `tempfile` (item 20) e mais
  cinco que o ruff encontrou.
* `server.py` ganhou docstring de módulo, que documenta as decisões de segurança
  transversais.
* As regras de docstring (`D205`, `D212`, `D400`, `D401`, `D413`, `D301`) e `E501`
  estão em `per-file-ignores` **só para `moodle_mcp/server.py`**, com comentário a
  dizer que se ligam quando as docstrings das 32 ferramentas forem normalizadas
  (item 18). O gate fica verde e ratcheia; `config.py` e o resto cumprem tudo.

## Verificação

15 asserções de regressão (P0 + P2 + P1.6 juntas, depois do `ruff format`),
24 asserções de configuração, e o handshake MCP pelas quatro formas de arranque.
Testes de configuração incluem: precedência das cinco camadas nos dois sentidos,
permissões `0600` reais, `credentials.json` a conter apenas segredos,
`config.toml` a não conter nenhum, escape de aspas no TOML, rejeição de chaves
inválidas, ficheiros corrompidos a degradar para a camada seguinte em vez de
rebentar, e `describe()` a nunca revelar um token.

Os testes correram com `MOODLE_MCP_CONFIG_DIR` apontado para um diretório
temporário, pelo que a configuração real não foi tocada.

## O que falta do P1

* **7** — handler `moodlemobile://` por sistema operativo (o `install.sh` continua
  Linux-only, embora já instale o pacote e use o entrypoint novo)
* **8** — nomes de ficheiro reservados do Windows
* **9** — `--transport streamable-http`
* **17** — split completo do `server.py` em módulos
* **24** — testes como suite `pytest` versionada, em vez de ad-hoc, e CI em matriz


---

# Anexo F — Item 26: instâncias genéricas (implementado 2026-10-24)

Antes, `"uab"` e `"utad"` estavam escritos nas 32 assinaturas, em
`_resolve_target`, nas variáveis `MOODLE_UTAD_*`, no nome do servidor e nas tags
das notas geradas. A audiência endereçável eram duas universidades.

## O registo

As instâncias passaram a ser um registo aberto em `config.py`, alimentado por
quatro fontes com a precedência habitual:

| Fonte | Forma |
|:---|:---|
| Ambiente | `MOODLE_<NOME>_URL`, `MOODLE_<NOME>_TOKEN`, `MOODLE_DEFAULT_INSTANCE` |
| `config.toml` | `[instances.<nome>] url = "…"` e `default_instance` |
| `credentials.json` | `instances.<nome>.token`, a `0600` |
| `.env` legado | o mesmo esquema, mais o par sem nome |

A escolha do padrão `MOODLE_<NOME>_URL` não é arbitrária: é exactamente a forma
que o `MOODLE_UTAD_URL` já tinha, pelo que o esquema específico de ontem passou a
ser o esquema genérico e a retrocompatibilidade saiu de graça.

`Instance` é um dataclass congelado (`name`, `url`, `token`); `instances()`,
`default_instance()`, `resolve_instance()` e `instance_names()` são a API.

## O enum é gerado da configuração

O alias `Inst` deixou de ter uma descrição fixa. `_instance_field()` constrói o
campo a partir de `config.instance_names()`, pelo que o cliente recebe na schema
os nomes que *esta* instalação conhece, como `enum` quando há mais do que um:

```json
{"default": "ipl", "enum": ["ipl", "uab"],
 "description": "Instância Moodle a usar. Configuradas nesta instalação: 'ipl', 'uab'. Omissão: 'ipl'."}
```

Com uma só instância não há `enum`, só a omissão — não vale restringir o que não
tem alternativa. A omissão das 32 ferramentas passou de `"uab"` literal para a
constante `DEFAULT_INSTANCE`, resolvida no arranque.

Custo: a schema completa passou de ~8 483 para ~8 733 tokens, porque a descrição
do campo cresce com o número de instâncias. Proporcional e aceitável.

## Um bug que o teste apanhou, com implicação de segurança

A primeira versão atribuía o `MOODLE_URL`/`MOODLE_TOKEN` sem nome à instância
predefinida, fosse ela qual fosse. Com duas universidades configuradas e um
`.env` legado por limpar, isso fazia o token de uma ser usado contra o servidor
da outra — ou seja, enviar uma credencial institucional para terceiros.

O par sem nome passou a formar a sua própria instância **e só quando não existe
nenhuma instância nomeada**, que é o propósito da retrocompatibilidade. Havendo
nomes, é ignorado com registo em DEBUG em vez de silenciosamente ligado ao sítio
errado.

## Resto da des-institucionalização

Nome do servidor: `Moodle UAb/UTAD Doctoral Suite & Obsidian MCP Server` →
`Moodle & Obsidian MCP Server`. As tags das notas (`[doutoramento, uc, uab]`)
passaram a usar o nome da instância (`[moodle, uc, {instance}]`), assim como o
título do painel. O `token_catcher` deixou de fixar `instance="uab"`.

Ficam deliberadamente de fora, porque são outro item: os nomes de pasta e de nota
em português (`Unidades Curriculares`, `Doutoramento - Painel Moodle.md`) e o
vocabulário e-fólio/p-fólio, que precisam de templates e não de configuração.

## Verificação

16 asserções sobre o registo: `.env` legado a dar uma instância; três
universidades por ambiente; leitura e escrita do `config.toml`; tokens isolados
por instância e ausentes nas outras; `0600`; ambiente a ganhar ao ficheiro por
instância e por campo; `MOODLE_MCP_*` e `MOODLE_DEFAULT_*` a não serem
confundidos com nomes de instância; nome inválido a dar erro com a lista dos
válidos; `default_instance` inválido a avisar e a cair no fallback.

Mais 13 asserções de regressão do P0, P2 e P4, e o handshake MCP real em três
configurações, onde se confirma que o cliente recebe `enum=['ipl','uab']` e
`default='ipl'` quando é isso que está configurado.


---

# Anexo G — Item 25: LICENSE (implementado 2026-10-24)

O `pyproject.toml` declarava MIT e não havia ficheiro de licença, o que na
prática impedia qualquer terceiro de usar o código. Acrescentado `LICENSE` (MIT,
2026) e a metadata passada a PEP 639: `license = "MIT"` com
`license-files = ["LICENSE"]`, o que faz o ficheiro seguir dentro do wheel e do
sdist em vez de ficar só no repositório. O `METADATA` do pacote passou de
`License: MIT` para `License-Expression: MIT` e `License-File: LICENSE`.

## Um defeito que só apareceu por se construir o pacote

Ao verificar se a licença seguia no wheel, o conteúdo do wheel anterior mostrou
três módulos em vez de quatro: **o `moodle_mcp/token_catcher.py` não ia no
pacote**, enquanto o `pyproject.toml` continuava a declarar o console script
`moodle-mcp-token-catcher = "moodle_mcp.token_catcher:cli"`.

Causa: o `.gitignore` tinha `*token*`, herdado da altura em que o capturador se
chamava `capture_mobile_token.py` e tinha uma excepção própria. O ficheiro
renomeado passou a casar com o padrão, e como o backend de build usa o
`.gitignore` para escolher o que empacotar, o módulo era descartado em silêncio.

Consequência real: quem instalasse por `uvx` ou do PyPI receberia um comando
declarado que falha com `ImportError` — e a captura de token por SSO, que é a
funcionalidade mais distintiva do projeto, não existiria. Localmente nunca se
notava, porque a instalação editável lê do repositório.

Os padrões do `.gitignore` passaram a ser específicos (`credentials.json`,
`*.token`, `*_token.json`, `.env`, `*.log`) em vez de um `*token*` que apanhava
código a fingir que protegia segredos.

## Verificação

Build real com `uv build`, e comparação do wheel antes e depois:

| | módulos | LICENSE | token_catcher |
|:---|--:|:---:|:---:|
| antes | 3 | não | **não** |
| depois | 4 | sim | **sim** |

Mais a instalação do wheel num venv limpo **sem o extra `[pdf]`**, onde
`moodle-mcp --version`, `--list-toolsets` e `moodle-mcp-token-catcher` correm
todos, e o `importlib.metadata` devolve `License-Expression: MIT` com o ficheiro
em `dist-info/licenses/LICENSE`. O sdist leva ambos os ficheiros.

Nota sobre a AGPL: o PyMuPDF continua a ser extra opcional, pelo que a MIT do
projeto não entra em conflito. Quem instalar `moodle-mcp[pdf]` fica sujeito à
AGPL-3.0 dessa dependência, o que convém dizer no README se o projeto for
divulgado.
