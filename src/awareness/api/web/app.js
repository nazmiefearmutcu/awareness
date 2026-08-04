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
const ROUTES = ["dashboard", "captures", "work", "jobs", "tail", "analytics", "alerts", "saved", "x", "settings"];
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
  if (route === "analytics") void initAnalytics();
  if (route === "alerts") void initAlerts();
  if (route === "saved") void initSaved();
  if (route === "x") void initXView();
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
 * Summarize feed + tail fetch health from GET /metrics (process-local).
 * Pure — no DOM. Buckets feeds.fetch_attempts by outcome (ok / error /
 * retry_exhausted), sums non-200 counters, weighted p95 for feeds.fetch_seconds,
 * and derives a 0-100 health score:
 *   score = clamp(100 - 10*error_rate - 5*non200_rate, 0, 100)
 * where error_rate / non200_rate are percentages of total attempts.
 */
function summarizeFeedHealth(metricsSnap) {
  const empty = {
    attempts: 0,
    ok: 0,
    error: 0,
    retryExhausted: 0,
    non200: 0,
    tailNon200: 0,
    p95Sec: null,
    samples: 0,
    score: null,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let attempts = 0;
  let ok = 0;
  let error = 0;
  let retryExhausted = 0;
  let non200 = 0;
  let tailNon200 = 0;
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    if (n === "feeds.fetch_attempts") {
      attempts += v;
      const outcome = (c.labels && c.labels.outcome) || "";
      if (outcome === "ok") ok += v;
      else if (outcome === "retry_exhausted") retryExhausted += v;
      else error += v;
    } else if (n === "feeds.fetch_non_200") {
      non200 += v;
    } else if (n === "tail.fetch_non_200") {
      tailNon200 += v;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let weightedP95 = 0;
  let samples = 0;
  for (const h of hists) {
    if (!h || h.name !== "feeds.fetch_seconds") continue;
    const c = Number(h.count) || 0;
    if (c <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    samples += c;
    weightedP95 += p95 * c;
  }
  const p95Sec = samples > 0 ? weightedP95 / samples : null;
  let score = null;
  if (attempts > 0) {
    const errorRate = 100 * error / attempts;
    const non200Rate = 100 * non200 / attempts;
    score = Math.round(Math.max(0, Math.min(100, 100 - 10 * errorRate - 5 * non200Rate)));
  }
  return { attempts, ok, error, retryExhausted, non200, tailNon200, p95Sec, samples, score };
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
 * Worker task wall-clock + failure metrics from /metrics (process-local).
 * Pure — no DOM. Surfaces partition SLA latency and fail outcomes
 * (retry / dead_letter / no_adapter) without ranking concerns.
 */
function summarizeTaskMetrics(metricsSnap) {
  const empty = {
    completed: 0,
    failed: 0,
    retry: 0,
    deadLetter: 0,
    noAdapter: 0,
    durationP95: null,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let completed = 0;
  let failed = 0;
  let retry = 0;
  let deadLetter = 0;
  let noAdapter = 0;
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    const outcome = (c.labels && c.labels.outcome) || "";
    if (n === "tasks.completed") completed += v;
    else if (n === "tasks.failed") {
      failed += v;
      if (outcome === "retry") retry += v;
      else if (outcome === "dead_letter") deadLetter += v;
      else if (outcome === "no_adapter") noAdapter += v;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let weighted = 0;
  let histCount = 0;
  for (const h of hists) {
    if (!h || h.name !== "tasks.duration_seconds") continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    histCount += n;
    weighted += p95 * n;
  }
  return {
    completed,
    failed,
    retry,
    deadLetter,
    noAdapter,
    durationP95: histCount > 0 ? weighted / histCount : null,
  };
}

/**
 * WARC range-repair fetch/parse metrics from /metrics (process-local).
 * Pure — no DOM. Surfaces targeted Common Crawl byte-range repair health
 * (fetch outcomes, parse emit rate, latency) without ranking concerns.
 */
function summarizeWarcRepairMetrics(metricsSnap) {
  const empty = {
    docsEmitted: 0,
    fetchAttempts: 0,
    fetchOk: 0,
    fetchHttpError: 0,
    fetchNetworkError: 0,
    parseAttempts: 0,
    parseEmitted: 0,
    parseEmpty: 0,
    fetchP95: null,
    parseP95: null,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let docsEmitted = 0;
  let fetchAttempts = 0;
  let fetchOk = 0;
  let fetchHttpError = 0;
  let fetchNetworkError = 0;
  let parseAttempts = 0;
  let parseEmitted = 0;
  let parseEmpty = 0;
  for (const c of counters) {
    if (!c) continue;
    const n = c.name;
    const v = Number(c.value) || 0;
    const outcome = (c.labels && c.labels.outcome) || "";
    if (n === "warc_repair.docs_emitted") docsEmitted += v;
    else if (n === "warc_repair.fetch_attempts") {
      fetchAttempts += v;
      if (outcome === "ok") fetchOk += v;
      else if (outcome === "http_error") fetchHttpError += v;
      else if (outcome === "network_error") fetchNetworkError += v;
    } else if (n === "warc_repair.parse_attempts") {
      parseAttempts += v;
      if (outcome === "emitted") parseEmitted += v;
      else if (outcome === "empty") parseEmpty += v;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let fetchWeighted = 0;
  let fetchCount = 0;
  let parseWeighted = 0;
  let parseCount = 0;
  for (const h of hists) {
    if (!h) continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    if (h.name === "warc_repair.fetch_seconds") {
      fetchCount += n;
      fetchWeighted += p95 * n;
    } else if (h.name === "warc_repair.parse_seconds") {
      parseCount += n;
      parseWeighted += p95 * n;
    }
  }
  return {
    docsEmitted,
    fetchAttempts,
    fetchOk,
    fetchHttpError,
    fetchNetworkError,
    parseAttempts,
    parseEmitted,
    parseEmpty,
    fetchP95: fetchCount > 0 ? fetchWeighted / fetchCount : null,
    parseP95: parseCount > 0 ? parseWeighted / parseCount : null,
  };
}

/**
 * FTS path metrics from /metrics (full rebuild / incremental / restore).
 * Pure — no DOM. Used by dashboard KPIs so operators see whether search is
 * paying for rematerialize vs fingerprint restore (not ranking-related).
 */
function summarizeFtsMetrics(metricsSnap) {
  const empty = {
    builds: 0,
    full: 0,
    incremental: 0,
    restore: 0,
    errors: 0,
    buildP95: null,
    indexedRows: 0,
  };
  if (!metricsSnap || typeof metricsSnap !== "object") return empty;
  const counters = Array.isArray(metricsSnap.counters) ? metricsSnap.counters : [];
  let builds = 0;
  let full = 0;
  let incremental = 0;
  let restore = 0;
  let errors = 0;
  for (const c of counters) {
    if (!c) continue;
    const v = Number(c.value) || 0;
    if (c.name === "fts.builds") {
      builds += v;
      const mode = (c.labels || {}).mode;
      if (mode === "full") full += v;
      else if (mode === "incremental") incremental += v;
      else if (mode === "restore") restore += v;
    } else if (c.name === "fts.build_errors") {
      errors += v;
    }
  }
  const gauges = Array.isArray(metricsSnap.gauges) ? metricsSnap.gauges : [];
  let indexedRows = 0;
  for (const g of gauges) {
    if (g && g.name === "fts.indexed_rows") {
      const v = Number(g.value);
      if (Number.isFinite(v)) indexedRows = v;
    }
  }
  const hists = Array.isArray(metricsSnap.histograms) ? metricsSnap.histograms : [];
  let weightedP95 = 0;
  let histCount = 0;
  for (const h of hists) {
    if (!h || h.name !== "fts.build_seconds") continue;
    // Success-path series are labeled by mode only; skip error outcome series.
    const labels = h.labels || {};
    if (labels.outcome === "error") continue;
    const n = Number(h.count) || 0;
    if (n <= 0) continue;
    const p95 = Number(h.p95);
    if (!Number.isFinite(p95)) continue;
    histCount += n;
    weightedP95 += p95 * n;
  }
  return {
    builds,
    full,
    incremental,
    restore,
    errors,
    buildP95: histCount > 0 ? weightedP95 / histCount : null,
    indexedRows,
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

/** Last rendered feed-health summary — skip re-render when nothing changed. */
let lastFeedHealthSnap = null;

/**
 * Render the dashboard "Feed health" band (4 KPI articles + score badge).
 * DOM only — the math lives in summarizeFeedHealth(). All values are set via
 * el()/textContent, never innerHTML.
 */
function renderFeedHealth(h) {
  const band = $("#feed-health-band");
  if (!band) return;
  const key = JSON.stringify(h);
  if (key === lastFeedHealthSnap) return;
  lastFeedHealthSnap = key;

  const scoreNode = $("#feed-health-score");
  if (scoreNode) {
    if (h.score == null) {
      scoreNode.textContent = "—";
      scoreNode.className = "feed-health-score";
    } else {
      scoreNode.textContent = h.score + "/100";
      scoreNode.className = "feed-health-score is-" + (h.score >= 80 ? "good" : h.score >= 50 ? "mid" : "bad");
    }
  }

  const grid = $("#feed-health-kpis");
  if (!grid) return;
  clear(grid);

  const outcomeBits = [];
  if (h.attempts > 0) {
    if (h.ok) outcomeBits.push(`${fmt(h.ok)} ok`);
    if (h.error) outcomeBits.push(`${fmt(h.error)} err`);
    if (h.retryExhausted) outcomeBits.push(`${fmt(h.retryExhausted)} retry`);
  } else {
    outcomeBits.push("no attempts yet");
  }
  grid.appendChild(
    el("article", { class: "kpi" },
      el("div", { class: "kpi-label", text: "Feed attempts" }),
      el("div", { class: "kpi-value" + (h.attempts ? "" : " is-zero"), text: fmt(h.attempts) }),
      el("div", { class: "kpi-sub", text: outcomeBits.join(" · ") })
    )
  );

  grid.appendChild(
    el("article", { class: "kpi" },
      el("div", { class: "kpi-label", text: "Feed non-200" }),
      el("div", { class: "kpi-value" + (h.non200 ? "" : " is-zero"), text: fmt(h.non200) }),
      el("div", { class: "kpi-sub", text: h.non200 ? "non-200 responses · process" : "no non-200 responses" })
    )
  );

  grid.appendChild(
    el("article", { class: "kpi" },
      el("div", { class: "kpi-label", text: "Feed fetch p95" }),
      el("div", { class: "kpi-value" + (h.samples ? "" : " is-zero"), text: formatFetchLatency(h.p95Sec) }),
      el("div", { class: "kpi-sub", text: h.samples ? `${fmt(h.samples)} samples · feeds` : "no samples yet" })
    )
  );

  grid.appendChild(
    el("article", { class: "kpi" },
      el("div", { class: "kpi-label", text: "Tail non-200" }),
      el("div", { class: "kpi-value" + (h.tailNon200 ? "" : " is-zero"), text: fmt(h.tailNon200) }),
      el("div", { class: "kpi-sub", text: h.tailNon200 ? "recrawl HTTP non-200" : "no recrawl errors" })
    )
  );
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

  // Feed health band: outcome buckets, non-200, fetch p95, health score.
  renderFeedHealth(summarizeFeedHealth(metricsSnap));

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

  const fts = summarizeFtsMetrics(metricsSnap);
  setKPI("kpi-dash-fts-builds", fts.builds);
  const ftsBuildsSub = $("#kpi-dash-fts-builds-sub");
  if (ftsBuildsSub) {
    if (fts.builds) {
      const bits = [];
      if (fts.full) bits.push(`${fmt(fts.full)} full`);
      if (fts.incremental) bits.push(`${fmt(fts.incremental)} incr`);
      if (fts.restore) bits.push(`${fmt(fts.restore)} restore`);
      if (fts.errors) bits.push(`${fmt(fts.errors)} err`);
      ftsBuildsSub.textContent = bits.join(" · ") || "path counters this process";
    } else {
      ftsBuildsSub.textContent = "full · incremental · restore";
    }
  }
  const ftsP95Node = $("#kpi-dash-fts-p95");
  if (ftsP95Node) {
    ftsP95Node.textContent = formatFetchLatency(fts.buildP95);
    ftsP95Node.classList.toggle("is-zero", !fts.builds && fts.buildP95 == null);
  }
  const ftsP95Sub = $("#kpi-dash-fts-p95-sub");
  if (ftsP95Sub) {
    ftsP95Sub.textContent = fts.builds
      ? `${fmt(fts.builds)} builds · process`
      : "index materialize latency";
  }
  setKPI("kpi-dash-fts-rows", fts.indexedRows);
  const ftsRowsSub = $("#kpi-dash-fts-rows-sub");
  if (ftsRowsSub) {
    ftsRowsSub.textContent = fts.indexedRows
      ? "captures_idx after last build"
      : "no FTS materialize yet";
  }

  const warc = summarizeWarcRepairMetrics(metricsSnap);
  setKPI("kpi-dash-warc-docs", warc.docsEmitted);
  const warcDocsSub = $("#kpi-dash-warc-docs-sub");
  if (warcDocsSub) {
    if (warc.parseAttempts) {
      warcDocsSub.textContent = `${fmt(warc.parseEmitted)} emitted · ${fmt(warc.parseEmpty)} empty parse`;
    } else {
      warcDocsSub.textContent = "range-repair captures this process";
    }
  }
  const warcFetchP95Node = $("#kpi-dash-warc-fetch-p95");
  if (warcFetchP95Node) {
    warcFetchP95Node.textContent = formatFetchLatency(warc.fetchP95);
    warcFetchP95Node.classList.toggle(
      "is-zero",
      !warc.fetchAttempts && warc.fetchP95 == null
    );
  }
  const warcFetchP95Sub = $("#kpi-dash-warc-fetch-p95-sub");
  if (warcFetchP95Sub) {
    warcFetchP95Sub.textContent = warc.fetchAttempts
      ? `${fmt(warc.fetchOk)} ok / ${fmt(warc.fetchAttempts)} attempts · process`
      : "byte-range fetch latency";
  }
  setKPI("kpi-dash-warc-attempts", warc.fetchAttempts);
  const warcAttSub = $("#kpi-dash-warc-attempts-sub");
  if (warcAttSub) {
    if (warc.fetchAttempts) {
      const bits = [`${fmt(warc.fetchOk)} ok`];
      if (warc.fetchHttpError) bits.push(`${fmt(warc.fetchHttpError)} http`);
      if (warc.fetchNetworkError) bits.push(`${fmt(warc.fetchNetworkError)} net`);
      warcAttSub.textContent = bits.join(" · ");
    } else {
      warcAttSub.textContent = "range-fetch attempts this process";
    }
  }

  const tasks = summarizeTaskMetrics(metricsSnap);
  setKPI("kpi-dash-tasks-completed", tasks.completed);
  const tasksDoneSub = $("#kpi-dash-tasks-completed-sub");
  if (tasksDoneSub) {
    tasksDoneSub.textContent = tasks.completed
      ? "worker partitions this process"
      : "no completed partitions yet";
  }
  const tasksP95Node = $("#kpi-dash-tasks-p95");
  if (tasksP95Node) {
    tasksP95Node.textContent = formatFetchLatency(tasks.durationP95);
    tasksP95Node.classList.toggle(
      "is-zero",
      !tasks.completed && !tasks.failed && tasks.durationP95 == null
    );
  }
  const tasksP95Sub = $("#kpi-dash-tasks-p95-sub");
  if (tasksP95Sub) {
    const samples = tasks.completed + tasks.failed;
    tasksP95Sub.textContent = samples
      ? `${fmt(samples)} timed · process`
      : "wall-clock partition latency";
  }
  setKPI("kpi-dash-tasks-failed", tasks.failed);
  const tasksFailSub = $("#kpi-dash-tasks-failed-sub");
  if (tasksFailSub) {
    if (tasks.failed) {
      const bits = [];
      if (tasks.retry) bits.push(`${fmt(tasks.retry)} retry`);
      if (tasks.deadLetter) bits.push(`${fmt(tasks.deadLetter)} dead`);
      if (tasks.noAdapter) bits.push(`${fmt(tasks.noAdapter)} no-adapter`);
      tasksFailSub.textContent = bits.join(" · ") || "failure outcomes this process";
    } else {
      tasksFailSub.textContent = "retry · dead-letter · no-adapter";
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

  // Saved-search widgets (non-blocking; self-guards against rebuilds).
  void refreshDashSaved();

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

// ── Analytics ─────────────────────────────────────────────────
let analyticsReady = false;

/** Render a tiny inline bar chart (no deps) into a container. */
function renderBarChart(el, points, { color = "var(--accent, #0e9b8d)", maxBars = 60 } = {}) {
  el.textContent = "";
  if (!points || !points.length) { el.textContent = "—"; return; }
  const bars = points.slice(-maxBars);
  const max = Math.max(1, ...bars.map((p) => p.count));
  const wrap = document.createElement("div");
  wrap.style.cssText = "display:flex;align-items:flex-end;gap:2px;height:120px;overflow-x:auto;";
  for (const p of bars) {
    const col = document.createElement("div");
    const h = Math.max(2, Math.round((p.count / max) * 110));
    col.style.cssText = `width:14px;height:${h}px;background:${color};border-radius:2px 2px 0 0;flex:0 0 auto;`;
    col.title = `${p.ts ? p.ts.slice(0, 10) : "?"} — ${p.count}`;
    wrap.appendChild(col);
  }
  el.appendChild(wrap);
}

/** Render clickable chips. */
function renderChips(el, items, { key = (x) => x, label = (x) => x } = {}) {
  el.textContent = "";
  if (!items || !items.length) { el.textContent = "—"; return; }
  for (const it of items) {
    const chip = el("button", { class: "chip", type: "button" }, label(it));
    chip.addEventListener("click", () => {
      $("#an-term-input").value = key(it);
      void analyzeTerm();
    });
    el.appendChild(chip);
  }
}

async function analyzeTerm() {
  const term = ($("#an-term-input").value || "").trim();
  if (!term) { toast("enter a term first", "err"); return; }
  const windowDays = $("#an-term-window").value || "14";
  try {
    const [freq, spikes] = await Promise.all([
      api(`/analytics/term-frequency?term=${encodeURIComponent(term)}&window_days=${windowDays}`),
      api(`/analytics/spikes?term=${encodeURIComponent(term)}&window_days=${windowDays}`),
    ]);
    renderBarChart($("#an-term-chart"), freq);
    const body = $("#an-spikes-body");
    body.textContent = "";
    if (!spikes || !spikes.length) {
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 4;
      cell.textContent = "No spikes detected in this window.";
    } else {
      for (const s of spikes) {
        const row = body.insertRow();
        row.insertCell().textContent = s.bucket ? s.bucket.slice(0, 10) : "?";
        row.insertCell().textContent = s.count;
        row.insertCell().textContent = Number(s.zscore).toFixed(2);
        row.insertCell().textContent = s.vs_mean != null ? Number(s.vs_mean).toFixed(1) : "—";
      }
    }
  } catch (err) { toast("analytics failed: " + err.message, "err"); }
}

// ── GDELT comparison band ────────────────────────────────────
// Reuses the term-frequency band's term + window inputs; the bridge clamps
// window_days to its own 60-day API cap (the term-frequency select goes to 90).

/** One-line stats for the GDELT comparison. Pure — no DOM. */
function gdeltStatsText(comparison) {
  const rRaw = comparison.correlation_r;
  const rText = rRaw == null || !Number.isFinite(Number(rRaw)) ? "—" : Number(rRaw).toFixed(2);
  const parts = [
    "local " + (comparison.local_count ?? 0),
    "GDELT " + (comparison.gdelt_count ?? 0),
    "r " + rText,
  ];
  const note = String(comparison.note || "").trim();
  if (note) parts.push(note);
  return parts.join(" · ");
}

/** Render the compare payload: two side-by-side bar charts + stats line.
 *  When GDELT returned no series (API unavailable) the external chart is
 *  replaced by a dim notice and the backend note is shown verbatim. */
function renderGdeltComparison(comparison) {
  const localBox = $("#an-gdelt-local");
  const extBox = $("#an-gdelt-ext");
  const stats = $("#an-gdelt-stats");
  if (!localBox || !extBox || !stats) return;
  renderBarChart(localBox, comparison.local_series || []);
  const extSeries = comparison.gdelt_series || [];
  extBox.textContent = "";
  if (extSeries.length) {
    renderBarChart(extBox, extSeries, { color: "var(--gdelt, #c9a227)" });
  } else {
    extBox.appendChild(el("span", { class: "an-gdelt-offline", text: "GDELT unavailable" }));
  }
  stats.textContent = "";
  stats.appendChild(document.createTextNode(gdeltStatsText(comparison)));
}

async function compareWithGdelt() {
  const term = ($("#an-term-input").value || "").trim();
  if (!term) { toast("enter a term first", "err"); return; }
  const windowDays = Number($("#an-term-window").value) || 14;
  const gdeltDays = Math.min(windowDays, 60); // /gdelt/compare caps at 60
  const btn = $("#an-gdelt-go");
  if (btn) btn.disabled = true;
  try {
    const comparison = await api(`/gdelt/compare?term=${encodeURIComponent(term)}&window_days=${gdeltDays}`);
    renderGdeltComparison(comparison);
  } catch (err) { toast("gdelt compare failed: " + err.message, "err"); }
  finally { if (btn) btn.disabled = false; }
}

async function loadCoOccurring() {
  const term = ($("#an-co-input").value || "").trim();
  if (!term) { toast("enter a term first", "err"); return; }
  try {
    const co = await api(`/analytics/co-occurring?term=${encodeURIComponent(term)}&limit=40`);
    renderChips($("#an-co-terms"), co || [], { key: (x) => x.term, label: (x) => `${x.term} ×${x.count}` });
  } catch (err) { toast("co-occurrence failed: " + err.message, "err"); }
}

async function initAnalytics() {
  if (analyticsReady) return;
  analyticsReady = true;
  const goBtn = $("#an-term-go");
  const coBtn = $("#an-co-go");
  goBtn.addEventListener("click", analyzeTerm);
  $("#an-term-input").addEventListener("keydown", (e) => { if (e.key === "Enter") analyzeTerm(); });
  const gdeltBtn = $("#an-gdelt-go");
  if (gdeltBtn) gdeltBtn.addEventListener("click", compareWithGdelt);
  coBtn.addEventListener("click", loadCoOccurring);
  $("#an-co-input").addEventListener("keydown", (e) => { if (e.key === "Enter") loadCoOccurring(); });
  const netBtn = $("#an-entity-build");
  netBtn.addEventListener("click", () => void buildEntityNetwork($("#an-entity-input").value));
  $("#an-entity-input").addEventListener("keydown", (e) => { if (e.key === "Enter") buildEntityNetwork($("#an-entity-input").value); });

  const [top, entities, domains] = await Promise.all([
    api("/analytics/top-terms?limit=40"),
    api("/entities/top?limit=40").catch(() => []),
    api("/source-intel/domains?limit=20").catch(() => []),
  ]);
  renderChips($("#an-top-terms"), top || [], { key: (x) => x.term, label: (x) => `${x.term} ×${x.count}` });
  renderChips($("#an-entities"), entities || [], {
    key: (x) => x.text, label: (x) => `${x.text} [${x.kind}] ×${x.count}`,
  });
  const firstEntity = entities && entities.length ? entities[0].text : "";
  if (firstEntity) {
    $("#an-entity-input").value = firstEntity;
    void buildEntityNetwork(firstEntity);
  }
  const body = $("#an-domains-body");
  body.textContent = "";
  if (!domains || !domains.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 6;
    cell.textContent = "No corpus yet — start tail or run a backfill.";
  } else {
    for (const d of domains) {
      const row = body.insertRow();
      row.insertCell().textContent = d.domain;
      row.insertCell().textContent = Number(d.score).toFixed(3);
      row.insertCell().textContent = d.captures;
      row.insertCell().textContent = d.replication_ratio != null ? Number(d.replication_ratio).toFixed(3) : "—";
      row.insertCell().textContent = d.avg_length != null ? Math.round(d.avg_length) : "—";
      row.insertCell().textContent = d.velocity != null ? Number(d.velocity).toFixed(2) : "—";
    }
  }
  if ($("#an-term-input").value) void analyzeTerm();
}

// ── Entity network ────────────────────────────────────────────
/** Pure ring layout: `count` points evenly spaced on a circle of `radius`.
 * Returns [{angle, x, y}] with angle = 2πi/count; x/y centered on (0, 0). */
function entityNetworkLayout(count, radius) {
  const points = [];
  for (let i = 0; i < count; i++) {
    const angle = (2 * Math.PI * i) / count;
    points.push({ angle, x: radius * Math.cos(angle), y: radius * Math.sin(angle) });
  }
  return points;
}

/** Fetch co-occurring entities and render a concentric (root + ring) graph. */
async function buildEntityNetwork(rootEntity) {
  const name = String(rootEntity || "").trim();
  if (!name) { toast("enter an entity first", "err"); return; }
  const container = $("#an-entity-network");
  if (!container) return;
  try {
    const nodes = await api(`/entities/co-occurring?entity=${encodeURIComponent(name)}&limit=12`);
    container.textContent = "";
    if (!nodes || !nodes.length) { container.textContent = "—"; return; }
    const size = 280;
    const center = size / 2;
    const radius = 100;
    const layout = entityNetworkLayout(nodes.length, radius);
    const maxCount = Math.max(1, ...nodes.map((n) => n.count));
    const svg = el("svg", {
      class: "an-network-edges",
      width: size, height: size,
      viewBox: "0 0 " + size + " " + size,
      "aria-hidden": "true",
    });
    const rootChip = el("button", {
      class: "an-node an-root-node",
      type: "button",
      title: `${name} — root`,
      style: { left: (center - 34) + "px", top: (center - 34) + "px", width: "68px", height: "68px" },
    }, name);
    rootChip.addEventListener("click", () => void buildEntityNetwork(name));
    const ring = [];
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      const p = layout[i];
      const cx = center + p.x;
      const cy = center + p.y;
      svg.appendChild(el("line", { x1: center, y1: center, x2: cx, y2: cy, class: "an-edge" }));
      const d = 20 + Math.round((node.count / maxCount) * 26);
      const chip = el("button", {
        class: "an-node",
        type: "button",
        title: `${node.entity} [${node.kind}] — ${node.count}`,
        style: { left: (cx - d / 2) + "px", top: (cy - d / 2) + "px", width: d + "px", height: d + "px" },
      }, node.entity);
      chip.addEventListener("click", () => void buildEntityNetwork(node.entity));
      ring.push(chip);
    }
    container.appendChild(svg);
    container.appendChild(rootChip);
    for (const chip of ring) container.appendChild(chip);
  } catch (err) { toast("entity network failed: " + err.message, "err"); }
}

// ── Alerts ────────────────────────────────────────────────────
let alertsReady = false;

async function initAlerts() {
  if (alertsReady) return;
  alertsReady = true;
  $("#al-refresh")?.addEventListener("click", () => void loadAlertsView());
  $("#al-form")?.addEventListener("submit", createAlertRule);
  await loadAlertsView();
}

async function loadAlertsView() {
  try {
    const [rules, status, firings] = await Promise.all([
      api("/alerts/rules"),
      api("/alerts/status"),
      api("/alerts/firings?limit=20"),
    ]);
    renderAlertsRules(rules || []);
    updateAlertsStatus(status || {});
    renderAlertsFirings(firings || []);
  } catch (err) {
    toast("alerts load failed: " + err.message, "err");
  }
}

function updateAlertsStatus(status) {
  const set = (id, text) => {
    const node = $(id);
    if (node) node.textContent = text;
  };
  set("#al-status-total", fmt(status.rules_total));
  set("#al-status-active", fmt(status.rules_active));
  set("#al-status-firings", fmt(status.firings_24h));
  set("#al-status-last", status.last_firing ? ago(status.last_firing, false) : "—");
}

function renderAlertsRules(rules) {
  const body = $("#al-rules-body");
  if (!body) return;
  clear(body);
  if (!rules.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 10;
    cell.textContent = "No rules yet — create one below.";
    return;
  }
  for (const r of rules) {
    const row = body.insertRow();
    row.insertCell().textContent = r.name || "—";
    row.insertCell().textContent = r.kind || "—";
    row.insertCell().textContent = r.term || "—";
    row.insertCell().textContent = fmt(r.threshold);
    row.insertCell().textContent = fmt(r.window_hours) + "h";
    row.insertCell().textContent = fmt(r.cooldown_minutes) + "m";
    const toggleCell = row.insertCell();
    const toggle = el("input", {
      type: "checkbox",
      class: "al-toggle",
      "aria-label": "Active: " + (r.name || r.term || r.id),
      checked: !!r.active,
    });
    toggle.addEventListener("change", () => void toggleAlertRule(r.id, toggle.checked));
    toggleCell.appendChild(toggle);
    const url = r.webhook_url || "";
    row.insertCell().textContent = url
      ? (url.length > 42 ? url.slice(0, 42) + "…" : url)
      : "—";
    row.insertCell().textContent = r.created_at
      ? new Date(r.created_at).toISOString().slice(0, 10)
      : "—";
    const actCell = row.insertCell();
    const testBtn = el("button", { class: "btn btn-link al-test-btn", type: "button", text: "Test" });
    testBtn.addEventListener("click", () => void runAlertsCheck());
    actCell.appendChild(testBtn);
    const delBtn = el("button", { class: "btn btn-link al-del-btn", type: "button", text: "Delete" });
    delBtn.addEventListener("click", () => void deleteAlertRule(r.id, r.name));
    actCell.appendChild(delBtn);
  }
}

async function toggleAlertRule(ruleId, active) {
  try {
    await api("/alerts/rules/" + encodeURIComponent(ruleId), {
      method: "PUT",
      body: JSON.stringify({ active }),
    });
    toast(active ? "rule enabled" : "rule disabled", "ok");
  } catch (err) {
    toast("update failed: " + err.message, "err");
  } finally {
    void loadAlertsView();
  }
}

async function deleteAlertRule(ruleId, name) {
  if (!window.confirm(`Delete alert rule "${name || ruleId}"?`)) return;
  try {
    await api("/alerts/rules/" + encodeURIComponent(ruleId), { method: "DELETE" });
    toast("rule deleted", "ok");
  } catch (err) {
    toast("delete failed: " + err.message, "err");
  } finally {
    void loadAlertsView();
  }
}

/** Run a full evaluation pass (all active rules); show firings in the panel. */
async function runAlertsCheck() {
  const panel = $("#al-test-panel");
  const body = $("#al-test-body");
  if (panel) panel.hidden = false;
  if (body) {
    clear(body);
    body.appendChild(document.createTextNode("Evaluating rules…"));
  }
  try {
    const res = await api("/alerts/check", { method: "POST", body: "{}" });
    const firings = Array.isArray(res.firings) ? res.firings : [];
    const deliveries = Array.isArray(res.deliveries) ? res.deliveries : [];
    if (body) {
      clear(body);
      body.appendChild(el("strong", { class: "al-test-count", text: `${firings.length} firing${firings.length === 1 ? "" : "s"}` }));
      const last = firings[firings.length - 1];
      if (last) {
        const when = last.fired_at ? ago(last.fired_at, false) : "just now";
        body.appendChild(document.createTextNode(" · last: "));
        body.appendChild(el("span", { class: "al-test-last", text: `${last.rule_name || last.rule_id} — ${last.term} count ${last.count} vs ${last.threshold} (${when})` }));
      } else {
        body.appendChild(document.createTextNode(" · all active rules evaluated clean"));
      }
      if (deliveries.length) {
        const okDel = deliveries.filter((d) => d.delivered).length;
        body.appendChild(el("span", { class: "al-test-del", text: ` · webhooks ${okDel}/${deliveries.length} delivered` }));
      }
    }
    toast(`check complete: ${firings.length} firing(s)`, firings.length ? "err" : "ok");
  } catch (err) {
    if (body) body.textContent = "check failed: " + err.message;
    toast("check failed: " + err.message, "err");
  }
}

async function createAlertRule(e) {
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  try {
    const name = ($("#al-name").value || "").trim();
    if (!name) { toast("name is required", "err"); return; }
    const term = ($("#al-term").value || "").trim();
    if (!term) { toast("term is required", "err"); return; }
    const body = {
      name,
      kind: $("#al-kind").value,
      term,
      threshold: Number($("#al-threshold").value),
      window_hours: Number($("#al-window").value),
      cooldown_minutes: Number($("#al-cooldown").value),
      webhook_url: ($("#al-webhook").value || "").trim() || null,
      active: $("#al-active").checked,
    };
    const created = await api("/alerts/rules", { method: "POST", body: JSON.stringify(body) });
    toast(`rule "${created.name || body.name}" created`, "ok");
    e.target.reset();
    $("#al-active").checked = true;
    void loadAlertsView();
  } catch (err) {
    // api() surfaces the backend 400 detail inside err.message.
    toast("create failed: " + err.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function renderAlertsFirings(firings) {
  const body = $("#al-firings-body");
  if (!body) return;
  clear(body);
  if (!firings.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 6;
    cell.textContent = "No firings yet.";
    return;
  }
  for (const f of firings) {
    const row = body.insertRow();
    row.insertCell().textContent = f.fired_at ? new Date(f.fired_at).toLocaleString() : "—";
    row.insertCell().textContent = f.rule_name || f.rule_id || "—";
    row.insertCell().textContent = f.term || "—";
    row.insertCell().textContent = fmt(f.count);
    row.insertCell().textContent = fmt(f.threshold);
    row.insertCell().textContent = f.detail || "—";
  }
}

// ── Saved searches ────────────────────────────────────────────
let savedReady = false;

function truncateText(text, max) {
  const s = String(text || "");
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

async function initSaved() {
  if (savedReady) return;
  savedReady = true;
  $("#sv-refresh")?.addEventListener("click", () => void loadSavedView());
  await loadSavedView();
}

async function loadSavedView() {
  const list = $("#saved-list");
  if (!list) return;
  clear(list);
  list.appendChild(el("p", { class: "muted", text: "loading…" }));
  try {
    const saved = await api("/saved");
    renderSavedList(saved || []);
  } catch (err) {
    clear(list);
    list.appendChild(el("p", { class: "muted", text: "saved searches failed: " + err.message }));
  }
}

function renderSavedList(saved) {
  const list = $("#saved-list");
  if (!list) return;
  clear(list);
  if (!saved.length) {
    list.appendChild(el("p", { class: "muted", text: "No saved searches yet — bookmark a query from Captures with ★ Save." }));
    return;
  }
  for (const s of saved) {
    const card = el("article", { class: "saved-card" + (s.pinned ? " is-pinned" : "") });

    const head = el("div", { class: "saved-card-head" });
    const pin = el("button", {
      class: "saved-pin",
      type: "button",
      "aria-label": (s.pinned ? "Unpin" : "Pin") + ": " + s.name,
      title: s.pinned ? "Unpin" : "Pin to top",
      text: s.pinned ? "★" : "☆",
    });
    pin.addEventListener("click", () => void toggleSavedPin(s.id, !s.pinned));
    head.appendChild(pin);
    head.appendChild(el("strong", { class: "saved-name", text: s.name || "—" }));
    head.appendChild(el("span", { class: "saved-mode badge", text: s.mode || "auto" }));
    head.appendChild(el("span", { class: "saved-when", text: s.updated_at ? "ran " + ago(s.updated_at, true) + " ago" : "" }));
    card.appendChild(head);

    card.appendChild(el("code", { class: "saved-query", title: s.query, text: truncateText(s.query, 120) }));

    const meta = el("div", { class: "saved-card-meta" });
    meta.appendChild(el("span", { class: "saved-meta-bit", text: "fields: " + (s.fields || "title,text") }));
    meta.appendChild(el("span", { class: "saved-meta-bit", text: "limit: " + fmt(s.limit) }));
    card.appendChild(meta);

    const actions = el("div", { class: "saved-actions" });
    const runBtn = el("button", { class: "btn btn-primary", type: "button", text: "Run" });
    runBtn.addEventListener("click", () => void runSaved(s));
    actions.appendChild(runBtn);
    const editBtn = el("button", { class: "btn btn-ghost", type: "button", text: "Edit" });
    editBtn.addEventListener("click", () => void editSavedName(s));
    actions.appendChild(editBtn);
    const delBtn = el("button", { class: "btn btn-link", type: "button", text: "Delete" });
    delBtn.addEventListener("click", () => void deleteSaved(s.id, s.name));
    actions.appendChild(delBtn);
    card.appendChild(actions);

    list.appendChild(card);
  }
}

async function toggleSavedPin(savedId, pinned) {
  try {
    await api("/saved/" + encodeURIComponent(savedId) + "/pin", {
      method: "POST",
      body: JSON.stringify({ pinned }),
    });
    toast(pinned ? "pinned to top" : "unpinned", "ok");
  } catch (err) {
    toast("pin failed: " + err.message, "err");
  } finally {
    void loadSavedView();
  }
}

async function runSaved(s) {
  const band = $(".sv-run-band");
  const meta = $("#saved-run-meta");
  const list = $("#saved-run-list");
  if (band) band.hidden = false;
  if (meta) meta.textContent = "running…";
  if (list) clear(list);
  try {
    const res = await api("/saved/" + encodeURIComponent(s.id) + "/run");
    const rows = (res && res.rows) || [];
    if (meta) {
      const modeLabel = formatSearchModeLabel(res.mode, !!res.ranked);
      meta.textContent =
        `${fmt(res.total || 0)} match${(res.total || 0) === 1 ? "" : "es"} · ${modeLabel}`;
    }
    if (list) {
      renderCaps(list, rows, { search: String(s.query || ""), ranked: !!res.ranked });
    }
    if (band && !rows.length) {
      if (meta) meta.textContent = "no matches";
      toast("no results for this saved search", "err");
    }
  } catch (err) {
    if (meta) meta.textContent = "run failed: " + err.message;
    toast("run failed: " + err.message, "err");
  } finally {
    void loadSavedView();
  }
}

async function editSavedName(s) {
  const name = window.prompt("Rename saved search", s.name);
  if (name == null) return;
  const trimmed = name.trim();
  if (!trimmed) { toast("name is required", "err"); return; }
  try {
    await api("/saved/" + encodeURIComponent(s.id), {
      method: "PUT",
      body: JSON.stringify({ name: trimmed }),
    });
    toast("renamed", "ok");
  } catch (err) {
    toast("edit failed: " + err.message, "err");
  } finally {
    void loadSavedView();
  }
}

async function deleteSaved(savedId, name) {
  if (!window.confirm(`Delete saved search "${name || savedId}"?`)) return;
  try {
    await api("/saved/" + encodeURIComponent(savedId), { method: "DELETE" });
    toast("saved search deleted", "ok");
  } catch (err) {
    toast("delete failed: " + err.message, "err");
  } finally {
    void loadSavedView();
  }
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

// ── X sessions ────────────────────────────────────────────────
let xViewReady = false;

async function initXView() {
  if (xViewReady) return;
  xViewReady = true;
  $("#x-refresh")?.addEventListener("click", () => void loadXView());
  $("#x-form")?.addEventListener("submit", createXSession);
  await loadXView();
}

async function loadXView() {
  const body = $("#x-sessions-body");
  if (!body) return;
  clear(body);
  body.appendChild(emptyXRow("loading…"));
  try {
    const sessions = await api("/x/sessions");
    renderXSessionList(sessions || []);
  } catch (err) {
    clear(body);
    body.appendChild(emptyXRow("sessions failed: " + err.message));
    toast("sessions load failed: " + err.message, "err");
  }
}

function emptyXRow(text) {
  const row = document.createElement("tr");
  const cell = row.insertCell();
  cell.colSpan = 6;
  cell.textContent = text;
  return row;
}

function renderXSessionList(sessions) {
  const body = $("#x-sessions-body");
  if (!body) return;
  clear(body);
  const count = $("#x-sessions-count");
  if (count) {
    count.textContent = fmt(sessions.length) + (sessions.length === 1 ? " session" : " sessions");
  }
  if (!sessions.length) {
    body.appendChild(emptyXRow("No sessions yet — create one with the form above."));
    return;
  }
  for (const s of sessions) {
    const row = body.insertRow();
    row.insertCell().textContent = s.session_id || "—";
    row.insertCell().textContent = s.title || "(untitled)";
    const statusCell = row.insertCell();
    statusCell.appendChild(el("span", { class: "badge badge-" + (s.status || "unknown"), text: s.status || "—" }));
    row.insertCell().textContent = s.created_at
      ? new Date(s.created_at).toISOString().slice(0, 16).replace("T", " ")
      : "—";
    row.insertCell().textContent = fmt((s.backfill_tweets || 0) + (s.stream_tweets || 0));
    const actCell = row.insertCell();
    const simBtn = el("button", { class: "btn btn-ghost", type: "button", text: "Simulate" });
    simBtn.addEventListener("click", () => startXSimulate(s.session_id, simBtn));
    actCell.appendChild(simBtn);
    const anBtn = el("button", { class: "btn btn-ghost", type: "button", text: "Analyze" });
    anBtn.addEventListener("click", () => void analyzeXSession(s.session_id, anBtn));
    actCell.appendChild(anBtn);
    const twBtn = el("button", { class: "btn btn-ghost", type: "button", text: "Tweets" });
    twBtn.addEventListener("click", () => void showXSessionTweets(s.session_id, twBtn));
    actCell.appendChild(twBtn);
  }
}

async function createXSession(e) {
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]');
  if (btn) btn.disabled = true;
  try {
    const title = ($("#x-title").value || "").trim();
    if (!title) { toast("title is required", "err"); return; }
    const splitList = (v) => String(v || "").split(",").map((x) => x.trim()).filter(Boolean);
    const body = {
      title,
      keywords: splitList($("#x-keywords").value),
      accounts: splitList($("#x-accounts").value),
      raw_query: ($("#x-raw-query").value || "").trim() || null,
      lookback: ($("#x-lookback").value || "").trim() || "2h",
      language: ($("#x-language").value || "").trim() || null,
    };
    const created = await api("/x/sessions", { method: "POST", body: JSON.stringify(body) });
    toast(`session "${created.title || title}" created`, "ok");
    e.target.reset();
  } catch (err) {
    // api() surfaces the backend 400 detail inside err.message.
    toast("create failed: " + err.message, "err");
  } finally {
    if (btn) btn.disabled = false;
    void loadXView();
  }
}

/** Swap a row's Simulate button for an inline count input (default 20). */
function startXSimulate(sessionId, btn) {
  const parent = btn.parentElement;
  const wrap = el("span", { class: "x-sim-inline" });
  const input = el("input", {
    class: "inp x-sim-count",
    type: "number",
    min: "1",
    max: "200",
    value: "20",
    "aria-label": "Tweet count to simulate",
  });
  const goBtn = el("button", { class: "btn btn-primary", type: "button", text: "Go" });
  const cancelBtn = el("button", { class: "btn btn-link", type: "button", text: "cancel" });
  wrap.appendChild(input);
  wrap.appendChild(goBtn);
  wrap.appendChild(cancelBtn);
  const restore = () => parent.replaceChild(btn, wrap);
  const run = () => {
    const nTweets = Math.max(1, Math.min(200, parseInt(input.value, 10) || 20));
    goBtn.disabled = true;
    void simulateXSession(sessionId, nTweets).finally(restore);
  };
  goBtn.addEventListener("click", run);
  cancelBtn.addEventListener("click", restore);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") run();
    if (e.key === "Escape") restore();
  });
  parent.replaceChild(wrap, btn);
  input.focus();
  input.select();
}

async function simulateXSession(sessionId, nTweets) {
  try {
    const res = await api("/x/sessions/" + encodeURIComponent(sessionId) + "/simulate", {
      method: "POST",
      body: JSON.stringify({ n_tweets: nTweets }),
    });
    const inserted = res && res.inserted != null ? res.inserted : nTweets;
    toast(`simulated ${fmt(inserted)} tweet${inserted === 1 ? "" : "s"}`, "ok");
  } catch (err) {
    toast("simulate failed: " + err.message, "err");
  } finally {
    void loadXView();
  }
}

async function analyzeXSession(sessionId, btn) {
  if (btn) btn.disabled = true;
  const panel = $("#x-analysis");
  const root = $("#x-an-root");
  if (panel) panel.hidden = false;
  if (root) {
    clear(root);
    root.appendChild(el("p", { class: "muted", text: "analyzing…" }));
  }
  try {
    const analysis = await api("/x/sessions/" + encodeURIComponent(sessionId) + "/analysis");
    renderXAnalysis(root, analysis);
  } catch (err) {
    if (root) {
      clear(root);
      root.appendChild(el("p", { class: "muted", text: "analysis failed: " + err.message }));
    }
    toast("analysis failed: " + err.message, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** Render an X session analysis payload into *root*. DOM via el() only and
 *  reuses renderBarChart/renderChips, so it is node-executable in tests. */
function renderXAnalysis(root, analysis) {
  clear(root);
  const a = analysis || {};
  const sent = a.sentiment || {};
  const chips = el("div", { class: "x-an-sentiment" });
  chips.appendChild(el("span", { class: "x-an-chip x-an-pos", text: "positive " + fmt(sent.positive) }));
  chips.appendChild(el("span", { class: "x-an-chip x-an-neg", text: "negative " + fmt(sent.negative) }));
  chips.appendChild(el("span", { class: "x-an-chip x-an-neu", text: "neutral " + fmt(sent.neutral) }));
  chips.appendChild(el("span", { class: "x-an-chip x-an-avg", text: "avg score " + Number(sent.avg_score ?? 0).toFixed(2) }));
  root.appendChild(chips);

  root.appendChild(el("h3", { class: "x-an-sub", text: "Authors" }));
  const table = el("table", { class: "an-table x-an-table" });
  const thead = el("thead");
  const hrow = el("tr");
  hrow.appendChild(el("th", { text: "Username" }));
  hrow.appendChild(el("th", { text: "Tweets" }));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody", { class: "x-an-authors-body" });
  const authors = Array.isArray(a.authors) ? a.authors : [];
  if (!authors.length) {
    const row = el("tr");
    const cell = el("td", { colspan: "2", text: "No authors yet." });
    row.appendChild(cell);
    tbody.appendChild(row);
  } else {
    for (const au of authors) {
      const row = el("tr", { class: "x-an-author-row" });
      row.appendChild(el("td", { text: au.username || "—" }));
      row.appendChild(el("td", { text: fmt(au.count) }));
      tbody.appendChild(row);
    }
  }
  table.appendChild(tbody);
  root.appendChild(table);

  root.appendChild(el("h3", { class: "x-an-sub", text: "Top terms" }));
  const termsBox = el("div", { class: "an-chips x-an-terms" });
  renderChips(termsBox, Array.isArray(a.top_terms) ? a.top_terms : [], {
    key: (x) => x.term,
    label: (x) => `${x.term} ×${x.count}`,
  });
  root.appendChild(termsBox);

  root.appendChild(el("h3", { class: "x-an-sub", text: "Timeline" }));
  const chartBox = el("div", { class: "x-an-timeline", role: "img", "aria-label": "Tweets per day" });
  renderBarChart(chartBox, (Array.isArray(a.timeline) ? a.timeline : []).map((p) => ({ ts: p.date, count: p.count })));
  root.appendChild(chartBox);

  const eng = a.engagement || {};
  root.appendChild(el("p", {
    class: "x-an-engagement",
    text: `${a.tweet_count ?? 0} tweets · ${fmt(eng.total_likes)} likes · ${fmt(eng.total_retweets)} retweets · ${fmt(eng.avg_likes)} avg likes`,
  }));
}

async function showXSessionTweets(sessionId, btn) {
  if (btn) btn.disabled = true;
  const panel = $("#x-tweets");
  const list = $("#x-tweets-list");
  if (panel) panel.hidden = false;
  if (list) {
    clear(list);
    list.appendChild(el("li", { class: "muted-li", text: "loading…" }));
  }
  try {
    const tweets = await api("/x/sessions/" + encodeURIComponent(sessionId) + "/tweets");
    renderXTweetList(list, tweets || []);
  } catch (err) {
    if (list) {
      clear(list);
      list.appendChild(el("li", { class: "muted-li", text: "tweets failed: " + err.message }));
    }
    toast("tweets failed: " + err.message, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderXTweetList(list, tweets) {
  clear(list);
  if (!tweets.length) {
    list.appendChild(el("li", { class: "muted-li", text: "No tweets yet — run Simulate or a live session." }));
    return;
  }
  for (const t of tweets) {
    const li = el("li", { class: "x-tweet" });
    const head = el("div", { class: "x-tweet-head" });
    head.appendChild(el("strong", { class: "x-tweet-user", text: "@" + (t.username || "?") }));
    head.appendChild(el("span", { class: "x-tweet-when", text: t.created_at ? new Date(t.created_at).toLocaleString() : "—" }));
    li.appendChild(head);
    li.appendChild(el("p", { class: "x-tweet-text", text: truncateText(t.text, 160) }));
    list.appendChild(li);
  }
}

// ── Dashboard saved widgets ───────────────────────────────────
let dashSavedSig = null;
let dashSavedTick = 0;

/** Render saved searches as clickable chips on the dashboard band. */
function renderDashSaved(saved) {
  const box = $("#dash-saved-chips");
  if (!box) return;
  clear(box);
  const meta = $("#dash-saved-meta");
  if (meta) {
    meta.textContent = fmt(saved.length) + (saved.length === 1 ? " saved search" : " saved searches");
  }
  if (!saved.length) {
    box.appendChild(el("span", { class: "muted", text: "No saved searches yet — bookmark a query from Captures with ★ Save." }));
    return;
  }
  for (const s of saved) {
    const item = el("span", { class: "dash-saved-item" });
    const chip = el("button", {
      class: "chip dash-saved-chip",
      type: "button",
      title: s.query || "",
      text: `${s.name || "—"} · ${truncateText(s.query || "", 60)}`,
    });
    chip.addEventListener("click", () => void runDashSaved(s));
    item.appendChild(chip);
    const runBtn = el("button", {
      class: "btn btn-ghost dash-saved-run",
      type: "button",
      text: "Run",
      "aria-label": "Run: " + (s.name || ""),
    });
    runBtn.addEventListener("click", () => void runDashSaved(s));
    item.appendChild(runBtn);
    box.appendChild(item);
  }
}

async function runDashSaved(s) {
  const panel = $("#dash-saved-results");
  const title = $("#dash-saved-results-title");
  const list = $("#dash-saved-results-list");
  if (panel) panel.hidden = false;
  if (title) title.textContent = s.name || s.query || "Saved search";
  if (list) {
    clear(list);
    list.appendChild(el("li", { class: "muted-li", text: "running…" }));
  }
  try {
    const res = await api("/saved/" + encodeURIComponent(s.id) + "/run");
    const rows = (res && res.rows) || [];
    renderDashSavedResults(list, rows);
    if (panel && !rows.length) toast("no results for this saved search", "err");
  } catch (err) {
    if (list) {
      clear(list);
      list.appendChild(el("li", { class: "muted-li", text: "run failed: " + err.message }));
    }
    toast("run failed: " + err.message, "err");
  }
}

/** Compact inline results: title link, domain, date. */
function renderDashSavedResults(list, rows) {
  clear(list);
  if (!rows.length) {
    list.appendChild(el("li", { class: "muted-li", text: "No matches." }));
    return;
  }
  for (const r of rows) {
    const li = el("li", { class: "dash-saved-result" });
    const label = truncateText(r.title || "(untitled)", 120);
    li.appendChild(r.url
      ? el("a", { class: "dash-saved-result-title", href: r.url, target: "_blank", rel: "noopener noreferrer", text: label })
      : el("span", { class: "dash-saved-result-title", text: label }));
    li.appendChild(el("span", {
      class: "dash-saved-result-meta",
      text: `${r.domain || "—"} · ${r.fetch_ts ? ago(r.fetch_ts, true) + " ago" : "—"}`,
    }));
    list.appendChild(li);
  }
}

/** Refresh the dashboard saved widgets. Rebuilds only when the list
 *  changed, or every 12th tick (60 s) to keep staleness low. */
async function refreshDashSaved() {
  dashSavedTick += 1;
  let saved;
  try {
    saved = await api("/saved");
  } catch (_) {
    return; // non-fatal — dashboard KPIs keep rendering
  }
  const sig = JSON.stringify((saved || []).map((s) => `${s.id}:${s.name}:${s.updated_at || ""}:${s.query || ""}`));
  if (sig === dashSavedSig && dashSavedTick % 12 !== 0) return;
  dashSavedSig = sig;
  renderDashSaved(saved || []);
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
    { kind: "nav", icon: "△", label: "Go to Alerts",    do: () => navigate("alerts") },
    { kind: "nav", icon: "★", label: "Go to Saved searches", do: () => navigate("saved") },
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
  // Number shortcuts 1..9 for routes (when not typing)
  if (/^[1-9]$/.test(e.key) && !e.metaKey && !e.ctrlKey && !e.altKey) {
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

// Saved-search bookmarking from the captures search bar.
$("#saved-save-btn")?.addEventListener("click", () => {
  if (!$("#caps-search").value.trim()) { toast("type a query first", "err"); return; }
  const control = $("#saved-save-control");
  if (control) control.hidden = false;
  const name = $("#saved-save-name");
  if (name) name.focus();
});
$("#saved-save-cancel")?.addEventListener("click", () => {
  const control = $("#saved-save-control");
  if (control) control.hidden = true;
  const name = $("#saved-save-name");
  if (name) name.value = "";
});
$("#saved-save-ok")?.addEventListener("click", () => void saveCurrentSearch());
$("#saved-save-name")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); void saveCurrentSearch(); }
  if (e.key === "Escape") { $("#saved-save-cancel")?.click(); }
});

async function saveCurrentSearch() {
  const name = ($("#saved-save-name").value || "").trim();
  if (!name) { toast("name is required", "err"); return; }
  const query = $("#caps-search").value.trim();
  if (!query) { toast("type a query first", "err"); return; }
  const body = {
    name,
    query,
    mode: ($("#caps-mode")?.value || "auto").trim().toLowerCase(),
    fields: "title,text",
    limit: caps.limit,
  };
  const btn = $("#saved-save-ok");
  if (btn) btn.disabled = true;
  try {
    await api("/saved", { method: "POST", body: JSON.stringify(body) });
    toast(`saved "${name}"`, "ok");
    $("#saved-save-cancel")?.click();
  } catch (err) {
    toast("save failed: " + err.message, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

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


// ── theme toggle (observatory re-skin): honour system on first load, then
//    persist the user's explicit choice. Appended; touches nothing else. ──
(function initObservatoryTheme() {
  try {
    const root = document.documentElement;
    const saved = localStorage.getItem("aw-theme");
    if (saved === "light" || saved === "dark") {
      root.setAttribute("data-theme", saved);
    } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
      root.setAttribute("data-theme", "light");
    }
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", () => {
      const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      localStorage.setItem("aw-theme", next);
    });
  } catch (_e) { /* non-fatal */ }
})();
