#!/bin/bash
# macOS: double-click to start CryptoLedger (it opens in your browser).
cd "$(dirname "$0")" || exit 1
exec python3 -m cryptoledger "$@"
