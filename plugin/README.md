# Moodle + Obsidian

Gives Claude read access to your **Moodle** courses and keeps an **Obsidian** vault in sync with them: a dashboard with every course, grade and deadline, one note per course, a "what changed" note after each sync, a `.ics` calendar of deadlines and Obsidian Tasks. It works with universities that log in through SSO (Entra ID, SAML, Shibboleth, CAS), where students can't create an API token by hand.

This plugin runs the open-source MCP server [moodle-obsidian-mcp](https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp) on your computer and adds a `moodle-weekly-review` skill.

## Setup

In Claude Code you can also install it straight from the repository:

```text
/plugin install moodle-obsidian --marketplace CarlosAndreFerreira/moodle-obsidian-mcp
```

1. When you enable the plugin, choose your Obsidian vault folder and the note language.
2. Log in to Moodle once. Install the command-line tool and start the browser login (SSO included):

   ```bash
   uv tool install "moodle-obsidian-mcp[pdf]"
   moodle-mcp --install-handler
   moodle-mcp --login
   ```

   The token is stored in your user config folder with `0600` permissions and never passes through Claude. If your Moodle shows you a token, you can paste it in the plugin settings instead.

## What it runs, sends and writes

- **Runs** `uvx moodle-obsidian-mcp[pdf]==<version>`, which downloads the package from PyPI the first time and runs it locally over stdio.
- **Sends** requests only to the Moodle address you configured, with your token. There is no telemetry and no other server.
- **Writes** only inside the vault folder you chose (notes, the `.ics` calendar, downloaded course materials, and a `.moodle-mcp/` state folder).
- **Read-only by default.** Tools that act on Moodle (submitting, posting, messaging) are off unless you enable the `write` toolset, and even then each action asks for your confirmation.

Full documentation, security notes and the changelog: <https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp>.

Moodle and Obsidian are trademarks of their respective owners. This plugin is an independent project, not affiliated with or endorsed by either.
