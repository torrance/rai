"""The whole pipeline on a small synthetic source, with the real models."""

from __future__ import annotations

import pytest

from rai import cards, corpus_map, manuscript
from rai.brief import build_brief
from rai.check import check_quotes
from rai.ingest import ingest_local
from rai.search import read_location, search
from rai.store import Store
from rai.verify import verify_quote

pytestmark = [pytest.mark.live, pytest.mark.slow]

SOURCE = """# Ratings and Discipline on Delivery Platforms

## Introduction

Delivery platforms present themselves as neutral marketplaces that connect
customers, restaurants and independent couriers.

This study argues that customer ratings function as a disciplinary system that
replaces the supervisor without removing supervision.

## Method

We interviewed forty-two couriers in three cities between 2021 and 2023 and
analysed the platforms' published policies on deactivation.

## Findings

Couriers described ratings as a constant, invisible audit. Several reported
declining orders less often after a single poor rating.

Deactivation thresholds were rarely disclosed, which made the rules feel
arbitrary and encouraged couriers to over-comply.

Flexibility, the platforms' central promise, proved largely illusory for
couriers who depended on the work for most of their income.

## Conclusion

Ratings do not abolish management; they distribute it to customers and encode
it in software, while leaving couriers to bear its risks.
"""

QUOTE = "replaces the supervisor without removing supervision"


@pytest.fixture(scope="module")
def indexed(scratch_project):
    p = scratch_project
    p.project_md.write_text(
        "# Scratch\n\n## Thesis\nRatings are management by other means.\n\n"
        "## Open questions\n- How do ratings shape couriers' behaviour?\n"
        "- Is platform flexibility real?\n"
    )
    (p.sources_local_dir / "ratings.md").write_text(SOURCE)
    (p.sources_local_dir / "ratings.md.yaml").write_text(
        "title: Ratings and Discipline\nauthors: [Test Author]\nyear: 2024\n"
        "cite_key: test2024ratings\n"
    )
    report = ingest_local(p, say=print)
    assert report.added == ["test2024ratings"], report
    return p


def test_search_read_verify_with_real_embeddings(indexed):
    store = Store(indexed)
    hits = search(store, "do customer ratings act like a supervisor?", limit=3)
    assert hits and hits[0]["cite_key"] == "test2024ratings"
    read = read_location(store, "test2024ratings", hits[0]["location"], radius=1)
    assert "error" not in read
    assert verify_quote(store, "test2024ratings", QUOTE)["status"] == "exact"
    store.close()


def test_card_is_built_and_checked(indexed):
    store = Store(indexed)
    path = cards.build_card(indexed, store, store.source("test2024ratings"), say=print)
    meta, body = cards.read_frontmatter(path)
    assert "## Thesis" in body and len(body.split("## Thesis")[1].strip()) > 40
    ok = int(meta["checks"].split()[0])
    assert ok > 0, f"no entry survived the location checks: {meta['checks']}"
    note = cards.read_frontmatter(cards.project_note_path(indexed, "test2024ratings"))
    assert note[0]["cite_key"] == "test2024ratings"
    assert "Q" in note[1], "no relevance to the open questions"
    bad = [r for r in check_quotes(indexed) if r.status == "not_found"]
    assert not bad, f"card contains unverifiable quotes: {bad}"
    store.close()


def test_corpus_map(indexed):
    store = Store(indexed)
    if not cards.card_path(indexed, "test2024ratings").exists():
        cards.build_card(indexed, store, store.source("test2024ratings"), say=print)
    store.close()
    text = corpus_map.build_map(indexed, say=print)
    for heading in ("## Overview", "## Themes", "## Coverage", "## Gaps"):
        assert heading in text


def test_manuscript_claims_and_brief(indexed):
    draft = indexed.drafts_dir / "01-intro.md"
    draft.write_text(
        "# Introduction\n\nCustomer ratings let platforms manage couriers without "
        "managers [@test2024ratings, para 2].\n\nThis essay argues that the "
        "flexibility couriers are promised is mostly a story.\n"
    )
    # The agent tests share this scratch project and leave their own drafts
    # behind; map only this test's file.
    done = manuscript.build_manuscript(indexed, only=draft, force=True, say=print)
    assert len(done) == 1
    store = Store(indexed)
    claims = list(store.conn.execute("SELECT * FROM claims WHERE active=1"))
    store.close()
    assert claims, "no claims extracted from the draft"
    assert any(c["cited"] for c in claims), "citation not attached to a claim"
    brief = build_brief(indexed)
    assert "drafts/01-intro.md" in brief and "Ratings are management" in brief
