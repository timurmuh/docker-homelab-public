import http from 'node:http';

const SOURCES = [
  {
    name: 'proxyscrape',
    url: 'https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/all/data.json',
    format: 'proxyscrape-json',
  },
  {
    name: 'geonode',
    url: 'https://proxylist.geonode.com/api/proxy-list?limit=500&sort_by=lastChecked&sort_type=desc&protocols=http,https',
    format: 'geonode-json',
  },
  {
    name: 'iplocate-http',
    url: 'https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'iplocate-https',
    url: 'https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/https.txt',
    format: 'text-ip-port',
    defaultProtocol: 'https',
  },
  {
    name: 'thordata-http',
    url: 'https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'thespeedx',
    url: 'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'lonekingcode',
    url: 'https://raw.githubusercontent.com/LoneKingCode/free-proxy-db/main/proxies/all.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'komutan234',
    url: 'https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'gfpcom',
    url: 'https://raw.githubusercontent.com/gfpcom/free-proxy-list/main/sources/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'kangproxy',
    url: 'https://raw.githubusercontent.com/officialputuid/KangProxy/main/http/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'roosterkid',
    url: 'https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS.txt',
    format: 'text-ip-port',
    defaultProtocol: 'https',
  },
  {
    name: 'tuanminpay',
    url: 'https://raw.githubusercontent.com/tuanminpay/live-proxy/master/all.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'databay-labs',
    url: 'https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'volkansah',
    url: 'https://raw.githubusercontent.com/VolkanSah/Auto-Proxy-Fetcher/refs/heads/main/proxies.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'murtaja89',
    url: 'https://raw.githubusercontent.com/murtaja89/public-proxies/main/proxies_all.txt',
    format: 'text-ip-port',
    defaultProtocol: 'http',
  },
  {
    name: 'proxyscrape-v4-http',
    url: 'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=http',
    format: 'text-protocol-ip-port',
  },
  {
    name: 'proxyscrape-v4-https',
    url: 'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=https',
    format: 'text-protocol-ip-port',
  },
];

const VALID_PROTOCOLS = new Set(['http', 'https', 'socks4', 'socks5']);

function parseProxyscrapeJson(body) {
  const list = JSON.parse(body);
  return list
    .filter((o) => o.ip && o.port && o.protocol)
    .map((o) => ({ address: `${o.ip}:${o.port}`, protocol: o.protocol.toLowerCase() }));
}

function parseGeonodeJson(body) {
  const parsed = JSON.parse(body);
  if (!parsed.data) return [];
  const entries = [];
  for (const o of parsed.data) {
    if (!o.ip || !o.port || !o.protocols) continue;
    for (const protocol of o.protocols) {
      entries.push({ address: `${o.ip}:${o.port}`, protocol: protocol.toLowerCase() });
    }
  }
  return entries;
}

function parseTextIpPort(body, defaultProtocol) {
  const re = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}$/;
  return body
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => re.test(line))
    .map((address) => ({ address, protocol: defaultProtocol }));
}

function parseTextProtocolIpPort(body) {
  const re = /^(https?|socks[45]):\/\/(.+)$/;
  const entries = [];
  for (const line of body.split('\n')) {
    const trimmed = line.trim();
    const match = trimmed.match(re);
    if (match) {
      entries.push({ address: match[2], protocol: match[1].toLowerCase() });
    }
  }
  return entries;
}

async function fetchSource(source) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30_000);
  try {
    const resp = await fetch(source.url, { signal: controller.signal });
    if (!resp.ok) {
      console.warn(`[aggregator] ${source.name}: HTTP ${resp.status}`);
      return [];
    }
    const body = await resp.text();
    switch (source.format) {
      case 'proxyscrape-json':
        return parseProxyscrapeJson(body);
      case 'geonode-json':
        return parseGeonodeJson(body);
      case 'text-ip-port':
        return parseTextIpPort(body, source.defaultProtocol || 'http');
      case 'text-protocol-ip-port':
        return parseTextProtocolIpPort(body);
    }
  } catch (err) {
    console.warn(`[aggregator] ${source.name}: ${err.message}`);
    return [];
  } finally {
    clearTimeout(timer);
  }
}

let cached = null;
let inFlight = null;
const CACHE_TTL_MS = 5 * 60 * 1000;

async function fetchAllProxies() {
  const merged = new Map();
  const results = await Promise.allSettled(SOURCES.map(fetchSource));

  for (const result of results) {
    if (result.status === 'fulfilled') {
      for (const entry of result.value) {
        merged.set(entry.address, entry);
      }
    }
  }

  return merged;
}

async function getAllProxies() {
  if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
    return cached.data;
  }

  if (!inFlight) {
    inFlight = fetchAllProxies().then(
      (data) => {
        cached = { data, timestamp: Date.now() };
        return data;
      },
      (err) => {
        throw err;
      }
    );
  }

  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}

const PORT = parseInt(process.env.PORT, 10) || 3002;

const server = http.createServer(async (req, res) => {
  if (req.method !== 'GET') {
    res.writeHead(405, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Method not allowed' }));
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host}`);

  if (url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('ok');
    return;
  }

  const protocol = url.searchParams.get('protocol');

  if (!protocol || !VALID_PROTOCOLS.has(protocol)) {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Query parameter "protocol" must be one of: http, https, socks4, socks5' }));
    return;
  }

  try {
    const all = await getAllProxies();
    const filtered = Array.from(all.values())
      .filter((e) => e.protocol === protocol)
      .map((e) => e.address);

    if (filtered.length === 0) {
      res.writeHead(503, { 'Content-Type': 'text/plain' });
      res.end('No proxies available');
      return;
    }

    res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end(filtered.join('\n') + '\n');
  } catch (err) {
    console.error(`[aggregator] fetch error: ${err.message}`);
    res.writeHead(503, { 'Content-Type': 'text/plain' });
    res.end('Failed to fetch proxies');
  }
});

// --- Rota declarative management layer -------------------------------------
//
// rota has NO declarative config: pools, proxy-users, sources, settings live
// only in TimescaleDB and are mutated only via the API. This block is the
// reproducible upsert-by-name layer: every 5 min it reconciles rota state to
// the config arrays below. All operations are idempotent (find-or-create, then
// reconcile only the fields that differ) so a wiped DB self-heals on the next
// cycle without creating duplicates.

const ROTA_API = 'http://rota-core:8001';

// Pool config. Membership of the curated pools is owned by THIS aggregator
// (syncMembership: true). proxies_all membership is owned by rota auto_sync
// (142 explicit country geo-filters) — the aggregator must NEVER touch its
// membership or its filters (sending any non-nil filter array to PUT /pools
// replaces the filters AND triggers an immediate membership re-sync).
//
// Health checks: NO per-pool HC on the curated tiers. rota's cron HC is the
// ONLY writer of proxies.status for pool members (verified in rota v2.1.0: the
// pool-chain request path never touches the DB on failure — it evicts
// in-memory after 3 consecutive fails and re-imports from the DB every 30s),
// and a single flaky check marks a proxy failed GLOBALLY. Enabling HC on
// sub45s (which covers sub20s members) caused the SearXNG starvation
// regression — a working proxy that failed one ipify check got pruned from the
// fast pool. proxies_all is therefore the sole HC engine: it refreshes status
// + avg_response_time across the whole bulk pool every 6h.
//
// WARNING: an EMPTY health_check_cron does NOT mean "never" — rota falls back
// to a 30-min default when enabled. Disable HC with health_check_enabled:false,
// never with an empty cron string.
//
// WARNING: rota's cronInterval() ONLY parses `*/N` minute patterns. A real
// cron like `0 */6 * * *` silently falls back to 30 minutes. To get a true 6h
// cadence use `*/360 * * * *` (no upper clamp exists).
const POOLS = [
  {
    name: 'proxies_stable_sub20s',
    description: 'Fastest curated proxies (<20s avg_response_time) for interactive consumers (SearXNG)',
    maxResponseTime: 20_000,
    healthCheckEnabled: false,
    healthCheckCron: '*/30 * * * *', // irrelevant while disabled; kept for determinism
    rotationMethod: 'random',
    autoSync: false,
    syncMode: 'auto',
    syncMembership: true,
    proxyUser: { username: 'searxng', passwordEnv: 'SEARXNG_PROXY_PASSWORD' },
  },
  {
    name: 'proxies_stable_sub45s',
    description: 'Curated proxies (<45s avg_response_time) for background scrape (Firecrawl)',
    maxResponseTime: 45_000,
    healthCheckEnabled: false, // no per-pool HC — proxies_all is the sole HC engine
    healthCheckCron: '*/30 * * * *', // irrelevant while disabled; kept for determinism
    rotationMethod: 'random',
    autoSync: false,
    syncMode: 'auto',
    syncMembership: true,
    proxyUser: { username: 'firecrawl', passwordEnv: 'FIRECRAWL_PROXY_PASSWORD' },
  },
  {
    name: 'proxies_stable_any',
    description: 'All active, tested proxies regardless of response time — non-elite usable proxies',
    maxResponseTime: null, // no rt filter; any active proxy with rt > 0 qualifies
    healthCheckEnabled: false, // no per-pool HC — proxies_all is the sole HC engine
    healthCheckCron: '*/30 * * * *', // irrelevant while disabled; kept for determinism
    rotationMethod: 'random',
    autoSync: false,
    syncMode: 'auto',
    syncMembership: true,
    proxyUser: { username: 'stable', passwordEnv: 'STABLE_PROXY_PASSWORD' },
  },
  {
    name: 'proxies_all',
    description: 'Bulk pool. Membership owned by rota auto_sync via country geo-filters',
    maxResponseTime: null,
    healthCheckEnabled: true,
    healthCheckCron: '*/360 * * * *', // true 6h — rota cronInterval only parses */N (see comment above)
    rotationMethod: 'random',
    autoSync: true,
    syncMode: 'auto',
    syncMembership: false, // never managed by the aggregator
    proxyUser: { username: 'all', passwordEnv: 'ALL_PROXY_PASSWORD' },
  },
];

// rota sources that feed THIS aggregator's HTTP endpoint. All four share the
// same name (uniqueness key is the URL). Sources are already cleanup-enabled;
// this upsert makes them reproducible after a DB reset.
const ROTA_SOURCES = [
  { name: 'Aggregated list', url: 'http://rota-aggregator:3002/?protocol=http', protocol: 'http', intervalMinutes: 60, cleanupEnabled: true, cleanupDays: 1 },
  { name: 'Aggregated list', url: 'http://rota-aggregator:3002/?protocol=https', protocol: 'https', intervalMinutes: 60, cleanupEnabled: true, cleanupDays: 1 },
  { name: 'Aggregated list', url: 'http://rota-aggregator:3002/?protocol=socks4', protocol: 'socks4', intervalMinutes: 60, cleanupEnabled: true, cleanupDays: 1 },
  { name: 'Aggregated list', url: 'http://rota-aggregator:3002/?protocol=socks5', protocol: 'socks5', intervalMinutes: 60, cleanupEnabled: true, cleanupDays: 1 },
];

// Global dead-proxy cleanup. Deletes failed proxies globally after N days.
// NOTE: rota has NO min-value validation on these (interval <=0 falls back to
// 24h; max_failed_days <=0 disables the clause). Per-source cleanup_days
// (1-365, 0 -> 7) is separately set on each source above.
const PROXY_CLEANUP = { enabled: true, max_failed_days: 3, cleanup_interval_hours: 6 };

let syncAdminToken = null;
let syncFailedStreak = 0;

async function syncLogin() {
  const resp = await fetch(`${ROTA_API}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: process.env.ROTA_ADMIN_USER || 'admin',
      password: process.env.ROTA_ADMIN_PASSWORD || '',
    }),
  });
  if (!resp.ok) throw new Error(`rota login: HTTP ${resp.status}`);
  const data = await resp.json();
  return data.token;
}

const POOL_HEADERS = (token) => ({
  'Authorization': `Bearer ${token}`,
  'Content-Type': 'application/json',
});

// The rota JWT secret is random per process start, so every rota-core restart
// invalidates our cached token. On 401 we re-login and retry the request once.
async function rotaRequest(method, path, body) {
  let token = syncAdminToken;
  if (!token) {
    token = syncAdminToken = await syncLogin();
  }
  const doRequest = async (t) => {
    const resp = await fetch(`${ROTA_API}${path}`, {
      method,
      headers: POOL_HEADERS(t),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (resp.status === 401) return { retry: true };
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`${method} ${path}: HTTP ${resp.status} ${text}`);
    }
    return { retry: false, json: await resp.json() };
  };

  let result = await doRequest(token);
  if (result.retry) {
    syncAdminToken = await syncLogin();
    result = await doRequest(syncAdminToken);
  }
  if (result.retry) throw new Error(`${method} ${path}: auth failed after re-login`);
  return result.json;
}

// find-or-create a pool by name, then reconcile only fields that differ.
// CRITICAL: never send geo_filters/isp_filters/tag_filters/country_code/etc —
// a non-nil filter array REPLACES filters and immediately re-syncs membership
// (wipes proxies_all's 142 country filters). The Update SQL also sets
// country_code unconditionally from the request (NULL if omitted), so it must
// never be included here.
async function ensurePool(cfg) {
  const { pools } = await rotaRequest('GET', '/api/v1/pools');
  const existing = (pools || []).find((p) => p.name === cfg.name);
  if (existing) {
    const patch = {};
    if (existing.rotation_method !== cfg.rotationMethod) patch.rotation_method = cfg.rotationMethod;
    if (existing.health_check_enabled !== cfg.healthCheckEnabled) patch.health_check_enabled = cfg.healthCheckEnabled;
    if (existing.health_check_cron !== cfg.healthCheckCron) patch.health_check_cron = cfg.healthCheckCron;
    if (existing.auto_sync !== cfg.autoSync) patch.auto_sync = cfg.autoSync;
    if (existing.sync_mode !== cfg.syncMode) patch.sync_mode = cfg.syncMode;
    if (!existing.enabled) patch.enabled = true;
    if (Object.keys(patch).length > 0) {
      await rotaRequest('PUT', `/api/v1/pools/${existing.id}`, patch);
      console.log(`[aggregator] pool "${cfg.name}" reconciled: ${Object.keys(patch).join(', ')}`);
    }
    return existing.id;
  }

  const created = await rotaRequest('POST', '/api/v1/pools', {
    name: cfg.name,
    description: cfg.description,
    rotation_method: cfg.rotationMethod,
    health_check_cron: cfg.healthCheckCron,
    health_check_enabled: cfg.healthCheckEnabled,
    auto_sync: cfg.autoSync,
    sync_mode: cfg.syncMode,
    enabled: true,
  });
  console.log(`[aggregator] created pool "${cfg.name}" id=${created.id}`);
  return created.id;
}

async function ensureProxyUser(cfg, poolId) {
  const users = await rotaRequest('GET', '/api/v1/proxy-users');
  const existing = (users.users || []).find((u) => u.username === cfg.proxyUser.username);
  if (existing) return;

  const password = process.env[cfg.proxyUser.passwordEnv] || '';
  if (!password) throw new Error(`password env ${cfg.proxyUser.passwordEnv} not set`);

  await rotaRequest('POST', '/api/v1/proxy-users', {
    username: cfg.proxyUser.username,
    password,
    enabled: true,
    main_pool_id: poolId,
    max_retries: 3,
  });
  console.log(`[aggregator] created proxy user "${cfg.proxyUser.username}" -> pool ${cfg.name}`);
}

// Paginate active proxies sorted by response time ascending. The rota API
// silently caps limit at 100 — must paginate to see all results.
async function listActiveProxies() {
  const qualifying = [];
  let page = 1;
  let totalPages = 1;
  while (page <= totalPages) {
    const data = await rotaRequest(
      'GET',
      `/api/v1/proxies?sort=avg_response_time&order=asc&status=active&limit=100&page=${page}`
    );
    const proxies = data.proxies || data;
    totalPages = (data.pagination && data.pagination.total_pages) || 1;
    for (const p of proxies) {
      qualifying.push(p);
    }
    page++;
  }
  return qualifying;
}

async function getPoolMemberIds(poolId) {
  const data = await rotaRequest('GET', `/api/v1/pools/${poolId}/proxies`);
  const list = data.proxies || data || [];
  return new Set(list.map((p) => p.proxy_id));
}

// Check whether a proxy still exists in the rota DB. Used to distinguish
// "removed because it degraded" from "hard-deleted by cleanup". The List API's
// `search` is a substring ILIKE match (%addr%) capped at 100 results/page, so
// `1.2.3.4:80` also matches `1.2.3.4:8080` — paginate and exact-match to avoid
// false positives/negatives.
async function proxyExists(address) {
  let page = 1;
  let totalPages = 1;
  while (page <= totalPages) {
    const data = await rotaRequest(
      'GET',
      `/api/v1/proxies?search=${encodeURIComponent(address)}&limit=100&page=${page}`
    );
    const proxies = data.proxies || [];
    if (proxies.some((p) => p.address === address)) return true;
    totalPages = (data.pagination && data.pagination.total_pages) || 1;
    page++;
  }
  return false;
}

// F4 churn measurement: per-source cleanup (cleanup_days) hard-DELETEs proxy
// rows globally by last_seen_at staleness, regardless of status or pool
// membership — a good (active) proxy that merely dropped off its source's list
// can be deleted. The pool's GetProxies is an INNER JOIN on proxies, so a
// deleted proxy never shows up as a pool member to prune — the only way to see
// it is to track previously-active proxies across cycles and detect ones that
// vanished from the DB. Cap per-cycle existence checks to bound API load.
const MAX_EXISTENCE_CHECKS = 50;

let prevActiveById = null; // Map<proxyId, address> from the last successful cycle

async function measureCleanupChurn(active) {
  const activeById = new Map(active.map((p) => [p.id, p.address]));
  let deletedCount = 0;
  let checked = 0;
  if (prevActiveById) {
    for (const [id, address] of prevActiveById) {
      if (activeById.has(id)) continue; // still active — not a churn candidate
      if (checked >= MAX_EXISTENCE_CHECKS) break;
      checked++;
      try {
        if (!(await proxyExists(address))) {
          deletedCount++;
          console.warn(`[aggregator] F4: proxy ${id} ${address} was active, now GONE from DB (cleanup hard-delete?)`);
        }
      } catch (err) {
        // ignore existence-check errors; purely diagnostic
      }
    }
  }
  if (deletedCount > 0) {
    console.warn(`[aggregator] F4: ${deletedCount}/${checked} previously-active proxies vanished from DB this cycle`);
  }
  prevActiveById = activeById;
}

// Prune pass: for each curated pool, remove members that no longer qualify.
// Process sub20s BEFORE sub45s so a nested proxy is dropped from the smallest
// pool first.
async function prunePoolMembership(cfg, poolId, active) {
  const qualifying = active.filter((p) => {
    if (cfg.maxResponseTime !== null) {
      return p.avg_response_time > 0 && p.avg_response_time < cfg.maxResponseTime;
    }
    return p.avg_response_time > 0; // "tested" (rt measured)
  });
  const qualifyingIds = new Set(qualifying.map((p) => p.id));

  const currentIds = await getPoolMemberIds(poolId);
  const toRemove = [...currentIds].filter((id) => !qualifyingIds.has(id));

  if (toRemove.length === 0) return;

  const result = await rotaRequest('DELETE', `/api/v1/pools/${poolId}/proxies`, {
    proxy_ids: toRemove,
  });
  const n = result.removed || toRemove.length;
  console.log(`[aggregator] pool ${cfg.name}: removed ${n} stale proxies`);
}

// Add pass: for each curated pool, add qualifying members that are missing.
// Process sub45s BEFORE sub20s so a proxy entering sub20s is already covered
// by sub45s' HC (sub20s HC is off).
async function addPoolMembership(cfg, poolId, active) {
  const qualifying = active.filter((p) => {
    if (cfg.maxResponseTime !== null) {
      return p.avg_response_time > 0 && p.avg_response_time < cfg.maxResponseTime;
    }
    return p.avg_response_time > 0; // "tested" (rt measured)
  });
  const qualifyingIds = new Set(qualifying.map((p) => p.id));

  const currentIds = await getPoolMemberIds(poolId);
  const toAdd = qualifying.filter((p) => !currentIds.has(p.id));

  if (toAdd.length === 0) return;

  const result = await rotaRequest('POST', `/api/v1/pools/${poolId}/proxies`, {
    proxy_ids: toAdd.map((p) => p.id),
  });
  const n = result.added || toAdd.length;
  console.log(`[aggregator] pool ${cfg.name}: added ${n} proxies (qualifying: ${qualifying.length})`);
}

async function ensureSources() {
  const data = await rotaRequest('GET', '/api/v1/sources');
  const sources = data.sources || [];
  for (const want of ROTA_SOURCES) {
    const existing = sources.find((s) => s.url === want.url);
    if (existing) {
      const patch = {};
      if (existing.enabled !== true) patch.enabled = true;
      if (existing.interval_minutes !== want.intervalMinutes) patch.interval_minutes = want.intervalMinutes;
      if (existing.cleanup_enabled !== want.cleanupEnabled) patch.cleanup_enabled = want.cleanupEnabled;
      if (existing.cleanup_days !== want.cleanupDays) patch.cleanup_days = want.cleanupDays;
      if (Object.keys(patch).length > 0) {
        await rotaRequest('PUT', `/api/v1/sources/${existing.id}`, patch);
        console.log(`[aggregator] source "${want.url}" reconciled: ${Object.keys(patch).join(', ')}`);
      }
    } else {
      await rotaRequest('POST', '/api/v1/sources', {
        name: want.name,
        url: want.url,
        protocol: want.protocol,
        enabled: true,
        interval_minutes: want.intervalMinutes,
        cleanup_enabled: want.cleanupEnabled,
        cleanup_days: want.cleanupDays,
      });
      console.log(`[aggregator] created source "${want.url}"`);
    }
  }
}

async function reconcileCleanupSettings() {
  // PUT /settings replaces the WHOLE object, and validateSettings rejects
  // zeroed rotation/healthcheck timeouts — must GET, merge, PUT.
  const current = await rotaRequest('GET', '/api/v1/settings');
  const pc = current.proxy_cleanup || {};
  const wanted = { ...pc, ...PROXY_CLEANUP };
  const changed =
    pc.enabled !== wanted.enabled ||
    pc.max_failed_days !== wanted.max_failed_days ||
    pc.cleanup_interval_hours !== wanted.cleanup_interval_hours;
  if (!changed) return;

  await rotaRequest('PUT', '/api/v1/settings', { ...current, proxy_cleanup: wanted });
  console.log(`[aggregator] proxy_cleanup settings enabled=${wanted.enabled} max_failed_days=${wanted.max_failed_days} cleanup_interval_hours=${wanted.cleanup_interval_hours}`);
}

async function runManagementSync() {
  try {
    // 1. Pools (find-or-create + reconcile).
    const poolIds = {};
    for (const cfg of POOLS) {
      poolIds[cfg.name] = await ensurePool(cfg);
    }

    // 2. Proxy users.
    for (const cfg of POOLS) {
      await ensureProxyUser(cfg, poolIds[cfg.name]);
    }

    // 3. Membership sync for curated pools. Two passes preserve the
    //    sub20s ⊆ sub45s ⊆ any nesting: prune from sub20s first (drop from the
    //    smallest pool before the superset), add to sub45s/any first (so a
    //    proxy entering sub20s is already in its supersets). Order only matters
    //    for clean nesting of the curated tiers — no HC-coverage dependency,
    //    since curated pools have per-pool HC disabled.
    const curated = POOLS.filter((c) => c.syncMembership);
    const active = await listActiveProxies();
    const poolByName = (name) => curated.find((c) => c.name === name);

    // Prune pass (smallest/nested pool first).
    for (const name of ['proxies_stable_sub20s', 'proxies_stable_sub45s', 'proxies_stable_any']) {
      const cfg = poolByName(name);
      if (cfg) await prunePoolMembership(cfg, poolIds[cfg.name], active);
    }

    // Add pass (largest/superset pool first).
    for (const name of ['proxies_stable_sub45s', 'proxies_stable_sub20s', 'proxies_stable_any']) {
      const cfg = poolByName(name);
      if (cfg) await addPoolMembership(cfg, poolIds[cfg.name], active);
    }

    // 4. F4 churn measurement (previously-active proxies that vanished from DB).
    await measureCleanupChurn(active);

    // 5. Sources upsert (app-level upsert-by-name is impossible — POST always
    //    inserts; match by URL and PUT).
    await ensureSources();

    // 6. Global proxy cleanup settings.
    await reconcileCleanupSettings();

    syncFailedStreak = 0;
    console.log(`[aggregator] rota sync: pools ok (${active.length} active proxies)`);
  } catch (err) {
    syncFailedStreak += 1;
    if (syncFailedStreak > 1) {
      console.warn(`[aggregator] rota sync failed (streak ${syncFailedStreak}): ${err.message}`);
    }
    syncAdminToken = null;
  }
}

// --- Start periodic sync ---

let syncTimer = null;
const SYNC_INTERVAL_MS = 5 * 60 * 1000;

function startRotaSync() {
  if (!process.env.SEARXNG_PROXY_PASSWORD) {
    console.warn('[aggregator] SEARXNG_PROXY_PASSWORD not set — rota sync is DISABLED');
    return;
  }

  console.log(`[aggregator] starting rota declarative sync every ${SYNC_INTERVAL_MS / 1000}s`);

  runManagementSync().catch(() => {});
  syncTimer = setInterval(() => {
    runManagementSync().catch(() => {});
  }, SYNC_INTERVAL_MS);
}

server.listen(PORT, () => {
  console.log(`Proxy aggregator running on :${PORT}`);
  startRotaSync();
});
