# XRP News Terminal

A terminal-style page for XRP: live price chart, news and X posts rated by a fixed rubric and tagged by theme, spot XRP ETF flows with a rule-based read, and checks on popular theories. A scheduled GitHub Actions pipeline gathers and rates the data and writes `data.json`; `index.html` reads it.

Until `data.json` exists, the page runs on clearly labeled sample data.

## Setup

1. **Add the API keys as repository secrets.** Settings → Secrets and variables → Actions → New repository secret:
   - `XAI_API_KEY` from console.x.ai (X scan, web news search, rating)
   - `COINGLASS_API_KEY` from coinglass.com (ETF flows, funding and open interest)

   Either can be added later; the pipeline skips whatever has no key.
2. **Turn on GitHub Pages.** Settings → Pages → Build and deployment → Source: *Deploy from a branch* → Branch: `main`, folder `/ (root)`. The page appears at `https://<user>.github.io/xrp-terminal/`.
3. **Check the X account list.** Actions → *pipeline* → Run workflow → task `verify_handles`. The log lists each handle with the account it belongs to. Edit `config/handles.yaml` to fix or add accounts.
4. **Run a first scan.** Actions → *pipeline* → Run workflow → task `scan`, then `etf`. After that the schedule takes over.

## Schedule

All times New York. The workflow fires at both UTC hours that can map to a slot, so daylight saving changes don't shift it.

| What | When |
|---|---|
| X scan (curated accounts + open discovery), RSS, rating | 8:00, 17:00 |
| Web news search (Grok) | 8:00 scan only |
| ETF flows, positioning | 20:00 (flows post after the US close) |
| Price reactions, base rates, `data.json` rebuild | every run |

Change times in `config/settings.yaml`.

## How items are rated

Grok fills in categorical fields for each item: source tier, confirmation status, whether it's new or already known, size relative to the market, and whether it's specifically about XRP. The impact tier is then computed from those fields with fixed points (`rubric` in `config/settings.yaml`), the same way for every item.

Every rated item is logged in `data/events.jsonl`. The pipeline later measures XRP's return and XRP minus BTC over 1h, 4h and 24h from Coinbase candles. Once a theme has 30 events with a 24h reaction, its items show the historical share that moved more than ±3% versus BTC within 24 hours. Events overlap in time, so treat these as rough base rates, not causal estimates.

## Cost control

Each Grok call's usage is logged and priced with the rates in `config/settings.yaml`. When the month's estimate reaches `budget.monthly_usd` (default $40), scans stop until the next month. Spend shows in the page footer.

## Files

| Path | What |
|---|---|
| `index.html` | The terminal page |
| `data.json` | What the page reads (written by the pipeline) |
| `data/events.jsonl` | Every rated item with its price reactions: the dataset behind the base rates |
| `data/state.json` | Seen posts, schedule slots, spend |
| `data/etf.json` | Last ETF pull |
| `config/handles.yaml` | X accounts for the signal scan, and accounts to block from discovery |
| `config/feeds.yaml` | RSS feeds |
| `config/theories.yaml` | Theory Check panel |
| `pipeline/` | The pipeline (`python -m pipeline --task auto`) |

## Local test

```
pip install -r requirements.txt
python -m pipeline.selftest      # offline, fake APIs, no keys, no cost
```
