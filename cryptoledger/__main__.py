"""`python3 -m cryptoledger …` runs Crumenae, the app's new name."""
import sys

from crumenae.__main__ import main

print("Note: CryptoLedger is now Crumenae; use `python3 -m crumenae` from now on.", file=sys.stderr)
main()
