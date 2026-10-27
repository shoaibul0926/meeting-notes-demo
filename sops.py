"""Local SOP (Standard Operating Procedure) library -- title + Markdown body,
one folder per SOP under sops/, same folder-per-record convention as
meetings/<slug>/ (meta.json + a content file). No file upload: SOPs are
written and edited directly in the app, since they're short reference docs
the admin team maintains themselves, not something extracted from a source
file."""
import json
import re
import shutil
import time
from pathlib import Path

SOPS = Path(__file__).parent / "sops"


def _slug(title):
    return re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower() or "sop"


def _unique_slug(title):
    base = _slug(title)
    slug = base
    i = 2
    while (SOPS / slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


def list_sops():
    """[{slug, title, body, updated}, ...] sorted by title."""
    if not SOPS.exists():
        return []
    out = []
    for folder in SOPS.iterdir():
        meta_file = folder / "meta.json"
        if not folder.is_dir() or not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        body_file = folder / "sop.md"
        out.append({
            "slug": folder.name,
            "title": meta.get("title", folder.name),
            "body": body_file.read_text(encoding="utf-8") if body_file.exists() else "",
            "updated": meta.get("updated", ""),
        })
    return sorted(out, key=lambda s: s["title"].lower())


def save_sop(title, body, slug=None):
    """Creates a new SOP when slug is None, else updates the existing one in place."""
    SOPS.mkdir(exist_ok=True)
    if slug is None:
        slug = _unique_slug(title)
    folder = SOPS / slug
    folder.mkdir(exist_ok=True)
    (folder / "sop.md").write_text(body, encoding="utf-8")
    (folder / "meta.json").write_text(
        json.dumps({"title": title, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2),
        encoding="utf-8",
    )
    return slug


def delete_sop(slug):
    folder = (SOPS / slug).resolve()
    if SOPS.exists() and folder.is_relative_to(SOPS.resolve()) and folder.is_dir():
        shutil.rmtree(folder)
