"""JWT signing with Ed25519 (EdDSA) and ECDSA P-256 (ES256), in plain Python.

The app uses the standard library only; some exchanges (Coinbase) sign API requests with these
keys, which the standard library does not cover. Both are short textbook implementations:
Ed25519 per RFC 8032, ECDSA P-256 with deterministic nonces per RFC 6979 (so a signature never
depends on a random number generator). They only sign: the private key never leaves the computer.
Tested against the test vectors of both RFCs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any, Dict, Optional, Tuple

# --- Ed25519 (RFC 8032) -------------------------------------------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_GX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_GY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _ed_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _ed_mul(s: int, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        s >>= 1
    return q


def _ed_encode(p) -> bytes:
    zi = pow(p[2], _P - 2, _P)
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_public_key(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    return _ed_encode(_ed_mul(a, _G))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("an Ed25519 private key is a 32-byte seed")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    public = _ed_encode(_ed_mul(a, _G))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % _L
    big_r = _ed_encode(_ed_mul(r, _G))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % _L
    return big_r + ((r + k * a) % _L).to_bytes(32, "little")


# --- ECDSA P-256 with SHA-256 (RFC 6979 nonces) -------------------------------------------

_EP = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_EA = _EP - 3
_EN = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_EG = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
       0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _ec_add(p: Optional[Tuple[int, int]], q: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _EP == 0:
            return None
        lam = (3 * p[0] * p[0] + _EA) * pow(2 * p[1], _EP - 2, _EP) % _EP
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], _EP - 2, _EP) % _EP
    x = (lam * lam - p[0] - q[0]) % _EP
    return x, (lam * (p[0] - x) - p[1]) % _EP


def _ec_mul(k: int, p: Tuple[int, int]) -> Optional[Tuple[int, int]]:
    out = None
    while k:
        if k & 1:
            out = _ec_add(out, p)
        p = _ec_add(p, p)
        k >>= 1
    return out


def _rfc6979_k(d: int, h1: bytes):
    x = d.to_bytes(32, "big")
    h = (int.from_bytes(h1, "big") % _EN).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < _EN:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def p256_sign(d: int, message: bytes) -> bytes:
    """ES256 signature (r || s, 64 bytes) of SHA-256(message)."""
    h1 = hashlib.sha256(message).digest()
    e = int.from_bytes(h1, "big")
    for k in _rfc6979_k(d, h1):
        point = _ec_mul(k, _EG)
        r = point[0] % _EN if point else 0
        if not r:
            continue
        s = pow(k, _EN - 2, _EN) * (e + r * d) % _EN
        if s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    raise ValueError("no signature")   # unreachable


# --- private keys as exchanges hand them out ------------------------------------------------

def _der(data: bytes, pos: int = 0) -> Tuple[int, bytes, int]:
    """One DER element at pos: (tag, content, next position)."""
    tag, length = data[pos], data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + length], pos + length


def _children(content: bytes):
    pos, out = 0, []
    while pos < len(content):
        tag, value, pos = _der(content, pos)
        out.append((tag, value))
    return out


ED25519_OID = bytes.fromhex("2b6570")          # 1.3.101.112
EC_OID = bytes.fromhex("2a8648ce3d0201")        # 1.2.840.10045.2.1


def load_private_key(text: str) -> Tuple[str, Any]:
    """("EdDSA", 32-byte seed) or ("ES256", secret integer) from what the exchange gave you: a PEM
    block (EC or PKCS#8, line breaks optional or written as \\n) or a base64 Ed25519 key."""
    text = (text or "").strip().replace("\\n", "\n")
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S)
    if not m:
        raw = base64.b64decode(re.sub(r"\s", "", text), validate=True)
        if len(raw) not in (32, 64):
            raise ValueError("not a PEM key, and not a base64 Ed25519 key (32 or 64 bytes)")
        return "EdDSA", raw[:32]
    der = base64.b64decode(re.sub(r"\s", "", m.group(2)))
    _, body, _ = _der(der)
    items = _children(body)
    if m.group(1) == "EC PRIVATE KEY":                    # SEC1: version, d, [0] curve, [1] public
        return "ES256", int.from_bytes(items[1][1], "big")
    if m.group(1) == "PRIVATE KEY":                       # PKCS#8: version, algorithm, key
        algorithm = _children(items[1][1])
        oid = algorithm[0][1]
        key = items[2][1]
        if oid == ED25519_OID:
            return "EdDSA", _der(key)[1]
        if oid == EC_OID:
            return "ES256", int.from_bytes(_children(_der(key)[1])[1][1], "big")
    raise ValueError(f"unsupported key type ({m.group(1)}): use an ECDSA or Ed25519 key")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(claims: Dict[str, Any], private_key: str, headers: Optional[Dict[str, Any]] = None) -> str:
    """A signed JWT (EdDSA or ES256, chosen by the key)."""
    alg, key = load_private_key(private_key)
    header = dict(headers or {}, alg=alg, typ="JWT")
    signing_input = (_b64(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64(json.dumps(claims, separators=(",", ":")).encode())).encode("ascii")
    sig = ed25519_sign(key, signing_input) if alg == "EdDSA" else p256_sign(key, signing_input)
    return signing_input.decode("ascii") + "." + _b64(sig)
