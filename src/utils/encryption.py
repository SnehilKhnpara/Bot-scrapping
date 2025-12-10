"""Secure credential encryption utilities.

This module provides encrypted storage for sensitive credentials
using Fernet symmetric encryption from the cryptography library.
"""

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class CredentialEncryption:
    """Handle secure encryption and decryption of credentials.

    Uses PBKDF2 key derivation with a master password to generate
    encryption keys, then Fernet for symmetric encryption.
    """

    def __init__(self, master_password: str, salt_file: Path | None = None):
        """Initialize encryption with master password.

        Args:
            master_password: The master password for key derivation.
            salt_file: Optional path to store/load the salt. Defaults to ~/.marketplace_agent_salt
        """
        self.salt_file = salt_file or Path.home() / ".marketplace_agent_salt"
        self._salt = self._get_or_create_salt()
        self._fernet = self._derive_key(master_password)

    def _get_or_create_salt(self) -> bytes:
        """Get existing salt or create a new one."""
        if self.salt_file.exists():
            return self.salt_file.read_bytes()

        salt = os.urandom(16)
        self.salt_file.parent.mkdir(parents=True, exist_ok=True)
        self.salt_file.write_bytes(salt)
        os.chmod(self.salt_file, 0o600)  # Restrict permissions
        return salt

    def _derive_key(self, password: str) -> Fernet:
        """Derive encryption key from password using PBKDF2."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self._salt,
            iterations=480000,  # OWASP recommended minimum
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return Fernet(key)

    def encrypt(self, data: str) -> str:
        """Encrypt a string value.

        Args:
            data: Plain text string to encrypt.

        Returns:
            Base64-encoded encrypted string.
        """
        encrypted = self._fernet.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """Decrypt an encrypted string.

        Args:
            encrypted_data: Base64-encoded encrypted string.

        Returns:
            Decrypted plain text string.
        """
        decoded = base64.urlsafe_b64decode(encrypted_data.encode())
        return self._fernet.decrypt(decoded).decode()

    def encrypt_dict(self, data: dict[str, Any]) -> str:
        """Encrypt a dictionary to a single encrypted string.

        Args:
            data: Dictionary to encrypt.

        Returns:
            Encrypted JSON string.
        """
        json_str = json.dumps(data)
        return self.encrypt(json_str)

    def decrypt_dict(self, encrypted_data: str) -> dict[str, Any]:
        """Decrypt an encrypted string back to a dictionary.

        Args:
            encrypted_data: Encrypted JSON string.

        Returns:
            Original dictionary.
        """
        json_str = self.decrypt(encrypted_data)
        return json.loads(json_str)

    def save_encrypted_file(self, data: dict[str, Any], filepath: Path) -> None:
        """Save encrypted data to a file.

        Args:
            data: Dictionary to encrypt and save.
            filepath: Path to save the encrypted file.
        """
        encrypted = self.encrypt_dict(data)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(encrypted)
        os.chmod(filepath, 0o600)  # Restrict permissions

    def load_encrypted_file(self, filepath: Path) -> dict[str, Any]:
        """Load and decrypt data from a file.

        Args:
            filepath: Path to the encrypted file.

        Returns:
            Decrypted dictionary.

        Raises:
            FileNotFoundError: If the file doesn't exist.
        """
        encrypted = filepath.read_text()
        return self.decrypt_dict(encrypted)
