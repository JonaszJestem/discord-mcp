# Discord MCP — project context for Claude

## Purpose

An MCP server that drives a real Discord browser session (via Playwright) so an LLM can list servers/channels, read messages, and optionally send messages. No Discord API, no bot tokens — just the user's own authenticated browser session.

## Security posture (non-negotiable)

- **No password handling anywhere in the code.** Users log in manually via `discord-mcp login`; the tool only ever sees post-login session cookies.
- Session is AES-256-GCM encrypted. Key lives in the OS keyring, ciphertext in `~/.config/discord-mcp/session.enc` (mode `0600`).
- Any change that introduces a password prompt, saves credentials to disk, or weakens the encryption scheme is a regression — push back.

## Architecture

Layered, protocol-oriented:

```
src/discord_mcp/
├── errors.py          # Typed error hierarchy (DiscordMcpError base)
├── models.py          # Snowflake (NewType + smart ctor), Guild/Channel/Message
├── config.py          # Single Config.load(), no os.getenv elsewhere
├── logger.py          # stderr-only logger (stdout is the MCP stdio channel)
├── auth/
│   ├── keyring_vault.py   # KeyVault protocol + OSKeyringVault
│   ├── session_store.py   # SessionStore protocol + EncryptedFileSessionStore
│   └── service.py         # AuthService: interactive login/logout/status
├── discord/
│   ├── browser_pool.py    # Generic BrowserPool[T: Closable]; broken-eviction
│   ├── browser_driver.py  # Only place that talks to Playwright/Discord DOM
│   └── service.py         # DiscordService: use cases, pool discipline
├── cli.py                 # Subcommand dispatch + composition root
└── mcp_server.py          # Thin FastMCP wiring
```

Dependencies go **inward**: `mcp_server` → `DiscordService` → `BrowserPool` + `DiscordBrowserDriver`. Nothing in `domain`, `auth`, or `discord/service` imports from `mcp_server` or `cli`.

## CLI entry points

```
discord-mcp serve    # MCP stdio server — what MCP clients invoke
discord-mcp login    # Headful browser; user signs in; session encrypted + saved
discord-mcp logout   # Clear session file + keyring key
discord-mcp status   # Report whether a session is stored
```

## Tooling

- `uv` for package management; always prefix Python commands with `uv run`
- `uv run pyright` — **strict mode** on `src/discord_mcp/`; must stay at 0 errors
- `uv run pytest` — unit tests; 55+ tests covering domain, auth, pool, config, chunking. No Discord calls in the test suite — those would be flaky.
- `uvx ruff format .` and `uvx ruff check . --fix`

## Conventions

- **Type safety**: every function typed. Use Protocol for I/O boundaries so impls are swappable (KeyVault, SessionStore). Use PEP 695 generics (`class BrowserPool[T]`, `def _with_driver[T]`).
- **Errors**: raise typed subclasses of `DiscordMcpError`. The MCP server catches and converts to structured `{error, message, action}` dicts so the LLM can surface them.
- **Snowflakes**: never accept a raw `str` for a Discord ID. Call `snowflake(value, field=...)` at the edge; from then on types carry the guarantee.
- **No `os.getenv` outside `config.py`**. All env reads go through `Config.load()`.
- **Logging**: always `logger.debug/info/...` to stderr. Never `print()` from library code (corrupts MCP stdio).

## What tests cover

- Domain: snowflake validation (accept/reject), model immutability
- Auth: session round-trips, missing/corrupt/wrong-key paths, file perms (`0600`), directory perms (`0700`), atomic writes, ciphertext opacity
- Keyring: `OSKeyringVault` against fake/failing backends — never touches the real keyring
- Pool: lazy creation, reuse, broken-eviction, blocking at capacity, close-all, post-close acquire
- Config: defaults, env parsing, XDG path, integer clamping, graceful bad input
- MCP server: message chunking (newline/word/single-word-longer-than-limit), error-to-dict shape

No integration tests hit real Discord — they're too flaky and the encrypted session makes them hard to share. Manual E2E: `uv run discord-mcp login` then a smoke script that calls `DiscordService.list_guilds()`.

## Publishing / distribution

Public repo at `github.com/JonaszJestem/discord-mcp`. Users install by cloning and running `uv sync && uv run playwright install chromium`, then `uv run discord-mcp login`.

## Gotchas

- Playwright ships a Chromium build; `uv run playwright install chromium` is a one-time setup step.
- On Linux without GNOME Keyring or KWallet, the tool fails fast with `KeyringUnavailable` — document this, don't try to work around it (silent fallback would defeat the security model).
- Discord automation of user accounts violates ToS. README says this loudly. Don't add features that make it more bot-like (mass-DM, scheduled sends) — they're detection triggers.
