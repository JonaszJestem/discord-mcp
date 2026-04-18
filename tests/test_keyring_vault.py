"""Tests for OSKeyringVault using an in-process fake keyring backend.

We don't touch the user's real keyring — a fake `keyring` backend is
registered for the test run via the `keyring.backend` API.
"""

import keyring
import keyring.backend
import keyring.errors
import pytest

from discord_mcp.auth.keyring_vault import OSKeyringVault
from discord_mcp.errors import KeyringUnavailable


class FakeKeyringBackend(keyring.backend.KeyringBackend):
    priority: float = 1000.0  # beat real backends

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._store:
            raise keyring.errors.PasswordDeleteError
        del self._store[(service, username)]


class FailingKeyringBackend(keyring.backend.KeyringBackend):
    priority = 1000

    def get_password(self, service: str, username: str):
        raise keyring.errors.NoKeyringError("no backend here")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.NoKeyringError("no backend here")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.NoKeyringError("no backend here")


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyringBackend:
    backend = FakeKeyringBackend()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


@pytest.fixture
def failing_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FailingKeyringBackend()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)


class TestOSKeyringVault:
    def test_get_or_create_generates_key_once(self, fake_keyring: FakeKeyringBackend):
        vault = OSKeyringVault(service="test", user="k")
        key1 = vault.get_or_create(size=32)
        key2 = vault.get_or_create(size=32)
        assert len(key1) == 32
        assert key1 == key2

    def test_get_returns_none_when_absent(self, fake_keyring: FakeKeyringBackend):
        assert OSKeyringVault(service="test", user="k").get() is None

    def test_get_returns_stored_key(self, fake_keyring: FakeKeyringBackend):
        vault = OSKeyringVault(service="test", user="k")
        created = vault.get_or_create(size=32)
        assert vault.get() == created

    def test_delete_is_idempotent(self, fake_keyring: FakeKeyringBackend):
        vault = OSKeyringVault(service="test", user="k")
        vault.delete()  # no-op when absent, must not raise
        vault.get_or_create(size=32)
        vault.delete()
        assert vault.get() is None

    def test_missing_backend_raises_keyring_unavailable(self, failing_keyring: None):
        vault = OSKeyringVault(service="test", user="k")
        with pytest.raises(KeyringUnavailable):
            vault.get_or_create(size=32)
        with pytest.raises(KeyringUnavailable):
            vault.get()
        with pytest.raises(KeyringUnavailable):
            vault.delete()
