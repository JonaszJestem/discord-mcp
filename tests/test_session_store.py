import os
import pathlib as pl

import pytest

from discord_mcp.auth.keyring_vault import KeyVault
from discord_mcp.auth.session_store import EncryptedFileSessionStore
from discord_mcp.errors import SessionCorrupt, SessionMissing


class InMemoryVault:
    """A KeyVault impl that keeps the key in a Python dict — for tests only."""

    def __init__(self) -> None:
        self._key: bytes | None = None

    def get_or_create(self, *, size: int) -> bytes:
        if self._key is None:
            self._key = os.urandom(size)
        return self._key

    def get(self) -> bytes | None:
        return self._key

    def delete(self) -> None:
        self._key = None


# Sanity: ensure the test double actually satisfies the protocol.
_: KeyVault = InMemoryVault()


@pytest.fixture
def vault() -> InMemoryVault:
    return InMemoryVault()


@pytest.fixture
def store(tmp_path: pl.Path, vault: InMemoryVault) -> EncryptedFileSessionStore:
    return EncryptedFileSessionStore(path=tmp_path / "session.enc", vault=vault)


class TestEncryptedFileSessionStore:
    def test_round_trip(self, store: EncryptedFileSessionStore):
        data = {"cookies": [{"name": "x", "value": "y"}], "origins": []}
        store.save(data)
        assert store.load() == data

    def test_load_missing_raises(self, store: EncryptedFileSessionStore):
        with pytest.raises(SessionMissing):
            store.load()

    def test_load_without_key_raises_missing(
        self, store: EncryptedFileSessionStore, vault: InMemoryVault
    ):
        store.save({"cookies": []})
        vault.delete()  # simulate keyring wipe while the file survives
        with pytest.raises(SessionMissing):
            store.load()

    def test_load_with_wrong_key_raises_corrupt(
        self, store: EncryptedFileSessionStore, vault: InMemoryVault
    ):
        store.save({"cookies": []})
        # Replace the key — ciphertext can't be authenticated anymore.
        vault._key = os.urandom(32)  # type: ignore[attr-defined]
        with pytest.raises(SessionCorrupt):
            store.load()

    def test_load_with_truncated_file_raises_corrupt(
        self, store: EncryptedFileSessionStore
    ):
        store.save({"cookies": []})
        store.path.write_bytes(b"x")
        with pytest.raises(SessionCorrupt):
            store.load()

    def test_file_permissions_are_restrictive(self, store: EncryptedFileSessionStore):
        store.save({"cookies": []})
        mode = store.path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_parent_dir_permissions_are_restrictive(
        self, store: EncryptedFileSessionStore
    ):
        store.save({"cookies": []})
        mode = store.path.parent.stat().st_mode & 0o777
        assert mode == 0o700

    def test_exists_requires_both_file_and_key(
        self, store: EncryptedFileSessionStore, vault: InMemoryVault
    ):
        assert not store.exists()
        store.save({"cookies": []})
        assert store.exists()
        vault.delete()
        assert not store.exists()

    def test_delete_removes_both(
        self, store: EncryptedFileSessionStore, vault: InMemoryVault
    ):
        store.save({"cookies": []})
        assert store.path.exists()
        store.delete()
        assert not store.path.exists()
        assert vault.get() is None

    def test_save_is_atomic_no_tmp_file_left_behind(
        self, store: EncryptedFileSessionStore
    ):
        store.save({"cookies": []})
        leftovers = list(store.path.parent.glob("*.tmp"))
        assert leftovers == []

    def test_ciphertext_looks_random(self, store: EncryptedFileSessionStore):
        """The on-disk blob should not leak cleartext fields."""
        store.save({"cookies": [{"name": "auth_token", "value": "secret-xyz"}]})
        blob = store.path.read_bytes()
        assert b"auth_token" not in blob
        assert b"secret-xyz" not in blob
