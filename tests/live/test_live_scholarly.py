"""OpenAlex and Unpaywall keys work, and the candidate reader gets full text."""

from __future__ import annotations

import httpx
import pytest

from rai import candidates

from .conftest import need_env

pytestmark = pytest.mark.live

# An open-access article both services know well (Ioannidis 2005, PLOS Medicine).
DOI = "10.1371/journal.pmed.0020124"


def test_openalex_key_works():
    key = need_env("OPENALEX_API_KEY")
    resp = httpx.get(
        "https://api.openalex.org/rate-limit", params={"api_key": key}, timeout=30
    )
    assert resp.status_code == 200, f"OpenAlex rejected the key: {resp.text[:200]}"
    with candidates._client() as client:
        work = candidates.openalex(client, DOI)
    assert work and "false" in work["title"].lower()


def test_unpaywall_email_works():
    need_env("UNPAYWALL_EMAIL")
    with candidates._client() as client:
        urls = candidates.unpaywall(client, DOI)
    assert urls, "Unpaywall returned no open-access locations"


def test_tinyfish_key_is_set():
    """OpenCode's web search reads TINYFISH_API_KEY; `rai open` passes it on."""
    need_env("TINYFISH_API_KEY")


def test_read_candidate_gets_text(scratch_project):
    need_env("OPENALEX_API_KEY")
    result = candidates.read_candidate(scratch_project, DOI)
    assert result["metadata"].get("title"), "no metadata"
    assert result["read_depth"] != "abstract only", (
        "no free full text fetched; check network access to publisher sites"
    )
