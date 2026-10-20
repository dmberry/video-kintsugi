#!/usr/bin/env python3
"""Video Kintsugi: join recordings and clean up their sound with ffmpeg, behind a small standard-library web server.

Serves index.html on http://127.0.0.1:8767/ and runs one job at a time. The files are joined
in order (the video is stream-copied when the files share a format, and re-encoded with
VideoToolbox when they do not) and the audio goes through an editable ffmpeg filter chain.

No third-party packages. Runs on the system or Homebrew python3 (3.9+).
"""

import atexit
import copy
import glob
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "VERSION")) as _f:
    VERSION = _f.read().strip()
HOST = "127.0.0.1"
PORT = int(os.environ.get("KINTSUGI_PORT", "8767"))
STATIC = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          "desk.js": "text/javascript; charset=utf-8",
          "VERSION": "text/plain; charset=utf-8"}
BRANDING_TYPES = {".svg": "image/svg+xml", ".png": "image/png"}

SEARCH = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]   # Homebrew (Apple, Intel), MacPorts
BREWS = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
FFMPEG = FFPROBE = None   # set by find_ffmpeg(); the page explains how to install it while missing
SUPPORT = os.path.expanduser("~/Library/Application Support/Video Kintsugi")
PRESETS_FILE = os.path.join(SUPPORT, "presets.json")
LIST_FILE = os.path.join(SUPPORT, "join-list.txt")   # kept so a copied command still runs

RATE = 48000              # every input's audio is resampled to this before joining
PREVIEW_SECONDS = 20
IDLE_SECONDS = 15 * 60    # exit when no page has polled for this long and nothing is running

DEFAULT_PRESETS = [
    {"id": "lecture", "name": "Spoken word (lecture)", "bitrate": "96k",
     "about": "One voice from a laptop or room microphone: mono, hum and hiss removed, "
              "presence lifted, quiet and loud passages evened out, then set to podcast loudness.",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=90", "lowpass=f=8000", "afftdn=nf=-25",
                 "equalizer=f=3000:t=q:w=1:g=3", "dynaudnorm=f=250:g=15:p=0.9:m=8",
                 "loudnorm=I=-16:TP=-1.5:LRA=7"]},
    {"id": "noisy", "name": "Spoken word, noisy room", "bitrate": "96k",
     "about": "As the lecture preset, with stronger noise reduction that follows changing "
              "background noise (fans, air conditioning, traffic).",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=100", "lowpass=f=7500",
                 "afftdn=nr=20:nf=-20:tn=1", "equalizer=f=3000:t=q:w=1:g=4",
                 "dynaudnorm=f=250:g=15:p=0.9:m=8", "loudnorm=I=-16:TP=-1.5:LRA=7"]},
    {"id": "far", "name": "Speaker far from the microphone", "bitrate": "96k",
     "about": "For a speaker who moves about or turns away: heavier levelling that lifts "
              "quiet stretches further, so expect more room sound between phrases.",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=80", "lowpass=f=8000", "afftdn=nf=-25",
                 "equalizer=f=2500:t=q:w=1:g=4", "dynaudnorm=f=150:g=31:p=0.9:m=20",
                 "loudnorm=I=-16:TP=-1.5:LRA=7"]},
    {"id": "discussion", "name": "Discussion, several voices", "bitrate": "128k",
     "about": "Keeps stereo, so voices stay where they were; removes rumble and hiss and "
              "evens out loud and quiet speakers more gently.",
     "filters": ["highpass=f=80", "afftdn=nf=-28", "dynaudnorm=f=300:g=21:p=0.9:m=6",
                 "loudnorm=I=-16:TP=-1.5:LRA=9"]},
    {"id": "light", "name": "Light clean-up", "bitrate": "160k",
     "about": "Stereo, rumble and faint hiss removed, loudness set. Leaves the dynamics alone.",
     "filters": ["highpass=f=60", "afftdn=nf=-30", "loudnorm=I=-16:TP=-1.5:LRA=11"]},
    {"id": "loudness", "name": "Loudness only", "bitrate": "160k",
     "about": "No cleaning: only brings the whole recording to a standard loudness.",
     "filters": ["loudnorm=I=-16:TP=-1.5:LRA=11"]},
    {"id": "none", "name": "No change (join only)", "bitrate": "",
     "about": "The audio is copied as it is when the files allow it, otherwise re-encoded unchanged.",
     "filters": []},
]

last_seen = time.time()
last_dir = os.path.expanduser("~")
queue = []                # paths handed over by launch.sh (Finder Quick Action)
job = None
job_lock = threading.Lock()
server = None

for _old in glob.glob(os.path.join(tempfile.gettempdir(), "kintsugi-preview-*")):
    shutil.rmtree(_old, True)
PREVIEW_ROOT = tempfile.mkdtemp(prefix="kintsugi-preview-")
atexit.register(shutil.rmtree, PREVIEW_ROOT, True)


class JobError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def find_ffmpeg():
    global FFMPEG, FFPROBE
    if FFMPEG:
        return True
    for d in SEARCH + os.environ.get("PATH", "").split(":"):
        f, p = os.path.join(d, "ffmpeg"), os.path.join(d, "ffprobe")
        if d and os.access(f, os.X_OK) and os.access(p, os.X_OK):
            FFMPEG, FFPROBE = f, p
            print("Using %s" % FFMPEG, flush=True)
            return True
    return False


def brew():
    return next((b for b in BREWS if os.access(b, os.X_OK)), None)


def install_ffmpeg():
    """Run the Homebrew install in a Terminal window, where its progress and any questions are visible."""
    b = brew()
    if not b:
        raise JobError("Homebrew isn't installed.")
    cmd = "%s install ffmpeg" % shlex.quote(b)
    subprocess.run(["osascript", "-e", 'tell application "Terminal"', "-e", "activate",
                    "-e", "do script %s" % json.dumps(cmd), "-e", "end tell"], capture_output=True)


def hms(seconds):
    s = int(seconds)
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def chain(lines):
    """One filter per line; blank lines and # comments are ignored, stray commas dropped."""
    out = []
    for line in lines if isinstance(lines, list) else str(lines or "").splitlines():
        line = line.split("#", 1)[0].strip().strip(",").strip()
        if line:
            out.append(line)
    return ",".join(out)


def probe(path):
    path = os.path.expanduser(path.strip().strip("'\""))
    if not os.path.isfile(path):
        raise JobError("File not found: %s" % path)
    r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", path], capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or ["unreadable"])[-1]
        if "Operation not permitted" in r.stderr:
            msg = "macOS refused access (System Settings › Privacy & Security › Files and Folders)"
        raise JobError("Can't read %s: %s" % (os.path.basename(path), msg))
    info = json.loads(r.stdout)
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v and not a:
        raise JobError("%s has no video or audio." % os.path.basename(path))
    duration = float(fmt.get("duration") or (v or a).get("duration") or 0)
    d = {"path": path, "name": os.path.basename(path), "dir": os.path.dirname(path),
         "size": int(fmt.get("size") or os.path.getsize(path)), "duration": duration,
         "video": None, "audio": None}
    if v:
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0
        d["video"] = {"codec": v.get("codec_name"), "profile": v.get("profile"),
                      "width": v.get("width"), "height": v.get("height"),
                      "pix_fmt": v.get("pix_fmt"), "fps": round(fps, 3),
                      "rate": v.get("r_frame_rate"), "bitrate": int(v.get("bit_rate") or 0)}
    if a:
        d["audio"] = {"codec": a.get("codec_name"), "rate": int(a.get("sample_rate") or 0),
                      "channels": a.get("channels"), "layout": a.get("channel_layout"),
                      "bitrate": int(a.get("bit_rate") or 0)}
    return d


def osa_choose(kind, prompt, default_dir=None, multiple=False):
    expr = 'choose %s with prompt "%s"' % (kind, prompt)
    if default_dir and os.path.isdir(default_dir):
        quoted = default_dir.replace("\\", "\\\\").replace('"', '\\"')
        expr += ' default location (POSIX file "%s")' % quoted
    if multiple:
        expr += " with multiple selections allowed"
        script = ['set out to ""', "repeat with f in (%s)" % expr,
                  "set out to out & POSIX path of f & linefeed", "end repeat", "out"]
    else:
        script = ["POSIX path of (%s)" % expr]
    args = ["osascript", "-e", "activate"]
    for line in script:
        args += ["-e", line]
    r = subprocess.run(args, capture_output=True, text=True)
    paths = [p for p in r.stdout.splitlines() if p.strip()]
    if r.returncode != 0 or not paths:
        return []
    return [p.rstrip("/") for p in paths] if kind == "folder" else paths


def resolve_dropped(name, size, modified_ms):
    """Find the real path of a file dropped on the page, via Spotlight."""
    quoted = name.replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run(["mdfind", 'kMDItemFSName == "%s"' % quoted],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    hits = []
    for p in r.stdout.splitlines():
        try:
            st = os.stat(p)
        except OSError:
            continue
        if os.path.basename(p) == name and st.st_size == size:
            hits.append((abs(st.st_mtime * 1000 - (modified_ms or 0)), p))
    hits.sort()
    if len(hits) == 1 or (hits and hits[0][0] < 2000):
        return hits[0][1]
    return None


def check_chain(af):
    """Run the chain over a moment of silence, so a typing mistake shows before a long job."""
    if not af:
        return None
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=%d:cl=stereo" % RATE, "-t", "0.2", "-af", af,
                        "-f", "null", "-"], capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        return None
    lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
    return lines[0] if lines else "ffmpeg rejected the filter chain."


# ---------------------------------------------------------------- presets

def load_presets():
    try:
        with open(PRESETS_FILE) as f:
            presets = json.load(f)
        if isinstance(presets, list) and presets:
            return presets
    except (OSError, ValueError):
        pass
    return copy.deepcopy(DEFAULT_PRESETS)


def save_presets(presets):
    clean = []
    for p in presets:
        clean.append({"id": str(p.get("id") or "p%d" % int(time.time() * 1000)),
                      "name": str(p.get("name") or "Untitled").strip()[:80],
                      "bitrate": str(p.get("bitrate") or ""),
                      "about": str(p.get("about") or "").strip(),
                      "filters": [l.rstrip() for l in (p.get("filters") or []) if isinstance(l, str)]})
    if not clean:
        raise JobError("Keep at least one preset.")
    os.makedirs(SUPPORT, exist_ok=True)
    tmp = PRESETS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clean, f, indent=2)
    os.replace(tmp, PRESETS_FILE)
    return clean


# ---------------------------------------------------------------- the plan

def video_key(d):
    v = d["video"]
    return v and (v["codec"], v["profile"], v["width"], v["height"], v["pix_fmt"], v["rate"])


def audio_key(d):
    a = d["audio"]
    return a and (a["codec"], a["rate"], a["channels"])


def unique_path(outdir, name, ext):
    path, n = os.path.join(outdir, name + ext), 2
    while os.path.exists(path):
        path = os.path.join(outdir, "%s %d%s" % (name, n, ext))
        n += 1
    return path


def plan(opts):
    """Turn the page's choices into one ffmpeg command. Returns the command and what it will do."""
    files = [probe(p) for p in opts.get("paths") or []]
    if not files:
        raise JobError("Add at least one file.")
    with_video = [f for f in files if f["video"]]
    if with_video and len(with_video) != len(files):
        raise JobError("Some files have no video. Join video files with video files.")
    has_video = bool(with_video)
    same_video = has_video and len({video_key(f) for f in files}) == 1
    reencode = has_video and (opts.get("video") == "reencode" or not same_video)
    af = chain(opts.get("filters") or "")
    bitrate = str(opts.get("bitrate") or "")
    if af and not bitrate:
        bitrate = "128k"
    copy_audio = (not af and all(f["audio"] for f in files)
                  and len({audio_key(f) for f in files}) == 1 and not reencode)
    if not af and not copy_audio and not bitrate:
        bitrate = "160k"

    first = files[0]
    ext = os.path.splitext(first["name"])[1].lower() if has_video else ".m4a"
    if ext not in (".mov", ".mp4", ".m4v", ".mkv", ".m4a"):
        ext = ".mp4"
    outdir = os.path.expanduser(opts.get("outdir") or "") or first["dir"]
    if not os.path.isdir(outdir):
        raise JobError("Output folder not found: %s" % outdir)
    name = re.sub(r"[/:]", "-", (opts.get("outname") or "").strip()) or "joined"
    out = unique_path(outdir, name, ext)

    cmd = [FFMPEG, "-hide_banner", "-nostdin"]
    n = len(files)
    total = sum(f["duration"] for f in files)
    notes = []
    if n == 1:
        cmd += ["-i", first["path"]]
        maps = (["-map", "0:v:0"] if has_video else []) + (["-map", "0:a:0"] if first["audio"] else [])
        if af and first["audio"]:
            if first["audio"]["channels"] != 2:   # the presets expect stereo, as when joining
                af = "aformat=channel_layouts=stereo," + af
            cmd += maps + ["-af", af]
        else:
            cmd += maps
        meta = 0
    else:
        demux = has_video and not reencode
        offset = 0
        if demux:
            os.makedirs(SUPPORT, exist_ok=True)
            with open(LIST_FILE, "w") as f:
                for x in files:
                    f.write("file '%s'\nduration %.6f\n" % (x["path"].replace("'", "'\\''"), x["duration"]))
            cmd += ["-f", "concat", "-safe", "0", "-i", LIST_FILE]
            offset = 1
        separate = not copy_audio   # each file opened again for its sound (or its video, re-encoding)
        if separate:
            for x in files:
                cmd += ["-i", x["path"]]
        parts = []
        if reencode:
            W, H = first["video"]["width"], first["video"]["height"]
            fps = first["video"]["rate"] or "30"
            for i, x in enumerate(files):
                parts.append("[%d:v:0]scale=%d:%d:force_original_aspect_ratio=decrease,"
                             "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%s,format=yuv420p[v%d]"
                             % (offset + i, W, H, W, H, fps, i))
            if len({video_key(f) for f in files}) > 1:
                notes.append("The files differ in format, so the video is re-encoded to match the first "
                             "(%d×%d, %s fps)." % (W, H, first["video"]["fps"]))
        if not copy_audio:
            for i, x in enumerate(files):
                if x["audio"]:
                    parts.append("[%d:a:0]aresample=%d,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                                 "apad=whole_dur=%.6f,atrim=end=%.6f,asetpts=PTS-STARTPTS[a%d]"
                                 % (offset + i, RATE, x["duration"], x["duration"], i))
                else:
                    parts.append("aevalsrc=0:s=%d:c=stereo:d=%.6f[a%d]" % (RATE, x["duration"], i))
                    notes.append("%s has no sound; silence fills its place." % x["name"])
        if reencode:
            labels = "".join("[v%d][a%d]" % (i, i) for i in range(n))
            parts.append("%sconcat=n=%d:v=1:a=1[v][j]" % (labels, n))
        elif not copy_audio:
            parts.append("%sconcat=n=%d:v=0:a=1[j]" % ("".join("[a%d]" % i for i in range(n)), n))
        if not copy_audio:
            parts.append("[j]%s[a]" % (af or "anull"))
        if parts:
            cmd += ["-filter_complex", ";".join(parts)]
        cmd += ["-map", "[v]" if reencode else "0:v:0"] if has_video else []
        cmd += ["-map", "0:a:0"] if copy_audio else ["-map", "[a]"]
        meta = offset if separate else 0

    if has_video and not reencode:
        cmd += ["-c:v", "copy"]
    elif has_video:
        hevc = first["video"]["codec"] == "hevc"
        rate = max([f["video"]["bitrate"] for f in files] + [0]) or 5000000
        cmd += ["-c:v", "hevc_videotoolbox" if hevc else "h264_videotoolbox",
                "-b:v", "%dk" % (rate * 11 // 10000), "-allow_sw", "1"]
    if has_video and first["video"]["codec"] == "hevc" and ext in (".mov", ".mp4", ".m4v"):
        cmd += ["-tag:v", "hvc1"]   # so QuickTime and Finder previews play HEVC
    if copy_audio or (n == 1 and not af and first["audio"]):
        cmd += ["-c:a", "copy"]
    elif first["audio"] or n > 1:
        cmd += ["-c:a", "aac", "-b:a", bitrate or "160k"]
    cmd += ["-map_metadata", str(meta)]
    if ext in (".mov", ".mp4", ".m4v", ".m4a"):
        cmd += ["-movflags", "+faststart"]
    cmd += [out]

    if has_video and not reencode:
        notes.insert(0, "The video is copied without re-encoding%s." % (", as the files share a format" if n > 1 else ""))
    if copy_audio or (n == 1 and not af):
        notes.append("The sound is copied unchanged.")
    home = os.path.expanduser("~") + "/"
    # ~/'Library/…' still expands in a shell, and keeps the home folder's name off the screen
    shown = " ".join(("~/" + shlex.quote(a[len(home):])) if a.startswith(home) else shlex.quote(a)
                     for a in ["ffmpeg"] + cmd[1:])
    return {"cmd": cmd, "shown": shown, "out": out, "total": total, "notes": notes,
            "files": files, "filters": af, "chain_error": check_chain(af) if af else None,
            "reencode": reencode}


# ---------------------------------------------------------------- the job

class Job:
    def __init__(self, p):
        self.plan = p
        self.out = p["out"]
        stem, ext = os.path.splitext(os.path.basename(self.out))
        self.partial = os.path.join(os.path.dirname(self.out), ".%s.partial%s" % (stem, ext))
        self.state = "running"
        self.error = None
        self.done_s = 0.0
        self.speed = None
        self.started = time.time()
        self.finished = None
        self.proc = None
        self.cancelled = False
        self.tail = []

    def run(self):
        cmd = self.plan["cmd"][:-1] + ["-progress", "pipe:1", "-nostats", "-loglevel", "error",
                                        "-y", self.partial]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            threading.Thread(target=self._read_err, daemon=True).start()
            for line in self.proc.stdout:
                k, _, v = line.strip().partition("=")
                if k == "out_time_us" and v.isdigit():
                    self.done_s = int(v) / 1e6
                elif k == "speed" and v.endswith("x"):
                    try:
                        self.speed = float(v[:-1])
                    except ValueError:
                        pass
            code = self.proc.wait()
            if self.cancelled:
                raise JobError("Cancelled.")
            if code != 0 or not os.path.isfile(self.partial):
                raise JobError("\n".join(self.tail[-6:]) or "ffmpeg stopped (exit %d)." % code)
            out = self.out
            if os.path.exists(out):   # something took the name while we worked
                stem, ext = os.path.splitext(os.path.basename(out))
                out = unique_path(os.path.dirname(out), stem, ext)
            os.replace(self.partial, out)
            self.out = out
            self.state = "done"
        except Exception as e:
            self.state = "cancelled" if self.cancelled else "error"
            self.error = str(e) if isinstance(e, JobError) else "%s: %s" % (type(e).__name__, e)
            if os.path.exists(self.partial):
                os.remove(self.partial)
        finally:
            self.finished = time.time()
            print("[%s] %s → %s" % (time.strftime("%H:%M:%S"), self.state, self.out), flush=True)

    def _read_err(self):
        for line in self.proc.stderr:
            if line.strip():
                self.tail = (self.tail + [line.rstrip()])[-40:]

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def status(self):
        total = self.plan["total"] or 1
        elapsed = (self.finished or time.time()) - self.started
        left = None
        if self.speed and self.state == "running":
            left = max(0, (total - self.done_s) / self.speed)
        return {"state": self.state, "error": self.error, "out": self.out,
                "done": round(self.done_s, 1), "total": round(total, 1),
                "fraction": min(1, self.done_s / total) if self.state != "done" else 1,
                "speed": self.speed, "elapsed": round(elapsed, 1), "left": left,
                "size": os.path.getsize(self.out) if self.state == "done" else None}


# ---------------------------------------------------------------- preview

def make_preview(opts):
    """Two short clips from the same moment, before and after the chain, with their loudness."""
    files = [probe(p) for p in opts.get("paths") or []]
    if not files:
        raise JobError("Add a file first.")
    at = max(0.0, float(opts.get("at") or 0))
    length = max(3.0, min(60.0, float(opts.get("length") or PREVIEW_SECONDS)))
    start = 0.0
    for f in files:
        if at < start + f["duration"] or f is files[-1]:
            break
        start += f["duration"]
    off = max(0.0, min(at - start, f["duration"] - length))
    if not f["audio"]:
        raise JobError("%s has no sound." % f["name"])
    af = chain(opts.get("filters") or "")
    err = check_chain(af)
    if err:
        raise JobError(err)
    base = "aresample=%d,aformat=sample_fmts=fltp:channel_layouts=stereo" % RATE
    token = str(int(time.time() * 1000))
    for old in glob.glob(os.path.join(PREVIEW_ROOT, "*.wav")):
        os.remove(old)
    result = {"token": token, "file": f["name"], "from": off, "at": start + off, "length": length}
    for which, filt in (("before", base), ("after", base + ("," + af if af else ""))):
        dest = os.path.join(PREVIEW_ROOT, "%s-%s.wav" % (which, token))
        # read a little earlier so the levelling filters have settled when the clip starts
        lead = min(off, 5.0)
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", "%.3f" % (off - lead),
                            "-t", "%.3f" % (length + lead), "-i", f["path"], "-vn", "-map", "0:a:0",
                            "-af", "%s,atrim=start=%.3f,asetpts=PTS-STARTPTS" % (filt, lead),
                            "-c:a", "pcm_s16le", "-y", dest], capture_output=True, text=True)
        if r.returncode != 0:
            raise JobError((r.stderr.strip().splitlines() or ["preview failed"])[-1])
        result[which] = loudness(dest)
    return result


def loudness(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", path, "-af", "ebur128=framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True)
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    lra = re.findall(r"LRA:\s+([\d.]+) LU", r.stderr)
    return {"lufs": float(i[-1]) if i else None, "lra": float(lra[-1]) if lra else None}


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file_range(self, path, ctype):
        """Serve a file with HTTP Range support, so the audio player can seek."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = f.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _guard(self, write=False):
        """Refuse other hosts (DNS rebinding) and cross-site writes (no custom header)."""
        global last_seen
        host_ok = self.headers.get("Host", "") in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)
        if not host_ok or (write and self.headers.get("X-Kintsugi") != "1"):
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        global last_seen
        if not self._guard():
            return
        url = urlparse(self.path)
        path = url.path
        name = "index.html" if path == "/" else path.lstrip("/")
        ext = os.path.splitext(name)[1]
        if name in STATIC or (name.startswith("branding/") and "/" not in name[9:]
                              and ext in BRANDING_TYPES):
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    self._send(200, f.read(), STATIC.get(name) or BRANDING_TYPES[ext])
            except OSError:
                self._json({"error": "not found"}, 404)
        elif path == "/api/status":
            if "page" in parse_qs(url.query):
                last_seen = time.time()
            self._json({"version": VERSION, "queued": len(queue), "page_seen": last_seen,
                        "job": job.status() if job else None,
                        "ffmpeg": FFMPEG if find_ffmpeg() else None, "brew": bool(brew()),
                        "brew_path": brew() or ("/opt/homebrew/bin/brew" if platform.machine() == "arm64"
                                                else "/usr/local/bin/brew")})
        elif path == "/api/presets":
            self._json({"presets": load_presets(), "defaults": DEFAULT_PRESETS,
                        "builtin": [p["id"] for p in DEFAULT_PRESETS]})
        elif path == "/api/preview-audio":
            q = parse_qs(url.query)
            which = q.get("which", [""])[0]
            token = re.sub(r"\D", "", q.get("token", [""])[0])
            p = os.path.join(PREVIEW_ROOT, "%s-%s.wav" % (which, token))
            if which not in ("before", "after") or not os.path.isfile(p):
                return self._json({"error": "No preview."}, 404)
            self._send_file_range(p, "audio/wav")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global job, last_dir, queue
        if not self._guard(write=True):
            return
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/install-ffmpeg":
                install_ffmpeg()
                return self._json({"ok": True})
            if path not in ("/api/queue", "/api/presets", "/api/quit", "/api/cancel") and not find_ffmpeg():
                raise JobError("FFmpeg isn't installed, so recordings can't be read yet.")
            if path == "/api/choose-files":
                ps = osa_choose("file", "Choose the recordings to join, in any order", last_dir, True)
                if ps:
                    last_dir = os.path.dirname(ps[0])
                return self._json(self._describe_all(ps))
            if path == "/api/choose-folder":
                ps = osa_choose("folder", "Choose where to save the result", body.get("start") or last_dir)
                return self._json({"path": ps[0]} if ps else {"cancelled": True})
            if path == "/api/resolve":
                p = resolve_dropped(body.get("name", ""), int(body.get("size") or 0), body.get("modified"))
                return self._json(self._describe_all([p]) if p else {"files": [], "errors": []})
            if path == "/api/probe":
                return self._json(self._describe_all(body.get("paths") or []))
            if path == "/api/queue":
                queue += [p for p in body.get("paths") or [] if isinstance(p, str)]
                return self._json({"ok": True})
            if path == "/api/take-queue":
                ps, queue = queue, []
                return self._json(self._describe_all(ps))
            if path == "/api/presets":
                return self._json({"presets": save_presets(body.get("presets") or [])})
            if path == "/api/plan":
                p = plan(body)
                return self._json({k: p[k] for k in ("shown", "out", "total", "notes", "chain_error", "reencode")})
            if path == "/api/preview":
                return self._json(make_preview(body))
            if path == "/api/run":
                with job_lock:
                    if job and job.state == "running":
                        return self._json({"error": "A job is already running."}, 409)
                    p = plan(body)
                    if p["chain_error"]:
                        raise JobError("The filter chain has a problem: %s" % p["chain_error"])
                    job = Job(p)
                    threading.Thread(target=job.run, daemon=True).start()
                return self._json({"ok": True, "out": job.out})
            if path == "/api/cancel":
                if job:
                    job.cancel()
                return self._json({"ok": True})
            if path == "/api/open":
                if job and job.state == "done" and os.path.exists(job.out):
                    subprocess.run(["open", "-R", job.out] if body.get("reveal") else ["open", job.out])
                return self._json({"ok": True})
            if path == "/api/quit":
                if job:
                    job.cancel()
                self._json({"ok": True})
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            self._json({"error": "not found"}, 404)
        except JobError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    @staticmethod
    def _describe_all(paths):
        files, errors = [], []
        for p in paths:
            try:
                files.append(probe(p))
            except JobError as e:
                errors.append(str(e))
        return {"files": files, "errors": errors}


def watchdog():
    while True:
        time.sleep(30)
        busy = job is not None and job.state == "running"
        if not busy and time.time() - last_seen > IDLE_SECONDS:
            server.shutdown()
            return


def main():
    global server
    if not find_ffmpeg():
        print("FFmpeg not found; the page will say how to install it.", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so atexit cleanup runs
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=watchdog, daemon=True).start()
    print("Video Kintsugi %s on http://%s:%d/" % (VERSION, HOST, PORT), flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if job and job.state == "running":
            job.cancel()


if __name__ == "__main__":
    main()
