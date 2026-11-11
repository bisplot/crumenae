# Working on Crumenae

Notes for contributors and coding agents (Claude Code reads `CLAUDE.md`, which points here).

## What this is

A local web app: Python standard-library server (`crumenae/`) + plain JavaScript UI (`web/`).
It tracks the user's own addresses, stores a normalized ledger in SQLite, values everything in
EUR at the time, and computes balances at each fiscal-year cutoff (23:59:59 local time).

## Rules

- **No personal data in the repository.** No real wallet addresses, API keys, balances, tax
  figures or names in code, tests, fixtures, docs or commit messages. Test fixtures use synthetic
  or well-known public contract addresses only. Everything personal lives in `data/` (ignored).
- **Standard library only** at runtime. Optional extras (`keyring`, `certifi`, `tzdata`) must
  stay optional. Support Python 3.9+ (`from __future__ import annotations`, no `match`, no `X | Y`
  at runtime). Check with `vermin -t=3.9- crumenae tests`.
- **Never guess a value.** If a balance or price cannot be established, store none and add a
  flag (⛔ blocking, 🟡 to review). Every stored value keeps its source and timestamp.
- **Prices by contract, not by ticker.** Unknown tokens stay unmapped until mapped by contract
  (CoinGecko lookup) or by the user.
- **UI safety:** token names and symbols come from the blockchain and are untrusted. In
  `web/app.js` insert text only through `el()` (textContent). No `innerHTML` with data.
- **Local-only server:** keep the Host-header check and the `X-Crumenae` header requirement
  on state-changing requests. Secrets are never returned by the API.
- **No long transactions around network calls:** the DB lock is shared with the UI threads.
- **Providers are data, not code:** a new service that speaks a supported type (Etherscan-,
  Alchemy-compatible, Cosmos SDK LCD, Esplora, Horizon) is added from Settings → API providers, and a
  new chain from Settings → Networks. Write an adapter only for a new protocol, and register its
  type in `providers.TYPES` so its providers are addable from the UI (docs/ADDING_AN_ADAPTER.md).
- **One free key to start:** every built-in EVM chain must work with an Alchemy key alone.
  `auto_providers` in `config/networks.json` picks the first provider whose key is saved
  (Etherscan where its free plan works); a choice in Settings wins. EVM readers stamp
  `state["reader"]` and resume after the last block when the reader changes (`evm.resume_block`).
- **Brand in one place:** colours, font and radii are CSS variables in `web/brand.css`; never
  hard-code them in `web/style.css` or `web/app.js`. Logo and icons are files in `web/`. Follow
  [docs/BRAND.md](docs/BRAND.md) (primary actions `.btn.primary`, exports `.btn.highlight`).
  The UI makes no external requests: fonts and images are bundled.

## Layout

| Path | Role |
|---|---|
| `crumenae/adapters/` | one module per protocol (evm, alchemy, cosmos, solana, solar, stellar, tron, bitcoin, binance, manual) |
| `crumenae/providers.py` | API provider types and built-in providers; providers added in Settings live in the `providers` setting |
| `crumenae/prices/` | Binance klines, CoinGecko, CoinMarketCap, ECB rates, demo prices |
| `crumenae/ledger.py` | storing sync results, reconstructing balances |
| `crumenae/yearend.py` | balance at each cutoff from the best source + cross-checks |
| `crumenae/report.py` | RW values, days held, report folder (CSV/Markdown) |
| `crumenae/exports.py` | standard exports: wallets.csv, balances_YYYY-12-31.csv, transactions_YYYY.csv |
| `crumenae/server.py` | JSON API + static UI |
| `config/*.json` | networks, known tokens, ticker map |
| `web/` | UI: `index.html`, `app.js`, `style.css` (layout), `brand.css` (colours, font), logo, favicons, `fonts/` |
| `docs/` | methodology, report contract, how to add an adapter, brand guideline (`BRAND.md`, `brand/`) |

## Workflow

```bash
python3 -m unittest discover -s tests
```

```bash
python3 -m crumenae demo --port 8799 --no-browser
```

Add a test with every adapter or engine change (see `tests/helpers.py: FakeHttp`). Keep
`docs/REPORT_CONTRACT.md` in sync with any export change: columns are only appended.
