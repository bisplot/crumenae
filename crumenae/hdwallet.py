"""Bitcoin extended public keys (xpub / ypub / zpub): addresses derived on your computer.

A wallet app (Ledger Live, Trezor Suite, Sparrow, Electrum, BlueWallet…) shows one extended
public key per account. From it every receive address (chain 0) and change address (chain 1)
can be derived, without any private key (BIP32 public derivation on secp256k1). The key only
lets anyone *see* the account; it cannot spend. Private keys (xprv…) are refused.

Address types, by the key's prefix:
  * zpub → native SegWit bc1q… (BIP84)       * ypub → wrapped SegWit 3… (BIP49)
  * xpub → any of: legacy 1… (BIP44), wrapped SegWit 3…, native SegWit bc1q…, Taproot bc1p…
    (BIP86): many apps export "xpub" whatever the type, so the type is found from which
    addresses have transactions.

Standard library only: secp256k1 point arithmetic, HMAC-SHA512, RIPEMD-160 (with a pure-Python
fallback, as some OpenSSL 3 builds leave it out), Base58Check and Bech32/Bech32m.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from typing import Dict, List, Optional, Tuple

from .b58 import b58check_decode, b58check_encode

# --- secp256k1 ------------------------------------------------------------------------------
_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
Point = Optional[Tuple[int, int]]


def _add(p: Point, q: Point) -> Point:
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        lam = 3 * p[0] * p[0] * pow(2 * p[1], _P - 2, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], _P - 2, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _jdouble(p):
    x, y, z = p
    if not y:
        return (0, 0, 0)
    ysq = y * y % _P
    s = 4 * x * ysq % _P
    m = 3 * x * x % _P
    nx = (m * m - 2 * s) % _P
    return nx, (m * (s - nx) - 8 * ysq * ysq) % _P, 2 * y * z % _P


def _jadd(p, q):
    if not p[2]:
        return q
    if not q[2]:
        return p
    z1s, z2s = p[2] * p[2] % _P, q[2] * q[2] % _P
    u1, u2 = p[0] * z2s % _P, q[0] * z1s % _P
    s1, s2 = p[1] * z2s * q[2] % _P, q[1] * z1s * p[2] % _P
    if u1 == u2:
        return _jdouble(p) if s1 == s2 else (0, 0, 0)
    h, r = (u2 - u1) % _P, (s2 - s1) % _P
    h2 = h * h % _P
    h3 = h * h2 % _P
    u1h2 = u1 * h2 % _P
    nx = (r * r - h3 - 2 * u1h2) % _P
    return nx, (r * (u1h2 - nx) - s1 * h3) % _P, h * p[2] * q[2] % _P


def _mul(k: int, p: Point = _G) -> Point:
    """k·P in Jacobian coordinates (one inversion at the end)."""
    if p is None:
        return None
    out, q = (0, 0, 0), (p[0], p[1], 1)
    while k:
        if k & 1:
            out = _jadd(out, q)
        q = _jdouble(q)
        k >>= 1
    if not out[2]:
        return None
    zi = pow(out[2], _P - 2, _P)
    return out[0] * zi * zi % _P, out[1] * zi * zi * zi % _P


def _decompress(data: bytes) -> Tuple[int, int]:
    if len(data) != 33 or data[0] not in (2, 3):
        raise ValueError("not a compressed public key")
    x = int.from_bytes(data[1:], "big")
    y = pow((pow(x, 3, _P) + 7) % _P, (_P + 1) // 4, _P)
    if (y * y - x ** 3 - 7) % _P:
        raise ValueError("public key not on the curve")
    if y & 1 != data[0] & 1:
        y = _P - y
    return x, y


def _compress(p: Tuple[int, int]) -> bytes:
    return bytes([2 + (p[1] & 1)]) + p[0].to_bytes(32, "big")


# --- hashes ---------------------------------------------------------------------------------
def _ripemd160_py(data: bytes) -> bytes:
    """RIPEMD-160 (for Python builds whose OpenSSL lacks it)."""
    r1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12, 1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
    r2 = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13, 8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
    s1 = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8, 7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5, 11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
    s2 = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6, 9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5, 15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
    k1 = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    k2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
    mask = 0xFFFFFFFF

    def f(j: int, x: int, y: int, z: int) -> int:
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    def rol(x: int, n: int) -> int:
        return ((x << n) | (x >> (32 - n))) & mask

    msg = bytearray(data) + b"\x80"
    while len(msg) % 64 != 56:
        msg += b"\x00"
    msg += struct.pack("<Q", 8 * len(data))
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    for off in range(0, len(msg), 64):
        x = struct.unpack("<16I", msg[off:off + 64])
        a1, b1, c1, d1, e1 = h
        a2, b2, c2, d2, e2 = h
        for j in range(80):
            t = rol((a1 + (f(j, b1, c1, d1) & mask) + x[r1[j]] + k1[j // 16]) & mask, s1[j]) + e1
            a1, e1, d1, c1, b1 = e1, d1, rol(c1, 10), b1, t & mask
            t = rol((a2 + (f(79 - j, b2, c2, d2) & mask) + x[r2[j]] + k2[j // 16]) & mask, s2[j]) + e2
            a2, e2, d2, c2, b2 = e2, d2, rol(c2, 10), b2, t & mask
        t = (h[1] + c1 + d2) & mask
        h[1] = (h[2] + d1 + e2) & mask
        h[2] = (h[3] + e1 + a2) & mask
        h[3] = (h[4] + a1 + b2) & mask
        h[4] = (h[0] + b1 + c2) & mask
        h[0] = t
    return struct.pack("<5I", *h)


def ripemd160(data: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", data).digest()
    except ValueError:
        return _ripemd160_py(data)


def hash160(data: bytes) -> bytes:
    return ripemd160(hashlib.sha256(data).digest())


def _tagged(tag: str, data: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


# --- Bech32 / Bech32m (BIP173, BIP350) -----------------------------------------------------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: List[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def segwit_address(hrp: str, version: int, program: bytes) -> str:
    acc, bits, data = 0, 0, [version]
    for b in program:
        acc, bits = (acc << 8) | b, bits + 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    const = 1 if version == 0 else 0x2BC830A3
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(hrp_exp + data + [0] * 6) ^ const
    return hrp + "1" + "".join(_CHARSET[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


# --- extended keys --------------------------------------------------------------------------
TYPES = ("p2pkh", "p2sh-p2wpkh", "p2wpkh", "p2tr")
LABELS = {"p2pkh": "legacy (1…)", "p2sh-p2wpkh": "wrapped SegWit (3…)", "p2wpkh": "native SegWit (bc1q…)",
          "p2tr": "Taproot (bc1p…)"}
_PUBLIC = {0x0488B21E: ("xpub", None), 0x049D7CB2: ("ypub", "p2sh-p2wpkh"), 0x04B24746: ("zpub", "p2wpkh")}
_REFUSED = {
    0x0488ADE4: "this is a private key (xprv): never paste it anywhere; use the account's public key (xpub/zpub)",
    0x049D7878: "this is a private key (yprv): never paste it anywhere; use the account's public key (ypub)",
    0x04B2430C: "this is a private key (zprv): never paste it anywhere; use the account's public key (zpub)",
    0x0295B43F: "multisig keys (Ypub) are not supported: add the multisig address instead",
    0x02AA7ED3: "multisig keys (Zpub) are not supported: add the multisig address instead",
    0x043587CF: "testnet keys (tpub) are not supported", 0x044A5262: "testnet keys (upub) are not supported",
    0x045F1CF6: "testnet keys (vpub) are not supported",
}
PREFIXES = ("xpub", "ypub", "zpub", "xprv", "yprv", "zprv", "Ypub", "Zpub", "tpub", "upub", "vpub")


def looks_extended(text: str) -> bool:
    t = (text or "").strip()
    return len(t) > 100 and t[:4] in PREFIXES


class ExtendedKey:
    """An account-level extended public key: derive(chain, index) → address."""

    def __init__(self, text: str):
        text = (text or "").strip()
        try:
            raw = b58check_decode(text)
        except ValueError:
            raise ValueError("not an extended public key: checksum wrong (check that it is copied in full)")
        if len(raw) != 78:
            raise ValueError("not an extended public key (wrong length)")
        version = int.from_bytes(raw[:4], "big")
        if version in _REFUSED or version not in _PUBLIC:
            raise ValueError(_REFUSED.get(version) or "unknown extended key type: use an xpub, ypub or zpub")
        self.text = text
        self.prefix, self.fixed_type = _PUBLIC[version]
        self.depth = raw[4]
        self.chain_code = raw[13:45]
        self.key = raw[45:78]
        self.point = _decompress(self.key)
        self._children: Dict[int, Tuple[bytes, bytes, Tuple[int, int]]] = {}
        self._cache: Dict[Tuple[str, int, int], str] = {}

    @property
    def types(self) -> Tuple[str, ...]:
        """Address types this key can stand for (an xpub is ambiguous)."""
        return (self.fixed_type,) if self.fixed_type else TYPES

    def _child(self, key: bytes, chain_code: bytes, point: Tuple[int, int], i: int):
        if i >= 0x80000000:
            raise ValueError("hardened derivation needs the private key")
        mac = hmac.new(chain_code, key + i.to_bytes(4, "big"), hashlib.sha512).digest()
        tweak = int.from_bytes(mac[:32], "big")
        if tweak >= _N:
            raise ValueError("invalid child (next index)")
        child = _add(_mul(tweak), point)
        if child is None:
            raise ValueError("invalid child (next index)")
        return _compress(child), mac[32:], child

    def pubkey(self, chain: int, index: int) -> Tuple[bytes, Tuple[int, int]]:
        if chain not in self._children:
            key, cc, pt = self._child(self.key, self.chain_code, self.point, chain)
            self._children[chain] = (key, cc, pt)
        key, cc, pt = self._children[chain]
        child_key, _, child_pt = self._child(key, cc, pt, index)
        return child_key, child_pt

    def address(self, addr_type: str, chain: int, index: int, hrp: str = "bc") -> str:
        k = (addr_type, chain, index)
        if k in self._cache:
            return self._cache[k]
        key, pt = self.pubkey(chain, index)
        if addr_type == "p2pkh":
            out = b58check_encode(b"\x00" + hash160(key))
        elif addr_type == "p2sh-p2wpkh":
            out = b58check_encode(b"\x05" + hash160(b"\x00\x14" + hash160(key)))
        elif addr_type == "p2wpkh":
            out = segwit_address(hrp, 0, hash160(key))
        elif addr_type == "p2tr":            # BIP86: key-path only, output key = P + H_TapTweak(P)·G
            if pt[1] & 1:
                pt = (pt[0], _P - pt[1])
            x = pt[0].to_bytes(32, "big")
            t = int.from_bytes(_tagged("TapTweak", x), "big")
            q = _add(pt, _mul(t))
            out = segwit_address(hrp, 1, q[0].to_bytes(32, "big"))   # type: ignore[index]
        else:
            raise ValueError(f"unknown address type {addr_type}")
        self._cache[k] = out
        return out


def validate(text: str) -> str:
    """The key as typed (trimmed) if it is a usable extended public key; ValueError otherwise."""
    ExtendedKey(text)
    return text.strip()
