"""
Backtest Analyser — reads your existing signal_logs/ folder and simulates
what would have happened if every MATURE / FLIP alert had been executed
mechanically at the bot's specified entry, stop, and TP levels.

Outputs a clean report:
- Win rate by alert tier
- Win rate by symbol
- Average R:R per trade
- P&L curve
- Maximum drawdown
- Which alert tiers/symbols are worth trading live

USAGE:
    python backtest_analyser.py [--days N]

By default analyses all logs in signal_logs/.
Use --days 30 to look only at the last 30 days.

REQUIREMENTS:
    pip install requests
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

LOG_DIR = Path("signal_logs")

# Yahoo Finance proxy symbols for price lookups
YAHOO_SYMBOLS = {
    "XAUUSD": "GC=F",
    "BTCUSD": "BTC-USD",
    "SPY":    "SPY",
    "US30":   "^DJI",
    "NAS100": "^NDX",
}

# Tiers we'd actually paper-trade in a real workflow
ACTIONABLE_TIERS = {"HIGH", "FLIP", "MODERATE"}


def parse_price_level(text):
    """
    Extract a numeric price from strings like "$4,650", "$29,250-$29,300",
    "29,250-29,300", "around $50,103". Returns a tuple (low, high) or None.
    """
    if not text:
        return None
    # Strip non-numeric noise but keep digits, dots, dashes, commas
    cleaned = text.replace("$", "").replace(",", "").replace(" ", "")
    # Look for a range like 29250-29300
    range_match = re.search(r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)", cleaned)
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))
    # Single number
    single_match = re.search(r"(\d+\.?\d*)", cleaned)
    if single_match:
        v = float(single_match.group(1))
        return v, v
    return None


def fetch_price_history(symbol, start_date, end_date):
    """
    Fetch hourly bars between two dates from Yahoo Finance.
    Returns list of (timestamp, open, high, low, close) tuples.
    """
    yf_sym = YAHOO_SYMBOLS.get(symbol)
    if not yf_sym:
        return []

    period1 = int(start_date.timestamp())
    period2 = int(end_date.timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
           f"?period1={period1}&period2={period2}&interval=1h")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        result = data["chart"]["result"][0]
        ts = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(ts):
            o = quote["open"][i]
            h = quote["high"][i]
            l = quote["low"][i]
            c = quote["close"][i]
            if None in (o, h, l, c):
                continue
            bars.append((datetime.fromtimestamp(t, tz=timezone.utc), o, h, l, c))
        return bars
    except Exception as e:
        print(f"  ⚠ price fetch failed for {symbol}: {e}")
        return []


def simulate_trade(direction, entry, stop, tp1, tp2, bars):
    """
    Given hourly bars AFTER entry, determine outcome:
    - "TP2_HIT" if price reached tp2
    - "TP1_HIT" if price reached tp1 but not tp2
    - "STOP_HIT" if stop hit first
    - "OPEN" if never hit either (still running at end of data)

    For LONG: stop is below, tp above. For SHORT: stop above, tp below.
    """
    if not bars:
        return {"outcome": "NO_DATA", "exit_price": entry, "bars_held": 0}

    for i, (ts, o, h, l, c) in enumerate(bars):
        if direction == "BUY":
            if l <= stop:
                return {"outcome": "STOP_HIT", "exit_price": stop, "bars_held": i + 1, "exit_time": ts}
            if tp2 and h >= tp2:
                return {"outcome": "TP2_HIT", "exit_price": tp2, "bars_held": i + 1, "exit_time": ts}
            if tp1 and h >= tp1:
                return {"outcome": "TP1_HIT", "exit_price": tp1, "bars_held": i + 1, "exit_time": ts}
        else:  # SELL
            if h >= stop:
                return {"outcome": "STOP_HIT", "exit_price": stop, "bars_held": i + 1, "exit_time": ts}
            if tp2 and l <= tp2:
                return {"outcome": "TP2_HIT", "exit_price": tp2, "bars_held": i + 1, "exit_time": ts}
            if tp1 and l <= tp1:
                return {"outcome": "TP1_HIT", "exit_price": tp1, "bars_held": i + 1, "exit_time": ts}

    # Never hit either — exit at last close
    last_close = bars[-1][4]
    return {"outcome": "OPEN", "exit_price": last_close, "bars_held": len(bars)}


def calculate_r(direction, entry, stop, exit_price):
    """Return the trade outcome in R-multiples (risk units)."""
    risk = abs(entry - stop)
    if risk == 0:
        return 0
    if direction == "BUY":
        return (exit_price - entry) / risk
    else:
        return (entry - exit_price) / risk


def extract_trade_setup(entry_record):
    """
    From a log entry, pull out direction, entry price, stop, and TP levels
    by parsing the bot's entry_guidance + key_levels.

    For backtest we use:
    - entry: midpoint of bot's specified entry zone (or current_price if not specified)
    - stop: bot's mentioned stop OR midpoint of resistance (for shorts) / support (for longs)
    - tp1: opposite key level
    """
    primary = entry_record.get("confirmation") or entry_record.get("screening")
    if not primary:
        return None

    bias = primary.get("overall_bias")
    if bias not in ("BUY", "SELL"):
        return None

    levels = primary.get("key_levels", {})
    support = parse_price_level(levels.get("support", ""))
    resistance = parse_price_level(levels.get("resistance", ""))
    current = parse_price_level(primary.get("current_price", ""))
    entry_guidance = primary.get("entry_guidance", "")

    # Try to extract a specific entry price from entry_guidance text
    guidance_levels = parse_price_level(entry_guidance)

    if bias == "BUY":
        # Long: enter at support retest, stop below support, target resistance
        if support and resistance:
            entry = (support[0] + support[1]) / 2
            stop = support[0] * 0.998  # 0.2% below support
            tp1 = (resistance[0] + resistance[1]) / 2
            tp2 = tp1 + (tp1 - entry) * 0.6  # extension
        elif current and resistance:
            entry = current[0]
            stop = current[0] * 0.985  # 1.5% stop
            tp1 = (resistance[0] + resistance[1]) / 2
            tp2 = tp1 + (tp1 - entry) * 0.6
        else:
            return None
    else:  # SELL
        if support and resistance:
            entry = (resistance[0] + resistance[1]) / 2
            stop = resistance[1] * 1.002
            tp1 = (support[0] + support[1]) / 2
            tp2 = tp1 - (entry - tp1) * 0.6
        elif current and support:
            entry = current[0]
            stop = current[0] * 1.015
            tp1 = (support[0] + support[1]) / 2
            tp2 = tp1 - (entry - tp1) * 0.6
        else:
            return None

    return {
        "direction": bias,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
    }


def load_logs(days_filter=None):
    """Load all JSONL log entries from signal_logs/. Optionally filter to last N days."""
    if not LOG_DIR.exists():
        print(f"❌ No {LOG_DIR}/ folder found. Run the bot first.")
        sys.exit(1)

    cutoff = None
    if days_filter:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_filter)

    entries_by_symbol = defaultdict(list)
    for log_file in sorted(LOG_DIR.glob("*.jsonl")):
        # filename pattern: SYMBOL_YYYY-MM-DD.jsonl
        parts = log_file.stem.split("_", 1)
        if len(parts) != 2:
            continue
        symbol = parts[0]

        with open(log_file) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                if cutoff and ts < cutoff:
                    continue
                entry["_timestamp"] = ts
                entries_by_symbol[symbol].append(entry)

    return entries_by_symbol


def run_backtest(days_filter=None):
    print(f"\n{'='*70}")
    print(f"  SIGNAL MONITOR — BACKTEST ANALYSER")
    print(f"  Run: {datetime.now().isoformat(timespec='seconds')}")
    if days_filter:
        print(f"  Window: last {days_filter} days")
    print(f"{'='*70}\n")

    entries_by_symbol = load_logs(days_filter)
    if not entries_by_symbol:
        print("No log entries found.")
        return

    # Aggregate stats
    overall_trades = []
    by_tier = defaultdict(list)
    by_symbol = defaultdict(list)

    for symbol, entries in entries_by_symbol.items():
        print(f"━━ {symbol} ━━ ({len(entries)} log entries)")

        # Filter to actionable alerts only (tier in ACTIONABLE_TIERS)
        actionable = [e for e in entries if e.get("tier") in ACTIONABLE_TIERS]
        print(f"  → {len(actionable)} actionable alerts (HIGH / FLIP / MODERATE)")

        if not actionable:
            print()
            continue

        # Determine date range for price fetch
        earliest = min(e["_timestamp"] for e in actionable)
        latest = max(e["_timestamp"] for e in actionable) + timedelta(days=7)
        print(f"  → Fetching price history {earliest.date()} to {latest.date()}...")

        bars = fetch_price_history(symbol, earliest, latest)
        if not bars:
            print(f"  ⚠ No price data — skipping {symbol}")
            print()
            continue
        print(f"  → {len(bars)} hourly bars retrieved")

        # Backtest each actionable alert
        for entry in actionable:
            setup = extract_trade_setup(entry)
            if not setup:
                continue

            entry_time = entry["_timestamp"]
            # Use bars AFTER the alert time
            future_bars = [b for b in bars if b[0] > entry_time]
            if not future_bars:
                continue

            result = simulate_trade(
                setup["direction"], setup["entry"], setup["stop"],
                setup["tp1"], setup["tp2"], future_bars,
            )
            r = calculate_r(setup["direction"], setup["entry"], setup["stop"], result["exit_price"])

            trade = {
                "symbol": symbol,
                "tier": entry["tier"],
                "time": entry_time,
                **setup,
                **result,
                "r": r,
            }
            overall_trades.append(trade)
            by_tier[entry["tier"]].append(trade)
            by_symbol[symbol].append(trade)
        print()

    # ─────────── Report ───────────
    if not overall_trades:
        print("No simulatable trades produced.")
        return

    print(f"\n{'='*70}")
    print(f"  RESULTS — {len(overall_trades)} simulated trades")
    print(f"{'='*70}\n")

    def summarise(label, trades):
        if not trades:
            return
        closed = [t for t in trades if t["outcome"] in ("STOP_HIT", "TP1_HIT", "TP2_HIT")]
        if not closed:
            print(f"  {label}: {len(trades)} trades, none closed")
            return
        wins = [t for t in closed if t["r"] > 0]
        losses = [t for t in closed if t["r"] <= 0]
        win_rate = len(wins) / len(closed) * 100
        avg_r = sum(t["r"] for t in closed) / len(closed)
        total_r = sum(t["r"] for t in closed)
        avg_win = sum(t["r"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["r"] for t in losses) / len(losses) if losses else 0
        print(f"  {label}")
        print(f"    Trades: {len(closed)} closed ({len(wins)}W / {len(losses)}L)  |  Open: {len(trades) - len(closed)}")
        print(f"    Win rate:    {win_rate:.1f}%")
        print(f"    Avg R/trade: {avg_r:+.2f}R")
        print(f"    Total R:     {total_r:+.2f}R")
        print(f"    Avg win:     {avg_win:+.2f}R  ·  Avg loss: {avg_loss:+.2f}R")
        print()

    print("──── BY TIER ────\n")
    for tier in ("HIGH", "FLIP", "MODERATE"):
        summarise(f"[{tier}]", by_tier.get(tier, []))

    print("──── BY SYMBOL ────\n")
    for sym, trades in sorted(by_symbol.items()):
        summarise(f"[{sym}]", trades)

    print("──── OVERALL ────\n")
    summarise("[ALL]", overall_trades)

    # Equity curve / max drawdown
    print("──── EQUITY CURVE (in R-multiples) ────\n")
    sorted_trades = sorted(overall_trades, key=lambda t: t["time"])
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in sorted_trades:
        if t["outcome"] not in ("STOP_HIT", "TP1_HIT", "TP2_HIT"):
            continue
        cumulative += t["r"]
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    print(f"  Final cumulative R: {cumulative:+.2f}R")
    print(f"  Peak R:             {peak:+.2f}R")
    print(f"  Max drawdown:       {max_dd:.2f}R")

    # Practical interpretation
    print(f"\n──── INTERPRETATION ────\n")
    if cumulative > 0:
        print(f"  ✓ Net positive expectancy ({cumulative:+.2f}R total)")
    else:
        print(f"  ✗ Net negative expectancy ({cumulative:+.2f}R total)")

    high_trades = by_tier.get("HIGH", [])
    if high_trades:
        high_closed = [t for t in high_trades if t["outcome"] in ("STOP_HIT", "TP1_HIT", "TP2_HIT")]
        if high_closed:
            high_wr = sum(1 for t in high_closed if t["r"] > 0) / len(high_closed) * 100
            print(f"  → HIGH tier win rate: {high_wr:.1f}% over {len(high_closed)} trades")
            if high_wr >= 55:
                print(f"    Recommendation: HIGH tier appears profitable, consider live trading at small size")
            elif high_wr >= 45:
                print(f"    Recommendation: HIGH tier marginal — need more data, continue paper-trading")
            else:
                print(f"    Recommendation: HIGH tier underperforming — investigate why before trading live")

    print()
    print(f"{'='*70}")
    print(f"  Notes:")
    print(f"  - Backtest uses bot's specified entry zones (midpoint), structural stops,")
    print(f"    and TP at opposite key level. Real trades may differ.")
    print(f"  - 'Open' trades are still running at last available price data.")
    print(f"  - This is historical signal performance, not a guarantee of future results.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    days = None
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])
    run_backtest(days_filter=days)
