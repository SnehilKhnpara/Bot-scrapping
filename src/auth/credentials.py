"""Secure credential loading and management.

Supports loading credentials from:
- Environment variables
- Encrypted configuration files
- Direct configuration (for testing only)
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

from ..utils.encryption import CredentialEncryption


class MarketplaceCredentials(BaseModel):
    """Marketplace login credentials model."""

    username: str = Field(..., min_length=1, description="Login username or email")
    password: SecretStr = Field(..., min_length=1, description="Login password")
    totp_secret: Optional[SecretStr] = Field(None, description="TOTP secret for 2FA if enabled")
    api_key: Optional[SecretStr] = Field(None, description="API key if available")

    class Config:
        """Pydantic configuration."""

        extra = "forbid"


@dataclass
class CredentialSource:
    """Information about where credentials were loaded from."""

    source_type: str  # "env", "file", "encrypted_file"
    source_path: Optional[str] = None


class CredentialsLoader:
    """Load and manage marketplace credentials securely.

    Priority order:
    1. Environment variables (MARKETPLACE_USERNAME, MARKETPLACE_PASSWORD, etc.)
    2. Encrypted credentials file
    3. Plain config file (development only)
    """

    ENV_PREFIX = "MARKETPLACE_"

    def __init__(
        self,
        env_file: Optional[Path] = None,
        encrypted_file: Optional[Path] = None,
        master_password: Optional[str] = None,
    ):
        """Initialize credentials loader.

        Args:
            env_file: Path to .env file (optional).
            encrypted_file: Path to encrypted credentials file.
            master_password: Master password for encrypted file.
        """
        self.env_file = env_file
        self.encrypted_file = encrypted_file
        self.master_password = master_password
        self._credentials: Optional[MarketplaceCredentials] = None
        self._source: Optional[CredentialSource] = None

        # Load .env file if specified
        if env_file and env_file.exists():
            load_dotenv(env_file)

    def load(self) -> MarketplaceCredentials:
        """Load credentials from the first available source.

        Returns:
            MarketplaceCredentials object.

        Raises:
            ValueError: If no valid credentials source found.
        """
        # Try environment variables first
        creds = self._load_from_env()
        if creds:
            self._credentials = creds
            self._source = CredentialSource(source_type="env")
            return creds

        # Try encrypted file
        if self.encrypted_file and self.master_password:
            creds = self._load_from_encrypted_file()
            if creds:
                self._credentials = creds
                self._source = CredentialSource(
                    source_type="encrypted_file",
                    source_path=str(self.encrypted_file),
                )
                return creds

        raise ValueError(
            "No valid credentials found. Set environment variables "
            f"({self.ENV_PREFIX}USERNAME, {self.ENV_PREFIX}PASSWORD) "
            "or provide an encrypted credentials file."
        )

    def _load_from_env(self) -> Optional[MarketplaceCredentials]:
        """Load credentials from environment variables."""
        username = os.getenv(f"{self.ENV_PREFIX}USERNAME")
        password = os.getenv(f"{self.ENV_PREFIX}PASSWORD")

        if not username or not password:
            return None

        return MarketplaceCredentials(
            username=username,
            password=SecretStr(password),
            totp_secret=SecretStr(s) if (s := os.getenv(f"{self.ENV_PREFIX}TOTP_SECRET")) else None,
            api_key=SecretStr(s) if (s := os.getenv(f"{self.ENV_PREFIX}API_KEY")) else None,
        )

    def _load_from_encrypted_file(self) -> Optional[MarketplaceCredentials]:
        """Load credentials from encrypted file."""
        if not self.encrypted_file or not self.encrypted_file.exists():
            return None

        if not self.master_password:
            return None

        try:
            encryption = CredentialEncryption(self.master_password)
            data = encryption.load_encrypted_file(self.encrypted_file)
            return MarketplaceCredentials(**data)
        except Exception:
            return None

    def save_encrypted(
        self,
        credentials: MarketplaceCredentials,
        filepath: Path,
        master_password: str,
    ) -> None:
        """Save credentials to an encrypted file.

        Args:
            credentials: Credentials to save.
            filepath: Path for the encrypted file.
            master_password: Master password for encryption.
        """
        encryption = CredentialEncryption(master_password)
        data = {
            "username": credentials.username,
            "password": credentials.password.get_secret_value(),
        }
        if credentials.totp_secret:
            data["totp_secret"] = credentials.totp_secret.get_secret_value()
        if credentials.api_key:
            data["api_key"] = credentials.api_key.get_secret_value()

        encryption.save_encrypted_file(data, filepath)

    @property
    def credentials(self) -> Optional[MarketplaceCredentials]:
        """Get loaded credentials."""
        return self._credentials

    @property
    def source(self) -> Optional[CredentialSource]:
        """Get credential source information."""
        return self._source

    def clear(self) -> None:
        """Clear loaded credentials from memory."""
        self._credentials = None
        self._source = None
