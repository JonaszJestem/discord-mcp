# Discord MCP

An [MCP server](https://modelcontextprotocol.io) that lets Claude and other LLM assistants browse your Discord: list servers, read messages, and (optionally) send messages. It drives a real browser session under the hood, so there's no bot token or special permissions needed — the LLM sees exactly the channels you see.

> **Your Discord password never touches this tool.** You log in once in a real browser window, the session is encrypted with a key stored in your OS keyring, and only ciphertext is ever written to disk. See [Security](#security) below.

---

## What it can do

**Read-side (always available):**

| Tool | What it does |
|---|---|
| `get_servers` | Lists the Discord servers (guilds) you're in |
| `get_channels` | Lists channels in a specific server |
| `read_messages` | Reads recent messages from a channel (time-filtered) |
| `get_pinned_messages` | Lists pinned messages in a channel (often the rules/summary) |
| `get_threads` | Lists active threads in a channel |
| `read_thread` | Reads messages inside a specific thread |
| `search_messages` | Searches messages in a server (optionally scoped to one channel) |
| `get_mentions` | Lists recent @mentions of you across all servers |

**Write-side (only when `DISCORD_READ_ONLY=false`):**

| Tool | What it does |
|---|---|
| `send_message` | Sends a message to a channel |
| `reply_to_message` | Replies to a specific message (creates a Discord reply ref) |
| `react_to_message` | Adds an emoji reaction (`thumbsup`, `eyes`, etc.) |

By default the server is **read-only**. To enable write-side tools, set `DISCORD_READ_ONLY=false` in the MCP client config.

---

## Security

No password is ever asked for, typed, or stored by this tool. Here's what actually happens:

1. You run `discord-mcp login` once. A real Chromium window opens pointing at `discord.com/login`.
2. You log in yourself — with your password, fingerprint, security key, or whatever 2FA you have.
3. Once you're logged in, the tool captures the browser's session cookies and encrypts them with AES-256-GCM.
4. The ciphertext goes to `~/.config/discord-mcp/session.enc` (mode `0600`). The encryption key goes to your **OS keyring** (macOS Keychain, Windows Credential Manager, or GNOME Keyring/KWallet on Linux).
5. The MCP server decrypts this at startup to use the session. It never has your password — because your password never entered the program.

**Leak scenarios:**

| If this leaks | What the attacker gets |
|---|---|
| The `session.enc` file alone | Nothing — useless without the keyring key |
| Your OS keyring alone | Nothing — useless without the ciphertext |
| Both, from a full local compromise | Your current Discord session (revokable via *Settings → Devices* on Discord) |
| The MCP source code / config | Nothing — no credentials exist to leak |

Sessions are time-bounded. If yours expires, run `discord-mcp login` again.

---

## Install

### Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/)
- **A working OS keyring.** macOS and Windows have one built in. On Linux, install `gnome-keyring` or `kwallet`.

### One-liner (recommended)

Clone the repo once — you'll run commands from inside the directory:

```bash
git clone https://github.com/JonaszJestem/discord-mcp.git
cd discord-mcp
uv sync
uv run playwright install chromium
```

### Log in to Discord

```bash
uv run discord-mcp login
```

A Chromium window will open. Log in to Discord as you normally would — use your password, complete any 2FA (fingerprint, authenticator app, email code). When you see the Discord sidebar, the tool will save your session automatically and close the window.

Verify it worked:

```bash
uv run discord-mcp status
```

You should see `Logged in.`

---

## Connect to Claude Code

Edit `~/.claude.json` (your Claude Code config) and add to the `mcpServers` section:

```json
{
  "mcpServers": {
    "discord": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/discord-mcp",
        "discord-mcp",
        "serve"
      ],
      "env": {
        "DISCORD_HEADLESS": "true",
        "DISCORD_READ_ONLY": "true"
      }
    }
  }
}
```

Replace `/absolute/path/to/discord-mcp` with where you cloned the repo. Restart Claude Code. The Discord tools should now appear.

To also allow sending, flip `DISCORD_READ_ONLY` to `"false"`.

---

## Connect to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) — same block:

```json
{
  "mcpServers": {
    "discord": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/discord-mcp",
        "discord-mcp",
        "serve"
      ],
      "env": {
        "DISCORD_HEADLESS": "true",
        "DISCORD_READ_ONLY": "true"
      }
    }
  }
}
```

Restart Claude Desktop.

---

## Example prompts

Once connected, try any of these in Claude:

- *"List my Discord servers."*
- *"Show me channels in the {name} server."*
- *"Summarize the last 24 hours from the #announcements channel."*
- *"What important things happened this week across my servers?"*
- *"What pinned messages does the #rules channel have?"*
- *"Any new @mentions I should know about?"*
- *"Search for 'deploy' in the #devops channel last month."*
- *"What active threads are in #engineering?"*

If you have `DISCORD_READ_ONLY=false`:

- *"Send 'running 5 min late' to the #standup channel."*
- *"React with 👀 to that last message."*
- *"Reply 'on it' to the announcement."*

---

## CLI commands

```bash
discord-mcp login    # Interactive browser login, save session
discord-mcp status   # Show whether a session is stored
discord-mcp logout   # Delete the encrypted session and keyring key
discord-mcp serve    # Run the MCP stdio server (MCP clients call this)
```

---

## Configuration

All configuration is via environment variables (set in your MCP client config or shell):

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_HEADLESS` | `true` | Run Playwright headless. Always keep this `true` in production |
| `DISCORD_READ_ONLY` | `true` | Hide `send_message` from the tool list |
| `DISCORD_POOL_SIZE` | `4` | How many browsers stay warm. Higher = more parallelism, more RAM |
| `DISCORD_MCP_LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose logs (goes to stderr) |
| `XDG_CONFIG_HOME` | `~/.config` | Where `session.enc` lives |

---

## Troubleshooting

**"Not logged in"** — Run `discord-mcp login`. If the browser opens but the tool never detects success, make sure you end up on `discord.com/channels/@me` (the main Discord view).

**Browser fails to launch** — Run `uv run playwright install chromium` once more to re-download the browser.

**"No OS keyring is available"** — On Linux, install `gnome-keyring` (`sudo apt install gnome-keyring` on Debian/Ubuntu, `sudo pacman -S gnome-keyring` on Arch) or `kwallet`. Make sure your desktop session unlocked it. On a headless server, you'll need to set up a keyring backend manually.

**Session expired mid-use** — Discord periodically invalidates sessions. Run `discord-mcp login` again.

**"Discord session is invalid or expired"** when calling a tool — Same fix as above.

**Tool calls are slow** — The first call after server startup spins up a Chromium instance (~5 seconds). Subsequent calls reuse the pool and are much faster.

---

## Legal / terms of service

This tool automates a **regular user account** by driving a real browser. That mode of automation is against [Discord's Terms of Service](https://discord.com/terms) (they prohibit "self-bots"). Discord can suspend accounts detected doing this.

Use at your own risk, and consider:

- Only using accounts you own.
- Not using this for spam, mass messaging, or anything that would draw Discord's automated attention.
- Not using this on accounts that matter to you (paid Nitro, community moderator, etc.).

For production use cases, build a real Discord bot with OAuth — that's the sanctioned path.

---

## Development

```bash
uv run pyright       # Strict type-check (src/ must be clean)
uv run pytest        # Unit tests (no Discord calls, fully offline)
uvx ruff format .
uvx ruff check . --fix
```

Architecture overview:

```
src/discord_mcp/
├── errors.py          # Typed error hierarchy
├── models.py          # Guild, Channel, Message, Snowflake
├── config.py          # Single Config object
├── auth/              # KeyVault + SessionStore protocols, AuthService
├── discord/           # Generic BrowserPool, DiscordBrowserDriver, DiscordService
├── cli.py             # Subcommand entry + composition root
└── mcp_server.py      # Thin FastMCP wiring
```

Everything is Protocol-based where it crosses an I/O boundary, so the auth layer, browser pool, and keyring can be swapped for alternatives without touching the tools.

---

## License

MIT
