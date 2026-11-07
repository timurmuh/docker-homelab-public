import json

import pytest

import eval.run as run
import eval.score as score
from eval.corpus.derive import canonicalise, accept, pick


def test_canonicalise_strips_tracking_and_trailing_slash():
    assert canonicalise("https://Example.com/a/?utm_source=x") == "https://example.com/a"
    assert canonicalise("https://example.com/") == "https://example.com"


def test_accept_rejects_private_and_tokenised_urls():
    assert not accept("http://192.168.1.1/admin")
    assert not accept("http://localhost:8080/")
    assert not accept("https://example.com/reset?token=abc123")
    assert not accept("https://example.com/photo.jpg")
    assert accept("https://blog.example.com/post")


def test_pick_respects_host_cap_and_bucket_targets():
    cands = {"a.com/1": 9, "a.com/2": 9, "a.com/3": 9, "a.com/4": 9, "b.com/1": 3, "c.com/1": 1}
    got = pick(cands, content=2, reach=1, per_host=3)
    assert len([r for r in got if r["bucket"] == "content"]) == 2
    assert len([r for r in got if r["bucket"] == "reach"]) == 1
    assert max(sum(1 for r in got if r["host"] == h) for h in {r["host"] for r in got}) <= 3


def test_load_done_reads_partial_jsonl(tmp_path):
    p = tmp_path / "exa.jsonl"
    p.write_text('{"url":"https://a/1","provider":"exa"}\n{"url":"https://a/2"}\n')
    assert run.load_done(p) == {("https://a/1", "exa")}


def test_row_from_502_keeps_the_reason_and_writes_no_text():
    row, text = run.row_from_response(
        "https://a/1", "exa", 502,
        {"success": False, "error": "exa: too short: 172 chars"}, 190.0)
    assert row["ok"] is False and row["error"] == "exa: too short: 172 chars"
    assert row["text_sha1"] is None and text is None


def test_row_from_200_stores_markdown_and_counts_words():
    row, text = run.row_from_response(
        "https://a/1", "exa", 200,
        {"success": True, "data": {"markdown": "alpha beta gamma"}}, 12.0)
    assert row["ok"] is True and row["words"] == 3 and text == "alpha beta gamma"


# -- the metric -------------------------------------------------------------


def test_normalise_drops_link_targets_keeps_anchor_text():
    assert score.normalise("[Docs](https://x.io/a) are good") == ["docs", "are", "good"]


def test_containment_is_one_for_identical_text():
    t = " ".join(f"w{i}" for i in range(50))
    assert score.containment(t, t)["recall"] == 1.0


def test_containment_ignores_extra_material_in_the_candidate():
    canon = " ".join(f"w{i}" for i in range(50))
    cand = canon + " " + " ".join(f"junk{i}" for i in range(50))
    got = score.containment(canon, cand)
    assert got["recall"] == 1.0 and got["precision"] < 0.6


def test_containment_detects_a_truncated_tail():
    paragraphs = [" ".join(f"p{p}w{i}" for i in range(20)) for p in range(5)]
    canon = "\n\n".join(paragraphs)
    cand = "\n\n".join(paragraphs[:3])
    got = score.containment(canon, cand)
    assert got["recall"] == pytest.approx(56 / 96)
    assert got["thin_paragraphs"] == 2


def test_gate_rejects_a_canon_that_is_only_chrome():
    canon = "Let us know your cookie preferences. Sign up. Log in."
    innertext = " ".join(f"w{i}" for i in range(300))
    got = score.gate(canon, innertext)
    assert got["ok"] is False and got["ratio"] < 0.5


def test_gate_rejects_a_consent_marker_even_when_the_ratio_passes():
    # 253 canon tokens against 460 page tokens is 0.55 exactly: only the marker
    # check can reject this, which is the case the gate exists for.
    canon = "Let us know your cookie preferences. " + " ".join(f"w{i}" for i in range(247))
    innertext = " ".join(f"w{i}" for i in range(460))
    got = score.gate(canon, innertext)
    assert round(got["ratio"], 2) == 0.55
    assert got["ok"] is False and got["reason"] == "consent_marker"


def test_reach_pass_needs_half_the_long_title_tokens_and_200_words():
    title = "sysinfo: a performance journey"
    body = " ".join(["sysinfo", "performance", "journey"] + ["x"] * 250)
    assert score.reach_pass(title, body) is True
    assert score.reach_pass(title, "sysinfo " + "x " * 30) is False


def test_classify_row_calls_a_200_error_page_a_failure():
    row = {"http": 200, "ok": True}
    assert score.classify_row(row, "Access denied. You do not have permission.") == "error_page"


def test_classify_row_separates_a_harness_budget_from_a_provider_failure():
    row = {"http": 502, "ok": False, "error": "crw: budget_truncated"}
    assert score.classify_row(row, None) == "harness_budget"


def test_usable_drops_a_row_whose_capture_redirected_elsewhere():
    entry = {"status": "redirected", "final_url": "https://other.example/x"}
    assert score.usable({"url": "https://short.example/y"}, entry) is False
    assert score.usable({"url": "https://short.example/y"}, {"status": "ok"}) is True


# -- the report -------------------------------------------------------------


def test_report_groups_by_bucket_and_split_and_states_denominators():
    corpus = [
        {"url": "https://a/1", "bucket": "content", "split": "derive"},
        {"url": "https://b/1", "bucket": "reach", "split": "validate"},
    ]
    canon = {"https://a/1": {"status": "ok", "text": "w0 w1 w2 w3 w4 w5", "title": "t"}}
    status = {"https://a/1": {"status": "ok"}, "https://b/1": {"status": "ok"}}
    rows = [
        {"url": "https://a/1", "provider": "exa", "http": 200, "ok": True,
         "chars": 20, "elapsed_ms": 100, "error": None, "text": "w0 w1 w2 w3 w4 w5"},
        {"url": "https://a/1", "provider": "crw", "http": 502, "ok": False,
         "chars": 0, "elapsed_ms": 200, "error": "crw: wall: just a moment"},
        {"url": "https://b/1", "provider": "exa", "http": 200, "ok": True,
         "chars": 900, "elapsed_ms": 50, "error": None, "text": "x " * 400},
    ]
    md = score.build_report(rows, canon, corpus, status)
    assert "## content" in md and "## reach" in md
    assert "derive" in md and "validate" in md
    assert "denominator" in md


def test_report_drops_a_redirected_capture_from_the_content_denominator():
    corpus = [{"url": "https://a/1", "bucket": "content", "split": "derive"}]
    canon = {"https://a/1": {"status": "redirected", "text": "w " * 300, "title": "t"}}
    status = {"https://a/1": {"status": "redirected"}}
    rows = [{"url": "https://a/1", "provider": "exa", "http": 200, "ok": True,
             "chars": 600, "elapsed_ms": 10, "error": None, "text": "w " * 300}]
    md = score.build_report(rows, canon, corpus, status)
    assert "denominator: 0 of 1" in md


def test_row_from_200_keeps_the_provider_title_and_source_url():
    row, _ = run.row_from_response(
        "https://a/1", "exa", 200,
        {"success": True, "data": {"markdown": "alpha beta",
                                   "metadata": {"title": "Real Title",
                                                "sourceURL": "https://a/1/final"}}}, 1.0)
    assert row["title"] == "Real Title"
    assert row["source_url"] == "https://a/1/final"


def test_taxonomy_names_a_wall_separately_from_a_status():
    corpus = [{"url": "https://a/1", "bucket": "content", "split": "derive"}]
    canon = {"https://a/1": {"status": "ok", "text": "w " * 300, "title": "t"}}
    status = {"https://a/1": {"status": "ok"}}
    rows = [
        {"url": "https://a/1", "provider": "jina", "http": 502, "ok": False,
         "chars": 0, "elapsed_ms": 1, "error": "jina: wall: just a moment"},
        {"url": "https://a/1", "provider": "crw", "http": 502, "ok": False,
         "chars": 0, "elapsed_ms": 1, "error": "crw: status 999"},
    ]
    md = score.build_report(rows, canon, corpus, status)
    assert "wall: just a moment" in md
    assert "wall: status 999" not in md


def test_n_counts_every_answer_so_failures_cannot_hide():
    corpus = [{"url": "https://a/1", "bucket": "content", "split": "derive"}]
    canon = {"https://a/1": {"status": "ok", "text": "w " * 300, "title": "t"}}
    status = {"https://a/1": {"status": "ok"}}
    rows = [
        {"url": "https://a/1", "provider": "crw", "http": 200, "ok": True,
         "chars": 600, "elapsed_ms": 10, "error": None, "text": "w " * 300},
        {"url": "https://a/1", "provider": "crw", "http": 502, "ok": False,
         "chars": 0, "elapsed_ms": 10, "error": "crw: wall: just a moment"},
    ]
    md = score.build_report(rows, canon, corpus, status)
    line = next(l for l in md.splitlines() if l.startswith("| crw |"))
    assert "| 2 |" in line and "0.50" in line


def test_status_json_is_the_truth_about_a_capture():
    corpus = [{"url": "https://a/1", "bucket": "content", "split": "derive"}]
    canon = {"https://a/1": {"status": "redirected", "text": "w " * 300, "title": "t"}}
    status = {"https://a/1": {"status": "ok"}}
    rows = [{"url": "https://a/1", "provider": "crw", "http": 200, "ok": True,
             "chars": 600, "elapsed_ms": 10, "error": None, "text": "w " * 300}]
    md = score.build_report(rows, canon, corpus, status)
    assert "denominator: 1 of 1" in md


def test_report_flags_a_canon_older_than_seven_days():
    corpus = [{"url": "https://a/1", "bucket": "content", "split": "derive"}]
    canon = {"https://a/1": {"status": "ok", "text": "w " * 300, "title": "t",
                             "captured_at": "2020-01-01T00:00:00Z"}}
    status = {"https://a/1": {"status": "ok"}}
    rows = [{"url": "https://a/1", "provider": "crw", "http": 200, "ok": True,
             "chars": 600, "elapsed_ms": 10, "error": None, "text": "w " * 300,
             "fetched_at": "2026-10-31T00:00:00Z"}]
    md = score.build_report(rows, canon, corpus, status)
    assert "stale" in md.lower()
