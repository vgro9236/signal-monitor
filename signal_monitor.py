"""
Signal Monitor — Two-Stage Confirmation Edition
- Stage 1: Haiku analyses every cycle (cheap, fast)
- Stage 2: Sonnet confirms ONLY when Haiku flags confidence ≥ threshold
- Alert logic:
    Both agree on direction + both ≥7 confidence → HIGH CONVICTION alert
    Same direction, Sonnet conf 5-6                → MODERATE alert ("watch")
    Disagree on direction                          → SILENT (log only)
    Sonnet downgrades below 5                      → SILENT (log only)
- Pulls news from free RSS feeds
- Sends Telegram notifications to your phone
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import anthropic

# ────────────────────────────────────────────────────────────────────
# MODEL CONFIG
# ────────────────────────────────────────────────────────────────────

SCREENING_MODEL = "claude-haiku-4-5-20251001"       # stage 1 — every cycle
CONFIRMATION_MODEL = "claude-sonnet-4-5-20250929"   # stage 2 — only on high conf
SCREENING_THRESHOLD = 7  # Haiku must hit this before Sonnet is called

# ────────────────────────────────────────────────────────────────────
# RSS FEEDS
# ────────────────────────────────────────────────────────────────────

FEEDS = {
    "macro_news": [
        "https://www.forexlive.com/feed/",
        "https://www.investing.com/rss/news.rss",
        "https://www.investing.com/rss/news_25.rss",
        "https://www.investing.com/rss/news_285.rss",
        "http://feeds.marketwatch.com/marketwatch/marketpulse/",
        "http://feeds.marketwatch.com/marketwatch/topstories/",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "https://www.zerohedge.com/fullrss2.xml",
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.fxstreet.com/rss/news",
    ],
    "central_banks": [
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.federalreserve.gov/feeds/speeches.xml",
        "https://www.ecb.europa.eu/rss/press.html",
        "https://www.bankofengland.co.uk/rss/news",
    ],
    "crypto": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://bitcoinmagazine.com/.rss/full/",
    ],
    "geopolitics": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
}

ASSET_FEEDS = {
    "XAUUSD": ["macro_news", "central_banks", "geopolitics"],
    "BTCUSD": ["macro_news", "crypto", "central_banks"],
    "SPY":    ["macro_news", "central_banks"],
    "US30":   ["macro_news", "central_banks", "geopolitics"],
    "NAS100": ["macro_news", "central_banks", "crypto"],
}

ASSET_PROFILES = {
    "XAUUSD": {
        "display_name": "Gold Spot vs USD",
        "factors": [
            # MONETARY / RATES (the dominant long-term driver)
            "Real yields (10y TIPS yield) — single most correlated factor with gold; lower real yields are STRONGLY bullish",
            "Federal Reserve policy: latest FOMC decision, dot plot, Powell/Williams/Waller/other FedSpeak, balance sheet (QT/QE)",
            "Other central banks: ECB, BoE, BoJ policy (BoJ especially via yen carry trade and USD/JPY)",
            "US 10-year Treasury yield nominal direction and 2s10s curve",
            # INFLATION DATA (resets the entire monetary picture)
            "Inflation prints: latest CPI, Core CPI, PCE, Core PCE, PPI — actual vs expected matters more than absolute level",
            "Inflation expectations: Michigan survey, 5y5y forward breakevens, TIPS-implied breakevens",
            # USD
            "US Dollar Index (DXY) direction and key pairs (EUR/USD, USD/JPY)",
            # GEOPOLITICS
            "Geopolitical tensions: Middle East, Russia-Ukraine, China-Taiwan, US election risk, tariff/trade war news, banking stress",
            # PHYSICAL DEMAND
            "Central bank gold buying (PBoC, RBI, Turkey, Poland — large structural bid)",
            "Gold ETF flows (GLD, IAU holdings changes — institutional positioning)",
            "Physical demand seasonality (Indian wedding/festival demand, Chinese New Year)",
            # OIL & GROWTH DATA
            "Oil prices (Brent/WTI) — direct inflation input, indirect via Fed implications",
            "Other US economic data: NFP, ISM Manufacturing/Services, retail sales, GDP nowcasts",
            # RISK SENTIMENT
            "Risk sentiment regime: VIX, equity action, credit spreads — gold can act as safe-haven OR move with risk depending on regime",
            "Competing safe-haven flows: USD strength (sometimes competes with gold), BTC ('digital gold' narrative), Swiss franc",
            # FORWARD-LOOKING
            "Upcoming high-impact events in next 24-72h: FOMC, CPI/PCE release dates, NFP, Powell speeches, key geopolitical deadlines",
        ],
    },
    "BTCUSD": {
        "display_name": "Bitcoin vs USD",
        "factors": [
            # FLOWS (the dominant driver since spot ETFs launched)
            "Spot ETF flows: IBIT, FBTC, ARKB, GBTC daily net inflows/outflows — biggest single short-term driver",
            "Stablecoin market cap (USDT, USDC) — liquidity entering crypto",
            "On-chain: exchange balances (declining = bullish), LTH supply, MVRV, miner flows, dormant supply moving",
            # MACRO
            "Macro liquidity: Fed policy, balance sheet, global M2, RRP and TGA dynamics",
            "DXY direction (strong inverse correlation short/medium-term)",
            "Real yields (BTC competes with bonds as 'pristine collateral' / store of value)",
            "Risk sentiment: NQ correlation, VIX, equity action",
            # REGULATORY
            "US regulatory news: SEC, CFTC, Treasury, OCC, ETF approvals/rejections, custody rules",
            "Global regulatory: EU MiCA, Asia (Hong Kong, Japan, Korea), enforcement actions",
            # MARKET STRUCTURE
            "Derivatives positioning: funding rates, open interest, options skew, CME basis",
            "Whale wallet activity, large transactions, exchange deposits/withdrawals",
            "BTC dominance trend (rotation in/out of alts signals risk appetite within crypto)",
            # NETWORK
            "Mining: hashrate, difficulty adjustments, miner capitulation, post-halving supply dynamics",
            # NARRATIVE / SENTIMENT
            "Corporate treasury adoption (MicroStrategy, Tesla, miners holding BTC)",
            "Geopolitical: BTC sometimes catches safe-haven flows on USD-system stress, sanctions, banking issues",
            # FORWARD-LOOKING
            "Upcoming high-impact events: FOMC, CPI, ETF flow data, options expiries (monthly/quarterly), regulatory deadlines",
        ],
    },
    "SPY": {
        "display_name": "S&P 500 ETF",
        "factors": [
            # EARNINGS (the dominant longer-term driver)
            "Earnings season: actual vs estimates, guidance changes, revisions trend, margin commentary",
            "Forward earnings estimates and consensus revisions for S&P 500 aggregate",
            # MONETARY
            "Fed policy: latest FOMC, dot plot, Powell/FedSpeak, balance sheet runoff pace",
            "10-year Treasury yield (rising yields compress equity multiples, especially for long-duration growth)",
            "Real yields and TIPS dynamics",
            # INFLATION DATA
            "CPI, Core CPI, PCE, Core PCE — surprise vs expected drives Fed re-pricing",
            "Inflation expectations and breakevens",
            # ECONOMIC HEALTH
            "Labour market: NFP, unemployment rate, JOLTS, wage growth, jobless claims",
            "Growth data: ISM Manufacturing, ISM Services, GDP nowcasts, retail sales, durable goods",
            "Consumer health: confidence (Conference Board, Michigan), credit card delinquencies",
            # INTERNAL MARKET STRUCTURE
            "Mega-cap tech leadership (NVDA, AAPL, MSFT, GOOGL, META, AMZN, TSLA — they drive >30% of SPY)",
            "Market breadth: advance/decline, % above 50/200 DMA, equal-weight vs cap-weight divergence",
            "Sector rotation: defensive (XLU, XLP) vs cyclical (XLI, XLF, XLY) leadership",
            # POSITIONING / FLOWS
            "VIX level and term structure, put/call ratio, dealer gamma positioning",
            "Credit spreads: HY OAS, IG spreads (widening = risk-off warning)",
            "Fund flows: ETF flows into SPY/QQQ, CFTC positioning",
            # MACRO HEADWINDS
            "Dollar strength impact on multinational earnings",
            "Geopolitical risk, tariff/trade policy, election risk",
            # FORWARD-LOOKING
            "Upcoming high-impact events: FOMC, CPI, NFP, mega-cap earnings dates, Powell speeches",
        ],
    },
    "US30": {
        "display_name": "Dow Jones Industrial Average",
        "factors": [
            # CYCLICAL / INDUSTRIAL HEALTH (more relevant to Dow than SPY)
            "ISM Manufacturing PMI, ISM Services PMI — direct read on Dow's industrial-heavy composition",
            "Durable goods orders, factory orders, industrial production",
            "Cass Freight Index, rail/trucking data, port volumes (real-economy signals)",
            # EARNINGS — heavyweight specifics
            "Dow heavyweight earnings (UNH, MSFT, GS, HD, CAT, V, JPM, BA — price-weighted index, so individual names move it hard)",
            "Forward guidance from cyclical leaders (CAT, DE, MMM, HON, GE)",
            # MONETARY
            "Fed policy and rate expectations",
            "10-year Treasury yield (rate-sensitive financials and dividend payers)",
            "Long-end yields (rate-sensitive utilities and REITs within Dow exposure)",
            # INFLATION
            "CPI, PCE, PPI — input cost pressures for industrials",
            # TRADE / DOLLAR
            "Trade policy and tariffs (Dow has ~50% international revenue exposure)",
            "Dollar strength (multinational translation effects — heavier impact than SPY)",
            "China growth data and PMI (Caterpillar, Boeing, materials demand)",
            # COMMODITY INPUTS
            "Oil prices (CVX direct, energy input costs broadly, transportation costs)",
            "Industrial metals (copper, steel — input costs)",
            # CONSUMER
            "Consumer confidence, retail sales, housing starts (relevant to Dow consumer names)",
            # RISK
            "VIX, credit spreads, geopolitical events affecting global trade and supply chains",
            # FORWARD-LOOKING
            "Upcoming high-impact events: FOMC, ISM, Dow component earnings, China data releases",
        ],
    },
    "NAS100": {
        "display_name": "Nasdaq 100",
        "factors": [
            # TECH EARNINGS (the dominant driver — top 7 names are ~50% of NDX)
            "Mega-cap tech earnings: AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA — single names can move NDX 2-3%",
            "Forward guidance from hyperscalers on cloud capex (AWS, Azure, GCP)",
            "AI-related capex announcements and semiconductor demand outlook",
            # MONETARY (especially impactful on long-duration growth)
            "Fed policy and rate expectations",
            "10-year Treasury yield (massive impact on long-duration growth multiples)",
            "Real yields (TIPS) — long-duration tech extremely sensitive to real rate moves",
            # INFLATION
            "CPI, Core CPI, PCE — surprises drive Fed re-pricing and growth multiple compression/expansion",
            # SEMICONDUCTOR-SPECIFIC
            "NVDA earnings, AI demand commentary, hyperscaler order trends",
            "Semiconductor supply chain news (TSMC, ASML, Samsung)",
            "China-Taiwan tensions and chip export controls",
            # REGULATORY
            "Antitrust news: DOJ, FTC, EU DMA actions against mega-caps",
            "AI regulation, content moderation, privacy laws",
            # MARKET STRUCTURE
            "VIX, VXN (Nasdaq vol index), options positioning, single-name dealer gamma in NVDA/AAPL/TSLA",
            "ETF flows: QQQ, XLK, semiconductor ETFs (SMH, SOXX)",
            "Tech-specific sentiment indicators",
            # CROSS-ASSET
            "Dollar direction (NDX components have heavy international revenue)",
            "Crypto correlation (NQ-BTC linkage on risk-on/risk-off days)",
            # FORWARD-LOOKING
            "Upcoming high-impact events: FOMC, CPI, NFP, NVDA/MSFT/AAPL/GOOGL earnings dates, major tech conferences",
        ],
    },
}

# ────────────────────────────────────────────────────────────────────
# RSS FETCHER
# ────────────────────────────────────────────────────────────────────

def fetch_news(categories, hours_back=24, per_feed_cap=25):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    items = []
    for cat in categories:
        for url in FEEDS.get(cat, []):
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:per_feed_cap]:
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else datetime.now(timezone.utc)
                    if pub_dt < cutoff:
                        continue
                    summary = entry.get("summary", "") or entry.get("description", "")
                    summary = re.sub(r"<[^>]+>", "", summary)[:400]
                    items.append({
                        "title": entry.get("title", "").strip(),
                        "summary": summary.strip(),
                        "source": (feed.feed.get("title") or url.split("/")[2])[:40],
                        "published": pub_dt.isoformat(),
                    })
            except Exception as e:
                print(f"  ⚠ feed failed: {url[:60]} ({e})")

    items.sort(key=lambda x: x["published"], reverse=True)
    seen, unique = set(), []
    for it in items:
        key = it["title"][:80].lower()
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique[:100]


# ────────────────────────────────────────────────────────────────────
# PROMPT + ANALYSIS
# ────────────────────────────────────────────────────────────────────

def build_prompt(symbol, news_items, role="screening"):
    profile = ASSET_PROFILES[symbol]
    factors_list = "\n".join(f"  {i+1}. {f}" for i, f in enumerate(profile["factors"]))
    if news_items:
        news_block = "\n\n".join(
            f"[{it['source']} · {it['published'][:16]}]\n{it['title']}\n{it['summary']}"
            for it in news_items
        )
    else:
        news_block = "(no recent items fetched — base analysis on general knowledge)"

    role_note = (
        "You are doing a fast first-pass screening." if role == "screening"
        else "You are doing CONFIRMATION analysis. A first-pass screener flagged this setup as high-conviction. "
             "Your job is to verify or push back, paying special attention to nuances, second-order effects, "
             "and whether the news genuinely supports the bias or just looks like it does on surface."
    )

    return f"""You are a senior macro analyst monitoring {profile['display_name']} ({symbol}).
{role_note}

Recent news from major free public sources (last ~24h):

NEWS:
─────
{news_block}
─────

Score these drivers:
{factors_list}

Return ONLY a JSON object — no preamble, no markdown fences:

{{
  "symbol": "{symbol}",
  "overall_bias": "BUY" or "SELL" or "NEUTRAL",
  "confidence": <integer 1-10>,
  "current_price": "<approx level if mentioned, else best estimate>",
  "factors": [
    {{
      "name": "<short factor label>",
      "signal": "BULLISH" or "BEARISH" or "NEUTRAL",
      "summary": "<1-2 sentences with specifics>"
    }}
  ],
  "key_levels": {{"support": "<price>", "resistance": "<price>"}},
  "narrative": "<3-4 sentences: dominant story and recent shifts>",
  "catalysts_ahead": ["<upcoming events>"],
  "risk_warning": "<what would invalidate the bias>"
}}

Rules:
- BULLISH = bullish FOR THE ASSET. Mind inverse correlations (weaker USD = bullish gold/BTC; lower real yields = bullish gold; lower nominal yields = bullish equities).
- Cite specific data / sources from the news where possible (e.g. "CPI came in at 3.1% vs 3.0% expected per CNBC").
- DATA RELEASES are high-priority: if a major release (CPI, PCE, NFP, FOMC, GDP) happened in the news window, it dominates the bias.
   • Hot inflation surprise → typically BEARISH gold (initial), BEARISH equities, BULLISH dollar
   • Cool inflation surprise → typically BULLISH gold, BULLISH equities, BEARISH dollar
   • Hawkish Fed surprise → BEARISH gold/equities, BULLISH dollar
   • Dovish Fed surprise → BULLISH gold/equities, BEARISH dollar
- For XAUUSD specifically: real yields and DXY are the two single biggest factors. Weight them accordingly.
- Always populate "catalysts_ahead" with upcoming releases/events in the next 24-72h that could flip the bias.
- If news is sparse or mixed, lower confidence to 4-6.
- Confidence 8+ requires CLEAR factor confluence (5+ aligned in same direction) AND no major contradicting forces.
- If a major upcoming catalyst is hours away (e.g. FOMC tomorrow), cap confidence at 6 — markets often chop into events.

JSON only.
"""


def analyze(symbol, client, model, news, role="screening"):
    response = client.messages.create(
        model=model,
        max_tokens=3000,
        messages=[{"role": "user", "content": build_prompt(symbol, news, role)}],
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON in output. Got: {text[:200]}")
    result = json.loads(cleaned[start:end])
    result["model"] = model
    result["role"] = role
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result


# ────────────────────────────────────────────────────────────────────
# TWO-STAGE DECISION LOGIC
# ────────────────────────────────────────────────────────────────────

def decide_alert(screening, confirmation, prev_state):
    """
    Returns (should_alert: bool, alert_tier: str, reason: str)

    alert_tier ∈ {"HIGH", "MODERATE", "FLIP", "NONE"}
    """
    # Skip if Haiku didn't flag anything in the first place
    if confirmation is None:
        # Haiku-only path: only alert on bias flips at moderate+ confidence
        if not prev_state:
            return False, "NONE", "first run, screening only, below threshold"
        if prev_state.get("bias") != screening["overall_bias"] and screening["confidence"] >= 5:
            return True, "FLIP", f"bias flip {prev_state.get('bias')} → {screening['overall_bias']} (screening only)"
        return False, "NONE", "no alert from screening"

    # Both models ran — apply two-stage logic
    s_bias, s_conf = screening["overall_bias"], screening["confidence"]
    c_bias, c_conf = confirmation["overall_bias"], confirmation["confidence"]

    # Direction disagreement → silent. The models can't agree, don't trade.
    if s_bias != c_bias:
        return False, "NONE", f"DISAGREEMENT: Haiku={s_bias} vs Sonnet={c_bias} — silent log"

    # Both agree on direction, both high conviction → top tier
    if c_conf >= 7:
        # If this is also a flip from previous state, flag it as such
        if prev_state and prev_state.get("bias") != c_bias:
            return True, "FLIP", f"FLIP + CONFIRMED: {prev_state.get('bias')} → {c_bias}, both models conf ≥7"
        # Or if confidence has stepped up meaningfully
        if prev_state and prev_state.get("confidence", 0) < 7:
            return True, "HIGH", f"confidence climbed to confirmed high conviction"
        if not prev_state:
            return True, "HIGH", "first signal, confirmed high conviction"
        return False, "NONE", "already alerted at this level"

    # Sonnet downgrades conviction to moderate (5-6) — softer alert
    if c_conf >= 5:
        if not prev_state or prev_state.get("bias") != c_bias:
            return True, "MODERATE", f"watch-level: Haiku flagged {s_conf}/10, Sonnet sees {c_conf}/10"
        return False, "NONE", "moderate already known"

    # Sonnet downgrades below 5 → quiet
    return False, "NONE", f"Sonnet downgraded to {c_conf}/10 — no alert"


# ────────────────────────────────────────────────────────────────────
# TELEGRAM FORMATTING
# ────────────────────────────────────────────────────────────────────

TIER_HEADERS = {
    "HIGH": "🎯 *HIGH CONVICTION*",
    "FLIP": "⚡ *BIAS FLIP — CONFIRMED*",
    "MODERATE": "👀 *WATCH (moderate)*",
}


def send_telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  ⚠ telegram failed: {e}")
        return False


def _smart_truncate(text, max_len):
    """Truncate at a word boundary so we don't cut mid-word."""
    if not text or len(text) <= max_len:
        return text
    cutoff = text[:max_len].rsplit(" ", 1)[0]
    return cutoff + "…"


def format_message(screening, confirmation, tier):
    """Build the Telegram message. confirmation may be None for screening-only paths."""
    primary = confirmation if confirmation else screening
    bias_emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "🟡"}
    sig_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}

    header = TIER_HEADERS.get(tier, "📊 *SIGNAL*")
    msg = f"{header}\n"
    msg += f"{bias_emoji.get(primary['overall_bias'], '⚪')} *{primary['symbol']}: {primary['overall_bias']}* `{primary['confidence']}/10`"

    if confirmation:
        msg += f"  _(Haiku {screening['confidence']}/10 → Sonnet {confirmation['confidence']}/10)_"
    msg += f"\n_{primary.get('current_price', '?')}_\n\n"
    msg += f"{primary.get('narrative', '')}\n\n"

    msg += "*Factors:*\n"
    for f in primary.get("factors", [])[:8]:
        summary = _smart_truncate(f.get("summary", ""), 220)
        msg += f"{sig_emoji.get(f['signal'], '⚪')} *{f['name']}:* {summary}\n"

    levels = primary.get("key_levels", {})
    msg += f"\n📍 S `{levels.get('support', '?')}` · R `{levels.get('resistance', '?')}`\n"
    msg += f"\n⚠️ _{_smart_truncate(primary.get('risk_warning', ''), 350)}_"
    return msg[:4000]


# ────────────────────────────────────────────────────────────────────
# STATE & LOGGING
# ────────────────────────────────────────────────────────────────────

STATE_FILE = Path("signal_state.json")
LOG_DIR = Path("signal_logs")


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def save_log(entry, symbol):
    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{symbol}_{today}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────

def run_once():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEY not set"); sys.exit(1)

    symbols = [s.strip().upper() for s in os.getenv("SYMBOLS", "XAUUSD").split(",") if s.strip()]
    tg_token = os.getenv("TELEGRAM_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")

    client = anthropic.Anthropic(api_key=api_key)
    state = load_state()

    print(f"▶ Two-stage Signal Monitor · {datetime.now().isoformat(timespec='seconds')}")
    print(f"  Symbols: {symbols}")
    print(f"  Stage 1: {SCREENING_MODEL}  ·  threshold conf ≥ {SCREENING_THRESHOLD}")
    print(f"  Stage 2: {CONFIRMATION_MODEL}  (only when stage 1 trips)")
    print(f"  Telegram: {'configured' if (tg_token and tg_chat) else 'NOT configured'}\n")

    for symbol in symbols:
        if symbol not in ASSET_PROFILES:
            print(f"  ✗ Unknown: {symbol}"); continue

        print(f"━━ {symbol} ━━")
        try:
            feed_cats = ASSET_FEEDS.get(symbol, ["macro_news"])
            news = fetch_news(feed_cats)
            print(f"  → {len(news)} news items")

            # STAGE 1 — Haiku
            screening = analyze(symbol, client, SCREENING_MODEL, news, "screening")
            s_bias, s_conf = screening["overall_bias"], screening["confidence"]
            colors = {"BUY": "\033[92m", "SELL": "\033[91m", "NEUTRAL": "\033[93m"}
            print(f"  [Haiku]   {colors.get(s_bias, '')}{s_bias}\033[0m {s_conf}/10  @ {screening.get('current_price', '?')}")

            # STAGE 2 — Sonnet (only if Haiku tripped the threshold)
            confirmation = None
            if s_conf >= SCREENING_THRESHOLD:
                print(f"  → Haiku flagged ≥{SCREENING_THRESHOLD}, calling Sonnet to confirm…")
                confirmation = analyze(symbol, client, CONFIRMATION_MODEL, news, "confirmation")
                c_bias, c_conf = confirmation["overall_bias"], confirmation["confidence"]
                agree = "✓ agree" if c_bias == s_bias else "✗ DISAGREE"
                print(f"  [Sonnet]  {colors.get(c_bias, '')}{c_bias}\033[0m {c_conf}/10  ({agree})")

            # Decide whether to alert
            prev = state.get(symbol)
            alert, tier, reason = decide_alert(screening, confirmation, prev)
            print(f"  Alert: {alert} [{tier}] — {reason}")

            # Log everything regardless
            save_log({
                "timestamp": screening["timestamp"],
                "screening": screening,
                "confirmation": confirmation,
                "alert": alert,
                "tier": tier,
                "reason": reason,
            }, symbol)

            # Send Telegram only on alert
            if alert and tg_token and tg_chat:
                msg = format_message(screening, confirmation, tier)
                sent = send_telegram(msg, tg_token, tg_chat)
                print(f"  Telegram: {'✓ sent' if sent else '✗ failed'}")

            # Update state — use confirmation if available, else screening
            primary = confirmation or screening
            state[symbol] = {
                "bias": primary["overall_bias"],
                "confidence": primary["confidence"],
                "ts": primary["timestamp"],
                "price": primary.get("current_price"),
                "last_tier": tier,
            }

        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}")

    save_state(state)
    print("\n✓ Cycle complete.")


def run_loop():
    interval = int(os.getenv("INTERVAL_MINUTES", "20"))
    print(f"Loop mode · every {interval} min · Ctrl+C to stop\n")
    while True:
        run_once()
        print(f"\n💤 Sleeping {interval} min…\n")
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("\n👋 Stopped."); break


if __name__ == "__main__":
    if os.getenv("RUN_ONCE") or "--once" in sys.argv:
        run_once()
    else:
        run_loop()
