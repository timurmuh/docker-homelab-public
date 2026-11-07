# The fetch-quality eval

Ranks the four fetch providers against canonical page text captured in a real
browser, so `FETCH_ORDER` is a decision with numbers behind it rather than a
preference.

## What is and is not in this repository

The harness is here. The corpus data is not.

`eval/corpus/urls.json` and `eval/corpus/status.json` describe a specific
operator's reviewed URL set, and `eval/canon/` and `eval/results/` contain the
pages and answers fetched for it. Those are deliberately excluded from the
public snapshot: a corpus derived from private browsing data is not something
to publish. `eval/corpus/derive.py` is included so the derivation is
reproducible; supply your own `corpus/urls.json` and capture its canon locally.

The corpus format is:

```json
{
  "entries": [
    {"url": "https://example.com/article", "bucket": "content", "host": "example.com"},
    {"url": "https://example.org/feed", "bucket": "reach", "host": "example.org"}
  ]
}
```

`bucket` is `content` or `reach`; `host` is used to split derive/validate sets
and to cap URLs per host.

## Running it

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python pytest
```

```sh
# 1. fetch through searchhub, one pinned provider at a time
SEARCHHUB_TOKEN=... python3 eval/run.py \
  --run-id 2026-11-01-a \
  --base https://searchhub.example.com \
  --providers crw,jina,tavily,exa

# 2. score the run; optionally include an earlier run's rows
python3 eval/score.py --run-id 2026-11-01-a --also-run 2026-10-31-exa --report

# 3. capture canon for new corpus URLs (drives the operator's Chrome)
EVAL_BROWSER="install:Chrome" node eval/capture.mjs --bucket content
```

`eval/run.py` skips `(url, provider)` pairs already present in the run's JSONL
unless `--force`, so a crashed run resumes instead of restarting. It exits 1
when a provider hard-fails (`no_credits`, `unauthorized`) because that run is
incomplete and a silent `0.00` would read as a quality verdict.

`SEARCHHUB_TOKEN` is the bearer token for `searchhub`; `--base` points at the
instance used for the run. Neither is committed.

## The corpus

A `content` URL is scored on containment. A `reach` URL is scored on identity
(`document.title`) because a login wall has no article to contain. A `content`
URL whose canon fails the gate is reclassified to `reach`; one that redirects
somewhere else is dropped from every denominator.

`corpus/status.json` records what the browser did with each URL during capture:
`ok`, `redirected`, `reclassified` or `dropped`.

## Columns

| column | meaning |
|---|---|
| `n` | answers scored for that provider in that bucket |
| `recall` | `|canon ∩ cand| / |canon|`, 5-word shingles — how much of the article came back |
| `precision` | `|canon ∩ cand| / |cand|` — how much of what came back is the article |
| `para median` | per-paragraph recall; separates a truncated tail from uniform noise |
| `p50` / `p95` | wall-clock per request, milliseconds |
| `failures` | answers that were not `ok` by `classify_row` |

`recall` and `precision` are normalised the same way: lowercase, markdown link
targets replaced by their anchor text, whitespace collapsed, tokens
`[a-z0-9]+`. Link targets are dropped on **both** sides on purpose — otherwise
precision would punish `crw` and Jina for emitting markdown links that Exa never
emits, which is a format difference, not a content one.

Thresholds: `recall ≥ 0.75` is OK, `< 0.25` is a failure, the band between is
reviewed by hand. Shingles are a multiset, so repeating a paragraph cannot
inflate a score.

## The canon gate

A canon is scored only when `canon_words / innertext_words ≥ 0.5`, no
consent/challenge marker appears in its first 400 characters, and it has at
least 200 words. Below that ratio the extractor found navigation, not the page.
Such a URL is reclassified, never scored.

Consent banners are dismissed only when extraction fails, and only by
reject/decline, never accept. Every dismissal is recorded per URL.

## Adding a URL

1. Add the entry to `corpus/urls.json`.
2. Capture its canon with `eval/capture.mjs`.
3. Review the captured text and the status the gate assigned it.
4. Re-run the provider pass and score it.

The two halves are separate on purpose: `run.py` does not decide whether an
answer is *good*, and `score.py` never touches the network. That is what lets a
run be re-scored without spending a provider credit.
