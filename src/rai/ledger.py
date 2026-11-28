"""Your decisions, recorded as you make them in conversation.

Objections you've dismissed, decisions you've stated, which sources you've read,
and which proposed works you've shortlisted or dropped. They are
appended as JSON lines under ``<project>/.rai/ledger/``, and the current state
of any record is its latest event. The ledger is the one part of ``.rai/`` that
git tracks: everything else there can be rebuilt, but a decision of yours can't.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import Project
from .store import Store, now

OBJECTION_STATUSES = ("open", "will_address", "answered", "dismissed")
READING_STATUSES = ("not read", "skimmed", "read", "studied")
CANDIDATE_DECISIONS = ("proposed", "shortlisted", "dismissed")
CANDIDATE_FIELDS = (
    "title",
    "serves",
    "claim",
    "evidence",
    "standing",
    "fit",
    "read_depth",
    "access",
    "verdict",
)


def _path(project: Project, stream: str) -> Path:
    return project.ledger_dir / f"{stream}.jsonl"


def _append(project: Project, stream: str, record: dict[str, Any]) -> dict[str, Any]:
    project.ledger_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": now(), **record}
    with _path(project, stream).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _events(project: Project, stream: str) -> list[dict[str, Any]]:
    path = _path(project, stream)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a damaged line shouldn't take the whole ledger down
    return out


def _next_id(events: list[dict[str, Any]], prefix: str) -> str:
    numbers = [
        int(str(e["id"])[len(prefix) :])
        for e in events
        if str(e.get("id", "")).startswith(prefix)
        and str(e["id"])[len(prefix) :].isdigit()
    ]
    return f"{prefix}{max(numbers, default=0) + 1}"


def draft_rel(project: Project, file: str) -> str:
    """A draft's project-relative path, from 'ch1.md', 'drafts/ch1.md' or absolute."""
    raw = Path(file)
    options = [
        raw if raw.is_absolute() else project.root / raw,
        project.drafts_dir / raw,
    ]
    for option in options:
        if option.exists():
            return project.rel(option)
    return file


def file_hash(project: Project, rel: str) -> str:
    path = project.root / rel
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# Objections ------------------------------------------------------------------
def raise_objections(
    project: Project, file: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Register objections to a draft file and give each a stable id (O1, O2...)."""
    rel = draft_rel(project, file)
    digest = file_hash(project, rel)
    events = _events(project, "objections")
    raised = []
    for item in items:
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        oid = _next_id(events, "O")
        record = _append(
            project,
            "objections",
            {
                "event": "raise",
                "id": oid,
                "file": rel,
                "paras": str(item.get("paras", "")).strip(),
                "summary": summary,
                "file_hash": digest,
            },
        )
        events.append(record)
        raised.append(
            {"id": oid, "file": rel, "paras": record["paras"], "summary": summary}
        )
    return raised


def objections_state(project: Project) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for e in _events(project, "objections"):
        if e.get("event") == "raise":
            state[e["id"]] = {
                "id": e["id"],
                "file": e["file"],
                "paras": e.get("paras", ""),
                "summary": e["summary"],
                "status": "open",
                "note": "",
                "raised": e["ts"][:10],
                "decided": "",
                "hash_when_decided": e.get("file_hash", ""),
            }
        elif e.get("event") == "resolve" and e.get("id") in state:
            state[e["id"]].update(
                status=e["status"],
                note=e.get("note", ""),
                decided=e["ts"][:10],
                hash_when_decided=e.get("file_hash", ""),
            )
    return state


def resolve_objection(
    project: Project, oid: str, status: str, note: str = ""
) -> dict[str, Any]:
    if status not in OBJECTION_STATUSES:
        return {"error": f"status must be one of {', '.join(OBJECTION_STATUSES)}"}
    state = objections_state(project)
    current = state.get(oid.strip().upper())
    if current is None:
        known = ", ".join(state) or "none yet"
        return {"error": f"No objection {oid!r}. Known: {known}."}
    _append(
        project,
        "objections",
        {
            "event": "resolve",
            "id": current["id"],
            "status": status,
            "note": note,
            "file_hash": file_hash(project, current["file"]),
        },
    )
    return {"ok": True, "id": current["id"], "status": status}


def list_objections(
    project: Project, file: str = "", status: str = ""
) -> list[dict[str, Any]]:
    """Objections, optionally for one file and one status.

    ``status`` may also be ``unresolved`` (open or will_address). Each carries
    ``draft_changed_since``: whether the draft differs from when it was raised
    or last decided, which is the cue to re-check an "answered" objection.
    """
    rel = draft_rel(project, file) if file else ""
    out = []
    for item in objections_state(project).values():
        if rel and item["file"] != rel:
            continue
        if status == "unresolved":
            if item["status"] not in ("open", "will_address"):
                continue
        elif status and item["status"] != status:
            continue
        current = file_hash(project, item["file"])
        out.append(
            {
                **item,
                "draft_changed_since": bool(current)
                and current != item["hash_when_decided"],
            }
        )
    return out


# Decisions -------------------------------------------------------------------
def record_decision(project: Project, text: str, refs: str = "") -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {"error": "A decision needs some text."}
    events = _events(project, "decisions")
    did = _next_id(events, "D")
    _append(
        project,
        "decisions",
        {"event": "decide", "id": did, "text": text, "refs": refs.strip()},
    )
    return {"ok": True, "id": did}


def revoke_decision(project: Project, did: str) -> dict[str, Any]:
    known = {d["id"] for d in list_decisions(project)}
    did = did.strip().upper()
    if did not in known:
        return {"error": f"No active decision {did!r}."}
    _append(project, "decisions", {"event": "revoke", "id": did})
    return {"ok": True, "id": did}


def list_decisions(project: Project) -> list[dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for e in _events(project, "decisions"):
        if e.get("event") == "decide":
            state[e["id"]] = {
                "id": e["id"],
                "text": e["text"],
                "refs": e.get("refs", ""),
                "date": e["ts"][:10],
            }
        elif e.get("event") == "revoke":
            state.pop(e.get("id", ""), None)
    return list(state.values())


# Reading status --------------------------------------------------------------
def set_reading_status(
    project: Project, cite_key: str, status: str, note: str = ""
) -> dict[str, Any]:
    if status not in READING_STATUSES:
        return {"error": f"status must be one of {', '.join(READING_STATUSES)}"}
    _append(
        project,
        "reading",
        {"cite_key": cite_key, "status": status, "note": note.strip()},
    )
    return {"ok": True, "cite_key": cite_key, "status": status}


def reading_statuses(project: Project) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for e in _events(project, "reading"):
        if e.get("event") == "rename":
            old, new = e.get("cite_key", ""), e.get("to", "")
            if old in state:
                if new in state:
                    state.pop(old)
                else:
                    state[new] = state.pop(old)
            continue
        state[e["cite_key"]] = {
            "status": e["status"],
            "note": e.get("note", ""),
            "date": e["ts"][:10],
        }
    return state


def rename_reading_status(project: Project, old: str, new: str) -> dict[str, Any]:
    """Follow a citation-key rename without leaving the old key in reading views."""
    if old not in reading_statuses(project):
        return {"ok": True, "moved": False}
    _append(project, "reading", {"event": "rename", "cite_key": old, "to": new})
    return {"ok": True, "moved": True}


# Proposed works ------------------------------------------------------
def norm_ref(ref: str) -> str:
    ref = ref.strip().lower()
    ref = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", ref)
    ref = re.sub(r"^doi:\s*", "", ref)
    return ref.rstrip("/")


def candidates(project: Project) -> list[dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for e in _events(project, "candidates"):
        if e.get("event") == "propose":
            state[e["id"]] = {
                **{k: e.get(k, "") for k in CANDIDATE_FIELDS},
                "id": e["id"],
                "ref": e["ref"],
                "status": "proposed",
                "note": "",
                "proposed": e["ts"][:10],
            }
        elif e.get("event") == "decide" and e.get("id") in state:
            state[e["id"]].update(
                status=e["decision"], note=e.get("note", ""), decided=e["ts"][:10]
            )
    return list(state.values())


def find_candidate(project: Project, ref: str) -> dict[str, Any] | None:
    wanted = norm_ref(ref)
    if not wanted:
        return None
    for item in candidates(project):
        if norm_ref(item["ref"]) == wanted:
            return item
    return None


def propose_candidate(project: Project, entry: dict[str, Any]) -> dict[str, Any]:
    ref = str(entry.get("url_or_doi", "")).strip()
    if not ref:
        return {"ok": False, "error": "A candidate needs a DOI or URL."}
    existing = find_candidate(project, ref)
    if existing:
        message = (
            f"{existing['id']} is already on the list ({existing['status']}); "
            "not added again."
        )
        if existing["status"] == "dismissed":
            message = f"{existing['id']} was dismissed earlier; not proposed again."
        return {
            "ok": False,
            "duplicate": True,
            "id": existing["id"],
            "status": existing["status"],
            "message": message,
        }
    cid = _next_id(_events(project, "candidates"), "C")
    _append(
        project,
        "candidates",
        {
            "event": "propose",
            "id": cid,
            "ref": ref,
            **{k: str(entry.get(k, "")).strip() for k in CANDIDATE_FIELDS},
        },
    )
    return {"ok": True, "id": cid, "status": "proposed"}


def decide_candidate(
    project: Project, cid: str, decision: str, note: str = ""
) -> dict[str, Any]:
    if decision not in CANDIDATE_DECISIONS:
        return {"error": f"decision must be one of {', '.join(CANDIDATE_DECISIONS)}"}
    cid = cid.strip().upper()
    if cid not in {c["id"] for c in candidates(project)}:
        return {"error": f"No candidate {cid!r}."}
    _append(
        project,
        "candidates",
        {"event": "decide", "id": cid, "decision": decision, "note": note.strip()},
    )
    return {"ok": True, "id": cid, "status": decision}


# Session summaries -----------------------------------------------------------
def record_session(
    project: Project,
    discussed: str,
    understandings: list[str],
    left_off: str,
    agent: str = "",
    reports: list[str] | None = None,
    replaces: str = "",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Store a summary of one conversation: what was discussed, what the writer and
    the agent came to understand, and where they stopped. ``replaces`` supersedes an
    earlier summary (when the writer corrects one)."""
    discussed, left_off = discussed.strip(), left_off.strip()
    if not (discussed or left_off):
        return {"error": "A summary needs what was discussed or where you left off."}
    known = {item["id"] for item in sessions(project)}
    replaces = replaces.strip().upper()
    if replaces and replaces not in known:
        return {"error": f"No session summary {replaces!r} to replace."}
    sid = _next_id(_events(project, "sessions"), "S")
    _append(
        project,
        "sessions",
        {
            "event": "summary",
            "id": sid,
            "agent": agent.strip(),
            "discussed": discussed,
            "understandings": [u.strip() for u in understandings if u and u.strip()],
            "left_off": left_off,
            "reports": [r.strip() for r in (reports or []) if r and r.strip()],
            "notes": [n.strip().upper() for n in (notes or []) if n and n.strip()],
            "replaces": replaces,
        },
    )
    return {"ok": True, "id": sid}


def sessions(project: Project) -> list[dict[str, Any]]:
    """Session summaries, newest first, without ones a later summary replaced."""
    state: dict[str, dict[str, Any]] = {}
    replaced: set[str] = set()
    for e in _events(project, "sessions"):
        if e.get("event") != "summary":
            continue
        state[e["id"]] = {
            "id": e["id"],
            "date": e["ts"][:10],
            "agent": e.get("agent", ""),
            "discussed": e.get("discussed", ""),
            "understandings": e.get("understandings", []),
            "left_off": e.get("left_off", ""),
            "reports": e.get("reports", []),
            "notes": e.get("notes", []),
        }
        if e.get("replaces"):
            replaced.add(e["replaces"])
    return [state[k] for k in reversed(list(state)) if k not in replaced]


# Working notes ---------------------------------------------------------------
NOTE_LINK_KEYS = ("notes", "decisions", "objections", "sessions", "questions")
NOTE_FIELDS = ("title", "abstract", "body", "sources", "evidence", "links")


def _strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalise_sources(sources: Any, evidence: list[dict[str, Any]]) -> list[str]:
    out = _strings(sources)
    for entry in evidence:
        key = str(entry.get("cite_key", "")).strip()
        if key and key not in out:
            out.append(key)
    return out


def _normalise_evidence(evidence: Any) -> list[dict[str, Any]]:
    if not evidence:
        return []
    if not isinstance(evidence, list):
        return []
    out = []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("cite_key", "")).strip()
        if not key:
            continue
        out.append(
            {
                "cite_key": key,
                "location": str(entry.get("location", "")).strip(),
                "quote": str(entry.get("quote", "")).strip(),
                "verified": str(entry.get("verified", "unverified")).strip()
                or "unverified",
            }
        )
    return out


def _normalise_links(links: Any, known_notes: set[str]) -> dict[str, list[str]]:
    if not links:
        return {}
    if not isinstance(links, dict):
        raise ValueError("links must be an object of lists.")
    out: dict[str, list[str]] = {}
    for key in NOTE_LINK_KEYS:
        values = [v.upper() for v in _strings(links.get(key))]
        if key == "notes":
            unknown = [v for v in values if v not in known_notes]
            if unknown:
                raise ValueError(
                    "No note " + ", ".join(repr(v) for v in unknown) + " to link."
                )
        if values:
            out[key] = values
    return out


def _verify_evidence(
    project: Project, evidence: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    """Check quotations against the library before they enter a note."""
    from .verify import verify_quote

    entries = _normalise_evidence(evidence)
    if not entries:
        return [], []
    warnings: list[str] = []
    store = Store(project)
    try:
        for entry in entries:
            key = entry["cite_key"]
            quote = entry["quote"]
            if not quote:
                continue
            source = store.source(key)
            if source is None:
                entry["verified"] = "unverified"
                warnings.append(
                    f"{key}: not in the library, so the quotation is unverified."
                )
                continue
            result = verify_quote(store, key, quote, entry["location"] or None)
            status = result.get("status")
            if status == "exact":
                entry["verified"] = "exact"
            elif status == "near":
                entry["quote"] = result.get("actual_wording") or quote
                entry["verified"] = "near"
            elif status == "elsewhere":
                # A fuzzy match elsewhere returns the source's actual wording;
                # keep it, or `rai check` will fail against the new location.
                if result.get("actual_wording"):
                    entry["quote"] = result["actual_wording"]
                entry["location"] = result.get("location") or entry["location"]
                entry["verified"] = "elsewhere"
            else:
                entry["quote"] = ""
                entry["verified"] = "unverified"
                warnings.append(
                    f"{key}: quotation not found at {entry['location'] or 'the source'}"
                    "; kept the location and dropped the quotation."
                )
    finally:
        store.close()
    return entries, warnings


def _renamed(value: str, renames: list[tuple[str, str]]) -> str:
    for old, new in renames:
        if value == old:
            value = new
    return value


def notes_state(project: Project) -> dict[str, dict[str, Any]]:
    """Every working note, with citation-key renames applied at read time."""
    state: dict[str, dict[str, Any]] = {}
    renames: list[tuple[str, str]] = []
    for event in _events(project, "notes"):
        kind = event.get("event")
        if kind == "rename":
            old, new = event.get("from", ""), event.get("to", "")
            if old and new:
                renames.append((old, new))
            continue
        note_id = str(event.get("id", "")).strip().upper()
        if not note_id:
            continue
        if kind == "create":
            state[note_id] = {
                "id": note_id,
                "title": str(event.get("title", "")).strip(),
                "abstract": str(event.get("abstract", "")).strip(),
                "body": str(event.get("body", "")).strip(),
                "sources": _strings(event.get("sources")),
                "evidence": _normalise_evidence(event.get("evidence")),
                "links": {
                    key: _strings(value)
                    for key, value in (event.get("links") or {}).items()
                },
                "status": "active",
                "created": event["ts"][:10],
                "revised": event["ts"][:10],
                "deleted_at": "",
                "delete_note": "",
            }
        elif kind == "revise" and note_id in state:
            state[note_id].update(
                title=str(event.get("title", "")).strip(),
                abstract=str(event.get("abstract", "")).strip(),
                body=str(event.get("body", "")).strip(),
                sources=_strings(event.get("sources")),
                evidence=_normalise_evidence(event.get("evidence")),
                links={
                    key: _strings(value)
                    for key, value in (event.get("links") or {}).items()
                },
                revised=event["ts"][:10],
            )
        elif kind == "delete" and note_id in state:
            state[note_id].update(
                status="deleted",
                deleted_at=event["ts"][:10],
                delete_note=str(event.get("note", "")).strip(),
            )
    for item in state.values():
        item["sources"] = list(
            dict.fromkeys(_renamed(key, renames) for key in item["sources"])
        )
        for entry in item["evidence"]:
            entry["cite_key"] = _renamed(entry["cite_key"], renames)
    return state


def note(project: Project, note_id: str) -> dict[str, Any] | None:
    return notes_state(project).get(str(note_id).strip().upper())


def list_notes(
    project: Project,
    status: str = "",
    include_deleted: bool = False,
    source: str = "",
) -> list[dict[str, Any]]:
    """Working notes, newest revision first, active by default."""
    status = status.strip().lower()
    if status and status not in {"active", "deleted", "all"}:
        return []
    items = list(notes_state(project).values())
    if status == "deleted":
        items = [n for n in items if n["status"] == "deleted"]
    elif status == "all" or include_deleted:
        pass
    else:
        items = [n for n in items if n["status"] == "active"]
    if source:
        items = [n for n in items if source in n["sources"]]
    items.sort(
        key=lambda n: (n["revised"], int(n["id"][1:]) if n["id"][1:].isdigit() else 0),
        reverse=True,
    )
    return items


def create_note(
    project: Project,
    title: str,
    abstract: str,
    body: str = "",
    sources: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    links: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Record one working note and give it a stable id (N1, N2...)."""
    title = str(title or "").strip()
    abstract = str(abstract or "").strip()
    if not title:
        return {"ok": False, "error": "A note needs a title."}
    if not abstract:
        return {
            "ok": False,
            "error": "A note needs an abstract: a one- or two-sentence gist.",
        }
    state = notes_state(project)
    try:
        clean_links = _normalise_links(links, set(state))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    clean_evidence, warnings = _verify_evidence(project, evidence)
    if len(abstract.split()) > 50:
        warnings.append(
            f"The abstract is {len(abstract.split())} words; keep it to about 50 "
            "so notes stay easy to survey."
        )
    note_id = _next_id(_events(project, "notes"), "N")
    _append(
        project,
        "notes",
        {
            "event": "create",
            "id": note_id,
            "title": title,
            "abstract": abstract,
            "body": str(body or "").strip(),
            "sources": _normalise_sources(sources, clean_evidence),
            "evidence": clean_evidence,
            "links": clean_links,
        },
    )
    return {"ok": True, "id": note_id, "warnings": warnings}


def revise_note(
    project: Project,
    note_id: str,
    title: str | None = None,
    abstract: str | None = None,
    body: str | None = None,
    sources: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    links: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Append a complete revised version of an active note, keeping history."""
    note_id = str(note_id or "").strip().upper()
    current = notes_state(project).get(note_id)
    if current is None:
        return {"ok": False, "error": f"No note {note_id!r}."}
    if current["status"] == "deleted":
        return {
            "ok": False,
            "error": (
                f"{note_id} is deleted and cannot be revised. Create a new note "
                "and link the old one."
            ),
        }
    new_title = current["title"] if title is None else str(title).strip()
    new_abstract = current["abstract"] if abstract is None else str(abstract).strip()
    if not new_title:
        return {"ok": False, "error": "A note needs a title."}
    if not new_abstract:
        return {"ok": False, "error": "A note needs an abstract."}
    state = notes_state(project)
    try:
        clean_links = (
            current["links"]
            if links is None
            else _normalise_links(links, set(state) - {note_id})
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if evidence is None:
        clean_evidence, warnings = current["evidence"], []
    else:
        clean_evidence, warnings = _verify_evidence(project, evidence)
    if len(new_abstract.split()) > 50:
        warnings.append(
            f"The abstract is {len(new_abstract.split())} words; keep it to about "
            "50 so notes stay easy to survey."
        )
    if sources is None:
        clean_sources = list(current["sources"])
        for entry in clean_evidence:
            if entry["cite_key"] not in clean_sources:
                clean_sources.append(entry["cite_key"])
    else:
        clean_sources = _normalise_sources(sources, clean_evidence)
    _append(
        project,
        "notes",
        {
            "event": "revise",
            "id": note_id,
            "title": new_title,
            "abstract": new_abstract,
            "body": current["body"] if body is None else str(body).strip(),
            "sources": clean_sources,
            "evidence": clean_evidence,
            "links": clean_links,
        },
    )
    return {"ok": True, "id": note_id, "warnings": warnings}


def delete_note(project: Project, note_id: str, note: str = "") -> dict[str, Any]:
    """Move a note to the archive. Deleted notes cannot be reactivated."""
    note_id = str(note_id or "").strip().upper()
    current = notes_state(project).get(note_id)
    if current is None:
        return {"ok": False, "error": f"No note {note_id!r}."}
    if current["status"] == "deleted":
        return {"ok": True, "id": note_id, "status": "deleted", "already": True}
    _append(
        project,
        "notes",
        {"event": "delete", "id": note_id, "note": str(note or "").strip()},
    )
    return {"ok": True, "id": note_id, "status": "deleted"}


def note_history(project: Project, note_id: str) -> list[dict[str, Any]]:
    """The create/revise/delete (and relevant rename) events for one note."""
    note_id = str(note_id or "").strip().upper()
    out = []
    previous: dict[str, Any] | None = None
    seen = False
    sources: set[str] = set()
    evidence_keys: set[str] = set()
    for event in _events(project, "notes"):
        kind = event.get("event")
        if kind == "rename":
            old, new = str(event.get("from", "")), str(event.get("to", ""))
            if not seen or not old or not new:
                continue
            changed = []
            if old in sources:
                changed.append("sources")
                sources.discard(old)
                sources.add(new)
            if old in evidence_keys:
                changed.append("evidence")
                evidence_keys.discard(old)
                evidence_keys.add(new)
            if changed:
                out.append(
                    {
                        "event": "rename",
                        "ts": event.get("ts", ""),
                        "from": old,
                        "to": new,
                        "changed": changed,
                    }
                )
            continue
        if str(event.get("id", "")).strip().upper() != note_id:
            continue
        seen = True
        if kind in ("create", "revise"):
            sources = {str(k) for k in event.get("sources", []) if str(k).strip()}
            evidence_keys = {
                str(entry.get("cite_key", ""))
                for entry in event.get("evidence", [])
                if isinstance(entry, dict) and str(entry.get("cite_key", "")).strip()
            }
        changed = []
        if kind == "create":
            changed = ["created"]
        elif kind == "revise" and previous is not None:
            changed = [
                field
                for field in NOTE_FIELDS
                if event.get(field) != previous.get(field)
            ]
        out.append(
            {
                "event": kind or "",
                "ts": event.get("ts", ""),
                "title": event.get("title", ""),
                "abstract": event.get("abstract", ""),
                "note": event.get("note", ""),
                "changed": changed,
            }
        )
        previous = event
    return out


def rename_note_sources(project: Project, old: str, new: str) -> dict[str, Any]:
    """Follow a citation-key rename in working notes without revising them."""
    old, new = str(old).strip(), str(new).strip()
    if not old or not new or old == new:
        return {"ok": True, "moved": False}
    used = any(
        old in item["sources"]
        or any(entry["cite_key"] == old for entry in item["evidence"])
        for item in notes_state(project).values()
    )
    if not used:
        return {"ok": True, "moved": False}
    _append(project, "notes", {"event": "rename", "from": old, "to": new})
    return {"ok": True, "moved": True}
