const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  company: null,
  suggestions: [],
  sugIndex: -1,
  abort: null,
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function money(v, currency = "MYR", digits = 2) {
  if (!Number.isFinite(v)) return "—";
  try {
    return new Intl.NumberFormat("en-MY", {
      style: "currency",
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(v);
  } catch {
    return `${v.toFixed(digits)}`;
  }
}

function compact(v, currency = "MYR") {
  if (!Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  const units = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ];
  for (const [size, suffix] of units) {
    if (abs >= size) {
      const n = v / size;
      const digits = Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2;
      return `${currency === "MYR" ? "RM " : ""}${n.toFixed(digits)}${suffix}`;
    }
  }
  return money(v, currency);
}

const pct = (v, digits = 2) => (Number.isFinite(v) ? `${(v * 100).toFixed(digits)}%` : "—");

const ratio = (v, digits = 2) => (Number.isFinite(v) ? v.toFixed(digits) : "—");

const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-MY", { day: "2-digit", month: "short", year: "numeric" });
};

/* ---------------- theme ---------------- */
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("sawit.theme", theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "dark" ? "#000000" : "#fbfbfd";
  if (state.company?.price?.history) drawSpark(state.company.price.history);
}

(function themeBoot() {
  const saved = localStorage.getItem("sawit.theme");
  document.documentElement.dataset.theme = saved || "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = document.documentElement.dataset.theme === "dark" ? "#000000" : "#fbfbfd";
  $("#themeBtn")?.addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
})();

/* ---------------- search ---------------- */
function hideSuggestions() {
  $("#suggestions").hidden = true;
  state.sugIndex = -1;
}

function renderSuggestions(items) {
  const box = $("#suggestions");
  if (!items.length) {
    hideSuggestions();
    return;
  }
  state.suggestions = items;
  state.sugIndex = -1;
  box.innerHTML = items
    .map(
      (r, i) => `
      <li class="sug" data-i="${i}">
        <span class="sug-code">${esc(r.code)}</span>
        <span class="sug-name">${esc(r.name)}</span>
        <span class="sug-sec">${esc(r.sector || r.exchange || "")}</span>
      </li>`
    )
    .join("");
  box.hidden = false;
}

function highlight(index) {
  const items = $$(".sug");
  items.forEach((el, i) => el.classList.toggle("on", i === index));
  if (items[index]) items[index].scrollIntoView({ block: "nearest" });
}

let searchTimer = null;

function onQueryInput(value) {
  clearTimeout(searchTimer);
  const q = value.trim();
  if (q.length < 2) {
    hideSuggestions();
    return;
  }
  searchTimer = setTimeout(async () => {
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      renderSuggestions(data.results || []);
    } catch {
      hideSuggestions();
    }
  }, 260);
}

/* ---------------- loading states ---------------- */
function setLoading() {
  $("#loading").hidden = false;
  $("#error").hidden = true;
  $("#results").hidden = true;
}

function showError(title, message) {
  $("#loading").hidden = true;
  $("#results").hidden = true;
  $("#error").hidden = false;
  $("#errorTitle").textContent = title;
  $("#errorMessage").textContent = message;
  $("#error").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function loadCompany(code, { scroll = false } = {}) {
  const clean = String(code || "").trim();
  if (!clean) return;
  state.abort?.abort();
  const ctrl = new AbortController();
  state.abort = ctrl;
  hideSuggestions();
  setLoading();
  if (scroll) $("#loading").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const res = await fetch(`/api/company/${encodeURIComponent(clean)}`, { signal: ctrl.signal });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(data?.error || "Could not load that company.");
    if (ctrl.signal.aborted) return;
    state.company = data;
    $("#loading").hidden = true;
    render(data);
    $("#results").hidden = false;
    const url = new URL(location.href);
    url.searchParams.set("code", data.code);
    history.replaceState({}, "", url);
    document.title = `${data.name} — Sawit`;
    if (scroll || true) {
      requestAnimationFrame(() => {
        const target = $("#results");
        const top = target.getBoundingClientRect().top + window.scrollY - 74;
        window.scrollTo({ top, behavior: "smooth" });
      });
    }
  } catch (err) {
    if (err?.name === "AbortError") return;
    $("#loading").hidden = true;
    showError("Could not load that company.", err.message || "Check the stock code and try again.");
  }
}

/* ---------------- sparkline ---------------- */
function drawSpark(points) {
  const canvas = $("#spark");
  if (!canvas || !points || points.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 400;
  const h = 90;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const values = points.map((p) => p.c).filter((v) => Number.isFinite(v));
  if (values.length < 2) return;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 8;
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue("--accent").trim() || "#0071e3";
  const line = css.getPropertyValue("--line-soft").trim() || "#e8e8ed";

  const coords = points.map((p, i) => [
    pad + (i / (points.length - 1)) * (w - pad * 2),
    h - pad - ((p.c - min) / span) * (h - pad * 2),
  ]);

  ctx.strokeStyle = line;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad, h - pad);
  ctx.lineTo(w - pad, h - pad);
  ctx.stroke();

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, hexToRgba(accent, 0.18));
  grad.addColorStop(1, hexToRgba(accent, 0));
  ctx.beginPath();
  coords.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.lineTo(coords[coords.length - 1][0], h - pad);
  ctx.lineTo(coords[0][0], h - pad);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  coords.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  // Leading dot so the current value reads as a live point.
  const [lx, ly] = coords[coords.length - 1];
  ctx.beginPath();
  ctx.arc(lx, ly, 3.2, 0, Math.PI * 2);
  ctx.fillStyle = accent;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(lx, ly, 6.5, 0, Math.PI * 2);
  ctx.fillStyle = hexToRgba(accent, 0.18);
  ctx.fill();
}

function hexToRgba(hex, alpha) {
  const clean = String(hex).trim();
  if (clean.startsWith("rgb")) return clean.replace("rgb(", "rgba(").replace(")", `, ${alpha})`);
  const h = clean.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const num = parseInt(full, 16);
  if (Number.isNaN(num)) return `rgba(0, 113, 227, ${alpha})`;
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/* ---------------- renderers ---------------- */
function render(c) {
  const p = c.price || {};
  const f = c.financials || {};

  $("#codeBadge").textContent = c.code || "—";
  $("#ticker").textContent = c.symbol || "";
  $("#exchange").textContent = c.exchange || "Bursa Malaysia";
  $("#sectorPill").textContent = c.sector || c.industry || "Listed";
  $("#companyName").textContent = c.name || "";
  $("#companySub").textContent = [c.industry, c.city, c.country, Number.isFinite(c.employees) ? `${c.employees.toLocaleString()} employees` : null]
    .filter(Boolean)
    .join(" · ");

  $("#bursaLink").href = `https://www.bursamalaysia.com/market_information/announcements/company_announcement?company=${encodeURIComponent(c.code || "")}`;
  const site = c.website;
  const siteLink = $("#siteLink");
  if (site) {
    siteLink.href = site;
    siteLink.hidden = false;
  } else {
    siteLink.hidden = true;
  }

  // price
  $("#priceTag").textContent = `${c.currency || "MYR"} · Bursa`;
  $("#bigPrice").textContent = Number.isFinite(p.last) ? money(p.last, c.currency) : "—";
  const changeEl = $("#changePct");
  if (Number.isFinite(p.changePercent)) {
    const up = p.changePercent >= 0;
    changeEl.className = `change ${up ? "up" : "down"}`;
    changeEl.textContent = `${up ? "▲" : "▼"} ${(Math.abs(p.changePercent) * 100).toFixed(2)}% today`;
  } else {
    changeEl.className = "change";
    changeEl.textContent = "—";
  }
  $("#range52").textContent =
    Number.isFinite(p.low52) && Number.isFinite(p.high52)
      ? `52w ${money(p.low52, c.currency)} – ${money(p.high52, c.currency)}`
      : "";

  drawSpark(p.history || []);

  // stat grid
  const stats = [
    ["Market cap", compact(p.marketCap, c.currency)],
    ["P/E", ratio(f.pe)],
    ["Forward P/E", ratio(f.forwardPe)],
    ["EPS", Number.isFinite(f.eps) ? money(f.eps, c.currency) : "—"],
    ["Dividend yield", pct(f.dividendYield)],
    ["Dividend / share", Number.isFinite(f.dividendRate) ? money(f.dividendRate, c.currency) : "—"],
    ["Payout ratio", pct(f.payoutRatio, 1)],
    ["Price / book", ratio(f.priceToBook)],
    ["Book value", Number.isFinite(f.bookValue) ? money(f.bookValue, c.currency) : "—"],
    ["ROE", pct(f.returnOnEquity, 1)],
    ["ROA", pct(f.returnOnAssets, 2)],
    ["Net margin", pct(f.profitMargin, 1)],
    ["Operating margin", pct(f.operatingMargin, 1)],
    ["Revenue growth", pct(f.revenueGrowth, 1)],
    ["Beta", ratio(f.beta)],
    ["Shares out", Number.isFinite(f.shares) ? compact(f.shares, "") .replace("RM ", "") : "—"],
  ];
  $("#statGrid").innerHTML = stats
    .filter(([, v]) => v && v !== "—")
    .map(([label, value]) => `<div class="stat"><span>${esc(label)}</span><b>${esc(value)}</b></div>`)
    .join("");

  // reports
  $("#reportList").innerHTML = (c.reports || [])
    .map(
      (r) => `
      <li class="report-item">
        <a href="${esc(r.url)}" target="_blank" rel="noopener">
          <svg class="icon"><use href="#i-file"></use></svg>
          <span class="report-text">
            <span class="report-label">${esc(r.label)}</span>
            <span class="report-note">${esc(r.note || "")}</span>
          </span>
          <svg class="icon report-arrow"><use href="#i-external"></use></svg>
        </a>
      </li>`
    )
    .join("");

  // ESG checklist for this company
  $("#esgNote").textContent =
    "These are the disclosures Bursa-listed issuers are expected to publish. The right-hand column names the document that should hold each answer — open it from the Reports panel above.";
  renderEsgInto($("#esgPanels"), c.esgChecklist || []);

  // governance
  const g = c.governance || {};
  $("#govTag").textContent = g.available ? `${(c.officers || []).length} disclosed` : "Not disclosed";
  $("#govFacts").innerHTML = (g.facts || []).map((x) => `<li>${esc(x)}</li>`).join("");
  $("#officerGrid").innerHTML = (c.officers || [])
    .map((o) => {
      const badges = [];
      if (o.isCEO) badges.push("CEO");
      if (o.isCFO) badges.push("CFO");
      if (o.isChair) badges.push("CHAIR");
      const meta = [];
      if (Number.isFinite(o.age)) meta.push(`<span>age <b>${Math.round(o.age)}</b></span>`);
      if (Number.isFinite(o.pay)) meta.push(`<span>pay <b>${compact(o.pay, c.currency)}</b></span>`);
      return `
        <div class="officer">
          <span class="officer-name">${esc(o.name)}${badges.length ? ` <span class="sug-code">${badges.join(" · ")}</span>` : ""}</span>
          <p class="officer-title">${esc(o.title || "—")}</p>
          ${meta.length ? `<div class="officer-meta">${meta.join("")}</div>` : ""}
        </div>`;
    })
    .join("");

  $("#aboutText").textContent = c.description || "No company description is published in this data source.";
}

function renderEsgInto(container, checklist) {
  const order = ["Environment", "Social", "Governance"];
  const icons = { Environment: "#i-leaf", Social: "#i-users", Governance: "#i-shield" };
  container.innerHTML = order
    .map((pillar) => {
      const group = checklist.find((g) => g.pillar === pillar);
      if (!group) return "";
      return `
        <div class="esg-col">
          <h4><svg class="icon"><use href="${icons[pillar] || "#i-file"}"></use></svg>${esc(pillar.toUpperCase())}</h4>
          ${group.items
            .map(
              (i) => `
              <div class="esg-item">
                <b>${esc(i.label)}</b>
                <span>${esc(i.what)}</span>
                <em class="esg-where">${esc(i.where)}</em>
              </div>`
            )
            .join("")}
        </div>`;
    })
    .join("");
}

/* ---------------- compare ---------------- */
async function runCompare() {
  const raw = $("#compareInput").value.trim();
  const out = $("#compareOut");
  if (!raw) {
    out.innerHTML = `<p class="cmp-note">Enter at least one Bursa code.</p>`;
    return;
  }
  out.innerHTML = `<p class="cmp-note">Loading…</p>`;
  try {
    const res = await fetch(`/api/compare?symbols=${encodeURIComponent(raw)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data?.error || "Compare failed.");
    const companies = data.companies || [];
    if (!companies.length) {
      out.innerHTML = `<p class="cmp-note">No companies could be loaded.</p>`;
      return;
    }
    const rows = [
      ["Price", (c) => money(c.price?.last, c.currency)],
      ["Change", (c) => (Number.isFinite(c.price?.changePercent) ? `${(c.price.changePercent * 100).toFixed(2)}%` : "—")],
      ["Market cap", (c) => compact(c.price?.marketCap, c.currency)],
      ["P/E", (c) => ratio(c.financials?.pe)],
      ["Forward P/E", (c) => ratio(c.financials?.forwardPe)],
      ["Dividend yield", (c) => pct(c.financials?.dividendYield)],
      ["Payout ratio", (c) => pct(c.financials?.payoutRatio, 1)],
      ["Price / book", (c) => ratio(c.financials?.priceToBook)],
      ["ROE", (c) => pct(c.financials?.returnOnEquity, 1)],
      ["Net margin", (c) => pct(c.financials?.profitMargin, 1)],
      ["Revenue growth", (c) => pct(c.financials?.revenueGrowth, 1)],
      ["Beta", (c) => ratio(c.financials?.beta)],
      ["Sector", (c) => c.sector || "—"],
      ["Employees", (c) => (Number.isFinite(c.employees) ? c.employees.toLocaleString() : "—")],
    ];
    out.innerHTML = `
      <div class="cmp-wrap">
        <table class="cmp-table">
          <thead>
            <tr>
              <th>Metric</th>
              ${companies
                .map(
                  (c) => `<th><span class="cmp-name">${esc(c.name)}</span><span class="cmp-code">${esc(c.code)} · ${esc(c.symbol)}</span></th>`
                )
                .join("")}
            </tr>
          </thead>
          <tbody>
            ${rows
              .map(
                ([label, get]) => `
                <tr>
                  <th>${esc(label)}</th>
                  ${companies.map((c) => `<td>${esc(get(c))}</td>`).join("")}
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
      <p class="cmp-note">Fetched ${esc(fmtDate(new Date().toISOString()))}. Figures are as reported by the data source and may lag official filings.</p>`;
  } catch (err) {
    out.innerHTML = `<p class="cmp-note">${esc(err.message || "Compare failed.")}</p>`;
  }
}

/* ---------------- boot ---------------- */
async function boot() {
  try {
    const res = await fetch("/api/popular");
    const data = await res.json();
    $("#popular").innerHTML = (data.results || [])
      .slice(0, 7)
      .map((r) => `<button class="chip" data-code="${esc(r.code)}">${esc(r.code)} ${esc((r.name || "").split(" ")[0])}</button>`)      .join("");
  } catch {
    /* chips are optional */
  }

  try {
    const res = await fetch("/api/esg");
    const data = await res.json();
    const guide = $("#esgGuide");
    const icons = { Environment: "#i-leaf", Social: "#i-users", Governance: "#i-shield" };
    guide.innerHTML = (data.checklist || [])
      .map(
        (group) => `
        <div class="guide-col">
          <h3><svg class="icon"><use href="${icons[group.pillar] || "#i-file"}"></use></svg>${esc(group.pillar)}</h3>
          ${group.items
            .map(
              (i) => `
              <div class="guide-item">
                <b>${esc(i.label)}</b>
                <p>${esc(i.what)}</p>
                <span>${esc(i.where)}</span>
              </div>`
            )
            .join("")}
        </div>`
      )
      .join("");
  } catch {
    $("#esgGuide").innerHTML = `<p class="cmp-note">Could not load the ESG guide.</p>`;
  }

  const input = $("#q");
  input.addEventListener("input", (e) => onQueryInput(e.target.value));
  input.addEventListener("keydown", (e) => {
    const items = $$(".sug");
    if (e.key === "ArrowDown" && items.length) {
      e.preventDefault();
      state.sugIndex = Math.min(state.sugIndex + 1, items.length - 1);
      highlight(state.sugIndex);
    } else if (e.key === "ArrowUp" && items.length) {
      e.preventDefault();
      state.sugIndex = Math.max(state.sugIndex - 1, 0);
      highlight(state.sugIndex);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const pick = state.suggestions[state.sugIndex];
      loadCompany(pick ? pick.code : input.value.trim(), { scroll: true });
    } else if (e.key === "Escape") {
      hideSuggestions();
    }
  });

  $("#go").addEventListener("click", () => loadCompany(input.value.trim(), { scroll: true }));

  $("#suggestions").addEventListener("click", (e) => {
    const el = e.target.closest(".sug");
    if (!el) return;
    const pick = state.suggestions[Number(el.dataset.i)];
    if (pick) {
      input.value = `${pick.code} ${pick.name}`;
      loadCompany(pick.code, { scroll: true });
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) hideSuggestions();
  });

  $("#popular").addEventListener("click", (e) => {
    const btn = e.target.closest(".chip");
    if (!btn) return;
    input.value = btn.dataset.code;
    loadCompany(btn.dataset.code, { scroll: true });
  });

  $("#compareGo").addEventListener("click", runCompare);
  $("#compareInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runCompare();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== input && !e.target.closest("input")) {
      e.preventDefault();
      input.focus();
    }
  });

  window.addEventListener("resize", () => {
    if (state.company?.price?.history) drawSpark(state.company.price.history);
  });

  const code = new URL(location.href).searchParams.get("code");
  if (code) {
    input.value = code;
    loadCompany(code);
  }
}

boot();
