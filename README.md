# Kärcher K5 Power Control CHK — Price Tracker

A hands-off website that tracks the price of the **Kärcher K5 Power Control CHK** (Car & Home Kit)
pressure washer across US retailers. Prices are gathered automatically on a
schedule — there's nothing to update by hand.

## How it works

```
GitHub Actions (cron, every 6h)
        │  runs scraper/scrape.py
        ▼
fetch each retailer's product page → extract the price
        │  (JSON-LD structured data → meta tags → HTML fallback → headless browser)
        ▼
write data/latest.json + append data/history.json
        │  commit the updated data back to the repo
        ▼
GitHub Pages serves site/ → live table + price-history chart
```

Everything runs on free GitHub infrastructure: **Actions** does the scraping,
**Pages** hosts the site. No server, no database, no manual price entry.

## Project layout

| Path | What it is |
|------|-----------|
| `config/retailers.json` | The one file you edit — the product + a URL per retailer. |
| `scraper/scrape.py` | Fetches every retailer, extracts prices, writes the data files. |
| `scraper/extractors.py` | Price-extraction logic (JSON-LD, meta tags, HTML fallback). |
| `data/latest.json` | Current price per retailer (the site reads this). |
| `data/history.json` | Append-only price history (powers the chart). |
| `index.html`, `app.js`, `styles.css` | Static website, served from the repo root. |
| `.github/workflows/scrape.yml` | The scheduled scrape + auto-commit. |

## One-time setup

1. **Enable GitHub Pages**: repo **Settings → Pages → Build and deployment →
   Deploy from a branch**, pick the **`main`** branch and the root folder (`/`).
   The site is then served at `https://<you>.github.io/tracker/`.
2. **Enable Actions write access** (if not already): **Settings → Actions →
   General → Workflow permissions → Read and write permissions**. This lets the
   scraper commit updated prices.
3. That's it. The scraper runs every 6 hours, or you can trigger it manually
   from the **Actions** tab → *Scrape prices* → *Run workflow*.

## Adjusting what's tracked

Edit `config/retailers.json`. Each retailer is one entry:

```json
{ "id": "homedepot", "name": "The Home Depot", "country": "US",
  "currency": "USD", "url": "https://www.homedepot.com/p/.../315054900" }
```

- To **add** a retailer, copy an entry and set a unique `id` + the product `url`.
- To **remove** one, delete its entry.
- If a URL is wrong or the product page moves, just fix the `url`.

## Running locally

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium   # optional; only used as a fallback
python scraper/scrape.py                # writes data/latest.json + data/history.json

# preview the site
python -m http.server 8000                     # then open http://localhost:8000
```

## Honest limitations

- This works by **scraping** retailer pages — no retailer offers a free price
  API for a single product. Extraction prefers stable structured data
  (schema.org JSON-LD) and falls back to HTML, but markup changes can still
  break an individual retailer. When that happens, that retailer simply shows
  **unavailable** and keeps its last known price — the rest of the site keeps
  working.
- Some retailers (Amazon especially) block automated requests from
  datacenter/CI IP addresses, so their prices may be intermittent from GitHub
  Actions. Coverage is best-effort by design; the config makes it easy to
  add/remove sites.
- Seeded product URLs are best guesses and may need correcting to the exact
  listing you care about — that's the one bit of setup worth a quick check.
- Always confirm the price on the retailer's own site before buying.
