"""Score provider answers against the canonical text.

Three pure judgements live here, and nothing that talks to a network:

* :func:`containment` — how much of the article a provider returned (``recall``)
  and how much of what it returned *is* the article (``precision``), over
  5-token shingles. Both directions matter: recall alone cannot tell "complete
  but noisy" from "clean but truncated", which is the difference between a
  provider that renders the page and one that trims it.
* :func:`gate` — whether a captured canon is trustworthy at all. A canon that is
  9% of the page's text is navigation; scoring against it measures the
  extractor, not the provider.
* :func:`classify_row` — what a stored answer row actually was. A 200 carrying
  an error page is a failure, and our own budget truncation is not a provider's
  fault.

Shingles are a multiset on purpose: a provider that repeats a paragraph forty
times must not be able to inflate recall by repetition.
"""

from __future__ import annotations

import collections
import json
import re
import statistics

CONSENT_MARKERS: tuple[str, ...] = (
    "cookie preferences",
    "we use cookies",
    "accept all cookies",
    "before you continue",
    "consent",
)
"""Phrases that mean the text is a consent interstitial rather than a page.

Matched in the first :data:`HEAD_CHARS` characters only: a 4,000-character
article that discusses cookies is content, and the design's whole point is that
a banner *with* an article stays content.
"""

CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "performing security verification",
    "prove your humanity",
)
"""The strongest bot-wall phrases, used for measuring what the shim accepted."""

HEAD_CHARS: int = 400
GATE_RATIO: float = 0.5
GATE_MIN_WORDS: int = 200
MIN_CONTENT_WORDS: int = 200
SHINGLE_SIZE: int = 5

ERROR_PAGE = re.compile(
    r"access denied|forbidden|rate limit|too many requests|service unavailable"
)
"""A 200 that is really an error: the provider reached *something*, not the page."""

LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
TOKEN = re.compile(r"[a-z0-9]+")


def normalise(text: str) -> list[str]:
    """Tokens for scoring: link targets dropped, anchor text kept.

    Keeping a URL's path would let a provider score on a link it never read;
    dropping the anchor text would discard real words.
    """
    without_targets = LINK.sub(r"\1", text or "")
    return TOKEN.findall(without_targets.lower())


def shingles(tokens: list[str], n: int = SHINGLE_SIZE) -> collections.Counter:
    """5-token shingles as a multiset."""
    if len(tokens) < n:
        return collections.Counter()
    return collections.Counter(" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1))


def _recall(canon: collections.Counter, cand: collections.Counter) -> float:
    total = sum(canon.values())
    if not total:
        return 0.0
    return sum((canon & cand).values()) / total


def containment(canon: str, cand: str) -> dict:
    """Coverage of ``canon`` by ``cand``, and how much of ``cand`` is canon.

    ``paragraph_median`` and ``thin_paragraphs`` exist because whole-document
    recall cannot tell a provider that dropped the last third from one that
    returned a uniform smear: the first has intact leading paragraphs, the
    second has none.
    """
    canon_shingles = shingles(normalise(canon))
    cand_shingles = shingles(normalise(cand))
    article = sum(canon_shingles.values())
    returned = sum(cand_shingles.values())
    overlap = sum((canon_shingles & cand_shingles).values())
    canon_words = len(normalise(canon))

    coverages = []
    for paragraph in re.split(r"\n\s*\n", canon or ""):
        if len(normalise(paragraph)) < 8:
            continue
        coverages.append(_recall(shingles(normalise(paragraph)), cand_shingles))
    coverage_values = sorted(coverages)

    return {
        "recall": overlap / article if article else 0.0,
        "precision": overlap / returned if returned else 0.0,
        "paragraph_median": statistics.median(coverage_values) if coverage_values else 0.0,
        "thin_paragraphs": sum(1 for value in coverage_values if value < 0.25),
        "paragraphs": len(coverage_values),
        "canon_words": canon_words,
    }


def gate(canon: str, innertext: str) -> dict:
    """Whether a captured canon may be scored against, and why not if not.

    Order is load-bearing: the ratio first, because a canon smaller than half
    the page is the extractor failing, and only then the marker checks, which
    catch a canon that is *most* of the page but still the wrong part of it —
    a consent overlay that Readability preferred to the article.
    """
    canon_words = len(normalise(canon))
    page_words = len(normalise(innertext))
    ratio = canon_words / page_words if page_words else 0.0
    head = (canon or "")[:HEAD_CHARS].lower()

    if ratio < GATE_RATIO:
        reason = "ratio_low"
    elif any(marker in head for marker in CONSENT_MARKERS):
        reason = "consent_marker"
    elif any(marker in head for marker in CHALLENGE_MARKERS):
        reason = "challenge_marker"
    elif canon_words < GATE_MIN_WORDS:
        reason = "too_short"
    else:
        reason = None

    return {"ok": reason is None, "reason": reason, "ratio": ratio, "canon_words": canon_words}


def reach_pass(title: str, cand: str, min_words: int = MIN_CONTENT_WORDS) -> bool:
    """Whether a provider plausibly reached the page, judged by its identity.

    Half the title's content-bearing words (4+ characters) must appear, and the
    answer must carry at least ``min_words`` words. Deliberately loose: ``reach``
    asks "did you get the page", not "did you get it byte for byte".
    """
    wanted = {token for token in TOKEN.findall((title or "").lower()) if len(token) >= 4}
    if not wanted:
        return False
    if len((cand or "").split()) < min_words:
        return False
    present = set(TOKEN.findall((cand or "").lower()))
    return len(wanted & present) >= len(wanted) / 2


def classify_row(row: dict, text: str | None) -> str:
    """The honest name of one stored answer: ``ok`` or the way it failed.

    ``harness_budget`` comes first because our own budget and cap truncations
    are recorded in the same ``error`` field as provider failures, and counting
    them against a provider would blame crw for the caller's timeout.
    """
    error = str(row.get("error") or "")
    if "budget_truncated" in error or "cap_truncated" in error:
        return "harness_budget"
    if row.get("http") != 200 or not row.get("ok"):
        return "no_text"
    if not (text or "").strip():
        return "empty"
    if len(text) < 1000 and ERROR_PAGE.search(text.lower()):
        return "error_page"
    if len(text.split()) < MIN_CONTENT_WORDS:
        return "too_short"
    return "ok"


def usable(row: dict, entry: dict | None) -> bool:
    """Whether a row may be scored, given what the capture did with that URL.

    A redirected capture means the browser and the provider may have been
    looking at different documents; scoring it would measure the redirect.
    """
    status = (entry or {}).get("status")
    return status not in ("redirected", "dropped")


def _percentile(values: list[float], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return int(ordered[index])


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_report(rows: list[dict], canon: dict, corpus: list[dict], status: dict) -> str:
    """The committed report: per bucket, per split, per provider, with denominators.

    Every number is printed beside the population it was computed on, because a
    recall of 0.95 over four URLs and over fifty are different claims and a
    table that hides the denominator invites the wrong one.
    """
    by_url = {entry["url"]: entry for entry in corpus}
    usable_rows: dict[str, list[tuple[dict, dict]]] = collections.defaultdict(list)
    for row in rows:
        entry = by_url.get(row["url"])
        if entry is None:
            continue
        if not usable(row, status.get(row["url"])):
            continue
        usable_rows[entry["bucket"]].append((row, entry))

    lines = ["# Fetch eval report", ""]

    for bucket in ("content", "reach"):
        pairs = usable_rows.get(bucket, [])
        total = sum(1 for entry in corpus if entry["bucket"] == bucket)
        # Both buckets need a canon: content is scored against its text, reach
        # against its title, and neither exists when the capture was refused.
        # status.json is the authority for what the capture did: it is the
        # overlay the design names, and it is what usable() already trusts.
        # Reading canon records here instead let the two disagree and silently
        # dropped seven usable rows.
        # Reach has no canon to gate: a page that reads as navigation is still
        # a fair question ("did anyone get through"), and its title is enough.
        required = "ok" if bucket == "content" else ("ok", "reclassified")
        scored = [
            (row, entry)
            for row, entry in pairs
            if (status.get(row["url"]) or {}).get("status") in (
                required if isinstance(required, tuple) else (required,)
            )
        ]
        providers = sorted({row["provider"] for row, _ in scored})

        lines += [f"## {bucket}", ""]
        lines.append(
            f"denominator: {len({entry['url'] for _, entry in scored})} of {total} "
            f"{bucket} URLs have a usable canon and were scored"
        )
        lines.append("")
        lines.append(
            "| provider | n | recall | precision | para median | p50 ms | p95 ms | failures |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")

        for provider in providers:
            answers = [row for row, _ in scored if row["provider"] == provider]
            good = [(row, (canon.get(row["url"]) or {}).get("text", "")) for row in answers
                    if classify_row(row, row.get("text")) == "ok"]
            if bucket == "content":
                # A failed answer scores 0 on both axes, so `n` is the same
                # population the means were computed over. Averaging only the
                # survivors made four providers' numbers come from four
                # different URL sets.
                scores = [
                    containment((canon.get(row["url"]) or {}).get("text", ""), row.get("text") or "")
                    if classify_row(row, row.get("text")) == "ok"
                    else {"recall": 0.0, "precision": 0.0, "paragraph_median": 0.0}
                    for row in answers
                ]
                recall = _mean([item["recall"] for item in scores])
                precision = _mean([item["precision"] for item in scores])
                median = _mean([item["paragraph_median"] for item in scores])
            else:
                passed = 0
                for row in answers:
                    if classify_row(row, row.get("text")) != "ok":
                        continue
                    title = (canon.get(row["url"]) or {}).get("title", "")
                    if reach_pass(title, row.get("text") or ""):
                        passed += 1
                recall = passed / len(answers) if answers else 0.0
                precision = float("nan")
                median = float("nan")
            failures = len(answers) - len(good)
            recall_text = f"{recall:.2f}" if recall == recall else "n/a"
            precision_text = f"{precision:.2f}" if precision == precision else "—"
            median_text = f"{median:.2f}" if median == median else "—"
            lines.append(
                f"| {provider} | {len(answers)} | {recall_text} | {precision_text} | "
                f"{median_text} | {_percentile([r['elapsed_ms'] for r in answers], 0.5)} | "
                f"{_percentile([r['elapsed_ms'] for r in answers], 0.95)} | {failures} |"
            )
        lines.append("")

        split_lines = ["| provider | split | n | recall | precision |", "|---|---|---|---|---|"]
        for provider in providers:
            for split in ("derive", "validate"):
                answers = [
                    row for row, entry in scored
                    if row["provider"] == provider and entry["split"] == split
                    and classify_row(row, row.get("text")) == "ok"
                ]
                if bucket != "content":
                    split_lines.append(f"| {provider} | {split} | {len(answers)} | — | — |")
                    continue
                scores = [containment((canon.get(row["url"]) or {}).get("text", ""), row.get("text") or "")
                          for row in answers]
                split_lines.append(
                    f"| {provider} | {split} | {len(answers)} | "
                    f"{_mean([s['recall'] for s in scores]):.2f} | "
                    f"{_mean([s['precision'] for s in scores]):.2f} |"
                )
        lines += split_lines + [""]

    taxonomy = collections.Counter()
    for row in rows:
        if row["url"] in by_url and usable(row, status.get(row["url"])):
            outcome = classify_row(row, row.get("text"))
            # A wall is the single most decision-relevant failure, and
            # classify_row folds it into no_text. Name the signature.
            if outcome == "no_text" and "wall: " in str(row.get("error") or ""):
                outcome = "wall: " + str(row["error"]).split("wall: ", 1)[1]
            taxonomy[(row["provider"], outcome)] += 1
    stale = []
    for row in rows:
        captured = (canon.get(row["url"]) or {}).get("captured_at")
        fetched = row.get("fetched_at")
        if not captured or not fetched:
            continue
        try:
            from datetime import datetime

            then = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            now = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - then).days > 7:
            stale.append(row["url"])
    lines += ["## staleness", ""]
    if stale:
        lines.append(
            f"- **stale**: {len(set(stale))} URLs were captured more than 7 days before the run "
            f"and may have changed since: {', '.join(sorted(set(stale))[:5])}"
        )
    else:
        lines.append("- no URL was captured more than 7 days before the run")
    lines.append("")
    lines += ["## failure taxonomy", "", "| provider | outcome | n |", "|---|---|---|"]
    for (provider, outcome), count in sorted(taxonomy.items()):
        lines.append(f"| {provider} | {outcome} | {count} |")
    lines.append("")
    return "\n".join(lines)


def load_canon(directory: str = "eval/canon") -> dict:
    """Canon per URL: the text, the capture status, and the page title."""
    from pathlib import Path

    out: dict[str, dict] = {}
    for record_path in Path(directory).glob("*.json"):
        record = json.loads(record_path.read_text())
        gz = Path(directory) / f"{record.get('sha1')}.md.gz"
        text = ""
        if gz.exists():
            import gzip

            text = gzip.open(gz, "rt").read()
        out[record["url"]] = {
            "status": record.get("status"),
            "text": text,
            "title": record.get("title", ""),
            "captured_at": record.get("captured_at", ""),
            "final_url": record.get("final_url", record["url"]),
        }
    return out


def load_rows(run_ids: list[str], results: str = "eval/results") -> list[dict]:
    """Every stored answer for the given runs, with its text attached."""
    from pathlib import Path

    rows: list[dict] = []
    for run_id in run_ids:
        root = Path(results) / run_id
        for jsonl in sorted(root.glob("*.jsonl")):
            for line in jsonl.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                digest = row.get("text_sha1")
                text = None
                if digest:
                    gz = root / "text" / row["provider"] / f"{digest}.txt.gz"
                    if gz.exists():
                        import gzip

                        text = gzip.open(gz, "rt").read()
                row["text"] = text
                rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--also-run", action="append", default=[])
    parser.add_argument("--corpus", default="eval/corpus/urls.json")
    parser.add_argument("--status", default="eval/corpus/status.json")
    parser.add_argument("--canon", default="eval/canon")
    parser.add_argument("--results", default="eval/results")
    parser.add_argument("--report", action="store_true", help="write eval/report/<id>.md")

    args = parser.parse_args(argv)

    corpus = _json.load(open(args.corpus))["entries"]
    status = _json.load(open(args.status))["entries"]
    canon = load_canon(args.canon)
    rows = load_rows([args.run_id, *args.also_run], args.results)
    report = build_report(rows, canon, corpus, status) + analysis_sections(rows, canon, corpus, status)
    # The recommendation is a human judgement, not a computed table. Keeping it
    # in its own committed file means re-scoring a run cannot delete the
    # decision the run was built to inform.
    from pathlib import Path as _Path

    decision = _Path("eval/report/recommendation.md")
    if decision.exists():
        report = report + "\n" + decision.read_text()
    print(report)
    if args.report:
        Path("eval/report").mkdir(parents=True, exist_ok=True)
        target = Path("eval/report") / f"{args.run_id}.md"
        target.write_text(report)
        print(f"\nwritten: {target}")
    return 0




FETCH_ORDER: tuple[str, ...] = ("crw", "jina", "tavily", "exa")
"""The order in force for the run, recorded so a later reorder cannot rewrite
an analysis that was computed under the old one."""


def analysis_sections(rows: list[dict], canon: dict, corpus: list[dict], status: dict) -> str:
    """The two numbers the plan owes from measurement alone.

    * how often an **accepted** answer carried a challenge marker (design item 5,
      the over-1,000-character class), and
    * how often **bounded patience** would have fired, and whether the next
      provider in the chain was measurably better.
    """
    by_url: dict[str, list[dict]] = collections.defaultdict(list)
    buckets = {entry["url"]: entry["bucket"] for entry in corpus}
    for row in rows:
        by_url[row["url"]].append(row)

    accepted_with_marker = 0
    accepted_error_page = 0
    accepted_total = 0
    for row in rows:
        text = row.get("text")
        if not text or classify_row(row, text) != "ok":
            continue
        accepted_total += 1
        if any(marker in text.lower() for marker in CHALLENGE_MARKERS):
            accepted_with_marker += 1
        if len(text) < 2500 and ERROR_PAGE.search(text.lower()):
            accepted_error_page += 1

    considered = flagged = better = 0
    for url, answers in by_url.items():
        if buckets.get(url) != "content":
            continue
        if (status.get(url) or {}).get("status") != "ok":
            continue
        ordered = sorted(
            [row for row in answers if row.get("text")],
            key=lambda row: FETCH_ORDER.index(row["provider"])
            if row["provider"] in FETCH_ORDER else len(FETCH_ORDER),
        )
        if len(ordered) < 2:
            continue
        considered += 1
        first = ordered[0]
        if len(first["text"].split()) < 300 or any(
            marker in first["text"][:400].lower() for marker in CONSENT_MARKERS + CHALLENGE_MARKERS
        ):
            flagged += 1
            first_precision = containment(canon[url]["text"], first["text"])["precision"]
            for later in ordered[1:]:
                if containment(canon[url]["text"], later["text"])["precision"] > first_precision:
                    better += 1
                    break

    lines = ["## analysis", ""]
    lines.append(
        f"- accepted answers carrying a challenge marker: **{accepted_with_marker}** of "
        f"{accepted_total} (the over-1,000-character class; near zero needs no scan extension)"
    )
    lines.append(
        f"- accepted answers that look like an error page: **{accepted_error_page}** of "
        f"{accepted_total} (the over-1,000-character class item 5 measures)"
    )
    lines.append(
        f"- bounded patience: would fire on **{flagged}** of {considered} content URLs with "
        f"two or more answers; the next provider was better on **{better}** of those"
    )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
