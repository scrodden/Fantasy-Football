// Shared client for every page. Each page sets <body data-page="..."> and the
// matching render function below draws it from the JSON files in ./data.
const PAGE_SIZE = 100;
const qs = new URLSearchParams(location.search);
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const today = new Date().toISOString().slice(0, 10);

async function load(...names) {
  const out = await Promise.all(names.map((n) => fetch(`data/${n}.json`).then((r) => r.json())));
  return Object.fromEntries(names.map((n, i) => [n, out[i]]));
}

function setParam(key, value) {
  const p = new URLSearchParams(location.search);
  if (value) p.set(key, value); else p.delete(key);
  history.replaceState(null, "", `${location.pathname}${p.toString() ? "?" + p : ""}`);
}

function bindFilter(el, key, onChange) {
  if (qs.get(key)) el.value = qs.get(key);
  el.addEventListener("input", () => { setParam(key, el.value); onChange(); });
}

function sectorOptions(select, sectors) {
  select.innerHTML = `<option value="">All sectors</option>` +
    sectors.map((s) => `<option>${esc(s)}</option>`).join("");
}

function fmtDate(d) {
  if (!d) return "—";
  const dt = new Date(d + "T12:00:00");
  return dt.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

function companyLink(c) {
  return c ? `<a href="company.html?id=${encodeURIComponent(c.id)}">${esc(c.n)}</a> <span class="tick">${esc(c.t[0] || "")}</span>` : "";
}

function dateRangeOk(d, range) {
  if (!range) return true;
  if (!d) return range === "undated";
  if (range === "upcoming") return d >= today;
  const days = { "30": 30, "90": 90, "365": 365 }[range];
  if (days) {
    const from = new Date(Date.now() - days * 864e5).toISOString().slice(0, 10);
    return d >= from && d <= today;
  }
  return d.startsWith(range); // a year, e.g. "2026"
}

// Paged table of webcasts with the standard filter set.
function webcastTable({ rows, companies, conferences, container, showCompany = true, showConference = true, filters = {} }) {
  const byId = Object.fromEntries(companies.map((c) => [c.id, c]));
  const confById = Object.fromEntries((conferences || []).map((c) => [c.id, c]));
  let limit = PAGE_SIZE;

  function matches(w) {
    const c = byId[w.c];
    const q = (filters.q?.value || "").trim().toLowerCase();
    if (filters.sector?.value && c?.s !== filters.sector.value) return false;
    if (filters.type?.value && w.ty !== filters.type.value) return false;
    if (filters.exchange?.value && c?.x !== filters.exchange.value) return false;
    if (!dateRangeOk(w.d, filters.range?.value)) return false;
    if (q) {
      const hay = `${w.ti} ${c?.n} ${c?.t.join(" ")} ${confById[w.cf]?.name || ""}`.toLowerCase();
      if (!q.split(/\s+/).every((t) => hay.includes(t))) return false;
    }
    return true;
  }

  function draw() {
    const hits = rows.filter(matches);
    const shown = hits.slice(0, limit);
    if (!hits.length) {
      container.innerHTML = `<div class="empty">No webcasts match these filters.</div>`;
      return;
    }
    container.innerHTML = `
      <div class="count">${hits.length.toLocaleString()} webcast${hits.length === 1 ? "" : "s"}</div>
      <table><thead><tr><th>Date</th>${showCompany ? "<th>Company</th>" : ""}<th>Event</th><th class="hide-sm">Sector</th></tr></thead><tbody>
      ${shown.map((w) => {
        const c = byId[w.c];
        const conf = confById[w.cf];
        return `<tr>
          <td class="date">${fmtDate(w.d)}${w.d && w.d >= today ? `<br><span class="chip upcoming">upcoming</span>` : ""}</td>
          ${showCompany ? `<td>${companyLink(c)}</td>` : ""}
          <td><a href="${esc(w.u)}" target="_blank" rel="noopener">${esc(w.ti)}</a><br>
            <span class="chip type-${esc(w.ty)}">${esc(w.ty)}</span>
            ${conf && showConference ? `<a class="chip" href="conference.html?id=${encodeURIComponent(conf.id)}">${esc(conf.name)}</a>` : ""}
            ${w.src ? `<a class="chip" href="${esc(w.src)}" target="_blank" rel="noopener">source</a>` : ""}</td>
          <td class="hide-sm"><span class="chip">${esc(c?.s || "")}</span></td>
        </tr>`;
      }).join("")}
      </tbody></table>
      ${hits.length > limit ? `<button class="more">Show more</button>` : ""}`;
    container.querySelector(".more")?.addEventListener("click", () => { limit += PAGE_SIZE; draw(); });
  }

  for (const [key, el] of Object.entries(filters)) if (el) bindFilter(el, key, () => { limit = PAGE_SIZE; draw(); });
  draw();
}

function yearOptions(select, rows) {
  const years = [...new Set(rows.map((w) => (w.d || "").slice(0, 4)).filter(Boolean))].sort().reverse();
  select.insertAdjacentHTML("beforeend", years.map((y) => `<option value="${y}">${y}</option>`).join(""));
}

const pages = {
  async webcasts() {
    const { webcasts, companies, conferences, meta } = await load("webcasts", "companies", "conferences", "meta");
    sectorOptions($("#sector"), meta.sectors);
    yearOptions($("#range"), webcasts);
    $("#stats").textContent = `${meta.webcasts.toLocaleString()} webcasts · ${meta.companies.toLocaleString()} companies · ${meta.conferences.toLocaleString()} conferences · updated ${meta.built}`;
    webcastTable({ rows: webcasts, companies, conferences, container: $("#list"),
      filters: { q: $("#q"), sector: $("#sector"), type: $("#type"), exchange: $("#exchange"), range: $("#range") } });
  },

  async companies() {
    const { companies, meta } = await load("companies", "meta");
    sectorOptions($("#sector"), meta.sectors);
    const list = $("#list");
    let limit = 200;
    function draw() {
      const q = $("#q").value.trim().toLowerCase();
      const s = $("#sector").value, x = $("#exchange").value, has = $("#has").value;
      const hits = companies.filter((c) => (!s || c.s === s) && (!x || c.x === x)
        && (!has || (has === "webcasts" ? c.w > 0 : has === "ir" ? !!c.ir : !c.ir))
        && (!q || `${c.n} ${c.t.join(" ")}`.toLowerCase().includes(q)));
      list.innerHTML = `<div class="count">${hits.length.toLocaleString()} companies</div>` + (hits.length ? `
        <table><thead><tr><th>Company</th><th class="hide-sm">Exchange</th><th>Sector</th><th>Webcasts</th></tr></thead><tbody>
        ${hits.slice(0, limit).map((c) => `<tr><td>${companyLink(c)}</td><td class="hide-sm">${esc(c.x || "")}</td>
          <td><span class="chip">${esc(c.s)}</span></td><td>${c.w || "—"}</td></tr>`).join("")}
        </tbody></table>${hits.length > limit ? `<button class="more">Show more</button>` : ""}` : `<div class="empty">No companies match.</div>`);
      list.querySelector(".more")?.addEventListener("click", () => { limit += 200; draw(); });
    }
    for (const id of ["q", "sector", "exchange", "has"]) bindFilter($("#" + id), id, () => { limit = 200; draw(); });
    draw();
  },

  async company() {
    const { companies, webcasts, conferences } = await load("companies", "webcasts", "conferences");
    const id = qs.get("id"), ticker = (qs.get("t") || "").toUpperCase();
    const c = companies.find((x) => x.id === id || (ticker && x.t.includes(ticker)));
    if (!c) { $("#head").innerHTML = `<h1>Company not found</h1>`; return; }
    document.title = `${c.n} webcasts`;
    const rows = webcasts.filter((w) => w.c === c.id);
    $("#head").innerHTML = `<h1>${esc(c.n)}</h1>
      <div class="facts"><span><b>${esc(c.t.join(", "))}</b> · ${esc(c.x || "")}</span>
      <span>Sector: <a href="index.html?sector=${encodeURIComponent(c.s)}"><b>${esc(c.s)}</b></a></span>
      ${c.i ? `<span>Industry: <b>${esc(c.i)}</b></span>` : ""}
      ${c.ir ? `<span><a href="${esc(c.ir)}" target="_blank" rel="noopener">Investor relations site ↗</a></span>` : `<span>IR site not found yet</span>`}</div>`;
    yearOptions($("#range"), rows);
    webcastTable({ rows, companies, conferences, container: $("#list"), showCompany: false,
      filters: { q: $("#q"), type: $("#type"), range: $("#range") } });
  },

  async conferences() {
    const { conferences, meta } = await load("conferences", "meta");
    sectorOptions($("#sector"), meta.sectors);
    const years = [...new Set(conferences.map((c) => c.year).filter(Boolean))].sort().reverse();
    $("#year").insertAdjacentHTML("beforeend", years.map((y) => `<option>${y}</option>`).join(""));
    function draw() {
      const q = $("#q").value.trim().toLowerCase(), s = $("#sector").value, y = $("#year").value;
      const min = +$("#min").value || 1;
      const hits = conferences.filter((c) => (!s || c.sectors[s]) && (!y || c.year === y)
        && c.company_count >= min && (!q || `${c.name} ${c.host || ""}`.toLowerCase().includes(q)));
      $("#list").innerHTML = `<div class="count">${hits.length.toLocaleString()} conferences</div>` + (hits.length ?
        `<div class="cards">${hits.map((c) => `<div class="card">
          <h3><a href="conference.html?id=${encodeURIComponent(c.id)}">${esc(c.name)}</a></h3>
          <div class="meta">${c.start ? fmtDate(c.start) + (c.end && c.end !== c.start ? " – " + fmtDate(c.end) : "") : esc(c.year || "")}
          ${c.host ? ` · Host: ${esc(c.host)}` : ""}</div>
          <div class="meta">${c.company_count} presenting compan${c.company_count === 1 ? "y" : "ies"}</div>
          <div>${Object.keys(c.sectors).slice(0, 3).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div>
        </div>`).join("")}</div>` : `<div class="empty">No conferences match.</div>`);
    }
    for (const id of ["q", "sector", "year", "min"]) bindFilter($("#" + id), id, draw);
    draw();
  },

  async conference() {
    const { conferences, webcasts, companies, meta } = await load("conferences", "webcasts", "companies", "meta");
    const conf = conferences.find((c) => c.id === qs.get("id"));
    if (!conf) { $("#head").innerHTML = `<h1>Conference not found</h1>`; return; }
    document.title = conf.name;
    const rows = webcasts.filter((w) => w.cf === conf.id).sort((a, b) => (a.d || "").localeCompare(b.d || ""));
    $("#head").innerHTML = `<h1>${esc(conf.name)}</h1>
      <div class="facts">${conf.start ? `<span><b>${fmtDate(conf.start)}${conf.end && conf.end !== conf.start ? " – " + fmtDate(conf.end) : ""}</b></span>` : ""}
      ${conf.host ? `<span>Host: <b>${esc(conf.host)}</b></span>` : ""}
      <span><b>${conf.company_count}</b> presenting companies</span></div>`;
    sectorOptions($("#sector"), meta.sectors.filter((s) => conf.sectors[s]));
    webcastTable({ rows, companies, conferences, container: $("#list"), showConference: false,
      filters: { q: $("#q"), sector: $("#sector") } });
  },
};

document.querySelectorAll("nav a").forEach((a) => {
  if (a.getAttribute("href") === location.pathname.split("/").pop() || (a.getAttribute("href") === "index.html" && !location.pathname.split("/").pop())) a.classList.add("on");
});
pages[document.body.dataset.page]().catch((err) => {
  $("#list").innerHTML = `<div class="empty">Could not load data (${esc(err.message)}). Run <code>python -m irwebcasts build</code> and serve this folder over HTTP.</div>`;
});
