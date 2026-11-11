"""Address checks for Bitcoin-like coins (Bitcoin, Litecoin, Dogecoin, Bitcoin Cash).

The network config names the format ("address_format"); a wrong coin's address is refused with
a clear message instead of syncing an empty wallet.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Tuple

from ..b58 import b58check_decode

BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _base58(address: str, versions: Tuple[int, ...]) -> bool:
    try:
        raw = b58check_decode(address)
    except (ValueError, KeyError):
        return False
    return len(raw) == 21 and raw[0] in versions


def _segwit(address: str, hrp: str) -> bool:
    a = address.lower()
    return bool(re.match(rf"^{hrp}1[{BECH32}]{{11,71}}$", a))


def _cashaddr_polymod(values) -> int:
    c = 1
    for d in values:
        c0 = c >> 35
        c = ((c & 0x07FFFFFFFF) << 5) ^ d
        for i, g in enumerate((0x98F2BC8E61, 0x79B76D99E2, 0xF33E5FB3C4, 0xAE2EABE2A8, 0x1E4F43E470)):
            if c0 & (1 << i):
                c ^= g
    return c ^ 1


def cashaddr_valid(address: str, prefix: str = "bitcoincash") -> bool:
    a = address.lower()
    if ":" in a:
        p, _, a = a.partition(":")
        if p != prefix:
            return False
    if not a or a[0] not in "qp" or any(ch not in BECH32 for ch in a) or not 42 <= len(a) <= 112:
        return False
    data = [BECH32.index(ch) for ch in a]
    return _cashaddr_polymod([ord(ch) & 31 for ch in prefix] + [0] + data) == 0


def _bitcoin(a: str) -> str:
    if a.lower().startswith("bc1"):
        if _segwit(a, "bc"):
            return a.lower()
    elif re.match(r"^[13][1-9A-HJ-NP-Za-km-z]{25,34}$", a):
        return a
    raise ValueError("not a Bitcoin mainnet address (1…, 3… or bc1…)")


def _litecoin(a: str) -> str:
    if a.lower().startswith("ltc1"):
        if _segwit(a, "ltc"):
            return a.lower()
    elif _base58(a, (0x30, 0x32, 0x05)):
        return a
    raise ValueError("not a Litecoin address (L…, M…, 3… or ltc1…)")


def _dogecoin(a: str) -> str:
    if _base58(a, (0x1E, 0x16)):
        return a
    raise ValueError("not a Dogecoin address (D…, A… or 9…)")


def _bitcoin_cash(a: str) -> str:
    if _base58(a, (0x00, 0x05)):
        return a                                   # legacy format, as old wallets show it
    if cashaddr_valid(a):
        return "bitcoincash:" + a.lower().split(":")[-1]
    raise ValueError("not a Bitcoin Cash address (bitcoincash:q…, q…, or a legacy 1…/3… address)")


FORMATS: Dict[str, Callable[[str], str]] = {
    "bitcoin": _bitcoin, "litecoin": _litecoin, "dogecoin": _dogecoin, "bitcoin-cash": _bitcoin_cash,
}


def validate(fmt: str, address: str) -> str:
    address = address.strip()
    if address.lower().startswith(("xpub", "ypub", "zpub", "ltub", "mtub", "dgub")):
        raise ValueError("extended public keys are not supported yet: add each address")
    return FORMATS.get(fmt or "bitcoin", _bitcoin)(address)


def same(a: str, b: str) -> bool:
    """Compare addresses as explorers write them (cashaddr with or without its prefix)."""
    return (a or "").lower().split(":")[-1] == (b or "").lower().split(":")[-1]
