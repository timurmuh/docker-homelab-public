## recommendation

**Keep `FETCH_ORDER=crw,jina,tavily,exa`.** No reorder is justified by these
numbers, and the argument is written down so a future reader does not have to
redo the eval to change it.

- `crw` leads because it is the only provider that reaches the hard pages:
  **0.62** of reach URLs against 0.53 (tavily), 0.51 (jina) and 0.44 (exa),
  with the fewest reach failures (10 of 45). On content it is second on recall
  (0.78) and cheap per page, and its p50 is 532 ms.
- `jina` stays second on **recall** — the best of the four at 0.83 — despite
  being the slowest (p50 4.2 s, p95 16.4 s) and the noisiest (precision 0.60).
  Position two is cheap to hold: crw accepts most content URLs, so jina only
  runs when crw fails and its latency is rarely on the user's path.
- `tavily` third: fastest median (267 ms), mid-pack reach (0.53), but the
  lowest content recall (0.68) and the most content failures (7 of 65).
- `exa` last on purpose. Its fidelity is the best of the paid pair (precision
  0.74) and its median is fast (334 ms), but it has the worst reach (0.44) and
  the most reach failures (19 of 45), and it is the only rung that costs money
  per page ($1/1k). Leading with it would spend credits on pages crw fetches
  for free, and would return "no content" on exactly the pages the chain exists
  to rescue.

The honest summary: the eval found no reordering that improves the outcome,
which is itself the result. `SEARCH_ORDER` was left alone by design — search
ranking was dropped from this plan on 2026-10-31.

### Caveats a reader must carry

- **Reach is 45 of 60 URLs.** Fifteen were redirected somewhere else — nine of
  them LinkedIn's consent interstitial, which is only reachable by accepting
  cookies, and the rest login walls on Reddit, X and TikTok — and two more were
  a captured page that was navigation rather than content. The reach column is
  therefore a statement about Reddit, X, Quora, YouTube, TikTok and academic
  paywalls. The corpus contains Cloudflare's own docs (`developers.cloudflare.com`)
  but **no Cloudflare-challenged site**, so the column says nothing about
  Cloudflare specifically.
- **`n` counts every answer, failures included as 0.** A provider that failed
  scores 0 rather than disappearing from its own average.
- **The ≥0.75 / <0.25 bands were not applied to any row.** Every content URL in
  the corpus is a real page with a healthy canon, so the middle band never came
  into play; the means above are aggregates, not a tally of bands.
