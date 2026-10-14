"""Offline regression suite; no Telegram account or provider key is used."""

import unittest

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.discover("tests")
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
