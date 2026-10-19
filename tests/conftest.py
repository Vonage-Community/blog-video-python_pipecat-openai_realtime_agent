"""Shared test fixtures. No test here talks to Vonage or OpenAI."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Secret, Settings
from tests.fakes import APPLICATION_ID


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def private_key_file(tmp_path: Path, private_key_pem: str) -> Path:
    path = tmp_path / "private.key"
    path.write_text(private_key_pem)
    return path


@pytest.fixture
def valid_env(private_key_file: Path) -> dict[str, str]:
    return {
        "VONAGE_APPLICATION_ID": APPLICATION_ID,
        "VONAGE_PRIVATE_KEY_PATH": str(private_key_file),
        "OPENAI_API_KEY": "sk-test-0123456789abcdef",
    }


@pytest.fixture
def settings() -> Settings:
    return Settings(
        vonage_application_id=APPLICATION_ID,
        vonage_private_key=Secret("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"),
        openai_api_key=Secret("sk-test-0123456789abcdef"),
        max_active_sessions=1,
        max_session_seconds=600,
        ready_timeout_seconds=60,
        session_creates_per_minute=100,
    )


def with_settings(settings: Settings, **changes: object) -> Settings:
    return replace(settings, **changes)  # type: ignore[arg-type]
