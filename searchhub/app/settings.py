"""Runtime configuration, resolved once from the process environment.

Every value the service needs to start is read here and carried in an immutable
:class:`Settings`; nothing else in the app reads ``os.environ``. Reading the
environment once means a restart is the only way to change configuration, which
is what a read-only container and a Compose ``env_file`` imply.

Secrets are only ever read from the environment. Nothing in this module writes
a key to disk, a log, or a response.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from app.botwall import MIN_CONTENT_CHARS

DEFAULT_CRW_URL: str = "http://crw:3000"
"""Where crw listens inside the Compose network (Plan B spec, 2026-10-29)."""

DEFAULT_SEARCH_BUDGET_S: float = 35.0
"""Wall-clock ceiling for one ``/v2/search`` request (Plan B spec)."""

DEFAULT_SCRAPE_BUDGET_S: float = 75.0
"""Wall-clock ceiling for one ``/v2/scrape`` request (Plan B spec)."""

DEFAULT_PROVIDER_TIMEOUT_S: float = 15.0
"""Per-upstream HTTP timeout for one *search* call (Plan B spec)."""

DEFAULT_FETCH_PROVIDER_TIMEOUT_S: float = 30.0
"""Per-upstream ceiling for one *fetch* call, in seconds.

Separate from :data:`DEFAULT_PROVIDER_TIMEOUT_S` on purpose. crw's measured
worst case is 16.96 s on exactly the terse pages it renders through the top of
its ladder, and its own per-tier ceiling is 30 s, so a 15 s cap would cancel a
fetch that was about to succeed and attribute the cap to crw. The search chain
never renders, so it keeps 15 s.
"""

DEFAULT_SEARCH_ORDER: tuple[str, ...] = ("exa", "tavily", "brave", "searxng")
"""Which search provider answers first, second, ... (Plan B spec, 2026-10-29).

A *preference*, not a capability list: a provider with no key keeps its place
here and is skipped at run time. Names are validated in
:func:`app.search.build_search_providers`, not at import, so a test (or a future
tool) can construct any order and still get a clear error naming the typo.
"""


DEFAULT_FETCH_ORDER: tuple[str, ...] = ("crw", "jina", "tavily", "exa")
"""Which fetch provider answers first, second, ... (Plan B spec, 2026-10-29).

The fetch half's counterpart of :data:`DEFAULT_SEARCH_ORDER`, and the same kind
of value: a *preference* whose names are validated in
:func:`app.fetch.build_fetch_providers`, not at import. A provider with no key
keeps its place and is skipped at run time; ``crw`` needs no key.
"""


def parse_order(raw: str | tuple[str, ...]) -> tuple[str, ...]:
    """Normalise ``SEARCH_ORDER``-style input: comma-split, trimmed, deduped.

    Accepts the environment string and the already-parsed tuple, so one rule
    covers ``SEARCH_ORDER="exa, tavily"`` and a hand-built
    ``Settings(search_order="exa, tavily")``.

    Duplicates are dropped, keeping the **first** occurrence: a repeated name
    would otherwise make one request pay that provider twice and split its
    breaker state across two entries for one upstream. Deduping rather than
    raising keeps a benign copy-paste from failing a deploy, and the order of
    the remaining names is exactly what the operator wrote.
    """
    parts = raw.split(",") if isinstance(raw, str) else raw
    seen: set[str] = set()
    names: list[str] = []
    for part in parts:
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return tuple(names)


def _blank_to_none(value: str | None) -> str | None:
    """Map an unset or blank environment value to ``None``.

    A blank key means *absent*, never "configured with an empty key": a Compose
    ``env_file`` or a ``pass`` template that leaves a line empty must not make
    the service try an anonymous call to a paid provider.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


def _str(name: str, default: str) -> str:
    return _blank_to_none(os.environ.get(name)) or default


def _float(name: str, default: float) -> float:
    """A positive, finite float from the environment.

    ``float()`` happily accepts ``nan``, ``inf`` and negative numbers, and every
    number this service reads is a duration or a size: a NaN budget is not
    "unlimited", it is a deadline comparison that is always false, and a
    negative one expires before the request starts. Both would disable a chain
    silently, so they refuse to start instead, naming the variable.
    """
    raw = _blank_to_none(os.environ.get(name))
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive finite number, got {raw!r}")
    return value


def _int(name: str, default: int) -> int:
    """A positive integer from the environment (see :func:`_float`)."""
    raw = _blank_to_none(os.environ.get(name))
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    """Everything the app is configured with, resolved at startup.

    ``api_key`` is the only field without a default and therefore comes first:
    both provider orders are appended after the keys and budgets, and a field
    without a default may not follow one with a default.
    """

    api_key: str
    """Bearer token every non-``/health`` request must present."""

    crw_url: str = DEFAULT_CRW_URL
    exa_api_key: str | None = None
    tavily_api_key: str | None = None
    brave_api_key: str | None = None
    jina_api_key: str | None = None
    min_content_chars: int = MIN_CONTENT_CHARS
    search_budget_s: float = DEFAULT_SEARCH_BUDGET_S
    scrape_budget_s: float = DEFAULT_SCRAPE_BUDGET_S
    provider_timeout_s: float = DEFAULT_PROVIDER_TIMEOUT_S
    fetch_provider_timeout_s: float = DEFAULT_FETCH_PROVIDER_TIMEOUT_S
    """Per-upstream ceiling for one fetch call (30 s), separate from search's 15 s."""
    search_order: tuple[str, ...] = DEFAULT_SEARCH_ORDER
    """Provider preference, e.g. ``("exa", "tavily", "brave", "searxng")``.

    A blank ``SEARCH_ORDER`` falls back to :data:`DEFAULT_SEARCH_ORDER`; blank
    entries in the middle are dropped and a repeated name keeps its first
    position only. A string is accepted and split on commas for the same reason
    the environment arrives that way.
    """

    fetch_order: tuple[str, ...] = DEFAULT_FETCH_ORDER
    """Fetch provider preference, e.g. ``("crw", "jina", "tavily", "exa")``.

    Normalised exactly like :attr:`search_order`: blank falls back to
    :data:`DEFAULT_FETCH_ORDER`, blanks are dropped, duplicates keep their first
    position, and a string is split on commas.
    """

    def __post_init__(self) -> None:
        # Frozen, so normalisation goes through ``object.__setattr__``. Doing it
        # here rather than in ``load_settings`` means every construction path --
        # environment, test, or a later tool -- sees the same tuple shape. An
        # empty order is never meaningful, so it falls back to the default
        # rather than producing a chain that can answer nothing.
        order = parse_order(self.search_order) or DEFAULT_SEARCH_ORDER
        object.__setattr__(self, "search_order", order)
        fetch_order = parse_order(self.fetch_order) or DEFAULT_FETCH_ORDER
        object.__setattr__(self, "fetch_order", fetch_order)


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment, or refuse to start.

    Missing or blank ``SEARCHHUB_API_KEY`` is a hard error: a service that can
    only be reached by callers holding a token must not come up unauthenticated
    because a secret failed to resolve.
    """
    api_key = _blank_to_none(os.environ.get("SEARCHHUB_API_KEY"))
    if api_key is None:
        raise RuntimeError("SEARCHHUB_API_KEY is required")

    return Settings(
        api_key=api_key,
        crw_url=_str("CRW_URL", DEFAULT_CRW_URL),
        exa_api_key=_blank_to_none(os.environ.get("EXA_API_KEY")),
        tavily_api_key=_blank_to_none(os.environ.get("TAVILY_API_KEY")),
        brave_api_key=_blank_to_none(os.environ.get("BRAVE_API_KEY")),
        jina_api_key=_blank_to_none(os.environ.get("JINA_API_KEY")),
        min_content_chars=_int("MIN_CONTENT_CHARS", MIN_CONTENT_CHARS),
        search_budget_s=_float("SEARCH_BUDGET_S", DEFAULT_SEARCH_BUDGET_S),
        scrape_budget_s=_float("SCRAPE_BUDGET_S", DEFAULT_SCRAPE_BUDGET_S),
        provider_timeout_s=_float("PROVIDER_TIMEOUT_S", DEFAULT_PROVIDER_TIMEOUT_S),
        fetch_provider_timeout_s=_float(
            "FETCH_PROVIDER_TIMEOUT_S", DEFAULT_FETCH_PROVIDER_TIMEOUT_S
        ),
        # Blank or only-commas falls back to the default in ``__post_init__``.
        search_order=parse_order(os.environ.get("SEARCH_ORDER") or ""),
        fetch_order=parse_order(os.environ.get("FETCH_ORDER") or ""),
    )
