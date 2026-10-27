"""Records mic + system audio (other participants) and optionally the screen."""
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import warnings
import wave
from pathlib import Path

import numpy as np

try:
    import soundcard as sc
except Exception:
    sc = None  # no usable audio backend (e.g. headless cloud container)

SAMPLE_RATE = 48000
BLOCK = SAMPLE_RATE // 10
warnings.filterwarnings("ignore", message=".*discontinuity.*")


class _AudioCapture(threading.Thread):
    def __init__(self, device, channels, label):
        super().__init__(daemon=True)
        self.device = device
        self.channels = channels
        self.label = label
        self.chunks = []
        self.error = None
        self._halt = threading.Event()
        self._flushed_idx = 0

    def run(self):
        try:
            _ensure_com()
            with self.device.recorder(samplerate=SAMPLE_RATE, channels=self.channels,
                                      blocksize=BLOCK) as rec:
                while not self._halt.is_set():
                    data = rec.record(numframes=BLOCK)
                    self.chunks.append(data.mean(axis=1).astype(np.float32))
        except Exception as exc:
            self.error = exc

    def stop(self):
        self._halt.set()
        self.join(timeout=5)

    def samples(self):
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.chunks)

    def pull_new(self):
        """Returns samples captured since the last call, for incremental processing."""
        n = len(self.chunks)
        new = self.chunks[self._flushed_idx:n]
        self._flushed_idx = n
        if not new:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(new)


def _ensure_com():
    """soundcard's Windows backend needs COM initialized on whatever thread calls it.
    Streamlit runs the app script on its own internal thread, not the process main thread,
    so this must be called there too, not just inside the capture threads."""
    if platform.system() == "Windows":
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)


def _screen_command(out_path):
    system = platform.system()
    if system == "Windows":
        source = ["-f", "gdigrab", "-framerate", "15", "-i", "desktop"]
    elif system == "Darwin":
        source = ["-f", "avfoundation", "-framerate", "15", "-capture_cursor", "1", "-i", "1:none"]
    else:
        source = ["-f", "x11grab", "-framerate", "15", "-i", os.environ.get("DISPLAY", ":0.0")]
    return ["ffmpeg", "-y", "-loglevel", "error", *source,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path)]


class MeetingRecorder:
    def __init__(self, out_dir, title="Meeting", record_screen=False):
        self.out_dir = Path(out_dir)
        self.title = title
        self.record_screen = record_screen and shutil.which("ffmpeg") is not None
        self.captures = []
        self.screen_proc = None
        self.started_at = None
        self.warnings = []

    def start(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        if sc is None:
            raise RuntimeError(
                "No local audio backend available (this looks like a cloud/demo environment). "
                "Run the app on your own machine to record a meeting.")
        _ensure_com()
        try:
            self.captures.append(_AudioCapture(sc.default_microphone(), 1, "microphone"))
        except Exception as exc:
            self.warnings.append(f"No microphone found ({exc}).")
        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            channels = min(2, getattr(loopback, "channels", 2) or 2)
            self.captures.append(_AudioCapture(loopback, channels, "system audio"))
        except Exception as exc:
            self.warnings.append(f"Could not capture system audio ({exc}).")
        if not self.captures:
            raise RuntimeError("No audio device available to record from: " + " ".join(self.warnings))
        for cap in self.captures:
            cap.start()
        if self.record_screen:
            self.screen_proc = subprocess.Popen(
                _screen_command(self.out_dir / "screen.mp4"),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        ended_at = time.time()
        for cap in self.captures:
            cap.stop()
            if cap.error:
                self.warnings.append(f"{cap.label} stopped early: {cap.error}")
        audio_path = self.out_dir / "audio.wav"
        self._write_mix(audio_path)
        video_path = None
        if self.screen_proc:
            try:
                self.screen_proc.communicate(b"q", timeout=30)
            except Exception:
                self.screen_proc.kill()
            video_path = self._merge(self.out_dir / "screen.mp4", audio_path)
        meta = {
            "title": self.title,
            "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started_at)),
            "duration_s": round(ended_at - self.started_at),
            "audio": audio_path.name,
            "video": video_path.name if video_path else None,
            "warnings": self.warnings,
        }
        (self.out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta

    def pull_new_audio(self):
        """Mixes whatever's been captured since the last pull, without stopping recording."""
        return _mix(c.pull_new() for c in self.captures)

    def _write_mix(self, path):
        write_wav(path, _mix(c.samples() for c in self.captures))

    def _merge(self, screen_path, audio_path):
        if not screen_path.exists():
            self.warnings.append("Screen recording failed; only audio was saved.")
            return None
        out = self.out_dir / "meeting.mp4"
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(screen_path), "-i", str(audio_path),
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0 and out.exists():
            screen_path.unlink(missing_ok=True)
            return out
        self.warnings.append("Couldn't merge screen and audio; kept them separately.")
        return screen_path


def _mix(tracks):
    tracks = list(tracks)
    length = max((len(t) for t in tracks), default=0)
    mix = np.zeros(length, dtype=np.float32)
    for t in tracks:
        mix[:len(t)] += t
    peak = float(np.abs(mix).max()) if length else 0.0
    if peak > 0.99:
        mix *= 0.99 / peak
    return mix


def write_wav(path, mix, rate=SAMPLE_RATE):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((mix * 32767).astype(np.int16).tobytes())
