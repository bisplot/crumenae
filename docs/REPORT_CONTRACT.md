# Export and report contract

Two sets of files. Downstream tools (a tax-return workflow, a spreadsheet) should read these
files and nothing else. Columns are stable; new columns are only appended.

1. **Standard exports** in `<data>/exports/`, always up to date (below).
2. **Year-end report folder**, written on demand by `python3 -m crumenae report <YYYY>` or
   Year-end → Write report folder, with the RW-oriented files. Amounts there are plain decimals
   with a dot, EUR values unrounded unless stated.

## Standard exports: `<data>/exports/`

`wallets.csv` and `transactions_YYYY.csv` are rewritten after every sync and after edits to
wallets and categories. `balances_YYYY-MM-DD.csv` follows the year-end values, which are only
computed on request (Recompute year-end) and right after each fiscal-year cutoff while the app
runs. All files are also rewritten by `python3 -m crumenae export`. A file whose content did not change is not rewritten.
Files exist for every completed fiscal year; the current year's transactions can be downloaded
from the Transactions page (year-to-date).

Common rules: UTF-8, comma-separated, header row, dot as decimal separator, quantities at full
precision, EUR values with exactly 2 decimals, `network` is the network id (`ethereum`, `bsc`,
`solana`, `tron`, `bitcoin`, `binance`, `manual`, or a custom id). An empty cell means unknown.

### `wallets.csv`

One row per wallet: `wallet, network, address, platform`.

- `wallet`: the label you gave it.
- `address`: empty for exchange and manual accounts.
- `platform`: what you entered (MetaMask, Ledger, Binance…); if empty, `Binance` for the exchange
  account, `manual` for manual accounts, `self-custody` for addresses.

### `balances_YYYY-MM-DD.csv`

One row per wallet × asset held at 23:59:59 of the fiscal-year end (`balances_2025-12-31.csv` for
TY2025): `wallet, network, asset, quantity, price_eur, value_eur, price_source`.

- `asset`: ticker as shown in the app (the same ticker on two networks is told apart by `network`).
- `price_source`: provider, pair and conversion, e.g. `binance SOLUSDT 1m close × ECB USD/EUR 1.18 (2025-12-31)`.
  When no price exists, `price_eur` and `value_eur` are empty and `price_source` starts with `none:`
  followed by the reason. Assets marked spam are not listed.

### `transactions_YYYY.csv`

One row per transaction of the fiscal year, per wallet: `datetime, wallet, network, tx_hash, type,
asset_in, qty_in, asset_out, qty_out, fee_asset, fee_qty, value_eur, from_address, to_address`.

- `datetime`: ISO 8601 in the fiscal time zone, e.g. `2025-03-05T16:00:00+01:00`.
- Quantities are positive; `qty_out` and `fee_qty` are what left the wallet.
- Same asset in and out within one transaction is netted (a partial refund shows as the net amount).
- A transaction moving more than one asset in or out (e.g. a liquidity deposit) uses one row per
  extra asset, with the same `tx_hash`; the fee is on the first row.
- A transfer between two of your wallets appears once in each: `transfer_out` and `transfer_in`.
- `type`, from the category of the transaction's movements:

  | type | when |
  |---|---|
  | `buy` / `sell` | a movement is categorised buy / sell |
  | `swap` | one asset leaves and another arrives (category swap or swap_taxable, or automatic) |
  | `reward` / `airdrop` | an incoming movement is categorised reward / airdrop |
  | `transfer_in` / `transfer_out` | anything else received / sent (own-wallet transfers, deposits, withdrawals, payments) |
  | `fee` | only a fee was paid (failed transaction, approval, staking operation) |

- `value_eur`: value at the minute of the transaction of what was received (`asset_in`); if
  nothing was received, of what was sent; for `fee` rows, of the fee. Fees are never included.
  Empty when no price is available.
- `from_address` / `to_address`: sender and recipient as reported by the chain. For a swap, the
  wallet and the contract it called. Binance does not report deposit senders: empty.
- Spam (zero-value transfers, assets marked spam) is not exported.

## Year-end report folder: `<data>/reports/crypto_report_TY<YYYY>/`

### `rw_crypto.csv`

One row per wallet held during the year, ordered by RW slot.

| Column | Meaning |
|---|---|
| `slot` | permanent RW slot set on the wallet (empty if none) |
| `wallet`, `network`, `address` | identification |
| `col7_initial_value_eur` | initial value, 2 decimals |
| `col8_final_value_eur` | final value, 2 decimals |
| `days_held` | days held in the year (empty = unknown, see notes) |
| `quota_pct` | ownership share, always 100 |
| `bollo_applied` | 1 if an Italian intermediary applies the stamp duty (no IC) |
| `ic_eur` | IC estimate, 2 decimals (empty when bollo applies or inputs are missing) |
| `notes` | flags separated by ` | `: ⛔ must be fixed, 🟡 to review |

### `year_end_balances.csv`

One row per wallet × asset × (opening, closing) cutoff: `fiscal_year, balance, cutoff_utc,
wallet, network, portfolio, address, asset, asset_key, quantity, price_eur, value_eur,
balance_source, price_source, price_time_utc, flags`.

### `transactions.csv` (one row per movement)

Every movement in the fiscal year: `time_local, wallet, network, tx_hash, asset, asset_key,
amount, kind, category, counterparty, price_eur, value_eur, price_source, status, note`.

### `evidence.md` and `report.json`

Human-readable sources, prices, data pulls and open flags; `report.json` is the same data as
the Year-end page.

### Planned (tax module)

Not produced yet; reserved names so consumers can rely on them later:

- `rt_crypto.csv`: proceeds, cost (weighted average), gains, losses, income (rewards/airdrops),
  prior losses used, carry-forward, tax at the applicable rate.
- `events_taxable.csv`: each taxable event with the lots and cost used.
- `loss_carryforward.csv`.
- `reconciliation.md`: against exchange certificates/statements and DAC8 data.
