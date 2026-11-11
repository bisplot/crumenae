"""The app was renamed from CryptoLedger to Crumenae: the old database file, keychain entries
and environment variables keep working."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from crumenae.context import Context
from crumenae.secrets import SecretStore


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


class RenameTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_old_database_file_is_adopted(self):
        con = sqlite3.connect(self.dir / "cryptoledger.db")
        con.execute("CREATE TABLE marker(x)")
        con.commit()
        con.close()
        ctx = Context(self.dir, secrets_backend="file", offline=True)
        try:
            self.assertFalse((self.dir / "cryptoledger.db").exists())
            self.assertTrue(ctx.db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='marker'"))
        finally:
            ctx.db.close()

    def test_old_keychain_entry_is_found_and_copied(self):
        store = SecretStore(self.dir, backend="file")
        store._keyring = FakeKeyring()
        store._keyring.set_password("cryptoledger", "alchemy_api_key", "K")
        self.assertEqual(store.get("alchemy_api_key"), "K")
        self.assertEqual(store._keyring.get_password("crumenae", "alchemy_api_key"), "K")
        store.delete("alchemy_api_key")
        self.assertIsNone(store.get("alchemy_api_key"))                 # gone under both names

    def test_old_environment_variable_still_read(self):
        os.environ["CRYPTOLEDGER_ETHERSCAN_API_KEY"] = "E"
        try:
            store = SecretStore(self.dir, backend="file")
            self.assertEqual((store.get("etherscan_api_key"), store.source("etherscan_api_key")), ("E", "env"))
        finally:
            del os.environ["CRYPTOLEDGER_ETHERSCAN_API_KEY"]


if __name__ == "__main__":
    unittest.main()
