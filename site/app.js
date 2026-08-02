/* Kärcher K5 Power Control price tracker — frontend.
   Loads data/latest.json + data/history.json (written by scraper/scrape.py)
   and renders the table, the "lowest price" card, and the history chart.
   Pure static; no build step. */

const DATA_BASE = "../data";

async function loadJSON(path, fallback) {
  try {
    const res = await fetch(`${path}?_=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    return await res.json();
  } catch (e) {
    console.warn("Could not load", path, e);
    return fallback;
  }
}

function fmtPrice(price, currency) {
  if (price == null) return null;
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency || "USD",
    }).format(price);
  } catch {
    return `$${Number(price).toFixed(2)}`;
  }
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function render(latest) {
  const product = latest.product || {};
  if (product.name) document.getElementById("product-name").textContent = product.name;
  document.getElementById("updated-at").textContent = fmtTime(latest.updated_at);

  const retailers = latest.retailers || [];
  const priced = retailers.filter((r) => r.status === "ok" && r.price != null);
  const best = priced.reduce(
    (a, b) => (a == null || b.price < a.price ? b : a),
    null
  );

  // Lowest-price card
  const bestCard = document.getElementById("best-card");
  if (best) {
    bestCard.hidden = false;
    document.getElementById("best-price").textContent = fmtPrice(best.price, best.currency);
    const link = document.getElementById("best-retailer");
    link.textContent = `at ${best.name}`;
    link.href = best.url;
  } else {
    bestCard.hidden = true;
  }

  // Table
  const rows = document.getElementById("price-rows");
  rows.innerHTML = "";
  // Sort: priced ascending first, then unavailable.
  const sorted = [...retailers].sort((a, b) => {
    const ap = a.status === "ok" ? a.price : Infinity;
    const bp = b.status === "ok" ? b.price : Infinity;
    return ap - bp;
  });

  for (const r of sorted) {
    const tr = document.createElement("tr");
    if (best && r.retailer_id === best.retailer_id) tr.className = "best-row";

    const priceText = fmtPrice(r.price, r.currency);
    let priceCell;
    if (priceText) {
      priceCell = `<span class="price">${priceText}</span>`;
    } else if (r.last_known_price != null) {
      priceCell = `<span class="price na">last: ${fmtPrice(r.last_known_price, r.currency)}</span>`;
    } else {
      priceCell = `<span class="price na">—</span>`;
    }

    const badge =
      r.status === "ok"
        ? `<span class="badge ok">in stock</span>`
        : `<span class="badge unavailable">unavailable</span>`;

    tr.innerHTML = `
      <td>${r.name}</td>
      <td class="num">${priceCell}</td>
      <td>${badge}</td>
      <td class="muted-time">${fmtTime(r.timestamp)}</td>
      <td><a class="link-btn" href="${r.url}" target="_blank" rel="noopener">View →</a></td>
    `;
    rows.appendChild(tr);
  }
}

const PALETTE = [
  "#ffd23f", "#37c978", "#4ea8ff", "#ff6b6b",
  "#b083ff", "#ff9f43", "#2bc4b4", "#f368e0",
];

function renderChart(history, latest) {
  const canvas = document.getElementById("history-chart");
  const hint = document.getElementById("chart-hint");
  if (!Array.isArray(history) || history.length === 0) return;

  const nameById = {};
  (latest.retailers || []).forEach((r) => (nameById[r.retailer_id] = r.name));

  // Group points by retailer.
  const byRetailer = {};
  for (const point of history) {
    (byRetailer[point.retailer_id] ||= []).push(point);
  }

  const datasets = Object.entries(byRetailer).map(([rid, points], i) => {
    points.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    return {
      label: nameById[rid] || rid,
      data: points.map((p) => ({ x: p.timestamp, y: p.price })),
      borderColor: PALETTE[i % PALETTE.length],
      backgroundColor: PALETTE[i % PALETTE.length],
      tension: 0.25,
      pointRadius: 3,
      spanGaps: true,
    };
  });

  const totalPoints = history.length;
  if (totalPoints < datasets.length * 2) {
    hint.textContent = "History builds up as the tracker runs over time — check back for trends.";
  } else {
    hint.textContent = "";
  }

  new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      scales: {
        x: {
          type: "time",
          time: { unit: "day" },
          ticks: { color: "#93a4b7" },
          grid: { color: "#263647" },
        },
        y: {
          ticks: {
            color: "#93a4b7",
            callback: (v) => "$" + v,
          },
          grid: { color: "#263647" },
        },
      },
      plugins: {
        legend: { labels: { color: "#e7eef6" } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: $${ctx.parsed.y}`,
          },
        },
      },
    },
  });
}

(async function main() {
  const latest = await loadJSON(`${DATA_BASE}/latest.json`, { retailers: [] });
  const history = await loadJSON(`${DATA_BASE}/history.json`, []);
  render(latest);
  // Chart.js time scale needs a date adapter; if it's missing we skip the chart.
  if (window.Chart) {
    try {
      renderChart(history, latest);
    } catch (e) {
      console.warn("Chart render skipped:", e);
    }
  }
})();
