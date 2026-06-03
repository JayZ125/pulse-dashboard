# US Tech Pulse

Automated US stock screener. Every 15 minutes during US market hours, scans 37 tickers
(tech, semis, energy, finance, pharma, gold, aerospace, quantum, USDC, GaN, AI test
equipment) and renders a dark-themed dashboard with top 3 picks + full ranking.

**Live dashboard**: <https://jayz125.github.io/pulse-dashboard/>
**Single-stock deep-dive helper**: <https://jayz125.github.io/pulse-dashboard/deepdive.html>

## How it works

GitHub Actions runs `scan_web.py` every 15 minutes on weekdays, 13:00-21:59 UTC
(covers US market hours across DST). The script:

1. Pulls 15-min intraday data via [yfinance](https://github.com/ranaroussi/yfinance)
2. Scores each ticker on momentum (30%) + breakout (30%) + reversal (20%) + catalyst (20%)
3. Renders an HTML dashboard with top 3 picks + full ranking table
4. Deploys to GitHub Pages

## Universe

37 tickers, defined in `state.json`. The 4-dimension scoring weights are also there
and can be edited in-place (just commit the change).

## Local usage

This repo is the **web** deploy. For the **local** experience (with macOS
notifications + desktop HTML), see the parent project at
`~/.claude/skills/us-tech-pulse/`.

To run this web version locally:
```bash
cd pulse-dashboard
pip install -r requirements.txt
python scan_web.py
# then open index.html in your browser
```

## Disclaimer

All picks are rule-based quantitative signals. **Not investment advice.** See
disclaimer in the rendered dashboard.
