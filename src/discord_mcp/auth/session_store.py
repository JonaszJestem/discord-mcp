"""SessionStore: persist Playwright session data (cookies + origins).

The EncryptedFileSessionStore writes AES-256-GCM ciphertext to a single file
and delegates key storage to a KeyVault. Neither artifact is useful in
isolation — the file is opaque bytes and the keyring entry is a random 32-byte
value unconnected to Discord.
"""

from __future__ import annotations

import json
import os
import pathlib as pl
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import SessionCorrupt, SessionMissing
from ..models import SessionData
from .keyring_vault import KeyVault


_KEY_SIZE = 32
_NONCE_SIZE = 12


class SessionStore(Protocol):
    """Persists a Playwright-compatible storage_state dict."""

    def load(self) -> SessionData:
        """Return the stored session. Raises SessionMissing or SessionCorrupt."""
        ...

    def save(self, data: SessionData) -> None:
        """Persist `data`, replacing any previous session atomically."""
        ...

    def delete(self) -> None:
        """Remove the stored session if present. No-op if absent."""
        ...

    def exists(self) -> bool:
        """Return True iff both the persisted data and its key are present."""
        ...


class EncryptedFileSessionStore:
    """AES-GCM file-backed session store. Key lives in a KeyVault."""

    def __init__(self, path: pl.Path, vault: KeyVault) -> None:
        self._path = path
        self._vault = vault

    @property
    def path(self) -> pl.Path:
        return self._path

    def load(self) -> SessionData:
        if not self._path.exists():
            raise SessionMissing(
                f"No session at {self._path}. Run `discord-mcp login` to create one."
            )
        key = self._vault.get()
        if key is None:
            raise SessionMissing(
                "Session file exists but no key in keyring. "
                "Run `discord-mcp login` to re-authenticate."
            )

        blob = self._path.read_bytes()
        if len(blob) < _NONCE_SIZE + 16:
            raise SessionCorrupt(
                f"Session file at {self._path} is too short to be valid."
            )

        nonce, ciphertext = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as e:
            raise SessionCorrupt(
                f"Failed to decrypt session at {self._path}. "
                "Run `discord-mcp login` to re-authenticate."
            ) from e

        return json.loads(plaintext)

    def save(self, data: SessionData) -> None:
        key = self._vault.get_or_create(size=_KEY_SIZE)
        plaintext = json.dumps(data).encode("utf-8")
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)

        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Atomic write via tempfile + rename. Create the tempfile with 0o600
        # at open time so there's no umask race between write and chmod.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, nonce + ciphertext)
        finally:
            os.close(fd)
        tmp.replace(self._path)

    def delete(self) -> None:
        if self._path.exists():
            self._path.unlink()
        self._vault.delete()

    def exists(self) -> bool:
        if not self._path.exists():
            return False
        try:
            return self._vault.get() is not None
        except Exception:
            return False
