# CryptoLedger

A local app for tracking your **own** crypto addresses and exchange accounts in one dashboard,
with every value in **EUR** at the rate of its time and balances taken at **23:59:59 of each
fiscal year** (31/12, Europe/Rome by default).

It runs on your computer and opens in your browser. Your addresses, API keys and ledger never
leave your machine: only public addresses are sent to block explorers, and only to read them.

![Dashboard (demo data)](docs/screenshot.png)

- **Settings page for your addresses**: pick the network, paste the address, done. One EVM
  address can be tracked on several chains at once. Exchange accounts use a read-only API key.
- **Transactions and balances from public APIs**: Ethereum and EVM chains (Etherscan V2 or any
  Etherscan-compatible explorer), Solana (Solscan), Ronin (Moralis), Tron, Bitcoin, Solar (SXP),
  Binance; manual accounts for anything else.
- **API call limits you control**: calls per second or per minute, and per day, for each service, so a free plan
  is never exceeded (Settings → Blockchain APIs and Price sources).
- **Year-end balances at 23:59:59** from the best available source (snapshot, chain state,
  full transaction history), cross-checked, with every value traced to its source.
- **EUR at the time**: Binance public 1-minute prices (no key, full history), CoinGecko,
  CoinMarketCap; USD prices converted with the official **ECB reference rate** of that day.
- **Exports kept up to date**: `balances_YYYY-12-31.csv` (every wallet × asset at 31/12),
  `transactions_YYYY.csv` (one row per transaction, EUR at its time) and `wallets.csv`.
- **Year-end report** per fiscal year: initial/final values, days held, IC estimate
  (Italian RW quadro) and an evidence file.
- **Easy to extend**: a new EVM chain is a form in Settings; a new blockchain or exchange API is
  one Python class (see [docs/ADDING_AN_ADAPTER.md](docs/ADDING_AN_ADAPTER.md)).

> CryptoLedger is a record-keeping tool, not tax advice. Check flagged values (⛔ / 🟡) and
> confirm your tax treatment with a professional.

## Quick start

You need **Python 3.9 or newer** (already on macOS; on Windows get it from python.org). Nothing
else is required.

```bash
git clone https://github.com/occhinog/cryptoledger.git
```

```bash
cd cryptoledger
```

```bash
python3 -m cryptoledger
```

The app opens at <http://127.0.0.1:8765>. On Windows use `py -m cryptoledger`, or double-click
`start.bat` (macOS: `start.command`).

Want to look around first? `python3 -m cryptoledger demo` starts with fictional wallets and
synthetic prices, fully offline, in a separate `demo-data/` folder.

### Optional extras

```bash
python3 -m pip install -r requirements-optional.txt
```

| Package | Why |
|---|---|
| `keyring` | Stores API keys in the macOS Keychain / Windows Credential Locker instead of `data/secrets.json` |
| `certifi` | Fixes "certificate verify failed" on python.org builds of Python for macOS |
| `tzdata` | Needed on Windows only, for time zones |

## Set up your data

1. **Wallets** → choose the network, paste the address, give it a label. For an EVM address,
   tick the other chains where you use it. Optional fields:
   - **Portfolio / owner** groups wallets (yours, a family member's, a client's); the dashboard filters by it.
   - **RW slot** fixes the row order in the Italian RW form.
   - **Bollo already applied**: tick it for accounts at an Italian intermediary, so no IC is estimated.
2. **Settings** has two boxes of APIs, one row per service. Each row takes the key (if any), the
   plan's **speed limit** (per second or per minute, as your provider states it) and **calls per
   day** (defaults are conservative free-plan values; empty = no limit). The app spaces its calls,
   shows the calls used today, and stops calling a service once its daily cap is reached, until
   00:00 UTC. **Test** makes one call to check the service and your key, and says what it cost:
   only CoinMarketCap's check is free; elsewhere it counts as one call at the provider.

   **Blockchain APIs** (where your wallets are read):

   | Service | Needed for | Free? |
   |---|---|---|
   | Etherscan | Ethereum, BNB Chain, Polygon, Arbitrum, Base, OP, Avalanche (one key for all) | Yes, [etherscan.io/myapikey](https://etherscan.io/myapikey). If your plan does not cover a chain, point it to Blockscout/Routescan in Blockchain endpoints |
   | Solana (Solscan) | Solana wallets; without a key the slow public endpoint is used | No: Pro API from Lite plan, [solscan.io/apis](https://solscan.io/apis) |

   Ronin (Moralis) and Tron (TronGrid) rows appear only once you add a wallet on those
   networks; Bitcoin and Solar need no key. Binance account keys (read-only) are entered per
   account on the Wallets page.

   **Price sources** (market prices, they never read your wallets): Binance public prices and
   ECB rates (no key), CoinGecko (optional key; Demo covers the last 365 days) and
   CoinMarketCap (optional key; free plan has current prices only), plus the order in which
   they are used and how USD prices become EUR.

   Solar (SXP) is winding down and its public API may move: if syncs fail, put the current URL
   in Settings → Blockchain endpoints (advanced).
3. Press **Sync now**. The first sync reads the whole history and can take a few minutes;
   later syncs only read what is new.
4. Press **Recompute year-end** (top bar) to compute balances and EUR values at every
   fiscal-year end. This never happens by itself: after a sync, a manual entry, a price mapping
   or a settings change the button lights up and the Year-end page says the values are out of
   date, until you press it. Recomputing also rewrites the exports.
5. **Assets**: tokens the app does not know are listed here. Map them to a CoinGecko id or a
   Binance ticker, or mark them as spam (airdropped scam tokens are common).
6. **Year-end**: check each wallet's balance source and flags, enter manual balances where an
   API cannot help, then download the report.

### Solana with Solscan

Solscan's API needs a paid plan (Lite or higher). Without a key Solana is read from the public
Solana endpoint, which works but is slow.

1. **Get a key.** Subscribe to a Solscan Pro API plan at [solscan.io/apis](https://solscan.io/apis),
   then open your Solscan account → **API Management** and copy the **V2** key (the V1 key
   listed there does not work on the endpoints the app uses).
2. **Add it to the app.** Settings → Blockchain APIs → **Solana (Solscan)** row → paste the key →
   **Save** → **Test**.
   It is stored in your OS keychain if `keyring` is installed, otherwise in `data/secrets.json`.
3. **Set your plan's limits** in the same row:
   - *Speed limit*: Lite allows 1,000 calls **per minute** (the default): enter 1000 and choose
     "per minute".
   - *Calls / day*: each call costs 100 compute units. Lite includes 20 million a month, i.e.
     200,000 calls a month, about **6,600 a day**. Enter that (or your plan's figure) so the
     app never runs your plan dry.
   - Press **Save**.
4. **Add your Solana wallets and sync.** Wallets → network **Solana** → paste the address →
   **Add wallet** → **Sync now** → **Recompute year-end**. With a Solscan key set, Solana always
   reads from Solscan.
5. **Check the first sync.** Compare the SOL and token balances on the Dashboard with the
   wallet's page on [solscan.io](https://solscan.io).

Good to know:

- A sync reads the wallet's balance changes, its transactions (40 per call) and its transfers,
  plus two calls for the live balances. A wallet with a long history costs more calls on the
  first sync; later syncs only read what is new.
- Removing the Solscan key switches Solana back to the public endpoint; both produce the same
  entries, so nothing is duplicated.
- *"Solscan … key refused (401)"*: you pasted the V1 key, or the Pro API plan is not active.
  Copy the **V2** key from API Management and press **Test**: it checks the key with Solscan's
  `/monitor/usage` call (100 compute units) and shows what is left on your plan.

## How year-end values are computed

The cutoff for fiscal year *Y* is 23:59:59 local time on the last day of *Y* (Settings → Fiscal
year). For each wallet, the balance at the cutoff comes from the best source available:

1. a **manual entry** for that cutoff;
2. a **live snapshot** taken within 5 minutes of the cutoff (see below);
3. **chain state** at the cutoff block, if you set an archive RPC URL (EVM);
4. the **full transaction history** summed up to the cutoff (exact for Bitcoin; Solana SOL uses
   the balance the chain reports after each transaction);
5. an **exchange daily snapshot** (Binance takes one at 23:59:59 UTC, kept for 30 days: sync in January).

When two sources exist they are compared and differences are flagged ⛔. Each asset is then
priced at the cutoff: Binance 1-minute close (`ETHEUR`, or `SOLUSDT` × ECB USD/EUR of that
day), else CoinGecko, else CoinMarketCap. Details: [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### Snapshots at the cutoff

While the app is running it snapshots every wallet about a minute before and after each cutoff.
To do it even when the app is closed, schedule `python3 -m cryptoledger snapshot`:

- **macOS**: edit and load [scripts/launchd/com.cryptoledger.snapshot.plist](scripts/launchd/com.cryptoledger.snapshot.plist):

  ```bash
  cp scripts/launchd/com.cryptoledger.snapshot.plist ~/Library/LaunchAgents/
  ```

  ```bash
  launchctl load ~/Library/LaunchAgents/com.cryptoledger.snapshot.plist
  ```

  The Mac must be awake at 23:59 on 31 December.
- **Linux** (cron uses the system time zone):

  ```bash
  (crontab -l; echo "59 23 31 12 * cd $PWD && python3 -m cryptoledger snapshot") | crontab -
  ```

- **Windows** (from the cryptoledger folder, in Command Prompt):

  ```bash
  schtasks /Create /TN CryptoLedgerSnapshot /SC MONTHLY /MO 12 /D 31 /SD 12/31/2026 /ST 23:59 /TR "py -m cryptoledger --data-dir %CD%\data snapshot"
  ```

## Exports

Kept in `data/exports/`. `wallets.csv` and `transactions_YYYY.csv` are rewritten after every
sync and when you edit wallets or categories; `balances_YYYY-12-31.csv` when you press
**Recompute year-end** and right after each 31/12 cutoff (unchanged files are not touched). Download them from the Year-end, Transactions and Wallets pages, or run
`python3 -m cryptoledger export`.

| File | When | One row per | Columns |
|---|---|---|---|
| `balances_YYYY-12-31.csv` | every fiscal year, at 31/12 23:59:59 | wallet × asset | wallet, network, asset, quantity, price_eur, value_eur, price_source |
| `transactions_YYYY.csv` | every fiscal year | transaction | datetime, wallet, network, tx_hash, type, asset_in, qty_in, asset_out, qty_out, fee_asset, fee_qty, value_eur, from_address, to_address |
| `wallets.csv` | whenever your wallets change | wallet | wallet, network, address, platform |

`type` is one of `buy`, `sell`, `swap`, `transfer_in`, `transfer_out`, `reward`, `airdrop`, `fee`,
taken from the transaction's category (editable on the Transactions page). `value_eur` is the
value at the moment of the transaction. Details: [docs/REPORT_CONTRACT.md](docs/REPORT_CONTRACT.md).

## Reports

Year-end → **Write report folder** (or `python3 -m cryptoledger report 2025`) creates
`data/reports/crypto_report_TY2025/`:

| File | Content |
|---|---|
| `rw_crypto.csv` | One row per wallet held in the year: RW slot, initial value (col. 7), final value (col. 8), days held, bollo flag, IC estimate, notes |
| `year_end_balances.csv` | Every asset at the opening and closing cutoff: quantity, EUR price, price source and quote time, balance source, flags |
| `transactions.csv` | Every movement of the year with its category and EUR value at its own time |
| `evidence.md` | Sources, prices used, last data pulls and open flags, for your records |

The format is described in [docs/REPORT_CONTRACT.md](docs/REPORT_CONTRACT.md).

## Privacy and security

- Everything personal lives in `data/` (git-ignored): the SQLite ledger, `secrets.json` if no
  keychain is available, and reports. Back it up; delete it to start over.
- The server listens on `127.0.0.1` only, rejects foreign `Host` headers and requires a custom
  header on every change, so other websites cannot drive it.
- API keys are never sent to the browser. Use read-only exchange keys.

## Command line

```bash
python3 -m cryptoledger --help
```

`serve` (default), `sync [--wallet ID] [--full]`, `snapshot`, `report YEAR`, `demo`, and
`--data-dir PATH` to keep the data elsewhere (an encrypted volume, for example).

## Not done yet

- Tax computation (capital gains with weighted-average cost, RT quadro): the ledger and the
  categories are ready for it.
- Binance trades/Convert/Earn import (balances come from snapshots meanwhile).
- Bitcoin xpub wallets, DeFi positions, NFTs, rebasing tokens (use snapshots or manual entries).

## Development

```bash
python3 -m unittest discover -s tests
```

No dependencies, no build step: Python standard library on the server, plain JavaScript in
`web/`. Contribution notes for humans and coding agents: [AGENTS.md](AGENTS.md).
