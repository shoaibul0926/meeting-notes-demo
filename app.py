"""Meeting Recorder & Auto-Notes demo UI. Run: python -m streamlit run app.py"""
import datetime as dt
import json
import re
import shutil
import time
from pathlib import Path

import streamlit as st

import followups
import gdocs
import sops
from notes import LiveProcessor, OLLAMA_MODEL, finalize_notes, ollama_available, process_meeting
from recorder import MeetingRecorder

MEETINGS = Path(__file__).parent / "meetings"
MEETINGS.mkdir(exist_ok=True)
HAS_FFMPEG = shutil.which("ffmpeg") is not None

st.set_page_config(page_title="Meeting Recorder", layout="wide")
ss = st.session_state
ss.setdefault("rec", None)
ss.setdefault("processor", None)
ss.setdefault("selected", None)


def list_meetings():
    return sorted((p for p in MEETINGS.iterdir() if (p / "meta.json").exists()), reverse=True)


def load_meta(folder):
    return json.loads((folder / "meta.json").read_text())


with st.sidebar:
    view = st.radio("View", ["Meetings", "Follow-ups", "SOPs"], label_visibility="collapsed")
    if ss.rec is not None and view != "Meetings":
        st.warning(f"Recording '{ss.rec.title}' is still running in the background.")
    st.divider()
    if view == "Meetings":
        st.header("Setup")
        st.write("Audio: mic + system sound")
        st.write("Screen recording: " + ("ready" if HAS_FFMPEG else "install ffmpeg to enable"))
        st.write("Notes AI: " + (f"Ollama - {OLLAMA_MODEL}" if ollama_available()
                                 else "basic (start Ollama for better notes)"))
        st.divider()
        st.header("Past meetings")
        past = list_meetings()
        if past:
            labels = {p: f"{load_meta(p)['started']} - {load_meta(p)['title']}" for p in past}
            pick = st.radio("Open", past, format_func=labels.get, label_visibility="collapsed",
                            index=past.index(ss.selected) if ss.selected in past else 0)
            if ss.rec is None:
                ss.selected = pick
        else:
            st.caption("Nothing recorded yet.")


def render_followups():
    st.title("Follow-ups")
    st.caption("Action items pulled automatically from every meeting's notes — mark them done as they're completed.")
    items = followups.list_followups()
    if not items:
        st.info('No action items yet — they show up here once a recorded meeting\'s notes '
                'include an "Action items" section.')
        return
    open_items = sorted((i for i in items if not i["done"]), key=lambda i: (i["due"] is None, i["due"] or ""))
    done_items = [i for i in items if i["done"]]

    st.subheader(f"Open ({len(open_items)})")
    if not open_items:
        st.caption("Nothing open right now.")
    for item in open_items:
        _followup_row(item)

    if done_items:
        with st.expander(f"Done ({len(done_items)})"):
            for item in done_items:
                _followup_row(item)


def _followup_row(item):
    c1, c2 = st.columns([0.06, 0.94])
    checked = c1.checkbox("Done", value=item["done"], key=f"fu-{item['id']}", label_visibility="collapsed")
    if checked != item["done"]:
        followups.set_done(item["id"], checked)
        st.rerun()
    due = f" — due {item['due']}" if item["due"] else ""
    c2.markdown(f"{item['text']}{due}  \n:gray[from *{item['meeting_title']}*, {item['meeting_date']}]")


def render_sops():
    st.title("SOPs")
    st.caption("Standard Operating Procedures — write once, keep them up to date, reuse across meetings.")

    with st.expander("+ New SOP"):
        with st.form("new-sop", clear_on_submit=True):
            title = st.text_input("Title")
            body = st.text_area("Content (Markdown)", height=200)
            if st.form_submit_button("Save SOP") and title.strip():
                sops.save_sop(title.strip(), body)
                st.rerun()

    all_sops = sops.list_sops()
    if not all_sops:
        st.info("No SOPs yet — add the first one above.")
        return

    for sop in all_sops:
        with st.expander(f"{sop['title']} — updated {sop['updated']}"):
            with st.form(f"edit-sop-{sop['slug']}"):
                new_title = st.text_input("Title", value=sop["title"], key=f"title-{sop['slug']}")
                new_body = st.text_area("Content (Markdown)", value=sop["body"], height=200, key=f"body-{sop['slug']}")
                c1, c2 = st.columns([1, 1])
                with c1:
                    save = st.form_submit_button("Save changes")
                with c2:
                    delete = st.form_submit_button("Delete")
            if save and new_title.strip():
                sops.save_sop(new_title.strip(), new_body, slug=sop["slug"])
                st.rerun()
            if delete:
                sops.delete_sop(sop["slug"])
                st.rerun()


if view == "Follow-ups":
    render_followups()
    st.stop()
if view == "SOPs":
    render_sops()
    st.stop()

st.title("Meeting Recorder & Auto-Notes")
st.caption("Records your Google Meet / Zoom call on this computer, then writes the notes automatically. 100% local and free.")

if ss.rec is None:
    c1, c2 = st.columns([3, 2])
    title = c1.text_input("Meeting title", "Team sync")
    screen = c2.checkbox("Also record screen", value=HAS_FFMPEG, disabled=not HAS_FFMPEG,
                         help="Needs ffmpeg installed")
    st.info("Tell everyone in the call that it's being recorded before you start.")
    if st.button("Start recording", type="primary", use_container_width=True):
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower() or "meeting"
        folder = MEETINGS / f"{dt.datetime.now():%Y-%m-%d_%H-%M-%S}_{slug}"
        rec = MeetingRecorder(folder, title=title, record_screen=screen)
        try:
            rec.start()
            ss.rec = rec
            processor = LiveProcessor(rec)
            processor.start()
            ss.processor = processor
            st.rerun()
        except Exception as exc:
            st.error(f"Couldn't start recording: {exc}")
else:
    @st.fragment(run_every=1)
    def timer():
        elapsed = int(time.time() - ss.rec.started_at)
        st.error(f"RECORDING '{ss.rec.title}' - {elapsed // 60:02d}:{elapsed % 60:02d}")
    timer()
    st.caption("Notes are being transcribed and drafted in the background as you talk.")
    for w in ss.rec.warnings:
        st.warning(w)
    if ss.processor.error:
        st.warning(f"Background notes processing hit an issue: {ss.processor.error}")
    if st.button("Stop & generate notes", type="primary", use_container_width=True):
        rec, ss.rec = ss.rec, None
        processor, ss.processor = ss.processor, None
        with st.status("Finishing up...", expanded=True) as status:
            status.write("Finishing background transcription...")
            segments, extracted = processor.finish()
            status.write("Saving recording...")
            rec.stop()
            finalize_notes(rec.out_dir, segments, extracted,
                           language=processor.language or "unknown", progress=status.write)
            status.update(label="Notes ready", state="complete")
        ss.selected = rec.out_dir
        st.rerun()

folder = ss.selected
if ss.rec is None and folder and Path(folder).exists():
    folder = Path(folder)
    meta = load_meta(folder)
    st.divider()
    st.subheader(meta["title"])
    for w in meta.get("warnings", []):
        st.warning(w)
    left, right = st.columns([3, 2])
    with left:
        notes_file = folder / "notes.md"
        if notes_file.exists():
            notes = notes_file.read_text(encoding="utf-8")
            st.markdown(notes)
            st.download_button("Download notes (.md)", notes, file_name=f"{folder.name}_notes.md")
            if gdocs.available():
                if st.button("Push to Google Docs"):
                    with st.spinner("Creating Google Doc (sign in if a browser tab opens)..."):
                        try:
                            url = gdocs.push_notes(meta["title"], notes)
                            st.success(f"Created: {url}")
                        except Exception as exc:
                            st.error(f"Couldn't push to Google Docs: {exc}")
            else:
                st.caption("Add client_secret.json to enable pushing notes to Google Docs.")
        else:
            st.info("No notes yet for this meeting.")
        if st.button("Regenerate notes"):
            with st.status("Regenerating...", expanded=True) as status:
                process_meeting(folder, progress=status.write)
            st.rerun()
    with right:
        if meta.get("video") and (folder / meta["video"]).exists():
            st.video(str(folder / meta["video"]))
        elif (folder / "audio.wav").exists():
            st.audio(str(folder / "audio.wav"))
        transcript = folder / "transcript.txt"
        if transcript.exists():
            with st.expander("Full transcript"):
                st.text(transcript.read_text(encoding="utf-8"))
        st.caption(f"Files saved in: {folder}")
