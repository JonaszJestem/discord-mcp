"""KeyVault: abstraction over secret-key storage.

The default implementation wraps the OS keyring (GNOME Keyring on Linux,
Keychain on macOS, Credential Manager on Windows). Swap in a different
backend (HashiCorp Vault, etc.) by implementing the protocol.
"""

from __future__ import annotations

import os
from typing import Protocol

import keyring
import keyring.errors

from ..errors import KeyringUnavailable


class KeyVault(Protocol):
    """Stores a single symmetric key. Not aware of what the key is for."""

    def get_or_create(self, *, size: int) -> bytes:
        """Return the stored key, generating one of `size` random bytes if absent."""
        ...

    def get(self) -> bytes | None:
        """Return the stored key, or None if not present."""
        ...

    def delete(self) -> None:
        """Remove the stored key if present. No-op if absent."""
        ...


class OSKeyringVault:
    """KeyVault backed by the `keyring` library (OS credential store)."""

    def __init__(self, service: str, user: str) -> None:
        self._service = service
        self._user = user

    def get_or_create(self, *, size: int) -> bytes:
        encoded = self._get_password()
        if encoded is not None:
            return bytes.fromhex(encoded)
        key = os.urandom(size)
        self._set_password(key.hex())
        return key

    def get(self) -> bytes | None:
        encoded = self._get_password()
        return bytes.fromhex(encoded) if encoded is not None else None

    def delete(self) -> None:
        try:
            keyring.delete_password(self._service, self._user)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.NoKeyringError as e:
            raise KeyringUnavailable(self._unavailable_message(e)) from e

    def _get_password(self) -> str | None:
        try:
            return keyring.get_password(self._service, self._user)
        except keyring.errors.NoKeyringError as e:
            raise KeyringUnavailable(self._unavailable_message(e)) from e

    def _set_password(self, value: str) -> None:
        try:
            keyring.set_password(self._service, self._user, value)
        except keyring.errors.NoKeyringError as e:
            raise KeyringUnavailable(self._unavailable_message(e)) from e

    @staticmethod
    def _unavailable_message(cause: Exception) -> str:
        return (
            f"No OS keyring is available on this system ({cause}). "
            "discord-mcp needs a keyring backend — GNOME Keyring / KWallet on Linux, "
            "Keychain on macOS, Credential Manager on Windows."
        )
