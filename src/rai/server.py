"""The rai MCP server, started by OpenCode over stdio from the project folder."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:  # mcp >= 2 renamed FastMCP to MCPServer; the decorator API is the same.
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import candidates as cand
from . import ledger
from . import notes as notes_view
from .brief import build_brief
from .cards import build_card
from .cards import read_card as _read_card
from .claims import update_claim as _update_claim
from .config import Project, load_project
from .manuscript import DRAFT_SUFFIXES, build_manuscript
from .search import read_location as _read_location
from .search import search
from .store import Store
from .verify import verify_quote as _verify_quote

INSTRUCTIONS = """\
rai gives you this project's library, its internal notes and a ledger of what
the writer has decided. How the tools fit together:
- Maps point, sources prove. Cards (read_card) and the notes from read_notes
  tell you where to look. Anything you state as fact about a source must come
  from read_location, and every quotation must pass verify_quote first.
- Search passages with search_passages: it is this project's semantic + keyword
  search over the ingested library, and the only passage search here. Zotero's
  tools find items by metadata or read one known item; there is no Zotero
  semantic search in this setup. kind="card" finds what sources are
  about; the default searches their text. Pass sources=["key", ...] to stay in
  named works and per_source_limit=2 to stop one long book filling a
  library-wide search. Then read_location around the best hits before
  summarising. read_location(cite_key, "toc") gives a source's full heading
  outline, and read_notes("sources") lists the library inventory.
- Cite as [@cite_key, location], copied exactly from tool output.
- The writer talks to you in chat and never edits files. When they decide,
  dismiss or say they've read something, record it with the matching tool
  (resolve_objection, record_decision, set_reading_status, decide_candidate) and
  confirm in one line what you recorded.
- /wrap asks you to summarise the conversation: record it with record_session
  (what was discussed, new understandings, where you left off).
- refresh_card / refresh_chapter rebuild a stale note; they take minutes.
- read_candidate, read_candidate_section, save_candidate and save_candidates are
  for texts that are not in the library yet.
"""

log = logging.getLogger("rai.server")


def make_server(project: Project) -> FastMCP:
    mcp = FastMCP("rai", instructions=INSTRUCTIONS)

    def store() -> Store:
        return Store(project)

    @mcp.tool()
    def search_passages(
        query: str,
        kind: str = "text",
        limit: int = 8,
        sources: list[str] | None = None,
        per_source_limit: int = 0,
    ) -> list[dict]:
        """Semantic + keyword search over the project's ingested library.

        This is the passage search for the writer's sources: everything rai has
        ingested (the project's Zotero scope and sources-local/), not just
        Zotero metadata. The Zotero tools find items or read one known item;
        they do not provide a library passage search.

        kind="text" (default) searches passages; kind="card" searches source
        cards, which is the quick way to find which sources are about a topic.
        limit caps results (at most 20). sources restricts the search to those
        cite keys or numeric source ids (omit for the whole library); an unknown
        key returns [{"error": ...}]. per_source_limit caps hits per source
        (0 = no cap); use 2 for a library-wide sweep so one long book does not
        fill the results.

        Returns a ranked list of dicts with citation, cite_key, source_id,
        title, authors, year, location, first_block, heading_path and snippet.
        Snippets are cut to about 150 words, so call read_location around a hit
        before summarising or quoting. An empty list means nothing in the
        indexed library matches (or the project has not been ingested yet).
        """
        s = store()
        try:
            return search(
                s,
                query,
                kind=kind,
                limit=min(limit, 20),
                sources=sources,
                per_source_limit=per_source_limit,
            )
        except (ValueError, RuntimeError) as exc:
            return [{"error": str(exc)}]
        finally:
            s.close()

    @mcp.tool()
    def read_location(
        source: str, location: str, radius: int = 2, max_tokens: int = 8000
    ) -> dict:
        """Read the exact text around a location in a source.

        source: cite key (or numeric source_id). location: as given in search
        results, e.g. "p. 79", "pp. 79-81", "ch. 4 para 12", "paras 3-7",
        "seq:120", or a heading's words. Use location="toc" for the source's
        full heading outline with locations. radius widens by pages (PDF) or
        paragraphs.
        """
        s = store()
        try:
            return _read_location(s, source, location, radius, max_tokens)
        finally:
            s.close()

    @mcp.tool()
    def verify_quote(source: str, quote: str, location: str = "") -> dict:
        """Check a quotation against the source text.

        Returns status exact | near (with the real wording to use instead) |
        elsewhere (with the right location) | not_found (do not use it).
        """
        s = store()
        try:
            return _verify_quote(s, source, quote, location or None)
        finally:
            s.close()

    @mcp.tool()
    def read_card(cite_key: str, part: str = "both") -> str:
        """A source's general card and/or project note. part: general|project|both."""
        return _read_card(project, cite_key, part)

    @mcp.tool()
    def refresh_card(cite_key: str) -> str:
        """Rebuild one source's card and project note (takes minutes)."""
        s = store()
        try:
            source = s.source(cite_key)
            if source is None:
                return f"No source {cite_key!r}."
            # build_card ends by rebuilding the source's project note too.
            path = build_card(project, s, source, say=log.info)
            return f"Rebuilt {project.rel(path)}."
        finally:
            s.close()

    @mcp.tool()
    def refresh_chapter(path: str) -> str:
        """Rebuild the note and claims for one draft file, e.g. drafts/intro.md."""
        rel = ledger.draft_rel(project, path)
        target = (project.root / rel).resolve()
        if (
            not target.is_relative_to(project.drafts_dir.resolve())
            or target.suffix.lower() not in DRAFT_SUFFIXES
        ):
            return f"{path!r} is not a draft file under drafts/."
        if not target.exists():
            return f"No draft file {path!r}."
        done = build_manuscript(project, only=target, force=True, say=log.info)
        return f"Rebuilt {len(done)} chapter note(s) and the manuscript map."

    @mcp.tool()
    def update_claim(
        claim_id: str, status: str, evidence: list[dict[str, Any]]
    ) -> dict:
        """Record a claim check in the ledger.

        status: supported | partly supported | contradicted | unsupported |
        unchecked. evidence: list of {"citation": "[@key, p. 4]", "note": "..."}
        drawn from passages you have read.
        """
        s = store()
        try:
            return _update_claim(s, claim_id, status, evidence)
        finally:
            s.close()

    @mcp.tool()
    def read_notes(what: str, file: str = "", status: str = "", limit: int = 0) -> str:
        """Read one of the project's internal notes.

        what: brief | corpus_map | manuscript_map | chapter_note (with file=, a
        draft path) | claims | objections (optionally file=, and status= open,
        will_address, answered, dismissed or unresolved) | decisions | reading |
        sources | candidates | sessions (summaries of past conversations, newest
        first; limit= how many, default 5).
        """
        return notes_view.read_notes(project, what, file, status, limit)

    @mcp.tool()
    def raise_objections(file: str, objections: list[dict[str, str]]) -> list[dict]:
        """Register objections to a draft file and get an id (O1, O2...) for each.

        objections: a list of {"paras": "14-16", "summary": "one sentence"}.
        Present them to the writer with these ids.
        """
        return ledger.raise_objections(project, file, objections)

    @mcp.tool()
    def resolve_objection(objection_id: str, status: str, note: str = "") -> dict:
        """Record what the writer decided about an objection.

        status: dismissed (never raise it again), will_address (still open, they
        plan to fix it), answered (they've dealt with it in the draft), or open
        (reopen it).
        """
        return ledger.resolve_objection(project, objection_id, status, note)

    @mcp.tool()
    def record_decision(text: str, refs: str = "") -> dict:
        """Record something the writer explicitly decided, in their own terms."""
        result = ledger.record_decision(project, text, refs)
        if result.get("ok"):
            build_brief(project)
        return result

    @mcp.tool()
    def revoke_decision(decision_id: str) -> dict:
        """Withdraw a recorded decision (D1, D2...) at the writer's request."""
        result = ledger.revoke_decision(project, decision_id)
        if result.get("ok"):
            build_brief(project)
        return result

    @mcp.tool()
    def set_reading_status(cite_key: str, status: str, note: str = "") -> dict:
        """Record how much of a source the writer has read.

        status: not read | skimmed | read | studied.
        """
        s = store()
        try:
            source = s.source(cite_key)
        finally:
            s.close()
        if source is None:
            return {"error": f"No source {cite_key!r} in the library."}
        return ledger.set_reading_status(project, source["cite_key"], status, note)

    @mcp.tool()
    def record_session(
        discussed: str,
        understandings: list[str],
        left_off: str,
        agent: str = "",
        reports: list[str] | None = None,
        replaces: str = "",
    ) -> dict:
        """Record a summary of this conversation (what /wrap asks for).

        discussed: what was covered, in order. understandings: new understandings
        that arose, one sentence each (not decisions; empty if none). left_off: where
        you stopped and what's next. agent: your name. reports: files you saved in
        reports/. replaces: an earlier summary id (S1...) the writer corrected.
        The next conversation starts with this in its briefing.
        """
        result = ledger.record_session(
            project, discussed, understandings, left_off, agent, reports, replaces
        )
        if result.get("ok"):
            build_brief(project)
        return result

    @mcp.tool()
    def decide_candidate(candidate_id: str, decision: str, note: str = "") -> dict:
        """Record the writer's decision on a proposed work (C1, C2...).

        decision: shortlisted | dismissed | proposed (to undo).
        """
        result = ledger.decide_candidate(project, candidate_id, decision, note)
        if result.get("ok"):
            cand.render_reading_list(project)
        return result

    @mcp.tool()
    def read_candidate(doi_or_url: str, max_tokens: int = 0) -> dict:
        """Metadata plus the full open text of a candidate source, if it fits.

        Longer texts return a profile (abstract, table of contents with
        section sizes, opening and closing sections). Flags candidates already
        in the library.
        """
        return cand.read_candidate(project, doi_or_url, max_tokens or None)

    @mcp.tool()
    def read_candidate_section(candidate_id: str, heading: str) -> dict:
        """Read one section of a long candidate by (part of) its heading."""
        return cand.read_candidate_section(project, candidate_id, heading)

    @mcp.tool()
    def save_candidate(
        title: str,
        url_or_doi: str,
        serves: str,
        claim: str,
        evidence: str,
        standing: str,
        fit: str,
        read_depth: str,
        access: str,
        verdict: str,
    ) -> dict:
        """Propose an assessed candidate; it gets an id (C1, C2...) on the reading list.

        verdict: "read in full", "read sections: ...", or "skip", each with a
        one-line reason. Base it on what you actually read; say so in read_depth.
        """
        entry = {
            "title": title,
            "url_or_doi": url_or_doi,
            "serves": serves,
            "claim": claim,
            "evidence": evidence,
            "standing": standing,
            "fit": fit,
            "read_depth": read_depth,
            "access": access,
            "verdict": verdict,
        }
        return cand.save_candidate(project, entry)

    @mcp.tool()
    def save_candidates(candidates: list[dict]) -> dict:
        """Propose several assessed candidates in one call.

        Each entry needs the same ten fields as save_candidate. Returns one
        result per entry in input order, plus saved, duplicate and error counts.
        Record one work per entry; never bundle several works into one id.
        """
        return cand.save_candidates(project, candidates)

    return mcp


def serve(project_path: Path | None = None) -> None:
    project = load_project(project_path)
    project.ensure_dirs()
    logging.basicConfig(
        filename=project.logs_dir / "server.log",
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    make_server(project).run()
