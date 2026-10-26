"""Meeting Recorder & Auto-Notes demo UI. Run: python -m streamlit run app.py"""
import datetime as dt
import json
import re
import shutil
import time
from pathlib import Path

import streamlit as st

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
