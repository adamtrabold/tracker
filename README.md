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
3. **Add a scraping-API key** (strongly recommended). Big US retailers (Home
   Depot, Lowe's, Amazon, Walmart, Best Buy, Target) block automated requests
   from datacenter IPs with a 403, so a plain fetch can't read their prices.
   A scraping API rotates residential IPs and renders the page for you:
   - Get a free key from [ScraperAPI](https://www.scraperapi.com/) (free tier
     ~1,000 requests/month — plenty for a 6-hourly run over ~9 retailers).
   - Add it under **Settings → Secrets and variables → Actions → New repository
     secret**, named `SCRAPER_API_KEY`.
   - (Optional) To use a different provider, add a repo **variable**
     `SCRAPER_API_PROVIDER` = `scrapingbee` or `zenrows` (default `scraperapi`).

   Without a key the tracker still runs, but only non-blocking sites will
   report prices.
4. That's it. The scraper runs every 6 hours, or you can trigger it manually
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

# optional: route through a scraping API to get past retailer blocks
export SCRAPER_API_KEY=your_key_here    # e.g. from scraperapi.com
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
- Big retailers block datacenter/CI IPs with a 403, so from GitHub Actions a
  plain fetch can't read most of them. The **scraping-API key** (setup step 3)
  fixes this by routing through rotating residential IPs — with it configured,
  coverage is reliable; without it, only non-blocking sites report prices.
- A `config.product.expected_price_range` guard rejects implausible values
  (e.g. an accessory price a fallback selector might grab), so a wrong number
  never shows as the lowest price. Adjust the range if you track a different SKU.
- Always confirm the price on the retailer's own site before buying.
