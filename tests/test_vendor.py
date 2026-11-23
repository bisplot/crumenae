"""Vendored front-end libraries: present, pinned, licensed, loaded before the app."""
import hashlib
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


class VendorTest(unittest.TestCase):
    def test_anime_is_pinned_and_licensed(self):
        lib = WEB / "vendor" / "anime.umd.min.js"
        readme = (WEB / "vendor" / "README.md").read_text(encoding="utf-8")
        self.assertIn("@version v4.5.0", lib.read_text(encoding="utf-8")[:300])
        self.assertIn(hashlib.sha256(lib.read_bytes()).hexdigest(), readme)      # the file is the one recorded
        self.assertIn("MIT License", (WEB / "vendor" / "anime.LICENSE.md").read_text(encoding="utf-8"))

    def test_loaded_before_the_app_and_optional(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/vendor/anime.umd.min.js"), html.index("/app.js"))
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (!A || !A.animate || reduceMotion()) return;", js)     # missing library / reduce motion
        self.assertTrue(re.search(r"prefers-reduced-motion: reduce", js))


if __name__ == "__main__":
    unittest.main()
