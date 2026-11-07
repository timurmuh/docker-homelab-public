import pytest

from app.botwall import MIN_CONTENT_CHARS, SIGNATURES, looks_blocked


@pytest.mark.parametrize("sig", [s for s in SIGNATURES if s != "cloudflare"])
def test_every_phrase_signature_is_detected_alone(sig):
    # The brand token is excluded on purpose: since plan C it is only a wall
    # when a co-occurring marker is present (see the compound-rule tests below).
    assert looks_blocked(f"prefix text {sig.upper()} suffix text") == sig


@pytest.mark.parametrize("marker", ["ray id", "cf-chl", "attention required"])
def test_the_brand_token_is_detected_alongside_a_marker(marker):
    assert looks_blocked(f"Cloudflare {marker}") == "cloudflare"


def test_clean_page_is_not_blocked():
    text = "SearXNG is a free internet metasearch engine. " * 20
    assert looks_blocked(text) is None


def test_long_article_discussing_captchas_is_not_blocked():
    text = ("This article explains how captcha systems work and why they are "
            "annoying. " * 400)
    assert looks_blocked(text) is None


def test_short_wall_is_blocked():
    assert looks_blocked("Just a moment...") is not None


def test_wall_below_the_threshold_is_blocked():
    assert looks_blocked("Attention Required! Cloudflare " * 20) is not None


def test_banner_above_the_threshold_is_not_blocked():
    text = "We value your privacy. " + "real article content " * 300
    assert looks_blocked(text) is None


def test_signature_in_a_long_page_is_not_blocked():
    text = ("you've been blocked by network security " * 100)
    assert len(text) > 1000
    assert looks_blocked(text) is None


def test_min_content_chars_is_200():
    assert MIN_CONTENT_CHARS == 200


# -- the compound Cloudflare rule (plan C, follow-up item 4) -----------------


def test_cloudflare_alone_no_longer_rejects():
    # A short page that merely mentions the brand is not a challenge page; this
    # is the false positive the eval's wall sample made undeniable.
    text = "A short note about Cloudflare's outage yesterday. " * 3
    assert len(text) < 1000
    assert looks_blocked(text) is None


def test_cloudflare_with_ray_id_still_rejects():
    assert looks_blocked("Cloudflare Ray ID: 8f3a2b1c Check your connection.") == "cloudflare"


def test_cloudflare_with_checking_your_browser_reports_the_phrase():
    # The phrase-level signature wins over the brand token: position only
    # decides which string is returned, never whether the page is rejected.
    assert looks_blocked("Cloudflare Checking your browser ...") == "checking your browser"


def test_cloudflare_with_cf_chl_marker_rejects():
    assert looks_blocked("cf-chl-opt cloudflare interstitial") == "cloudflare"


@pytest.mark.parametrize(
    "length, expected",
    [(199, "cloudflare"), (200, "cloudflare"), (999, "cloudflare"), (1000, None), (1001, None)],
)
def test_the_length_boundary_is_exact(length, expected):
    # Pins the 1,000-character cap from both sides: the same text that is a
    # wall at 999 characters is content at 1,000.
    base = "cloudflare ray id "
    text = (base + "x" * length)[:length]
    assert looks_blocked(text) == expected


# -- signatures the wall sample justified ------------------------------------


def test_the_block_phrase_matches_without_the_youve_been_prefix():
    # Live capture: Jina returned 258 characters reading "Blocked by network
    # security" -- over MIN_CONTENT_CHARS, so it was handed to the model as
    # content. The old signature required "you've been".
    text = "[Sign In](https://www.example.com/register) # Blocked by network security"
    assert looks_blocked(text) == "blocked by network security"


def test_a_site_error_interstitial_is_the_known_over_cap_gap():
    # A real 1,112-character error page was served as content. It cannot be
    # caught by a text signature at all -- looks_blocked refuses to scan at or
    # above WALL_MAX_CHARS -- so this is the over-cap class (design item 5),
    # recorded rather than papered over with a signature that can never fire.
    text = "# We're sorry. There seems to be a problem with our website." * 20
    assert len(text) >= 1000
    assert looks_blocked(text) is None


def test_a_login_link_inside_a_real_post_is_not_a_wall():
    # The evidence that killed a generic login signature: real social posts
    # carry "[Log in]" in their chrome and are content.
    text = "# example (@user) on X ## Post [Log in](/i/jf/onboarding/web) " + "post body " * 200
    assert looks_blocked(text) is None
