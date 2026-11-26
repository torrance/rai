"""Your decisions, recorded as you make them in conversation.

Objections you've dismissed, decisions you've stated, which sources you've read,
and which proposed works you've shortlisted or dropped. They are
appended as JSON lines under ``<project>/.nfh/ledger/``, and the current state
of any record is its latest event. The ledger is the one part of ``.nfh/`` that
git tracks: everything else there can be rebuilt, but a decision of yours can't.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import Project
from .store import now

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
        }
        if e.get("replaces"):
            replaced.add(e["replaces"])
    return [state[k] for k in reversed(list(state)) if k not in replaced]
