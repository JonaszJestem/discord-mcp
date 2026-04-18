"""DiscordBrowserDriver: owns a logged-in Playwright browser and scrapes it.

The driver is the only place that touches Playwright APIs or Discord's DOM.
Everything else in the codebase speaks domain types (Guild, Channel, Message).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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
        """Open the pins popout for a channel and scrape its contents."""
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}/{channel_id}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(_CHAT_MESSAGES_SELECTOR, timeout=15000)

        pin_btn = await page.query_selector('[aria-label*="Pinned" i]')
        if pin_btn is None:
            logger.debug("No Pinned Messages button found; channel may have no pins UI")
            return []
        await pin_btn.click()
        await page.wait_for_timeout(2000)

        # The pins popover renders message-like rows with the same content
        # selectors as chat — grab anything with a chat-messages-* id.
        elements = await page.query_selector_all(
            '[role="dialog"] [id^="chat-messages-"], [role="menu"] [id^="chat-messages-"]'
        )
        if not elements:
            elements = await page.query_selector_all('[id^="chat-messages-"]')

        messages: list[Message] = []
        seen: set[str] = set()
        for el in elements:
            msg = await _extract_message(el, channel_id)
            if msg is None or msg.id in seen:
                continue
            seen.add(msg.id)
            messages.append(msg)

        # Close popover to avoid interfering with next call.
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        return messages

    async def list_threads(
        self, guild_id: Snowflake, channel_id: Snowflake
    ) -> list[Thread]:
        """Open the threads browser for a channel and scrape active threads."""
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}/{channel_id}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(_CHAT_MESSAGES_SELECTOR, timeout=15000)

        threads_btn = await page.query_selector('[aria-label*="Threads" i]')
        if threads_btn is None:
            return []
        await threads_btn.click()
        await page.wait_for_timeout(2000)

        raw = await page.evaluate(_extract_threads_js(guild_id, channel_id))
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

        return [
            Thread(
                id=snowflake(t["id"], field="thread_id"),
                name=t["name"],
                parent_channel_id=channel_id,
            )
            for t in raw
        ]

    async def search_messages(
        self,
        guild_id: Snowflake,
        query: str,
        *,
        channel_id: Snowflake | None,
        limit: int,
    ) -> list[Message]:
        """Run Discord's server-scoped search and scrape result rows."""
        page = self._page
        await page.goto(
            f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded"
        )
        await page.wait_for_selector(
            _LOGGED_IN_SELECTOR, state="visible", timeout=15000
        )

        search_box = await page.query_selector(
            'input[placeholder*="Search" i], [aria-label*="Search" i]'
        )
        if search_box is None:
            logger.debug("Could not find search input")
            return []

        full_query = query if channel_id is None else f"in:{channel_id} {query}"
        await search_box.click()
        await search_box.fill(full_query)
        await page.keyboard.press("Enter")
        # Search results render in a side panel; give Discord time.
        await page.wait_for_timeout(3000)

        elements = await page.query_selector_all(
            '[class*="searchResultsWrap"] [id^="chat-messages-"], '
            '[class*="searchResults"] [id^="chat-messages-"]'
        )
        if not elements:
            elements = await page.query_selector_all('[id^="chat-messages-"]')

        scope_channel = (
            channel_id if channel_id is not None else snowflake("0", field="channel_id")
        )
        messages: list[Message] = []
        seen: set[str] = set()
        for el in elements:
            if len(messages) >= limit:
                break
            msg = await _extract_message(el, scope_channel)
            if msg is None or msg.id in seen:
                continue
            seen.add(msg.id)
            messages.append(msg)

        return messages

    async def list_mentions(self, *, limit: int) -> list[Message]:
        """Open the @mentions inbox panel and scrape recent mentions."""
        page = self._page
        await page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )
        await page.wait_for_selector(
            _LOGGED_IN_SELECTOR, state="visible", timeout=15000
        )

        inbox_btn = await page.query_selector(
            '[aria-label*="Inbox" i], [aria-label*="mention" i]'
        )
        if inbox_btn is None:
            return []
        await inbox_btn.click()
        await page.wait_for_timeout(1000)

        # Click the "Mentions" tab inside the inbox if present.
        try:
            mentions_tab = await page.query_selector(
                '[role="tab"]:has-text("Mentions")'
            )
            if mentions_tab is not None:
                await mentions_tab.click()
                await page.wait_for_timeout(1500)
        except Exception as e:
            logger.debug(f"Could not switch to Mentions tab: {e}")

        elements = await page.query_selector_all(
            '[role="dialog"] [id^="chat-messages-"], [role="menu"] [id^="chat-messages-"]'
        )
        # Inbox rows don't carry a channel id; use a placeholder.
        placeholder_channel = snowflake("0", field="channel_id")
        messages: list[Message] = []
        seen: set[str] = set()
        for el in elements:
            if len(messages) >= limit:
                break
            msg = await _extract_message(el, placeholder_channel)
            if msg is None or msg.id in seen:
                continue
            seen.add(msg.id)
            messages.append(msg)

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        return messages

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


def _extract_threads_js(guild_id: Snowflake, channel_id: Snowflake) -> str:
    """Extract thread links from the Threads popover.

    Both IDs are pre-validated Snowflakes, so interpolating them into the JS
    regex is safe (they match the numeric pattern).
    """
    return f"""
    (() => {{
        const threads = [];
        const seen = new Set();
        const prefix = '/channels/{guild_id}/';
        document.querySelectorAll('a[href^="' + prefix + '"]').forEach(link => {{
            const m = link.href.match(/\\/channels\\/{guild_id}\\/([0-9]+)/);
            if (!m) return;
            const id = m[1];
            if (id === '{channel_id}' || seen.has(id)) return;
            seen.add(id);
            const name = (link.textContent || '').trim().replace(/\\s+/g, ' ');
            threads.push({{ id, name: name || 'thread-' + id }});
        }});
        return threads;
    }})()
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


async def _extract_message(
    element: ElementHandle, channel_id: Snowflake
) -> Message | None:
    try:
        raw_id = await element.get_attribute("id") or ""
        msg_id = raw_id.replace("chat-messages-", "") or "unknown"

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
