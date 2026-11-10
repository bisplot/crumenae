"""Demo data: fictional wallets and transactions so you can explore the app
without any API key. Runs fully offline, in its own data folder (demo-data/)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from . import assets as assets_mod
from . import classify, ledger, yearend
from .b58 import b58encode
from .context import Context
from .models import AssetInfo, Movement, Snapshot, StakePosition, SyncResult
from .prices import PriceService
from .util import now


def _t(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def _wallet(ctx: Context, label: str, network: str, address: str, portfolio: str = "Personal",
            rw_slot: Optional[int] = None, bollo: int = 0, created: str = "2022-12-01", platform: str = "") -> dict:
    cur = ctx.db.execute(
        "INSERT INTO wallets(label, network, address, portfolio, platform, rw_slot, bollo_applied, created_at,"
        " last_sync_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (label, network, address, portfolio, platform, rw_slot, bollo, _t(created + "T00:00:00"), now()))
    return ctx.wallet(cur.lastrowid)


def seed(ctx: Context) -> None:
    if ctx.db.scalar("SELECT COUNT(*) FROM wallets"):
        return
    ctx.db.set_meta("demo", "1")
    ctx.offline = True
    ctx.settings.set("first_fiscal_year", 2023)
    nets = {n.id: n for n in ctx.networks.all()}
    eth_native = assets_mod.native_asset(nets["ethereum"])
    usdc = assets_mod.token_asset(ctx, nets["ethereum"], "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
    spam = AssetInfo(key="ethereum:0x00000000000000000000000000000000000f4ee0", network="ethereum",
                     symbol="FREE-AIRDROP", name="Visit claim-reward.example", decimals=18,
                     contract="0x00000000000000000000000000000000000f4ee0")
    sol_native = assets_mod.native_asset(nets["solana"])
    btc_native = assets_mod.native_asset(nets["bitcoin"])
    bn = {t: assets_mod.ticker_asset(ctx, "binance", t) for t in ("BTC", "ETH", "BNB", "EUR", "SOL")}
    manual = {t: assets_mod.ticker_asset(ctx, "manual", t) for t in ("DOT", "ADA")}
    assets_mod.upsert(ctx, [eth_native, usdc, spam, sol_native, btc_native] + list(bn.values()) + list(manual.values()))

    binance = _wallet(ctx, "Binance account", "binance", "", rw_slot=1, bollo=1)
    eth = _wallet(ctx, "MetaMask (demo)", "ethereum", "0x0000000000000000000000000000000000de0001", rw_slot=3,
                  platform="MetaMask")
    sol = _wallet(ctx, "Phantom (demo)", "solana", b58encode(bytes(range(1, 33))), rw_slot=2, created="2025-03-01",
                  platform="Phantom")
    btc = _wallet(ctx, "Bitcoin cold wallet (demo)", "bitcoin", "bc1q" + "q" * 38, rw_slot=4, platform="Ledger")
    man = _wallet(ctx, "Ledger Live staking (manual)", "manual", "", rw_slot=5, platform="Ledger Live")
    _wallet(ctx, "Client wallet (demo)", "ethereum", "0x0000000000000000000000000000000000de0002",
            portfolio="Client A")

    def D(x: str) -> Decimal:
        return Decimal(x)

    h1, h2, h3 = "0x" + "a1" * 32, "0x" + "b2" * 32, "0x" + "c3" * 32
    eth_moves: List[Movement] = [
        Movement("t1:in", h1, _t("2023-03-14T10:20:00"), eth_native.key, D("2.0"), "in", "0xbinance-hot-wallet"),
        Movement("t2:fee", h2, _t("2023-06-02T18:05:00"), eth_native.key, D("-0.0042"), "fee", "0xrouter"),
        Movement("t2:out", h2, _t("2023-06-02T18:05:00"), eth_native.key, D("-0.5"), "out", "0xrouter", info="swap"),
        Movement("t2:usdc", h2, _t("2023-06-02T18:05:00"), usdc.key, D("905.12"), "in", "0xrouter", info="swap"),
        Movement("t3:in", "0x" + "d4" * 32, _t("2024-02-20T09:00:00"), eth_native.key, D("0.1"), "in", "0xstaking"),
        Movement("t4:fee", h3, _t("2025-05-10T12:30:00"), eth_native.key, D("-0.0011"), "fee", "0xshop"),
        Movement("t4:out", h3, _t("2025-05-10T12:30:00"), usdc.key, D("-300"), "out", "0xshop"),
        Movement("t5:spam", "0x" + "e5" * 32, _t("2025-11-02T03:00:00"), spam.key, D("5000"), "in", "0xspammer"),
    ]
    ledger.store_sync_result(ctx, eth, SyncResult(movements=eth_moves))
    ledger.store_current(ctx, eth["id"], {eth_native.key: D("1.5947"), usdc.key: D("605.12"), spam.key: D("5000")}, now())

    sol_moves = [
        Movement("s1:in", "5demoSig1", _t("2025-03-05T15:00:00"), sol_native.key, D("10"), "in", "binance",
                 balance_after=D("10")),
        Movement("s2:fee", "5demoSig2", _t("2025-09-01T11:00:00"), sol_native.key, D("-0.000005"), "fee",
                 balance_after=D("7.499995")),
    ]
    # Native staking: 2.5 SOL delegated to a validator from a stake account, rewards each epoch.
    stake_acct = b58encode(bytes([7] * 32))
    staked = assets_mod.staked_asset(nets["solana"], stake_acct)
    assets_mod.upsert(ctx, [staked])
    sol_moves += [
        Movement("s2:out", "5demoSig2", _t("2025-09-01T11:00:00"), sol_native.key, D("-2.5"), "out", stake_acct,
                 balance_after=D("7.499995")),
        Movement(f"s2:stake:{stake_acct}", "5demoSig2", _t("2025-09-01T11:00:00"), staked.key, D("2.5"), "in",
                 sol["address"], info="stake", balance_after=D("2.5")),
    ]
    bal = D("2.5")
    for i, day in enumerate(("2025-10-03", "2025-11-07", "2025-12-12", "2026-01-16")):
        bal += D("0.0012")
        sol_moves.append(Movement(f"stake:{stake_acct}:reward:{900 + i}", "", _t(day + "T06:00:00"), staked.key,
                                  D("0.0012"), "in", "", info="reward: Solana staking", balance_after=bal))
    ledger.store_sync_result(ctx, sol, SyncResult(movements=sol_moves))
    ledger.store_current(ctx, sol["id"], {sol_native.key: D("7.499995"), staked.key: bal}, now())
    ledger.store_staking(ctx, sol["id"], [StakePosition(staked.key, bal, protocol="Solana native staking",
                                                         validator=b58encode(bytes([8] * 32)))], now())

    btc_moves = [
        Movement("b1", "f1" * 32, _t("2022-12-20T08:00:00"), btc_native.key, D("0.05"), "in", "bc1qsender"),
        Movement("b2", "f2" * 32, _t("2024-04-11T08:00:00"), btc_native.key, D("0.02"), "in", "bc1qsender"),
        Movement("b3:fee", "f3" * 32, _t("2025-08-08T08:00:00"), btc_native.key, D("-0.00002"), "fee"),
        Movement("b3", "f3" * 32, _t("2025-08-08T08:00:00"), btc_native.key, D("-0.01"), "out", "bc1qshop"),
    ]
    ledger.store_sync_result(ctx, btc, SyncResult(movements=btc_moves))
    ledger.store_current(ctx, btc["id"], {btc_native.key: D("0.05998")}, now())

    # Binance: deposits/withdrawals (link to the on-chain wallets) + daily snapshots at 23:59:59 UTC.
    bmoves = [
        Movement("wd:1", h1, _t("2023-03-14T10:19:00"), bn["ETH"].key, D("-2.0"), "out", eth["address"]),
        Movement("wd:2", "5demoSig1", _t("2025-03-05T14:59:00"), bn["SOL"].key, D("-10"), "out", sol["address"]),
    ]
    snaps = []
    for day, holdings in (("2022-12-31", {"BTC": "0.01", "EUR": "50"}),
                          ("2023-12-31", {"BTC": "0.03", "ETH": "0.8", "BNB": "1.2", "EUR": "120"}),
                          ("2024-12-31", {"BTC": "0.035", "ETH": "1.1", "BNB": "1.5", "SOL": "10", "EUR": "300"}),
                          ("2025-12-31", {"BTC": "0.04", "ETH": "0.9", "BNB": "2.0", "EUR": "220"})):
        for t, q in holdings.items():
            snaps.append(Snapshot(_t(day + "T23:59:59"), bn[t].key, D(q), "exchange_daily",
                                  "Binance daily Spot snapshot 23:59:59 UTC"))
    ledger.store_sync_result(ctx, binance, SyncResult(movements=bmoves, snapshots=snaps))
    ledger.store_current(ctx, binance["id"], {bn["BTC"].key: D("0.041"), bn["ETH"].key: D("0.95"),
                                              bn["BNB"].key: D("2.1"), bn["EUR"].key: D("180")}, now())
    # Found automatically at each sync (Staking page → Staked now): part of the balances above.
    ledger.store_staking(ctx, binance["id"], [
        StakePosition(bn["ETH"].key, D("0.5"), protocol="Binance Simple Earn", validator="Flexible"),
        StakePosition(bn["BNB"].key, D("1.0"), protocol="Binance Simple Earn", validator="Locked")], now())

    cal = ctx.calendar
    for year, holdings in ((2022, {"DOT": "100"}), (2023, {"DOT": "100", "ADA": "1500"}),
                           (2024, {"DOT": "104", "ADA": "1500"}), (2025, {"DOT": "108", "ADA": "1200"})):
        ledger.store_sync_result(ctx, man, SyncResult(snapshots=[
            Snapshot(cal.cutoff(year), manual[t].key, D(q), "manual") for t, q in holdings.items()]))
    ledger.store_current(ctx, man["id"], {manual["DOT"].key: D("110"), manual["ADA"].key: D("1200")}, now(),
                         snapshot_source=None)

    # Staking position with rewards typed by hand (DOT 104 -> 108 during 2025).
    ctx.db.execute("UPDATE wallets SET staking=1 WHERE id=?", (man["id"],))
    for day in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-15"):
        ts = _t(day + "T10:00:00")
        ctx.db.execute(
            "INSERT INTO movements(wallet_id, uid, ts, asset_key, amount, kind, category, category_source, note)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (man["id"], f"manual-reward:{ts}:DOT:demo", ts, manual["DOT"].key, "1", "in",
                                           "reward", "user", "quarterly staking reward"))
    classify.run(ctx)
    ctx.db.execute("UPDATE movements SET category='reward', category_source='user' WHERE uid='t3:in'")
    ctx.db.execute("UPDATE movements SET category='payment', category_source='user' WHERE uid='t4:out'")
    prices = PriceService(ctx)
    yearend.compute_all(ctx, prices)
    from .sync import refresh_prices, value_movements, write_exports
    refresh_prices(ctx, prices, lambda m: None)
    value_movements(ctx, prices, lambda m: None)
    write_exports(ctx, lambda m: None)
