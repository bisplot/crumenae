"""The Settings page shows one limits row per API provider: every provider must have its limits
(a provider without one made the whole page fail: "Cannot read properties of undefined")."""
import unittest

from crumenae import providers
from crumenae.server import App

from helpers import TempContext


class ProviderLimitsTest(unittest.TestCase):
    def test_every_provider_has_limits(self):
        t = TempContext(offline=True)
        try:
            app = App(t.ctx)
            limits = {row["id"] for row in app.dispatch("GET", "/api/limits", {}, None)}
            data = app.dispatch("GET", "/api/providers", {}, None)
            provs = data if isinstance(data, list) else data.get("providers", [])
            missing = [p["id"] for p in provs if p["id"] not in limits]
            self.assertEqual(missing, [])
            self.assertTrue({p["id"] for p in providers.BUILTIN} <= limits)
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
