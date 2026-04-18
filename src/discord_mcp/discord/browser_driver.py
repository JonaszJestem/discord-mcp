"""DiscordBrowserDriver: owns a logged-in Playwright browser and scrapes it.

The driver is the only place that touches Playwright APIs or Discord's DOM.
Everything else in the codebase speaks domain types (Guild, Channel, Message).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, cast

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    async_playwright,
)

from ..errors import SessionExpired
from ..logger import logger
from ..models import Channel, Guild, Message, SessionData, Snowflake, Thread, snowflake
from .api_client import ApiUnavailable, DiscordApiClient


_LOGGED_IN_SELECTOR = '[data-list-id="guildsnav"] [role="treeitem"]'
_CHAT_MESSAGES_SELECTOR = '[data-list-id="chat-messages"]'
_MESSAGE_INPUT_SELECTOR = '[data-slate-editor="true"]'


class DiscordBrowserDriver:
    """A browser session logged into Discord, owning all Playwright handles."""

    def __init__(
        self,
        pw: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
    ) -> None:
        self._pw = pw
        self._browser = browser
        self._context = context
        self._page = page
        self._api = DiscordApiClient(page)

    @classmethod
    async def create(
        cls, session: SessionData, *, headless: bool
    ) -> "DiscordBrowserDriver":
        """Launch a browser with the given session and verify it's still valid."""
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=session)  # type: ignore[arg-type]
        page = await context.new_page()
        driver = cls(pw, browser, context, page)

        if not await driver._is_logged_in():
            await driver.close()
            raise SessionExpired(
                "Discord session is invalid or expired. "
                "Run `discord-mcp login` to re-authenticate."
            )
        return driver

    async def close(self) -> None:
        # Close in reverse order of creation; swallow individual failures.
        for closer in (
            self._page.close,
            self._context.close,
            self._browser.close,
            self._pw.stop,
        ):
            try:
                await closer()
            except Exception as e:
                logger.debug(f"Error during driver close: {e}")

    # ---------------------------------------------------------------- queries

    async def list_guilds(self) -> list[Guild]:
        page = self._page
        logger.debug("Listing guilds")
        await page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )
        try:
            await page.wait_for_selector(
                _LOGGED_IN_SELECTOR, state="visible", timeout=15000
            )
            await page.wait_for_timeout(3000)
            await page.evaluate(_SCROLL_GUILDS_JS)
            await page.wait_for_timeout(2000)
            # Expand any collapsed folders so their nested guilds enter the DOM.
            # Discord only renders folder children when the folder is open.
            await page.evaluate(_EXPAND_FOLDERS_JS)
            await page.wait_for_timeout(1500)
        except Exception as e:
            logger.debug(f"Guild list prep failed (continuing): {e}")

        raw = await page.evaluate(_EXTRACT_GUILDS_JS)
        return [
            Guild(id=snowflake(g["id"], field="guild_id"), name=g["name"]) for g in raw
        ]

    async def list_channels(self, guild_id: Snowflake) -> list[Channel]:
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(3000)

        original = await page.evaluate(_extract_channels_js(guild_id))
        browse: list[dict[str, str]] = []
        try:
            browse_el = await page.query_selector('*:has-text("Browse Channels")')
            if browse_el and await browse_el.is_visible():
                await browse_el.click()
                await page.wait_for_timeout(5000)
                await page.evaluate(_SCROLL_ALL_JS)
                await page.wait_for_timeout(3000)
                browse = await page.evaluate(_extract_channels_js(guild_id))
        except Exception as e:
            logger.debug(f"Browse Channels failed: {e}")

        seen: set[str] = set()
        merged: list[dict[str, str]] = []
        for ch in [*original, *browse]:
            if ch["id"] in seen:
                continue
            seen.add(ch["id"])
            merged.append(ch)

        return [
            Channel(
                id=snowflake(ch["id"], field="channel_id"),
                name=ch["name"],
                type=0,
                guild_id=guild_id,
            )
            for ch in merged
        ]

    async def read_messages(
        self,
        guild_id: Snowflake,
        channel_id: Snowflake,
        *,
        limit: int,
    ) -> list[Message]:
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}/{channel_id}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(_CHAT_MESSAGES_SELECTOR, timeout=15000)
        await page.evaluate(_SCROLL_CHAT_TO_BOTTOM_JS)
        await page.wait_for_timeout(2000)

        messages: list[Message] = []
        seen_ids: set[str] = set()
        for _ in range(10):
            elements = await page.query_selector_all(
                f'{_CHAT_MESSAGES_SELECTOR} [id^="chat-messages-"]'
            )
            if not elements:
                await page.keyboard.press("PageUp")
                await page.wait_for_timeout(1000)
                continue

            for element in reversed(elements):
                if len(messages) >= limit:
                    break
                msg = await _extract_message(element, channel_id)
                if msg is None or msg.id in seen_ids:
                    continue
                seen_ids.add(msg.id)
                messages.append(msg)

            if len(messages) >= limit:
                break
            await page.keyboard.press("PageUp")
            await page.wait_for_timeout(1000)

        messages.sort(key=lambda m: m.timestamp, reverse=True)
        return messages[:limit]

    async def send_message(
        self, guild_id: Snowflake, channel_id: Snowflake, content: str
    ) -> str:
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}/{channel_id}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(_MESSAGE_INPUT_SELECTOR, timeout=10000)

        input_el = await page.query_selector(_MESSAGE_INPUT_SELECTOR)
        if input_el is None:
            raise RuntimeError("Could not find Discord message input")
        await input_el.fill(content)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)
        return f"sent-{int(datetime.now().timestamp())}"

    async def list_pinned(
        self, guild_id: Snowflake, channel_id: Snowflake
    ) -> list[Message]:
        """List pinned messages via Discord's internal API."""
        _ = guild_id  # accepted for symmetry; API endpoint is channel-scoped
        await self._ensure_discord_origin()
        try:
            payload = await self._api.fetch_json(f"/api/v9/channels/{channel_id}/pins")
        except ApiUnavailable as e:
            logger.debug(f"Pins API failed: {e}")
            return []
        if not isinstance(payload, list):
            return []
        items = cast(list[Any], payload)
        return [_message_from_api(m, channel_id) for m in items]

    async def list_threads(
        self, guild_id: Snowflake, channel_id: Snowflake
    ) -> list[Thread]:
        """List active threads parented at `channel_id` via Discord's API.

        The active-threads endpoint returns every thread in the guild; we
        filter client-side by parent_id to match the tool's contract.
        """
        await self._ensure_discord_origin()
        try:
            payload = await self._api.fetch_json(
                f"/api/v9/guilds/{guild_id}/threads/active"
            )
        except ApiUnavailable as e:
            logger.debug(f"Threads API failed: {e}")
            return []
        if not isinstance(payload, dict):
            return []
        raw_threads_any = cast(dict[str, Any], payload).get("threads")
        if not isinstance(raw_threads_any, list):
            return []
        raw_threads = cast(list[Any], raw_threads_any)

        results: list[Thread] = []
        for t in raw_threads:
            if not isinstance(t, dict):
                continue
            t_dict = cast(dict[str, Any], t)
            if t_dict.get("parent_id") != channel_id:
                continue
            raw_id = t_dict.get("id")
            if not isinstance(raw_id, str):
                continue
            try:
                tid = snowflake(raw_id, field="thread_id")
            except Exception:
                continue
            name = t_dict.get("name")
            results.append(
                Thread(
                    id=tid,
                    name=name if isinstance(name, str) else f"thread-{tid}",
                    parent_channel_id=channel_id,
                )
            )
        return results

    async def search_messages(
        self,
        guild_id: Snowflake,
        query: str,
        *,
        channel_id: Snowflake | None,
        limit: int,
    ) -> list[Message]:
        """Search messages via Discord's guild search API."""
        await self._ensure_discord_origin()
        params = [f"content={_url_encode(query)}"]
        if channel_id is not None:
            params.append(f"channel_id={channel_id}")
        path = f"/api/v9/guilds/{guild_id}/messages/search?" + "&".join(params)
        try:
            payload = await self._api.fetch_json(path)
        except ApiUnavailable as e:
            logger.debug(f"Search API failed: {e}")
            return []

        # Response shape: { messages: [[msg, context1, ...], ...], total_results }
        raw_groups_any: Any = None
        if isinstance(payload, dict):
            raw_groups_any = cast(dict[str, Any], payload).get("messages")
        raw_groups: list[Any] = (
            cast(list[Any], raw_groups_any) if isinstance(raw_groups_any, list) else []
        )
        placeholder = (
            channel_id
            if channel_id is not None
            else snowflake("0" * 17, field="channel_id")
        )
        results: list[Message] = []
        seen: set[str] = set()
        for group_any in raw_groups:
            if len(results) >= limit:
                break
            if not isinstance(group_any, list):
                continue
            group = cast(list[Any], group_any)
            match: dict[str, Any] | None = None
            for item in group:
                if isinstance(item, dict):
                    item_dict = cast(dict[str, Any], item)
                    if item_dict.get("hit"):
                        match = item_dict
                        break
            if match is None and group:
                first = group[0]
                if isinstance(first, dict):
                    match = cast(dict[str, Any], first)
            if match is None:
                continue
            msg = _message_from_api(match, placeholder)
            if msg.id in seen:
                continue
            seen.add(msg.id)
            results.append(msg)
        return results

    async def list_mentions(self, *, limit: int) -> list[Message]:
        """Fetch recent @mentions via Discord's API."""
        await self._ensure_discord_origin()
        try:
            payload = await self._api.fetch_json(
                f"/api/v9/users/@me/mentions?limit={limit}"
            )
        except ApiUnavailable as e:
            logger.debug(f"Mentions API failed: {e}")
            return []
        if not isinstance(payload, list):
            return []
        items = cast(list[Any], payload)
        placeholder = snowflake("0" * 17, field="channel_id")
        return [_message_from_api(m, placeholder) for m in items[:limit]]

    async def reply_to_message(
        self,
        guild_id: Snowflake,
        channel_id: Snowflake,
        message_id: str,
        content: str,
    ) -> str:
        """Reply in-thread to a specific message. Requires the message to be in view."""
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}/{channel_id}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(_CHAT_MESSAGES_SELECTOR, timeout=15000)

        msg_el = await page.query_selector(f"#chat-messages-{channel_id}-{message_id}")
        if msg_el is None:
            raise RuntimeError(
                f"Message {message_id} not in view. Scroll-back lookup not implemented."
            )
        await msg_el.hover()
        await page.wait_for_timeout(500)

        reply_btn = await page.query_selector('[aria-label="Reply" i]')
        if reply_btn is None:
            raise RuntimeError("Could not find Reply button on hovered message")
        await reply_btn.click()
        await page.wait_for_timeout(500)

        input_el = await page.query_selector(_MESSAGE_INPUT_SELECTOR)
        if input_el is None:
            raise RuntimeError("Message input not found after opening reply")
        await input_el.fill(content)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)
        return f"replied-{int(datetime.now().timestamp())}"

    async def react_to_message(
        self,
        guild_id: Snowflake,
        channel_id: Snowflake,
        message_id: str,
        emoji: str,
    ) -> None:
        """Add an emoji reaction to a message. `emoji` is the name (e.g. 'thumbsup')."""
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}/{channel_id}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(_CHAT_MESSAGES_SELECTOR, timeout=15000)

        msg_el = await page.query_selector(f"#chat-messages-{channel_id}-{message_id}")
        if msg_el is None:
            raise RuntimeError(
                f"Message {message_id} not in view. Scroll-back lookup not implemented."
            )
        await msg_el.hover()
        await page.wait_for_timeout(500)

        react_btn = await page.query_selector(
            '[aria-label="Add Reaction" i], [aria-label*="reaction" i]'
        )
        if react_btn is None:
            raise RuntimeError("Could not find Add Reaction button")
        await react_btn.click()
        await page.wait_for_timeout(800)

        search = await page.query_selector('input[placeholder*="emoji" i]')
        if search is None:
            raise RuntimeError("Emoji search input not found")
        await search.fill(emoji)
        await page.wait_for_timeout(800)

        first = await page.query_selector(
            '[role="gridcell"] [role="button"], [role="button"][aria-label*="emoji" i]'
        )
        if first is None:
            raise RuntimeError(f"No emoji match for {emoji!r}")
        await first.click()
        await asyncio.sleep(1)

    # --------------------------------------------------------------- internal

    async def _ensure_discord_origin(self) -> None:
        """Ensure the page is on a discord.com origin before an API fetch.

        Discord's `/api/v9/...` endpoints reject requests from other origins,
        and `fetch()` uses the page's current origin. We pin to @me — cheap
        if we're already there, one navigation otherwise.
        """
        if self._page.url.startswith("https://discord.com/"):
            return
        await self._page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )

    async def _is_logged_in(self) -> bool:
        page = self._page
        try:
            await page.goto(
                "https://discord.com/channels/@me", wait_until="domcontentloaded"
            )
            await page.wait_for_selector(
                _LOGGED_IN_SELECTOR, state="visible", timeout=15000
            )
            url = page.url
            return (
                "/login" not in url
                and "/register" not in url
                and "/channels/@me" in url
            )
        except Exception as e:
            logger.debug(f"Login check failed: {e}")
            return False


# --------------------------------------------------------------------- JS

_SCROLL_GUILDS_JS = """
() => {
    const guildNav = document.querySelector('[data-list-id="guildsnav"]');
    const container = guildNav?.closest('[class*="guilds"]') || guildNav?.parentElement;
    if (!container) return;
    container.scrollTop = 0;
    return new Promise(resolve => {
        let scrolls = 0;
        const interval = setInterval(() => {
            container.scrollBy(0, 100);
            if (++scrolls >= 20 || container.scrollTop + container.clientHeight >= container.scrollHeight - 10) {
                clearInterval(interval);
                resolve();
            }
        }, 100);
    });
}
"""

_EXPAND_FOLDERS_JS = """
() => {
    // Click any collapsed tree items in the guild nav — those are folders.
    // Regular guild tiles don't have aria-expanded, so this only hits folders.
    const folders = document.querySelectorAll(
        '[data-list-id="guildsnav"] [role="treeitem"][aria-expanded="false"]'
    );
    folders.forEach(f => f.click());
    return folders.length;
}
"""

_EXTRACT_GUILDS_JS = """
() => {
    const guilds = [];
    const treeItems = document.querySelectorAll('[data-list-id="guildsnav"] [role="treeitem"]');
    treeItems.forEach(item => {
        const listItemId = item.getAttribute('data-list-item-id');
        if (!listItemId?.startsWith('guildsnav___') || listItemId === 'guildsnav___home') return;
        const guildId = listItemId.replace('guildsnav___', '');
        // Real Discord snowflakes are 17+ digits. Shorter numeric IDs
        // (e.g. folder container IDs) should be skipped.
        if (!/^[0-9]{17,}$/.test(guildId)) return;
        let name = null;
        for (const elem of item.querySelectorAll('*')) {
            const text = elem.textContent?.trim();
            if (text && text.length > 2 && text.length < 100 &&
                !text.includes('notification') && !text.includes('unread') &&
                !text.match(/^\\d+$/)) {
                name = text;
                break;
            }
        }
        if (!name) {
            const fullText = item.textContent?.trim();
            if (fullText) name = fullText.replace(/\\s+/g, ' ').trim();
        }
        if (name) name = name.replace(/^\\d+\\s+mentions?,\\s*/, '').trim();
        if (name && !guilds.some(g => g.id === guildId)) {
            guilds.push({ id: guildId, name });
        }
    });
    return guilds;
}
"""

_SCROLL_ALL_JS = """
() => Array.from(document.querySelectorAll('*'))
    .filter(el => el.scrollHeight > el.clientHeight + 5)
    .forEach(el => el.scrollTop = el.scrollHeight)
"""

_SCROLL_CHAT_TO_BOTTOM_JS = """
() => {
    const chat = document.querySelector('[data-list-id="chat-messages"]');
    if (chat) chat.scrollTo(0, chat.scrollHeight);
    window.scrollTo(0, document.body.scrollHeight);
}
"""


def _extract_channels_js(guild_id: Snowflake) -> str:
    # `guild_id` is a Snowflake, so it's already been validated as numeric —
    # safe to interpolate into the JS regex.
    return f"""
    (() => {{
        const channels = [];
        const seen = new Set();
        document.querySelectorAll('a[href*="/channels/"]').forEach(link => {{
            const m = link.href.match(/\\/channels\\/{guild_id}\\/([0-9]+)/);
            if (!m) return;
            const id = m[1];
            if (seen.has(id)) return;
            seen.add(id);
            let name = (link.textContent || '').trim();
            name = name.replace(/^[^a-zA-Z0-9#\\-_]+/, '').replace(/\\s+/g, ' ').trim();
            channels.push({{ id, name: name || ('channel-' + id) }});
        }});
        return channels;
    }})()
    """


def _url_encode(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def _message_from_api(raw: Any, fallback_channel_id: Snowflake) -> Message:
    """Convert a Discord API message object into our domain `Message`.

    Discord responses aren't statically typed in our codebase, so we
    defensively narrow each field.
    """
    if not isinstance(raw, dict):
        return Message(
            id="unknown",
            content="",
            author_name="Unknown",
            channel_id=fallback_channel_id,
            timestamp=datetime.now(timezone.utc),
            attachments=[],
        )
    data = cast(dict[str, Any], raw)

    channel_raw = data.get("channel_id")
    channel_id = (
        snowflake(channel_raw, field="channel_id")
        if isinstance(channel_raw, str) and channel_raw.isdigit()
        else fallback_channel_id
    )

    ts_raw = data.get("timestamp")
    ts = (
        datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if isinstance(ts_raw, str)
        else datetime.now(timezone.utc)
    )

    attachments: list[str] = []
    raw_attachments = data.get("attachments")
    if isinstance(raw_attachments, list):
        for att in cast(list[Any], raw_attachments):
            if isinstance(att, dict):
                url = cast(dict[str, Any], att).get("url")
                if isinstance(url, str):
                    attachments.append(url)

    raw_author = data.get("author")
    if isinstance(raw_author, dict):
        author = cast(dict[str, Any], raw_author)
        author_name = author.get("global_name") or author.get("username") or "Unknown"
        if not isinstance(author_name, str):
            author_name = "Unknown"
    else:
        author_name = "Unknown"

    raw_id = data.get("id")
    raw_content = data.get("content")

    return Message(
        id=str(raw_id) if isinstance(raw_id, str) else "unknown",
        content=raw_content if isinstance(raw_content, str) else "",
        author_name=author_name,
        channel_id=channel_id,
        timestamp=ts,
        attachments=attachments,
    )


def _split_message_id(
    trimmed: str, default_channel_id: Snowflake
) -> tuple[Snowflake, str]:
    """Parse "channel_id-message_id" → (channel_id, message_id).

    Falls back to `default_channel_id` and a raw id if the prefix doesn't
    look like a valid snowflake (e.g. when the element has a non-standard id).
    """
    if not trimmed:
        return default_channel_id, "unknown"
    head, sep, tail = trimmed.partition("-")
    if sep and head.isdigit() and len(head) >= 17:
        try:
            return snowflake(head, field="channel_id"), tail or "unknown"
        except Exception:
            pass
    return default_channel_id, trimmed


async def _extract_message(
    element: ElementHandle, default_channel_id: Snowflake
) -> Message | None:
    """Scrape a Discord chat-message-looking element into a Message.

    Discord's DOM gives each row an id of the form
    `chat-messages-{channel_id}-{message_id}`. We parse the channel ID out
    of the prefix so pinned / search / mention results carry the *real*
    source channel instead of whatever container we were scraping from.
    If parsing fails (unexpected format) we fall back to `default_channel_id`.
    """
    try:
        raw_id = await element.get_attribute("id") or ""
        trimmed = raw_id.removeprefix("chat-messages-")
        channel_id, msg_id = _split_message_id(trimmed, default_channel_id)

        content = ""
        for selector in (
            '[class*="messageContent"]',
            '[class*="markup"]',
            ".messageContent",
        ):
            content_el = await element.query_selector(selector)
            if content_el is None:
                continue
            text = await content_el.text_content()
            if text:
                content = text.strip()
                break

        author_name = "Unknown"
        for selector in ('[class*="username"]', '[class*="authorName"]', ".username"):
            author_el = await element.query_selector(selector)
            if author_el is None:
                continue
            text = await author_el.text_content()
            if text:
                author_name = text.strip()
                break

        ts_el = await element.query_selector("time")
        ts_attr = await ts_el.get_attribute("datetime") if ts_el else None
        ts = (
            datetime.fromisoformat(ts_attr.replace("Z", "+00:00"))
            if ts_attr
            else datetime.now(timezone.utc)
        )

        attachments: list[str] = []
        for att in await element.query_selector_all('a[href*="cdn.discordapp.com"]'):
            href = await att.get_attribute("href")
            if href:
                attachments.append(href)

        if not content and not attachments:
            return None

        return Message(
            id=msg_id,
            content=content,
            author_name=author_name,
            channel_id=channel_id,
            timestamp=ts,
            attachments=attachments,
        )
    except Exception as e:
        logger.debug(f"Failed to extract message: {e}")
        return None
