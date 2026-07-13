// awareness — research workbench
// Vanilla ES module. Router + 5 views + command palette + reader + live feed
// + keyboard navigation + a11y. All dynamic DOM via createElement+textContent
// so server values never reach an HTML parser.

const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => Array.from(root.querySelectorAll(q));

// ── DOM builder ───────────────────────────────────────────────
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") {} // intentionally unsupported
      else if (k === "dataset") for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
      else if (k === "style") Object.assign(node.style, v);
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "href") {
        const href = safeHrefAttribute(v);
        if (href) node.setAttribute(k, href);
      }
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

// ── Helpers ───────────────────────────────────────────────────
const fmt = (n) => (n == null ? "—" : new Intl.NumberFormat("en-US").format(n));
const ago = (iso, short = true) => {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return String(iso);
  const d = Math.max(0, (Date.now() - t) / 1000);
  if (d < 60) return short ? Math.max(1, Math.round(d)) + "s" : Math.max(1, Math.round(d)) + "s ago";
  if (d < 3600) return short ? Math.round(d / 60) + "m" : Math.round(d / 60) + "m ago";
  if (d < 86400) return short ? Math.round(d / 3600) + "h" : Math.round(d / 3600) + "h ago";
  return short ? Math.round(d / 86400) + "d" : Math.round(d / 86400) + "d ago";
};
const isoDay = (d) => new Date(d).toISOString().slice(0, 10);

/** Human label for API search mode. Pure — no DOM. */
function formatSearchModeLabel(mode, ranked = false) {
  const m = String(mode || "").toLowerCase();
  if (m === "fts") return ranked ? "FTS · ranked" : "FTS";
  if (m === "prefix") return "prefix fallback";
  if (m === "substring") return "substring";
  // Quoted whole-query search (API mode=phrase after C2-T18).
  if (m === "phrase") return "exact phrase";
  if (m === "auto") return ranked ? "auto · FTS ranked" : "auto";
  if (ranked) return "FTS · ranked";
  return m || "search";
}

function isHttpUrl(url) {
  return url.protocol === "http:" || url.protocol === "https:";
}

function safeHrefAttribute(value) {
  try {
    const raw = String(value);
    return isHttpUrl(new URL(raw, window.location.href)) ? raw : null;
  } catch (_) {
    return null;
  }
}

function safeOutboundHref(value) {
  try {
    const url = new URL(String(value));
    return isHttpUrl(url) ? url.href : null;
  } catch (_) {
    return null;
  }
}

// ── Toast ─────────────────────────────────────────────────────
let toastTimer;
function toast(msg, kind = "ok") {
  const t = $("#toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3400);
}

// ── API ───────────────────────────────────────────────────────
// Connection health: if N consecutive fetches fail with a network error
// (server down, CORS, DNS, etc.) show a persistent offline banner; clear
// it as soon as one fetch succeeds.
const apiHealth = { failures: 0, threshold: 2, offline: false };

function setOffline(offline, reason) {
  if (offline === apiHealth.offline) return;
  apiHealth.offline = offline;
  const banner = $("#api-offline");
  if (!banner) return;
  if (offline) {
    banner.hidden = false;
    const msg = banner.querySelector(".api-offline-msg");
    if (msg) msg.textContent = "API unreachable — start it with `awareness-api`, or check the port.";
    const why = banner.querySelector(".api-offline-why");
    if (why) why.textContent = reason || "";
  } else {
    banner.hidden = true;
  }
}

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
  } catch (netErr) {
    // Network-level failure: server down, CORS, DNS. fetch() rejects with
    // a TypeError "Failed to fetch" — not an HTTP status we can read.
    apiHealth.failures += 1;
    if (apiHealth.failures >= apiHealth.threshold) {
      setOffline(true, netErr.message || "network error");
    }
    throw new Error("API unreachable: " + (netErr.message || "network error"));
  }
  if (!res.ok) {
    // We DID reach the server; not an "offline" condition.
    apiHealth.failures = 0;
    setOffline(false);
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(res.status + " " + detail);
  }
  // Healthy response — clear any offline state.
  apiHealth.failures = 0;
  setOffline(false);
  if (res.status === 204) return null;
  return await res.json();
}

// ── KPI animation (count-up) ──────────────────────────────────
const kpiState = new Map();
function setKPI(id, target, opts = {}) {
  const node = $("#" + id);
  if (!node) return;
  const prev = kpiState.get(id) ?? 0;
  if (target === prev) {
    node.textContent = fmt(target);
    node.classList.toggle("is-zero", target === 0);
    return;
  }
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    node.textContent = fmt(target);
    kpiState.set(id, target);
    node.classList.toggle("is-zero", target === 0);
    return;
  }
  const start = performance.now();
  const dur = 700;
  function frame(now) {
    const k = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    const v = Math.round(prev + (target - prev) * eased);
    node.textContent = fmt(v);
    if (k < 1) requestAnimationFrame(frame);
    else { kpiState.set(id, target); node.classList.toggle("is-zero", target === 0); }
  }
  requestAnimationFrame(frame);
}

// ── Router ────────────────────────────────────────────────────
const ROUTES = ["dashboard", "captures", "work", "jobs", "tail", "settings"];
let currentRoute = "dashboard";
function navigate(route, { push = true } = {}) {
  if (!ROUTES.includes(route)) route = "dashboard";
  currentRoute = route;
  $$(".view").forEach((v) => {
    const match = v.dataset.view === route;
    v.toggleAttribute("hidden", !match);
    v.classList.toggle("is-active", match);
  });
  $$(".nav-item").forEach((n) => {
    const match = n.dataset.route === route;
    if (match) n.setAttribute("aria-current", "page");
    else n.removeAttribute("aria-current");
  });
  // Hide rail except on dashboard.
  $(".app").classList.toggle("no-rail", route !== "dashboard");

  if (push) history.pushState({ route }, "", "#" + route);
  // Move keyboard focus to main heading for screen readers.
  setTimeout(() => $("#main").focus({ preventScroll: false }), 50);

  // Lazy-load views' data on activation.
  if (route === "captures") void loadCaptures(true);
  if (route === "work") void initWork();
  if (route === "jobs") void loadJobs();
  if (route === "tail") startTailPolling();
  if (route === "settings") void loadSettings();
}
window.addEventListener("popstate", (e) => {
  const r = (location.hash || "#dashboard").slice(1);
  navigate(r, { push: false });
});

// ── Header / Dashboard refresh ────────────────────────────────
let lastFeedCaptureId = null;

/**
 * Summarize process HTTP fetch metrics from GET /metrics snapshot.
 * Pure — no DOM. Aggregates http.fetch_seconds histograms (all outcomes)
 * and http.fetch_attempts / http.fetch_retries counters.
 */
function summarizeHttpFetchMetrics(metricsSnap) {
  const empty = { p95Sec: null, count: 0, attempts: 0, retries: 0 };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let weightedP95 = 0;
  let totalCount = 0;
  let maxP95 = 0;
  for (const h of hists) {
    if (!h || h.name !== "http.fetch_seconds") continue;
    const c = Number(h.count) || 0;
    if (c <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    totalCount += c;
    weightedP95 += p95 * c;
    if (p95 > maxP95) maxP95 = p95;
  }
  // Prefer count-weighted average of per-series p95; fall back to max when empty math.
  const p95Sec = totalCount > 0 ? weightedP95 / totalCount : null;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let attempts = 0;
  let retries = 0;
  for (const c of counters) {
    if (!c) continue;
    if (c.name === "http.fetch_attempts") attempts += Number(c.value) || 0;
    if (c.name === "http.fetch_retries") retries += Number(c.value) || 0;
  }
  return {
    p95Sec: p95Sec != null ? p95Sec : (totalCount > 0 ? maxP95 : null),
    count: totalCount,
    attempts,
    retries,
  };
}

/** Format seconds as ms when small, else seconds — pure helper for KPIs. */
function formatFetchLatency(sec) {
  if (sec == null || !Number.isFinite(sec)) return "—";
  if (sec < 1) return Math.round(sec * 1000) + "ms";
  if (sec < 10) return sec.toFixed(2) + "s";
  return sec.toFixed(1) + "s";
}

/**
 * Discovery + tail fetch counters from /metrics (process-local).
 * Pure — no DOM. Sums feeds/GDELT URL discovery and tail recrawl fetches.
 * Also aggregates feed health (non-200, retries, charset, sitemaps).
 */
function summarizeDiscoveryMetrics(metricsSnap) {
  const empty = {
    feedsUrls: 0,
    gdeltUrls: 0,
    gdeltEnqueued: 0,
    gdeltFetchOk: 0,
    gdeltFetchAttempts: 0,
    gdeltFetchP95: null,
    tailFetches: 0,
    discovered: 0,
    feedNon200: 0,
    feedRetryable: 0,
    feedCharset: 0,
    feedSitemaps: 0,
    feedErrors: 0,
    feedFetchAttempts: 0,
    feedFetchOk: 0,
    feedFetchP95: null,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let feedsUrls = 0;
  let gdeltUrls = 0;
  let gdeltEnqueued = 0;
  let gdeltFetchOk = 0;
  let gdeltFetchAttempts = 0;
  let tailFetches = 0;
  let feedNon200 = 0;
  let feedRetryable = 0;
  let feedCharset = 0;
  let feedSitemaps = 0;
  let feedFetchAttempts = 0;
  let feedFetchOk = 0;
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    if (n === "feeds.urls_discovered") feedsUrls += v;
    else if (n === "gdelt.urls_discovered") gdeltUrls += v;
    else if (n === "gdelt.urls_enqueued") gdeltEnqueued += v;
    else if (n === "gdelt.fetch_attempts") {
      gdeltFetchAttempts += v;
      const labels = c.labels || {};
      if (labels.outcome === "ok") gdeltFetchOk += v;
    } else if (n === "tail.fetches") tailFetches += v;
    else if (n === "feeds.fetch_non_200") feedNon200 += v;
    else if (n === "feeds.retryable_http_error") feedRetryable += v;
    else if (n === "feeds.decode_charset") feedCharset += v;
    else if (n === "feeds.robots_sitemaps_discovered") feedSitemaps += v;
    else if (n === "feeds.fetch_attempts") {
      feedFetchAttempts += v;
      const labels = c.labels || {};
      if (labels.outcome === "ok") feedFetchOk += v;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let feedWeightedP95 = 0;
  let feedHistCount = 0;
  let gdeltWeightedP95 = 0;
  let gdeltHistCount = 0;
  for (const h of hists) {
    if (!h) continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    if (h.name === "feeds.fetch_seconds") {
      feedHistCount += n;
      feedWeightedP95 += p95 * n;
    } else if (h.name === "gdelt.fetch_seconds") {
      gdeltHistCount += n;
      gdeltWeightedP95 += p95 * n;
    }
  }
  return {
    feedsUrls,
    gdeltUrls,
    gdeltEnqueued,
    gdeltFetchOk,
    gdeltFetchAttempts,
    gdeltFetchP95: gdeltHistCount > 0 ? gdeltWeightedP95 / gdeltHistCount : null,
    tailFetches,
    discovered: feedsUrls + gdeltUrls,
    feedNon200,
    feedRetryable,
    feedCharset,
    feedSitemaps,
    feedErrors: feedNon200 + feedRetryable,
    feedFetchAttempts,
    feedFetchOk,
    feedFetchP95: feedHistCount > 0 ? feedWeightedP95 / feedHistCount : null,
  };
}

/**
 * Format a duration in seconds for staging age KPIs (compact, operator-facing).
 * Pure — no DOM. Mirrors CLI-ish age (s / m / h / d).
 */
function formatAgeSeconds(sec) {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return Math.max(0, Math.round(sec)) + "s";
  if (sec < 3600) return Math.round(sec / 60) + "m";
  if (sec < 86400) {
    const h = Math.floor(sec / 3600);
    const m = Math.round((sec % 3600) / 60);
    return m ? `${h}h${m}m` : `${h}h`;
  }
  const d = Math.floor(sec / 86400);
  const h = Math.round((sec % 86400) / 3600);
  return h ? `${d}d${h}h` : `${d}d`;
}

/**
 * Normalize GET /staging summary for dashboard KPIs. Pure — no DOM.
 */
function summarizeStagingBacklog(stagingSnap) {
  const empty = {
    pendingCount: 0,
    totalRecords: 0,
    totalBytes: 0,
    oldestAgeSeconds: null,
  };
  if (!stagingSnap || typeof stagingSnap !== "object") return empty;
  const pendingCount = Number(stagingSnap.pending_count) || 0;
  const totalRecords = Number(stagingSnap.total_records) || 0;
  const totalBytes = Number(stagingSnap.total_bytes) || 0;
  let oldestAgeSeconds = null;
  const age = Number(stagingSnap.oldest_age_seconds);
  if (Number.isFinite(age) && age >= 0) oldestAgeSeconds = age;
  return { pendingCount, totalRecords, totalBytes, oldestAgeSeconds };
}

/**
 * Common Crawl WET quality filter counters from /metrics (process-local).
 * Pure — no DOM. Sums quality drops (any reason) and admitted records.
 */
function summarizeWetQualityMetrics(metricsSnap) {
  const empty = { filtered: 0, admitted: 0, topReason: null };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let filtered = 0;
  let admitted = 0;
  const byReason = Object.create(null);
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    if (n === "cc_wet.quality_filtered") {
      filtered += v;
      const reason = (c.labels && c.labels.reason) || "unknown";
      byReason[reason] = (byReason[reason] || 0) + v;
    } else if (n === "cc_wet.records_admitted") {
      admitted += v;
    }
  }
  let topReason = null;
  let topVal = 0;
  for (const [r, v] of Object.entries(byReason)) {
    if (v > topVal) {
      topVal = v;
      topReason = r;
    }
  }
  return { filtered, admitted, topReason };
}

/**
 * Common Crawl WET shard download + parse latency from /metrics (process-local).
 * Pure — no DOM. Sums records seen/emitted, download attempts by outcome, and
 * weighted p95 for shard parse / download histograms.
 */
function summarizeWetParseMetrics(metricsSnap) {
  const empty = {
    recordsSeen: 0,
    parseEmitted: 0,
    downloadAttempts: 0,
    downloadCacheHits: 0,
    downloadOk: 0,
    downloadP95: null,
    parseP95: null,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let recordsSeen = 0;
  let parseEmitted = 0;
  let downloadAttempts = 0;
  let downloadCacheHits = 0;
  let downloadOk = 0;
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    if (n === "cc_wet.records_seen") recordsSeen += v;
    else if (n === "cc_wet.shard_parse_emitted") parseEmitted += v;
    else if (n === "cc_wet.shard_download_attempts") {
      downloadAttempts += v;
      const outcome = (c.labels && c.labels.outcome) || "";
      if (outcome === "cache_hit") downloadCacheHits += v;
      if (outcome === "ok" || outcome === "cache_hit") downloadOk += v;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let parseWeighted = 0;
  let parseCount = 0;
  let dlWeighted = 0;
  let dlCount = 0;
  for (const h of hists) {
    if (!h) continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    if (h.name === "cc_wet.shard_parse_seconds" || h.name === "cc_wet.iter_parse_seconds") {
      parseCount += n;
      parseWeighted += p95 * n;
    } else if (h.name === "cc_wet.shard_download_seconds") {
      dlCount += n;
      dlWeighted += p95 * n;
    }
  }
  return {
    recordsSeen,
    parseEmitted,
    downloadAttempts,
    downloadCacheHits,
    downloadOk,
    downloadP95: dlCount > 0 ? dlWeighted / dlCount : null,
    parseP95: parseCount > 0 ? parseWeighted / parseCount : null,
  };
}

/**
 * FineWeb HF stream counters + load latency from /metrics (process-local).
 * Pure — no DOM. Sums admitted/filtered/seen rows, load attempts, and weighted
 * load p95 across dataset labels.
 */
function summarizeFinewebMetrics(metricsSnap) {
  const empty = {
    admitted: 0,
    filtered: 0,
    seen: 0,
    topReason: null,
    loadAttempts: 0,
    loadOk: 0,
    loadP95: null,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let admitted = 0;
  let filtered = 0;
  let seen = 0;
  let loadAttempts = 0;
  let loadOk = 0;
  const byReason = Object.create(null);
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    if (n === "fineweb.rows_admitted") admitted += v;
    else if (n === "fineweb.rows_seen") seen += v;
    else if (n === "fineweb.rows_filtered") {
      filtered += v;
      const reason = (c.labels && c.labels.reason) || "unknown";
      byReason[reason] = (byReason[reason] || 0) + v;
    } else if (n === "fineweb.load_attempts") {
      loadAttempts += v;
      const labels = c.labels || {};
      if (labels.outcome === "ok") loadOk += v;
    }
  }
  let topReason = null;
  let topVal = 0;
  for (const [r, v] of Object.entries(byReason)) {
    if (v > topVal) {
      topVal = v;
      topReason = r;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let weightedP95 = 0;
  let histCount = 0;
  for (const h of hists) {
    if (!h || h.name !== "fineweb.load_seconds") continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    histCount += n;
    weightedP95 += p95 * n;
  }
  return {
    admitted,
    filtered,
    seen,
    topReason,
    loadAttempts,
    loadOk,
    loadP95: histCount > 0 ? weightedP95 / histCount : null,
  };
}

/**
 * Pull robots cache hit ratio + robots network fetch + Iceberg/JSONL counters
 * from /metrics. Pure — no DOM. Gauges for robots hit ratio; counters/histograms
 * for network robots probes and storage writes.
 */
function summarizeStorageObsMetrics(metricsSnap) {
  const empty = {
    robotsHitRatio: null,
    robotsResolutions: 0,
    robotsFetchAttempts: 0,
    robotsFetchOk: 0,
    robotsFetchP95: null,
    icebergRows: 0,
    icebergBatches: 0,
    icebergAppendP95: null,
    icebergCompactedRows: 0,
    icebergCompactManifests: 0,
    icebergCompactOk: 0,
    icebergCompactP95: null,
    jsonlRecords: 0,
    jsonlChunks: 0,
    jsonlCommitP95: null,
    jsonlSyncs: 0,
    jsonlSyncOk: 0,
    jsonlSyncP95: null,
    jsonlOrphansRecovered: 0,
    jsonlOrphansRemoved: 0,
    jsonlOpenRecords: 0,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const gauges = Array.isArray(metricsSnap.gauges) ? metricsSnap.gauges : [];
  let robotsHitRatio = null;
  let robotsResolutions = 0;
  let jsonlOpenRecords = 0;
  for (const g of gauges) {
    if (!g) continue;
    if (g.name === "robots.cache.hit_ratio") {
      const v = Number(g.value);
      if (Number.isFinite(v)) robotsHitRatio = v;
    }
    if (g.name === "robots.cache.resolutions") {
      const v = Number(g.value);
      if (Number.isFinite(v)) robotsResolutions = v;
    }
    if (g.name === "jsonl.open_records") {
      const v = Number(g.value);
      if (Number.isFinite(v)) jsonlOpenRecords = v;
    }
  }
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let icebergRows = 0;
  let icebergBatches = 0;
  let icebergCompactedRows = 0;
  let icebergCompactManifests = 0;
  let icebergCompactOk = 0;
  let jsonlRecords = 0;
  let jsonlChunks = 0;
  let jsonlSyncs = 0;
  let jsonlSyncOk = 0;
  let jsonlOrphansRecovered = 0;
  let jsonlOrphansRemoved = 0;
  let robotsFetchAttempts = 0;
  let robotsFetchOk = 0;
  for (const c of counters) {
    if (!c) continue;
    if (c.name === "iceberg.appended_rows") icebergRows += Number(c.value) || 0;
    if (c.name === "iceberg.append_batches") icebergBatches += Number(c.value) || 0;
    if (c.name === "iceberg.compacted_rows") icebergCompactedRows += Number(c.value) || 0;
    if (c.name === "iceberg.compact_manifests") {
      const v = Number(c.value) || 0;
      icebergCompactManifests += v;
      const labels = c.labels || {};
      if (labels.outcome === "ok" || labels.outcome === "empty") {
        icebergCompactOk += v;
      }
    }
    if (c.name === "jsonl.records_committed") jsonlRecords += Number(c.value) || 0;
    if (c.name === "jsonl.chunks_committed") jsonlChunks += Number(c.value) || 0;
    if (c.name === "jsonl.syncs") {
      const v = Number(c.value) || 0;
      jsonlSyncs += v;
      const labels = c.labels || {};
      if (labels.outcome === "ok") jsonlSyncOk += v;
    }
    if (c.name === "jsonl.orphans_recovered") jsonlOrphansRecovered += Number(c.value) || 0;
    if (c.name === "jsonl.orphans_removed") jsonlOrphansRemoved += Number(c.value) || 0;
    if (c.name === "robots.fetch_attempts") {
      const v = Number(c.value) || 0;
      robotsFetchAttempts += v;
      const labels = c.labels || {};
      // Successful policy resolution paths (body present, missing, or forbid-all).
      if (labels.outcome === "ok" || labels.outcome === "missing" || labels.outcome === "forbidden") {
        robotsFetchOk += v;
      }
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let weightedP95 = 0;
  let histCount = 0;
  let jsonlWeightedP95 = 0;
  let jsonlHistCount = 0;
  let jsonlSyncWeightedP95 = 0;
  let jsonlSyncHistCount = 0;
  let robotsWeightedP95 = 0;
  let robotsHistCount = 0;
  let compactWeightedP95 = 0;
  let compactHistCount = 0;
  for (const h of hists) {
    if (!h) continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    if (h.name === "iceberg.append_seconds") {
      histCount += n;
      weightedP95 += p95 * n;
    } else if (h.name === "jsonl.commit_seconds") {
      jsonlHistCount += n;
      jsonlWeightedP95 += p95 * n;
    } else if (h.name === "jsonl.sync_seconds") {
      jsonlSyncHistCount += n;
      jsonlSyncWeightedP95 += p95 * n;
    } else if (h.name === "robots.fetch_seconds") {
      robotsHistCount += n;
      robotsWeightedP95 += p95 * n;
    } else if (h.name === "iceberg.compact_seconds") {
      compactHistCount += n;
      compactWeightedP95 += p95 * n;
    }
  }
  return {
    robotsHitRatio,
    robotsResolutions,
    robotsFetchAttempts,
    robotsFetchOk,
    robotsFetchP95: robotsHistCount > 0 ? robotsWeightedP95 / robotsHistCount : null,
    icebergRows,
    icebergBatches,
    icebergAppendP95: histCount > 0 ? weightedP95 / histCount : null,
    icebergCompactedRows,
    icebergCompactManifests,
    icebergCompactOk,
    icebergCompactP95: compactHistCount > 0 ? compactWeightedP95 / compactHistCount : null,
    jsonlRecords,
    jsonlChunks,
    jsonlCommitP95: jsonlHistCount > 0 ? jsonlWeightedP95 / jsonlHistCount : null,
    jsonlSyncs,
    jsonlSyncOk,
    jsonlSyncP95: jsonlSyncHistCount > 0 ? jsonlSyncWeightedP95 / jsonlSyncHistCount : null,
    jsonlOrphansRecovered,
    jsonlOrphansRemoved,
    jsonlOpenRecords,
  };
}

/** Format 0–1 ratio as percent string. */
function formatHitRatio(ratio) {
  if (ratio == null || !Number.isFinite(ratio)) return "—";
  return Math.round(ratio * 100) + "%";
}

async function refreshDashboard() {
  let status, dedup, metricsSnap, stagingSnap;
  try {
    [status, dedup, metricsSnap, stagingSnap] = await Promise.all([
      api("/status"),
      api("/dedup-stats"),
      api("/metrics").catch(() => null),
      // Lightweight backlog summary (no per-manifest list) for fold lag KPIs.
      api("/staging?include_manifests=false").catch(() => null),
    ]);
  } catch (e) { console.error(e); return; }

  const tail = status.tail || {};
  const jobsTotal = (status.jobs || []).length;
  const docsTotal = (status.jobs || []).reduce((a, j) => a + (j.docs_emitted || 0), 0);

  // Corpus KPIs: folds = stored re-captures collapsed by content hash
  // (not fetch-gate skips or tight near-dup pre-store drops — those are process counters below).
  const captures = dedup.total_captures_seen || 0;
  const distinct = dedup.distinct_content_hashes || 0;
  const folds = Math.max(0, captures - distinct);
  setKPI("kpi-captures", captures);
  setKPI("kpi-distinct", distinct);
  setKPI("kpi-folds", folds);
  setKPI("kpi-jobs", jobsTotal);
  // Process-local skip counters (same source as Settings Runtime status).
  setKPI("kpi-dash-fetch-skipped", Number(dedup.fetch_skipped_seen || 0));
  setKPI("kpi-dash-tight-near", Number(dedup.tight_near_skipped || 0));

  // HTTP fetch observability (shared get_with_retries path).
  const http = summarizeHttpFetchMetrics(metricsSnap);
  const p95Node = $("#kpi-dash-http-p95");
  if (p95Node) {
    // setKPI expects numeric targets; write formatted text directly for latency.
    p95Node.textContent = formatFetchLatency(http.p95Sec);
    p95Node.classList.toggle("is-zero", !http.count);
  }
  setKPI("kpi-dash-http-attempts", http.attempts);
  const p95Sub = $("#kpi-dash-http-p95-sub");
  if (p95Sub) {
    p95Sub.textContent = http.count
      ? `${fmt(http.count)} samples · process GET`
      : "no samples yet";
  }
  const attSub = $("#kpi-dash-http-attempts-sub");
  if (attSub) {
    attSub.textContent = http.retries
      ? `${fmt(http.retries)} retries`
      : "retries included";
  }

  // Robots cache + network robots fetch + Iceberg append observability (process-local).
  const storageObs = summarizeStorageObsMetrics(metricsSnap);
  const robotsNode = $("#kpi-dash-robots-hit");
  if (robotsNode) {
    robotsNode.textContent = formatHitRatio(storageObs.robotsHitRatio);
    robotsNode.classList.toggle("is-zero", !storageObs.robotsResolutions);
  }
  const robotsSub = $("#kpi-dash-robots-hit-sub");
  if (robotsSub) {
    robotsSub.textContent = storageObs.robotsResolutions
      ? `${fmt(storageObs.robotsResolutions)} resolutions`
      : "no robots lookups yet";
  }
  const robotsP95Node = $("#kpi-dash-robots-p95");
  if (robotsP95Node) {
    robotsP95Node.textContent = formatFetchLatency(storageObs.robotsFetchP95);
    robotsP95Node.classList.toggle("is-zero", !storageObs.robotsFetchAttempts);
  }
  const robotsP95Sub = $("#kpi-dash-robots-p95-sub");
  if (robotsP95Sub) {
    robotsP95Sub.textContent = storageObs.robotsFetchAttempts
      ? `${fmt(storageObs.robotsFetchOk)}/${fmt(storageObs.robotsFetchAttempts)} ok · network`
      : "no robots network probes yet";
  }
  setKPI("kpi-dash-robots-attempts", storageObs.robotsFetchAttempts);
  const robotsAttSub = $("#kpi-dash-robots-attempts-sub");
  if (robotsAttSub) {
    robotsAttSub.textContent = storageObs.robotsFetchAttempts
      ? `${fmt(storageObs.robotsFetchOk)} resolved`
      : "network probes this process";
  }
  setKPI("kpi-dash-iceberg-rows", storageObs.icebergRows);
  const iceSub = $("#kpi-dash-iceberg-rows-sub");
  if (iceSub) {
    const p95 = formatFetchLatency(storageObs.icebergAppendP95);
    iceSub.textContent = storageObs.icebergBatches
      ? `${fmt(storageObs.icebergBatches)} batches · p95 ${p95}`
      : "appended this process";
  }
  // Iceberg compact (warehouse fold of JSONL staging → table).
  const compactP95Node = $("#kpi-dash-compact-p95");
  if (compactP95Node) {
    compactP95Node.textContent = formatFetchLatency(storageObs.icebergCompactP95);
    compactP95Node.classList.toggle("is-zero", !storageObs.icebergCompactManifests);
  }
  const compactP95Sub = $("#kpi-dash-compact-p95-sub");
  if (compactP95Sub) {
    compactP95Sub.textContent = storageObs.icebergCompactManifests
      ? `${fmt(storageObs.icebergCompactOk)}/${fmt(storageObs.icebergCompactManifests)} ok · fold`
      : "no compact runs this process";
  }
  setKPI("kpi-dash-compact-rows", storageObs.icebergCompactedRows);
  const compactRowsSub = $("#kpi-dash-compact-rows-sub");
  if (compactRowsSub) {
    compactRowsSub.textContent = storageObs.icebergCompactManifests
      ? `${fmt(storageObs.icebergCompactManifests)} manifests folded`
      : "rows folded this process";
  }
  setKPI("kpi-dash-jsonl-records", storageObs.jsonlRecords);
  const jsonlSub = $("#kpi-dash-jsonl-records-sub");
  if (jsonlSub) {
    const p95 = formatFetchLatency(storageObs.jsonlCommitP95);
    jsonlSub.textContent = storageObs.jsonlChunks
      ? `${fmt(storageObs.jsonlChunks)} chunks · p95 ${p95}`
      : "committed this process";
  }

  // Discovery firehose (feeds + GDELT) and tail recrawl fetch volume.
  const discovery = summarizeDiscoveryMetrics(metricsSnap);
  setKPI("kpi-dash-discover", discovery.discovered);
  const discSub = $("#kpi-dash-discover-sub");
  if (discSub) {
    if (discovery.discovered || discovery.gdeltFetchAttempts || discovery.feedSitemaps) {
      const bits = [];
      if (discovery.feedsUrls) bits.push(`${fmt(discovery.feedsUrls)} feeds`);
      if (discovery.gdeltUrls) bits.push(`${fmt(discovery.gdeltUrls)} gdelt`);
      if (discovery.gdeltFetchOk || discovery.gdeltFetchAttempts) {
        bits.push(
          `${fmt(discovery.gdeltFetchOk)}/${fmt(discovery.gdeltFetchAttempts)} slots ok`
        );
      }
      if (discovery.feedSitemaps) bits.push(`${fmt(discovery.feedSitemaps)} sitemaps`);
      if (discovery.feedCharset) bits.push(`${fmt(discovery.feedCharset)} charset`);
      discSub.textContent = bits.length ? bits.join(" · ") : "feeds + GDELT this process";
    } else {
      discSub.textContent = "feeds + GDELT this process";
    }
  }
  setKPI("kpi-dash-tail-fetches", discovery.tailFetches);
  const tailFetchSub = $("#kpi-dash-tail-fetches-sub");
  if (tailFetchSub) {
    tailFetchSub.textContent = discovery.tailFetches
      ? "recrawl HTTP GETs this process"
      : "no recrawl fetches yet";
  }

  // Feed fetch health (non-200 + retryable errors).
  setKPI("kpi-dash-feed-errors", discovery.feedErrors);
  const feedErrSub = $("#kpi-dash-feed-errors-sub");
  if (feedErrSub) {
    if (discovery.feedErrors) {
      const bits = [];
      if (discovery.feedNon200) bits.push(`${fmt(discovery.feedNon200)} non-200`);
      if (discovery.feedRetryable) bits.push(`${fmt(discovery.feedRetryable)} retryable`);
      feedErrSub.textContent = bits.join(" · ");
    } else {
      feedErrSub.textContent = "non-200 + retries this process";
    }
  }

  // Feed/sitemap discovery fetch latency + attempts (process-local).
  const feedP95Node = $("#kpi-dash-feed-p95");
  if (feedP95Node) {
    feedP95Node.textContent = formatFetchLatency(discovery.feedFetchP95);
    feedP95Node.classList.toggle("is-zero", !discovery.feedFetchAttempts);
  }
  const feedP95Sub = $("#kpi-dash-feed-p95-sub");
  if (feedP95Sub) {
    feedP95Sub.textContent = discovery.feedFetchAttempts
      ? `${fmt(discovery.feedFetchOk)}/${fmt(discovery.feedFetchAttempts)} ok · process`
      : "no feed fetches yet";
  }
  setKPI("kpi-dash-feed-attempts", discovery.feedFetchAttempts);
  const feedAttSub = $("#kpi-dash-feed-attempts-sub");
  if (feedAttSub) {
    feedAttSub.textContent = discovery.feedFetchAttempts
      ? `${fmt(discovery.feedFetchOk)} succeeded`
      : "discovery GETs this process";
  }

  // WET Gopher/C4 quality filter (process-local).
  const wetQ = summarizeWetQualityMetrics(metricsSnap);
  setKPI("kpi-dash-wet-quality", wetQ.filtered);
  const wetSub = $("#kpi-dash-wet-quality-sub");
  if (wetSub) {
    if (wetQ.filtered || wetQ.admitted) {
      const bits = [`${fmt(wetQ.admitted)} admitted`];
      if (wetQ.topReason) bits.push(`top: ${wetQ.topReason}`);
      wetSub.textContent = bits.join(" · ");
    } else {
      wetSub.textContent = "Gopher/C4 filter this process";
    }
  }

  // JSONL staging backlog age (GET /staging — warehouse fold lag).
  const staging = summarizeStagingBacklog(stagingSnap);
  setKPI("kpi-dash-staging-pending", staging.pendingCount);
  const stagingPendingSub = $("#kpi-dash-staging-pending-sub");
  if (stagingPendingSub) {
    stagingPendingSub.textContent = staging.pendingCount
      ? `${fmt(staging.totalRecords)} rows · fold lag`
      : "caught up · no pending manifests";
  }
  const stagingAgeNode = $("#kpi-dash-staging-age");
  if (stagingAgeNode) {
    stagingAgeNode.textContent = staging.pendingCount
      ? formatAgeSeconds(staging.oldestAgeSeconds)
      : "—";
    stagingAgeNode.classList.toggle(
      "is-zero",
      !staging.pendingCount || staging.oldestAgeSeconds == null
    );
  }
  const stagingAgeSub = $("#kpi-dash-staging-age-sub");
  if (stagingAgeSub) {
    if (staging.pendingCount && staging.oldestAgeSeconds != null) {
      stagingAgeSub.textContent = `${fmt(staging.pendingCount)} pending · warehouse fold lag`;
    } else if (staging.pendingCount) {
      stagingAgeSub.textContent = `${fmt(staging.pendingCount)} pending · age unknown`;
    } else {
      stagingAgeSub.textContent = "warehouse fold lag";
    }
  }

  // GDELT GKG slot fetch latency + attempts (process-local).
  const gdeltP95Node = $("#kpi-dash-gdelt-p95");
  if (gdeltP95Node) {
    gdeltP95Node.textContent = formatFetchLatency(discovery.gdeltFetchP95);
    gdeltP95Node.classList.toggle("is-zero", !discovery.gdeltFetchAttempts);
  }
  const gdeltP95Sub = $("#kpi-dash-gdelt-p95-sub");
  if (gdeltP95Sub) {
    gdeltP95Sub.textContent = discovery.gdeltFetchAttempts
      ? `${fmt(discovery.gdeltFetchOk)}/${fmt(discovery.gdeltFetchAttempts)} ok · process`
      : "no GDELT slot fetches yet";
  }
  setKPI("kpi-dash-gdelt-attempts", discovery.gdeltFetchAttempts);
  const gdeltAttSub = $("#kpi-dash-gdelt-attempts-sub");
  if (gdeltAttSub) {
    gdeltAttSub.textContent = discovery.gdeltFetchAttempts
      ? `${fmt(discovery.gdeltUrls)} urls · ${fmt(discovery.gdeltEnqueued)} enqueued`
      : "slot GETs this process";
  }

  // FineWeb HF stream admit/filter + load latency (process-local).
  const fineweb = summarizeFinewebMetrics(metricsSnap);
  setKPI("kpi-dash-fineweb-admitted", fineweb.admitted);
  const fwAdmSub = $("#kpi-dash-fineweb-admitted-sub");
  if (fwAdmSub) {
    if (fineweb.seen || fineweb.admitted) {
      fwAdmSub.textContent = `${fmt(fineweb.seen)} seen · HF rows kept`;
    } else {
      fwAdmSub.textContent = "HF rows kept this process";
    }
  }
  setKPI("kpi-dash-fineweb-filtered", fineweb.filtered);
  const fwFiltSub = $("#kpi-dash-fineweb-filtered-sub");
  if (fwFiltSub) {
    if (fineweb.filtered || fineweb.admitted) {
      const bits = [`${fmt(fineweb.admitted)} admitted`];
      if (fineweb.topReason) bits.push(`top: ${fineweb.topReason}`);
      fwFiltSub.textContent = bits.join(" · ");
    } else {
      fwFiltSub.textContent = "empty / lang / domain / short";
    }
  }
  const fwP95Node = $("#kpi-dash-fineweb-p95");
  if (fwP95Node) {
    fwP95Node.textContent = formatFetchLatency(fineweb.loadP95);
    fwP95Node.classList.toggle("is-zero", !fineweb.loadAttempts);
  }
  const fwP95Sub = $("#kpi-dash-fineweb-p95-sub");
  if (fwP95Sub) {
    fwP95Sub.textContent = fineweb.loadAttempts
      ? `${fmt(fineweb.loadOk)}/${fmt(fineweb.loadAttempts)} ok · process`
      : "no FineWeb loads yet";
  }
  setKPI("kpi-dash-fineweb-attempts", fineweb.loadAttempts);
  const fwAttSub = $("#kpi-dash-fineweb-attempts-sub");
  if (fwAttSub) {
    fwAttSub.textContent = fineweb.loadAttempts
      ? `${fmt(fineweb.loadOk)} succeeded`
      : "dataset loads this process";
  }

  // CC-WET shard download + parse latency (process-local).
  const wetParse = summarizeWetParseMetrics(metricsSnap);
  const wetParseP95Node = $("#kpi-dash-wet-parse-p95");
  if (wetParseP95Node) {
    wetParseP95Node.textContent = formatFetchLatency(wetParse.parseP95);
    wetParseP95Node.classList.toggle(
      "is-zero",
      wetParse.parseP95 == null && !wetParse.parseEmitted && !wetParse.recordsSeen
    );
  }
  const wetParseP95Sub = $("#kpi-dash-wet-parse-p95-sub");
  if (wetParseP95Sub) {
    wetParseP95Sub.textContent = wetParse.parseEmitted || wetParse.recordsSeen
      ? `${fmt(wetParse.parseEmitted)} emitted · shard parse`
      : "shard parse latency (process)";
  }
  setKPI("kpi-dash-wet-dl-attempts", wetParse.downloadAttempts);
  const wetDlSub = $("#kpi-dash-wet-dl-attempts-sub");
  if (wetDlSub) {
    if (wetParse.downloadAttempts) {
      const bits = [
        `${fmt(wetParse.downloadOk)}/${fmt(wetParse.downloadAttempts)} ok`,
      ];
      if (wetParse.downloadCacheHits) {
        bits.push(`${fmt(wetParse.downloadCacheHits)} cache`);
      }
      const dlP95 = formatFetchLatency(wetParse.downloadP95);
      if (wetParse.downloadP95 != null) bits.push(`p95 ${dlP95}`);
      wetDlSub.textContent = bits.join(" · ");
    } else {
      wetDlSub.textContent = "shard cache hits + downloads";
    }
  }
  setKPI("kpi-dash-wet-seen", wetParse.recordsSeen);
  const wetSeenSub = $("#kpi-dash-wet-seen-sub");
  if (wetSeenSub) {
    wetSeenSub.textContent = wetParse.recordsSeen || wetParse.parseEmitted
      ? `${fmt(wetParse.parseEmitted)} emitted after quality`
      : "WAR records scanned this process";
  }

  // JSONL crash-safe mid-chunk sync + orphan recovery (process-local).
  setKPI("kpi-dash-jsonl-syncs", storageObs.jsonlSyncs);
  const jsonlSyncSub = $("#kpi-dash-jsonl-syncs-sub");
  if (jsonlSyncSub) {
    if (storageObs.jsonlSyncs) {
      const bits = [
        `${fmt(storageObs.jsonlSyncOk)}/${fmt(storageObs.jsonlSyncs)} ok`,
      ];
      if (storageObs.jsonlOpenRecords) {
        bits.push(`${fmt(storageObs.jsonlOpenRecords)} open`);
      }
      jsonlSyncSub.textContent = bits.join(" · ");
    } else {
      jsonlSyncSub.textContent = "crash-safe fsyncs this process";
    }
  }
  const jsonlSyncP95Node = $("#kpi-dash-jsonl-sync-p95");
  if (jsonlSyncP95Node) {
    jsonlSyncP95Node.textContent = formatFetchLatency(storageObs.jsonlSyncP95);
    jsonlSyncP95Node.classList.toggle("is-zero", !storageObs.jsonlSyncs);
  }
  const jsonlSyncP95Sub = $("#kpi-dash-jsonl-sync-p95-sub");
  if (jsonlSyncP95Sub) {
    jsonlSyncP95Sub.textContent = storageObs.jsonlSyncs
      ? `${fmt(storageObs.jsonlSyncOk)} ok fsyncs · process`
      : "mid-chunk fsync latency";
  }
  setKPI("kpi-dash-jsonl-orphans", storageObs.jsonlOrphansRecovered);
  const jsonlOrphSub = $("#kpi-dash-jsonl-orphans-sub");
  if (jsonlOrphSub) {
    if (storageObs.jsonlOrphansRecovered || storageObs.jsonlOrphansRemoved) {
      const bits = [];
      if (storageObs.jsonlOrphansRecovered) {
        bits.push(`${fmt(storageObs.jsonlOrphansRecovered)} promoted`);
      }
      if (storageObs.jsonlOrphansRemoved) {
        bits.push(`${fmt(storageObs.jsonlOrphansRemoved)} empty dropped`);
      }
      jsonlOrphSub.textContent = bits.join(" · ");
    } else {
      jsonlOrphSub.textContent = "promoted leftover .tmp chunks";
    }
  }

  $("#kpi-captures-sub").textContent = (docsTotal ? `${fmt(docsTotal)} emitted across jobs` : "across the corpus");
  $("#kpi-distinct-sub").textContent = "unique content";
  // Folds stay hash-level among stored captures (distinct from process skip counters).
  $("#kpi-folds-sub").textContent = `${fmt(dedup.near_dup_index_rows || 0)} simhash rows`;
  $("#kpi-jobs-sub").textContent = "backfill & tail runs";

  // Tail strip (dashboard)
  const strip = $("#tail-strip");
  const pulse = strip.querySelector(".tail-pulse");
  pulse.dataset.state = tail.running ? "on" : "off";
  $("#tail-strip-state").textContent = tail.running ? "running" : "stopped";
  $("#tail-strip-detail").textContent = tail.running
    ? `Reading public feeds. Job ${tail.job_id || "—"}.`
    : (tail.stopped_at ? `Last stopped ${ago(tail.stopped_at, false)}.` : "No live capture running.");
  $("#tail-strip-meta").textContent = tail.running ? `started ${ago(tail.started_at, false)}` : "";

  // Sidebar tail card
  $("#sidebar-tail .tail-led").dataset.state = tail.running ? "on" : "off";
  $("#sidebar-tail-status").textContent = tail.running ? "running" : "stopped";
  $("#sidebar-tail-meta").textContent = tail.running
    ? `since ${ago(tail.started_at, true)}`
    : (tail.stopped_at ? `since ${ago(tail.stopped_at, true)} ago` : "—");

  // Recent jobs strip
  renderJobStrip($("#jobs-strip"), (status.jobs || []).slice(0, 4));

  return { status, dedup };
}

function appendJobRetryBits(counters, j) {
  // Surface failure / dead-letter counters when non-zero so operators can see
  // retry exhaustion without digging into task tables.
  const failed = Number(j.tasks_failed || 0);
  const dead = Number(j.tasks_dead_lettered || 0);
  if (failed > 0) {
    counters.appendChild(document.createTextNode(" · "));
    counters.appendChild(el("b", { class: "job-retry-fail", text: fmt(failed) }));
    counters.appendChild(document.createTextNode(" failed"));
  }
  if (dead > 0) {
    counters.appendChild(document.createTextNode(" · "));
    counters.appendChild(el("b", { class: "job-retry-dead", text: fmt(dead) }));
    counters.appendChild(document.createTextNode(" dead-lettered"));
  }
}

function renderJobStrip(root, jobs) {
  clear(root);
  if (!jobs.length) {
    root.appendChild(el("p", { class: "muted", style: { padding: "22px 24px" } }, "No jobs yet."));
    return;
  }
  for (const j of jobs) {
    const pct = j.tasks_total ? Math.round(100 * j.tasks_completed / j.tasks_total) : 0;
    const idCell = el("div", { class: "job-id" });
    idCell.appendChild(document.createTextNode(j.job_id));
    idCell.appendChild(el("span", { class: "kind", text: j.kind }));

    const progress = el("div", { class: "job-progress", "aria-label": `progress ${pct}%`, role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 });
    progress.appendChild(el("div", { class: "job-progress-bar", style: { width: pct + "%" } }));

    const counters = el("div", { class: "job-counters" });
    counters.appendChild(document.createTextNode(`${j.tasks_completed}/${j.tasks_total} tasks · `));
    counters.appendChild(el("b", { text: fmt(j.docs_emitted) }));
    counters.appendChild(document.createTextNode(" docs · "));
    counters.appendChild(el("b", { text: fmt(j.docs_dedup_dropped) }));
    counters.appendChild(document.createTextNode(" folded"));
    appendJobRetryBits(counters, j);

    const badge = el("span", { class: "badge badge-" + j.status, text: j.status });
    const row = el("div", { class: "job-row" }, idCell, progress, counters, badge);
    root.appendChild(row);
  }
}

// ── Captures view ─────────────────────────────────────────────
// Hide-duplicates preference: default ON for browse (unique=group).
// Persisted so users who want raw chronological can opt out once.
const CAPS_HIDE_DUP_KEY = "awareness.captures.hideDuplicates";
const CAPS_HIDE_DUP_DEFAULT = true;

function readCapsHideDuplicates() {
  try {
    const raw = localStorage.getItem(CAPS_HIDE_DUP_KEY);
    if (raw == null) return CAPS_HIDE_DUP_DEFAULT;
    if (raw === "1" || raw === "true") return true;
    if (raw === "0" || raw === "false") return false;
  } catch (_) {
    /* private mode / blocked storage */
  }
  return CAPS_HIDE_DUP_DEFAULT;
}

function writeCapsHideDuplicates(on) {
  try {
    localStorage.setItem(CAPS_HIDE_DUP_KEY, on ? "1" : "0");
  } catch (_) {
    /* ignore quota / private mode */
  }
}

function applyCapsHideDuplicates(checked) {
  const node = $("#caps-unique");
  if (node) node.checked = !!checked;
}

const caps = { limit: 30, offset: 0, total: 0 };
let capsSearchTimer = null;
/** Last search terms for re-highlighting title/body in the capture reader. */
let lastSearchTerms = [];

/** Show empty-search diagnostics (mode/corpus/window + hints); hide otherwise. */
function renderCapsDiagnostics(data, isSearch) {
  const box = $("#caps-diagnostics");
  if (!box) return;
  clear(box);
  if (!isSearch || !data || Number(data.total) !== 0) {
    box.hidden = true;
    return;
  }
  const diag = data.diagnostics || {};
  const hints = Array.isArray(diag.hints) ? diag.hints.slice() : [];
  // Prefer diagnostics.mode_used; fall back to response mode (e.g. phrase).
  const modeUsed = String(diag.mode_used || data.mode || "").trim();
  const metaParts = [];
  if (modeUsed) {
    metaParts.push("mode=" + formatSearchModeLabel(modeUsed, false));
  }
  if (diag.corpus_size != null && diag.corpus_size !== "") {
    metaParts.push("corpus=" + String(diag.corpus_size));
  }
  const win = diag.window;
  if (win && (win.start != null || win.end != null)) {
    const s = win.start != null ? String(win.start).slice(0, 10) : "…";
    const e = win.end != null ? String(win.end).slice(0, 10) : "…";
    metaParts.push("window=" + s + "→" + e);
  }
  // Active domain/source/language filters from diagnostics.filters (or form fields).
  const filters = diag.filters && typeof diag.filters === "object" ? diag.filters : {};
  const domainFilter = String(filters.domain || $("#caps-domain")?.value || "").trim();
  const sourceFilter = String(filters.source || $("#caps-source")?.value || "").trim();
  const languageFilter = String(filters.language || $("#caps-language")?.value || "").trim();
  if (domainFilter) metaParts.push("domain=" + domainFilter);
  if (sourceFilter) metaParts.push("source=" + sourceFilter);
  if (languageFilter) metaParts.push("language=" + languageFilter);
  // Phrase empty-state must stay informative even if the API omitted hints.
  if (modeUsed.toLowerCase() === "phrase") {
    const hasPhraseHint = hints.some((h) => /phrase|quotes/i.test(String(h)));
    if (!hasPhraseHint) {
      hints.push("No exact phrase matches; try without quotes or fewer words.");
    }
  }
  if (!metaParts.length && !hints.length) {
    box.hidden = true;
    return;
  }
  box.appendChild(el("p", { class: "caps-diagnostics-title", text: "No results — suggestions" }));
  if (metaParts.length) {
    box.appendChild(el("p", { class: "caps-diagnostics-meta", text: metaParts.join(" · ") }));
  }
  if (hints.length) {
    const ul = el("ul");
    for (const h of hints) {
      ul.appendChild(el("li", { text: String(h) }));
    }
    box.appendChild(ul);
  }
  box.hidden = false;
}


/** Sync is-active / aria-pressed on facet chips from current filter fields. */
function syncFacetChipSelection() {
  const box = $("#caps-facets");
  if (!box) return;
  const activeDomain = ($("#caps-domain")?.value || "").trim().toLowerCase();
  const activeSource = ($("#caps-source")?.value || "").trim().toLowerCase();
  const activeLang = ($("#caps-language")?.value || "").trim().toLowerCase();
  box.querySelectorAll(".facet-chip[data-facet-kind]").forEach((chip) => {
    const kind = chip.getAttribute("data-facet-kind") || "";
    let on = false;
    if (kind === "domain") {
      const v = (chip.getAttribute("data-facet-value") || "").toLowerCase();
      on = !!(activeDomain && v && activeDomain === v);
    } else if (kind === "source") {
      const v = (chip.getAttribute("data-facet-value") || "").toLowerCase();
      on = !!(activeSource && v && activeSource === v);
    } else if (kind === "language") {
      const v = (chip.getAttribute("data-facet-value") || "").toLowerCase();
      on = !!(activeLang && v && activeLang === v);
    }
    chip.classList.toggle("is-active", on);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

/** Domain + source + language facet chips (facets.domains / sources / languages). */
function renderCapsFacets(data, isSearch) {
  const box = $("#caps-facets");
  if (!box) return;
  clear(box);
  const facets = isSearch && data && data.facets ? data.facets : null;
  const domains = facets && Array.isArray(facets.domains) ? facets.domains : [];
  const sources = facets && Array.isArray(facets.sources) ? facets.sources : [];
  const languages = facets && Array.isArray(facets.languages) ? facets.languages : [];
  if (!domains.length && !sources.length && !languages.length) {
    box.hidden = true;
    return;
  }

  for (const item of domains) {
    const dom = String(item.domain || item.Domain || "").trim();
    if (!dom) continue;
    const n = item.n != null ? Number(item.n) : null;
    const chip = el("button", {
      type: "button",
      class: "facet-chip",
      title: n != null ? `${dom} (${n} matches)` : dom,
      "data-facet-kind": "domain",
      "data-facet-value": dom.toLowerCase(),
      "aria-pressed": "false",
      onclick: () => {
        const field = $("#caps-domain");
        if (!field) return;
        // Toggle: second click on the active chip clears the domain filter.
        if ((field.value || "").trim().toLowerCase() === dom.toLowerCase()) {
          field.value = "";
        } else {
          field.value = dom;
        }
        // Immediate selected-state feedback (do not wait for search round-trip).
        syncFacetChipSelection();
        void loadCaptures(true);
      },
    });
    chip.appendChild(document.createTextNode(dom));
    if (n != null && !Number.isNaN(n)) {
      chip.appendChild(el("span", { class: "facet-n", text: String(n) }));
    }
    box.appendChild(chip);
  }

  for (const item of sources) {
    const src = String(item.source_type || item.source || "").trim();
    if (!src) continue;
    const n = item.n != null ? Number(item.n) : null;
    const chip = el("button", {
      type: "button",
      class: "facet-chip facet-chip-source",
      title: n != null ? `source ${src} (${n} matches)` : `source ${src}`,
      "data-facet-kind": "source",
      "data-facet-value": src.toLowerCase(),
      "aria-pressed": "false",
      onclick: () => {
        const field = $("#caps-source");
        if (!field) return;
        // Ensure the select has an option for this source_type (API may return
        // kinds not in the static list).
        const hasOpt = Array.from(field.options || []).some(
          (o) => (o.value || "").toLowerCase() === src.toLowerCase()
        );
        if (!hasOpt) {
          field.appendChild(el("option", { value: src, text: src }));
        }
        // Toggle: second click on the active chip clears the source filter.
        if ((field.value || "").trim().toLowerCase() === src.toLowerCase()) {
          field.value = "";
        } else {
          field.value = src;
        }
        syncFacetChipSelection();
        void loadCaptures(true);
      },
    });
    chip.appendChild(document.createTextNode(src));
    if (n != null && !Number.isNaN(n)) {
      chip.appendChild(el("span", { class: "facet-n", text: String(n) }));
    }
    box.appendChild(chip);
  }

  for (const item of languages) {
    const lang = String(item.language || item.lang || "").trim();
    if (!lang) continue;
    const n = item.n != null ? Number(item.n) : null;
    const chip = el("button", {
      type: "button",
      class: "facet-chip facet-chip-lang",
      title: n != null ? `language ${lang} (${n} matches)` : `language ${lang}`,
      "data-facet-kind": "language",
      "data-facet-value": lang.toLowerCase(),
      "aria-pressed": "false",
      onclick: () => {
        const field = $("#caps-language");
        if (!field) return;
        // Toggle: second click on the active chip clears the language filter.
        if ((field.value || "").trim().toLowerCase() === lang.toLowerCase()) {
          field.value = "";
        } else {
          field.value = lang;
        }
        syncFacetChipSelection();
        void loadCaptures(true);
      },
    });
    chip.appendChild(document.createTextNode(lang));
    if (n != null && !Number.isNaN(n)) {
      chip.appendChild(el("span", { class: "facet-n", text: String(n) }));
    }
    box.appendChild(chip);
  }

  syncFacetChipSelection();
  box.hidden = box.childNodes.length === 0;
}

async function loadCaptures(reset = false) {
  if (reset) caps.offset = 0;
  const q = $("#caps-search").value.trim();
  const source = $("#caps-source").value;
  const domain = $("#caps-domain").value.trim();
  const language = ($("#caps-language")?.value || "").trim();
  const start = $("#caps-start").value;
  const end = $("#caps-end").value;

  const list = $("#caps-list");
  const meta = $("#caps-meta");
  meta.textContent = "loading…";
  renderCapsDiagnostics(null, false);
  // Keep facet chips visible during reload so domain/source/language selected
  // state stays highlighted (chips are replaced when the response arrives).
  syncFacetChipSelection();

  // Search-mode hits /search (BM25 ranked, with snippets); browse-mode hits
  // /captures (chronological).
  const params = new URLSearchParams();
  params.set("limit", caps.limit);
  params.set("offset", caps.offset);
  if (source) params.set("source", source);
  if (domain) params.set("domain", domain);
  if (language) params.set("language", language);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  const hideDups = !!$("#caps-unique")?.checked;
  const modeSel = ($("#caps-mode")?.value || "auto").trim().toLowerCase();

  const isSearch = !!q;
  let url;
  if (isSearch) {
    params.set("q", q);
    params.set("mode", modeSel || "auto");
    url = "/search?" + params.toString();
  } else {
    if (hideDups) params.set("unique", "group");
    url = "/captures?" + params.toString();
  }

  try {
    const data = await api(url);
    caps.total = data.total;
    if (isSearch) {
      const rows = data.rows || [];
      const fromRow = rows.find((r) => Array.isArray(r.terms) && r.terms.length)?.terms;
      lastSearchTerms = fromRow && fromRow.length
        ? fromRow.slice()
        : q.split(/[^\w']+/).filter((t) => t.length >= 2);
    } else {
      lastSearchTerms = [];
    }
    renderCaps(list, data.rows || [], { search: q, ranked: !!data.ranked });
    renderCapsDiagnostics(data, isSearch);
    renderCapsFacets(data, isSearch);
    const from = data.total ? caps.offset + 1 : 0;
    const to = Math.min(caps.offset + (data.rows || []).length, data.total);
    // Active filter bits shown on both search and browse meta lines so the
    // language/domain/source fields stay visibly synced with the last request.
    const filterBits = [];
    if (source) filterBits.push("source=" + source);
    if (domain) filterBits.push("domain=" + domain);
    if (language) filterBits.push("language=" + language);
    const filterSuffix = filterBits.length ? " · " + filterBits.join(" · ") : "";
    if (isSearch) {
      const modeLabel = formatSearchModeLabel(data.mode, !!data.ranked);
      let metaLine = `${from}–${to} of ${fmt(data.total)} matches · ${modeLabel}`;
      const rb = Number(data.recency_boost);
      if (Number.isFinite(rb) && rb > 0) {
        metaLine += ` · recency=${rb}`;
      }
      meta.textContent = metaLine + filterSuffix;
    } else {
      const fold = hideDups ? " · unique groups" : "";
      meta.textContent =
        `${from}–${to} of ${fmt(data.total)} captures · chronological${fold}` + filterSuffix;
    }
    $("#caps-pos").textContent = data.total ? `${from}–${to} of ${fmt(data.total)}` : "—";
    $("#caps-prev").disabled = caps.offset <= 0;
    $("#caps-next").disabled = caps.offset + caps.limit >= data.total;
  } catch (err) {
    console.error(err);
    meta.textContent = "query failed: " + err.message;
    renderCapsDiagnostics(null, false);
    renderCapsFacets(null, false);
  }
}

// Build DOM fragment with <mark> tags around every match of any term, case
// insensitive, word-boundary. Uses matchAll (no innerHTML on values).
function highlightedFragment(text, terms) {
  const frag = document.createDocumentFragment();
  if (!text) return frag;
  if (!terms || terms.length === 0) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  const pattern = new RegExp(
    "\\b(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\b",
    "ig"
  );
  let lastIndex = 0;
  for (const m of text.matchAll(pattern)) {
    if (m.index > lastIndex) frag.appendChild(document.createTextNode(text.slice(lastIndex, m.index)));
    frag.appendChild(el("mark", { class: "hl" }, m[0]));
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < text.length) frag.appendChild(document.createTextNode(text.slice(lastIndex)));
  return frag;
}

function renderCaps(root, rows, { search = "", ranked = false } = {}) {
  clear(root);
  for (const r of rows) {
    const li = el("li", {
      class: "cap-row" + (search ? " has-snippet" : ""),
      tabindex: "0",
      role: "button",
      "aria-label": `${r.title || "untitled"} — ${r.source_type}`,
      dataset: { cid: r.capture_id },
      onkeydown: (ev) => { if (ev.key === "Enter") openReader(r.capture_id); },
      onclick: () => openReader(r.capture_id),
    });

    // Left column: score chip in ranked search mode, otherwise time.
    if (search && ranked && typeof r.score === "number") {
      li.appendChild(el("div", { class: "cap-score", title: "BM25 score" }, r.score.toFixed(2)));
    } else {
      li.appendChild(el("div", { class: "cap-time", title: String(r.fetch_ts || "") }, ago(r.fetch_ts, true)));
    }

    // Main: title + (snippet) + meta.
    const main = el("div", { class: "cap-main" });
    const titleNode = el("h3", { class: "cap-title" });
    if (search && r.terms && r.terms.length) {
      titleNode.appendChild(highlightedFragment(r.title || "(untitled)", r.terms));
    } else {
      titleNode.textContent = r.title || "(untitled)";
    }
    main.appendChild(titleNode);

    if (search && r.snippet) {
      const snip = el("p", { class: "cap-snippet" });
      snip.appendChild(highlightedFragment(r.snippet, r.terms || []));
      main.appendChild(snip);
    }

    const m = el("div", { class: "cap-meta" });
    m.appendChild(el("span", { class: "domain", text: r.domain || "—" }));
    if (r.url) m.appendChild(el("span", { class: "url", text: r.url }));
    if (search) m.appendChild(el("span", { class: "when", text: ago(r.fetch_ts, true) + " ago" }));
    main.appendChild(m);
    li.appendChild(main);

    li.appendChild(el("div", { class: "cap-source", text: r.source_type }));
    li.appendChild(el("div", { class: "cap-size", text: fmt(r.text_len) + " ch" }));
    root.appendChild(li);
  }
}

// ── Reader drawer ─────────────────────────────────────────────
let readerLastFocus = null;
async function openReader(cid) {
  const reader = $("#reader");
  const scrim = $("#reader-scrim");
  const body = $("#reader-body");
  readerLastFocus = document.activeElement;
  scrim.hidden = false;
  reader.setAttribute("aria-hidden", "false");

  clear(body);
  body.appendChild(el("p", { class: "muted", text: "loading…" }));
  let d;
  try { d = await api("/captures/" + encodeURIComponent(cid)); }
  catch (err) {
    clear(body);
    body.appendChild(el("p", { class: "muted", text: "failed: " + err.message }));
    return;
  }

  clear(body);
  body.appendChild(el("div", { class: "reader-eyebrow-source", text: (d.source_type || "") + " · " + (d.source_name || "") }));
  const titleText = d.title || "(untitled)";
  const relatedTotal = Number(d.related_count || 0);
  const titleRow = el("div", { class: "reader-title-row" });
  if (lastSearchTerms.length) {
    const titleNode = el("h1", { class: "reader-title" });
    titleNode.appendChild(highlightedFragment(titleText, lastSearchTerms));
    titleRow.appendChild(titleNode);
  } else {
    titleRow.appendChild(el("h1", { class: "reader-title", text: titleText }));
  }
  // Related sibling count in the title row so operators see cluster size
  // before scrolling to the collapsible related panel.
  if (relatedTotal > 0) {
    titleRow.appendChild(el("span", {
      class: "reader-related-badge",
      text: relatedTotal === 1 ? "1 related" : relatedTotal + " related",
      title: relatedTotal + " other capture(s) in the same dup-group",
    }));
  }
  body.appendChild(titleRow);

  const byline = el("div", { class: "reader-byline" });
  function bk(label, value, opts = {}) {
    if (value == null || value === "") return;
    const span = el("span");
    span.appendChild(el("span", { class: "b-key", text: label }));
    if (opts.link) {
      const href = safeOutboundHref(value);
      if (href) {
        const a = el("a", { href, target: "_blank", rel: "noopener" });
        a.textContent = value;
        span.appendChild(a);
      } else {
        span.appendChild(document.createTextNode(String(value)));
      }
    } else {
      span.appendChild(document.createTextNode(String(value)));
    }
    byline.appendChild(span);
  }
  bk("domain", d.domain);
  bk("fetched", new Date(d.fetch_ts).toLocaleString());
  if (d.published_ts) bk("published", new Date(d.published_ts).toLocaleString());
  if (d.language) bk("lang", d.language);
  if (d.url) bk("source", d.url, { link: true });
  body.appendChild(byline);

  const bodyText = d.text || "(empty)";
  if (lastSearchTerms.length) {
    const textNode = el("div", { class: "reader-text" });
    textNode.appendChild(highlightedFragment(bodyText, lastSearchTerms));
    body.appendChild(textNode);
  } else {
    body.appendChild(el("div", { class: "reader-text", text: bodyText }));
  }

  const meta = el("div", { class: "reader-meta" });
  meta.appendChild(el("div", { class: "reader-meta-title", text: "provenance & identity" }));
  const dl = el("dl");
  function metaRow(k, v) {
    if (v == null || v === "") return;
    dl.appendChild(el("dt", { text: k }));
    dl.appendChild(el("dd", { text: String(v) }));
  }
  metaRow("doc_id", d.doc_id);
  metaRow("capture_id", d.capture_id);
  if (d.parent_doc_or_dup_group && d.parent_doc_or_dup_group !== d.doc_id)
    metaRow("dup_group", d.parent_doc_or_dup_group);
  metaRow("discovery", d.discovery_channel);
  metaRow("canonical_url", d.canonical_url);
  metaRow("content_hash", d.content_hash);
  if (d.near_dup_hash != null) metaRow("near_dup_hash", d.near_dup_hash);
  metaRow("robots", d.robots_decision);
  meta.appendChild(dl);
  body.appendChild(meta);

  // Related captures (sibling dup_group entries) — collapsible so long
  // sibling lists do not bury provenance/body below the fold.
  const relatedDetails = el("details", { class: "reader-related" });
  const relatedSummary = el("summary", { class: "related-summary" });
  relatedSummary.appendChild(el("span", { class: "related-summary-label", text: "related captures" }));
  const relatedCount = el("span", { class: "related-summary-count", text: "…" });
  relatedSummary.appendChild(relatedCount);
  relatedDetails.appendChild(relatedSummary);
  const relatedBody = el("div", { class: "related-body" });
  relatedBody.appendChild(el("p", { class: "muted", text: "loading…" }));
  relatedDetails.appendChild(relatedBody);
  body.appendChild(relatedDetails);

  void loadRelated(cid, relatedBody, relatedDetails, relatedCount);

  setTimeout(() => $("#reader-close").focus(), 80);
}

async function loadRelated(cid, host, detailsEl, countEl) {
  // Auto-open only for small sibling sets; larger clusters stay collapsed
  // so the reader body stays primary. User can expand via <summary>.
  const AUTO_OPEN_MAX = 3;
  try {
    const r = await api("/captures/" + encodeURIComponent(cid) + "/related?limit=12");
    clear(host);
    const sibs = r.siblings || [];
    // Prefer full related_count from API (siblings list may be limit-truncated).
    const total = (typeof r.related_count === "number") ? r.related_count : sibs.length;
    if (countEl) {
      countEl.textContent = total ? String(total) : "0";
    }
    if (detailsEl) {
      if (total === 0) {
        detailsEl.open = false;
        detailsEl.classList.add("is-empty");
      } else if (total <= AUTO_OPEN_MAX) {
        detailsEl.open = true;
        detailsEl.classList.remove("is-empty");
      } else {
        detailsEl.open = false;
        detailsEl.classList.remove("is-empty");
      }
    }
    if (total === 0) {
      host.appendChild(el("p", { class: "muted", text: "No related captures — this is the only member of its dup-group." }));
      return;
    }
    const list = el("ul", { class: "related-list" });
    for (const s of sibs) {
      const li = el("li", { class: "related-item" });
      const btn = el("button", {
        class: "related-link",
        onclick: () => openReader(s.capture_id),
        "aria-label": "Open " + (s.title || "related capture"),
      });
      btn.appendChild(el("span", { class: "related-when", text: ago(s.fetch_ts, true) + " ago" }));
      btn.appendChild(el("span", { class: "related-title", text: s.title || "(untitled)" }));
      const meta = el("span", { class: "related-meta" });
      meta.appendChild(el("span", { class: "src", text: s.source_type }));
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(el("span", { class: "dom", text: s.domain || "—" }));
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(document.createTextNode(fmt(s.text_len) + " ch"));
      btn.appendChild(meta);
      li.appendChild(btn);
      list.appendChild(li);
    }
    host.appendChild(list);
  } catch (err) {
    clear(host);
    if (countEl) countEl.textContent = "!";
    if (detailsEl) {
      detailsEl.open = true;
      detailsEl.classList.add("is-empty");
    }
    host.appendChild(el("p", { class: "muted", text: "failed: " + err.message }));
  }
}

function closeReader() {
  const reader = $("#reader");
  const scrim = $("#reader-scrim");
  reader.setAttribute("aria-hidden", "true");
  scrim.hidden = true;
  if (readerLastFocus && readerLastFocus.focus) readerLastFocus.focus();
}

// ── Jobs view (full) ──────────────────────────────────────────
async function loadJobs() {
  try {
    const status = await api("/status");
    renderJobsFull(status.jobs || []);
  } catch (err) { console.error(err); }
}

function renderJobsFull(jobs) {
  const root = $("#jobs-full");
  clear(root);
  if (!jobs.length) {
    root.appendChild(el("p", { class: "muted", style: { padding: "22px 24px" } }, "No jobs yet — submit a backfill above or start the tail."));
    return;
  }
  for (const j of jobs) {
    const pct = j.tasks_total ? Math.round(100 * j.tasks_completed / j.tasks_total) : 0;
    const idCell = el("div", { class: "job-id" });
    idCell.appendChild(document.createTextNode(j.job_id));
    idCell.appendChild(el("span", { class: "kind", text: j.kind }));

    const progress = el("div", { class: "job-progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 });
    progress.appendChild(el("div", { class: "job-progress-bar", style: { width: pct + "%" } }));

    const counters = el("div", { class: "job-counters" });
    counters.appendChild(document.createTextNode(`${j.tasks_completed}/${j.tasks_total} tasks · `));
    counters.appendChild(el("b", { text: fmt(j.docs_emitted) }));
    counters.appendChild(document.createTextNode(" docs · "));
    counters.appendChild(el("b", { text: fmt(j.docs_dedup_dropped) }));
    counters.appendChild(document.createTextNode(" folded · "));
    counters.appendChild(document.createTextNode(j.started_at ? "started " + ago(j.started_at, false) : "queued"));
    appendJobRetryBits(counters, j);

    const badge = el("span", { class: "badge badge-" + j.status, text: j.status });

    root.appendChild(el("div", { class: "job-row" }, idCell, progress, counters, badge));
  }
}

// ── Backfill form ─────────────────────────────────────────────
$("#bf-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const submit = e.target.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    const start = $("#bf-start").value;
    if (!start) { toast("pick a start date", "err"); return; }
    const sources = $$("#bf-form input[type=checkbox]").filter((x) => x.checked).map((x) => x.value);
    const domains = $("#bf-domains").value.split(",").map((s) => s.trim()).filter(Boolean);
    const langs = $("#bf-langs").value.split(",").map((s) => s.trim()).filter(Boolean);
    const max = parseInt($("#bf-max").value, 10);
    const body = {
      start: start,
      end_str: $("#bf-end").value || "now",
      sources: sources,
      domains: domains.length ? domains : null,
      languages: langs.length ? langs : null,
      max_tasks: Number.isFinite(max) ? max : null,
    };
    const resp = await api("/backfill", { method: "POST", body: JSON.stringify(body) });
    // Planner zero-task warning: RSS-only / empty range plans are not silent no-ops.
    if (resp.warning === "zero_tasks" || Number(resp.tasks_total || 0) === 0) {
      const reasons = Array.isArray(resp.zero_task_reasons) ? resp.zero_task_reasons : [];
      const detail = reasons.length
        ? reasons.map((r) => (r.source || "?") + ": " + (r.detail || r.reason || "")).join("; ")
        : (resp.notes || "no partitions for selected sources/range");
      toast(`job ${resp.job_id}: 0 tasks planned — ${detail}`, "err");
      void loadJobs();
      void refreshDashboard();
      return;
    }
    toast(`job ${resp.job_id} submitted (${resp.tasks_total} tasks)`, "ok");
    await api(`/backfill/${encodeURIComponent(resp.job_id)}/run`, { method: "POST", body: "{}" });
    toast(`job ${resp.job_id} running…`, "ok");
    void loadJobs();
    void refreshDashboard();
  } catch (err) {
    toast("backfill failed: " + err.message, "err");
  } finally { submit.disabled = false; }
});

// ── Tail page (rich live status) ──────────────────────────────
let tailPollTimer = null;
function startTailPolling() {
  if (tailPollTimer) return;
  loadTailView();
  tailPollTimer = setInterval(() => {
    if (currentRoute === "tail") loadTailView();
    else { clearInterval(tailPollTimer); tailPollTimer = null; }
  }, 2000);
}

async function loadTailView() {
  let data;
  try { data = await api("/tail/status"); } catch (e) { console.error(e); return; }
  const t = data.tail || {};
  const job = data.job || {};
  const counts = data.task_status_counts || {};
  const seed = data.per_seed || { feeds: [], fetch: {} };

  // Hero
  const big = $("#tail-big");
  big.dataset.state = t.running ? "on" : "off";
  $("#tail-big-state").textContent = t.running ? "running" : "stopped";
  $("#tail-big-detail").textContent = t.running
    ? "Reading public feeds. Newly discovered URLs are fetched politely, normalized to text, deduped, and written to the corpus."
    : (t.stopped_at ? `Last live run ended ${ago(t.stopped_at, false)}.` : "No live capture has been started yet.");
  $("#tail-big-meta").textContent = t.running
    ? `job ${t.job_id || "—"} · started ${ago(t.started_at, false)}`
    : (t.stopped_at ? `stopped at ${new Date(t.stopped_at).toLocaleString()}` : "");

  // Progress bar + counters
  const total = Number(job.tasks_total || 0);
  const done = Number(job.tasks_completed || 0);
  const pct = total ? Math.round(100 * done / total) : 0;
  $("#tail-progress-fill").style.width = pct + "%";
  $("#tail-progress-meta").textContent = total
    ? `${done}/${total} tasks · ${pct}%`
    : (t.running ? "queue empty" : "no active job");

  const cwrap = $("#tail-counters");
  clear(cwrap);
  function ctr(label, value, kind = "") {
    const c = el("div", { class: "ctr " + kind });
    c.appendChild(el("span", { class: "ctr-num", text: fmt(value) }));
    c.appendChild(el("span", { class: "ctr-lbl", text: label }));
    return c;
  }
  cwrap.appendChild(ctr("pending", counts.pending || 0, "ctr-pending"));
  cwrap.appendChild(ctr("fetching", counts.running || 0, "ctr-running"));
  cwrap.appendChild(ctr("completed", counts.completed || 0, "ctr-done"));
  cwrap.appendChild(ctr("docs captured", job.docs_emitted || 0, "ctr-docs"));
  cwrap.appendChild(ctr("folded", job.docs_dedup_dropped || 0, "ctr-folded"));
  const retryN = Number(data.retry_scheduled_count || 0);
  if (retryN > 0) cwrap.appendChild(ctr("retrying", retryN, "ctr-retry"));
  if (counts.failed) cwrap.appendChild(ctr("failed", counts.failed, "ctr-failed"));
  if (counts.dead_lettered) cwrap.appendChild(ctr("dead-lettered", counts.dead_lettered, "ctr-failed"));
  const deadJob = Number(job.tasks_dead_lettered || 0);
  if (deadJob > 0 && !counts.dead_lettered) cwrap.appendChild(ctr("dead-lettered", deadJob, "ctr-failed"));

  // Now fetching
  const nowList = $("#tail-now-list");
  const running = data.running_tasks || [];
  const engine = data.engine || {};
  const pollSec = data.tail_poll_seconds || 60;
  clear(nowList);
  // Compute "next poll" countdown.
  let nextPollIn = null;
  if (t.running && engine.next_reseed_at) {
    const remaining = Math.max(0, Math.round(engine.next_reseed_at - Date.now() / 1000));
    nextPollIn = remaining;
  }
  $("#tail-now-meta").textContent = running.length
    ? `${running.length} in flight`
    : (t.running ? (nextPollIn !== null ? `next poll in ${nextPollIn}s` : `idle · poll every ${pollSec}s`) : "tail stopped");
  if (running.length === 0) {
    const idleMsg = t.running
      ? (engine.last_reseed_at
          ? `Idle. Last poll ${ago(engine.last_reseed_at, false)} found ${engine.last_reseed_count || 0} feed${engine.last_reseed_count === 1 ? "" : "s"} to re-arm. Next poll in ~${nextPollIn ?? pollSec}s.`
          : `Idle. First poll in ~${nextPollIn ?? pollSec}s.`)
      : "Tail is stopped.";
    nowList.appendChild(el("li", { class: "muted-li" }, idleMsg));
  } else {
    for (const r of running) {
      const li = el("li", { class: "tn-row" });
      li.appendChild(el("span", { class: "tn-spin", "aria-hidden": "true" }));
      li.appendChild(el("span", { class: "tn-source", text: r.source_type }));
      li.appendChild(el("span", { class: "tn-target", title: r.partition_key, text: shortPartition(r.partition_key) }));
      const attempts = Number(r.attempts || 0);
      if (attempts > 1) {
        li.appendChild(el("span", {
          class: "tn-attempts",
          text: "try " + attempts,
          title: "Task attempt " + attempts + " (prior failures backed off)",
        }));
      }
      li.appendChild(el("span", { class: "tn-elapsed", text: r.started_at ? ago(r.started_at, true) : "" }));
      nowList.appendChild(li);
    }
  }

  // Retry backlog (PENDING with future next_attempt_at)
  const retryList = data.retry_scheduled || [];
  const retryHost = $("#tail-retry-list");
  const retryMetaEl = $("#tail-retry-meta");
  if (retryHost) {
    clear(retryHost);
    if (retryMetaEl) retryMetaEl.textContent = retryN > 0 ? `${retryN} waiting` : "—";
    if (retryList.length === 0) {
      retryHost.appendChild(el("li", { class: "muted-li" }, t.running ? "No tasks waiting on retry backoff." : "—"));
    } else {
      for (const r of retryList) {
        const li = el("li", { class: "tn-row" });
        li.appendChild(el("span", { class: "tn-retry-ic", "aria-hidden": "true" }, "↻"));
        li.appendChild(el("span", { class: "tn-source", text: r.source_type }));
        li.appendChild(el("span", { class: "tn-target", title: r.partition_key, text: shortPartition(r.partition_key) }));
        const att = Number(r.attempts || 0);
        li.appendChild(el("span", {
          class: "tn-attempts",
          text: att ? ("try " + att) : "retry",
          title: r.last_error || "scheduled retry",
        }));
        li.appendChild(el("span", {
          class: "tn-elapsed",
          text: r.next_attempt_at ? ("next " + ago(r.next_attempt_at, true)) : "",
          title: r.next_attempt_at || "",
        }));
        retryHost.appendChild(li);
      }
    }
  }

  // Just captured
  const doneList = $("#tail-done-list");
  const recent = data.recent_completed || [];
  clear(doneList);
  $("#tail-done-meta").textContent = recent.length ? `${recent.length} recent` : "—";
  if (recent.length === 0) {
    doneList.appendChild(el("li", { class: "muted-li" }, "No completed fetches yet."));
  } else {
    for (const r of recent) {
      const li = el("li", { class: "tn-row" });
      li.appendChild(el("span", { class: "tn-tick", "aria-hidden": "true" }, "✓"));
      li.appendChild(el("span", { class: "tn-source", text: r.source_type }));
      li.appendChild(el("span", { class: "tn-target", title: r.partition_key, text: shortPartition(r.partition_key) }));
      const meta = el("span", { class: "tn-result" });
      meta.appendChild(document.createTextNode(`${r.docs_emitted || 0} doc`));
      if (r.docs_dedup_dropped) meta.appendChild(document.createTextNode(` · ${r.docs_dedup_dropped} folded`));
      li.appendChild(meta);
      li.appendChild(el("span", { class: "tn-elapsed", text: r.completed_at ? ago(r.completed_at, true) : "" }));
      doneList.appendChild(li);
    }
  }

  // Recent chunks
  const chunksList = $("#tail-chunks-list");
  const chunks = data.recent_chunks || [];
  clear(chunksList);
  if (chunks.length === 0) {
    chunksList.appendChild(el("li", { class: "muted-li" }, "No JSONL chunks committed yet."));
  } else {
    for (const c of chunks) {
      const li = el("li", { class: "tc-row" });
      li.appendChild(el("span", { class: "tc-ic", "aria-hidden": "true" }, "▢"));
      li.appendChild(el("span", { class: "tc-records", text: fmt(c.records) + " records" }));
      li.appendChild(el("span", { class: "tc-bytes", text: fmtBytes(c.bytes) }));
      li.appendChild(el("span", { class: "tc-path", title: c.path, text: shortPath(c.path) }));
      li.appendChild(el("span", { class: "tc-when", text: c.committed_at ? ago(c.committed_at, true) : "" }));
      chunksList.appendChild(li);
    }
  }

  // Seeds
  const seedsBlock = $("#seeds-block");
  clear(seedsBlock);
  $("#seeds-meta").textContent = `${seed.feeds.length} configured · fetch: ${
    Object.entries(seed.fetch || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "none"
  }`;
  if (!seed.feeds.length) {
    seedsBlock.appendChild(el("p", { class: "muted" },
      "Edit ", el("code", { text: "configs/tail_seeds.yaml" }),
      " to change which feeds are read."));
  } else {
    const ul = el("ul", { class: "seeds-list" });
    for (const f of seed.feeds) {
      const li = el("li", { class: "seed-row" });
      const kind = f.partition_key.split(":", 1)[0];
      const url = f.partition_key.slice(kind.length + 1);
      li.appendChild(el("span", { class: "seed-kind", text: kind }));
      li.appendChild(el("span", { class: "seed-url", title: url, text: url }));
      li.appendChild(el("span", { class: "seed-status badge badge-" + f.status, text: f.status }));
      ul.appendChild(li);
    }
    seedsBlock.appendChild(ul);
  }
}

function shortPartition(pk) {
  if (!pk) return "";
  const idx = pk.indexOf("://");
  if (idx < 0) return pk;
  const url = pk.slice(pk.indexOf(":") + 1);
  try {
    const u = new URL(url);
    return u.host + (u.pathname.length > 30 ? u.pathname.slice(0, 27) + "…" : u.pathname);
  } catch { return url.slice(0, 80); }
}
function shortPath(p) {
  if (!p) return "";
  const parts = p.split("/");
  return parts.slice(-3).join("/");
}
function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

// Bind tail buttons (both strip + big)
for (const id of ["tail-start-btn", "tail-big-start"]) {
  $("#" + id)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("/tail/start", { method: "POST", body: "{}" }); toast("tail started", "ok"); }
    catch (err) { toast("tail start failed: " + err.message, "err"); }
    finally { e.target.disabled = false; void refreshDashboard(); void loadTailView(); }
  });
}
for (const id of ["tail-stop-btn", "tail-big-stop"]) {
  $("#" + id)?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("/tail/stop", { method: "POST", body: "{}" }); toast("tail paused", "ok"); }
    catch (err) { toast("tail stop failed: " + err.message, "err"); }
    finally { e.target.disabled = false; void refreshDashboard(); void loadTailView(); }
  });
}

// ── Settings ──────────────────────────────────────────────────
let settingsReady = false;
let engineSchemaCache = null;
/** True when the settings form has local edits not yet saved. */
let settingsDirty = false;

function setSettingsDirty(on) {
  settingsDirty = !!on;
  const btn = $("#set-save-all");
  if (btn && !btn.disabled) {
    btn.textContent = settingsDirty ? "Save all · unsaved" : "Save all";
    btn.classList.toggle("is-dirty", settingsDirty);
  }
  const hero = $(".view-settings .set-hero");
  hero?.classList.toggle("has-unsaved", settingsDirty);
  const badge = $("#set-dirty-badge");
  if (badge) badge.hidden = !settingsDirty;
}

/** One-time delegated listeners so dynamically rendered engine fields mark dirty. */
function initSettingsDirtyWatchers() {
  const root = $(".view-settings");
  if (!root || root.dataset.dirtyBound === "1") return;
  root.dataset.dirtyBound = "1";
  const mark = (e) => {
    const t = e.target;
    if (!t || !t.matches) return;
    if (t.matches("input, textarea, select")) setSettingsDirty(true);
  };
  root.addEventListener("input", mark);
  root.addEventListener("change", mark);
}

async function loadSettings() {
  const banner = $("#set-banner");
  if (banner) { banner.hidden = true; banner.textContent = ""; }
  try {
    const [schema, seeds, catalog, profile, health, dedup] = await Promise.all([
      api("/settings/schema"),
      api("/settings/tail-seeds"),
      api("/jobsearch/sources"),
      api("/jobsearch/profile"),
      api("/healthz"),
      api("/dedup-stats"),
    ]);
    engineSchemaCache = schema;
    renderEngineSchema(schema);
    fillTailSeeds(seeds);
    renderJobBoards(catalog.sources || [], profile.sources || []);
    fillJobProfile(profile);
    cachedJobProfile = profile;
    updateWorkPrefsSummary(profile);
    renderRuntimeStatus(health, dedup);
    buildSettingsToc();
    settingsReady = true;
    initSettingsDirtyWatchers();
    setSettingsDirty(false);
  } catch (err) {
    console.error(err);
    toast("Settings load failed: " + (err.message || err), "err");
  }
}

function buildSettingsToc() {
  const toc = $("#set-toc");
  if (!toc) return;
  clear(toc);
  const links = [
    ["#set-sec-work", "Work profile"],
    ["#set-sec-boards", "Job boards"],
    ["#set-sec-seeds", "Tail seeds"],
  ];
  (engineSchemaCache?.sections || []).forEach((sec, i) => {
    links.push(["#set-engine-" + i, sec.name]);
  });
  links.push(["#set-sec-status", "Runtime status"]);
  for (const [href, label] of links) {
    const a = el("a", { href, text: label });
    a.addEventListener("click", (e) => {
      const t = $(href);
      if (t) { e.preventDefault(); t.scrollIntoView({ behavior: "smooth", block: "start" }); }
    });
    toc.appendChild(a);
  }
}

function renderEngineSchema(schema) {
  const root = $("#set-engine-root");
  if (!root) return;
  clear(root);
  const note = el("p", { class: "set-banner-static", text: schema.note || "" });
  if (schema.config_path) {
    note.textContent = (schema.note || "") + "  File: " + schema.config_path;
  }
  root.appendChild(note);

  (schema.sections || []).forEach((sec, i) => {
    const section = el("section", {
      class: "set-sec",
      id: "set-engine-" + i,
    });
    const head = el("header", { class: "set-sec-head" });
    head.appendChild(el("h2", { class: "set-sec-title", text: sec.name }));
    head.appendChild(el("p", {
      class: "set-sec-desc",
      text: (sec.fields || []).length + " knobs · written to awareness.yaml",
    }));
    section.appendChild(head);

    const grid = el("div", { class: "set-engine-grid" });
    for (const f of sec.fields || []) {
      grid.appendChild(renderEngineField(f));
    }
    section.appendChild(grid);
    root.appendChild(section);
  });
}

function renderEngineField(f) {
  const wrap = el("label", {
    class: "set-efield" + (f.env_locked ? " is-locked" : ""),
    for: "cfg-" + f.key,
  });
  const top = el("div", { class: "set-efield-top" });
  top.appendChild(el("span", { class: "set-efield-key", text: f.key }));
  const src = el("span", {
    class: "set-efield-src src-" + (f.source || "default"),
    text: f.env_locked ? "env" : (f.source || "default"),
    title: f.env_var || "",
  });
  top.appendChild(src);
  wrap.appendChild(top);
  wrap.appendChild(el("p", { class: "set-efield-desc", text: f.description || "" }));

  let input;
  const id = "cfg-" + f.key;
  if (f.kind === "bool") {
    input = el("input", { type: "checkbox", id, dataset: { key: f.key, kind: "bool" } });
    input.checked = !!f.value;
    if (f.env_locked) input.disabled = true;
    const row = el("div", { class: "set-efield-bool" });
    row.appendChild(input);
    row.appendChild(el("span", { text: input.checked ? "on" : "off" }));
    input.addEventListener("change", () => {
      row.querySelector("span").textContent = input.checked ? "on" : "off";
    });
    wrap.appendChild(row);
  } else if (f.kind === "choice" && f.choices?.length) {
    input = el("select", { id, dataset: { key: f.key, kind: "choice" } });
    for (const c of f.choices) {
      const opt = el("option", { value: c, text: c });
      if (String(f.value) === c) opt.selected = true;
      input.appendChild(opt);
    }
    if (f.env_locked) input.disabled = true;
    wrap.appendChild(input);
  } else {
    const type = f.kind === "int" || f.kind === "float" ? "number" : "text";
    input = el("input", {
      type,
      id,
      dataset: { key: f.key, kind: f.kind },
      value: f.value == null ? "" : String(f.value),
      placeholder: f.example || (f.default != null ? String(f.default) : ""),
    });
    if (f.kind === "int" || f.kind === "float") {
      if (f.minimum != null) input.min = f.minimum;
      if (f.maximum != null) input.max = f.maximum;
      if (f.kind === "float") input.step = "any";
    }
    if (f.env_locked) input.disabled = true;
    wrap.appendChild(input);
  }
  return wrap;
}

function collectEngineValues() {
  const out = {};
  const root = $("#set-engine-root");
  if (!root) return out;
  root.querySelectorAll("[data-key]").forEach((node) => {
    const key = node.dataset.key;
    if (!key) return;
    const kind = node.dataset.kind;
    if (node.disabled) return;
    if (kind === "bool") {
      out[key] = !!node.checked;
    } else if (kind === "int") {
      const v = node.value.trim();
      if (v === "") return;
      out[key] = Number(v);
    } else if (kind === "float") {
      const v = node.value.trim();
      if (v === "") return;
      out[key] = Number(v);
    } else {
      out[key] = node.value;
    }
  });
  return out;
}

function fillTailSeeds(seeds) {
  const feeds = (seeds.feeds || []).join("\n");
  const maps = (seeds.sitemaps || []).join("\n");
  if ($("#set-feeds")) $("#set-feeds").value = feeds;
  if ($("#set-sitemaps")) $("#set-sitemaps").value = maps;
  if ($("#set-seeds-path")) {
    $("#set-seeds-path").textContent = seeds.path
      ? "File: " + seeds.path
      : "";
  }
}

function readTailSeedsForm() {
  const lines = (ta) =>
    String(ta?.value || "")
      .split(/[\n,;]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  return {
    feeds: lines($("#set-feeds")),
    atom: [],
    sitemaps: lines($("#set-sitemaps")),
  };
}

function renderRuntimeStatus(health, dedup) {
  const root = $("#settings-block");
  clear(root);
  for (const [k, v] of Object.entries(health || {})) {
    const row = el("div", { class: "kv-row" });
    row.appendChild(el("div", { class: "kv-key", text: k }));
    row.appendChild(el("div", { class: "kv-val", text: typeof v === "object" ? JSON.stringify(v) : String(v) }));
    root.appendChild(row);
  }
  // Highlight process skip counters from /dedup-stats (also listed in raw dump below).
  const d = dedup || {};
  setKPI("kpi-fetch-skipped", Number(d.fetch_skipped_seen || 0));
  setKPI("kpi-tight-near", Number(d.tight_near_skipped || 0));
  const dblock = $("#dedup-block");
  clear(dblock);
  for (const [k, v] of Object.entries(d)) {
    const row = el("div", { class: "kv-row" });
    row.appendChild(el("div", { class: "kv-key", text: k }));
    row.appendChild(el("div", { class: "kv-val", text: String(v) }));
    dblock.appendChild(row);
  }
}

function readJobProfile() {
  const sources = $$('#set-sources input[type="checkbox"]:checked').map((c) => c.value);
  const minRaw = ($("#set-min-salary")?.value || "").trim();
  return {
    titles: csvList($("#set-titles")?.value),
    skills: csvList($("#set-skills")?.value),
    locations: csvList($("#set-locations")?.value),
    exclude: csvList($("#set-exclude")?.value),
    remote_only: !!$("#set-remote-only")?.checked,
    min_salary: minRaw ? Number(minRaw) : null,
    notes: ($("#set-notes")?.value || "").trim(),
    sources,
  };
}

function fillJobProfile(p) {
  if (!p) return;
  if ($("#set-titles")) $("#set-titles").value = (p.titles || []).join(", ");
  if ($("#set-skills")) $("#set-skills").value = (p.skills || []).join(", ");
  if ($("#set-locations")) $("#set-locations").value = (p.locations || []).join(", ");
  if ($("#set-exclude")) $("#set-exclude").value = (p.exclude || []).join(", ");
  if ($("#set-notes")) $("#set-notes").value = p.notes || "";
  if ($("#set-min-salary")) $("#set-min-salary").value = p.min_salary != null ? p.min_salary : "";
  if ($("#set-remote-only")) $("#set-remote-only").checked = !!p.remote_only;
  const selected = new Set(p.sources || []);
  $$('#set-sources input[type="checkbox"]').forEach((c) => {
    c.checked = selected.size ? selected.has(c.value) : true;
    c.closest(".set-board")?.classList.toggle("is-on", c.checked);
  });
}

function renderJobBoards(sources, selected) {
  const box = $("#set-sources");
  if (!box) return;
  clear(box);
  const sel = new Set(selected && selected.length ? selected : sources.map((s) => s.id));
  for (const s of sources) {
    const id = "set-src-" + s.id;
    const lab = el("label", {
      class: "set-board" + (sel.has(s.id) ? " is-on" : ""),
      for: id,
      title: s.note || s.url || s.label,
    });
    const cb = el("input", { type: "checkbox", id, value: s.id });
    cb.checked = sel.has(s.id);
    cb.addEventListener("change", () => lab.classList.toggle("is-on", cb.checked));
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(s.label));
    box.appendChild(lab);
  }
}

function updateWorkPrefsSummary(p) {
  const node = $("#work-prefs-summary");
  if (!node) return;
  if (!p) {
    node.textContent = "";
    return;
  }
  const bits = [];
  if (p.titles?.length) bits.push(p.titles.slice(0, 3).join(", "));
  if (p.skills?.length) bits.push(p.skills.slice(0, 4).join(", "));
  if (p.locations?.length) bits.push(p.locations.slice(0, 3).join(", "));
  if (p.remote_only) bits.push("remote only");
  if (p.sources?.length) bits.push(p.sources.length + " boards");
  node.replaceChildren();
  if (!bits.length) {
    node.appendChild(el("span", { class: "muted", text: "No profile yet — " }));
    node.appendChild(el("a", { href: "#settings", "data-route": "settings", text: "configure in Settings" }));
    node.querySelector("a")?.addEventListener("click", (e) => {
      e.preventDefault();
      navigate("settings");
    });
    return;
  }
  node.appendChild(el("span", { text: bits.join(" · ") + " · " }));
  const link = el("a", { href: "#settings", text: "Edit in Settings" });
  link.addEventListener("click", (e) => {
    e.preventDefault();
    navigate("settings");
  });
  node.appendChild(link);
}

async function saveAllSettings() {
  const btn = $("#set-save-all");
  if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
  const banner = $("#set-banner");
  try {
    // 1) Work profile + boards
    const p = await api("/jobsearch/profile", {
      method: "PUT",
      body: JSON.stringify(readJobProfile()),
    });
    fillJobProfile(p);
    cachedJobProfile = p;
    updateWorkPrefsSummary(p);

    // 2) Tail seeds
    await api("/settings/tail-seeds", {
      method: "PUT",
      body: JSON.stringify(readTailSeedsForm()),
    });

    // 3) Engine knobs (schema)
    const engineRes = await api("/settings/config", {
      method: "PUT",
      body: JSON.stringify({ values: collectEngineValues() }),
    });

    // Refresh schema display with new values / sources
    const schema = await api("/settings/schema");
    engineSchemaCache = schema;
    renderEngineSchema(schema);
    buildSettingsToc();

    let msg = "Settings saved";
    if (engineRes.errors && Object.keys(engineRes.errors).length) {
      msg = "Saved with errors: " + Object.entries(engineRes.errors).map(([k, v]) => k + " (" + v + ")").join(", ");
      if (banner) {
        banner.hidden = false;
        banner.textContent = msg;
        banner.className = "set-banner is-err";
      }
      toast(msg, "err");
      // Partial save — keep dirty so the user can fix and re-save.
      setSettingsDirty(true);
    } else {
      if (banner) {
        banner.hidden = false;
        banner.textContent = "Saved. Env-locked keys unchanged. Restart API if workers/tail knobs don't apply.";
        banner.className = "set-banner is-ok";
      }
      toast("Settings saved", "ok");
      setSettingsDirty(false);
    }
  } catch (e) {
    toast(String(e.message || e), "err");
    if (banner) {
      banner.hidden = false;
      banner.textContent = String(e.message || e);
      banner.className = "set-banner is-err";
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = settingsDirty ? "Save all · unsaved" : "Save all";
      btn.classList.toggle("is-dirty", settingsDirty);
    }
  }
}

// ── Live activity feed (dashboard rail) ───────────────────────
const FEED_MAX = 25;
async function refreshFeed() {
  try {
    const data = await api(`/captures?limit=${FEED_MAX}&offset=0`);
    const rows = data.rows || [];
    if (rows.length === 0) {
      $("#feed").replaceChildren();
      $("#rail-empty").hidden = false;
      $("#rail-sub").textContent = "no captures yet";
      return;
    }
    $("#rail-empty").hidden = true;
    $("#rail-sub").textContent = `${fmt(data.total)} captures total`;
    renderFeed(rows);
  } catch (err) { console.error("feed", err); }
}

function renderFeed(rows) {
  const root = $("#feed");
  // Diff: if first row's capture_id is new, animate it.
  const prevFirst = lastFeedCaptureId;
  const newFirst = rows[0]?.capture_id;
  lastFeedCaptureId = newFirst;
  const newCaptureIds = new Set();
  if (prevFirst && newFirst !== prevFirst) {
    for (const r of rows) {
      if (r.capture_id === prevFirst) break;
      newCaptureIds.add(r.capture_id);
    }
  }
  clear(root);
  for (const r of rows) {
    const isNew = newCaptureIds.has(r.capture_id);
    const item = el("li", { class: "feed-item" + (isNew ? " is-new" : "") });
    item.appendChild(el("div", { class: "feed-bullet" }));
    const inner = el("div");
    inner.appendChild(el("div", { class: "feed-when", text: ago(r.fetch_ts, false) }));
    const title = el("button", {
      class: "feed-title",
      style: { background: "transparent", border: "none", padding: 0, textAlign: "left" },
      "aria-label": "Open " + (r.title || "capture"),
      onclick: () => openReader(r.capture_id),
    }, r.title || "(untitled)");
    inner.appendChild(title);
    const where = el("div", { class: "feed-where" });
    where.appendChild(el("span", { class: "src", text: r.source_type }));
    where.appendChild(document.createTextNode(" · "));
    where.appendChild(el("span", { class: "dom", text: r.domain || "—" }));
    inner.appendChild(where);
    item.appendChild(inner);
    root.appendChild(item);
  }
}

// ── Work / career search ──────────────────────────────────────
let workReady = false;
let workSearching = false;
let cachedJobProfile = null;

function csvList(v) {
  return String(v || "")
    .split(/[,;]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function scoreClass(score) {
  if (score >= 80) return "is-hi";
  if (score >= 35) return "is-mid";
  return "";
}

function snippetText(j) {
  const d = String(j.description || "").replace(/\s+/g, " ").trim();
  if (!d || d.length < 40) return "";
  const stub = `${j.title || ""} at ${j.company || ""}`.toLowerCase();
  if (d.toLowerCase().startsWith(stub.slice(0, Math.min(20, stub.length))) && d.length < 100) return "";
  return d.length > 200 ? d.slice(0, 197) + "…" : d;
}

function prettyReason(r) {
  const s = String(r || "");
  // shorten noisy labels for UI
  return s
    .replace(/^query:/, "")
    .replace(/^skill:/, "")
    .replace(/^title:/, "")
    .replace(/@description$/, "")
    .replace(/@title$/, "")
    .replace(/@tags$/, "")
    .replace(/^phrase:/, "≈ ")
    .slice(0, 28);
}

function renderWorkResults(payload) {
  const list = $("#work-list");
  const meta = $("#work-meta");
  if (!list) return;
  clear(list);
  const results = payload.results || [];
  const errKeys = Object.keys(payload.sources_err || {});
  const sc = payload.source_counts || {};
  const scParts = Object.keys(sc).map((k) => `${k} ${sc[k]}`).join(" · ");
  let msg = `${results.length} roles`;
  if (payload.raw_total) msg += ` from ${payload.raw_total}`;
  if (payload.took_ms) msg += ` · ${(payload.took_ms / 1000).toFixed(1)}s`;
  if (payload.enriched) msg += ` · ${payload.enriched} with full text`;
  if (scParts) msg += ` · ${scParts}`;
  if (errKeys.length) msg += ` · failed: ${errKeys.join(", ")}`;
  if (meta) meta.textContent = results.length ? msg : "No results yet — search above, or edit prefs in Settings";

  if (!results.length) {
    list.appendChild(el("li", {
      class: "work-empty",
      text: "No matches. Broaden keywords or update your profile in Settings.",
    }));
    return;
  }

  for (const j of results) {
    const li = el("li", { class: "work-card" });
    const body = el("div", { class: "work-card-body" });

    const top = el("div", { class: "work-card-top" });
    const h = el("h3", { class: "work-card-title" });
    const href = safeOutboundHref(j.url);
    if (href) {
      h.appendChild(el("a", {
        href, target: "_blank", rel: "noopener noreferrer",
        text: j.title || "Untitled",
      }));
    } else {
      h.appendChild(document.createTextNode(j.title || "Untitled"));
    }
    top.appendChild(h);
    top.appendChild(el("span", {
      class: "work-score " + scoreClass(j.score || 0),
      text: String(Math.round(j.score || 0)),
      title: (j.score_reasons || []).join(", ") || "match score",
    }));
    body.appendChild(top);

    const sub = el("div", { class: "work-card-sub" });
    if (j.company) sub.appendChild(el("span", { text: j.company }));
    if (j.location) sub.appendChild(el("span", { text: j.location }));
    if (j.remote) sub.appendChild(el("span", { text: "Remote" }));
    if (j.source_label || j.source) {
      sub.appendChild(el("span", { class: "work-src", text: j.source_label || j.source }));
    }
    if (j.published_at) sub.appendChild(el("span", { text: ago(j.published_at, false) }));
    if (j.salary) sub.appendChild(el("span", { text: j.salary }));
    body.appendChild(sub);

    const snip = snippetText(j);
    if (snip) body.appendChild(el("p", { class: "work-card-snip", text: snip }));

    const reasons = (j.score_reasons || []).slice(0, 4);
    if (reasons.length) {
      const chips = el("div", { class: "work-card-reasons" });
      reasons.forEach((r) => chips.appendChild(el("span", {
        class: "work-reason",
        text: prettyReason(r),
        title: String(r),
      })));
      body.appendChild(chips);
    }

    const actions = el("div", { class: "work-card-actions" });
    if (href) {
      actions.appendChild(el("a", {
        class: "btn btn-ghost",
        href,
        target: "_blank",
        rel: "noopener noreferrer",
        text: "Open →",
      }));
    }

    li.appendChild(body);
    li.appendChild(actions);
    list.appendChild(li);
  }
}

async function initWork() {
  try {
    // Prefer live form if Settings already loaded; else fetch profile
    if (settingsReady && $("#set-titles")) {
      cachedJobProfile = readJobProfile();
    } else {
      cachedJobProfile = await api("/jobsearch/profile");
    }
    updateWorkPrefsSummary(cachedJobProfile);
    workReady = true;
  } catch (e) {
    console.error(e);
    toast("Could not load work prefs", "err");
  }
}

async function runWorkSearch() {
  if (workSearching) return;
  workSearching = true;
  const btn = $("#work-search-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Searching…"; }
  const meta = $("#work-meta");
  if (meta) meta.textContent = "Fetching public boards…";
  try {
    // Always use saved Settings profile (or current settings form if open)
    let profile = cachedJobProfile;
    if (settingsReady && $("#set-titles")) profile = readJobProfile();
    if (!profile) profile = await api("/jobsearch/profile");
    const body = {
      q: ($("#work-q")?.value || "").trim(),
      profile,
      limit: 40,
      save_profile: false,
    };
    const res = await api("/jobsearch/search", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (res.profile) {
      cachedJobProfile = res.profile;
      updateWorkPrefsSummary(res.profile);
    }
    renderWorkResults(res);
  } catch (e) {
    console.error(e);
    if (meta) meta.textContent = "Search failed";
    toast(String(e.message || e), "err");
  } finally {
    workSearching = false;
    if (btn) { btn.disabled = false; btn.textContent = "Search"; }
  }
}

// ── Command palette ───────────────────────────────────────────
const cmdkOverlay = $("#cmdk");
const cmdkInput = $("#cmdk-input");
const cmdkList = $("#cmdk-list");
let cmdkActive = 0;
let cmdkResults = [];

function buildCommands(query = "") {
  const q = query.trim().toLowerCase();
  const sections = [
    { kind: "nav", icon: "◐", label: "Go to Dashboard", do: () => navigate("dashboard") },
    { kind: "nav", icon: "≡", label: "Go to Captures",  do: () => navigate("captures") },
    { kind: "nav", icon: "◇", label: "Go to Work",      do: () => navigate("work") },
    { kind: "nav", icon: "▱", label: "Go to Pipeline",  do: () => navigate("jobs") },
    { kind: "nav", icon: "⟳", label: "Go to Tail",      do: () => navigate("tail") },
    { kind: "nav", icon: "⚙", label: "Go to Settings",  do: () => navigate("settings") },
    { kind: "action", icon: "▶", label: "Start tail",   do: async () => { await api("/tail/start", { method: "POST", body: "{}" }); toast("tail started", "ok"); void refreshDashboard(); } },
    { kind: "action", icon: "■", label: "Pause tail",   do: async () => { await api("/tail/stop", { method: "POST", body: "{}" }); toast("tail paused", "ok"); void refreshDashboard(); } },
    { kind: "action", icon: "⌕", label: "Search captures…", do: () => { navigate("captures"); setTimeout(() => $("#caps-search").focus(), 200); } },
    { kind: "action", icon: "◇", label: "Find jobs…", do: () => { navigate("work"); setTimeout(() => $("#work-q")?.focus(), 200); } },
  ];
  if (!q) return sections;
  if (q.length >= 2) {
    // Local search shortcut → jump to captures with this query.
    sections.unshift({ kind: "search", icon: "⌕", label: `Search corpus for "${query}"`, do: () => { navigate("captures"); $("#caps-search").value = query; void loadCaptures(true); } });
    sections.unshift({ kind: "search", icon: "◇", label: `Find jobs for "${query}"`, do: () => { navigate("work"); setTimeout(() => { if ($("#work-q")) $("#work-q").value = query; void runWorkSearch(); }, 120); } });
  }
  return sections.filter((c) => c.label.toLowerCase().includes(q) || c.kind.includes(q));
}

function openCmdk() {
  cmdkOverlay.hidden = false;
  cmdkInput.value = "";
  cmdkActive = 0;
  renderCmdk("");
  setTimeout(() => cmdkInput.focus(), 30);
}
function closeCmdk() { cmdkOverlay.hidden = true; }
function renderCmdk(q) {
  cmdkResults = buildCommands(q);
  clear(cmdkList);
  if (cmdkResults.length === 0) {
    cmdkList.appendChild(el("li", { class: "cmdk-empty" }, "no matches"));
    return;
  }
  cmdkResults.forEach((c, i) => {
    const li = el("li", { class: "cmdk-item" + (i === cmdkActive ? " is-active" : ""), role: "option", "aria-selected": i === cmdkActive ? "true" : "false" });
    li.appendChild(el("span", { class: "cmdk-item-icon", text: c.icon }));
    li.appendChild(el("span", { class: "cmdk-item-label", text: c.label }));
    li.appendChild(el("span", { class: "cmdk-item-kind", text: c.kind }));
    li.addEventListener("click", () => { closeCmdk(); c.do(); });
    li.addEventListener("mouseenter", () => { cmdkActive = i; renderCmdk(q); });
    cmdkList.appendChild(li);
  });
}
cmdkInput?.addEventListener("input", (e) => { cmdkActive = 0; renderCmdk(e.target.value); });
cmdkInput?.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); cmdkActive = Math.min(cmdkResults.length - 1, cmdkActive + 1); renderCmdk(cmdkInput.value); }
  else if (e.key === "ArrowUp") { e.preventDefault(); cmdkActive = Math.max(0, cmdkActive - 1); renderCmdk(cmdkInput.value); }
  else if (e.key === "Enter") { e.preventDefault(); const c = cmdkResults[cmdkActive]; if (c) { closeCmdk(); c.do(); } }
  else if (e.key === "Escape") { closeCmdk(); }
});
cmdkOverlay?.addEventListener("click", (e) => { if (e.target === cmdkOverlay) closeCmdk(); });

// ── Global keyboard shortcuts ─────────────────────────────────
document.addEventListener("keydown", (e) => {
  // Cmd/Ctrl+K → open palette
  if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); openCmdk(); return; }
  // Cmd/Ctrl+S → save settings when on the Settings view (or any settings input focused).
  if ((e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "S")) {
    if (currentRoute === "settings" || document.activeElement?.closest?.(".view-settings")) {
      e.preventDefault();
      void saveAllSettings();
      return;
    }
  }
  // "/" focus search (when on captures view)
  if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea" && tag !== "select") {
      e.preventDefault();
      if (currentRoute !== "captures") navigate("captures");
      setTimeout(() => $("#caps-search").focus(), 80);
    }
  }
  // Number shortcuts 1..6 for routes (when not typing)
  if (/^[1-6]$/.test(e.key) && !e.metaKey && !e.ctrlKey && !e.altKey) {
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea" && tag !== "select") {
      navigate(ROUTES[parseInt(e.key, 10) - 1]);
    }
  }
  // Esc: close any overlay
  if (e.key === "Escape") {
    if (!cmdkOverlay.hidden) closeCmdk();
    else if ($("#reader").getAttribute("aria-hidden") === "false") closeReader();
    else if ($(".sidebar").classList.contains("is-open")) $(".sidebar").classList.remove("is-open");
  }
});

// ── Bind nav buttons ─────────────────────────────────────────
$$(".nav-item").forEach((b) => {
  b.addEventListener("click", () => navigate(b.dataset.route));
});
$$('[data-route]').forEach((b) => {
  if (b.classList.contains("nav-item")) return;
  b.addEventListener("click", (e) => { e.preventDefault(); navigate(b.dataset.route); });
});
$('[data-action="open-cmdk"]')?.addEventListener("click", openCmdk);
$("#reader-close")?.addEventListener("click", closeReader);
$("#reader-scrim")?.addEventListener("click", closeReader);

// Captures bindings
$("#caps-apply")?.addEventListener("click", () => loadCaptures(true));
$("#caps-reset")?.addEventListener("click", () => {
  $("#caps-search").value = "";
  $("#caps-source").value = "";
  $("#caps-domain").value = "";
  if ($("#caps-language")) $("#caps-language").value = "";
  $("#caps-start").value = "";
  $("#caps-end").value = "";
  applyCapsHideDuplicates(CAPS_HIDE_DUP_DEFAULT);
  writeCapsHideDuplicates(CAPS_HIDE_DUP_DEFAULT);
  if ($("#caps-mode")) $("#caps-mode").value = "auto";
  loadCaptures(true);
});
$("#caps-search")?.addEventListener("input", () => {
  clearTimeout(capsSearchTimer);
  capsSearchTimer = setTimeout(() => loadCaptures(true), 300);
});
$("#caps-source")?.addEventListener("change", () => loadCaptures(true));
$("#caps-mode")?.addEventListener("change", () => loadCaptures(true));
// Domain + language text fields: keep browse/search filters in sync with the
// form (Enter applies; change fires on commit/blur). Chips already call loadCaptures.
function applyCapsTextFilter(ev) {
  if (ev && ev.type === "keydown" && ev.key !== "Enter") return;
  if (ev && ev.type === "keydown") ev.preventDefault();
  syncFacetChipSelection();
  void loadCaptures(true);
}
$("#caps-language")?.addEventListener("change", applyCapsTextFilter);
$("#caps-language")?.addEventListener("keydown", applyCapsTextFilter);
$("#caps-domain")?.addEventListener("change", applyCapsTextFilter);
$("#caps-domain")?.addEventListener("keydown", applyCapsTextFilter);
$("#caps-unique")?.addEventListener("change", () => {
  writeCapsHideDuplicates(!!$("#caps-unique").checked);
  loadCaptures(true);
});
$("#caps-prev")?.addEventListener("click", () => { caps.offset = Math.max(0, caps.offset - caps.limit); loadCaptures(false); });
$("#caps-next")?.addEventListener("click", () => { caps.offset += caps.limit; loadCaptures(false); });
$("#jobs-refresh")?.addEventListener("click", () => loadJobs());

// Work / career search
$("#work-search-btn")?.addEventListener("click", () => runWorkSearch());
$("#work-q")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); void runWorkSearch(); }
});
$("#set-save-all")?.addEventListener("click", () => saveAllSettings());
$("#set-reload")?.addEventListener("click", () => loadSettings());
// Warn before closing the tab with unsaved settings edits.
window.addEventListener("beforeunload", (e) => {
  if (!settingsDirty) return;
  e.preventDefault();
  e.returnValue = "";
});

// Mobile nav
$("#mobile-nav-btn")?.addEventListener("click", () => {
  const sb = $(".sidebar");
  const open = sb.classList.toggle("is-open");
  $("#mobile-nav-btn").setAttribute("aria-expanded", String(open));
});

// API offline retry button — re-probes /healthz; the next successful
// api() call clears the banner automatically.
$("#api-offline-retry")?.addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "checking…";
  try {
    await api("/healthz");
    // success — banner already cleared by api() itself.
    void refreshDashboard();
    void refreshFeed();
    if (currentRoute === "tail") void loadTailView();
  } catch (_) {
    // still offline — banner remains.
  } finally {
    btn.disabled = false;
    btn.textContent = "retry";
  }
});

// ── Boot ──────────────────────────────────────────────────────
const initialRoute = (location.hash || "#dashboard").slice(1);
$("#bf-start") && ($("#bf-start").value = isoDay(Date.now() - 30 * 86400 * 1000));
// Restore Captures hide-duplicates before any route load hits /captures.
applyCapsHideDuplicates(readCapsHideDuplicates());
navigate(initialRoute, { push: false });
void refreshDashboard();
void refreshFeed();
setInterval(refreshDashboard, 5000);
setInterval(refreshFeed, 5000);
