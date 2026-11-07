"""Run every corpus URL through one pinned provider and store the answer.

The shim already exposes ``?provider=<name>``, so the eval is an ordinary client
of the shipped contract: one request per (URL, provider), a JSONL row per
answer, and the returned text in a sibling gzipped file named by its hash. The
rows are appended as they complete, so a crash resumes instead of restarting,
and the text is content-addressed so two runs that return the same page share
one file.

Nothing here decides whether an answer is *good* — that is :mod:`eval.score`'s
job, and keeping the two apart is what lets a run be re-scored without spending
a provider credit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = os.environ.get("SEARCHHUB_BASE", "https://searchhub.homelab.example.com")
HARD_FAILURES = ("no_credits", "unauthorized")
"""Provider states that end the run: they will not clear without a restart."""

MAX_WORKERS = 4
"""Four, matching the shim's own concurrency; more would just queue there."""

TOKEN_ENV = "SEARCHHUB_TOKEN"


def load_corpus(path: Path) -> list[dict]:
    """The reviewed corpus, or an empty list if it has not been committed yet."""
    with open(path) as handle:
        return json.load(handle)["entries"]


def load_done(path: Path) -> set[tuple[str, str]]:
    """``(url, provider)`` pairs already in ``path``.

    A partially written last line is ignored rather than fatal: a crash
    mid-append must not make the next run skip a URL it never fetched.
    """
    done: set[tuple[str, str]] = set()
    if not path.exists():
        return done
    with open(path) as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if "url" in row and "provider" in row:
                done.add((row["url"], row["provider"]))
    return done


def row_from_response(
    url: str, provider: str, http: int, body: dict, elapsed_ms: float
) -> tuple[dict, str | None]:
    """One stored row plus the text, if the shim handed any over.

    ``success: true`` with a non-blank markdown is the only shape that carries
    text. Everything else keeps the shim's own words in ``error``, which is
    what makes ``wall: just a moment`` and ``too short: 172 chars`` visible to
    the scorer without guessing.
    """
    data = body.get("data") if isinstance(body, dict) else None
    text = ""
    metadata: dict = {}
    if isinstance(body, dict) and body.get("success") is True and isinstance(data, dict):
        markdown = data.get("markdown")
        if isinstance(markdown, str):
            text = markdown
        if isinstance(data.get("metadata"), dict):
            metadata = data["metadata"]
    ok = bool(text.strip())
    row = {
        "url": url,
        "provider": provider,
        "http": http,
        "ok": ok,
        "chars": len(text),
        "words": len(text.split()),
        "elapsed_ms": round(elapsed_ms, 1),
        "error": None if ok else (
            body.get("error") if isinstance(body, dict) and body.get("error") else f"http {http}"
        ),
        "text_sha1": hashlib.sha1(text.encode()).hexdigest() if ok else None,
        # The provider's own idea of the page: evidence that it followed a
        # redirect somewhere else, and the only title a `reach` row can be
        # judged against when the canon has none.
        "title": str(metadata.get("title") or ""),
        "source_url": str(metadata.get("sourceURL") or ""),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return row, (text if ok else None)


def fetch_one(
    base: str, token: str, provider: str, url: str, budget_ms: int
) -> tuple[dict, str | None]:
    """One pinned scrape. Never raises for an upstream answer: those are rows."""
    request = urllib.request.Request(
        f"{base}/v2/scrape?provider={provider}",
        method="POST",
        data=json.dumps({"url": url, "formats": ["markdown"], "timeout": budget_ms}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=budget_ms / 1000.0 + 30) as response:
            body = json.loads(response.read() or b"{}")
            return row_from_response(
                url, provider, response.status, body, (time.monotonic() - started) * 1000
            )
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {"error": f"http {exc.code}"}
        return row_from_response(
            url, provider, exc.code, body, (time.monotonic() - started) * 1000
        )
    except Exception as exc:  # transport, timeout, malformed body
        body = {"error": f"{type(exc).__name__}: {exc}"}
        return row_from_response(
            url, provider, 0, body, (time.monotonic() - started) * 1000
        )


def _store_text(root: Path, provider: str, digest: str, text: str) -> None:
    target = root / "text" / provider / f"{digest}.txt.gz"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wt") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--providers", default="crw,jina,tavily,exa")
    parser.add_argument("--bucket", default="all", choices=("all", "content", "reach"))
    parser.add_argument("--corpus", default="eval/corpus/urls.json")
    parser.add_argument("--results", default="eval/results")
    parser.add_argument("--base", default=os.environ.get("SEARCHHUB_BASE", DEFAULT_BASE))
    parser.add_argument("--budget-ms", type=int, default=120_000)
    parser.add_argument("--jobs", type=int, default=MAX_WORKERS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"FATAL: {TOKEN_ENV} is not set", file=sys.stderr)
        return 2

    corpus = load_corpus(Path(args.corpus))
    if args.bucket != "all":
        corpus = [entry for entry in corpus if entry["bucket"] == args.bucket]

    root = Path(args.results) / args.run_id
    root.mkdir(parents=True, exist_ok=True)
    providers = [name.strip() for name in args.providers.split(",") if name.strip()]

    host_locks = {host: threading.Semaphore(1) for host in {e["host"] for e in corpus}}
    write_lock = threading.Lock()
    hard_failed: list[str] = []

    for provider in providers:
        jsonl = root / f"{provider}.jsonl"
        done = set() if args.force else load_done(jsonl)
        todo = [entry for entry in corpus if (entry["url"], provider) not in done]
        print(f"--- {provider}: {len(todo)} to fetch ({len(done)} already done)")

        def work(entry: dict, provider: str = provider) -> None:
            with host_locks[entry["host"]]:
                row, text = fetch_one(
                    args.base, token, provider, entry["url"], args.budget_ms
                )
            if text is not None:
                _store_text(root, provider, row["text_sha1"], text)
            with write_lock:
                with open(jsonl, "a") as handle:
                    handle.write(json.dumps(row) + "\n")
                if row["error"] and any(h in row["error"] for h in HARD_FAILURES):
                    hard_failed.append(f"{provider}: {row['error']}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for _ in pool.map(work, todo):
                pass

    rows = []
    for provider in providers:
        path = root / f"{provider}.jsonl"
        if path.exists():
            with open(path) as handle:
                rows.extend(json.loads(line) for line in handle if line.strip())
    ok = sum(1 for row in rows if row["ok"])
    print(f"--- {len(rows)} rows, {ok} with text, run {args.run_id}")
    if hard_failed:
        print(f"FATAL: provider hard-failed: {sorted(set(hard_failed))[:3]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
