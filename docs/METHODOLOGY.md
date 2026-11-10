# Methodology

## Cutoff

Fiscal year *Y* ends at **23:59:59 local time** on the fiscal-year-end day (Settings: time zone,
default `Europe/Rome`; end day, default `12-31`). For Italy the TY2025 cutoff is
2025-12-31 23:59:59 CET = 22:59:59 UTC. The opening balance of *Y* is the closing balance of *Y−1*.

## Balance at the cutoff

Sources in priority order. The wallet's year-end row records which one was used.

| # | Source | When | Quality |
|---|---|---|---|
| 1 | Manual entry | you typed it for that cutoff | as good as your statement |
| 2 | Live snapshot | taken by the app within ±300 s (configurable) of the cutoff | exact |
| 3 | Chain state | archive RPC `eth_getBalance` / `balanceOf` at the last block before the cutoff (EVM) | exact |
| 4 | Transaction history | sum of every movement up to the cutoff; SOL uses the post-transaction balance reported by the chain | exact if the history is complete |
| 5 | Exchange daily snapshot | Binance Spot snapshot at 23:59:59 UTC (1 h after a CET cutoff), kept 30 days | approximate, Spot only |

Cross-checks: when a snapshot or chain state exists together with the history, each asset is
compared and any difference is flagged ⛔. Negative reconstructed balances (missing history) are
flagged ⛔. On rollups (Arbitrum, OP, Base) explorer fees may exclude L1 data fees: flagged 🟡.

Known limits of history-based balances: rebasing tokens (stETH…), staking rewards credited
without a transfer, DeFi positions. Use snapshots, an archive RPC or manual entries for those.

## EUR prices

Every price is stored with its source and the exact quote time.

1. **Binance public market data** (`data-api.binance.vision`, no key, history since listing):
   close of the 1-minute candle containing the moment. Pairs tried: `<SYM>EUR`, `EUR<SYM>`
   (inverted), then `<SYM>USDT/USDC/FDUSD/BUSD` converted to EUR.
2. **CoinGecko** `market_chart/range` in EUR: last point at or before the moment (5-minute to
   hourly resolution). Demo keys only reach 365 days back.
3. **CoinMarketCap**: latest quotes on the free plan; historical quotes only on paid plans.

**USD → EUR**: by default the ECB euro reference rate of that day (`data-api.ecb.europa.eu`),
the last one published on or before the date (weekends and TARGET holidays). Stablecoins quoted
in USDT are treated as 1 USD and flagged. Setting: use Binance `EURUSDT` at the same minute
instead. Fiat balances on exchanges are converted with ECB rates; EUR is 1.

**Ghost tokens** (Assets page): real tokens that can no longer be traded. They stay in the
balances; a price is still looked up through their mapping (if any) for moments when they
traded, otherwise the value is €0 with source `ghost (no market)` and an ℹ flag instead of ⛔.
Live prices are looked up for them too (one Binance ticker call, then CoinGecko): the current
value is that price when a market still exists (CoinGecko quotes older than 7 days are ignored),
else €0. Clear the mapping to force €0 for every date. Ghost and spam tokens are not shown on
the Dashboard (its total excludes them); year-end values still include ghost tokens.

**Live prices** (each sync): Binance first, with one call for all symbols (EUR pair, inverted
EUR pair, or a USD stablecoin pair converted at the ECB rate), then CoinGecko for the rest. An
asset no source can price is remembered for 24 hours and not asked again in that time.

Tokens are matched to prices by **contract address** (built-in list, then CoinGecko's contract
lookup), never by ticker: scam tokens often copy real tickers. Unmatched tokens are not valued
and are listed on the Assets page.

## Transaction categories

Applied automatically, editable on the Transactions page; your edits are never overwritten.

| Category | Rule | Italian tax treatment (for the future RT module) |
|---|---|---|
| `internal_transfer` | counterparty is one of your wallets, or the same tx hash appears in two of your wallets (e.g. Binance withdrawal ↔ on-chain deposit) | neutral, cost carried over |
| `swap` | the same transaction sends one asset and receives another | neutral if same function; set `swap_taxable` for stablecoin/EMT/NFT |
| `fee` | gas or exchange fee paid by you | — |
| `spam` | zero-value transfer or asset marked spam | ignored |
| `deposit` / `withdrawal` | anything else, to classify: `buy`, `sell`, `reward`, `payment`… | depends |

## Staking

Staked coins stay in the wallet that staked them and are valued at each cutoff with it (RW and
IC like any other holding). Terra/Terra Classic delegations and unbonding, and frozen/unfreezing
TRX, are part of the wallet balance; a delegation or a freeze is not a disposal. Each sync also
reads the positions themselves (staked, unbonding, unclaimed rewards per validator, Binance
Simple Earn products, liquid-staking tokens held) for the Staking page; that reading does not
change any balance. Manual accounts ticked as staking are valued from their year-end entries.

Solana native staking: each stake account whose withdraw authority is the wallet is a separate
asset of that wallet ("SOL (staked)", priced as SOL). Its balance is anchored to the chain: the
balance after each transaction that touches it, and after each epoch's inflation reward
(`getInflationReward` gives the reward and the post-reward balance). Moving SOL into or out of a
stake account is an internal transfer. Rewards are paid over a few blocks at the start of the next
epoch; each is dated from its block (one block time per epoch, the others at 0.4 s per slot).

Contract staking on EVM chains (`config/staking.json`): the coins in a staking contract are a
separate asset of the wallet (`<network>:stake:<contract>`, priced as the native coin), and
sending coins to or from the contract is an internal transfer. Its year-end balance is read from
the contract at the cutoff block (archive node, source `chain_state`). Ronin: the history comes
from the contract's Delegated / Undelegated / RewardClaimed events; each claim is a `reward` at
its time (with "Claim and restake" too: the reward is received, then delegated again). StakeWise
v3 vaults (MetaMask Pooled Staking): the value is `convertToAssets(getShares)` plus the ETH in the
exit queue; rewards raise the share price with no transaction, so they are part of the value, not
income movements (the Staking page shows "rewards so far" = value − deposits + withdrawals). If
your adviser treats them as income, add them by hand.

Rewards are movements with category `reward`, valued in EUR at the moment of receipt: claimed
Terra rewards, claimed Tron voting rewards (WithdrawBalance), Binance Simple Earn rewards, and
rewards typed by hand on manual accounts (they never change a manual balance). Unclaimed rewards
are shown but are not income until claimed.

## RW values (Italy)

For each wallet held during the fiscal year:

- **Initial value (col. 7)**: value at the opening cutoff; if opened during the year, value at
  23:59:59 of the first day held.
- **Final value (col. 8)**: value at the closing cutoff; if emptied during the year, value at
  23:59:59 of the last day it closed with a balance.
- **Days held**: local days with a positive balance at any moment (from the history). Without
  history: 365/366 if held at both cutoffs, otherwise flagged ⛔.
- **IC estimate**: final value × IC rate (0.2%) × days / days in year, only where no Italian
  bollo is applied. The tax return decides the final figure.
