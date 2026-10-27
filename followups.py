"""Aggregates action items from every meeting's notes.md into one cross-meeting
follow-up list. notes.md itself is regenerable output (regenerating a meeting's
notes rewrites it from scratch), so it's the wrong place to store which items
are done -- that state lives separately, in meetings/_followups.json, keyed by
a stable id derived from the meeting folder + item text."""
import hashlib
import json
import re
from pathlib import Path

MEETINGS = Path(__file__).parent / "meetings"
STATE_PATH = MEETINGS / "_followups.json"

ACTION_ITEM_RE = re.compile(r"^-\s*\[([ xX])\]\s*(.+)$")
EMPTY_VALUES = {"none", "no action items", ""}

# Best-effort trailing due-date hint, e.g. "- [ ] Prepare budget - Priya - Friday"
# or "- [ ] Send minutes - 2026-11-03". Never fails -- just returns None when the
# item has no parseable date, which is most of the time for freeform speech.
DUE_DATE_RE = re.compile(
    r"\s*-\s*(\d{4}-\d{2}-\d{2}|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tomorrow|today|end of \w+|next week)\s*$",
    re.IGNORECASE,
)


def _load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state):
    MEETINGS.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _item_id(folder_name, text):
    return hashlib.sha1(f"{folder_name}::{text}".encode("utf-8")).hexdigest()[:12]


def _extract_action_items(notes_text):
    """Raw item strings from the '## Action items' section of one notes.md."""
    in_section = False
    items = []
    for line in notes_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip().lower() == "action items"
            continue
        if in_section:
            m = ACTION_ITEM_RE.match(stripped)
            if m:
                items.append(m.group(2).strip())
    return items


def _parse_due(text):
    """Returns (due, display_text) -- display_text has the trailing '- <date>'
    stripped off when a due date was found, so it isn't shown twice (once
    inline, once as the separate due badge)."""
    m = DUE_DATE_RE.search(text)
    if not m:
        return None, text
    return m.group(1), text[:m.start()].rstrip()


def list_followups():
    """[{id, text, done, meeting_title, meeting_date, meeting_folder, due}, ...]
    across every meeting that has notes, newest meeting first."""
    state = _load_state()
    out = []
    if not MEETINGS.exists():
        return out
    for folder in sorted((p for p in MEETINGS.iterdir() if p.is_dir()), reverse=True):
        notes_file = folder / "notes.md"
        if not notes_file.exists():
            continue
        meta_file = folder / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        for text in _extract_action_items(notes_file.read_text(encoding="utf-8")):
            if text.lower().strip(".") in EMPTY_VALUES:
                continue
            item_id = _item_id(folder.name, text)
            due, display_text = _parse_due(text)
            out.append({
                "id": item_id,
                "text": display_text,
                "done": bool(state.get(item_id, {}).get("done")),
                "meeting_title": meta.get("title", folder.name),
                "meeting_date": meta.get("started", ""),
                "meeting_folder": folder.name,
                "due": due,
            })
    return out


def set_done(item_id, done):
    state = _load_state()
    state.setdefault(item_id, {})["done"] = bool(done)
    _save_state(state)
