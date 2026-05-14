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

def build_prompt(symbol, news_items, role="screening", price_context=None, maturity_context=None):
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

    context_block = ""
    if price_context:
        context_block += f"\n\nPRICE ACTION CONTEXT:\n{price_context}\n"
    if maturity_context:
        context_block += f"\nBIAS HISTORY (your previous reads):\n{maturity_context}\n"

    return f"""You are a senior macro analyst monitoring {profile['display_name']} ({symbol}).
{role_note}
{context_block}
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
- PHASE AWARENESS — if PRICE ACTION CONTEXT shows the move in your bias direction has ALREADY happened (>1% in 4h or >2% in 24h), this is a PHASE 2/3 scenario, not PHASE 1:
   • Phase 1 (news fresh, move hasn't started) → full conviction OK
   • Phase 2 (move in progress, 30-180 min after catalyst) → REDUCE confidence by 1-2 points, note "move underway, wait for retest"
   • Phase 3 (move extended, >3h or >1% in direction) → REDUCE confidence further, note "move extended, late to chase, look for pullback entry"
- If BIAS HISTORY shows this is a fresh reaction (different from prior 2-3 reads), mark this clearly — first-cycle reactions are less reliable than sustained reads.
- Always populate "catalysts_ahead" with upcoming releases/events in the next 24-72h that could flip the bias.
- If news is sparse or mixed, lower confidence to 4-6.
- Confidence 8+ requires CLEAR factor confluence (5+ aligned in same direction) AND no major contradicting forces AND move not extended.
- If a major upcoming catalyst is hours away (e.g. FOMC tomorrow), cap confidence at 6 — markets often chop into events.

Additionally include in your JSON:
  "phase": "PHASE_1" (fresh, move not started) | "PHASE_2" (move in progress) | "PHASE_3" (move extended, late) | "UNKNOWN"
  "entry_guidance": "<1 sentence on where/when to consider entry — e.g. 'short on retest of $X resistance' or 'move extended, wait for pullback to $Y before considering'>"

JSON only.
"""


def fetch_price_action(symbol):
    """
    Fetch recent price action context for the symbol via free public API.
    Returns a short string like '$4,650 · -2.1% last 24h · -0.8% last 4h'
    Falls back to None if API unavailable.
    """
    # Yahoo Finance proxy symbols for our assets
    yahoo_symbols = {
        "XAUUSD": "GC=F",      # Gold futures
        "BTCUSD": "BTC-USD",
        "SPY": "SPY",
        "US30": "^DJI",        # Dow Jones index
        "NAS100": "^NDX",      # Nasdaq 100
    }
    yf_sym = yahoo_symbols.get(symbol)
    if not yf_sym:
        return None

    try:
        # Use 1-day range, 1-hour interval — gives ~24 hourly bars
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
               f"?range=1d&interval=1h")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 5:
            return None

        current = closes[-1]
        # 4h back ≈ 4 hourly bars back; 24h ≈ start of dataset
        price_4h_ago = closes[-5] if len(closes) >= 5 else closes[0]
        price_24h_ago = closes[0]

        pct_4h = ((current - price_4h_ago) / price_4h_ago) * 100
        pct_24h = ((current - price_24h_ago) / price_24h_ago) * 100

        # Format with appropriate decimals depending on instrument
        if symbol in ("BTCUSD",):
            price_str = f"${current:,.0f}"
        elif symbol in ("XAUUSD",):
            price_str = f"${current:,.2f}"
        else:
            price_str = f"{current:,.2f}"

        return (f"Current: {price_str}  ·  4h change: {pct_4h:+.2f}%  ·  "
                f"24h change: {pct_24h:+.2f}%")
    except Exception as e:
        print(f"  ⚠ price fetch failed for {symbol}: {e}")
        return None


def read_recent_history(symbol, n=5):
    """Read the last N analyses from today's log file to build maturity context."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{symbol}_{today}.jsonl"
    if not log_file.exists():
        return None

    try:
        with open(log_file) as f:
            lines = f.readlines()[-n:]
        if not lines:
            return None
        entries = [json.loads(line) for line in lines]
        summary_lines = []
        for entry in entries:
            ts = entry.get("timestamp", "")[:16].replace("T", " ")
            scr = entry.get("screening", {})
            conf_entry = entry.get("confirmation")
            if conf_entry:
                summary_lines.append(
                    f"  {ts}  Haiku: {scr.get('overall_bias')}{scr.get('confidence')}/10 → "
                    f"Sonnet: {conf_entry.get('overall_bias')}{conf_entry.get('confidence')}/10"
                )
            else:
                summary_lines.append(
                    f"  {ts}  Haiku: {scr.get('overall_bias')}{scr.get('confidence')}/10 (no confirmation triggered)"
                )
        return "\n".join(summary_lines)
    except Exception as e:
        print(f"  ⚠ history read failed: {e}")
        return None


def compute_maturity(symbol, current_bias, n=3):
    """
    How many of the last N cycles had the same bias as current?
    Returns (matched_count, total_count, label).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{symbol}_{today}.jsonl"
    if not log_file.exists():
        return 0, 0, "FRESH"

    try:
        with open(log_file) as f:
            lines = f.readlines()[-n:]
        if not lines:
            return 0, 0, "FRESH"
        entries = [json.loads(line) for line in lines]
        matched = 0
        for entry in entries:
            primary = entry.get("confirmation") or entry.get("screening")
            if primary and primary.get("overall_bias") == current_bias:
                matched += 1
        total = len(entries)
        if matched == total and total >= 2:
            return matched, total, "MATURE"
        if matched >= 2:
            return matched, total, "BUILDING"
        return matched, total, "FRESH"
    except Exception:
        return 0, 0, "FRESH"


def analyze(symbol, client, model, news, role="screening", price_context=None, maturity_context=None):
    response = client.messages.create(
        model=model,
        max_tokens=3500,
        messages=[{"role": "user", "content": build_prompt(symbol, news, role, price_context, maturity_context)}],
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

def decide_alert(screening, confirmation, prev_state, maturity_label, maturity_count):
    """
    Returns (should_alert: bool, alert_tier: str, reason: str)

    alert_tier ∈ {"HIGH", "MODERATE", "FLIP", "WATCH", "NONE"}

    NEW LOGIC:
    - HIGH requires both models agreeing on direction at ≥7 AND maturity (2+ matching cycles)
    - First-cycle high-confidence reads become WATCH alerts (informational, not actionable)
    - FLIP still fires immediately on bias change (these are inherently fresh)
    """
    # Skip if Haiku didn't flag anything in the first place
    if confirmation is None:
        if not prev_state:
            return False, "NONE", "first run, screening only, below threshold"
        if prev_state.get("bias") != screening["overall_bias"] and screening["confidence"] >= 5:
            return True, "FLIP", f"bias flip {prev_state.get('bias')} → {screening['overall_bias']} (screening only)"
        return False, "NONE", "no alert from screening"

    s_bias, s_conf = screening["overall_bias"], screening["confidence"]
    c_bias, c_conf = confirmation["overall_bias"], confirmation["confidence"]

    # Direction disagreement → silent
    if s_bias != c_bias:
        return False, "NONE", f"DISAGREEMENT: Haiku={s_bias} vs Sonnet={c_bias} — silent log"

    # Bias flip from previous state → always alert (these are fresh by nature)
    if prev_state and prev_state.get("bias") != c_bias:
        if c_conf >= 7:
            return True, "FLIP", f"FLIP + CONFIRMED: {prev_state.get('bias')} → {c_bias}, both models conf ≥7"
        if c_conf >= 5:
            return True, "FLIP", f"flip {prev_state.get('bias')} → {c_bias} at moderate conviction {c_conf}/10"

    # Both agree at high conviction (≥7)
    if c_conf >= 7:
        # Apply maturity gate — needs 2+ matching cycles before becoming HIGH
        if maturity_label == "MATURE":
            # Only re-alert if confidence stepped up or we haven't alerted yet
            if not prev_state or prev_state.get("confidence", 0) < 7 or prev_state.get("last_tier") not in ("HIGH", "FLIP"):
                return True, "HIGH", f"MATURE high conviction ({maturity_count} matching cycles)"
            return False, "NONE", "HIGH already established, no change"
        elif maturity_label == "BUILDING":
            # Second cycle of agreement — promote from WATCH to MODERATE
            if not prev_state or prev_state.get("last_tier") != "MODERATE":
                return True, "MODERATE", f"building conviction ({maturity_count} matching cycles, not yet mature)"
            return False, "NONE", "MODERATE already sent at this level"
        else:  # FRESH — first cycle at high conviction
            if not prev_state or prev_state.get("last_tier") != "WATCH":
                return True, "WATCH", f"FRESH high-conf read ({c_conf}/10) — informational only, needs confirmation"
            return False, "NONE", "WATCH already sent"

    # Moderate conviction (5-6) — softer alert
    if c_conf >= 5:
        if not prev_state or prev_state.get("bias") != c_bias:
            return True, "MODERATE", f"watch-level: Haiku {s_conf}/10, Sonnet {c_conf}/10"
        return False, "NONE", "moderate already known"

    return False, "NONE", f"Sonnet downgraded to {c_conf}/10 — no alert"


# ────────────────────────────────────────────────────────────────────
# TELEGRAM FORMATTING
# ────────────────────────────────────────────────────────────────────

TIER_HEADERS = {
    "HIGH": "🎯 *HIGH CONVICTION — MATURE*",
    "FLIP": "⚡ *BIAS FLIP — CONFIRMED*",
    "MODERATE": "👀 *BUILDING (watch)*",
    "WATCH": "📡 *FRESH READ (informational only)*",
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


def format_message(screening, confirmation, tier, price_context=None, maturity_label=None, maturity_count=None):
    """Build the Telegram message with new phase + maturity info."""
    primary = confirmation if confirmation else screening
    bias_emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "🟡"}
    sig_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}

    header = TIER_HEADERS.get(tier, "📊 *SIGNAL*")
    msg = f"{header}\n"
    msg += f"{bias_emoji.get(primary['overall_bias'], '⚪')} *{primary['symbol']}: {primary['overall_bias']}* `{primary['confidence']}/10`"

    if confirmation:
        msg += f"  _(Haiku {screening['confidence']}/10 → Sonnet {confirmation['confidence']}/10)_"
    msg += "\n"

    # Phase + maturity tags
    phase = primary.get("phase", "UNKNOWN")
    phase_tags = {
        "PHASE_1": "🟢 Phase 1 (fresh, move not started)",
        "PHASE_2": "🟡 Phase 2 (move underway — wait for retest)",
        "PHASE_3": "🔴 Phase 3 (move extended — late to chase)",
    }
    if phase in phase_tags:
        msg += f"_{phase_tags[phase]}_\n"

    if maturity_label and maturity_count is not None:
        maturity_tags = {
            "MATURE": f"✅ Maturity: {maturity_count}/3 cycles confirmed",
            "BUILDING": f"⏳ Maturity: {maturity_count}/3 cycles (building)",
            "FRESH": f"⚠️ Maturity: {maturity_count}/3 cycles (fresh — caution)",
        }
        if maturity_label in maturity_tags:
            msg += f"_{maturity_tags[maturity_label]}_\n"

    if price_context:
        msg += f"_{price_context}_\n"

    msg += f"\n{primary.get('narrative', '')}\n\n"

    # Entry guidance — the key actionable line
    entry_guidance = primary.get("entry_guidance", "")
    if entry_guidance:
        msg += f"📌 *Entry guidance:* _{entry_guidance}_\n\n"

    msg += "*Factors:*\n"
    for f in primary.get("factors", [])[:8]:
        summary = _smart_truncate(f.get("summary", ""), 220)
        msg += f"{sig_emoji.get(f['signal'], '⚪')} *{f['name']}:* {summary}\n"

    levels = primary.get("key_levels", {})
    msg += f"\n📍 S `{levels.get('support', '?')}` · R `{levels.get('resistance', '?')}`\n"
    msg += f"\n⚠️ _{_smart_truncate(primary.get('risk_warning', ''), 350)}_"

    # Final tier-specific footer
    if tier == "WATCH":
        msg += "\n\n_⏸ This is a FRESH read. Wait for next cycle to confirm before acting._"
    elif tier == "MODERATE":
        msg += "\n\n_⏳ Building conviction. Look for setup but don't force entry._"
    elif tier == "HIGH":
        msg += "\n\n_✅ Mature, multi-cycle confirmed. Highest-quality alert tier._"

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

            # NEW: fetch price action context (free via Yahoo Finance)
            price_context = fetch_price_action(symbol)
            if price_context:
                print(f"  → Price: {price_context}")

            # NEW: read recent history for bias maturity assessment
            history_context = read_recent_history(symbol, n=3)

            # STAGE 1 — Haiku
            screening = analyze(
                symbol, client, SCREENING_MODEL, news, "screening",
                price_context=price_context, maturity_context=history_context,
            )
            s_bias, s_conf = screening["overall_bias"], screening["confidence"]
            colors = {"BUY": "\033[92m", "SELL": "\033[91m", "NEUTRAL": "\033[93m"}
            phase_str = screening.get("phase", "UNKNOWN")
            print(f"  [Haiku]   {colors.get(s_bias, '')}{s_bias}\033[0m {s_conf}/10  @ {screening.get('current_price', '?')}  ({phase_str})")

            # Compute maturity based on history + this bias
            mat_count, mat_total, mat_label = compute_maturity(symbol, s_bias, n=3)
            print(f"  Maturity: {mat_label} ({mat_count}/{mat_total} matching cycles)")

            # STAGE 2 — Sonnet (only if Haiku tripped the threshold)
            confirmation = None
            if s_conf >= SCREENING_THRESHOLD:
                print(f"  → Haiku flagged ≥{SCREENING_THRESHOLD}, calling Sonnet to confirm…")
                confirmation = analyze(
                    symbol, client, CONFIRMATION_MODEL, news, "confirmation",
                    price_context=price_context, maturity_context=history_context,
                )
                c_bias, c_conf = confirmation["overall_bias"], confirmation["confidence"]
                agree = "✓ agree" if c_bias == s_bias else "✗ DISAGREE"
                print(f"  [Sonnet]  {colors.get(c_bias, '')}{c_bias}\033[0m {c_conf}/10  ({agree})")
                # Recompute maturity using Sonnet's bias (the more trusted one)
                mat_count, mat_total, mat_label = compute_maturity(symbol, c_bias, n=3)

            # Decide whether to alert
            prev = state.get(symbol)
            alert, tier, reason = decide_alert(screening, confirmation, prev, mat_label, mat_count)
            print(f"  Alert: {alert} [{tier}] — {reason}")

            # Log everything regardless
            save_log({
                "timestamp": screening["timestamp"],
                "screening": screening,
                "confirmation": confirmation,
                "alert": alert,
                "tier": tier,
                "reason": reason,
                "maturity": {"label": mat_label, "count": mat_count, "total": mat_total},
                "price_context": price_context,
            }, symbol)

            # Send Telegram only on alert
            if alert and tg_token and tg_chat:
                msg = format_message(screening, confirmation, tier, price_context, mat_label, mat_count)
                sent = send_telegram(msg, tg_token, tg_chat)
                print(f"  Telegram: {'✓ sent' if sent else '✗ failed'}")

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
