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
    {"id": "devices", "name": "Spoken word (all devices)", "bitrate": "96k",
     "about": "The default for lectures and talks. Clear on phone speakers, which play little below "
              "300 Hz, as well as laptops and headphones: mono, more low cut, less boom, more presence, "
              "levelled, compressed, and set to -14 LUFS (the level YouTube plays speech at).",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=150", "lowpass=f=9000", "afftdn=nf=-25",
                 "equalizer=f=250:t=q:w=1:g=-3", "equalizer=f=3000:t=q:w=1:g=5",
                 "dynaudnorm=f=250:g=15:p=0.9:m=8",
                 "acompressor=threshold=0.1:ratio=4:attack=5:release=150", "loudnorm=I=-14:TP=-2"]},
    {"id": "lecture", "name": "Spoken word, podcast level", "bitrate": "96k",
     "about": "One voice, set to -16 LUFS, the level podcast platforms expect: mono, hum and hiss "
              "removed, presence lifted, quiet and loud passages evened out. Quieter on phones.",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=90", "lowpass=f=8000", "afftdn=nf=-25",
                 "equalizer=f=3000:t=q:w=1:g=3", "dynaudnorm=f=250:g=15:p=0.9:m=8",
                 "loudnorm=I=-16:TP=-2"]},
    {"id": "compressed", "name": "Spoken word, compressed", "bitrate": "96k",
     "about": "The podcast-level preset with a compressor after the levelling, which catches sudden "
              "peaks (laughs, coughs, a raised voice, hard consonants) phrase by phrase.",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=90", "lowpass=f=8000", "afftdn=nf=-25",
                 "equalizer=f=3000:t=q:w=1:g=3",
                 "dynaudnorm=f=250:g=15:p=0.9:m=8",
                 "acompressor=threshold=0.1:ratio=3:attack=10:release=200", "loudnorm=I=-16:TP=-2"]},
    {"id": "noisy", "name": "Spoken word, noisy room", "bitrate": "96k",
     "about": "As the lecture preset, with stronger noise reduction that follows changing "
              "background noise (fans, air conditioning, traffic).",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=100", "lowpass=f=7500",
                 "afftdn=nr=20:nf=-20:tn=1", "equalizer=f=3000:t=q:w=1:g=4",
                 "dynaudnorm=f=250:g=15:p=0.9:m=8", "loudnorm=I=-14:TP=-2"]},
    {"id": "far", "name": "Speaker far from the microphone", "bitrate": "96k",
     "about": "For a speaker who moves about or turns away: heavier levelling that lifts "
              "quiet stretches further, so expect more room sound between phrases.",
     "filters": ["pan=mono|c0=0.5*c0+0.5*c1", "highpass=f=80", "lowpass=f=8000", "afftdn=nf=-25",
                 "equalizer=f=2500:t=q:w=1:g=4", "dynaudnorm=f=150:g=31:p=0.9:m=20",
                 "loudnorm=I=-14:TP=-2"]},
    {"id": "discussion", "name": "Discussion, several voices", "bitrate": "128k",
     "about": "Keeps stereo, so voices stay where they were; removes rumble and hiss and "
              "evens out loud and quiet speakers more gently.",
     "filters": ["highpass=f=80", "afftdn=nf=-28", "dynaudnorm=f=300:g=21:p=0.9:m=6",
                 "loudnorm=I=-14:TP=-2"]},
    {"id": "light", "name": "Light clean-up", "bitrate": "160k",
     "about": "Stereo, rumble and faint hiss removed, loudness set. Leaves the dynamics alone.",
     "filters": ["highpass=f=60", "afftdn=nf=-30", "loudnorm=I=-14:TP=-2"]},
    {"id": "loudness", "name": "Loudness only", "bitrate": "160k",
     "about": "No cleaning: only brings the whole recording to a standard loudness.",
     "filters": ["loudnorm=I=-14:TP=-2"]},
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


# ---------------------------------------------------------------- loudness in two passes
#
# Single-pass loudnorm undershoots on long speech recordings (a lecture aimed at -16 LUFS came out at
# -17.5, with peaks over 0 dBFS after AAC encoding). When the chain ends in loudnorm, the job replaces it:
# it measures the chain without it, applies the gain that reaches the target and a limiter for the peaks,
# measures again to make up what the limiter took, and only then writes the file.

LOUDNORM_LAST = re.compile(r"(?:^|,)loudnorm(?:=([^,]*))?$")


def loudnorm_target(af):
    """(chain before a final loudnorm, target LUFS, peak ceiling dB), or None."""
    m = LOUDNORM_LAST.search(af or "")
    if not m:
        return None
    opts = {}
    for o in (m.group(1) or "").split(":"):
        k, _, v = o.partition("=")
        opts[k.strip().lower()] = v.strip()
    try:
        target = float(opts.get("i", "-24"))
        peak = float(opts.get("tp", "-2"))
    except ValueError:
        return None
    return af[:m.start()], target, min(peak, -0.5)


def measured_lufs(text):
    i = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", text)
    return float(i[-1]) if i and i[-1] != "-inf" else None


# short-term (3 s) loudness, one reading every 0.1 s
FRAME = re.compile(r"\[ebur128@(before|after) @ [^\]]+\] t: ([\d.]+)\s+TARGET:\S+ LUFS\s+M:\s*-?[\d.]+\s+S:\s*(-?[\d.]+)")
SUMMARY = re.compile(r"\[ebur128@(before|after) @ [^\]]+\] Summary:")
INTEGRATED = re.compile(r"^\s+I:\s+(-?[\d.]+|-inf) LUFS")


def gain_for(target, lufs):
    return 0.0 if lufs is None else max(-30.0, min(40.0, target - lufs))


def settle(measure, target, gain, tries=4):
    """Find the gain that reaches the target through the limiter, which takes more the louder it goes.
    measure(gain) returns the loudness it gives; each try moves along the slope of the last two."""
    points = []
    for _ in range(tries):
        got = measure(gain)
        if got is None:
            break
        points.append((gain, got))
        miss = target - got
        if abs(miss) < 0.3:
            break
        slope = 1.0
        if len(points) > 1 and points[-1][0] != points[-2][0]:
            slope = min(1.0, max(0.2, (points[-1][1] - points[-2][1]) / (points[-1][0] - points[-2][0])))
        gain = max(-30.0, min(40.0, gain + miss / slope))
    return gain


def level_chain(before, target, peak, lufs, gain=None):
    if gain is None:
        gain = gain_for(target, lufs)
    limit = max(0.0625, 10 ** (peak / 20))
    tail = "volume=%.2fdB,alimiter=limit=%.4f:attack=5:release=50:level=false:latency=true" % (gain, limit)
    return before + "," + tail if before else tail


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

    loud = None
    processed = af and (first["audio"] if n == 1 else not copy_audio)
    lt = loudnorm_target(af) if processed else None
    if lt:
        before, target, peak = lt
        if n == 1:
            i = cmd.index("-af") + 1
            inputs, graph, src = cmd[:5], "", "[0:a:0]"
        else:
            i = cmd.index("-filter_complex") + 1
            inputs, graph, src = cmd[:i - 1], cmd[i].rpartition("[j]")[0], "[j]"
        loud = {"index": i, "af": af, "before": before, "target": target, "peak": peak,
                "inputs": inputs, "graph": graph, "src": src, "vsink": ";[v]nullsink" if reencode else ""}
        notes.append("The loudness is measured over the whole recording, then set to %g LUFS with peaks "
                     "held below %g dB, checked again after the limiter, and the file written." % (target, peak))

    if has_video and not reencode:
        notes.insert(0, "The video is copied without re-encoding%s." % (", as the files share a format" if n > 1 else ""))
    if copy_audio or (n == 1 and not af):
        notes.append("The sound is copied unchanged.")
    home = os.path.expanduser("~") + "/"
    # ~/'Library/…' still expands in a shell, and keeps the home folder's name off the screen
    show = lambda c: " ".join(("~/" + shlex.quote(a[len(home):])) if a.startswith(home) else shlex.quote(a)
                              for a in ["ffmpeg"] + c[1:])
    if loud:
        final = list(cmd)
        final[loud["index"]] = final[loud["index"]].replace(
            af, level_chain(loud["before"], loud["target"], loud["peak"], None).replace("volume=0.00dB", "volume=GAIN"))
        shown = ("# 1. measure the loudness\n" + show(measure_cmd(loud, before_lane=True)) +
                 "\n\n# 2. write it, with GAIN = %g minus the loudness measured in step 1\n" % loud["target"] + show(final))
    else:
        shown = show(cmd)
    return {"cmd": cmd, "shown": shown, "out": out, "total": total, "notes": notes,
            "files": files, "filters": af, "chain_error": check_chain(af) if af else None,
            "reencode": reencode, "loud": loud}


def measure_cmd(loud, chain=None, before_lane=False):
    """Run the chain (by default, everything before loudnorm) into loudness meters, writing nothing."""
    chain = loud["before"] if chain is None else chain
    g = loud["graph"] + loud["src"]
    if before_lane:
        g += "asplit[kr][kc];[kr]ebur128@before=framelog=info,anullsink;[kc]"
    g += (chain + "," if chain else "") + "ebur128@after=framelog=info[ka]" + loud["vsink"]
    return loud["inputs"] + ["-filter_complex", g, "-map", "[ka]", "-f", "null", "-"]


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
        self.log = []
        self.stage = None
        self.passes = 3 if p["loud"] else 1   # measure, level (more if needed), write
        self.last_gain = 0.0
        self.lane_gen = 0
        self.pass_no = 0
        self.measured = None   # loudness of the original
        self.integrated = {}
        self.lanes = {"before": [], "after": []}
        self.result = None

    def run(self):
        loud = self.plan["loud"]
        cmd = list(self.plan["cmd"])
        meter = ["-progress", "pipe:1", "-nostats", "-loglevel", "info"]
        try:
            if loud:
                target, peak = loud["target"], loud["peak"]
                self._pass(0, "Measuring", measure_cmd(loud, before_lane=True) + meter)
                self.measured = self.integrated.get("before")
                tries = []

                def measure(gain):
                    if tries:
                        self.passes += 1   # another levelling pass
                    tries.append(gain)
                    self.lanes["after"] = []
                    self.lane_gen += 1
                    self._pass(len(tries), "Levelling",
                               measure_cmd(loud, level_chain(loud["before"], target, peak, None, gain)) + meter)
                    self.last_gain = gain
                    return self.integrated.get("after")
                gain = settle(measure, target, gain_for(target, self.integrated.get("after")))
                shift = gain - self.last_gain
                self.lanes["after"] = [round(v + shift, 1) for v in self.lanes["after"]]
                self.lane_gen += 1
                print("loudness %s LUFS; gain %.2f dB after %d levelling passes"
                      % (self.measured, gain, len(tries)), flush=True)
                cmd[loud["index"]] = cmd[loud["index"]].replace(
                    loud["af"], level_chain(loud["before"], target, peak, None, gain))
            self._pass(self.passes - 1, "Writing",
                       cmd[:-1] + ["-progress", "pipe:1", "-nostats", "-loglevel", "error", "-y", self.partial])
            if not os.path.isfile(self.partial):
                raise JobError("ffmpeg wrote nothing.")
            if loud:   # quick: the sound alone, to report what the file really measures
                self.stage = "Checking"
                r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", self.partial, "-map", "0:a:0",
                                    "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True)
                summary = r.stderr[r.stderr.rfind("Summary:"):]
                i = re.findall(r"I:\s+(-?[\d.]+) LUFS", summary)
                p = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
                self.result = {"lufs": float(i[0]) if i else None,
                               "peak": float(p[-1]) if p and p[-1] != "-inf" else None}
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

    def _pass(self, no, stage, cmd):
        self.pass_no, self.stage, self.done_s, self.speed = no, stage, 0.0, None
        self.log = []
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        reader = threading.Thread(target=self._read_err, daemon=True)
        reader.start()
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
        reader.join(5)
        if self.cancelled:
            raise JobError("Cancelled.")
        if code != 0:
            errors = [l for l in self.log if "rror" in l or "nvalid" in l] or self.log
            raise JobError("\n".join(errors[-6:]) or "ffmpeg stopped (exit %d)." % code)

    def _read_err(self):
        summary = None
        for line in self.proc.stderr:
            m = FRAME.match(line)
            if m:   # one reading every 0.1 s; the lanes keep one a second
                lane, t = self.lanes[m.group(1)], float(m.group(2))
                if int(t) >= len(lane):
                    lane.append(max(-70.0, float(m.group(3))))
                continue
            m = SUMMARY.match(line)
            if m:
                summary = m.group(1)
                continue
            m = INTEGRATED.match(line)
            if m and summary:
                self.integrated[summary] = None if m.group(1) == "-inf" else float(m.group(1))
                summary = None
                continue
            if line.strip():
                self.log.append(line.rstrip())

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def status(self, lb=0, la=0, gen=-1):
        total = self.plan["total"] or 1
        if gen != self.lane_gen:
            la = 0
        elapsed = (self.finished or time.time()) - self.started
        left = None
        if self.speed and self.state == "running":
            left = max(0, (total - self.done_s) / self.speed + (self.passes - 1 - self.pass_no) * total / self.speed)
        fraction = (self.pass_no + min(1, self.done_s / total)) / self.passes
        return {"state": self.state, "error": self.error, "out": self.out,
                "done": round(self.done_s, 1), "total": round(total, 1),
                "stage": self.stage if self.passes > 1 else None, "measured": self.measured,
                "pass": self.pass_no, "passes": self.passes, "result": self.result,
                "target": self.plan["loud"]["target"] if self.plan["loud"] else None,
                "lanes": {"before": self.lanes["before"][lb:], "after": self.lanes["after"][la:]}, "gen": self.lane_gen,
                "fraction": fraction if self.state != "done" else 1,
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
    # read a little earlier so the levelling filters have settled when the clip starts
    lead = min(off, 5.0)
    clip = [FFMPEG, "-hide_banner", "-nostdin", "-ss", "%.3f" % (off - lead), "-t", "%.3f" % (length + lead),
            "-i", f["path"], "-vn", "-map", "0:a:0"]
    trim = "atrim=start=%.3f,asetpts=PTS-STARTPTS" % lead
    lt = loudnorm_target(af)
    if lt:   # as in the job: measure, then settle the gain through the limiter
        before, target, peak = lt

        def lufs(chain):
            r = subprocess.run(clip + ["-af", ",".join(x for x in (base, chain, trim, "ebur128=framelog=quiet") if x),
                                       "-f", "null", "-"], capture_output=True, text=True)
            return measured_lufs(r.stderr)
        gain = settle(lambda g: lufs(level_chain(before, target, peak, None, g)), target,
                      gain_for(target, lufs(before)))
        af = level_chain(before, target, peak, None, gain)
    for which, filt in (("before", base), ("after", base + ("," + af if af else ""))):
        dest = os.path.join(PREVIEW_ROOT, "%s-%s.wav" % (which, token))
        r = subprocess.run(clip + ["-loglevel", "error", "-af", "%s,%s" % (filt, trim), "-c:a", "pcm_s16le",
                                   "-y", dest], capture_output=True, text=True)
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
            q = parse_qs(url.query)
            if "page" in q:
                last_seen = time.time()
            self._json({"version": VERSION, "queued": len(queue), "page_seen": last_seen,
                        "job": job.status(*(int((q.get(k) or ["0"])[0]) for k in ("lb", "la")),
                                                      int((q.get("gen") or ["-1"])[0])) if job else None,
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
    parent = int(os.environ.get("KINTSUGI_PARENT") or 0)   # set by the Mac app, which owns this server
    while True:
        time.sleep(2)
        if parent and os.getppid() != parent:   # the app has gone without stopping us
            server.shutdown()
            return
        busy = job is not None and job.state == "running"
        if not parent and not busy and time.time() - last_seen > IDLE_SECONDS:
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
            for _ in range(40):   # let the job thread remove its partial file
                if job.state != "running":
                    break
                time.sleep(0.1)


if __name__ == "__main__":
    main()
