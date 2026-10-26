"""Transcribes with faster-whisper and writes notes with Ollama (or a simple fallback)."""
import json
import os
import re
import threading
import urllib.request
from collections import Counter
from pathlib import Path

from recorder import SAMPLE_RATE, write_wav

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
_whisper = {}


def transcribe(audio_path, model_size=WHISPER_MODEL):
    from faster_whisper import WhisperModel
    if model_size not in _whisper:
        _whisper[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = _whisper[model_size].transcribe(str(audio_path), vad_filter=True, beam_size=1)
    segs = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments if s.text.strip()]
    return segs, info.language


def _ts(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_transcript(segs):
    return "\n".join(f"[{_ts(s['start'])}] {s['text']}" for s in segs)


def ollama_available():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return any(m.split(":")[0] == OLLAMA_MODEL.split(":")[0] for m in models)
    except Exception:
        return False


def _ollama(prompt):
    body = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.2}}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())["response"].strip()


NOTES_PROMPT = """You are a meeting assistant. Using ONLY the material below, write meeting notes in Markdown with exactly these sections:

## Summary
3-5 sentences.
## Key discussion points
Bullets.
## Decisions
Bullets.
## Action items
Lines like "- [ ] task - owner - due date". Use "unassigned" / "no date" when unclear.
## Open questions
Bullets.

Write "None" under any empty section. Write in English. Do not invent facts.

MATERIAL:
{material}
"""

CHUNK_PROMPT = """Extract from this part of a meeting transcript: key points, decisions, action items (with owner and deadline if mentioned) and open questions. Short bullets only, English.

TRANSCRIPT PART:
{text}
"""


def _chunks(text, size=8000):
    buf, out = [], []
    for line in text.splitlines():
        if sum(len(l) for l in buf) + len(line) > size and buf:
            out.append("\n".join(buf))
            buf = []
        buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return out


def notes_with_llm(transcript):
    parts = _chunks(transcript)
    if len(parts) == 1:
        material = transcript
    else:
        material = "\n\n".join(_ollama(CHUNK_PROMPT.format(text=p)) for p in parts)
    return _ollama(NOTES_PROMPT.format(material=material))


STOPWORDS = set("""a an the and or but if so to of in on at for with from by as is are was were be been
being it its this that these those i you we they he she me us them my our your their his her
um uh yeah yes no ok okay right just like really very also then than there here what which who
do does did have has had not can could will would should shall may might going get got know
think mean want one two thing things something all about some any more well actually""".split())

ACTION_RE = re.compile(
    r"\b(i'll|we'll|you'll|he'll|she'll|they'll|need to|needs to|have to|has to|"
    r"should|let's|action item|follow up|follow-up|to-do|todo|deadline|assign|"
    r"will (prepare|send|share|fix|update|check|create|write|call|schedule)|"
    r"by (monday|tuesday|wednesday|thursday|friday|tomorrow|today|next week|end of))\b",
    re.IGNORECASE)
DECISION_RE = re.compile(r"\b(decided|agreed|approved?|finali[sz]ed?|we will go with|go ahead)\b",
                         re.IGNORECASE)


def notes_without_llm(segs):
    text = " ".join(s["text"] for s in segs)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 5]
    words = [w for w in re.findall(r"[a-zA-Z']+", text.lower()) if w not in STOPWORDS and len(w) > 2]
    freq = Counter(words)

    def score(sentence):
        ws = [w for w in re.findall(r"[a-zA-Z']+", sentence.lower()) if w in freq]
        return sum(freq[w] for w in ws) / (len(ws) + 1)

    top = sorted(sorted(sentences, key=score, reverse=True)[:5], key=sentences.index)
    actions = [s for s in sentences if ACTION_RE.search(s)][:10]
    decisions = [s for s in sentences if DECISION_RE.search(s)][:8]
    topics = ", ".join(w for w, _ in freq.most_common(8)) or "None"

    def bullets(items, box=False):
        return "\n".join(f"- {'[ ] ' if box else ''}{i}" for i in items) or "None"

    return (f"## Summary\n{' '.join(top[:3]) or 'Not enough speech to summarise.'}\n\n"
            f"## Key discussion points\n{bullets(top)}\n\n"
            f"## Main topics\n{topics}\n\n"
            f"## Decisions\n{bullets(decisions)}\n\n"
            f"## Action items\n{bullets(actions, box=True)}\n\n"
            "> Notes generated with the basic built-in summarizer. Install Ollama for better notes.")


def process_meeting(folder, progress=print):
    folder = Path(folder)
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    progress("Transcribing audio (first run downloads the Whisper model)...")
    segs, language = transcribe(folder / "audio.wav")
    transcript = format_transcript(segs)
    (folder / "transcript.txt").write_text(transcript or "(no speech detected)", encoding="utf-8")
    if not segs:
        body, engine = "No speech was detected in the recording.", "none"
    elif ollama_available():
        progress(f"Writing notes with local AI ({OLLAMA_MODEL})...")
        try:
            body, engine = notes_with_llm(transcript), f"Ollama - {OLLAMA_MODEL}"
        except Exception as exc:
            progress(f"Ollama failed ({exc}); using basic summarizer.")
            body, engine = notes_without_llm(segs), "built-in summarizer"
    else:
        progress("Writing notes with the built-in summarizer...")
        body, engine = notes_without_llm(segs), "built-in summarizer"
    minutes = round(meta.get("duration_s", 0) / 60, 1)
    header = (f"# {meta.get('title', 'Meeting')}\n\n"
              f"**Date:** {meta.get('started', '-')} | **Duration:** {minutes} min | "
              f"**Language:** {language} | **Notes by:** {engine}\n\n")
    notes = header + body + "\n"
    (folder / "notes.md").write_text(notes, encoding="utf-8")
    meta.update({"language": language, "notes_engine": engine})
    meta_path.write_text(json.dumps(meta, indent=2))
    return notes


class LiveProcessor(threading.Thread):
    """Transcribes audio and extracts notes incrementally while the meeting is still recording,
    so that stopping only has to synthesize the final notes instead of processing everything."""

    def __init__(self, recorder, interval=20):
        super().__init__(daemon=True)
        self.recorder = recorder
        self.interval = interval
        self.segments = []
        self.extracted = []
        self.language = None
        self.error = None
        self._use_llm = ollama_available()
        self._pending_text = ""
        self._elapsed = 0.0
        self._halt = threading.Event()

    def run(self):
        while not self._halt.wait(self.interval):
            self._flush()

    def _flush(self):
        audio = self.recorder.pull_new_audio()
        if len(audio) < SAMPLE_RATE:  # skip slivers under ~1s
            return
        tmp = self.recorder.out_dir / "_live_chunk.wav"
        try:
            write_wav(tmp, audio, SAMPLE_RATE)
            segs, lang = transcribe(tmp)
        except Exception as exc:
            self.error = exc
            return
        finally:
            tmp.unlink(missing_ok=True)
        offset = self._elapsed
        self._elapsed += len(audio) / SAMPLE_RATE
        if not self.language and lang:
            self.language = lang
        for s in segs:
            self.segments.append({"start": s["start"] + offset, "end": s["end"] + offset, "text": s["text"]})
        new_text = format_transcript(segs)
        if new_text:
            self._pending_text = (self._pending_text + "\n" + new_text).strip()
        if self._use_llm and len(self._pending_text) > 4000:
            self._extract_pending()

    def _extract_pending(self):
        try:
            self.extracted.append(_ollama(CHUNK_PROMPT.format(text=self._pending_text)))
        except Exception as exc:
            self.error = exc
        self._pending_text = ""

    def finish(self):
        """Stops the background loop and processes whatever audio is left. Call before recorder.stop()."""
        self._halt.set()
        self.join(timeout=30)
        self._flush()
        if self._use_llm and self._pending_text.strip():
            self._extract_pending()
        return self.segments, self.extracted


def finalize_notes(folder, segments, extracted, language="unknown", progress=print):
    """Writes notes.md from material a LiveProcessor already transcribed/extracted during the call."""
    folder = Path(folder)
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    transcript = format_transcript(segments)
    (folder / "transcript.txt").write_text(transcript or "(no speech detected)", encoding="utf-8")
    if not segments:
        body, engine = "No speech was detected in the recording.", "none"
    elif extracted:
        progress(f"Writing notes with local AI ({OLLAMA_MODEL})...")
        try:
            body = _ollama(NOTES_PROMPT.format(material="\n\n".join(extracted)))
            engine = f"Ollama - {OLLAMA_MODEL}"
        except Exception as exc:
            progress(f"Ollama failed ({exc}); using basic summarizer.")
            body, engine = notes_without_llm(segments), "built-in summarizer"
    else:
        progress("Writing notes with the built-in summarizer...")
        body, engine = notes_without_llm(segments), "built-in summarizer"
    minutes = round(meta.get("duration_s", 0) / 60, 1)
    header = (f"# {meta.get('title', 'Meeting')}\n\n"
              f"**Date:** {meta.get('started', '-')} | **Duration:** {minutes} min | "
              f"**Language:** {language} | **Notes by:** {engine}\n\n")
    notes = header + body + "\n"
    (folder / "notes.md").write_text(notes, encoding="utf-8")
    meta.update({"language": language, "notes_engine": engine})
    meta_path.write_text(json.dumps(meta, indent=2))
    return notes
