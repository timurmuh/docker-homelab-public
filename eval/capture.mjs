#!/usr/bin/env node
/**
 * Capture the canonical text for one corpus URL, or for a whole bucket.
 *
 * The canon is what every provider is scored against, so it has to come from a
 * browser that a human would recognise: real Chrome, real logins, JavaScript
 * rendered, consent banners handled. Readability (`getPageMarkdown`) produces
 * the text; `document.body.innerText` is captured beside it purely as a quality
 * control, because a canon that is 9% of the page's text is navigation, not an
 * article.
 *
 * Two rules keep the browser usable:
 *
 * - one tab per URL, closed in a `finally`-equivalent, so a crash leaves at
 *   most one orphan and never a window full of them;
 * - a census before and after a batch, closing only pages whose URL is in the
 *   corpus (they are ours; the tab group is `eval`).
 *
 * Everything the gate decides is written down in `corpus/status.json`: `ok`,
 * `redirected`, `reclassified` (a content page with no article of its own),
 * or `dropped`.
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { gunzipSync, gzipSync } from "node:zlib";

// Playwriter browser id. Override with --browser or EVAL_BROWSER; the value
// depends on the local browser installation.
const DEFAULT_BROWSER = process.env.EVAL_BROWSER || "install:Chrome";
const TMP = ".capture-tmp";
const CANON = "eval/canon";
const CORPUS = "eval/corpus/urls.json";
const STATUS = "eval/corpus/status.json";
const GATE_RATIO = 0.5;
const GATE_MIN_WORDS = 200;
const CONSENT_MARKERS = [
  "cookie preferences",
  "we use cookies",
  "accept all cookies",
  "before you continue",
  "consent",
];
const CHALLENGE_MARKERS = [
  "just a moment",
  "checking your browser",
  "performing security verification",
  "prove your humanity",
];

function args() {
  const out = { jobs: 1, "per-host": 3, bucket: "all" };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i].startsWith("--")) {
      const key = argv[i].slice(2);
      const next = argv[i + 1];
      if (next && !next.startsWith("--")) {
        out[key] = next;
        i += 1;
      } else {
        out[key] = true;
      }
    }
  }
  return out;
}

const OPT = args();

function tokens(text) {
  return (String(text).toLowerCase().match(/[a-z0-9]+/g) || []).length;
}

function sha1(text) {
  return createHash("sha1").update(text).digest("hex");
}

function playwriterVersion() {
  try {
    return execFileSync("playwriter", ["--version"], { encoding: "utf8" }).trim().split("\n")[0];
  } catch {
    return "unknown";
  }
}

function run(script) {
  return execFileSync("playwriter", ["-s", SESSION, "-e", script], {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
}

let SESSION = OPT.session;

function createSession() {
  mkdirSync(TMP, { recursive: true });
  const out = execFileSync(
    "playwriter",
    ["session", "new", "--tab-group", "eval", "--browser", OPT.browser || DEFAULT_BROWSER],
    { encoding: "utf8" },
  );
  const match = out.match(/Session (\d+) created/);
  if (!match) throw new Error(`could not read a session id from: ${out}`);
  return match[1];
}

/** Pages the extension currently exposes. Only the `eval` group is visible. */
function census() {
  try {
    const out = run(
      'console.log(JSON.stringify(context.pages().map((p) => p.url()).filter(Boolean)))',
    );
    const line = out.split("\n").find((l) => l.trim().startsWith("["));
    return JSON.parse(line || "[]");
  } catch {
    return [];
  }
}

function closeOrphans(corpusUrls) {
  const open = census();
  const stray = open.filter((url) => corpusUrls.some((u) => url.startsWith(u.slice(0, 40))));
  if (stray.length) {
    run(`
      const wanted = ${JSON.stringify(stray)};
      for (const page of context.pages()) {
        if (wanted.some((u) => page.url().startsWith(u.slice(0, 40)))) await page.close();
      }
      console.log("closed", ${stray.length});
    `);
  }
  return { before: open.length, stray: stray.length };
}

function extractScript(url, { dismissConsent = false, selector = null } = {}) {
  // The `finally` is load-bearing: a selector that does not exist on the page
  // throws, and without it every failed attempt left a tab behind. Eleven
  // orphans accumulated during one selector sweep before this was added.
  return `
    const fs = require("node:fs");
    fs.mkdirSync(${JSON.stringify(TMP)}, { recursive: true });
    const page = await context.newPage();
    let captured = null;
    try {
      await page.goto(${JSON.stringify(url)}, { waitUntil: "domcontentloaded", timeout: 45000 });
      await waitForPageLoad({ page, timeout: 12000 }).catch(() => {});
      ${
        dismissConsent
          ? `
      for (const name of [/reject all/i, /decline/i, /no thanks/i, /reject/i]) {
        const button = page.getByRole("button", { name }).first();
        if (await button.count().catch(() => 0)) {
          await button.click({ timeout: 3000 }).catch(() => {});
          break;
        }
      }
      await page.waitForTimeout(800);
      `
          : ""
      }
      const title = await page.title();
      const finalUrl = page.url();
      let canon;
      let method;
      if (${JSON.stringify(selector)}) {
        canon = await page.locator(${JSON.stringify(selector)}).first().innerText({ timeout: 5000 });
        method = "element";
      } else {
        canon = await getPageMarkdown({ page, showDiffSinceLastCall: false });
        method = "readability";
      }
      const innertext = await page.evaluate(() => document.body.innerText);
      captured = { title, final_url: finalUrl, canon, innertext, method };
      fs.writeFileSync(${JSON.stringify(TMP)} + "/page.json", JSON.stringify(captured));
    } finally {
      await page.close().catch(() => {});
    }
    console.log("captured", captured ? captured.method : "none", captured ? captured.canon.length : 0);
  `;
}

function readPayload() {
  const path = `${TMP}/page.json`;
  const payload = JSON.parse(readFileSync(path, "utf8"));
  rmSync(path, { force: true });
  return payload;
}

function head(text, n = 400) {
  return String(text).slice(0, n).toLowerCase();
}

/**
 * Whether a redirect landed on the *same document* or somewhere else.
 *
 * A moved GitHub org, a new domain for the same blog, a locale prefix, a port
 * change -- all benign, and re-fetching them would waste a browser visit. A
 * bounce to `/login`, `/guest-cookies` or a site root is not benign: it is the
 * page refusing to be read, which is exactly the observation `reach` exists to
 * record.
 */
function isSamePage(finalUrl, originalUrl) {
  try {
    const a = new URL(originalUrl);
    const b = new URL(finalUrl);
    const registrable = (host) => host.replace(/^[^.]+(?=\.[^.]+\.[^.]+$)/, "");
    if (registrable(a.hostname) !== registrable(b.hostname)) return false;
    const last = (u) => u.pathname.split("/").filter(Boolean).pop() || "";
    const wanted = last(a);
    if (!wanted) return false;
    if (last(b) === wanted) return true;
    // Locale-prefix moves (`/EN/...`) keep the same final segment and are
    // caught above. Anything else is a different document: substring matching
    // here accepted `/post` -> `/post-2`, which is exactly the case the rule
    // exists to reject.
    const benign = ["/posts/", "/blog/", "/docs/", "/documentation/", "/articles/"];
    return benign.some((prefix) => b.pathname.startsWith(prefix) && last(b) === wanted);
  } catch {
    return false;
  }
}

function gate(canon, innertext) {
  const canonWords = tokens(canon);
  const pageWords = tokens(innertext);
  const ratio = pageWords ? canonWords / pageWords : 0;
  const headText = head(canon);
  const consent = CONSENT_MARKERS.some((m) => headText.includes(m));
  const challenge = CHALLENGE_MARKERS.some((m) => headText.includes(m));
  if (ratio < GATE_RATIO) return { ok: false, reason: "ratio_low", ratio, canonWords };
  if (consent) return { ok: false, reason: "consent_marker", ratio, canonWords };
  if (challenge) return { ok: false, reason: "challenge_marker", ratio, canonWords };
  if (canonWords < GATE_MIN_WORDS) return { ok: false, reason: "too_short", ratio, canonWords };
  return { ok: true, reason: null, ratio, canonWords };
}

function captureOne(entry) {
  const url = entry.url;
  const result = {
    url,
    status: "ok",
    reason: null,
    method: null,
    ratio: 0,
    canon_words: 0,
    innertext_words: 0,
    consent_dismissed: false,
    selector: null,
    final_url: url,
    title: "",
    captured_at: new Date().toISOString(),
    playwriter: playwriterVersion(),
    browser: OPT.browser || DEFAULT_BROWSER,
  };

  let payload;
  try {
    run(extractScript(url));
    payload = readPayload();
  } catch (error) {
    result.status = "dropped";
    result.reason = `capture_failed: ${String(error.message || error).slice(0, 160)}`;
    return result;
  }

  let verdict = gate(payload.canon, payload.innertext);
  if (!verdict.ok && verdict.reason === "ratio_low") {
    // Readability picked navigation. A human would have clicked the article;
    // the closest machine equivalent is to try the containers an article lives
    // in, and keep whichever yields the most text that still passes the gate.
    for (const selector of ["main", "article", "[role=main]", ".content", "#content"]) {
      try {
        run(extractScript(url, { selector }));
        const retry = readPayload();
        const retryVerdict = gate(retry.canon, retry.innertext);
        if (retryVerdict.ok || retryVerdict.canonWords > verdict.canonWords) {
          payload = retry;
          verdict = retryVerdict;
          result.selector = selector;
          if (retryVerdict.ok) break;
        }
      } catch {
        /* selector absent on this page; try the next */
      }
    }
  }
  if (!verdict.ok && verdict.reason === "consent_marker") {
    try {
      run(extractScript(url, { dismissConsent: true }));
      const retry = readPayload();
      const retryVerdict = gate(retry.canon, retry.innertext);
      if (retryVerdict.ok || retryVerdict.canonWords > verdict.canonWords) {
        payload = retry;
        verdict = retryVerdict;
        result.consent_dismissed = true;
      }
    } catch {
      /* keep the first attempt; the reason is already recorded */
    }
  }

  result.method = payload.method;
  result.ratio = Number(verdict.ratio.toFixed(3));
  result.canon_words = verdict.canonWords;
  result.innertext_words = tokens(payload.innertext);
  result.final_url = payload.final_url;
  result.title = payload.title;

  if (
    payload.final_url.replace(/\/$/, "") !== url.replace(/\/$/, "") &&
    !isSamePage(payload.final_url, url)
  ) {
    result.status = "redirected";
    result.reason = `landed on ${payload.final_url.slice(0, 160)}`;
  } else if (!verdict.ok) {
    result.status = entry.bucket === "reach" ? "ok" : "reclassified";
    result.reason = verdict.reason;
  }

  const digest = sha1(url);
  result.sha1 = digest;
  if (result.status !== "dropped") {
    mkdirSync(CANON, { recursive: true });
    writeFileSync(`${CANON}/${digest}.md.gz`, gzipSync(Buffer.from(payload.canon)));
    writeFileSync(`${CANON}/${digest}.json`, JSON.stringify(result, null, 1));
  }
  return result;
}

function main() {
  const corpusEntries = existsSync(CORPUS)
    ? JSON.parse(readFileSync(CORPUS, "utf8")).entries
    : [];
  const corpusUrls = corpusEntries.map((e) => e.url);

  SESSION = SESSION || createSession();
  console.log(`session ${SESSION}, playwriter ${playwriterVersion()}`);

  if (OPT.reclassify) {
    const state = JSON.parse(readFileSync(STATUS, "utf8"));
    let changed = 0;
    for (const record of Object.values(state.entries)) {
      const promoted = String(record.reason || "").startsWith("benign redirect");
      if (record.status !== "redirected" && !promoted) continue;
      if (!isSamePage(record.final_url, record.url)) continue;
      // The stored canon still has to pass the gate: promoting a redirect to
      // `ok` without re-reading it would admit a junk canon (the graphify issue
      // page is 380 canon words against 2,181 page tokens).
      const canonPath = `${CANON}/${record.sha1}.md.gz`;
      if (!existsSync(canonPath)) {
        record.status = "reclassified";
        record.reason = "redirected, and no stored canon to re-gate";
        changed += 1;
        continue;
      }
      const canon = gunzipSync(readFileSync(canonPath)).toString();
      const synthetic = Array.from({ length: Math.max(record.innertext_words || 0, 1) }, () => "w").join(" ");
      const verdict = gate(canon, synthetic);
      record.status = verdict.ok ? "ok" : "reclassified";
      record.reason = verdict.ok
        ? `benign redirect to ${record.final_url.slice(0, 120)}`
        : `${verdict.reason} after a benign redirect`;
      const recordPath = `${CANON}/${record.sha1}.json`;
      if (existsSync(recordPath)) writeFileSync(recordPath, JSON.stringify(record, null, 1));
      changed += 1;
    }
    state.generated = new Date().toISOString();
    writeFileSync(STATUS, JSON.stringify(state, null, 1));
    const counts = Object.values(state.entries).reduce((acc, r) => {
      acc[r.status] = (acc[r.status] || 0) + 1;
      return acc;
    }, {});
    console.log(`reclassified ${changed} redirects;`, counts);
    return;
  }

  if (OPT.url) {
    const entry = corpusEntries.find((e) => e.url === OPT.url) || {
      url: OPT.url,
      bucket: OPT["as-bucket"] || "content",
    };
    const result = captureOne(entry);
    const state = existsSync(STATUS)
      ? JSON.parse(readFileSync(STATUS, "utf8"))
      : { entries: {} };
    state.entries[entry.url] = result;
    writeFileSync(STATUS, JSON.stringify({ generated: new Date().toISOString(), entries: state.entries }, null, 1));
    console.log(JSON.stringify(result, null, 1));
    console.log(`census: ${census().length} pages`);
    return;
  }

  let entries = corpusEntries;
  if (OPT.bucket !== "all") entries = entries.filter((e) => e.bucket === OPT.bucket);
  if (OPT.limit) entries = entries.slice(0, Number(OPT.limit));

  const before = closeOrphans(corpusUrls);
  console.log(`census before: ${before.before} open, ${before.stray} closed`);

  const results = {};
  const perHost = {};
  for (const entry of entries) {
    perHost[entry.host] = (perHost[entry.host] || 0) + 1;
    if (perHost[entry.host] > Number(OPT["per-host"])) continue;
    const result = captureOne(entry);
    results[entry.url] = result;
    console.log(
      `${result.status.padEnd(13)} ${String(result.canon_words).padStart(5)}w ratio=${result.ratio} ${entry.url.slice(0, 90)}${result.reason ? `  (${result.reason})` : ""}`,
    );
  }

  const previous = existsSync(STATUS) ? JSON.parse(readFileSync(STATUS, "utf8")) : { entries: {} };
  const merged = { ...previous.entries, ...results };
  mkdirSync("eval/corpus", { recursive: true });
  writeFileSync(STATUS, JSON.stringify({ generated: new Date().toISOString(), entries: merged }, null, 1));

  const counts = Object.values(results).reduce((acc, r) => {
    acc[r.status] = (acc[r.status] || 0) + 1;
    return acc;
  }, {});
  console.log(`wrote ${Object.keys(results).length} records:`, counts);
  const after = census();
  console.log(`census after: ${after.length} pages`);
  try {
    execFileSync("playwriter", ["session", "reset", SESSION], { encoding: "utf8" });
    console.log(`session ${SESSION} reset`);
  } catch {
    console.log(`session ${SESSION} reset failed (harmless)`);
  }
  if (after.length) console.log("remaining tabs:", after.slice(0, 5).join(", "));
}

main();
