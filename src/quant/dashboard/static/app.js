// Quant Dashboard - EOD portfolio and technical analysis.

const app = document.getElementById("app");
const MAX_QUOTE_SYMBOLS = 20;

const api = {
  quote: (symbol, refresh = false) =>
    request(`/api/quote/${encodeURIComponent(symbol)}?refresh=${refresh}`),
  quotes: (symbols, refresh = false) =>
    request(
      `/api/quotes?symbols=${encodeURIComponent(symbols.join(","))}&refresh=${refresh}`
    ),
  analysis: (symbol, windows, price) =>
    request(
      `/api/analysis/${encodeURIComponent(symbol)}?windows=${encodeURIComponent(
        windows.join(",")
      )}&price=${encodeURIComponent(price)}`
    ),
  watchlistGet: () => request("/api/watchlist"),
  watchlistAdd: (symbol) => request("/api/watchlist", "POST", { symbol }),
  watchlistRemove: (symbol) =>
    request(`/api/watchlist/${encodeURIComponent(symbol)}`, "DELETE"),
  holdings: (refresh = false) => request(`/api/holdings?refresh=${refresh}`),
  holdingAdd: (symbol, shares, costBasis, metadata = {}) =>
    request("/api/holdings", "POST", {
      symbol,
      shares,
      costBasis,
      ...metadata,
    }),
  holdingRemove: (id) =>
    request(`/api/holdings/${encodeURIComponent(id)}`, "DELETE"),
};

async function request(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // Keep the HTTP status when an error response has no JSON body.
    }
    throw new Error(detail);
  }
  return response.json();
}

async function fetchQuotes(symbols, refresh = false) {
  const quotes = [];
  for (let index = 0; index < symbols.length; index += MAX_QUOTE_SYMBOLS) {
    const batch = symbols.slice(index, index + MAX_QUOTE_SYMBOLS);
    const result = await api.quotes(batch, refresh);
    quotes.push(...result.quotes);
  }
  return quotes;
}

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fmtMoney(value) {
  if (value == null || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function fmtPercent(value, signed = true) {
  if (value == null || Number.isNaN(value)) return "-";
  const sign = signed && value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function fmtNumber(value, digits = 2) {
  if (value == null || Number.isNaN(value)) return "-";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function changeClass(value) {
  if (value == null || value === 0) return "muted";
  return value > 0 ? "up" : "down";
}

function errorPanel(message) {
  return element("div", { class: "error", role: "alert" }, message);
}

function loadingPanel(message = "Loading stored market data...") {
  return element(
    "div",
    { class: "panel muted", role: "status", "aria-live": "polite" },
    message
  );
}

function field(label, control) {
  return element(
    "label",
    { class: "field" },
    element("span", { class: "field-label" }, label),
    control
  );
}

async function renderWatchlist() {
  const root = element("div", { class: "stack" });
  const content = element("div", {}, loadingPanel());
  const symbolInput = element("input", {
    placeholder: "AAPL",
    autocomplete: "off",
    "aria-label": "Ticker symbol",
    oninput: (event) => {
      event.target.value = event.target.value.toUpperCase();
    },
  });
  const addForm = element(
    "form",
    {
      class: "row",
      onsubmit: async (event) => {
        event.preventDefault();
        const symbol = symbolInput.value.trim().toUpperCase();
        if (!symbol) return;
        try {
          await api.watchlistAdd(symbol);
          symbolInput.value = "";
          await drawWatchlist(content);
        } catch (error) {
          content.replaceChildren(errorPanel(`Could not add symbol: ${error.message}`));
        }
      },
    },
    symbolInput,
    element("button", { type: "submit" }, "Add symbol")
  );
  const refreshButton = element(
    "button",
    {
      class: "ghost",
      type: "button",
      onclick: async () => {
        refreshButton.disabled = true;
        try {
          await drawWatchlist(content, true);
          await renderMarketStrip(true);
        } finally {
          refreshButton.disabled = false;
        }
      },
    },
    "Refresh EOD data"
  );

  root.append(
    element(
      "div",
      { class: "row between page-heading" },
      element(
        "div",
        {},
        element("h1", {}, "Watchlist"),
        element(
          "div",
          { class: "muted small" },
          "Latest stored daily close, change, and volume."
        )
      ),
      element("div", { class: "row" }, addForm, refreshButton)
    ),
    content
  );
  app.replaceChildren(root);
  await drawWatchlist(content);
}

async function drawWatchlist(content, refresh = false) {
  content.replaceChildren(loadingPanel());
  try {
    const { symbols } = await api.watchlistGet();
    if (!symbols.length) {
      content.replaceChildren(
        element(
          "div",
          { class: "panel empty-state" },
          element("h2", {}, "No symbols yet"),
          element("div", { class: "muted" }, "Add a US equity ticker to begin.")
        )
      );
      return;
    }

    const quotes = await fetchQuotes(symbols, refresh);
    const quoteBySymbol = new Map(quotes.map((quote) => [quote.symbol, quote]));
    const body = element("tbody");
    for (const symbol of symbols) {
      const quote = quoteBySymbol.get(symbol);
      const unavailable = !quote || quote.error;
      body.append(
        element(
          "tr",
          {},
          element("td", {}, element("a", { href: `#/stock/${symbol}` }, symbol)),
          element("td", { class: "muted" }, unavailable ? "-" : quote.name),
          element("td", {}, unavailable ? "Unavailable" : fmtMoney(quote.price)),
          element(
            "td",
            { class: unavailable ? "muted" : changeClass(quote.change) },
            unavailable ? "-" : fmtMoney(quote.change)
          ),
          element(
            "td",
            { class: unavailable ? "muted" : changeClass(quote.changePercent) },
            unavailable ? "-" : fmtPercent(quote.changePercent)
          ),
          element("td", { class: "muted" }, unavailable ? "-" : fmtNumber(quote.volume, 0)),
          element("td", { class: "muted small" }, unavailable ? "-" : quote.asOf),
          element(
            "td",
            { class: "right" },
            element(
              "button",
              {
                class: "ghost",
                type: "button",
                "aria-label": `Remove ${symbol} from watchlist`,
                onclick: async () => {
                  await api.watchlistRemove(symbol);
                  await drawWatchlist(content);
                },
              },
              "Remove"
            )
          )
        )
      );
    }
    content.replaceChildren(
      element(
        "div",
        { class: "panel table-panel" },
        element(
          "table",
          {},
          element(
            "thead",
            {},
            element(
              "tr",
              {},
              ...["Symbol", "Company", "Close", "Change", "Change %", "Volume", "As of", ""].map(
                (label) => element("th", {}, label)
              )
            )
          ),
          body
        )
      )
    );
  } catch (error) {
    content.replaceChildren(errorPanel(`Watchlist unavailable: ${error.message}`));
  }
}

let priceChart = null;
let navigationId = 0;

async function renderStock(symbol, requestId) {
  symbol = symbol.toUpperCase();
  const root = element("div", { class: "stack" }, loadingPanel("Loading technical history..."));
  app.replaceChildren(root);

  try {
    const technical = await api.analysis(symbol, [20, 50, 200], "close");
    if (requestId !== navigationId) return;
    const quote = await api.quote(symbol);
    if (requestId !== navigationId) return;
    const rows = technical.rows || [];
    if (!rows.length) throw new Error("No stored daily history is available.");
    const latest = rows[rows.length - 1];
    const chartCanvas = element(
      "canvas",
      {
        id: "price-chart",
        role: "img",
        "aria-label": `${symbol} stored daily close chart`,
      },
      `${symbol} stored daily close history is summarized in the table below.`
    );
    const chartWrap = element("div", { class: "chart-wrap" }, chartCanvas);
    const chartButtons = element(
      "div",
      { class: "row", role: "group", "aria-label": "Chart date range" }
    );
    const ranges = [
      ["3M", 90],
      ["6M", 180],
      ["1Y", 365],
      ["All loaded", null],
    ];

    root.replaceChildren(
      element(
        "div",
        { class: "page-heading" },
        element("div", { class: "muted small" }, quote.name || symbol),
        element("h1", {}, symbol),
        element(
          "div",
          { class: "row", style: { alignItems: "baseline" } },
          element("span", { class: "price-big" }, fmtMoney(quote.price)),
          element(
            "span",
            { class: changeClass(quote.changePercent) },
            `${fmtMoney(quote.change)} (${fmtPercent(quote.changePercent)})`
          )
        ),
        element("div", { class: "muted small" }, `Stored EOD observation - ${quote.asOf}`)
      ),
      element(
        "div",
        { class: "grid cols-4" },
        metric("Daily return", fmtPercent(latest.dailyChangePercent), changeClass(latest.dailyChangePercent)),
        metric("20-session high", fmtMoney(latest.rollingHighs?.["20"])),
        metric("20-session low", fmtMoney(latest.rollingLows?.["20"])),
        metric("Relative volume", formatMultiple(latest.relativeVolumes?.["20"]))
      ),
      element("div", { class: "panel stack" }, chartButtons, chartWrap),
      renderTechnicalSnapshot(latest)
    );

    function drawChart(days) {
      for (const button of chartButtons.querySelectorAll("button")) {
        const active = button.dataset.days === String(days);
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      }
      const latestDate = new Date(rows[rows.length - 1].date);
      const cutoff = days == null ? null : new Date(latestDate - days * 86400000);
      const visible = cutoff
        ? rows.filter((row) => new Date(row.date) >= cutoff)
        : rows;
      if (priceChart) priceChart.destroy();
      priceChart = new Chart(chartCanvas.getContext("2d"), {
        type: "line",
        data: {
          datasets: [
            chartDataset(symbol, visible, (row) => row.price, "#4f8cff", 2),
            chartDataset("50-day average", visible, (row) => row.movingAverages?.["50"], "#f59e0b", 1.25),
            chartDataset("200-day average", visible, (row) => row.movingAverages?.["200"], "#a78bfa", 1.25),
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { labels: { color: "#8a93a6" } },
            tooltip: {
              callbacks: { label: (context) => `${context.dataset.label}: ${fmtMoney(context.parsed.y)}` },
            },
          },
          scales: {
            x: {
              type: "time",
              ticks: { color: "#8a93a6" },
              grid: { color: "#1f2530" },
            },
            y: {
              ticks: { color: "#8a93a6", callback: (value) => Number(value).toFixed(2) },
              grid: { color: "#1f2530" },
            },
          },
        },
      });
    }

    for (const [label, days] of ranges) {
      const button = element(
        "button",
        {
          class: `ghost${days === 365 ? " active" : ""}`,
          type: "button",
          "data-days": String(days),
          "aria-pressed": String(days === 365),
          onclick: () => drawChart(days),
        },
        label
      );
      chartButtons.append(button);
    }
    drawChart(365);
  } catch (error) {
    if (requestId !== navigationId) return;
    root.replaceChildren(errorPanel(`Technical analysis unavailable: ${error.message}`));
  }
}

function chartDataset(label, rows, value, color, width) {
  return {
    label,
    data: rows
      .map((row) => ({ x: row.date, y: value(row) }))
      .filter((point) => point.y != null),
    borderColor: color,
    borderWidth: width,
    pointRadius: 0,
    fill: false,
    tension: 0.08,
  };
}

function metric(label, value, cls = "") {
  return element(
    "div",
    { class: "panel" },
    element("div", { class: "muted small" }, label),
    element("div", { class: `metric-value ${cls}` }, value)
  );
}

function formatMultiple(value) {
  return value == null ? "-" : `${value.toFixed(2)}x`;
}

function renderTechnicalSnapshot(row) {
  const windows = [20, 50, 200];
  return element(
    "div",
    { class: "panel table-panel" },
    element("h2", { class: "section-heading" }, `Technical snapshot - ${row.date}`),
    element(
      "table",
      {},
      element(
        "thead",
        {},
        element(
          "tr",
          {},
          element("th", {}, "Window"),
          element("th", {}, "Moving average"),
          element("th", {}, "Rolling high"),
          element("th", {}, "Rolling low"),
          element("th", {}, "Average volume"),
          element("th", {}, "Relative volume")
        )
      ),
      element(
        "tbody",
        {},
        ...windows.map((window) =>
          element(
            "tr",
            {},
            element("td", {}, `${window} sessions`),
            element("td", {}, fmtMoney(row.movingAverages?.[window])),
            element("td", {}, fmtMoney(row.rollingHighs?.[window])),
            element("td", {}, fmtMoney(row.rollingLows?.[window])),
            element("td", {}, fmtNumber(row.volumeAverages?.[window], 0)),
            element("td", {}, formatMultiple(row.relativeVolumes?.[window]))
          )
        )
      )
    )
  );
}

async function renderHoldings() {
  const root = element("div", { class: "stack" });
  const totals = element("div");
  const allocations = element("div");
  const table = element("div", {}, loadingPanel());
  const formSlot = element("div");
  const refreshButton = element(
    "button",
    {
      class: "ghost",
      type: "button",
      onclick: async () => {
        refreshButton.disabled = true;
        try {
          await drawHoldings(totals, allocations, table, true);
        } finally {
          refreshButton.disabled = false;
        }
      },
    },
    "Refresh EOD data"
  );

  root.append(
    element(
      "div",
      { class: "row between page-heading" },
      element(
        "div",
        {},
        element("h1", {}, "Holdings"),
        element("div", { class: "muted small" }, "Current lots, allocation, and stored market value.")
      ),
      refreshButton
    ),
    totals,
    allocations,
    formSlot,
    table
  );
  app.replaceChildren(root);
  renderHoldingForm(formSlot, async () => drawHoldings(totals, allocations, table));
  await drawHoldings(totals, allocations, table);
}

function renderHoldingForm(slot, onSaved) {
  const symbol = element("input", {
    placeholder: "AAPL",
    autocomplete: "off",
    oninput: (event) => {
      event.target.value = event.target.value.toUpperCase();
    },
  });
  const shares = element("input", { placeholder: "10", inputmode: "decimal" });
  const cost = element("input", { placeholder: "150.25", inputmode: "decimal" });
  const account = element(
    "select",
    {},
    element("option", { value: "" }, "Unspecified"),
    element("option", { value: "taxable" }, "Taxable"),
    element("option", { value: "ira" }, "IRA"),
    element("option", { value: "roth" }, "Roth")
  );
  const assetClass = element("input", { placeholder: "Equity" });
  const sector = element("input", { placeholder: "Technology" });
  const acquired = element("input", { type: "date" });
  const message = element("div", { class: "small down", hidden: "hidden" });
  const submit = element("button", { type: "submit" }, "Add holding");

  const form = element(
    "form",
    {
      class: "panel stack",
      onsubmit: async (event) => {
        event.preventDefault();
        message.hidden = true;
        const ticker = symbol.value.trim().toUpperCase();
        const quantity = Number.parseFloat(shares.value);
        const basis = Number.parseFloat(cost.value);
        if (!ticker) return showFormError(message, "Enter a ticker symbol.");
        if (!Number.isFinite(quantity) || quantity <= 0) {
          return showFormError(message, "Shares must be greater than zero.");
        }
        if (!Number.isFinite(basis) || basis < 0) {
          return showFormError(message, "Cost per share cannot be negative.");
        }
        submit.disabled = true;
        try {
          await api.holdingAdd(ticker, quantity, basis, {
            account: account.value || null,
            assetClass: assetClass.value.trim() || null,
            sector: sector.value.trim() || null,
            acquired: acquired.value || null,
          });
          for (const input of [symbol, shares, cost, assetClass, sector, acquired]) {
            input.value = "";
          }
          account.value = "";
          await onSaved();
        } catch (error) {
          showFormError(message, `Could not add holding: ${error.message}`);
        } finally {
          submit.disabled = false;
        }
      },
    },
    element(
      "div",
      { class: "holding-form-grid" },
      field("Symbol", symbol),
      field("Shares", shares),
      field("Cost per share", cost),
      field("Account", account),
      field("Asset class", assetClass),
      field("Sector", sector),
      field("Acquired", acquired),
      element("div", { class: "form-action" }, submit)
    ),
    message
  );
  slot.replaceChildren(form);
}

function showFormError(node, message) {
  node.textContent = message;
  node.hidden = false;
}

async function drawHoldings(totals, allocations, table, refresh = false) {
  table.replaceChildren(loadingPanel());
  try {
    const data = await api.holdings(refresh);
    const summary = data.totals || {};
    const unpriced = data.unpricedSymbols || [];
    totals.replaceChildren(
      element(
        "div",
        { class: "grid cols-4" },
        metric("Total cost basis", fmtMoney(summary.cost)),
        metric("Priced market value", fmtMoney(summary.value)),
        metric("Priced unrealized gain", fmtMoney(summary.gain), changeClass(summary.gain)),
        metric("Priced return", fmtPercent(summary.gainPercent), changeClass(summary.gainPercent))
      ),
      unpriced.length
        ? element(
            "div",
            { class: "warning", role: "status" },
            `Priced metrics use ${fmtMoney(summary.pricedCost)} of ` +
              `${fmtMoney(summary.cost)} total cost basis. Unavailable: ` +
              `${unpriced.join(", ")}.`
          )
        : null
    );
    allocations.replaceChildren(
      element(
        "div",
        { class: "grid cols-3" },
        allocationPanel("By account", data.allocations?.account || []),
        allocationPanel("By asset class", data.allocations?.assetClass || []),
        allocationPanel("By sector", data.allocations?.sector || [])
      )
    );

    const body = element("tbody");
    if (!data.holdings.length) {
      body.append(
        element(
          "tr",
          {},
          element("td", { colspan: 10, class: "muted empty-cell" }, "No holdings yet.")
        )
      );
    } else {
      for (const holding of data.holdings) {
        body.append(
          element(
            "tr",
            {},
            element("td", {}, element("a", { href: `#/stock/${holding.symbol}` }, holding.symbol)),
            element(
              "td",
              {},
              element("div", {}, holding.account || "-"),
              element(
                "div",
                { class: "muted small" },
                [holding.assetClass, holding.sector].filter(Boolean).join(" - ")
              )
            ),
            element("td", {}, fmtNumber(holding.shares, 4)),
            element("td", {}, fmtMoney(holding.costBasis)),
            element("td", {}, holding.marketDataAvailable ? fmtMoney(holding.price) : "Unavailable"),
            element("td", {}, fmtMoney(holding.marketValue)),
            element("td", { class: changeClass(holding.gain) }, fmtMoney(holding.gain)),
            element("td", { class: changeClass(holding.gainPercent) }, fmtPercent(holding.gainPercent)),
            element("td", {}, fmtPercent(holding.weightPercent, false)),
            element(
              "td",
              { class: "right" },
              element(
                "button",
                {
                  class: "danger",
                  type: "button",
                  "aria-label": `Remove ${holding.symbol} holding`,
                  onclick: async () => {
                    await api.holdingRemove(holding.id);
                    await drawHoldings(totals, allocations, table);
                  },
                },
                "Remove"
              )
            )
          )
        );
      }
    }
    table.replaceChildren(
      element(
        "div",
        { class: "panel table-panel" },
        element(
          "table",
          {},
          element(
            "thead",
            {},
            element(
              "tr",
              {},
              ...["Symbol", "Account / class", "Shares", "Cost", "Close", "Value", "Gain", "Return", "Weight", ""].map(
                (label) => element("th", {}, label)
              )
            )
          ),
          body
        )
      )
    );
  } catch (error) {
    table.replaceChildren(errorPanel(`Holdings unavailable: ${error.message}`));
  }
}

function allocationPanel(title, rows) {
  return element(
    "div",
    { class: "panel" },
    element("h2", {}, title),
    rows.length
      ? rows.map((row) =>
          element(
            "div",
            { class: "allocation-row" },
            element(
              "div",
              { class: "row between small" },
              element("span", {}, row.name),
              element("span", { class: "muted" }, fmtPercent(row.weightPercent, false))
            ),
            element(
              "div",
              { class: "allocation-track" },
              element("span", {
                style: { width: `${Math.max(0, Math.min(100, row.weightPercent || 0))}%` },
              })
            )
          )
        )
      : element("div", { class: "muted small" }, "No classified positions")
  );
}

const MARKET_INDICES = [
  { symbol: "^GSPC", label: "S&P 500" },
  { symbol: "^IXIC", label: "NASDAQ" },
  { symbol: "^DJI", label: "DOW" },
  { symbol: "^RUT", label: "RUSSELL" },
];

async function renderMarketStrip(refresh = false) {
  const strip = document.getElementById("market-strip");
  if (!strip) return;
  if (!strip.firstChild) {
    strip.replaceChildren(
      element("div", { class: "market-strip-inner muted" }, "Loading EOD market data...")
    );
  }
  try {
    const { quotes } = await api.quotes(
      MARKET_INDICES.map((market) => market.symbol),
      refresh
    );
    const bySymbol = new Map(quotes.map((quote) => [quote.symbol, quote]));
    const content = element("div", { class: "market-strip-inner" });
    for (const market of MARKET_INDICES) {
      const quote = bySymbol.get(market.symbol);
      if (!quote || quote.error) continue;
      content.append(
        element(
          "span",
          { class: "market-item" },
          element("span", { class: "sym" }, market.label),
          element("span", { class: "price" }, fmtNumber(quote.price)),
          element("span", { class: changeClass(quote.changePercent) }, fmtPercent(quote.changePercent)),
          element("span", { class: "muted small" }, quote.asOf)
        )
      );
    }
    if (!content.childNodes.length) {
      content.append(element("span", { class: "muted small" }, "Market data unavailable."));
    }
    strip.replaceChildren(content);
  } catch (error) {
    strip.replaceChildren(
      element("div", { class: "market-strip-inner muted small" }, `Market data unavailable: ${error.message}`)
    );
  }
}

function router() {
  const requestId = ++navigationId;
  if (priceChart) {
    priceChart.destroy();
    priceChart = null;
  }
  const hash = location.hash.replace(/^#/, "") || "/";
  for (const link of document.querySelectorAll("nav a")) {
    const href = link.getAttribute("href").replace(/^#/, "");
    const active = href === hash;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  app.setAttribute("tabindex", "-1");
  app.focus({ preventScroll: true });
  if (hash === "/") {
    document.title = "Watchlist - Quant";
    return renderWatchlist();
  }
  if (hash === "/holdings") {
    document.title = "Holdings - Quant";
    return renderHoldings();
  }
  const stock = hash.match(/^\/stock\/(.+)$/);
  if (stock) {
    const symbol = decodeURIComponent(stock[1]).toUpperCase();
    document.title = `${symbol} - Quant`;
    return renderStock(symbol, requestId);
  }
  document.title = "Not Found - Quant";
  app.replaceChildren(
    element("div", { class: "panel" }, "Page not found. ", element("a", { href: "#/" }, "Return home"))
  );
}

window.addEventListener("hashchange", router);
router();
renderMarketStrip();
