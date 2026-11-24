"""Bitcoin accounts from an extended public key (xpub / ypub / zpub): derivation checked
against the published BIP44/49/84/86 test vectors ("abandon … about" mnemonic, public test
data), address discovery with the gap limit, history netted over all addresses, resume, the
wallet check, and refused keys."""
import hashlib
import unittest
from decimal import Decimal

from crumenae import hdwallet, sync, views
from crumenae.adapters import get_adapter

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
# Test vectors (BIP44/49/84/86, mnemonic "abandon ×11 about"): key, the same key as "xpub", addresses
VECTORS = {
    "p2pkh": {
        "key": "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj",
        "xpub": "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj",
        "addresses": {
            "0/0": "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA",
            "0/1": "1Ak8PffB2meyfYnbXZR9EGfLfFZVpzJvQP",
            "1/0": "1J3J6EvPrv8q6AC3VCjWV45Uf3nssNMRtH",
            "1/1": "13vKxXzHXXd8HquAYdpkJoi9ULVXUgfpS5"
        }
    },
    "p2sh-p2wpkh": {
        "key": "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP",
        "xpub": "xpub6C6nQwHaWbSrzs5tZ1q7m5R9cPK9eYpNMFesiXsYrgc1P8bvLLAet9JfHjYXKjToD8cBRswJXXbbFpXgwsswVPAZzKMa1jUp2kVkGVUaJa7",
        "addresses": {
            "0/0": "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf",
            "0/1": "3LtMnn87fqUeHBUG414p9CWwnoV6E2pNKS",
            "1/0": "34K56kSjgUCUSD8GTtuF7c9Zzwokbs6uZ7",
            "1/1": "3516F2wmK51jVRrggEJsTUBNWMSLLjzvJ2"
        }
    },
    "p2wpkh": {
        "key": "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs",
        "xpub": "xpub6CatWdiZiodmUeTDp8LT5or8nmbKNcuyvz7WyksVFkKB4RHwCD3XyuvPEbvqAQY3rAPshWcMLoP2fMFMKHPJ4ZeZXYVUhLv1VMrjPC7PW6V",
        "addresses": {
            "0/0": "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
            "0/1": "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g",
            "1/0": "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el",
            "1/1": "bc1qggnasd834t54yulsep6fta8lpjekv4zj6gv5rf"
        }
    },
    "p2tr": {
        "key": "xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ",
        "xpub": "xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ",
        "addresses": {
            "0/0": "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr",
            "0/1": "bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh",
            "1/0": "bc1p3qkhfews2uk44qtvauqyr2ttdsw7svhkl9nkm9s9c3x4ax5h60wqwruhk7",
            "1/1": "bc1ptdg60grjk9t3qqcqczp4tlyy3z47yrx9nhlrjsmw36q5a72lhdrs9f00nj"
        }
    }
}
ZPUB = VECTORS["p2wpkh"]["key"]
R0, R1 = VECTORS["p2wpkh"]["addresses"]["0/0"], VECTORS["p2wpkh"]["addresses"]["0/1"]
C0 = VECTORS["p2wpkh"]["addresses"]["1/0"]
EXT = "bc1qexternal0000000000000000000000000000"


def tx(txid, t, vin, vout, fee=0, confirmed=True):
    return {"txid": txid, "fee": fee, "status": {"confirmed": confirmed, "block_time": t},
            "vin": [{"prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}


class FakeEsplora:
    """mempool.space for a few transactions: /address/{a} stats and /address/{a}/txs, newest first."""
    def __init__(self):
        self.txs = [   # oldest first
            tx("A", 1_700_000_000, [(EXT, 200_000_000)], [(R0, 100_000_000), (EXT, 99_990_000)], fee=10_000),
            tx("B", 1_710_000_000, [(R0, 100_000_000)], [(EXT, 30_000_000), (C0, 69_990_000)], fee=10_000),
            tx("C", 1_720_000_000, [(EXT, 50_000_000)], [(R1, 20_000_000), (EXT, 29_990_000)], fee=10_000),
            tx("P", 0, [(EXT, 1_000)], [(R1, 900)], fee=100, confirmed=False),       # in the mempool
        ]
        self.stats_calls, self.tx_calls = [], []

    def involving(self, a):
        return [t for t in reversed(self.txs) if any(i["prevout"]["scriptpubkey_address"] == a for i in t["vin"])
                or any(o["scriptpubkey_address"] == a for o in t["vout"])]

    def __call__(self, method, url, params, payload):
        path = url.split("/api", 1)[1]
        parts = path.strip("/").split("/")
        if parts[0] != "address":
            return None
        a = parts[1]
        mine = self.involving(a)
        conf = [t for t in mine if t["status"]["confirmed"]]
        if len(parts) == 2:
            self.stats_calls.append(a)
            funded = sum(o["value"] for t in conf for o in t["vout"] if o["scriptpubkey_address"] == a)
            spent = sum(i["prevout"]["value"] for t in conf for i in t["vin"] if i["prevout"]["scriptpubkey_address"] == a)
            return {"address": a, "chain_stats": {"tx_count": len(conf), "funded_txo_sum": funded, "spent_txo_sum": spent},
                    "mempool_stats": {"tx_count": len(mine) - len(conf)}}
        self.tx_calls.append(a)
        return mine[:25] if len(parts) == 3 else []


class DerivationTest(unittest.TestCase):
    def test_bip_test_vectors_for_every_key_and_type(self):
        for addr_type, v in VECTORS.items():
            for text in (v["key"], v["xpub"]):          # zpub/ypub, and the same account exported as "xpub"
                k = hdwallet.ExtendedKey(text)
                self.assertIn(addr_type, k.types)
                for path, want in v["addresses"].items():
                    chain, i = map(int, path.split("/"))
                    self.assertEqual(k.address(addr_type, chain, i), want, (addr_type, text[:4], path))
        self.assertEqual(hdwallet.ExtendedKey(ZPUB).types, ("p2wpkh",))
        self.assertEqual(len(hdwallet.ExtendedKey(VECTORS["p2tr"]["key"]).types), 4)   # xpub: ambiguous

    def test_ripemd160_fallback(self):
        for m in (b"", b"abc", b"message digest", b"a" * 1000):
            self.assertEqual(hdwallet._ripemd160_py(m).hex(), {
                b"": "9c1185a5c5e9fc54612808977ee8f548b2258d31", b"abc": "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc",
                b"message digest": "5d0689ef49d2fae572b881b123a85ffa21595f36"}.get(m) or hashlib.new("ripemd160", m).hexdigest())

    def test_refused_and_validated(self):
        t = TempContext()
        try:
            btc = get_adapter(t.ctx, t.ctx.networks.get("bitcoin"))
            self.assertEqual(btc.validate_address("  " + ZPUB + " "), ZPUB)
            self.assertEqual(btc.validate_address(R0), R0)                    # single addresses as before
            with self.assertRaises(ValueError):
                btc.validate_address(ZPUB[:-1] + ("A" if ZPUB[-1] != "A" else "B"))   # checksum
            xprv = hdwallet.b58check_encode(bytes.fromhex("0488ade4") + bytes(41) + b"\x00" + bytes(32))
            with self.assertRaises(ValueError) as e:
                btc.validate_address(xprv)
            self.assertIn("private key", str(e.exception))
            with self.assertRaises(ValueError) as e:
                get_adapter(t.ctx, t.ctx.networks.get("litecoin")).validate_address(ZPUB)
            self.assertIn("Bitcoin only", str(e.exception))
        finally:
            t.close()


class AccountSyncTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEsplora()
        self.t = TempContext(FakeHttp().on("https://mempool.space/api", self.fake))
        self.ctx = self.t.ctx

    def tearDown(self):
        self.t.close()

    def sync(self, key):
        w = add_wallet(self.ctx, "bitcoin", key, synced=False)
        sync.sync_wallet(self.ctx, self.ctx.wallet(w["id"]), LOG)
        return w

    def moves(self):
        return {r["uid"]: Decimal(r["amount"]) for r in self.ctx.db.query("SELECT uid, amount FROM movements")}

    def test_zpub_history_change_and_wallet_check(self):
        w = self.sync(ZPUB)
        # B pays 0.3 out and sends the rest to its own change address: not counted as a payment
        self.assertEqual(self.moves(), {"A:btc": Decimal(1), "B:fee": Decimal("-0.0001"), "B:btc": Decimal("-0.3"),
                                        "C:btc": Decimal("0.2")})
        bal = Decimal(self.ctx.db.scalar("SELECT amount FROM current_balances WHERE asset_key='bitcoin:native'"))
        self.assertEqual(bal, Decimal("0.8999"))
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "ok")
        self.assertEqual(self.ctx.sync_state(w["id"])["hd"]["types"], ["p2wpkh"])
        # gap limit: receive 0,1 used + 20 unused; change 0 used + 20 unused
        self.assertEqual(len(set(self.fake.stats_calls)), 22 + 21)

    def test_resume_reads_only_addresses_with_new_transactions(self):
        w = self.sync(ZPUB)
        self.fake.txs.append(tx("D", 1_730_000_000, [(EXT, 10_000_000)], [(R1, 5_000_000), (EXT, 4_990_000)], fee=10_000))
        self.fake.tx_calls.clear()
        again = get_adapter(self.ctx, self.ctx.networks.get("bitcoin")).sync(self.ctx.wallet(w["id"]),
                                                                             self.ctx.sync_state(w["id"]), LOG)
        self.assertEqual([m.uid for m in again.movements], ["D:btc"])
        self.assertEqual(self.fake.tx_calls, [R1])

    def test_plain_xpub_finds_its_address_type(self):
        w = self.sync(VECTORS["p2wpkh"]["xpub"])       # the same account, exported as "xpub" (e.g. Ledger Live)
        self.assertEqual(self.ctx.sync_state(w["id"])["hd"]["types"], ["p2wpkh"])
        self.assertEqual(sum(self.moves().values()), Decimal("0.8999"))
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "ok")
        self.fake.stats_calls.clear()
        get_adapter(self.ctx, self.ctx.networks.get("bitcoin")).sync(self.ctx.wallet(w["id"]),
                                                                     self.ctx.sync_state(w["id"]), LOG)
        self.assertFalse([a for a in self.fake.stats_calls if not a.startswith("bc1q")])   # other types not rescanned

    def test_empty_account(self):
        empty = hdwallet.ExtendedKey(VECTORS["p2tr"]["key"])
        self.assertTrue(empty)
        w = self.sync(VECTORS["p2tr"]["key"])          # no transactions on any type
        self.assertEqual(self.moves(), {})
        self.assertEqual(self.ctx.sync_state(w["id"])["hd"]["types"], [])
        self.assertIsNone(self.ctx.wallet(w["id"])["last_error"])


if __name__ == "__main__":
    unittest.main()
