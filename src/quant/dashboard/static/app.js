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
  holdings: (refresh = false) => request(`/api/holdings?refresh=${refresh}`),
  portfolioState: () => request("/api/alpha/portfolio/holdings"),
  search: (query, limit = 8) =>
    request(`/api/search?query=${encodeURIComponent(query)}&limit=${limit}`),
  risk: (symbols, period = "1y", benchmark = "SPY") =>
    request(
      `/api/risk?symbols=${encodeURIComponent(symbols.join(","))}` +
        `&period=${encodeURIComponent(period)}&benchmark=${encodeURIComponent(benchmark)}`
    ),
  portfolioRisk: (period = "1y", benchmark = "SPY") =>
    request(
      `/api/portfolio/risk?period=${encodeURIComponent(period)}` +
        `&benchmark=${encodeURIComponent(benchmark)}`
    ),
  screener: (symbols, period = "1y", benchmark = "SPY") => {
    const selected = symbols?.length
      ? `&symbols=${encodeURIComponent(symbols.join(","))}`
      : "";
    return request(
      `/api/screener?period=${encodeURIComponent(period)}` +
        `&benchmark=${encodeURIComponent(benchmark)}${selected}`
    );
  },
  research: (symbol, section, params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request(
      `/api/research/${encodeURIComponent(symbol)}/${section}${query ? `?${query}` : ""}`
    );
  },
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

function fmtReturn(value, signed = true) {
  return value == null ? "-" : fmtPercent(value * 100, signed);
}

function fmtNumber(value, digits = 2) {
  if (value == null || Number.isNaN(value)) return "-";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function changeClass(value) {
  if (value == null || value === 0) return "muted";
  return value > 0 ? "up" : "down";
}

function scoreClass(value) {
  if (value == null) return "";
  if (value >= 67) return "high";
  if (value < 34) return "low";
  return "";
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
    const riskSlot = element("div", {}, loadingPanel("Loading risk metrics..."));
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
      renderTechnicalSnapshot(latest),
      riskSlot,
      renderResearchPanel(symbol)
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
    drawStockRisk(riskSlot, symbol);
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

async function drawStockRisk(slot, symbol, period = "1y") {
  slot.replaceChildren(loadingPanel("Loading risk metrics..."));
  try {
    const data = await api.risk([symbol], period);
    const row = data.metrics?.[0];
    if (!row) throw new Error("No adjusted-close history is available.");
    slot.replaceChildren(
      element(
        "section",
        { class: "stack", "aria-labelledby": "risk-heading" },
        element(
          "div",
          { class: "row between" },
          element("h2", { id: "risk-heading" }, `Risk profile - ${period}`),
          periodSelect(period, (value) => drawStockRisk(slot, symbol, value))
        ),
        element(
          "div",
          { class: "grid cols-4" },
          metric("Cumulative return", fmtReturn(row.cumulativeReturn), changeClass(row.cumulativeReturn)),
          metric("Annualized volatility", fmtReturn(row.annualizedVolatility, false)),
          metric("Sharpe ratio", fmtNumber(row.sharpeRatio)),
          metric("Maximum drawdown", fmtReturn(row.maxDrawdown), changeClass(row.maxDrawdown))
        ),
        element(
          "div",
          { class: "grid cols-4" },
          metric("Beta vs SPY", fmtNumber(row.beta)),
          metric("Annualized alpha", fmtReturn(row.annualizedAlpha), changeClass(row.annualizedAlpha)),
          metric("1-month return", fmtReturn(row.oneMonthReturn), changeClass(row.oneMonthReturn)),
          metric("12-1 momentum", fmtReturn(row.twelveOneMomentum), changeClass(row.twelveOneMomentum))
        )
      )
    );
  } catch (error) {
    slot.replaceChildren(errorPanel(`Risk analysis unavailable: ${error.message}`));
  }
}

function periodSelect(selected, onchange) {
  return element(
    "select",
    {
      "aria-label": "Analysis period",
      onchange: (event) => onchange(event.target.value),
    },
    ...["1mo", "3mo", "6mo", "1y", "2y", "5y"].map((value) =>
      element("option", { value, selected: value === selected ? "selected" : null }, value)
    )
  );
}

function renderResearchPanel(symbol) {
  const content = element("div", {}, loadingPanel("Select a research section."));
  const tabs = element(
    "div",
    { class: "tabs", role: "tablist", "aria-label": `${symbol} research` }
  );
  const sections = [
    ["profile", "Profile", {}],
    ["analyst", "Analyst", {}],
    ["earnings", "Earnings", { limit: 12 }],
    ["options", "Options", { limit: 50 }],
    ["news", "News", { limit: 20 }],
    ["intraday", "Intraday", { period: "5d", interval: "15m", limit: 250 }],
  ];

  async function load(section, params, button) {
    for (const tab of tabs.querySelectorAll("button")) {
      const active = tab === button;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    }
    content.replaceChildren(loadingPanel(`Loading ${section} research...`));
    try {
      const result = await api.research(symbol, section, params);
      content.replaceChildren(renderResearchValue(result));
    } catch (error) {
      content.replaceChildren(errorPanel(`${section} research unavailable: ${error.message}`));
    }
  }

  for (const [section, label, params] of sections) {
    const button = element(
      "button",
      {
        class: "ghost",
        type: "button",
        role: "tab",
        "aria-selected": "false",
        onclick: () => load(section, params, button),
      },
      label
    );
    tabs.append(button);
  }
  const first = tabs.querySelector("button");
  queueMicrotask(() => first?.click());
  return element(
    "section",
    { class: "panel stack", "aria-labelledby": "research-heading" },
    element("h2", { id: "research-heading" }, "Company research"),
    tabs,
    content
  );
}

function renderResearchValue(value) {
  if (value == null || (Array.isArray(value) && !value.length)) {
    return element("div", { class: "muted empty-state" }, "No data returned for this section.");
  }
  if (Array.isArray(value)) {
    return renderRecordTable(value);
  }
  const primitives = [];
  const groups = [];
  for (const [key, item] of Object.entries(value)) {
    if (item == null || ["string", "number", "boolean"].includes(typeof item)) {
      primitives.push([key, item]);
    } else {
      groups.push([key, item]);
    }
  }
  return element(
    "div",
    {},
    primitives.length
      ? element(
          "dl",
          { class: "key-values" },
          ...primitives.map(([key, item]) =>
            element(
              "div",
              { class: "key-value" },
              element("dt", {}, humanize(key)),
              element("dd", {}, item == null ? "-" : String(item))
            )
          )
        )
      : null,
    ...groups.map(([key, item]) =>
      element(
        "details",
        { class: "research-group" },
        element("summary", {}, humanize(key)),
        Array.isArray(item) ? renderRecordTable(item) : renderResearchValue(item)
      )
    )
  );
}

function renderRecordTable(rows) {
  if (!rows.length) return element("div", { class: "muted empty-state" }, "No records returned.");
  if (!rows.every((row) => row && typeof row === "object" && !Array.isArray(row))) {
    return element("pre", {}, JSON.stringify(rows, null, 2));
  }
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].slice(0, 8);
  return element(
    "div",
    { class: "table-panel" },
    element(
      "table",
      {},
      element("thead", {}, element("tr", {}, ...columns.map((key) => element("th", {}, humanize(key))))),
      element(
        "tbody",
        {},
        ...rows.map((row) =>
          element(
            "tr",
            {},
            ...columns.map((key) => {
              const item = row[key];
              return element(
                "td",
                {},
                item != null && typeof item === "object" ? JSON.stringify(item) : item ?? "-"
              );
            })
          )
        )
      )
    )
  );
}

function humanize(value) {
  return String(value)
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}

async function renderHoldings() {
  const root = element("div", { class: "stack" });
  const totals = element("div");
  const allocations = element("div");
  const risk = element("div");
  const table = element("div", {}, loadingPanel());
  const accounts = element("div");
  const refreshButton = element(
    "button",
    {
      class: "ghost",
      type: "button",
      onclick: async () => {
        refreshButton.disabled = true;
        try {
          await drawHoldings(totals, allocations, table, true);
          await drawAccounts(accounts);
        } finally {
          refreshButton.disabled = false;
        }
      },
    },
    "Reload positions"
  );

  root.append(
    element(
      "div",
      { class: "row between page-heading" },
      element(
        "div",
        {},
        element("h1", {}, "Positions"),
        element("div", { class: "muted small" }, "Owned assets, account cash, and individual lots.")
      ),
      refreshButton
    ),
    totals,
    risk,
    allocations,
    accounts,
    table
  );
  app.replaceChildren(root);
  await drawAccounts(accounts);
  await drawHoldings(totals, allocations, table);
  try {
    const state = await api.portfolioState();
    if (state.data.lots.length) await drawPortfolioRisk(risk);
  } catch (error) {
    risk.replaceChildren(errorPanel(`Portfolio unavailable: ${error.message}`));
  }
}

async function drawAccounts(slot) {
  try {
    const { data } = await api.portfolioState();
    if (!data.accounts.length) { slot.replaceChildren(); return; }
    slot.replaceChildren(element("section", { class: "panel table-panel" },
      element("h2", { class: "section-heading" }, "Account cash"),
      element("table", {},
        element("thead", {}, element("tr", {}, ...["Account", "Currency", "Cash balance", "Recorded through"].map(label => element("th", {}, label)))),
        element("tbody", {}, ...data.accounts.map(account => element("tr", {},
          element("td", {}, account.name), element("td", {}, account.currency),
          element("td", {}, account.cash == null ? "Unknown" : fmtNumber(Number(account.cash), 2)),
          element("td", {}, account.asOf || "Unknown")))))));
  } catch (error) {
    slot.replaceChildren(errorPanel(`Accounts unavailable: ${error.message}`));
  }
}

async function drawPortfolioRisk(slot, period = "1y") {
  slot.replaceChildren(loadingPanel("Loading portfolio risk..."));
  try {
    const data = await api.portfolioRisk(period);
    const row = data.metrics?.[0];
    if (!row) throw new Error("No portfolio history is available.");
    const contributions = data.returnContributions || [];
    slot.replaceChildren(
      element(
        "section",
        { class: "stack", "aria-labelledby": "portfolio-risk-heading" },
        element(
          "div",
          { class: "row between" },
          element("h2", { id: "portfolio-risk-heading" }, `Hypothetical history of current holdings - ${period}`),
          periodSelect(period, (value) => drawPortfolioRisk(slot, value))
        ),
        element(
          "div",
          { class: "grid cols-4" },
          metric("Backcast return", fmtReturn(row.cumulativeReturn), changeClass(row.cumulativeReturn)),
          metric("Annualized volatility", fmtReturn(row.annualizedVolatility, false)),
          metric("Sharpe ratio", fmtNumber(row.sharpeRatio)),
          metric("Maximum drawdown", fmtReturn(row.maxDrawdown), changeClass(row.maxDrawdown))
        ),
        element(
          "div",
          { class: "panel table-panel" },
          element("h2", { class: "section-heading" }, "Endpoint return attribution"),
          element(
            "table",
            {},
            element(
              "thead",
              {},
              element(
                "tr",
                {},
                ...["Symbol", "Start value", "End value", "Contribution"].map((label) =>
                  element("th", {}, label)
                )
              )
            ),
            element(
              "tbody",
              {},
              ...contributions.map((item) =>
                element(
                  "tr",
                  {},
                  element("td", {}, item.symbol),
                  element("td", {}, fmtMoney(item.startValue)),
                  element("td", {}, fmtMoney(item.endValue)),
                  element("td", { class: changeClass(item.returnContribution) }, fmtReturn(item.returnContribution))
                )
              )
            )
          )
        )
      )
    );
  } catch (error) {
    slot.replaceChildren(
      element(
        "div",
        { class: "warning", role: "status" },
        `Portfolio risk unavailable: ${error.message}`
      )
    );
  }
}

async function drawHoldings(totals, allocations, table, refresh = false) {
  table.replaceChildren(loadingPanel());
  try {
    const data = await api.holdings(refresh);
    if (!data.holdings.length) {
      totals.replaceChildren();
      allocations.replaceChildren();
      table.replaceChildren(element("div", { class: "panel empty-state" },
        element("h2", {}, "No positions recorded"),
        element("p", { class: "muted" }, "Ask your agent to review a portfolio statement and propose an import. Individual lots and cash balances will appear here after it is applied.")));
      return;
    }
    const summary = data.totals || {};
    const unpriced = data.unpricedSymbols || [];
    totals.replaceChildren(
      element(
        "div",
        { class: "grid cols-4" },
        metric("Total cost basis", fmtMoney(summary.cost)),
        metric("Priced securities value", fmtMoney(summary.value)),
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
          element("td", { colspan: 10, class: "muted empty-cell" }, "No positions recorded. Ask your agent to review a portfolio statement and propose an import.")
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
            element("td", {}, holding.acquired || "Unknown")
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
              ...["Symbol", "Account / class", "Shares", "Cost / share", "Close", "Value", "Gain", "Return", "Weight", "Acquired"].map(
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

async function renderScreener() {
  const root = element("div", { class: "stack" });
  const content = element("div", {}, loadingPanel("Scoring stored EOD history..."));
  const symbols = element("input", {
    placeholder: "Optional: AAPL, MSFT, NVDA",
    "aria-label": "Screener symbols",
  });
  const period = periodSelect("1y", () => {});
  const submit = element("button", { type: "submit" }, "Run screen");
  const form = element(
    "form",
    {
      class: "row",
      onsubmit: async (event) => {
        event.preventDefault();
        submit.disabled = true;
        await drawScreener(
          content,
          symbols.value.split(",").map((value) => value.trim()).filter(Boolean),
          period.value
        );
        submit.disabled = false;
      },
    },
    field("Symbols", symbols),
    field("History", period),
    element("div", { class: "form-action" }, submit)
  );
  root.append(
    element(
      "div",
      { class: "page-heading" },
      element("h1", {}, "EOD momentum screener"),
      element(
        "div",
        { class: "muted small" },
        "Ranks held or explicitly selected assets by momentum, return, risk, trend, and data coverage."
      )
    ),
    element("div", { class: "panel" }, form),
    content
  );
  app.replaceChildren(root);
  await drawScreener(content, [], "1y");
}

async function drawScreener(content, symbols, period) {
  content.replaceChildren(loadingPanel("Scoring stored EOD history..."));
  try {
    const data = await api.screener(symbols, period);
    const body = element("tbody");
    for (const row of data.rows || []) {
      body.append(
        element(
          "tr",
          {},
          element("td", {}, element("a", { href: `#/stock/${row.symbol}` }, row.symbol)),
          element("td", {}, fmtMoney(row.latestPrice)),
          element("td", { class: changeClass(row.oneMonthReturn) }, fmtReturn(row.oneMonthReturn)),
          element("td", { class: changeClass(row.ytdReturn) }, fmtReturn(row.ytdReturn)),
          element("td", {}, element("span", { class: `score ${scoreClass(row.momentumScore)}` }, fmtNumber(row.momentumScore, 1))),
          element("td", {}, element("span", { class: `score ${scoreClass(row.returnScore)}` }, fmtNumber(row.returnScore, 1))),
          element("td", {}, element("span", { class: `score ${scoreClass(row.riskScore)}` }, fmtNumber(row.riskScore, 1))),
          element("td", {}, element("span", { class: `score ${scoreClass(row.trendScore)}` }, fmtNumber(row.trendScore, 1))),
          element("td", {}, element("span", { class: `score ${scoreClass(row.compositeScore)}` }, fmtNumber(row.compositeScore, 1))),
          element(
            "td",
            {},
            element(
              "div",
              { class: "signal-list" },
              ...(row.signals || []).map((signal) => element("span", { class: "signal" }, signal))
            )
          )
        )
      );
    }
    if (!body.childNodes.length) {
      body.append(
        element("tr", {}, element("td", { colspan: 10, class: "empty-cell muted" }, "No symbols could be scored."))
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
              ...["Symbol", "Close", "1 month", "YTD", "Momentum", "Return", "Risk", "Trend", "Composite", "Signals"].map(
                (label) => element("th", {}, label)
              )
            )
          ),
          body
        )
      )
    );
  } catch (error) {
    content.replaceChildren(errorPanel(`Screener unavailable: ${error.message}`));
  }
}

function initializeSecuritySearch() {
  const form = document.getElementById("security-search");
  const input = document.getElementById("security-search-input");
  const results = document.getElementById("security-search-results");
  if (!form || !input || !results) return;
  let timer = null;
  let searchId = 0;

  function hide() {
    results.hidden = true;
    results.replaceChildren();
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < 2) return hide();
    const current = ++searchId;
    timer = setTimeout(async () => {
      try {
        const data = await api.search(query);
        if (current !== searchId) return;
        const rows = data.local || [];
        results.replaceChildren(
          ...(rows.length
            ? rows.map((row) =>
                element(
                  "a",
                  {
                    class: "search-result",
                    href: `#/stock/${encodeURIComponent(row.symbol)}`,
                    onclick: hide,
                  },
                  element("span", { class: "search-symbol" }, row.symbol),
                  element("span", { class: "search-name" }, row.name)
                )
              )
            : [element("div", { class: "muted small", style: { padding: "9px 10px" } }, "No local matches")])
        );
        results.hidden = false;
      } catch (error) {
        if (current !== searchId) return;
        results.replaceChildren(errorPanel(`Search unavailable: ${error.message}`));
        results.hidden = false;
      }
    }, 180);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const symbol = input.value.trim().toUpperCase();
    if (!symbol) return;
    hide();
    input.value = "";
    location.hash = `#/stock/${encodeURIComponent(symbol)}`;
  });
  document.addEventListener("click", (event) => {
    if (!form.contains(event.target)) hide();
  });
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
    document.title = "Positions - Quant";
    return renderHoldings();
  }
  if (hash === "/holdings") {
    document.title = "Positions - Quant";
    return renderHoldings();
  }
  if (hash === "/screener") {
    document.title = "Screener - Quant";
    return renderScreener();
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
initializeSecuritySearch();
router();
renderMarketStrip();
