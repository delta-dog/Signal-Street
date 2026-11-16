# Signal Street: Indian market-news monitor

*Signal over noise, for the Nifty 50 and Sensex.* The Python package and commands are named `marketmon`.

Polls primary sources (NSE, BSE, RBI, SEBI, PIB), then media and global cues, for items
likely to move the Nifty or Sensex. Every item goes into one schema; the monitor dedupes,
filters noise, scores priority, and pushes alerts. It also logs **publish-to-seen lag per
source**, so you can measure whether free polling is fast enough.

```
sources (1 async worker each) ─▶ hard-key dedupe ─▶ rules (noise, P0-P3) ─▶ clustering ─▶ SQLite
                                                                                 │
                                         P0 now / P1 5-min digest ◀──────────────┤
                                         dashboard (127.0.0.1:8765) ◀────────────┘
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
# .venv/bin/python -m pip install -r requirements.txt       # Linux/macOS
```

Python 3.11+. Dependencies: httpx[http2], PyYAML, feedparser, selectolax, tzdata (Windows), pytest.

## Commands

| Command | What it does |
|---|---|
| `python -m marketmon check-sources [--source X] [--record DIR]` | Hit every endpoint once, print the health table (status, items, newest-item age, conditional-GET support). `--record` saves raw bodies for fixtures. |
| `python -m marketmon run [--web 8765]` | Run the live monitor. With `--web`, also serve the dashboard on http://127.0.0.1:8765. |
| `python -m marketmon report [--days 7]` | Per source: lag p50/p95, samples, items/day, poll error rate, block/captcha events, cookie refreshes. |
| `python -m marketmon dry-run` | Replay `tests/fixtures/` through the whole pipeline (no network) and print the alerts it would send. |
| `python -m marketmon web [--port 8765]` | Dashboard only, for an existing database. |
| `python -m pytest` | Tests: dedupe, filters/priorities, NSE cookie refresh, parsers, calendar, dry-run. |

## Configuration

- `sources.yaml`: sources, cadences (hot / cool / burst), per-host rate limits, alert sinks, dedupe settings.
- `config/index.yaml`: **you maintain this.** Nifty 50 constituents with NSE symbol, ISIN, BSE code,
  headline aliases and `heavyweight: true` for the top ~14. Seeded 2026-11-16 from NSE's
  constituent CSV. The heavyweight flags are approximate: check them against the index factsheet.
- `config/rules.yaml`: noise (`drop`) patterns, market-moving (`keep`) labels, and P0/P1 rules per source.
- `config/calendar.yaml`: 2026 NSE trading holidays (from NSE's holiday master), the hot window
  (08:45-16:00 IST), what to do on non-trading days, and Muhurat/special-session overrides.

### Index impact (Nifty 50 / Sensex first)

Every item gets an impact tag. Only tagged items can be P0 or P1, and the dashboard's feed opens on
**Nifty movers** (tagged items only):

| Tag | Meaning |
|---|---|
| ★ Heavyweight | filing or market-moving story about a top-weight constituent |
| Nifty 50 stock | the same for any other constituent |
| Index-wide | RBI rates and bank/NBFC prudential rules, inflation/GDP/IIP data, FII flows, crude, rupee, Fed and US data, ECB, budget/tax, F&O and margin rules, geopolitical shocks |

Live blogs, "stocks to watch" lists and "should you buy" pieces never count. A media story that
merely names a constituent needs a market-moving label (results, orders, M&A, regulatory action…).
The patterns live in `config/rules.yaml → index_impact`. On start-up the monitor re-scores the last
24 hours with the current rules, so a rules change applies to today's data too.

### Priorities

| | Meaning | Examples |
|---|---|---|
| P0 | pushed instantly | RBI notifications / policy items; SEBI item naming a heavyweight; Nifty 50 results or board-meeting outcome; any market-moving filing from a heavyweight; Cabinet decision on energy or finance |
| P1 | 5-minute digest | other market-moving filings from Nifty 50 names; SEBI circulars/press releases; other PIB items from watched ministries; FOMC items; media story naming a Nifty 50 company with a market-moving label |
| P2 | logged only | everything else |
| P3 | dropped as noise | trading-window closures, ESOP allotments, newspaper ads, analyst meets, share-certificate loss, … |

Only the first item of a story (the cluster **lead**) can alert. An NSE and BSE copy of the
same filing within 30 min is a **dupe**. A media story about something we already have from an
exchange or regulator is a **follow-up** inside that cluster.

### Start-up and backlog

A source's first poll after start-up is stored as backlog. It records no lag samples (we weren't
watching), and alerts only on items published in the last `fresh_minutes` (10).

### Burst mode (MPC days)

RBI has a 3-second `burst` cadence that polls only the press-release and notification feeds.
Switch it on and off with the **Burst** button on the dashboard (stored in the `control` table),
or with `sqlite3 marketmon.db "INSERT OR REPLACE INTO control VALUES('burst:rbi','1')"`.

### Alerts

Sinks are enabled when listed in `sources.yaml → alerts.sinks` **and** their env vars are set:

- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Email (SMTP + STARTTLS): `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`

Each alert carries priority, source, headline, link and latency (first seen − published).

## Disabling a source

Set `enabled: false` under the source in `sources.yaml`, or at runtime:

```bash
MARKETMON_DISABLE=nse,bse python -m marketmon run --web 8765
```

## Dashboard

`python -m marketmon run --web 8765`, then open http://127.0.0.1:8765.

- **Theme:** Auto (follows the OS), Light or Dark, top right. Remembered per browser.
- **India indices:** Nifty 50, Bank, IT, Financial Services, Midcap 100, India VIX from NSE's
  `allIndices`, every 15 s in market hours. Lines are today's ticks; the dotted line is the previous close.
- **Currencies:** INR per USD, EUR, GBP, JPY (per 100), AED, SGD from Coinbase's public rates every
  60 s. Change is against ExchangeRate-API's once-a-day reference.
- **Nifty 50 stocks:** live prices for the 49 BSE-listed constituents from BSE's group-A gainers and
  losers lists, every 30 s (BSE Ltd itself trades only on NSE; a stock exactly unchanged on the day can
  briefly drop out). Star a stock to track it. **Tracked stocks news** shows its news; the first visit
  tracks the heavyweights.
- **Charts:** the large chart opens on the Nifty 50. Click any index card, currency card or stock (row or
  symbol) to chart it. Ranges: indices 1D/1M/3M/6M/1Y (NSE chart API; the pre-open auction is left
  out), stocks 1D/5D/1M/6M/1Y (BSE), currencies 1D from our ticks and 1M–1Y from ECB reference rates
  (Frankfurter). Hover, or focus the chart and use ←/→ (Shift for bigger steps), to read any point. The
  dotted line is the previous close. Key stats and related news sit beside it.
  Intraday data is cached for 60 s and longer ranges for an hour. Served at `/api/chart?symbol=..&range=..`.
- **Pages:** Overview (`#/`: index cards, four key tiles, top index-moving stories, biggest Nifty 50 moves), Chart
  (`#/chart`), Nifty 50 (`#/stocks`), News (`#/news`) and Sources (`#/sources`). Only the open page re-renders on each poll.
  - **Wide screens:** the panel button in the sidebar header (or Ctrl/⌘ \) collapses the sidebar to an icon rail and expands it again. Hover an icon for its name. The choice is remembered.
  - **Phones and tablets:** the ☰ button opens the sidebar as a drawer.
- **Live clock:** the top bar shows India time to the second (hh:mm:ss IST). If this device's clock is more than a second off the monitor's, the clock corrects itself.
- **Logos:**
  - **Where:** news outlets, Nifty 50 companies, indices (the NSE mark) and sources show their site icon; currencies show their flag (from flagcdn.com).
  - **How icons are found:** the monitor's own server looks each site up once at `/api/logo?d=<domain>` and caches the result in `logo_cache/`, so your browser never contacts the icon services.
    - It tries Google's favicon service, then DuckDuckGo's, then the site's `/favicon.ico`, then the icon linked from its home page.
    - Misses are retried after a day; icons are refreshed after 30 days.
  - **Google News:** the outlet's own site comes from the feed's `<source url>`, stored with each item.
  - **Fallback:** sites with no reachable icon show a lettered tile. Today that's BEL (its site's certificate chain is incomplete) and Trent (no icon).
- **Brand mark:** a rising price line ending in a green "live" dot, in Material 3 tonal colours from the dashboard blue (#1a73e8).
  - Square icon: `logo.svg` (adapts to dark mode) and `logo-tonal.svg` (for light surfaces).
  - Circle icon: `logo-circle.svg` and `logo-circle-tonal.svg`.
  - Also `logo-mono.svg` (one colour) and `logo-lockup.svg` (icon + name).
  - All are in `marketmon/static/` and served at `/<name>.svg`.
  - The square icon is the sidebar mark and the browser-tab icon; `manifest.webmanifest` lists it for "add to home screen".
- **Clicking:** the source chips on the News page filter the feed by source type.
- **Staying on top of it:**
  - Items that arrived since you last looked get a blue dot. The News tab and the browser tab title show an unread count, and "Mark read" (or leaving the feed) clears it.
  - Each stock tag in the feed shows that stock's live % move.
  - The feed is grouped by time (last hour, earlier today, older).
  - A Live / Delayed / Offline pill shows how fresh the data is.
  - The Market tile shows time left in the session, or when it next opens (holidays skipped).
- **Notifications (opt-in):** the sidebar switch asks for browser permission, then pops a notification for new P0 items and
  for P1 news on stocks you track, but only while the page isn't in front of you. With it on, the page keeps a light 15 s
  watch in the background; otherwise polling pauses while the tab is hidden.
- **Remembered per browser:** feed view and filters, market tab, stock tab and sort order (click a column header), and the charted symbol and range.
- Price data is for personal use. Exchange data may not be redistributed without a licence.

## Source scouting (2026-11-16)

A 12-minute live race, matching the same filing across feeds (small samples, so treat as indicative):

| Source | Lag p50 | Fastest | Used? |
|---|---|---|---|
| BSE JSON API, all categories in one request | 33 s | 5 s | yes (primary) |
| NSE JSON API | 46 s | 18 s | yes (primary) |
| BSE XML feed | 60 s | 41 s | no: ~36 s behind the API, 2.5 MB per poll, no conditional GET |
| NSE official RSS (`Online_announcements.xml`) | 192 s | 40 s | off by default; robots-friendly fallback |
| Hindu BusinessLine (site-wide) | ~2.6 min | 2 min | yes |
| CNBC-TV18 market / latest | ~4 min | | yes (new) |
| Investing.com India | ~5 min | 2.8 min | yes (new) |
| Business Standard, Business Today | ~13 min | | Business Standard markets only |
| NDTV Profit | ~33 min | 12 min | yes, but slow |

Also checked: Financial Express market feed (empty), Zee Business (404), ANI business (403),
MoSPI (no RSS; its releases arrive via PIB), Marketaux free tier (aggregator, 100 requests/day).

**Faster than this needs a paid or licensed channel:**
- An authorised exchange data vendor such as TrueData or Global Datafeeds, whose corporate-announcement
  APIs push over WebSocket.
- A direct NSE/BSE data licence.
- X's pay-per-use API to follow the official NSE, BSE, RBI, SEBI and PIB accounts. Third-party
  reports put reads at about $0.005 per post.

None of these is wired in.

## What latency to expect (measured, not promised)

Checked from one machine on 2026-11-16. Run `report` after a few days for real numbers.

- **NSE / BSE filings:** measured medians of 33-46 s at a 7 s cadence, most of which is before the
  exchange API shows the filing. BSE's API sometimes takes 10+ s to answer, which adds directly to its lag.
- **RBI:** 30-60 s at the 45 s cadence, or a few seconds in burst mode.
- **PIB:** the listing has no time. The 'Posted On' time comes from each release page (~2.5 MB,
  capped at 5 per poll).
- **SEBI:** the RSS is empty. The listing pages carry only a date, so SEBI lag cannot be measured.
- **Media:** stories are typically 10-60 min old by the time they appear in RSS. ET's feeds trailed
  by hours. Moneycontrol's RSS is frozen (~2.5 years stale) and is not used.

## Licensing and politeness

- **NSE and BSE data:** the endpoints used here are unofficial. NSE's robots.txt disallows
  `/api/`. Redistributing exchange data (to a channel, group, website or anyone but you)
  needs a data licence from the exchange. For anything beyond personal use, use an
  authorised data vendor.
- Requests to each host are serialised with a minimum gap (`host_min_interval`). NSE and
  BSE are capped at 3 req/s. RSS uses conditional GETs (ETag / If-Modified-Since) and a
  descriptive User-Agent. Failures back off exponentially with jitter (≥60 s base when blocked).
- If a site blocks us or shows a captcha, we log a `blocked` event and back off. There is no
  proxy rotation and no CAPTCHA bypass.

## Not built (optional items from the spec)

- **LLM classifier** (index relevance, direction, entity) after the rules. The rules path does not
  need it, and P0 never depends on it.
- **Embedding similarity** as a second dedupe pass. `Clusterer(second_pass=fn)` is the hook. Today
  the second pass is token Jaccard.
