"""Zotero's local API, storage, citation keys and the project's scope."""

from __future__ import annotations

from collections import Counter

import pytest

from nfh.zotero import Zotero

from .conftest import advise

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def zotero(project):
    z = Zotero(project.zotero)
    if not z.ping():
        pytest.fail(
            "Zotero's local API isn't answering. Open Zotero and enable Settings > "
            "Advanced > 'Allow other applications on this computer to communicate "
            "with Zotero'."
        )
    return z


@pytest.fixture(scope="module")
def scoped_items(zotero, live_project):
    if live_project is None:
        pytest.skip("pass --live-project to check a real project's Zotero scope")
    z = live_project.zotero
    if not z.collection and not z.tags:
        pytest.fail("the project's nfh.toml has no [zotero] collection or tags")
    return zotero.items(collection=z.collection, tags=z.tags, tag_match=z.tag_match)


def test_storage_folder_exists(project):
    assert project.zotero.storage_dir.exists(), (
        f"{project.zotero.storage_dir} not found; set [zotero] data_dir in nfh.toml"
    )


def test_library_is_readable(zotero):
    page = zotero._get("/items/top", [("limit", "5")])
    assert isinstance(page, list)


def test_scope_is_not_empty(scoped_items):
    assert scoped_items, "the project's Zotero scope matches no items"


def test_scoped_items_have_files_on_disk(scoped_items):
    missing = [f"{i.cite_key} ({i.file_note})" for i in scoped_items if not i.file]
    if missing:
        advise("items that won't be ingested: " + ", ".join(missing))
    assert any(i.file for i in scoped_items), "no item in scope has a usable file"


def test_citation_keys_are_in_zotero_and_unique(scoped_items):
    """Keys should live in Zotero's own Citation Key field, filled by Better BibTeX.

    Zotero stores and syncs that field but doesn't generate keys itself.
    """
    by_source = Counter(i.key_source for i in scoped_items)
    if by_source["generated"]:
        missing = [i.cite_key for i in scoped_items if i.key_source == "generated"]
        advise(
            f"{len(missing)} item(s) have no citation key in Zotero, so nfh made "
            f"temporary ones ({', '.join(missing)}). Install Better BibTeX, select "
            "them, and choose Better BibTeX > Refresh BibTeX key."
        )
    if by_source["extra"]:
        advise(
            f"{by_source['extra']} key(s) are still in the Extra field (pre-Zotero 8); "
            "Better BibTeX's migration moves them into the Citation Key field"
        )
    if by_source["placeholder"]:
        placeholders = [
            i.cite_key for i in scoped_items if i.key_source == "placeholder"
        ]
        advise(
            f"{len(placeholders)} item(s) use placeholder citation keys "
            f"({', '.join(placeholders)}). Select them in Zotero and choose "
            "Better BibTeX > Refresh BibTeX key."
        )
    dupes = [k for k, n in Counter(i.cite_key for i in scoped_items).items() if n > 1]
    assert not dupes, f"duplicate citation keys in scope: {dupes}"


def test_tag_matching_modes_are_consistent(zotero, live_project):
    if live_project is None or len(live_project.zotero.tags) < 2:
        pytest.skip("needs a live project with two or more tags")
    tags = live_project.zotero.tags
    any_keys = {i.key for i in zotero.items(tags=tags, tag_match="any")}
    all_keys = {i.key for i in zotero.items(tags=tags, tag_match="all")}
    assert all_keys <= any_keys


def test_references_bib_covers_the_scope(live_project, scoped_items):
    bib = live_project.root / "references.bib"
    if not bib.exists():
        pytest.fail(
            "references.bib missing; set up a Better BibTeX auto-export (README)"
        )
    text = bib.read_text(encoding="utf-8")
    absent = [i.cite_key for i in scoped_items if "{" + i.cite_key + "," not in text]
    assert not absent, f"keys in scope but not in references.bib: {absent}"
