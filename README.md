# moodle-mcp

[![CI](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-server-purple)

A **Model Context Protocol** server that gives AI assistants (Claude, opencode, any MCP client) safe access to **Moodle**, and syncs your courses, grades, deadlines and materials into an **Obsidian** vault.

Works with any Moodle that has Web Services and the mobile app service enabled — **including universities behind federated SSO** (Entra ID, SAML, Shibboleth, CAS), where students can't generate an API token by hand.

🇵🇹 [Versão em português](README.pt.md)

---

## Why this one

Most MCP servers for third-party platforms pipe untrusted content straight into the model and expose every write action by default. Moodle is a worst case for that: any participant in a course can write forum posts the model will read, and actions like submitting an assignment can't be undone. This server is designed around that threat model:

- **Read-only by default.** Write tools (submissions, forum posts, messages) and the raw Web Services API are off unless you enable them explicitly. 17 of the 21 default tools carry `readOnlyHint`, so a client can auto-approve them without auto-approving anything irreversible.
- **Prompt-injection defenses on the server side** (OWASP LLM01), not delegated to the model:
  - hidden content is stripped from every Moodle response — `display:none`, `font-size:0`, `hidden`, HTML comments, Unicode tag characters, zero-width and bidi control characters — so the model reads what the student sees;
  - notes written to the vault can't execute anything: `dataviewjs`/`dataview` blocks, inline Dataview and Templater commands are neutralised, remote images become links (no tracking pixels);
  - every Moodle write requires **user confirmation via MCP elicitation**, showing the exact recipient and text — even when the client is set to auto-approve.
- **Token confinement.** Credentials never appear in tool responses. The token is only ever attached to `pluginfile.php` URLs on the configured instance; any other host is refused before a request is made. Tools that can overwrite credentials are off by default, so a malicious forum post can't swap your token.
- **Context-budget aware.** The full tool set costs ~8,900 tokens per request; selectable toolsets cut that to ~1,600–4,950.

## Features

- **SSO token capture** — a `moodlemobile://` URL handler intercepts the mobile-app login redirect from your browser, decodes the token and stores it (`0600`). Registered natively on Linux (`xdg-mime`), Windows (`HKEY_CURRENT_USER`, no admin) and macOS (Launch Services). The token never passes through the model.
- **Obsidian dashboard sync** — a central note with every course, progress, current grades and a sorted deadline calendar; one note per course with grade report, per-assignment submission status, assignment briefs converted to Markdown and the resource tree.
- **Course material download** — PDFs, slides and HTML pages saved into the vault, with each PDF converted to a searchable `.pdf.md` that agents can read (optional `[pdf]` extra).
- **32 tools** across courses, books and pages, completion, grades, assignments and submissions, quizzes, forums (search, export to Obsidian, reply), messages and calendar.
- **Multi-instance** — an open registry of Moodle instances; when more than one is configured, the `instance` parameter is exposed as an **enum in the schema**, so the model gets the valid names instead of free text.

## Install

```bash
uvx --from git+https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp moodle-mcp --show-config
```

or from a clone:

```bash
git clone https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp.git
cd moodle-obsidian-mcp
uv pip install -e ".[pdf]"
```

The `moodlemobile://` handler needs a permanent install (`uvx` runs from a cache that can be wiped):

```bash
uv tool install "moodle-obsidian-mcp[pdf] @ git+https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp"
```

The distribution is named `moodle-obsidian-mcp` (`moodle-mcp` is taken on PyPI); the commands are still `moodle-mcp` and `moodle-mcp-token-catcher`.

The `[pdf]` extra adds PDF text extraction via PyMuPDF. It's kept out of the base install because PyMuPDF is **AGPL-3.0**; without it everything works, PDF downloads just don't get the companion `.md`.

Register the `moodlemobile://` handler (needed for SSO login):

```bash
moodle-mcp --install-handler
moodle-mcp --uninstall-handler
```

## Reliability

- **One failing course doesn't stop a sync.** Each course syncs independently; a section Moodle refuses (quizzes, choices, grades) is left empty and reported in `errors` with `status: "partial"`. Only a missing or invalid token stops everything, with instructions to log in again.
- **Moodle errors arrive as errors** (`isError` in MCP), with the cause and what to do, instead of being disguised as results.
- **Nothing is overwritten by accident:** files, pages, forums and courses with the same name get ` (2)`, ` (3)`…; filenames are valid on Windows, macOS and Linux.
- **Transient failures are retried:** reads back off on 429/5xx and connection errors (honouring `Retry-After`); writes are never retried once the server has received them, so nothing gets posted twice. Connections are reused (keep-alive).
- **The server doesn't block:** sync, material download and forum export run in a thread and report per-course progress to the client.
- **Vault layout:** `MOODLE_MCP_SYNC_SUBDIR` puts the dashboard and course notes in a subfolder (links follow); `MOODLE_MCP_DASHBOARD_NAME` renames the dashboard. With several instances, each gets its own dashboard.
- **Atomic, bounded downloads:** a failed download leaves the previous file intact, and no file exceeds `MOODLE_MCP_MAX_DOWNLOAD_MB` (500).
- **Audit trail:** every action on Moodle is recorded in `audit.log` (`0600`, in the config directory) with its outcome — confirmed, refused, or run without confirmation — with no tokens and rotation at 1 MB.
- **Less personal data:** `get_course_participants` returns only id, name, roles and groups; classmates' emails and profile data never reach the LLM provider.
- **Secrets:** `credentials.json` and `last_auth.log` are written atomically and already `0600`; an unreadable `credentials.json` is set aside rather than deleted; `moodle-mcp --show-config` (and startup) warn about files readable by other users.

## Configuration

Nothing is required — every option has a default. Configuration is layered, highest precedence first:

1. **Environment variables** — how MCP clients inject options (`env` in Claude, `environment` in opencode).
2. **`config.toml`** in the user config directory.
3. **`credentials.json`** in the same directory — tokens only, `0600`.
4. **`.env`** next to the code — legacy, still read.

The config directory follows the OS: `~/.config/moodle-mcp` (Linux), `~/Library/Application Support/moodle-mcp` (macOS), `%LOCALAPPDATA%\moodle-mcp` (Windows). It doesn't live next to the code because under `uvx`/`pipx` the package runs from an ephemeral, possibly read-only cache.

```bash
moodle-mcp --show-config      # where everything is read from, secrets redacted
moodle-mcp --list-toolsets    # which tools each toolset exposes
```

### Moodle instances

```toml
default_instance = "uab"

[instances.uab]
url = "https://elearning.uab.pt"

[instances.utad]
url = "https://e-learning.utad.pt"
```

or via environment: `MOODLE_<NAME>_URL` and `MOODLE_<NAME>_TOKEN`.

Unnamed `MOODLE_URL` / `MOODLE_TOKEN` still work as the default instance, but only while no named instance exists — otherwise one university's token could be sent to another's server.

### Toolsets

| `--tools` | Tools | ~tokens | Writes to Moodle |
|:---|--:|--:|:---|
| **(default)** | **21** | **~4,950** | **no** |
| `core,grades,forums,obsidian,write` | 28 | ~7,250 | yes |
| `all` | 32 | ~8,900 | yes, plus the raw API |
| `core,obsidian` | 12 | ~3,200 | no |
| `core` | 8 | ~1,600 | no |

`core` is always included. `write` and `raw` log a warning at startup. `auth` (tools that store credentials) is off by default; even when on, `set_moodle_token` validates the token, accepts only `https://` URLs and won't change an existing instance's URL.

### Write confirmation

`MOODLE_MCP_CONFIRM_WRITES`:

| Value | When confirmation is requested |
|:---|:---|
| `always` (default) | before any Moodle action |
| `untrusted` | only after the session has read third-party content (posts, pages, messages) |
| `client` | never; relies on the MCP client's approval alone (for clients without elicitation — not recommended) |

**Limits:** a client that answers confirmation prompts automatically defeats the last line of defense, and visible malicious text still reaches the model. That's why write tools stay off by default.

## Client setup

**Claude Code / Claude Desktop** (`mcpServers`):

```json
{
  "mcpServers": {
    "moodle": {
      "command": "moodle-mcp",
      "args": ["--tools", "core,obsidian"],
      "env": { "MOODLE_URL": "https://your-moodle.example.edu" }
    }
  }
}
```

**opencode** (`opencode.json`):

```json
{
  "mcp": {
    "moodle": {
      "type": "local",
      "command": ["moodle-mcp", "--tools", "core,obsidian"],
      "environment": { "MOODLE_URL": "https://your-moodle.example.edu" },
      "enabled": true
    }
  }
}
```

## Browser login (SSO)

1. Log in to your university's Moodle in the browser as usual.
2. Run:
   ```bash
   moodle-mcp --login            # or: moodle-mcp --login utad
   ```
   This opens a login link with a random, single-use *passport* valid for 15 minutes (`--no-browser` just prints it).
3. Accept the **"Open Moodle Token Catcher"** prompt — the token is verified, stored in `credentials.json` under the right instance and the vault syncs immediately. The outcome is written to `last_auth.log` in the config directory.

The handler only accepts the response to a login started this way: Moodle signs it with `md5(wwwroot . passport)` and the handler checks that signature. A fixed passport would let any web page replace your token with another account's.

> **Note:** this uses the same login flow as the official Moodle mobile app and only grants what your own account can already do. Check your institution's acceptable-use policy before automating access to its platform.

## Development

```bash
uv pip install -e ".[dev,pdf]"
ruff check moodle_mcp tests && ruff format --check moodle_mcp tests
mypy moodle_mcp
pytest
```

The test suite covers token safety (credential confinement, cross-host refusal), prompt-injection sanitisation, resilience and robustness, plus Hypothesis property tests for file names, URL validation and content sanitisation. The base modules (`config`, `confirm`, `login`, `sanitize`, `transport`, `vault`) are type-checked with strict mypy flags. CI runs lint, mypy, `uv lock --check` and tests on Linux, Windows and macOS (Python 3.11–3.13), plus `pip-audit`.

Releases are published to PyPI from a `vX.Y.Z` tag via trusted publishing (no stored token) with provenance attestations; see [CHANGELOG.md](CHANGELOG.md).

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md) — never in public issues or group chats.

## License

MIT — see [LICENSE](LICENSE). The optional `[pdf]` extra installs PyMuPDF, which is **AGPL-3.0** and carries its own obligations.
