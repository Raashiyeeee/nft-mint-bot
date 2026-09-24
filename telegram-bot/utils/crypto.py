"""
utils/crypto.py — Fernet encryption/decryption for private key storage.
Private keys are encrypted before DB storage and decrypted only at sign time.
NEVER log or print decrypted key material.
"""
import os
import base64
from cryptography.fernet import Fernet, InvalidToken


def get_fernet() -> Fernet:
    """Load Fernet instance from FERNET_KEY env var."""
    key = os.environ.get("FERNET_KEY")
    if not key:
        raise RuntimeError("FERNET_KEY not set in environment")
    # Fernet requires URL-safe base64 encoded 32-byte key
    # Pad if necessary
    key = key.strip()
    if not key.endswith("="):
        key += "=" * (4 - len(key) % 4) if len(key) % 4 != 0 else ""
    return Fernet(key.encode())


def encrypt_private_key(private_key_hex: str) -> str:
    """
    Encrypt a private key (hex string with or without 0x prefix).
    Returns Fernet token as string.
    """
    if not private_key_hex:
        raise ValueError("Private key cannot be empty")

    # Normalize: ensure it starts with 0x, all lowercase
    key = private_key_hex.strip().lower()
    if not key.startswith("0x"):
        key = "0x" + key

    # Validate length (32 bytes = 64 hex chars + optional 0x)
    hex_part = key[2:]
    if len(hex_part) != 64:
        raise ValueError(f"Private key must be 32 bytes (64 hex chars), got {len(hex_part)}")

    try:
        bytes.fromhex(hex_part)
    except ValueError:
        raise ValueError("Private key contains invalid hex characters")

    f = get_fernet()
    encrypted = f.encrypt(key.encode())
    return encrypted.decode()


def decrypt_private_key(encrypted_token: str) -> str:
    """
    Decrypt a Fernet-encrypted private key.
    Returns hex string with 0x prefix.
    NEVER store the result — use immediately and discard.
    """
    f = get_fernet()
    try:
        decrypted = f.decrypt(encrypted_token.encode())
        return decrypted.decode()
    except InvalidToken:
        raise ValueError("Invalid or corrupted encrypted key token")


def mask_address(address: str) -> str:
    """Returns 0x1234...5678 format for display."""
    if len(address) < 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def validate_private_key_format(key: str) -> bool:
    """Validates that a string is a valid Ethereum private key."""
    key = key.strip().lower().lstrip("0x")
    if len(key) != 64:
        return False
    try:
        bytes.fromhex(key)
        return True
    except ValueError:
        return False


def validate_address_format(address: str) -> bool:
    """Validates Ethereum address format."""
    address = address.strip()
    if not address.startswith("0x"):
        return False
    hex_part = address[2:]
    if len(hex_part) != 40:
        return False
    try:
        bytes.fromhex(hex_part)
        return True
    except ValueError:
        return False
