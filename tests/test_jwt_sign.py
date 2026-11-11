"""Ed25519 and ECDSA P-256 signing in plain Python, against the RFC test vectors."""
import base64
import json
import unittest

from crumenae import jwt_sign


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        vectors = [   # RFC 8032 section 7.1, tests 1-3: (secret, public, message, signature)
            ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
             "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
             "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
             "d25bf5f0595bbe24655141438e7a100b"),
            ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
             "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
             "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
             "387b2eaeb4302aeeb00d291612bb0c00"),
            ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
             "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
             "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc659"
             "4a7c15e9716ed28dc027beceea1ec40a"),
        ]
        for secret, public, message, signature in vectors:
            seed = bytes.fromhex(secret)
            self.assertEqual(jwt_sign.ed25519_public_key(seed).hex(), public)
            self.assertEqual(jwt_sign.ed25519_sign(seed, bytes.fromhex(message)).hex(), signature)


class P256Test(unittest.TestCase):
    def test_rfc6979_vector(self):
        # RFC 6979 A.2.5, ECDSA P-256 with SHA-256, message "sample": deterministic k, known r and s.
        d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
        sig = jwt_sign.p256_sign(d, b"sample")
        self.assertEqual(sig[:32].hex().upper(), "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716")
        self.assertEqual(sig[32:].hex().upper(), "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8")
        sig = jwt_sign.p256_sign(d, b"test")
        self.assertEqual(sig[:32].hex().upper(), "F1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367")
        self.assertEqual(sig[32:].hex().upper(), "019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083")


# Test keys generated for these tests only (not anyone's key).
EC_SEC1 = """-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIMmvqdhFunUWa1whV2ex1pNOUMPbNuibEnuKYisSD2choAoGCCqGSM49
AwEHoUQDQgAEYP7UuiVanTHJYet0xjVtaMBJuJI7Yfps5mliLmDyn7Z5A/4QCLi8
maQa6elWKLxk8vGyDC1+n1F3o8KU1EYimQ==
-----END EC PRIVATE KEY-----"""
ED_PKCS8 = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIJ1hsZ3v/VpguoRK9JLsLMREScVpezJpGXA7rAMcrn9g\n-----END PRIVATE KEY-----"


class KeyLoadingTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(jwt_sign.load_private_key(EC_SEC1),
                         ("ES256", 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721))
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(jwt_sign.load_private_key(ED_PKCS8), ("EdDSA", seed))
        self.assertEqual(jwt_sign.load_private_key(ED_PKCS8.replace("\n", "")), ("EdDSA", seed))    # pasted on one line
        self.assertEqual(jwt_sign.load_private_key(ED_PKCS8.replace("\n", "\\n")), ("EdDSA", seed))  # JSON-escaped
        raw = base64.b64encode(seed + jwt_sign.ed25519_public_key(seed)).decode()
        self.assertEqual(jwt_sign.load_private_key(raw), ("EdDSA", seed))
        with self.assertRaises(ValueError):
            jwt_sign.load_private_key("not a key")

    def test_jwt(self):
        token = jwt_sign.jwt({"sub": "k", "iss": "cdp"}, ED_PKCS8, {"kid": "k", "nonce": "n"})
        head, body, sig = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head))), {"kid": "k", "nonce": "n", "alg": "EdDSA", "typ": "JWT"})
        seed = jwt_sign.load_private_key(ED_PKCS8)[1]
        self.assertEqual(base64.urlsafe_b64decode(pad(sig)), jwt_sign.ed25519_sign(seed, f"{head}.{body}".encode()))


if __name__ == "__main__":
    unittest.main()
