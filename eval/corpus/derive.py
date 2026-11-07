"""Derive the eval corpus from Open WebUI chat citations.

The corpus is *not* synthesised: it comes from the pages this homelab's models
actually cited, mined read-only from ``webui.db`` on the host. Everything here
is pure except :func:`mine`, which is the only function that talks to the
database, and it never writes to it.

Two rules shape the output:

* **One entry per canonical URL.** Tracking parameters and a trailing slash are
  not two pages, and the corpus is committed, so a token in a query string would
  be published.
* **Buckets follow what can be measured.** A social or video page has no
  article to contain, so it is graded on reach instead of containment; the
  article pool tops up the reach target when the history is short of social
  pages, because "did the fetcher get through" is a fair question anywhere.
"""

from __future__ import annotations

import collections
import ipaddress
import json
import os
import re
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset({"fbclid", "gclid"})
"""Query parameters that identify a click, never a document."""

BINARY_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".flac",
    ".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".pdf", ".epub", ".mobi",
)
"""Payloads with no HTML text to contain. PDFs are here on purpose: Chrome's
viewer exposes viewer chrome rather than the document, so there is no canon to
score against, and the design excludes them as a separate problem class."""

SECRET_QUERY = re.compile(
    r"(?:^|&)(?:token|sig|signature|session|sessionid|key|auth|access_token|apikey)=",
    re.IGNORECASE,
)
"""A query string that authenticates. Committed corpora must not carry these."""

LONG_SECRET = re.compile(r"[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-+/=]{40,}")
"""A bare long hex/base64 run, which is a credential more often than not."""

SOCIAL_HOSTS = (
    "reddit.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
    "linkedin.com", "quora.com", "tiktok.com", "threads.com", "threads.net",
    "mastodon.social",
)
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "twitch.tv")
DOCS_PREFIXES = ("docs.", "doc.", "readthedocs.", "developer.mozilla.org")
NON_PAGE_HOSTS = (
    "api.", "cdn.", "static.", "fonts.", "raw.githubusercontent.com",
    "chromewebstore.", "chrome.google.com", "play.google.", "apps.apple.com",
    "rocketreach.co", "chrome-stats.com", "extensionranker.com", "softonic.",
    "apkpure.", "apkmirror.", "addons.", "microsoftedge.microsoft.com",
)
"""Hosts whose URLs are data, assets or store listings rather than pages with
readable text. They inflate the citation counts and would waste corpus slots."""

PAPER_HOSTS = (
    "arxiv.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nature.com",
    "science.org", "sciencedirect.com", "acm.org", "ieee.org", "springer.com",
)
"""Hosts whose pages are usually behind a challenge or a paywall. Reach, not
containment, is the interesting question for them."""


def canonicalise(url: str) -> str | None:
    """Return the one spelling of ``url`` the corpus is allowed to carry.

    Lowercases the scheme and host, drops tracking parameters, and strips a
    trailing slash from a path (never from a bare origin, which would leave an
    empty path). Anything that is not http(s) is not a page: ``None``.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), urlencode(query), ""))


def _is_private(host: str) -> bool:
    """Whether ``host`` is an address that only exists inside a network."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def accept(url: str) -> bool:
    """Whether ``url`` may enter the corpus.

    Rejects everything the design calls out: non-http schemes, private and
    single-label hosts, binary payloads, and query strings that look like a
    credential. The corpus is committed, so this is a publication filter, not a
    quality filter.
    """
    canon = canonicalise(url)
    if canon is None:
        return False
    parts = urlsplit(canon)
    host = parts.hostname or ""
    if not host or host == "localhost" or "." not in host:
        return False
    if _is_private(host):
        return False
    if parts.path.lower().endswith(BINARY_SUFFIXES):
        return False
    if any(host.startswith(tag) or host == tag for tag in NON_PAGE_HOSTS):
        return False
    if "/node_modules/" in parts.path:
        # A dependency's README on a stranger's server: real URL, no page.
        return False
    if parts.path in ("", "/"):
        # A bare origin is a home page: no article, nothing to contain.
        return False
    if parts.path.lower().startswith(("/search", "/pdf/")) or "/search?" in url:
        # Search results are dynamic and a PDF has no canon (see the design).
        return False
    if SECRET_QUERY.search(parts.query) or LONG_SECRET.search(parts.query):
        return False
    return True


def _in_domain(host: str, domains: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of ``domains`` or a subdomain of one.

    Substring matching is wrong here and was a real bug during development:
    ``"x.com" in "dotenvx.com"`` is true, so a third of the corpus was
    classified as social and pushed into the reach bucket. The dot is part of
    the test.
    """
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def kind_of(url: str) -> str:
    """Classify a page by the shape of its URL, not by its content."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    if _in_domain(host, SOCIAL_HOSTS):
        return "social"
    if _in_domain(host, VIDEO_HOSTS):
        return "video"
    if (
        ("github.com" in host or "gitlab.com" in host)
        and ("/issues/" in path or "/pull/" in path or "/merge_requests/" in path)
    ):
        return "issue"
    if _in_domain(host, PAPER_HOSTS):
        return "paper"
    if host.startswith(DOCS_PREFIXES) or "readthedocs." in host or "/docs/" in path:
        return "docs"
    return "article"


def _as_url(key: str) -> str:
    """Accept either a full URL or the ``host/path`` shorthand the tests use."""
    return key if key.startswith(("http://", "https://")) else f"https://{key}"


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def pick(
    candidates: dict[str, int], *, content: int, reach: int, per_host: int
) -> list[dict]:
    """Choose the corpus: frequency-first, round-robin across hosts, capped.

    Round-robin matters because the raw counts are dominated by a handful of
    sites (GitHub alone supplies thousands of citations); taking the top N by
    count would produce a corpus that is mostly one host's URL template. The
    host cap is applied as rows are taken, so a host that has already spent its
    budget is skipped rather than displacing a later host.

    Buckets: ``social`` and ``video`` pages are always ``reach``. If that does
    not fill the reach target, the lowest-ranked article candidates top it up —
    "did the fetcher get through" is answerable for any page, and the operator's
    history simply does not contain sixty social pages. The top-up takes the
    *most*-cited remaining hosts, not the least: a reach verdict on a page cited
    two hundred times says more than one on a page cited once.
    """
    by_host: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    seen_url: set[str] = set()
    for key, count in candidates.items():
        url = _as_url(key)
        canon = canonicalise(url) or url
        if canon in seen_url:
            # Two spellings of one page (`/x` and `/x/`) are one corpus row.
            continue
        seen_url.add(canon)
        by_host[_host_of(canon)].append((count, canon))

    for rows in by_host.values():
        rows.sort(key=lambda row: (-row[0], row[1]))

    hosts = sorted(by_host, key=lambda host: (-by_host[host][0][0], host))
    picked: list[dict] = []
    seen: collections.Counter[str] = collections.Counter()
    cursor = {host: 0 for host in hosts}
    while True:
        progressed = False
        for host in hosts:
            if seen[host] >= per_host:
                continue
            index = cursor[host]
            if index >= len(by_host[host]):
                continue
            _, url = by_host[host][index]
            cursor[host] = index + 1
            seen[host] += 1
            picked.append({"url": url, "host": host, "kind": kind_of(url)})
            progressed = True
        if not progressed:
            break

    primary = [row for row in picked if row["kind"] in ("social", "video", "paper")]
    rest = [row for row in picked if row["kind"] not in ("social", "video")]
    reach_rows = primary[:reach]
    if len(reach_rows) < reach:
        needed = reach - len(reach_rows)
        reach_rows += rest[:needed] if needed else []
        chosen = {row["url"] for row in reach_rows}
        rest = [row for row in rest if row["url"] not in chosen]
    content_rows = rest[:content]

    chosen_rows = content_rows + reach_rows
    split_by_host: dict[str, str] = {}
    for row in chosen_rows:
        if row["host"] not in split_by_host:
            split_by_host[row["host"]] = (
                "derive" if len(split_by_host) % 2 == 0 else "validate"
            )
    for row in content_rows:
        row["bucket"] = "content"
    for row in reach_rows:
        row["bucket"] = "reach"
    for row in chosen_rows:
        row["split"] = split_by_host[row["host"]]
    return chosen_rows


MINING_SCRIPT = """
import json, sqlite3, re, collections, sys

def walk(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("sources", "citations") and isinstance(value, list):
                for item in value:
                    walk(item, out)
            else:
                walk(value, out)
    elif isinstance(node, list):
        for item in node:
            walk(item, out)
    elif isinstance(node, str) and node.startswith("http"):
        out.append(node.rstrip(".,;"))

db = sqlite3.connect("/app/backend/data/webui.db")
found = collections.Counter()
for (payload,) in db.execute("select chat from chat").fetchall():
    if not payload:
        continue
    try:
        data = json.loads(payload)
    except Exception:
        continue
    urls = []
    walk(data, urls)
    for url in urls:
        found[url] += 1
json.dump(found, sys.stdout)
"""


def mine() -> dict[str, int]:
    """Read the citations out of ``webui.db`` on the host. Read-only.

    Runs the query inside the running container, which is the only place the
    database exists, and returns ``{url: count}``. Set ``EVAL_HOST`` to the ssh
    host that runs the Open WebUI container.
    """
    host = os.environ.get("EVAL_HOST", "homelab-host")
    result = subprocess.run(
        ["ssh", host, "docker exec -i open-webui python -"],
        input=MINING_SCRIPT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"mining failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def emit(counts: dict[str, int], *, content: int, reach: int, per_host: int,
         strike: set[str]) -> list[dict]:
    """Pick the corpus and drop struck URLs, refilling from the same pool."""
    pool = {url: n for url, n in counts.items() if canonicalise(url) not in strike}
    return pick(pool, content=content, reach=reach, per_host=per_host)


def main(argv: list[str]) -> int:
    if "--mine" not in argv and "--emit" not in argv:
        print(
            "usage: python3 eval/corpus/derive.py --mine | --emit [--strike FILE]",
            file=sys.stderr,
        )
        return 2

    if "--mine" in argv:
        counts = mine()
        accepted = {url: n for url, n in counts.items() if accept(url)}
        with open("eval/corpus/candidates.json", "w") as handle:
            json.dump(
                {"mined": len(counts), "accepted": len(accepted), "urls": accepted},
                handle, indent=1, sort_keys=True,
            )
        hosts = collections.Counter(
            _host_of(canonicalise(url) or url) for url in accepted
        )
        print(f"mined {len(counts)} urls, {len(accepted)} accepted, {len(hosts)} hosts")
        for host, count in hosts.most_common(25):
            print(f"{count:6d}  {host}")
        return 0

    with open("eval/corpus/candidates.json") as handle:
        mined = json.load(handle)
    counts = {url: n for url, n in mined["urls"].items() if accept(url)}

    strike: set[str] = set()
    if "--strike" in argv:
        path = argv[argv.index("--strike") + 1]
        with open(path) as handle:
            strike = {line.strip() for line in handle if line.strip()}

    rows = emit(counts, content=72, reach=60, per_host=3, strike=strike)
    buckets = collections.Counter(row["bucket"] for row in rows)
    splits = collections.Counter(row["split"] for row in rows)
    with open("eval/corpus/urls.json", "w") as handle:
        json.dump(
            {
                "generated": "2026-10-31",
                "source": "webui.db citations, reviewed by the operator",
                "entries": rows,
            },
            handle, indent=1,
        )
    print(f"wrote {len(rows)} entries: {dict(buckets)} splits {dict(splits)}")
    for row in rows:
        print(f"{row['bucket']:8} {row['split']:8} {row['kind']:8} {row['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
