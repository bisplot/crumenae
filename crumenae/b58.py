"""Base58 / Base58Check (Bitcoin alphabet), used for Solana and Tron addresses."""
from __future__ import annotations

import hashlib

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _INDEX:
            raise ValueError(f"invalid base58 character '{ch}'")
        n = n * 58 + _INDEX[ch]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def b58check_encode(payload: bytes) -> str:
    return b58encode(payload + _checksum(payload))


def b58check_decode(text: str) -> bytes:
    raw = b58decode(text)
    if len(raw) < 5 or _checksum(raw[:-4]) != raw[-4:]:
        raise ValueError("bad base58check checksum")
    return raw[:-4]
