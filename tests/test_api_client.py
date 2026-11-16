"""Tests for the Discord API client's retry logic and helpers."""

import json

import pytest

from discord_mcp.discord.api_client import (
    ApiUnavailable,
    DiscordApiClient,
    _retry_after_seconds,
)


class TestRetryAfterSeconds:
    def test_parses_retry_after(self):
        body = json.dumps({"message": "limited", "retry_after": 0.3})
        # 0.3 + 0.1 margin
        assert _retry_after_seconds(body) == pytest.approx(0.4)

    def test_defaults_when_missing(self):
        assert _retry_after_seconds(json.dumps({"message": "x"})) == pytest.approx(1.1)

    def test_defaults_on_garbage(self):
        assert _retry_after_seconds("not json") == pytest.approx(1.1)

    def test_caps_pathological_global_limit(self):
        body = json.dumps({"retry_after": 9999})
        assert _retry_after_seconds(body) == 10.0

    def test_never_negative(self):
        assert _retry_after_seconds(json.dumps({"retry_after": -5})) == pytest.approx(
            0.1
        )


class _FakePage:
    """Stands in for a Playwright Page: scripted `evaluate` responses."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = responses
        self.calls = 0

    async def evaluate(self, _js: str, _arg: object = None) -> object:
        # First call is the token extraction; everything after is a fetch.
        if self.calls == 0 and _arg is None:
            self.calls += 1
            return "fake-token"
        resp = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _client_with(responses: list[dict[str, object]]) -> DiscordApiClient:
    client = DiscordApiClient(_FakePage(responses))  # type: ignore[arg-type]
    client._token = "fake-token"  # skip token extraction
    return client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("discord_mcp.discord.api_client.asyncio.sleep", _instant)


class TestFetchJsonRetry:
    @pytest.mark.asyncio
    async def test_retries_429_then_succeeds(self):
        client = _client_with(
            [
                {"status": 429, "body": json.dumps({"retry_after": 0.01})},
                {"status": 429, "body": json.dumps({"retry_after": 0.01})},
                {"status": 200, "body": json.dumps([{"id": "1"}])},
            ]
        )
        out = await client.fetch_json("/api/v9/test")
        assert out == [{"id": "1"}]

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self):
        client = _client_with(
            [{"status": 429, "body": json.dumps({"retry_after": 0.01})}]
        )
        with pytest.raises(ApiUnavailable, match="rate limited"):
            await client.fetch_json("/api/v9/test")

    @pytest.mark.asyncio
    async def test_non_429_error_still_raises_immediately(self):
        client = _client_with([{"status": 500, "body": "boom"}])
        with pytest.raises(ApiUnavailable, match="500"):
            await client.fetch_json("/api/v9/test")

    @pytest.mark.asyncio
    async def test_success_first_try(self):
        client = _client_with([{"status": 200, "body": json.dumps({"ok": True})}])
        assert await client.fetch_json("/api/v9/test") == {"ok": True}
