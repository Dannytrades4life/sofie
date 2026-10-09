"""Owner trade posts for futures: entries, exits, results.

Ways in (owner only):
  • /trade, /close                    typed values, posted straight away
  • text in #trade-submit             "long NQ 21012.25 sl 20992 tp 21072 x2"   /  "close 12 at 21062.5"
  • a screenshot in #trade-submit     read by a free vision model
  • /import-journal (CSV)             Tradovate, NinjaTrader or any CSV with entry/exit columns
  • JSON (file or message)            exact fields, no guessing; see JOURNAL_FORMAT
  • your journal app via webhook      /journal-link gives it a URL; it posts text, JSON, CSV or images
Everything that was read or guessed becomes a draft with a confirm card (Post / Edit / Cancel).
The bot only posts numbers the owner gave or confirmed. Losses are posted like wins.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from datetime import datetime

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from ..config import MARKET_TZ
from ..control import feature_on, owner_only, persona, publish
from ..futures import SESSION_EMOJI, Display, Spec, compute, fmt_dist, fmt_pct, fmt_price, fmt_pts, fmt_usd, outcome, resolve_root, session_label
from ..graphics import entry_card, result_card
from ..safety import log_action
from ..util import ALERT_ROLE, DISCLAIMER, GREEN, GREY, ORANGE, RED, brand_color, find_role, get_channel

log = logging.getLogger(__name__)

SIDES = [app_commands.Choice(name="Long", value="long"), app_commands.Choice(name="Short", value="short")]
NUM = r"(\d[\d,]*\.?\d*)"

ENTRY_RE = re.compile(rf"^(?P<side>long|short|buy|sell|bought|sold)\s+(?P<qty1>\d+\s*x\s+)?(?P<symbol>[/A-Za-z0-9!]+)\s+(?:@\s*|at\s+)?{NUM}(?P<rest>.*)$", re.I | re.S)
CLOSE_RE = re.compile(rf"^(?:close|exit|out)\s+#?(?P<id>\d+)\s+(?:@\s*|at\s+)?{NUM}(?P<rest>.*)$", re.I | re.S)

VISION_PROMPT = (
    "This is a screenshot from a futures trading platform (e.g. Tradovate, NinjaTrader, TradingView, Rithmic). "
    "Extract every completed or open trade you can clearly see. For each, give: symbol (as shown, e.g. NQZ6 or MNQ DEC26), "
    "side (long or short), entry (price), exit (price or null if still open), stop (or null), target (or null), "
    "contracts (number), pnl_usd (the dollar P&L shown, or null), entry_time and exit_time (ISO 8601 if visible, else null). "
    "Do not guess numbers you can't read; use null. "
    'Format: {"trades": [{...}], "notes": "anything unclear"}'
)


MONEY_RE = re.compile(r"(?:[£$€]\s?\d[\d,]*(?:\.\d+)?\s?[kK]?|\b\d[\d,]*(?:\.\d+)?\s?[kK]?\s?(?:usd|gbp|eur|dollars?|bucks|pounds?|quid)\b)", re.I)
TV_SNAPSHOT_RE = re.compile(r"https?://(?:www\.)?tradingview\.com/x/([A-Za-z0-9]+)/?")
IMAGE_URL_RE = re.compile(r"https?://\S+\.(?:png|jpe?g|webp)(?:\?\S*)?$", re.I)
JOURNAL_DIR = "data/journal"


def strip_money(text: str | None) -> str:
    """Remove currency amounts so private P&L never reaches a caption or post."""
    return re.sub(r"\s{2,}", " ", MONEY_RE.sub("[amount]", text or "")).strip()


def parse_jn(raw: str | bytes | None) -> dict | None:
    """A trade sent by the owner's JN journal ("Send to Sofie"): JSON with source == "jn"."""
    if not raw:
        return None
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("source") == "jn" else None


def _num(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def parse_text(text: str) -> dict | None:
    """Parse the owner's shorthand. Returns a draft dict or None."""
    text = text.strip()
    if m := CLOSE_RE.match(text):
        return {"close_of": int(m["id"]), "exit": _num(m.group(2)), "notes": m["rest"].strip() or None}
    m = ENTRY_RE.match(text)
    if not m:
        return None
    rest = " " + m["rest"] + " "
    out = {
        "side": "long" if m["side"].lower() in ("long", "buy", "bought") else "short",
        "contract": m["symbol"].upper().lstrip("/"),
        "entry": _num(m.group(4)),
        "contracts": int(re.sub(r"\D", "", m["qty1"])) if m["qty1"] else None,
    }
    patterns = {
        "stop": rf"\b(?:sl|stop)\s*:?\s*{NUM}",
        "target": rf"\b(?:tp|target|pt)\s*:?\s*{NUM}",
        "exit": rf"(?:\b(?:exit|out|closed?)\s*(?:@|at)?\s*|->\s*){NUM}",
        "contracts": r"(?:\bx\s*(\d+)\b|\b(\d+)\s*x\b|\b(?:qty|size)\s*:?\s*(\d+)\b|\b(\d+)\s*(?:contracts?|cts?|lots?)\b)",
    }
    for key, pat in patterns.items():
        mm = re.search(pat, rest, re.I)
        if mm:
            val = next(g for g in mm.groups() if g)
            out[key] = int(val) if key == "contracts" else _num(val)
            rest = rest.replace(mm.group(0), " ")
    out["notes"] = re.sub(r"\s+", " ", rest).strip() or None
    return out


# ------------------------------------------------------------------ journal CSV
ALIASES = {
    "contract": ["symbol", "instrument", "contract", "ticker", "market"],
    "side": ["side", "direction", "marketpos", "position", "type", "action"],
    "contracts": ["qty", "quantity", "contracts", "size", "lots"],
    "entry": ["entry", "entryprice", "buyprice", "open", "openprice", "avgentry", "avgentryprice"],
    "exit": ["exit", "exitprice", "sellprice", "close", "closeprice", "avgexit", "avgexitprice"],
    "pnl_usd": ["pnl", "profit", "netpnl", "netprofit", "realizedpnl", "gain"],
    "entry_time": ["entrytime", "boughttimestamp", "opentime", "opened", "entrydate"],
    "exit_time": ["exittime", "soldtimestamp", "closetime", "closed", "exitdate"],
}


JSON_FIELDS = {
    "contract": ["contract", "symbol", "instrument", "ticker"], "side": ["side", "direction", "action"],
    "entry": ["entry", "entry_price", "entryprice", "avg_entry"], "exit": ["exit", "exit_price", "exitprice", "avg_exit"],
    "stop": ["stop", "stop_loss", "sl"], "target": ["target", "take_profit", "tp"], "contracts": ["contracts", "qty", "quantity", "size"],
    "entry_time": ["entry_time", "opened_at", "open_time"], "exit_time": ["exit_time", "closed_at", "close_time"],
    "notes": ["notes", "note", "comment", "reason", "setup"], "pnl_usd_reported": ["pnl", "pnl_usd", "profit", "net_pnl"],
    "show_usd": ["show_dollars", "show_usd"], "close_of": ["close_of", "trade_id", "closes"],
}


def parse_json_trades(raw: str | bytes) -> list[dict] | None:
    """Exact-field trades from JSON: one object, a list, or {"trades": [...]}. None if it isn't JSON."""
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    if not text.startswith(("{", "[")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    items = data.get("trades", [data]) if isinstance(data, dict) else data
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        low = {str(k).lower().replace(" ", "_"): v for k, v in item.items()}
        d = {}
        for field, names in JSON_FIELDS.items():
            val = next((low[n] for n in names if low.get(n) not in (None, "")), None)
            if val is None:
                continue
            if field in ("entry", "exit", "stop", "target", "pnl_usd_reported"):
                val = _num(val)
            elif field in ("contracts", "close_of"):
                val = int(abs(_num(val) or 0)) or None
            elif field == "side":
                val = "short" if any(w in str(val).lower() for w in ("short", "sell", "sold")) else "long"
            elif field == "show_usd":
                val = str(val).lower() in ("1", "true", "yes", "on")
            elif field in ("entry_time", "exit_time"):
                val = _iso(str(val))
            else:
                val = str(val).strip()
            if val is not None:
                d[field] = val
        if d.get("close_of") and d.get("exit") is not None:
            out.append({"close_of": d["close_of"], "exit": d["exit"], "notes": d.get("notes"), "source": "json"})
        elif d.get("contract") and d.get("entry") is not None:
            if "show_usd" in d:
                d["show_size"] = d["show_usd"]
            out.append({**d, "source": "json"})
    return out


def parse_journal(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    norm = {h: re.sub(r"[^a-z]", "", (h or "").lower()) for h in (reader.fieldnames or [])}
    cols = {}
    for field, names in ALIASES.items():
        for h, n in norm.items():
            if n in names:
                cols[field] = h
                break
    tradovate = "buyprice" in norm.values() and "sellprice" in norm.values()
    out = []
    for row in reader:
        get = lambda f: row.get(cols[f]) if f in cols else None  # noqa: E731
        d = {
            "contract": (get("contract") or "").strip().upper(),
            "contracts": int(abs(_num(get("contracts")) or 1)),
            "pnl_usd_reported": _num(get("pnl_usd")),
            "entry_time": get("entry_time"), "exit_time": get("exit_time"), "source": "journal",
        }
        if tradovate:
            # Tradovate performance export: buy and sell legs; whichever came first is the entry.
            bought, sold = get("entry_time") or "", get("exit_time") or ""
            buy, sell = _num(row.get(next(h for h, n in norm.items() if n == "buyprice"))), _num(row.get(next(h for h, n in norm.items() if n == "sellprice")))
            long_first = _parse_dt(bought) <= _parse_dt(sold) if bought and sold else True
            d.update(side="long" if long_first else "short", entry=buy if long_first else sell, exit=sell if long_first else buy,
                     entry_time=bought if long_first else sold, exit_time=sold if long_first else bought)
        else:
            side = (get("side") or "long").lower()
            d.update(side="short" if any(w in side for w in ("short", "sell", "sold")) else "long",
                     entry=_num(get("entry")), exit=_num(get("exit")))
        if d["contract"] and d.get("entry") is not None:
            out.append(d)
    return out


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.min
    for fmt in (None, "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return datetime.min


def _iso(s) -> str | None:
    dt = _parse_dt(s) if isinstance(s, str) else s
    if not dt or dt == datetime.min:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ)
    return dt.isoformat()


# ------------------------------------------------------------------ confirm card buttons
class DraftButton(discord.ui.DynamicItem[discord.ui.Button], template=r"draft:(?P<act>post|edit|cancel):(?P<id>\d+)"):
    LABELS = {"post": ("Post it", discord.ButtonStyle.success), "edit": ("Edit", discord.ButtonStyle.secondary),
              "cancel": ("Cancel", discord.ButtonStyle.danger)}

    def __init__(self, act: str, draft_id: int):
        label, style = self.LABELS[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"draft:{act}:{draft_id}"))
        self.act, self.draft_id = act, draft_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        if interaction.user.id != bot.config.owner_id:
            await interaction.response.send_message("Only the owner can confirm trades.", ephemeral=True)
            return
        cog: Trades = bot.get_cog("Trades")
        row = await bot.db.fetchone("SELECT * FROM drafts WHERE id = ?", self.draft_id)
        if row is None or row["status"] != "pending":
            await interaction.response.send_message("This draft was already handled.", ephemeral=True)
            return
        data = json.loads(row["data"])
        if self.act == "cancel":
            await bot.db.execute("UPDATE drafts SET status = 'cancelled' WHERE id = ?", self.draft_id)
            await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        elif self.act == "edit":
            await interaction.response.send_modal(DraftModal(self.draft_id, data))
        else:
            await interaction.response.defer()
            try:
                msg = await cog.publish_draft(interaction.guild, self.draft_id, data, interaction.user.id)
            except ValueError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            await interaction.edit_original_response(content=f"✅ Posted: {msg.jump_url}", embed=None, view=None)


class DraftModal(discord.ui.Modal):
    def __init__(self, draft_id: int, d: dict):
        super().__init__(title=f"Edit draft #{draft_id}")
        self.draft_id = draft_id
        self.sym = discord.ui.TextInput(label="Symbol and side", default=f"{d.get('contract') or d.get('symbol') or ''} {d.get('side') or ''}".strip(), max_length=40)
        self.prices = discord.ui.TextInput(label="Entry / Exit (leave exit blank if open)", default=f"{_s(d.get('entry'))} / {_s(d.get('exit'))}".strip(" /"), required=False, max_length=60)
        self.risk = discord.ui.TextInput(label="Stop / Target", default=f"{_s(d.get('stop'))} / {_s(d.get('target'))}".strip(" /"), required=False, max_length=60)
        self.qty = discord.ui.TextInput(label="Contracts", default=_s(d.get("contracts") or 1), max_length=6)
        self.notes = discord.ui.TextInput(label="Notes / caption idea", style=discord.TextStyle.paragraph, default=d.get("notes") or "", required=False, max_length=500)
        for item in (self.sym, self.prices, self.risk, self.qty, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        row = await bot.db.fetchone("SELECT data FROM drafts WHERE id = ?", self.draft_id)
        d = json.loads(row["data"])
        parts = self.sym.value.split()
        if parts:
            d["contract"] = parts[0].upper().lstrip("/")
            d.pop("symbol", None)
        if len(parts) > 1 and parts[1].lower() in ("long", "short"):
            d["side"] = parts[1].lower()
        a, _, b = self.prices.value.partition("/")
        d["entry"], d["exit"] = _num(a), _num(b)
        a, _, b = self.risk.value.partition("/")
        d["stop"], d["target"] = _num(a), _num(b)
        d["contracts"] = int(_num(self.qty.value) or 1)
        d["notes"] = self.notes.value or None
        cog: Trades = bot.get_cog("Trades")
        d = await cog.normalize(interaction.guild.id, d)
        await bot.db.execute("UPDATE drafts SET data = ? WHERE id = ?", json.dumps(d), self.draft_id)
        await interaction.response.edit_message(embed=await cog.draft_embed(interaction.guild, self.draft_id, d))


def _s(x) -> str:
    return "" if x is None else (f"{x:g}" if isinstance(x, float) else str(x))


# ------------------------------------------------------------------ cog
class Trades(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._specs: dict[int, dict[str, Spec]] = {}

    async def specs(self, guild_id: int) -> dict[str, Spec]:
        if guild_id not in self._specs:
            rows = await self.bot.db.fetchall("SELECT * FROM symbols WHERE guild_id = ?", guild_id)
            self._specs[guild_id] = {r["root"]: Spec(r["root"], r["name"], r["tick_size"], r["tick_value"], r["data_ticker"], bool(r["market_posts"])) for r in rows}
        return self._specs[guild_id]

    def invalidate_specs(self, guild_id: int) -> None:
        self._specs.pop(guild_id, None)

    def display(self, guild_id: int, d: dict | None = None) -> Display:
        """Public posts hide dollars and contract count unless the owner turned them on
        (server-wide with /display, or for one trade with show_dollars)."""
        get = self.bot.db.get_setting
        usd = (d or {}).get("show_usd")
        if usd is None:
            usd = get(guild_id, "display:show_usd") == "on"
        size = (d or {}).get("show_size")
        if size is None:
            size = get(guild_id, "display:show_size") == "on"
        try:
            account = float(get(guild_id, "display:account_size") or 0) or None
        except ValueError:
            account = None
        return Display(usd=bool(usd), size=bool(size), account=account)

    async def normalize(self, guild_id: int, d: dict) -> dict:
        """Resolve the root symbol, session label and derived numbers."""
        specs = await self.specs(guild_id)
        raw = d.get("contract") or d.get("symbol") or ""
        root = resolve_root(raw, list(specs))
        d["symbol"] = root or raw.upper()
        d["contract"] = raw.upper() or root
        if d.get("entry_time") and not d.get("session"):
            try:
                d["session"] = session_label(datetime.fromisoformat(d["entry_time"]))
            except ValueError:
                pass
        d.setdefault("session", session_label(datetime.now(MARKET_TZ)))
        d.setdefault("entry_time", datetime.now(MARKET_TZ).isoformat())
        if d.get("exit") is not None:
            d.setdefault("exit_time", datetime.now(MARKET_TZ).isoformat())
        d["contracts"] = d.get("contracts") or 1
        return compute(d, specs.get(root)) if root else d

    # ---- drafts ----
    async def create_draft(self, guild: discord.Guild, d: dict, author_id: int, channel: discord.abc.Messageable) -> None:
        if d.get("close_of"):
            trade = await self.bot.db.get_trade(guild.id, d["close_of"])
            if trade is None:
                await channel.send(f"I can't find trade #{d['close_of']}. `/open-trades` lists the open ones.")
                return
            merged = {**trade["data"], "exit": d["exit"], "exit_time": datetime.now(MARKET_TZ).isoformat(), "close_of": d["close_of"]}
            if d.get("notes"):
                merged["notes"] = d["notes"]
            d = merged
        d = await self.normalize(guild.id, d)
        kind = "close" if d.get("close_of") else ("result" if d.get("exit") is not None else "entry")
        draft_id = await self.bot.db.execute(
            "INSERT INTO drafts (guild_id, kind, data, created_by, created_at) VALUES (?,?,?,?,?)",
            guild.id, kind, json.dumps(d), author_id, time.time())
        view = discord.ui.View(timeout=None)
        for act in ("post", "edit", "cancel"):
            view.add_item(DraftButton(act, draft_id))
        await channel.send(embed=await self.draft_embed(guild, draft_id, d), view=view)

    async def draft_embed(self, guild: discord.Guild, draft_id: int, d: dict) -> discord.Embed:
        if d.get("source") == "jn" and d.get("entry") is None:
            e = discord.Embed(title=f"Check before posting · draft #{draft_id} (journal)", color=ORANGE,
                              description=f"**{self.journal_title(d)}**\nI couldn't read prices from the chart, so this posts as a "
                                          "journal recap (no prices). Tap **Edit** to add symbol and prices for a full trade card.")
            e.add_field(name="Setup", value=(d.get("setup") or "—")[:200], inline=False)
            e.add_field(name="Session", value=d.get("journal_session") or "—")
            e.add_field(name="Discipline", value=f"{d['discipline']:.0f}%" if d.get("discipline") is not None else "—")
            if d.get("mistake"):
                e.add_field(name="Mistake", value=d["mistake"])
            if d.get("notes"):
                e.add_field(name="Notes (used for the caption only)", value=d["notes"][:1000], inline=False)
            if d.get("link"):
                e.add_field(name="Chart link", value=d["link"][:300], inline=False)
            e.set_footer(text="P&L from your journal is never sent or posted.")
            return e
        specs = await self.specs(guild.id)
        kind = "close" if d.get("close_of") else ("result" if d.get("exit") is not None else "entry")
        e = discord.Embed(title=f"Check before posting · draft #{draft_id} ({kind})", color=ORANGE)
        e.add_field(name="Symbol", value=f"{d.get('contract')} → {d.get('symbol')}")
        e.add_field(name="Side", value=str(d.get("side")))
        show = self.display(guild.id, d)
        e.add_field(name="Contracts", value=f"{d.get('contracts')}" + ("" if show.size else " (not shown on the post)"))
        e.add_field(name="Entry / Exit", value=f"{fmt_price(d.get('entry'))} / {fmt_price(d.get('exit'))}")
        e.add_field(name="Stop / Target", value=f"{fmt_price(d.get('stop'))} / {fmt_price(d.get('target'))}")
        e.add_field(name="Session", value=str(d.get("session")))
        if d.get("exit") is not None:
            parts = [fmt_pts(d.get("points"), d.get("ticks")), fmt_pct(d.get("pnl_pct")) + " move",
                     "%+.2fR" % d["r_multiple"] if d.get("r_multiple") is not None else "no stop → no R"]
            if show.account_pct(d) is not None:
                parts.append(f"{fmt_pct(show.account_pct(d))} account")
            if show.usd:
                parts.append(fmt_usd(d.get("pnl_usd")))
            e.add_field(name="Result", value=" · ".join(parts), inline=False)
        warnings = []
        if d.get("symbol") not in specs:
            warnings.append(f"I don't know **{d.get('symbol')}**. Add it with `/symbols add` so I can do ticks and R.")
        if d.get("entry") is None:
            warnings.append("No entry price.")
        if d.get("side") not in ("long", "short"):
            warnings.append("Side should be long or short.")
        rep = d.get("pnl_usd_reported")
        if rep is not None and d.get("pnl_usd") is not None and abs(rep - d["pnl_usd"]) > max(5, abs(rep) * 0.05):
            warnings.append("Your platform's P&L doesn't match my math for this contract count. Fees, or a wrong contract count?")
        if d.get("close_of"):
            e.description = f"Closes trade #{d['close_of']}."
        if warnings:
            e.add_field(name="⚠️ Check", value="\n".join(warnings), inline=False)
        if d.get("notes"):
            e.add_field(name="Notes", value=d["notes"][:1000], inline=False)
        e.set_footer(text="Dollar amounts are never posted unless you turn them on (/display or show_dollars on /trade).")
        return e

    async def publish_draft(self, guild: discord.Guild, draft_id: int, d: dict, user_id: int) -> discord.Message:
        if d.get("source") == "jn" and d.get("entry") is None:
            msg = await self.post_journal(guild, d)
            await self.bot.db.execute("UPDATE drafts SET status = 'posted' WHERE id = ?", draft_id)
            return msg
        if d.get("entry") is None or d.get("side") not in ("long", "short"):
            raise ValueError("Fix the draft first (entry price and side are required). Tap Edit.")
        if d.get("close_of"):
            msg = await self.close_trade(guild, d["close_of"], d["exit"], notes=d.get("notes"), extra=d)
        else:
            msg = await self.open_trade(guild, d)
        await self.bot.db.execute("UPDATE drafts SET status = 'posted' WHERE id = ?", draft_id)
        return msg

    # ---- rendering (also used by /edit to regenerate posts) ----
    async def caption(self, guild: discord.Guild, d: dict, kind: str) -> str:
        show = self.display(guild.id, d)
        keys = ["contract", "side", "entry", "exit", "stop", "target", "points", "ticks", "pnl_pct", "r_multiple", "risk_points", "session", "notes",
                "setup", "discipline"]
        keys += ["pnl_usd"] if show.usd else []
        keys += ["contracts"] if show.size else []
        facts = {k: d.get(k) for k in keys}
        if show.account_pct(d) is not None:
            facts["account_pct"] = show.account_pct(d)
        money = "" if show.usd else " Never mention dollar amounts, money made or lost, or position size; talk in points, ticks, % and R."
        text = await self.bot.llm.chat(
            persona(self.bot, guild),
            f"Write a 1-2 sentence caption for this {kind} post. Use only these facts and add no numbers that aren't here. "
            f"No hype, no promises. If it's a loss, own it calmly.{money}\n{json.dumps(facts)}",
            max_tokens=120,
        )
        if text:
            return text
        res = outcome(d)
        if kind == "entry":
            return d.get("notes") or f"{d['side']} {d['contract']} here, plan's on the card. manage your own risk."
        return {"win": "plan worked, paid. 🎯", "loss": "stopped out, it happens. on to the next one.",
                "breakeven": "scratched it, no harm done."}.get(res, "closed.")

    async def _logo(self, guild: discord.Guild) -> bytes | None:
        try:
            return await guild.icon.read() if guild.icon else None
        except discord.HTTPException:
            return None

    async def render(self, guild: discord.Guild, trade_id: int, d: dict, kind: str, entry_link: str | None = None) -> tuple[discord.Embed, bytes]:
        brand = self.bot.db.get_setting(guild.id, "brand_name") or guild.name
        color = brand_color(self.bot, guild)
        sess = d.get("session")
        sess_txt = f" · {SESSION_EMOJI.get(sess, '')} {sess}" if sess else ""
        show = self.display(guild.id, d)
        if kind == "entry":
            png = entry_card(d, brand=brand, brand_color=color, logo=await self._logo(guild), show=show)
            long_ = d["side"] == "long"
            e = discord.Embed(title=d.get("title") or f"{'🟢' if long_ else '🔴'} {d['side'].upper()} {d['contract']}{sess_txt}",
                              description=d.get("caption"), color=GREEN if long_ else RED)
            e.add_field(name="Entry", value=f"`{fmt_price(d.get('entry'))}`")
            e.add_field(name="Stop", value=f"`{fmt_price(d.get('stop'))}`")
            e.add_field(name="Target", value=f"`{fmt_price(d.get('target'))}`")
            if d.get("risk_points") is not None:
                e.add_field(name="Risk", value=f"`{fmt_dist(d.get('risk_points'), d.get('risk_ticks'))}`")
            if show.size:
                e.add_field(name="Contracts", value=f"`{d.get('contracts')}`")
            if d.get("rr_planned"):
                e.add_field(name="Planned R:R", value=f"`1 : {d['rr_planned']:.2f}`")
        else:
            png = result_card(d, brand=brand, brand_color=color, logo=await self._logo(guild), show=show)
            res = outcome(d)
            icon, col = {"win": ("✅", GREEN), "loss": ("❌", RED)}.get(res, ("➖", GREY))
            headline = fmt_usd(d.get("pnl_usd")) if show.usd and d.get("pnl_usd") is not None else (
                f"{d['points']:+,.2f} pts" if d.get("points") is not None else "closed")
            e = discord.Embed(title=d.get("result_title") or f"{icon} {d['contract']} {d['side'].upper()} · {headline}{sess_txt}",
                              description=d.get("result_caption") or d.get("caption"), color=col)
            e.add_field(name="Entry → Exit", value=f"`{fmt_price(d.get('entry'))} → {fmt_price(d.get('exit'))}`")
            e.add_field(name="Points / Ticks", value=f"`{fmt_pts(d.get('points'), d.get('ticks')).replace(' pts', '').replace(' ticks', 't')}`")
            e.add_field(name="Price move", value=f"`{fmt_pct(d.get('pnl_pct'))}`")
            if d.get("r_multiple") is not None:
                e.add_field(name="R multiple", value=f"`{d['r_multiple']:+.2f}R`")
            if show.account_pct(d) is not None:
                e.add_field(name="Account", value=f"`{fmt_pct(show.account_pct(d))}`")
            if show.size:
                e.add_field(name="Contracts", value=f"`{d.get('contracts')}`")
            if entry_link:
                e.add_field(name="Original call", value=f"[jump to entry]({entry_link})")
        if d.get("source") == "jn":
            if d.get("setup"):
                e.add_field(name="Setup", value=d["setup"].split(" → ")[0][:100])
            if d.get("discipline") is not None:
                e.add_field(name="Discipline", value=f"`{d['discipline']:.0f}%`")
            if d.get("link"):
                e.add_field(name="Chart", value=f"[open chart]({d['link']})")
        e.set_footer(text=f"Trade #{trade_id} • {DISCLAIMER}")
        e.timestamp = discord.utils.utcnow()
        return e, png

    # ---- open / close ----
    async def open_trade(self, guild: discord.Guild, d: dict) -> discord.Message:
        if not feature_on(self.bot, guild.id, "trade_posts"):
            raise ValueError("Trade posts are switched off. `/feature set trade_posts on` to turn them back on.")
        closed = d.get("exit") is not None
        if closed:
            d["result_caption"] = d.get("result_caption") or await self.caption(guild, d, "result")
        else:
            d["caption"] = d.get("caption") or await self.caption(guild, d, "entry")
        now = time.time()
        trade_id = await self.bot.db.execute(
            "INSERT INTO trades (guild_id, status, data, original, created_at, closed_at) VALUES (?,?,?,?,?,?)",
            guild.id, "closed" if closed else "open", json.dumps(d), json.dumps(d), now, now if closed else None)
        if closed:
            embed, png = await self.render(guild, trade_id, d, "result")
            msg = await publish(self.bot, guild, "trade_posts", "results", embed=embed, file=(png, f"trade-{trade_id}.png"),
                                kind="trade_result", ref_id=trade_id, owner_initiated=True)
            if msg:
                await self.bot.db.execute("UPDATE trades SET result_channel_id=?, result_message_id=? WHERE id=?", msg.channel.id, msg.id, trade_id)
        else:
            embed, png = await self.render(guild, trade_id, d, "entry")
            role = find_role(guild, ALERT_ROLE)
            msg = await publish(self.bot, guild, "trade_posts", "trade_entries", content=role.mention if role else None, embed=embed,
                                file=(png, f"trade-{trade_id}.png"), kind="trade_entry", ref_id=trade_id, owner_initiated=True,
                                allowed_mentions=discord.AllowedMentions(roles=[role] if role else []))
            if msg:
                await self.bot.db.execute("UPDATE trades SET entry_channel_id=?, entry_message_id=? WHERE id=?", msg.channel.id, msg.id, trade_id)
        if msg is None:
            await self.bot.db.execute("DELETE FROM trades WHERE id = ?", trade_id)
            raise ValueError("No trade channel found. Run the server rebuild (/rebuild) or create #trade-entries and #trade-results.")
        return msg

    async def close_trade(self, guild: discord.Guild, trade_id: int, exit_price: float, *, notes: str | None = None, extra: dict | None = None) -> discord.Message:
        trade = await self.bot.db.get_trade(guild.id, trade_id)
        if trade is None:
            raise ValueError(f"Trade #{trade_id} not found.")
        if trade["status"] == "closed":
            raise ValueError(f"Trade #{trade_id} is already closed. Use /edit to change it.")
        d = dict(extra or trade["data"])
        d["exit"] = exit_price
        d.setdefault("exit_time", datetime.now(MARKET_TZ).isoformat())
        if notes:
            d["notes"] = notes
        d = await self.normalize(guild.id, d)
        d["result_caption"] = await self.caption(guild, d, "result")
        link = f"https://discord.com/channels/{guild.id}/{trade['entry_channel_id']}/{trade['entry_message_id']}" if trade["entry_message_id"] else None
        embed, png = await self.render(guild, trade_id, d, "result", entry_link=link)
        msg = await publish(self.bot, guild, "trade_posts", "results", embed=embed, file=(png, f"trade-{trade_id}.png"),
                            kind="trade_result", ref_id=trade_id, owner_initiated=True)
        if msg is None:
            raise ValueError("No #trade-results channel found. Run the server rebuild (/rebuild) first.")
        original = {**trade["original"], **{k: d.get(k) for k in ("exit", "exit_time", "points", "ticks", "pnl_usd", "pnl_pct", "r_multiple")}}
        await self.bot.db.execute(
            "UPDATE trades SET status='closed', data=?, original=?, closed_at=?, result_channel_id=?, result_message_id=? WHERE id=?",
            json.dumps(d), json.dumps(original), time.time(), msg.channel.id, msg.id, trade_id)
        await log_action(self.bot, guild, "trade_closed", f"#{trade_id} {d['contract']} {fmt_pts(d.get('points'))}")
        return msg

    # ---- slash commands ----
    @app_commands.command(name="trade", description="Post a trade (add exit to post a closed trade)")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.choices(side=SIDES)
    @app_commands.describe(symbol="e.g. NQ, MNQZ6, CL", entry="Entry price", exit="Exit price (closed trade)", stop="Stop loss",
                           target="Target", contracts="Number of contracts", notes="Your reasoning; the bot writes the caption from it",
                           show_dollars="Show the $ amount and contracts on this one post (off by default)")
    async def trade_cmd(self, interaction: discord.Interaction, symbol: str, side: app_commands.Choice[str], entry: float,
                        contracts: app_commands.Range[int, 1, 1000] = 1, stop: float | None = None, target: float | None = None,
                        exit: float | None = None, notes: str | None = None, show_dollars: bool | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        d = {"contract": symbol, "side": side.value, "entry": entry, "exit": exit,
             "stop": stop, "target": target, "contracts": contracts, "notes": notes}
        if show_dollars is not None:
            d["show_usd"] = d["show_size"] = show_dollars
        d = await self.normalize(interaction.guild.id, d)
        try:
            msg = await self.open_trade(interaction.guild, d)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(f"Posted: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="close", description="Close an open trade and post the result")
    @app_commands.guild_only()
    @owner_only()
    async def close_cmd(self, interaction: discord.Interaction, trade_id: int, exit_price: float, notes: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            msg = await self.close_trade(interaction.guild, trade_id, exit_price, notes=notes)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(f"Posted: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="open-trades", description="List open trades")
    @app_commands.guild_only()
    @owner_only()
    async def open_trades(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT id, data FROM trades WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 25", interaction.guild.id)
        if not rows:
            await interaction.response.send_message("No open trades.", ephemeral=True)
            return
        lines = []
        for r in rows:
            d = json.loads(r["data"])
            lines.append(f"`#{r['id']}` {d['side']} **{d['contract']}** x{d.get('contracts', 1)} @ {fmt_price(d.get('entry'))} (stop {fmt_price(d.get('stop'))})")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="submit", description="Send a screenshot, CSV, JSON file or text for the bot to read, then confirm")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(file="Screenshot, journal CSV or JSON export", text="Or type the trade / paste JSON")
    async def submit_cmd(self, interaction: discord.Interaction, file: discord.Attachment | None = None, text: str | None = None):
        if not file and not text:
            await interaction.response.send_message("Attach a screenshot, CSV or JSON file, or type the trade.", ephemeral=True)
            return
        await interaction.response.send_message("Reading it… the confirm card will appear here.", ephemeral=True)
        await self.intake(interaction.guild, interaction.user.id, interaction.channel, text or "", [file] if file else [])

    @app_commands.command(name="journal-link", description="Give your journal app a private link that sends trades to #trade-submit")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(action="new: make a link · revoke: disable all links · adopt: use a webhook you made in the channel settings")
    @app_commands.choices(action=[app_commands.Choice(name=n, value=n) for n in ("new", "revoke", "adopt")])
    async def journal_link(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        g = interaction.guild
        ch = get_channel(self.bot, g, "trade_submit")
        if ch is None:
            await interaction.response.send_message("There's no #trade-submit channel yet. Run the server rebuild first.", ephemeral=True)
            return
        ids = self.bot.db.get_json(g.id, "journal_webhooks", []) or []
        try:
            if action.value == "revoke":
                for wh in await ch.webhooks():
                    if wh.id in ids:
                        await wh.delete(reason="Journal link revoked by the owner")
                await self.bot.db.set_json(g.id, "journal_webhooks", [])
                await interaction.response.send_message("Done. Old journal links no longer work.", ephemeral=True)
                return
            if action.value == "adopt":
                hooks = [w for w in await ch.webhooks() if w.type == discord.WebhookType.incoming]
                if not hooks:
                    await interaction.response.send_message("No webhooks in #trade-submit. Make one in Edit Channel → Integrations → Webhooks first.", ephemeral=True)
                    return
                wh = max(hooks, key=lambda w: w.created_at)
            else:
                wh = await ch.create_webhook(name="Journal", reason="Owner's trade journal")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I need the **Manage Webhooks** permission for that. Or make the webhook yourself (Edit Channel → Integrations → "
                "Webhooks → New Webhook, copy its URL) and run `/journal-link adopt`.", ephemeral=True)
            return
        await self.bot.db.set_json(g.id, "journal_webhooks", sorted(set(ids) | {wh.id}))
        url = f"\nYour link (keep it secret, it's like a password): ||{wh.url}||" if wh.token else ""
        await interaction.response.send_message(
            f"Linked webhook **{wh.name}**.{url}\nYour journal app sends trades to it as JSON, a CSV, or a screenshot "
            "(the README has the exact format). Every trade still waits for your **Post it** tap.",
            ephemeral=True)

    @app_commands.command(name="import-journal", description="Import trades from a journal CSV (Tradovate, NinjaTrader, generic)")
    @app_commands.guild_only()
    @owner_only()
    async def import_journal(self, interaction: discord.Interaction, file: discord.Attachment, limit: app_commands.Range[int, 1, 20] = 10):
        await interaction.response.defer(ephemeral=True, thinking=True)
        trades = parse_journal(await file.read())
        if not trades:
            await interaction.followup.send("I couldn't find trades in that file. It needs symbol, entry and exit columns.", ephemeral=True)
            return
        trades = trades[-limit:]
        for t in trades:
            t["entry_time"], t["exit_time"] = _iso(t.get("entry_time")), _iso(t.get("exit_time"))
            await self.create_draft(interaction.guild, {k: v for k, v in t.items() if v is not None}, interaction.user.id, interaction.channel)
        await interaction.followup.send(f"Made {len(trades)} draft(s). Post the ones you want; cancel the rest.", ephemeral=True)

    @app_commands.command(name="export-trades", description="Download every posted trade as a CSV (only you see it)")
    @app_commands.guild_only()
    @owner_only()
    async def export_trades(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT id, status, data FROM trades WHERE guild_id=? ORDER BY id", interaction.guild.id)
        cols = ["id", "status", "contract", "symbol", "side", "contracts", "entry", "exit", "stop", "target", "points", "ticks",
                "pnl_pct", "r_multiple", "pnl_usd", "session", "entry_time", "exit_time", "notes"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**json.loads(r["data"]), "id": r["id"], "status": r["status"]})
        await interaction.response.send_message(f"{len(rows)} trade(s).", ephemeral=True,
                                                file=discord.File(io.BytesIO(buf.getvalue().encode()), filename="trades.csv"))

    symbols = app_commands.Group(name="symbols", description="Edit the futures symbol list", guild_only=True)

    @symbols.command(name="list", description="Show symbols and tick values")
    @owner_only()
    async def symbols_list(self, interaction: discord.Interaction):
        specs = await self.specs(interaction.guild.id)
        lines = [f"**{s.root}** {s.name}: tick {s.tick_size:g} = ${s.tick_value:g} · data {s.data_ticker or '—'}{' · in market posts' if s.market_posts else ''}" for s in specs.values()]
        await interaction.response.send_message("\n".join(lines) or "No symbols.", ephemeral=True)

    @symbols.command(name="add", description="Add or update a symbol")
    @owner_only()
    @app_commands.describe(root="e.g. GC", tick_size="e.g. 0.1", tick_value="$ per tick per contract, e.g. 10",
                           data_ticker="Yahoo ticker for market posts, e.g. GC=F", market_posts="Include in premarket/recap")
    async def symbols_add(self, interaction: discord.Interaction, root: str, name: str, tick_size: float, tick_value: float,
                          data_ticker: str | None = None, market_posts: bool = False):
        await self.bot.db.execute(
            "INSERT INTO symbols (guild_id, root, name, tick_size, tick_value, data_ticker, market_posts) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (guild_id, root) DO UPDATE SET name=excluded.name, tick_size=excluded.tick_size, tick_value=excluded.tick_value, "
            "data_ticker=excluded.data_ticker, market_posts=excluded.market_posts",
            interaction.guild.id, root.upper(), name, tick_size, tick_value, data_ticker, int(market_posts))
        self.invalidate_specs(interaction.guild.id)
        await interaction.response.send_message(f"Saved **{root.upper()}**.", ephemeral=True)

    @symbols.command(name="remove", description="Remove a symbol")
    @owner_only()
    async def symbols_remove(self, interaction: discord.Interaction, root: str):
        await self.bot.db.execute("DELETE FROM symbols WHERE guild_id=? AND root=?", interaction.guild.id, root.upper())
        self.invalidate_specs(interaction.guild.id)
        await interaction.response.send_message(f"Removed **{root.upper()}**.", ephemeral=True)

    # ---- the owner's JN journal ----
    async def _link_image(self, link: str) -> bytes | None:
        """A TradingView snapshot link or a direct image link -> image bytes. Other links are only shown, never fetched."""
        m = TV_SNAPSHOT_RE.match(link or "")
        url = f"https://s3.tradingview.com/snapshots/{m.group(1)[0].lower()}/{m.group(1)}.png" if m else (
            link if IMAGE_URL_RE.match(link or "") else None)
        if not url:
            return None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(url)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) < 8_000_000:
                return r.content
        except httpx.HTTPError as exc:
            log.warning("couldn't fetch chart image %s: %s", url, exc)
        return None

    async def journal_intake(self, guild: discord.Guild, author_id: int, channel, j: dict, images: list[discord.Attachment]) -> None:
        """Turn a journaled trade into a draft. The journal has no prices, so a screenshot (or TradingView link) is read
        for symbol/entry/exit; without them it becomes a journal recap post (outcome, R, setup, discipline)."""
        side = "short" if str(j.get("side", "")).lower().startswith("s") else "long"
        d: dict = {"source": "jn", "journal_id": str(j.get("journal_id") or "")[:40], "side": side,
                   "journal_outcome": {"win": "win", "loss": "loss", "be": "breakeven"}.get(str(j.get("outcome", "")).lower()),
                   "journal_rr": _num(j.get("rr")), "setup": str(j.get("setup") or "")[:200],
                   "journal_session": str(j.get("session") or "")[:60], "discipline": _num(j.get("discipline")),
                   "rules_followed": [str(r)[:80] for r in (j.get("rules_followed") or [])][:10],
                   "mistake": str(j.get("mistake") or "")[:80], "notes": strip_money(str(j.get("notes") or ""))[:900] or None,
                   "link": str(j.get("link") or "")[:300] or None, "date": str(j.get("date") or "")[:10] or None}
        image, mime = None, "image/png"
        if images:
            image, mime = await images[0].read(), images[0].content_type or "image/png"
        elif d["link"]:
            image = await self._link_image(d["link"])
        if image:
            os.makedirs(JOURNAL_DIR, exist_ok=True)
            name = re.sub(r"\W", "", d["journal_id"]) or str(int(time.time()))
            path = os.path.join(JOURNAL_DIR, f"{name}.png")
            with open(path, "wb") as fh:
                fh.write(image)
            d["image_path"] = path
            if self.bot.vision.enabled:
                async with channel.typing():
                    data = await self.bot.vision.vision_json(VISION_PROMPT, image, mime)
                t = next(iter((data or {}).get("trades") or []), None)
                if t:
                    for k in ("entry", "exit", "stop", "target"):
                        if _num(t.get(k)) is not None:
                            d[k] = _num(t.get(k))
                    if t.get("symbol"):
                        d["contract"] = str(t["symbol"])[:20]
                    if t.get("entry_time"):
                        d["entry_time"] = _iso(t.get("entry_time"))
                    if t.get("exit_time"):
                        d["exit_time"] = _iso(t.get("exit_time"))
        if d.get("contract") and d.get("entry") is not None and d.get("exit") is not None:
            await self.create_draft(guild, {k: v for k, v in d.items() if v is not None}, author_id, channel)
            return
        for k in ("entry", "exit", "stop", "target", "contract", "entry_time", "exit_time"):
            d.pop(k, None)  # half-read prices are worse than none: post the journal recap instead
        draft_id = await self.bot.db.execute(
            "INSERT INTO drafts (guild_id, kind, data, created_by, created_at) VALUES (?,?,?,?,?)",
            guild.id, "journal", json.dumps(d), author_id, time.time())
        view = discord.ui.View(timeout=None)
        for act in ("post", "edit", "cancel"):
            view.add_item(DraftButton(act, draft_id))
        e = await self.draft_embed(guild, draft_id, d)
        file = discord.File(d["image_path"], filename="chart.png") if d.get("image_path") else None
        if file:
            e.set_image(url="attachment://chart.png")
        await channel.send(embed=e, view=view, **({"file": file} if file else {}))

    def journal_title(self, d: dict) -> str:
        res = d.get("journal_outcome") or "closed"
        icon = {"win": "✅", "loss": "❌", "breakeven": "➖"}.get(res, "📓")
        rr = d.get("journal_rr")
        r_txt = f" · {rr:g}R" if rr and res == "win" else ""
        return f"{icon} {res.upper()} · {d['side'].upper()}{r_txt}"

    async def post_journal(self, guild: discord.Guild, d: dict) -> discord.Message:
        """Publish a journal recap (no prices): outcome, R, setup, session, discipline, the chart."""
        if not feature_on(self.bot, guild.id, "trade_posts"):
            raise ValueError("Trade posts are switched off. `/feature set trade_posts on` to turn them back on.")
        facts = {k: d.get(k) for k in ("side", "journal_outcome", "journal_rr", "setup", "journal_session", "discipline", "mistake", "notes")}
        caption = await self.bot.llm.chat(
            persona(self.bot, guild),
            "Write a 1-2 sentence caption for a journaled futures trade recap. Use only these facts and add no numbers that "
            "aren't here. Focus on process: the setup, following the plan, the lesson. If it's a loss, own it calmly. Never mention "
            f"dollar or pound amounts, money made or lost, or position size.\n{json.dumps(facts)}",
            max_tokens=120) or (d.get("notes") if "[amount]" not in (d.get("notes") or "") else None) or {"win": "plan worked. 🎯", "loss": "stopped out, it happens. on to the next one."}.get(
                d.get("journal_outcome"), "logged it.")
        e = discord.Embed(title=self.journal_title(d), description=strip_money(caption),
                          color={"win": GREEN, "loss": RED}.get(d.get("journal_outcome"), GREY))
        if d.get("setup"):
            e.add_field(name="Setup", value=d["setup"].split(" → ")[0][:100])
        if d.get("journal_session"):
            e.add_field(name="Session", value=d["journal_session"])
        if d.get("journal_rr") and d.get("journal_outcome") == "win":
            e.add_field(name="R:R achieved", value=f"`{d['journal_rr']:g}R`")
        if d.get("discipline") is not None:
            e.add_field(name="Discipline", value=f"`{d['discipline']:.0f}%` of my checklist")
        if d.get("link"):
            e.add_field(name="Chart", value=f"[open chart]({d['link']})", inline=False)
        e.set_footer(text=f"Journal · {DISCLAIMER}")
        e.timestamp = discord.utils.utcnow()
        file = None
        if d.get("image_path") and os.path.exists(d["image_path"]):
            with open(d["image_path"], "rb") as fh:
                file = (fh.read(), "chart.png")
            e.set_image(url="attachment://chart.png")
        msg = await publish(self.bot, guild, "trade_posts", "results", embed=e, file=file, kind="journal_result", owner_initiated=True)
        if msg is None:
            raise ValueError("No #trade-results channel found. Run the server rebuild (/rebuild) first.")
        await log_action(self.bot, guild, "journal_posted", f"{d.get('journal_outcome')} {d.get('side')} {d.get('setup', '')[:60]}")
        return msg

    # ---- #trade-submit ----
    async def intake(self, guild: discord.Guild, author_id: int, channel, text: str, attachments: list[discord.Attachment]) -> None:
        images = [a for a in attachments if (a.content_type or "").startswith("image/")]
        jn = parse_jn(text)
        if jn is not None:
            await self.journal_intake(guild, author_id, channel, jn, images)
            return
        csvs = [a for a in attachments if a.filename.lower().endswith(".csv")]
        jsons = [a for a in attachments if a.filename.lower().endswith(".json")]
        drafts: list[dict] = []
        for raw in [await a.read() for a in jsons] + ([text] if text else []):
            found = parse_json_trades(raw)
            if found is not None:
                drafts += [{k: v for k, v in t.items() if v is not None} for t in found[:20]]
                if raw is text:
                    text = ""
                if not found:
                    await channel.send("I got JSON but no trade in it. Each trade needs at least `symbol`, `side` and `entry` "
                                       "(or `close_of` and `exit` to close one).")
        for a in csvs:
            for t in parse_journal(await a.read())[-10:]:
                t["entry_time"], t["exit_time"] = _iso(t.get("entry_time")), _iso(t.get("exit_time"))
                drafts.append({k: v for k, v in t.items() if v is not None})
        for img in images:
            if not self.bot.vision.enabled:
                await channel.send("Screenshot reading needs a free vision key (VISION_PROVIDER / VISION_API_KEY in .env). Type the trade instead.")
                break
            async with channel.typing():
                data = await self.bot.vision.vision_json(VISION_PROMPT, await img.read(), img.content_type or "image/png")
            found = (data or {}).get("trades") or []
            if not found:
                await channel.send("I couldn't read a trade from that screenshot. Try a tighter crop of the fills or order history, or type it.")
            for t in found[:5]:
                drafts.append({"contract": t.get("symbol"), "side": (t.get("side") or "").lower(), "entry": _num(t.get("entry")),
                               "exit": _num(t.get("exit")), "stop": _num(t.get("stop")), "target": _num(t.get("target")),
                               "contracts": int(_num(t.get("contracts")) or 1), "pnl_usd_reported": _num(t.get("pnl_usd")),
                               "entry_time": _iso(t.get("entry_time")), "exit_time": _iso(t.get("exit_time")),
                               "notes": text or None, "source": "screenshot"})
        if not drafts and text:
            parsed = parse_text(text)
            if parsed is None:
                parsed = await self.bot.llm.json(
                    "Extract a futures trade from the owner's message.",
                    f"Message: {text}\nFormat: {{\"contract\": str, \"side\": \"long|short\", \"entry\": num, \"exit\": num|null, "
                    "\"stop\": num|null, \"target\": num|null, \"contracts\": int|null, \"notes\": str|null}. Use null for anything not stated.")
                if not isinstance(parsed, dict) or parsed.get("entry") is None:
                    parsed = None
            if parsed:
                drafts.append({k: v for k, v in parsed.items() if v is not None})
        if not drafts:
            if text or attachments:
                await channel.send("Didn't catch a trade there. Try `long NQ 21012.25 sl 20992 tp 21072 x2`, `close 12 at 21062.5`, or a screenshot.")
            return
        for d in drafts:
            await self.create_draft(guild, d, author_id, channel)

    @commands.Cog.listener()
    async def on_journal_message(self, message: discord.Message):
        submit = get_channel(self.bot, message.guild, "trade_submit")
        if submit and message.channel.id == submit.id:
            await self.intake(message.guild, self.bot.config.owner_id, message.channel, message.content, message.attachments)

    @commands.Cog.listener()
    async def on_clean_message(self, message: discord.Message):
        if message.author.id != self.bot.config.owner_id:
            return
        submit = get_channel(self.bot, message.guild, "trade_submit")
        if not submit or message.channel.id != submit.id:
            return
        await self.intake(message.guild, message.author.id, message.channel, message.content, message.attachments)


async def setup(bot) -> None:
    bot.add_dynamic_items(DraftButton)
    await bot.add_cog(Trades(bot))
