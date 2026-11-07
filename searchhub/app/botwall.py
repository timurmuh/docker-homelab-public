"""Detection of blocked fetches: bot walls, consent walls, paywalls, login walls.

Every string in :data:`SIGNATURES` comes from a real failed fetch observed in
this homelab -- it appears verbatim (lowercased) in at least one recorded tool
output (``fetch_url``, or a ``run_command`` that curled a reader). See
``searchhub/README.md`` for the summary of the classes and the length rules.
"""

from __future__ import annotations

MIN_CONTENT_CHARS: int = 200
"""Fewest characters a fetch must return before it can count as content.

Comes from the Plan B spec (2026-10-29), not from the mined history: it is the
floor below which a page is assumed truncated or empty rather than merely short.
Task 6 uses it alongside :func:`looks_blocked`.
"""

WALL_SCAN_CHARS: int = 2000
"""Signatures are only searched for in the first 2,000 characters (2026-10-29 spec).

With ``WALL_MAX_CHARS`` at 1,000 this window is never the binding constraint --
every text that can match is shorter than the window. It exists so that raising
the cap later still cannot be fooled by a wall marker buried in a footer.
"""

WALL_MAX_CHARS: int = 1000
"""At or above this length the text is content, whatever it contains (2026-10-29 spec).

A wall is a page that is *only* the wall: in the mined history every such page
is under 1,000 characters (131 to 912). A cookie banner on a 4,000-character
article is not a wall -- the article is still there, so the text must be
returned to the caller. Task 6 relies on exactly this split: below the
threshold the text is poisoned and is never returned; above it, it is content.
"""

SIGNATURES: tuple[str, ...] = (
    # -- bot wall: Cloudflare / JS challenge / WAF interstitials ------------
    # Cloudflare challenge page title, e.g. "Title: Just a moment..." (observed
    # on a retailer's challenge page, plus one article that merely quotes the
    # phrase). Also the JS challenge heading.
    "just a moment",
    # "Checking your browser before accessing <host>" -- Cloudflare's
    # pre-challenge waiting room.
    "checking your browser",
    # Cloudflare's newer interstitial heading, seen across many hosts.
    "performing security verification",
    # Body of that same interstitial; kept separately because some readers
    # drop the heading but keep the paragraph.
    "protect against malicious bots",
    # First line of the Cloudflare block page ("Please enable cookies. Sorry,
    # you have been blocked / You are unable to access <host>"), several hosts.
    "sorry, you have been blocked",
    # A social site's challenge page heading.
    "prove your humanity",
    # A social site's block page body. Emitted without the possessive by
    # another site (a live capture: 258 characters, over the content floor,
    # returned to the model), so the shorter form is the one that matches; it
    # is a superset of that wording.
    "blocked by network security",
    # A localised "confirm you are flesh and blood" challenge.
    "masa a kostí",
    # CloudFront 403: "403 ERROR / The request could not be satisfied."
    "the request could not be satisfied",
    # A publisher's Cloudflare error page: "There was a problem providing the
    # content you requested", followed by a CF error box.
    "there was a problem providing the content you requested",
    # Some publishers serve this instead of the article when they refuse a
    # client.
    "reader experiencing an access issue",
    # Brand token present on every Cloudflare wall above, and on the canonical
    # "Attention Required! Cloudflare" page, which itself never appears in the
    # history. Last in the tuple so that a page carrying both this token and a
    # phrase-level signature reports the specific phrase -- position changes
    # only *which* string is returned, never *whether* a page is rejected.
    # Membership is what rejects a short page that merely mentions Cloudflare
    # (see the bot-wall section in `searchhub/README.md`); the proper fix is a
    # compound rule (this token AND "ray id" / "checking your browser"),
    # deferred to the eval plan.
    "cloudflare",
    # -- consent: cookie / GDPR walls --------------------------------------
    # EMPTY ON PURPOSE. No cookie wall in the history is a wall: every page
    # carrying consent text also carries the real article, and the shortest of
    # them is 1,180 characters -- above WALL_MAX_CHARS, so it is content, not a
    # wall. Adding "we value your privacy" here would only ever match real
    # articles.
    # -- paywall: subscriber-only pages ------------------------------------
    # EMPTY ON PURPOSE. "subscribe to continue", "to continue reading",
    # "already a subscriber" and "unlock this article" have zero matches in
    # the history; "subscribers only" appeared only inside a long article.
    # -- login: sign-in required -------------------------------------------
    # EMPTY ON PURPOSE. "sign in to continue" and "log in to continue" have
    # zero matches. The one login-gated fetch was a 314-character nav stub
    # whose only login marker is a link to that site's own auth URL -- not a
    # generalisable phrase.
)


CLOUDFLARE_MARKERS: tuple[str, ...] = (
    "ray id",
    "cf-chl",
    "attention required",
    "checking your browser",
    "just a moment",
    "performing security verification",
    "prove your humanity",
)
"""What must accompany the brand token for it to count as a challenge.

The bare token ``cloudflare`` was a membership test, and membership is not
evidence: a 400-character page about a Cloudflare outage was rejected as a
wall. Every actual challenge page in the sample carries one of these beside the
brand, and a page merely mentioning Cloudflare does not.
"""


def looks_blocked(text: str) -> str | None:
    """Return the bot-wall signature that ``text`` matches, else ``None``.

    A signature only counts when it appears in the first
    :data:`WALL_SCAN_CHARS` characters *and* the whole text is shorter than
    :data:`WALL_MAX_CHARS`. That two-sided rule is deliberate: below the
    length cap the text is a wall and must never reach a caller; above it the
    text is a real page that merely mentions a wall phrase somewhere.
    """
    if not text:
        return None
    if len(text) >= WALL_MAX_CHARS:
        return None
    head = text[:WALL_SCAN_CHARS].lower()
    for signature in SIGNATURES:
        if signature == "cloudflare":
            # Compound rule: the brand alone is not a challenge (plan C,
            # follow-up item 4). A co-occurring marker is what makes it one.
            if "cloudflare" in head and any(m in head for m in CLOUDFLARE_MARKERS):
                return signature
            continue
        if signature in head:
            return signature
    return None
