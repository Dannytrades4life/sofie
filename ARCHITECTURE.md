# Architecture and build plan (v3: Sofie)

## Tech stack (all $0)

| Piece | Choice | Why |
|---|---|---|
| Bot | Python 3.11+, **discord.py 2.4+**, `commands.Bot` | Official Bot API only. Slash commands, buttons, modals, native polls. Built-in rate-limit handling, reconnects and automatic sharding. |
| Database | **SQLite** (WAL) via aiosqlite | One file, zero setup. Every table is keyed by `guild_id`; moving to free Postgres (Neon / Supabase) means re-implementing `bot/db.py` only. |
| Text AI | **Groq** free tier (default) · Gemini · OpenRouter `:free` · Ollama (local) | All OpenAI-compatible, one client. Throttled for free-tier limits; falls back to templates so the bot never stops. |
| Vision AI | **Gemini** free tier (default) · Groq Llama 4 Scout · Ollama `llama3.2-vision` | Reads trade screenshots into structured fields you confirm. |
| Market data | Yahoo Finance chart API (delayed, unofficial) · Forex Factory weekly calendar JSON | Free, no keys. If either is down the post is skipped, never invented. |
| GIFs | GIPHY free API key (optional) | Attribution added automatically. |
| Graphics | Pillow | Branded entry/result cards and the default custom emojis, rendered locally. |
| Hosting | **Oracle Cloud Always Free** Arm VM, 1 of the 2 free CPUs (recommended) · Google Cloud e2-micro (fallback) | A bot needs a 24/7 process. systemd restarts it on crash and boot; a watchdog exits on a dead connection so it restarts clean; daily "online" DM; rotating logs; daily DB backups. |

## How it fits together

```
Discord gateway ──► TradingBot (commands.Bot)
  on_message ─► Moderation.inspect() ─ removed? stop
                 └─► "clean_message" ─► Levels · Engagement (replies) · Trades (#trade-submit) · Community (journal)
  slash commands ─► Trades · Edit · Panel · Giveaways · Rebuild · Market · Community · Growth · Experiments · Reports
  schedulers (NY time) ─► Market (calendar, premarket, recap, weekly) · Engagement (QOTD, poll, lessons, setups, GIFs,
                          quiet starters) · Community (challenges) · Experiments (plan / run / measure) · Reports · Giveaways (draws)

bot/control.py  publish(): kill switch → feature on? → your /teach rulebook → approval mode (auto | ask) → send + record
                staff(): owner always; Leaders/Mods per /perm, only in #staff-panel
bot/safety.py   action log (#bot-log + reports) · Approve/Deny DM buttons that survive restarts
bot/futures.py  contract specs (editable /symbols), symbol parsing (MNQZ6, /NQ, ES DEC26), points/ticks/%/R, sessions,
                Display: posts hide $ and contract count unless /display or a per-trade flag turns them on
journal app ─► webhook in #trade-submit (/journal-link) ─► same intake as the owner: JSON, CSV, screenshot ─► confirm card
bot/llm.py      text + vision clients with template fallback
```

## Autonomy and control

| | |
|---|---|
| **Acts alone (logged)** | welcomes, replies, daily question, Wednesday poll, quiet-chat starters, lessons, setup breakdowns, GIFs, calendar + alerts, premarket, recap, weekly performance, challenges, experiments, level/invite roles, spam/scam deletes and timeouts, raid slowmode |
| **Always asks you (DM buttons)** | bans, @everyone announcements, raid lockdown, every server-rebuild stage (stage 6 lists every channel it would delete and DMs a backup of each first), giveaways drafted by Leaders, every trade read from your journal |
| **Your choice per feature** | `/feature mode <feature> ask` makes any autonomous feature wait for Approve; `/feature set <feature> off` turns it off |
| **Your rules win** | `/teach add never post during FOMC`. Every autonomous post is checked against the rulebook (with the economic calendar as context) before it goes out, and the rulebook is injected into everything the bot writes. Held posts are logged. |
| **Kill switch** | `/kill`, `/pause` (leaders can pause), or DM the bot `stop`. Resume with `/resume` or DM `resume`. Persists across restarts. |
| **Staff** | Leaders and Mods (or roles you map with `/staff-roles`). `/perm` sets which commands each level can use. Staff commands only work in #staff-panel. Only you can change numbers on posted results. |

**Money stays private:** cards, embeds, captions, recaps, weekly summaries and social ideas show points, ticks, % price move,
R and (optionally) % of account. Dollar amounts and position size only appear if you switch them on. Your DM reports keep $.

**Honesty rules baked in:** the bot posts only numbers you gave or confirmed, posts losses like wins, puts "Not financial
advice" on every trade post and card, labels member trades as self-reported, and its persona forbids inventing results,
members or testimonials. Original trade values are kept forever and every edit is logged (who, when, old → new).

## Phased plan

**Phase 1 — MVP (done)** Sofie connects to your server and DMs you to confirm (the Phase 1 check), then: trade posts, welcome, XP, moderation, daily/weekly reports, approvals, kill switch.

**Phase 2 — Futures + control (done)**
- Futures trade intake: `/trade`, `/close`, text or screenshots in #trade-submit, `/submit`, `/import-journal` (Tradovate, NinjaTrader, generic CSV) → confirm card (Post / Edit / Cancel) → branded PNG + embed. Points, ticks, contracts, $ P&L, R, planned R:R, risk, Asia/London/NY session.
- Owner/staff panel: `/edit` + `/edit-history`, `/teach`, `/feature`, `/perm`, `/staff-roles`, `/identity`, `/pause`, `/resume`, `/kill`, `/status`, `/announce`.
- Giveaways + sponsors: plain-English create, button questions for anything ambiguous, preview → approve → live post with countdown and entry count → weighted random draw → DM claim → automatic re-draw → full log.
- Server rebuild: snapshot → full plan in DMs → 5 approved stages (roles, channels, permissions, archive, branding) → undo per stage or all.
- Market content: calendar + 15-minute alerts, premarket plan with levels and regime, EOD recap, weekly performance.
- Engagement: lessons, setup breakdowns, GIFs, member trade sharing, journal channel XP, weekly challenges, weekly self-proposed experiments with measured results, invite rewards, social post ideas with per-platform tracked invites.

**Phase 2.5 — Sofie (done, this version)**
- Name and persona: Sofie (she/her). Posts hide dollars and size by default (`/display`, per-trade `show_dollars`).
- Journal app link (`/journal-link` webhook), JSON trade intake with exact fields, `/submit` takes any file, `/export-trades`.
- 24/7 hosting: step-by-step Oracle guide (with Google Cloud fallback), auto-restart, watchdog, daily "Sofie is online" DM, crash-restart DM, `/health`, `/logs`, log rotation, daily backups.
- Rebuild stage 6: deletes archived channels unused for 60 days after a backup DM and its own approval; tidies the order.

**Phase 3 — Polish (next)**
- Server-icon/banner generator, sticker pack generator, per-member opt-in DM digests (opt-in only).
- Multi-image screenshot stitching; attach your chart screenshot to the trade post.
- Postgres migration script; web dashboard (free on Cloudflare Pages) for the edit log and stats.

**Phase 4 — Scale**
- Multi-process sharding (`shard_ids` per process) and Redis (Upstash free tier) for shared cooldowns.
- Move LLM calls to a queue with priorities (owner > replies > content).

## What can't be done for free (or at all)

- **SMS**: dropped as you asked. Twilio needs paid credit and US 10DLC registration.
- **Live, official market data**: free sources are delayed and unofficial (Yahoo). Real-time CME data requires a paid licence. Premarket/recap posts say the data is delayed.
- **Very large scale hosting**: the code shards automatically, but one free VM tops out somewhere in the tens of thousands of members. Beyond that you'd pay for hosting.
- **AI volume**: free tiers have per-minute and per-day caps. The bot throttles itself and falls back to templates, so in a very busy server some replies will be canned. Ollama on the Oracle VM removes caps at the cost of speed.
- **Perfect alt detection**: Discord doesn't expose IPs or devices to bots. Giveaways use account age, time in server, verified role, one entry per account, timeouts, and flag no-avatar new accounts in the log. Determined alt farms can still slip through.
- **Custom emoji/sticker slots** depend on your server boost level (50 emojis and 5 stickers at level 0).
- **Username changes** are rate-limited by Discord (about 2 per hour); the bot sets its server nickname immediately instead.
- **Card verification**: Oracle and Google Cloud ask for a card to verify identity even on Always Free; you're not charged on free shapes.
- **Oracle idle reclaim**: Oracle may reclaim an Always Free VM that stays under 20% CPU, network and memory for 7 days, which a light bot can. Upgrading to Pay As You Go (still $0 on Always Free shapes, with a $1 budget alert) is the usual fix; otherwise the daily online DM and backups make a rebuild quick.
- **Oracle capacity**: free Arm machines are sometimes "out of capacity" in busy regions; retry, switch availability domain, or use the free AMD micro shape.
