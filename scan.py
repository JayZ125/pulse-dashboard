#!/usr/bin/env python3
"""us-tech-pulse scan engine.

Pulls yfinance intraday data for Top 20 XLK + custom tickers,
scores momentum/breakout/reversal/catalyst (30/30/20/20),
picks top 2-3, writes to file + sends macOS notification.

Invoked by:
- launchd -> ~/bin/us-tech-pulse-runner.sh -> this script (headless, every 15 min)
- /us-pulse interactive skill -> this script (manual, on-demand)

Exit codes:
  0 = success or skipped (out of hours, no error)
  1 = data error
  2 = notify error (non-fatal, still returns 0 from main flow but logged)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, time

import pandas as pd
import pytz
import yfinance as yf

# === Paths ===
STATE_FILE = os.path.expanduser("~/Documents/us-stock/state.json")
OUTPUT_DIR = os.path.expanduser("~/Documents/us-stock/pulse")
LOG_FILE = "/tmp/us-tech-pulse.log"
LATEST_FILE = os.path.join(OUTPUT_DIR, "latest.md")
DESKTOP_HTML = os.path.expanduser("~/Desktop/Pulse-Latest.html")

# === Defaults (used if state.json missing) ===
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AVGO", "META",
    "GOOGL", "GOOG", "TSLA", "CSCO", "CRM",
    "ORCL", "ADBE", "AMD", "IBM", "INTU",
    "TXN", "QCOM", "NOW", "INTC", "AMAT",
]
DEFAULT_WEIGHTS = {
    "momentum": 0.30,
    "breakout": 0.30,
    "reversal": 0.20,
    "catalyst": 0.20,
}

ET = pytz.timezone("US/Eastern")


def log(msg):
    """Append to log file with timestamp."""
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def is_market_hours():
    """Return True if current ET time is within US market hours (9:30-16:00 ET, Mon-Fri)."""
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    market_open = time(9, 30)
    market_close = time(16, 0)
    return market_open <= now_et.time() <= market_close


def load_state():
    """Load state.json with tickers + weights. Falls back to defaults."""
    if not os.path.exists(STATE_FILE):
        return list(DEFAULT_TICKERS), dict(DEFAULT_WEIGHTS)
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        tickers = s.get("tickers", DEFAULT_TICKERS)
        weights = s.get("weights", DEFAULT_WEIGHTS)
        # Normalize weights to sum to 1
        wsum = sum(weights.values()) or 1
        weights = {k: v / wsum for k, v in weights.items()}
        return tickers, weights
    except Exception as e:
        log(f"state.json read error: {e}, using defaults")
        return list(DEFAULT_TICKERS), dict(DEFAULT_WEIGHTS)


def pull_one(ticker):
    """Pull 5-day 15-min bars for a single ticker. Returns DataFrame or None.

    Uses yf.Ticker().history() instead of yf.download() to avoid MultiIndex
    column ambiguity in yfinance 1.4+ — returns a clean flat-column DataFrame.
    """
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="5d", interval="15m", auto_adjust=True)
        if df is None or df.empty:
            return None
        return df.dropna()
    except Exception as e:
        log(f"  {ticker}: pull error: {e}")
        return None


def score_ticker(df, ticker):
    """Score a single ticker's 15-min bars on 4 dimensions. Returns dict or None."""
    if df is None or len(df) < 16:
        return None

    try:
        current = float(df["Close"].iloc[-1])

        # === Momentum (0-100) ===
        chg_15m = 0.0
        chg_1h = 0.0
        chg_4h = 0.0
        if len(df) >= 2:
            chg_15m = (df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100
        if len(df) >= 5:
            chg_1h = (df["Close"].iloc[-1] / df["Close"].iloc[-5] - 1) * 100
        if len(df) >= 17:
            chg_4h = (df["Close"].iloc[-1] / df["Close"].iloc[-17] - 1) * 100

        vol_ratio = 1.0
        if "Volume" in df.columns and len(df) >= 20:
            v_now = float(df["Volume"].iloc[-1])
            v_avg = float(df["Volume"].iloc[-20:].mean())
            if v_avg > 0:
                vol_ratio = v_now / v_avg

        # Composite momentum: weighted changes scaled by volume
        mom_raw = (chg_15m * 2.0 + chg_1h * 1.0 + chg_4h * 0.5) * (1.0 + (vol_ratio - 1.0) * 0.3)
        mom_score = max(0.0, min(100.0, 50.0 + mom_raw * 5.0))

        # === Breakout (0-100) ===
        # Restrict to today's session
        today_date = df.index[-1].date()
        today = df[df.index.date == today_date]
        if today.empty:
            today = df

        day_high = float(today["High"].max())
        day_low = float(today["Low"].min())
        day_open = float(today["Open"].iloc[0])
        day_range = day_high - day_low

        position_in_range = ((current - day_low) / day_range * 100.0) if day_range > 0 else 50.0
        pct_above_open = (current / day_open - 1.0) * 100.0 if day_open > 0 else 0.0

        five_day_high = float(df["High"].max())
        near_5d_high_pct = (current / five_day_high * 100.0) if five_day_high > 0 else 50.0

        breakout_score = (
            position_in_range * 0.4 +
            max(0.0, min(100.0, 50.0 + pct_above_open * 10.0)) * 0.3 +
            max(0.0, min(100.0, near_5d_high_pct)) * 0.3
        )

        # === Reversal (0-100) ===
        # RSI(14) computed on close
        rsi = 50.0
        if len(df) >= 15:
            delta = df["Close"].diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            avg_loss = float(loss.iloc[-1])
            if pd.isna(avg_loss) or avg_loss == 0:
                rsi = 100.0
            else:
                rs = float(gain.iloc[-1]) / avg_loss
                rsi = 100.0 - (100.0 / (1.0 + rs))

        # Oversold bounce scoring
        if rsi < 25:
            rev_score = 90.0
        elif rsi < 35:
            rev_score = 80.0
        elif rsi < 45:
            rev_score = 60.0
        elif rsi < 55:
            rev_score = 40.0
        elif rsi < 70:
            rev_score = 20.0
        else:
            rev_score = 10.0

        # Bonus: bouncing off day's low (0.3-2% off low)
        if day_low > 0:
            pct_off_low = (current / day_low - 1.0) * 100.0
            if 0.3 < pct_off_low < 2.0:
                rev_score = min(100.0, rev_score + 10.0)

        # === Catalyst (0-100) — v1 placeholder ===
        # TODO: integrate Finnhub news once API key is wired
        catalyst_score = 50.0

        return {
            "ticker": ticker,
            "current": round(current, 2),
            "open": round(day_open, 2),
            "chg_15m_pct": round(chg_15m, 2),
            "chg_1h_pct": round(chg_1h, 2),
            "chg_4h_pct": round(chg_4h, 2),
            "vol_ratio": round(vol_ratio, 2),
            "rsi": round(rsi, 1),
            "scores": {
                "momentum": round(mom_score, 1),
                "breakout": round(breakout_score, 1),
                "reversal": round(rev_score, 1),
                "catalyst": round(catalyst_score, 1),
            },
        }
    except Exception as e:
        log(f"  {ticker}: score error: {e}")
        return None


def send_notification(top_results, timestamp_str, latest_file):
    """Send macOS notification. Click opens latest_file with full details.

    Uses terminal-notifier if available (clickable, opens file/url).
    Falls back to osascript (non-clickable, banner text only).
    """
    if not top_results:
        return
    title = f"🔔 US Tech Pulse · {timestamp_str}"
    lines = []
    for r in top_results:
        chg_open = (r["current"] / r["open"] - 1) * 100 if r.get("open") else 0
        sign = "+" if chg_open >= 0 else ""
        sig_emoji = r.get("signal", "⚪")
        sig_short = sig_emoji.split()[-1] if sig_emoji else ""
        reason_short = r.get("reason", "")[:30]
        lines.append(
            f"{sig_emoji}{r['ticker']} {r['final_score']} ${r['current']} "
            f"({sign}{chg_open:.1f}% RSI {r['rsi']}) {reason_short}"
        )
    body = "\n".join(lines)

    # Try terminal-notifier first (clickable, runs shell command on click)
    try:
        result = subprocess.run(
            [
                "terminal-notifier",
                "-title", title,
                "-message", body,
                "-group", "us-tech-pulse",
                "-execute", f"open '{latest_file}'",
                "-sender", "com.apple.Terminal",
            ],
            check=True, timeout=10, capture_output=True, text=True,
        )
        log(f"notification sent (terminal-notifier, clickable): {[r['ticker'] for r in top_results]}")
        return
    except FileNotFoundError:
        log("terminal-notifier not found, falling back to osascript")
    except subprocess.CalledProcessError as e:
        log(f"terminal-notifier error: {e.stderr or e}, falling back to osascript")

    # Fallback: osascript (banner-only, click opens Script Editor empty)
    safe_body = body.replace('"', "'").replace("\n", " ")
    script = f'display notification "{safe_body}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
        log(f"notification sent (osascript fallback): {[r['ticker'] for r in top_results]}")
    except Exception as e:
        log(f"notification error (both methods failed): {e}")


def get_signal(final, rsi, vol, chg_open_pct):
    """Rule-based buy signal + reason. Returns (signal_emoji, reason_text)."""
    notes = []
    if final >= 70 and rsi < 75 and vol >= 1.5:
        sig = "🟢 BUY"
        notes.append("强势突破+量能")
    elif final >= 65 and rsi >= 75:
        sig = "🟡 WATCH"
        notes.append(f"分高但 RSI {rsi} 超买")
    elif final >= 60 and vol >= 1.2:
        sig = "🟡 WATCH"
        notes.append("动量合格, 等回踩")
    elif final >= 45:
        sig = "⚪ HOLD"
        notes.append("中性, 不入场")
    else:
        sig = "⚪ SKIP"
        notes.append("不符合入场")
    if chg_open_pct >= 3.0:
        notes.append("已涨3%+追高风险")
    elif chg_open_pct <= -2.0:
        notes.append("已跌2%+留意支撑")
    return sig, " / ".join(notes)


def write_web_index(all_results, full_ts, is_premarket):
    """Write the dashboard index.html for GitHub Pages (web mode only).

    Sets PULSE_WEB=1 to invoke. Skips macOS notification + desktop HTML.
    """
    warn = ""
    if is_premarket:
        warn = ('<div class="warn">⚠️ Pre-market data: Open/Now from most recent trading day. '
                'Live intraday data resumes at next market open (9:30 AM ET).</div>')

    picks_html = "".join(_render_pick_html(r) for r in all_results[:3])
    all_rows = "".join(_render_ranking_row(r, i + 1) for i, r in enumerate(all_results))
    ranking_html = f"""
<h2>📊 Full ranking — all {len(all_results)} tickers (by score, desc)</h2>
<table>
  <thead>
    <tr>
      <th>#</th><th>Ticker</th><th>Score</th><th>Signal</th>
      <th>Now</th><th>ΔDay</th><th>RSI</th><th>Vol</th>
    </tr>
  </thead>
  <tbody>{all_rows}</tbody>
</table>
"""
    nav = ('<div style="text-align: right; margin-bottom: 12px;">'
           '<a href="deepdive.html" style="color: #4af; text-decoration: none; '
           'background: rgba(74,170,255,0.1); padding: 6px 12px; border-radius: 4px; '
           'font-size: 14px;">🔍 Single-stock deep-dive tool →</a></div>')
    html = (DESKTOP_HTML_TEMPLATE
            .replace("<h1>🔔 US Tech Pulse</h1>", nav + "<h1>🔔 US Tech Pulse</h1>")
            .replace("{timestamp}", full_ts)
            .replace("{warn}", warn)
            .replace("{picks}", picks_html)
            .replace("{ranking}", ranking_html))
    try:
        with open("index.html", "w") as f:
            f.write(html)
        log(f"wrote index.html (web, {len(all_results)} tickers)")
    except Exception as e:
        log(f"web index write error: {e}")


def main():
    is_web = bool(os.environ.get("PULSE_WEB"))
    timestamp_str = datetime.now(ET).strftime("%H:%M ET")
    full_ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    log(f"scan started (TS={timestamp_str}, web={is_web})")

    if not is_market_hours() and not os.environ.get("FORCE_SCAN"):
        log("skipped: outside US market hours (set FORCE_SCAN=1 to bypass)")
        return 0

    tickers, weights = load_state()
    log(f"scanning {len(tickers)} tickers")

    results = []
    for ticker in tickers:
        df = pull_one(ticker)
        if df is None:
            continue
        scored = score_ticker(df, ticker)
        if scored is None:
            continue
        final = sum(scored["scores"][k] * weights[k] for k in weights)
        scored["final_score"] = round(final, 1)
        chg_open_pct = (scored["current"] / scored["open"] - 1) * 100 if scored.get("open") else 0
        sig, reason = get_signal(scored["final_score"], scored["rsi"], scored["vol_ratio"], chg_open_pct)
        scored["signal"] = sig
        scored["reason"] = reason
        scored["chg_open_pct"] = round(chg_open_pct, 2)
        results.append(scored)

    if not results:
        log("no data returned for any ticker")
        if is_web:
            try:
                with open("index.html", "w") as f:
                    f.write(
                        f"<!DOCTYPE html><html><body style=\"font-family:sans-serif;"
                        f"background:linear-gradient(135deg,#0a0a0a 0%,#1a1a2e 100%);"
                        f"color:#eee;padding:24px;min-height:100vh;\">"
                        f"<h1>🔔 US Tech Pulse</h1>"
                        f"<p>No data at {full_ts}. Will retry next scheduled run.</p>"
                        f"<p><a href=\"deepdive.html\" style=\"color:#4af;\">→ Single-stock deep-dive tool</a></p>"
                        f"</body></html>")
                log("wrote empty web index.html (no data)")
            except Exception as e:
                log(f"web empty index write error: {e}")
        return 1

    results.sort(key=lambda r: r["final_score"], reverse=True)
    top = results[:3]

    # === Web-only path: write index.html, skip local outputs + notification ===
    if is_web:
        write_web_index(results, full_ts, not is_market_hours())
        log(f"top picks (web): {[(r['ticker'], r['final_score']) for r in top]}")
        log("scan completed (web mode)")
        return 0

    # === Local path: build markdown report ===
    lines = [f"\n## {full_ts}\n"]
    if not is_market_hours():
        lines.append("> ⚠️ 当前盘前, Open/Now 来自最近一个完整交易日 (昨). 开盘后自动切到今日数据.\n")
    lines.append("| # | Ticker | Score | Signal | Open | Now | ΔDay | Mom | Brk | Rev | RSI | 备注 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(top, 1):
        s = r["scores"]
        chg_open = r.get("chg_open_pct", 0)
        sign = "+" if chg_open >= 0 else ""
        lines.append(
            f"| {i} | **{r['ticker']}** | {r['final_score']} | "
            f"{r['signal']} | ${r['open']} | ${r['current']} | "
            f"{sign}{chg_open:.2f}% | "
            f"{s['momentum']} | {s['breakout']} | {s['reversal']} | {r['rsi']} | "
            f"{r['reason']} |"
        )

    lines.append("\n### 触发详情\n")
    for r in top:
        s = r["scores"]
        details = []
        if s["momentum"] >= 70:
            details.append(f"动量 {s['momentum']} (15m {r['chg_15m_pct']:+.2f}%, 量比 {r['vol_ratio']}x)")
        if s["breakout"] >= 70:
            details.append(f"突破 {s['breakout']} (近5日高 ${r['current']})")
        if s["reversal"] >= 70:
            details.append(f"反转 {s['reversal']} (RSI {r['rsi']})")
        if not details:
            details.append(f"综合 {r['final_score']}")
        lines.append(f"- **{r['ticker']}** {r['signal']}: " + " / ".join(details))

    report = "\n".join(lines) + "\n"

    # Write to file (append, create if needed)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    out_file = os.path.join(OUTPUT_DIR, f"{date_str}.md")
    if not os.path.exists(out_file):
        with open(out_file, "w") as f:
            f.write(f"# US Tech Pulse — {date_str}\n\n"
                    f"_Generated every 15 min during US market hours. "
                    f"Not investment advice. 基于规则: 动量30/突破30/反转20/催化20._\n")
    with open(out_file, "a") as f:
        f.write(report)

    log(f"wrote {len(report)} chars to {out_file}")
    log(f"top picks: {[(r['ticker'], r['final_score']) for r in top]}")

    # Also write the latest entry to a separate file (overwrite) for click-to-details
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    latest_header = (
        f"# US Tech Pulse · Latest\n\n"
        f"_每次扫描覆盖. 点击通知 banner 即打开本文件, 显示当次扫描详情._\n\n"
    )
    with open(LATEST_FILE, "w") as f:
        f.write(latest_header + report)
    log(f"wrote latest to {LATEST_FILE}")

    # Write the desktop HTML (overwrite) for at-a-glance viewing
    write_desktop_html(results, full_ts, not is_market_hours())

    # Send notification
    send_notification(top, timestamp_str, LATEST_FILE)
    log("scan completed")
    return 0


DESKTOP_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>US Tech Pulse</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 100%);
  color: #eee; padding: 24px; max-width: 720px; margin: 0 auto; min-height: 100vh;
}
h1 { color: #fff; font-size: 28px; margin: 0 0 8px; }
.ts { color: #888; font-size: 14px; margin-bottom: 16px; }
.warn {
  background: rgba(255, 200, 50, 0.1);
  border: 1px solid rgba(255, 200, 50, 0.3);
  border-radius: 6px;
  padding: 8px 12px; color: #fc6; font-size: 13px; margin-bottom: 16px;
}
.pick {
  background: rgba(255,255,255,0.04);
  border-left: 4px solid #4af;
  border-radius: 6px; padding: 14px 18px; margin: 10px 0;
  display: flex; justify-content: space-between; align-items: center;
  gap: 12px;
}
.pick-buy { border-left-color: #4f4; }
.pick-watch { border-left-color: #fa4; }
.pick-skip { border-left-color: #888; }
.ticker { font-size: 28px; font-weight: 700; color: #fff; }
.signal {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 12px; margin-left: 8px; font-weight: 600;
}
.signal-buy { background: #4f4; color: #000; }
.signal-watch { background: #fa4; color: #000; }
.signal-skip { background: #555; color: #fff; }
.score { font-size: 32px; font-weight: 800; color: #4af; min-width: 60px; text-align: right; }
.note { color: #aaa; font-size: 13px; margin-top: 6px; }
.detail { margin-top: 8px; }
.detail span {
  display: inline-block; background: rgba(255,255,255,0.06);
  padding: 3px 8px; border-radius: 3px; margin-right: 6px; margin-bottom: 4px;
  font-size: 12px; color: #ccc;
}
.chg-up { color: #4f4; }
.chg-down { color: #f44; }
h2 {
  color: #ccc; font-size: 16px; margin: 32px 0 12px;
  border-bottom: 1px solid #333; padding-bottom: 6px;
}
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #222; }
th { color: #888; font-weight: normal; font-size: 12px; background: rgba(255,255,255,0.03); }
tbody tr:hover { background: rgba(255,255,255,0.04); }
td.shrink { white-space: nowrap; }
.footer {
  color: #666; font-size: 11px; margin-top: 32px; text-align: center;
  border-top: 1px solid #333; padding-top: 16px;
}
</style>
</head>
<body>
<h1>🔔 US Tech Pulse</h1>
<div class="ts">{timestamp}</div>
{warn}
{picks}
{ranking}
<div class="footer">
基于规则: 动量30 / 突破30 / 反转20 / 催化20 · 覆盖 34 只 XLK + 自选股 · 非投资建议
</div>
</body>
</html>
"""


def _render_pick_html(r):
    """Render one pick as HTML card."""
    chg_open = (r["current"] / r["open"] - 1) * 100 if r.get("open") else 0
    sign = "+" if chg_open >= 0 else ""
    signal = r.get("signal", "⚪ SKIP")
    if "BUY" in signal:
        pick_cls, sig_cls = "pick-buy", "signal-buy"
    elif "WATCH" in signal:
        pick_cls, sig_cls = "pick-watch", "signal-watch"
    else:
        pick_cls, sig_cls = "pick-skip", "signal-skip"
    s = r["scores"]
    chg_cls = "chg-up" if chg_open >= 0 else "chg-down"
    return f"""
<div class="pick {pick_cls}">
  <div style="flex:1;">
    <div><span class="ticker">{r['ticker']}</span><span class="signal {sig_cls}">{signal}</span></div>
    <div class="note">{r.get('reason', '')}</div>
    <div class="detail">
      <span>${r['current']}</span>
      <span class="{chg_cls}">{sign}{chg_open:.2f}%</span>
      <span>开 ${r['open']}</span>
      <span>RSI {r['rsi']}</span>
      <span>动量 {s['momentum']}</span>
      <span>突破 {s['breakout']}</span>
      <span>反转 {s['reversal']}</span>
    </div>
  </div>
  <div class="score">{r['final_score']}</div>
</div>
"""


def _render_ranking_row(r, rank):
    """Render one stock as a compact table row for the full ranking."""
    chg_open = (r["current"] / r["open"] - 1) * 100 if r.get("open") else 0
    sign = "+" if chg_open >= 0 else ""
    chg_cls = "chg-up" if chg_open >= 0 else "chg-down"
    sig = r.get("signal", "⚪ SKIP")
    return f"""
<tr>
  <td class="shrink">{rank}</td>
  <td class="shrink"><strong>{r['ticker']}</strong></td>
  <td class="shrink">{r['final_score']}</td>
  <td class="shrink">{sig}</td>
  <td class="shrink">${r['current']}</td>
  <td class="shrink {chg_cls}">{sign}{chg_open:.2f}%</td>
  <td class="shrink">{r['rsi']}</td>
  <td class="shrink">{r['vol_ratio']}x</td>
</tr>
"""


def write_desktop_html(all_results, full_ts, is_premarket):
    """Write the latest picks to ~/Desktop/Pulse-Latest.html (overwrite).

    Shows top 3 as cards, then a full ranking table of all tickers.
    On first run, opens the file in default browser. 15-min meta-refresh
    keeps the open tab up to date.
    """
    warn = ""
    if is_premarket:
        warn = ('<div class="warn">⚠️ 盘前数据, Open/Now 来自最近一个完整交易日 (昨). '
                '开盘后自动切到今日.</div>')

    top_3 = all_results[:3]
    picks_html = "".join(_render_pick_html(r) for r in top_3)

    all_rows = "".join(_render_ranking_row(r, i + 1) for i, r in enumerate(all_results))
    ranking_html = f"""
<h2>📊 全 {len(all_results)} 只排名 (按分数降序)</h2>
<table>
  <thead>
    <tr>
      <th>#</th><th>Ticker</th><th>Score</th><th>Signal</th>
      <th>Now</th><th>ΔDay</th><th>RSI</th><th>Vol</th>
    </tr>
  </thead>
  <tbody>{all_rows}</tbody>
</table>
"""

    # Use replace() instead of .format() to avoid conflict with CSS curly braces
    html = (DESKTOP_HTML_TEMPLATE
            .replace("{timestamp}", full_ts)
            .replace("{warn}", warn)
            .replace("{picks}", picks_html)
            .replace("{ranking}", ranking_html))

    is_first_run = not os.path.exists(DESKTOP_HTML)
    try:
        with open(DESKTOP_HTML, "w") as f:
            f.write(html)
        log(f"wrote desktop HTML to {DESKTOP_HTML} (top 3 + {len(all_results)} ranked)")
        if is_first_run:
            subprocess.run(["open", DESKTOP_HTML], check=True, timeout=5)
            log(f"opened {DESKTOP_HTML} in default browser (first run)")
    except Exception as e:
        log(f"desktop HTML error: {e}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)
