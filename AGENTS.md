# Working on CryptoLedger

Notes for contributors and coding agents (Claude Code reads `CLAUDE.md`, which points here).

## What this is

A local web app: Python standard-library server (`cryptoledger/`) + plain JavaScript UI (`web/`).
It tracks the user's own addresses, stores a normalized ledger in SQLite, values everything in
EUR at the time, and computes balances at each fiscal-year cutoff (23:59:59 local time).

## Rules

- **No personal data in the repository.** No real wallet addresses, API keys, balances, tax
  figures or names in code, tests, fixtures, docs or commit messages. Test fixtures use synthetic
  or well-known public contract addresses only. Everything personal lives in `data/` (ignored).
- **Standard library only** at runtime. Optional extras (`keyring`, `certifi`, `tzdata`) must
  stay optional. Support Python 3.9+ (`from __future__ import annotations`, no `match`, no `X | Y`
  at runtime). Check with `vermin -t=3.9- cryptoledger tests`.
- **Never guess a value.** If a balance or price cannot be established, store none and add a
  flag (⛔ blocking, 🟡 to review). Every stored value keeps its source and timestamp.
- **Prices by contract, not by ticker.** Unknown tokens stay unmapped until mapped by contract
  (CoinGecko lookup) or by the user.
- **UI safety:** token names and symbols come from the blockchain and are untrusted. In
  `web/app.js` insert text only through `el()` (textContent). No `innerHTML` with data.
- **Local-only server:** keep the Host-header check and the `X-CryptoLedger` header requirement
  on state-changing requests. Secrets are never returned by the API.
- **No long transactions around network calls:** the DB lock is shared with the UI threads.

## Layout

| Path | Role |
|---|---|
| `cryptoledger/adapters/` | one module per API (evm, solana, tron, bitcoin, binance, manual) |
| `cryptoledger/prices/` | Binance klines, CoinGecko, CoinMarketCap, ECB rates, demo prices |
| `cryptoledger/ledger.py` | storing sync results, reconstructing balances |
| `cryptoledger/yearend.py` | balance at each cutoff from the best source + cross-checks |
| `cryptoledger/report.py` | RW values, days held, CSV/Markdown exports |
| `cryptoledger/server.py` | JSON API + static UI |
| `config/*.json` | networks, known tokens, ticker map |
| `docs/` | methodology, report contract, how to add an adapter |

## Workflow

```bash
python3 -m unittest discover -s tests
```

```bash
python3 -m cryptoledger demo --port 8799 --no-browser
```

Add a test with every adapter or engine change (see `tests/helpers.py: FakeHttp`). Keep
`docs/REPORT_CONTRACT.md` in sync with any export change: columns are only appended.
