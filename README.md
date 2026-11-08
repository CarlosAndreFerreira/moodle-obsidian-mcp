# moodle-mcp

[![PyPI](https://img.shields.io/pypi/v/moodle-obsidian-mcp)](https://pypi.org/project/moodle-obsidian-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/moodle-obsidian-mcp)](https://pypi.org/project/moodle-obsidian-mcp/)
[![CI](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/blob/main/LICENSE)
![MCP](https://img.shields.io/badge/MCP-server-purple)

A **Model Context Protocol** server that gives AI assistants (Claude, opencode, any MCP client) safe access to **Moodle**, and syncs your courses, grades, deadlines and materials into an **Obsidian** vault.

Works with any Moodle that has Web Services and the mobile app service enabled — **including universities behind federated SSO** (Entra ID, SAML, Shibboleth, CAS), where students can't generate an API token by hand.

🇵🇹 [Versão em português](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/blob/main/README.pt.md)

---

## Why this one

Most MCP servers for third-party platforms pipe untrusted content straight into the model and expose every write action by default. Moodle is a worst case for that: any participant in a course can write forum posts the model will read, and actions like submitting an assignment can't be undone. This server is designed around that threat model:

- **Read-only by default.** Write tools (submissions, forum posts, messages) and the raw Web Services API are off unless you enable them explicitly. 18 of the 22 default tools carry `readOnlyHint`, so a client can auto-approve them without auto-approving anything irreversible.
- **Prompt-injection defenses on the server side** (OWASP LLM01), not delegated to the model:
  - hidden content is stripped from every Moodle response — `display:none`, `font-size:0`, `hidden`, HTML comments, Unicode tag characters, zero-width and bidi control characters — so the model reads what the student sees;
  - notes written to the vault can't execute anything: `dataviewjs`/`dataview` blocks, inline Dataview and Templater commands are neutralised, remote images become links (no tracking pixels);
  - every Moodle write requires **user confirmation via MCP elicitation**, showing the exact recipient and text — even when the client is set to auto-approve.
- **Token confinement.** Credentials never appear in tool responses. The token is only ever attached to `pluginfile.php` URLs on the configured instance; any other host is refused before a request is made. Tools that can overwrite credentials are off by default, so a malicious forum post can't swap your token.
- **Context-budget aware.** The full tool set costs ~9,700 tokens per request; selectable toolsets cut that to ~1,750–5,500.

## Features

- **SSO token capture** — `moodle-mcp --login` starts the mobile-app login in your browser with a single-use passport; a `moodlemobile://` URL handler receives the redirect, checks Moodle's signature and stores the token (`0600`). Registered natively on Linux (`xdg-mime`), Windows (`HKEY_CURRENT_USER`, no admin) and macOS (an AppleScript applet registered with Launch Services). The token never passes through the model, and the handler moves the URL out of its command-line arguments at once, so other local users can't read it with `ps`.
- **Obsidian dashboard sync** — a central note with every course, progress, current grades and a sorted deadline calendar; one note per course with grade report, per-assignment submission status, assignment briefs converted to Markdown and the resource tree.
- **What changed** — each sync compares with the previous one and writes a note (and `get_recent_changes` returns it, without calling Moodle): new grades, changed deadlines, new assignments and quizzes, new materials, new forum topics and topics with new replies.
- **Deadlines everywhere** — a `.ics` calendar next to the dashboard (import or subscribe in Google Calendar, Outlook, Thunderbird or your phone; a reminder the day before), and an Obsidian Tasks list (`- [ ] … 📅 YYYY-MM-DD`) in the dashboard.
- **Scheduled sync** — `moodle-mcp --schedule 6` syncs every 6 hours with the OS scheduler (systemd timer, launchd or Windows Task Scheduler), no MCP client running. `moodle-mcp --sync` runs one sync now.
- **English or Portuguese notes** — `moodle-mcp --set language=en` switches the text of every generated note; folders stay put.
- **MCP resources and prompts** — `moodle://changes`, `moodle://deadlines` and `moodle://dashboard` serve the last sync without calling Moodle; the `weekly_review`, `prepare_assignment` and `forum_catch_up` prompts are ready-made flows. The Vault tools return typed structured output (`outputSchema`).
- **Course material download** — PDFs, slides and HTML pages saved into the vault, with each PDF converted to a searchable `.pdf.md` that agents can read (optional `[pdf]` extra).
- **33 tools** across courses, books and pages, completion, grades, assignments and submissions, quizzes, forums (search, export to Obsidian, reply), messages and calendar.
- **Multi-instance** — an open registry of Moodle instances; when more than one is configured, the `instance` parameter is exposed as an **enum in the schema**, so the model gets the valid names instead of free text.

## Install

The package is on PyPI as [`moodle-obsidian-mcp`](https://pypi.org/project/moodle-obsidian-mcp/); it installs two commands, `moodle-mcp` and `moodle-mcp-token-catcher`.

```bash
uv tool install "moodle-obsidian-mcp[pdf]"     # or: pipx install "moodle-obsidian-mcp[pdf]"
moodle-mcp --install-handler                    # once: register the moodlemobile:// handler
moodle-mcp --login                              # log in through your browser (SSO works)
```

Upgrade with `uv tool upgrade moodle-obsidian-mcp` (or `pipx upgrade moodle-obsidian-mcp`).

Use a permanent install (`uv tool` or `pipx`) rather than `uvx`: the login handler is registered with the executable's path, and `uvx` runs from a cache that can be wiped. To try it without installing:

```bash
uvx --from moodle-obsidian-mcp moodle-mcp --show-config
```

From a clone, `./install.sh [vault-path]` does the permanent install, sets the vault, registers the handler and tells you to run `moodle-mcp --login`. For development, see [Development](#development).

The `[pdf]` extra adds PDF text extraction via PyMuPDF. It's kept out of the base install because PyMuPDF is **AGPL-3.0**; without it everything works, PDF downloads just don't get the companion `.md`.

`moodle-mcp --uninstall-handler` removes the `moodlemobile://` handler again.

## Reliability

- **Your notes are yours.** Generated content lives between `%% moodle-mcp:start %%` and `%% moodle-mcp:end %%`; anything you write outside that block, and any frontmatter properties you add, survive every sync. A note from an older version (without the markers) is copied to `.moodle-mcp/backup/` in the vault before its first rewrite.
- **Incremental downloads.** Each file's `timemodified` and size are recorded in `.moodle-mcp/state-<instance>.json` in the vault; files that haven't changed in Moodle and are still in the vault aren't downloaded again.
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
moodle-mcp --set sync_subdir=Moodle --set instances.utad.url=https://e-learning.utad.pt
moodle-mcp --set language=en  # note text in English (default: pt)
```

Note options: `language` (`pt`/`en`), `dashboard_name` (default: an existing `Doutoramento - Painel Moodle` note, else `Painel Moodle`/`Moodle Dashboard`), `courses_dir` (default `Unidades Curriculares`), `sync_subdir`.

### Scheduled sync

```bash
moodle-mcp --sync                    # sync every instance with a token, now
moodle-mcp --schedule 6              # every 6 hours (1–24); add --materials to download files too
moodle-mcp --unschedule
```

The task runs `moodle-mcp --sync` and reads the same `config.toml` and `credentials.json`; no token goes into the task files or the command line. The scheduler doesn't see your shell or MCP client environment, so put the vault in `config.toml` (`moodle-mcp --set obsidian_vault_dir=…`) rather than only in the client's `env`. The result of the last run is in `last_sync.log`, in the config directory.

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
| **(default)** | **22** | **~5,500** | **no** |
| `core,grades,forums,obsidian,write` | 29 | ~7,900 | yes |
| `all` | 33 | ~9,700 | yes, plus the raw API |
| `core,obsidian` | 13 | ~3,700 | no |
| `core` | 8 | ~1,750 | no |

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

If the client doesn't inherit your shell's `PATH`, use the absolute path of `moodle-mcp` as the command (`~/.local/bin/moodle-mcp` with `uv tool` or `pipx` on Linux and macOS; see `uv tool dir --bin`).

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
git clone https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp.git
cd moodle-obsidian-mcp
uv pip install -e ".[dev,pdf]"
ruff check moodle_mcp tests && ruff format --check moodle_mcp tests
mypy moodle_mcp
pytest
```

The test suite covers token safety (credential confinement, cross-host refusal), prompt-injection sanitisation, resilience and robustness, plus Hypothesis property tests for file names, URL validation and content sanitisation. The base modules (`config`, `confirm`, `login`, `sanitize`, `transport`, `vault`) are type-checked with strict mypy flags. CI runs lint, mypy, `uv lock --check` and tests on Linux, Windows and macOS (Python 3.11–3.13), plus `pip-audit`.

Releases are published to PyPI from a `vX.Y.Z` tag via trusted publishing (no stored token) with provenance attestations: bump the version in `pyproject.toml` and `moodle_mcp/__init__.py`, add a [CHANGELOG](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/blob/main/CHANGELOG.md) entry, merge, push the tag and approve the `pypi` deployment.

## Security

Report vulnerabilities privately as described in [SECURITY.md](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/blob/main/SECURITY.md) — never in public issues or group chats.

## License

MIT — see [LICENSE](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/blob/main/LICENSE). The optional `[pdf]` extra installs PyMuPDF, which is **AGPL-3.0** and carries its own obligations.
