"""Offline tests: python -m pytest tests"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["LLM_PROVIDER"] = "none"
os.environ["VISION_PROVIDER"] = "none"

from bot.cogs.giveaways import parse_request
from bot.cogs.levels import level_from_xp, total_xp_for
from bot.cogs.market import levels, parse_calendar, parse_chart, regime
from bot.cogs.moderation import is_scam
from bot.cogs.trades import parse_journal, parse_json_trades, parse_text
from bot.config import MARKET_TZ, Config
from bot.futures import DEFAULT_SYMBOLS, Display, compute, resolve_root, session_label
from bot.graphics import entry_card, result_card

SPECS = {s.root: s for s in DEFAULT_SYMBOLS}


def test_symbols_and_sessions():
    roots = list(SPECS)
    assert [resolve_root(x, roots) for x in ["MNQZ6", "/NQ", "NQ1!", "ES DEC26", "mcl", "CLX25", "MESZ2025", "AAPL"]] == \
        ["MNQ", "NQ", "NQ", "ES", "MCL", "CL", "MES", None]
    at = lambda h, m=0: datetime(2026, 10, 7, h, m, tzinfo=MARKET_TZ)  # noqa: E731
    assert [session_label(at(10)), session_label(at(4)), session_label(at(20)), session_label(at(17, 30))] == ["NY", "London", "Asia", "After hours"]


def test_futures_math():
    t = compute({"side": "long", "entry": 21000, "exit": 21050, "stop": 20980, "target": 21060, "contracts": 2}, SPECS["NQ"])
    assert (t["points"], t["ticks"], t["pnl_usd"], t["r_multiple"], t["risk_usd"], t["rr_planned"]) == (50, 200, 2000, 2.5, 800, 3.0)
    t = compute({"side": "short", "entry": 71.42, "exit": 71.80, "stop": 71.70, "contracts": 3}, SPECS["MCL"])
    assert round(t["pnl_usd"], 2) == -114 and round(t["ticks"]) == -38
    t = compute({"side": "long", "entry": 5800, "exit": 5810, "contracts": 1, "pnl_usd": 999, "overrides": ["pnl_usd"]}, SPECS["MES"])
    assert t["pnl_usd"] == 999 and t["ticks"] == 40  # owner override kept, rest recomputed


def test_trade_text_parser():
    assert parse_text("long NQ 21012.25 sl 20992 tp 21072 x2 ORB retest") == {
        "side": "long", "contract": "NQ", "entry": 21012.25, "contracts": 2, "stop": 20992.0, "target": 21072.0, "notes": "ORB retest"}
    p = parse_text("short 3x MCLZ6 @ 71.42 exit 71.80 sl 71.70")
    assert (p["contracts"], p["exit"], p["stop"]) == (3, 71.8, 71.7)
    assert parse_text("close #12 at 21,062.5 trailed") == {"close_of": 12, "exit": 21062.5, "notes": "trailed"}
    assert parse_text("buy /ES 5800 2 contracts")["contracts"] == 2
    assert parse_text("gm everyone") is None


def test_journal_import():
    tradovate = (b"symbol,_priceFormat,_priceFormatType,_tickSize,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp,duration\n"
                 b"MNQZ6,-2,0,0.25,1,2,2,21000.25,21010.25,$40.00,10/07/2026 09:41:02,10/07/2026 09:45:10,4min\n"
                 b"MNQZ6,-2,0,0.25,3,4,1,21020.00,21030.00,$-10.00,10/07/2026 10:05:00,10/07/2026 10:01:00,4min\n")
    a, b = parse_journal(tradovate)
    assert (a["side"], a["entry"], a["exit"], a["contracts"], a["pnl_usd_reported"]) == ("long", 21000.25, 21010.25, 2, 40.0)
    assert (b["side"], b["entry"], b["exit"]) == ("short", 21030.0, 21020.0)
    ninja = b"Instrument,Market pos.,Qty,Entry price,Exit price,Entry time,Exit time,Profit\nES 12-26,Short,1,5800.25,5790.25,10/7/2026 9:35:00,10/7/2026 9:50:00,$500.00\n"
    (n,) = parse_journal(ninja)
    assert (n["contract"], n["side"], n["entry"], n["exit"]) == ("ES 12-26", "short", 5800.25, 5790.25)
    assert resolve_root(n["contract"], list(SPECS)) == "ES"


def test_scam_filter():
    for bad in ["FREE NITRO here", "dm me for signals", "guaranteed profit daily!!", "connect your wallet to claim", "https://dlscord.gift/abc"]:
        assert is_scam(bad), bad
    for ok in ["took profit on nq", "my stop got hit lol", "anyone trading CPI?"]:
        assert not is_scam(ok), ok


def test_levels_xp():
    assert level_from_xp(0) == 0 and level_from_xp(100) == 1 and level_from_xp(total_xp_for(10)) == 10


def test_cards_render():
    t = compute({"symbol": "NQ", "contract": "NQZ6", "side": "long", "entry": 21012.25, "exit": 21062.5, "stop": 20992.25,
                 "contracts": 2, "session": "NY"}, SPECS["NQ"])
    assert len(result_card(t, brand="Test", brand_color=0x00C2A8)) > 10000
    assert len(entry_card(t, brand="Test", brand_color=0x00C2A8)) > 10000


def test_market_parsers():
    cal = parse_calendar([
        {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
        {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-10-14T10:30:00-04:00", "impact": "Medium"},
        {"title": "German ZEW", "country": "EUR", "date": "2026-10-14T05:00:00-04:00", "impact": "High"},
        {"title": "Mortgage Delinquencies", "country": "USD", "date": "2026-10-14T09:00:00-04:00", "impact": "Low"},
    ])
    assert [e["title"] for e in cal] == ["CPI m/m", "Crude Oil Inventories"]
    base = datetime(2026, 6, 1, tzinfo=MARKET_TZ).timestamp()
    n = 60
    closes = [20000 + i * 20 for i in range(n)]
    data = {"chart": {"result": [{"timestamp": [int(base + i * 86400) for i in range(n)], "indicators": {"quote": [{
        "open": closes, "high": [c + 50 for c in closes], "low": [c - 50 for c in closes], "close": closes}]}}]}}
    bars = parse_chart(data)
    assert len(bars) == n and regime(bars)["label"].startswith("Trending up")
    lv = levels(bars, [], bars[-1]["t"] + timedelta(days=1))
    assert lv["pdc"] == closes[-1]


def test_giveaway_request_parser():
    sp = [{"name": "Lucid", "sizes": ["25K", "50K", "150K"]}]
    assert parse_request("create a giveaway for two Lucid accounts", sp) == {"sponsor": "Lucid", "quantity": 2}
    assert parse_request("3 lucid 50k accounts for 7 days", sp) == {"sponsor": "Lucid", "size": "50K", "quantity": 3, "duration_hours": 168}


def test_rebuild_plan_adopts_and_archives():
    from bot.cogs.rebuild import compute_plan, plan_text

    class Ch(SimpleNamespace):
        topic = None
        overwrites = {}

    general = Ch(id=1, name="general", category=None, category_id=None, position=0)
    signals = Ch(id=2, name="signals", category=None, category_id=None, position=1)
    random_ = Ch(id=3, name="random-stuff", category=None, category_id=None, position=2)
    guild = SimpleNamespace(name="Test", roles=[], categories=[], text_channels=[general, signals, random_], channels=[general, signals, random_],
                            rules_channel=None, public_updates_channel=None, system_channel=None)
    plan = compute_plan(guild)
    s2 = plan["stages"]["2"]
    adopted = {o["key"]: o["channel_id"] for o in s2 if o["op"] == "adopt_channel"}
    assert adopted == {"general": 1, "trade_entries": 2}
    assert [o["channel_id"] for o in plan["stages"]["4"] if o["op"] == "archive"] == [3]
    assert not any(o["op"].startswith("delete") for ops in plan["stages"].values() for o in ops)
    assert "Stage 5" in plan_text(guild, plan)
    assert plan["stages"]["7"] == [{"op": "polish_pending"}]


def test_polish_stage_orders_roles_unlocks_members_and_adds_headers():
    from bot.cogs.rebuild import HAS_CONTENT, Rebuild, describe_op
    from bot.util import CHANNELS, FOUNDER_ROLE, VERIFIED_ROLE

    class Role(SimpleNamespace):
        def __eq__(self, other):
            return getattr(other, "id", None) == self.id
        __hash__ = object.__hash__

    trader = Role(id=5, name=VERIFIED_ROLE, color=SimpleNamespace(value=0x95A5A6), hoist=False)
    members = [SimpleNamespace(id=10, bot=False, roles=[]), SimpleNamespace(id=11, bot=False, roles=[trader]), SimpleNamespace(id=12, bot=True, roles=[])]
    guild = SimpleNamespace(id=1, name="T", roles=[trader], chunked=True, members=members, owner_id=10)
    bot = SimpleNamespace(config=SimpleNamespace(owner_id=10), db=SimpleNamespace(get_setting=lambda gid, k, d=None: "1" if k == "header:general" else d))
    ops = asyncio.run(Rebuild.polish_ops(SimpleNamespace(bot=bot), guild))
    kinds = [o["op"] for o in ops]
    assert {"op": "create_role", "name": FOUNDER_ROLE} .items() <= next(o for o in ops if o.get("name") == FOUNDER_ROLE).items()
    assert any(o["op"] == "style_role" and o["role_id"] == 5 for o in ops)  # Trader gets its brighter color
    assert "role_order" in kinds
    gives = {o["role"]: o["user_ids"] for o in ops if o["op"] == "give_role"}
    assert gives == {FOUNDER_ROLE: [10], VERIFIED_ROLE: [10]}  # bots and members who already have it are skipped
    headers = {o["key"] for o in ops if o["op"] == "post_header"}
    assert headers == set(CHANNELS) - HAS_CONTENT - {"general"}
    assert all(describe_op(guild, o) for o in ops)


def test_json_trades():
    got = parse_json_trades('```json\n{"symbol": "MNQZ6", "side": "Buy", "entry": "21,012.25", "exit": 21052.25, "qty": 2, '
                            '"opened_at": "2026-10-07 09:45:00", "show_dollars": false}\n```')
    assert got[0]["contract"] == "MNQZ6" and got[0]["side"] == "long" and got[0]["entry"] == 21012.25 and got[0]["contracts"] == 2
    assert got[0]["show_usd"] is False and got[0]["entry_time"].startswith("2026-10-07T09:45")
    assert parse_json_trades('{"trades": [{"close_of": 12, "exit": 100.5}, {"junk": 1}]}')[0]["close_of"] == 12
    assert parse_json_trades("long NQ 21000") is None


def test_posts_hide_dollars_by_default():
    t = compute({"contract": "MNQZ6", "symbol": "MNQ", "side": "long", "entry": 21012.25, "stop": 20992.25, "target": 21072.25,
                 "exit": 21052.25, "contracts": 3}, SPECS["MNQ"])
    assert t["risk_points"] == 20 and t["risk_ticks"] == 80 and t["reward_points"] == 60
    assert Display().account_pct(t) is None and Display(account=48000).account_pct(t) == 0.5
    # cards render in every display mode
    for show in (Display(), Display(usd=True, size=True, account=48000)):
        assert len(result_card(t, brand="T", brand_color=0x00C2A8, show=show)) > 10000
        assert len(entry_card(t, brand="T", brand_color=0x00C2A8, show=show)) > 10000


def test_cleanup_stage_deletes_only_unused_archived_channels():
    import discord
    from bot.cogs.rebuild import ARCHIVE, Rebuild, describe_op

    now = discord.utils.utcnow()

    class Hist(discord.TextChannel):
        def __init__(self, id, name, last):
            self.id, self.name, self._last = id, name, last

        def history(self, limit=1, **kw):
            async def gen():
                if self._last:
                    yield SimpleNamespace(created_at=self._last)
            return gen()

        def __hash__(self):
            return self.id

    old, fresh, empty = Hist(1, "old-signals", now - timedelta(days=200)), Hist(2, "vip", now - timedelta(days=3)), Hist(3, "unused", None)
    archive = SimpleNamespace(name=ARCHIVE, channels=[old, fresh, empty])
    stale_cat = SimpleNamespace(id=9, name="OLD STUFF", channels=[])
    guild = SimpleNamespace(categories=[archive, stale_cat])
    ops = asyncio.run(Rebuild.cleanup_ops(SimpleNamespace(), guild))
    by = {o.get("name"): o["op"] for o in ops}
    assert by == {"old-signals": "delete_channel", "vip": "keep_archived", "unused": "delete_channel", "OLD STUFF": "delete_category", None: "tidy_order"}
    assert all(describe_op(guild, o) for o in ops)


def test_bot_loads_and_commands_fit_discord_limits():
    from bot.control import rules_allow
    from bot.core import TradingBot
    os.environ["DB_PATH"] = ":memory:"
    cfg = Config.from_env(require_token=False)

    async def run():
        bot = TradingBot(cfg)
        await bot._async_setup_hook()
        await bot.init()
        cmds = bot.tree.get_commands()
        names = {c.name for c in cmds}
        assert len(cmds) <= 100, len(cmds)
        for c in cmds:
            if hasattr(c, "commands"):
                assert len(c.commands) <= 25, c.name
        for need in ["trade", "close", "submit", "import-journal", "edit", "edit-history", "teach", "feature", "perm", "identity",
                     "pause", "resume", "kill", "giveaway", "sponsor", "rebuild", "market", "share-trade", "challenge", "experiments",
                     "social", "invites", "report", "rank", "leaderboard", "symbols", "display", "journal-link", "export-trades", "logs", "health"]:
            assert need in names, need
        # rulebook gate works offline: "never post during FOMC" blocks when FOMC is near
        g = SimpleNamespace(id=42)
        bot.rulebook[42] = [{"id": 1, "text": "Never post during FOMC"}]
        market = bot.get_cog("Market")
        market.events = [{"title": "FOMC Statement", "time": datetime.now(MARKET_TZ) + timedelta(minutes=10), "id": "x"}]
        ok, rule = await rules_allow(bot, g, "Post a poll in #general")
        assert not ok and "FOMC" in rule
        market.events = []
        assert (await rules_allow(bot, g, "Post a poll in #general"))[0]
        await bot.seed_guild(42)
        assert len(await bot.get_cog("Trades").specs(42)) == 6
        # dollars and size are hidden unless turned on, server-wide or per trade
        trades = bot.get_cog("Trades")
        assert trades.display(42) == Display(usd=False, size=False, account=None)
        assert trades.display(42, {"show_usd": True}).usd
        await bot.db.set_setting(42, "display:show_usd", "on")
        await bot.db.set_setting(42, "display:account_size", 50000)
        assert trades.display(42).usd and trades.display(42).account == 50000
        assert not trades.display(42, {"show_usd": False}).usd
        from bot.safety import HANDLERS
        assert {"rebuild_all", "rebuild_stage"} <= set(HANDLERS) and cfg.auto_rebuild
        # health: first start is reported as "first", an unclean stop as a crash, a clean stop as clean
        health = bot.get_cog("Health")
        assert (await health._record_start())[0] == "first"
        assert (await health._record_start())[0] == "crash"
        await health.mark_clean_exit()
        assert (await health._record_start())[0] == "clean"
        assert (await health.status_embed()).title.endswith("is online")
        await bot.close()
        return len(cmds)

    print("top-level commands:", asyncio.run(run()))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
