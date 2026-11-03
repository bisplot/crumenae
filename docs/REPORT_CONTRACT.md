# Year-end report contract

`python3 -m cryptoledger report <YYYY>` (or Year-end → Write report folder) writes
`<data>/reports/crypto_report_TY<YYYY>/`. Downstream tools (a tax-return workflow, a spreadsheet)
should read these files and nothing else. Columns are stable; new columns are only appended.
All amounts are plain decimals with a dot, EUR values unrounded unless stated.

## `rw_crypto.csv` (implemented)

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

## `year_end_balances.csv` (implemented)

One row per wallet × asset × (opening, closing) cutoff: `fiscal_year, balance, cutoff_utc,
wallet, network, portfolio, address, asset, asset_key, quantity, price_eur, value_eur,
balance_source, price_source, price_time_utc, flags`.

## `transactions.csv` (implemented)

Every movement in the fiscal year: `time_local, wallet, network, tx_hash, asset, asset_key,
amount, kind, category, counterparty, price_eur, value_eur, price_source, status, note`.

## `evidence.md` and `report.json` (implemented)

Human-readable sources, prices, data pulls and open flags; `report.json` is the same data as
the Year-end page.

## Planned (tax module)

Not produced yet; reserved names so consumers can rely on them later:

- `rt_crypto.csv`: proceeds, cost (weighted average), gains, losses, income (rewards/airdrops),
  prior losses used, carry-forward, tax at the applicable rate.
- `events_taxable.csv`: each taxable event with the lots and cost used.
- `loss_carryforward.csv`.
- `reconciliation.md`: against exchange certificates/statements and DAC8 data.
