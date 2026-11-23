"""Beat Tool: YuE2 Studio prepared on a graphics card, every part of it checked, and only then opened for you.

The page (beat_tool.html) opens in your browser at http://127.0.0.1:8796. This backend talks to YuE2 Studio's MCP
server at http://127.0.0.1:8791/mcp, which answers programs on this computer but not web pages. Every step is a
click (or all of them at once: Prepare everything), and a step counts only when it is checked on the machine that
computes: the chain at the top of the page fills in link by link, and a failure turns its step red with the exact
error. Nothing is trusted from an earlier session; a click checks again.

The order is the order things fail in: the card, then CUDA (and Vulkan on a rented machine) by using them, before
anything of the studio is downloaded; the LoRA trainer before the studio starts, so it starts with it; the models,
the engine on the card and every part the studio would otherwise download mid-work. Open YuE2 Studio is the last
step: it checks all of that again and opens the studio's window in a tab. From there you work in the studio yourself.

Everything it makes stays in this folder: the studio unpacked as YuE2-Studio-<version>_from_github_dateDDMMYYtimeHHMM
(the studio keeps its models and library inside it).

The work can also run on a machine rented on vast.ai (step 0, beat_remote.py): the same steps, the same page, with
YuE2 Studio's Linux build computing on the rented card.
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath

import beat_remote
from beat_remote import KeyStore, Remote, RemoteError, Vast, VastError, _number

HERE = Path(__file__).resolve().parent
PAGE = HERE / "beat_tool.html"
PORT = 8796
MCP_URL = "http://127.0.0.1:8791/mcp"

# The YuE2 Studio release this tool is pinned to on this PC (27 Oct 2026): the portable Windows build unpacked in this
# folder, with the models and library already in it. Its MCP tools were read at this tag. Moving to another release is
# a choice, not something that happens by itself: the start step refuses any other version.
STUDIO_TAG = "v3.8.2"
STUDIO_VERSION = "3.8.2"
STUDIO_ZIP = "YuE2-Studio-3.8.2-portable-windows-x64.zip"
STUDIO_ZIP_URL = f"https://github.com/timoncool/YuE2-Studio/releases/download/{STUDIO_TAG}/{STUDIO_ZIP}"
STUDIO_ZIP_BYTES = 335_664_562
STUDIO_ZIP_SHA256 = "b86a86f1093b0950df782bd65bb5459ffa4d02c943349c3e51ac078f8971281d"
STUDIO_NAME = "YuE2-Studio-3.8.2"          # unpacked as <name>_from_github_dateDDMMYYtimeHHMM
# On a machine rented on vast.ai runs the studio's Linux build (the Windows one cannot run there), the AppImage of
# release 3.8.4, read on 5 Nov 2026 and checked against the size and SHA-256 below on the rented machine. Its MCP
# server (crates/music-server/src/mcp.rs) is byte-identical to v3.8.2's (SHA-256 D87ED167...F4B5), so every step makes
# the same calls on either. The studio's own label for the Linux build is experimental. It carries GTK and WebKitGTK
# itself; unpacked with --appimage-extract it needs no FUSE.
REMOTE_TAG = "v3.8.4"
REMOTE_VERSION = "3.8.4"
LINUX_ASSET = "YuE2-Studio-3.8.4-linux-x86_64.AppImage"
LINUX_URL = f"https://github.com/timoncool/YuE2-Studio/releases/download/{REMOTE_TAG}/{LINUX_ASSET}"
LINUX_BYTES = 224_049_656
LINUX_SHA256 = "cc864de7f06bd158238f63cbaf205a7d3455ea3dd2f4fff085a2047ec6eba7f7"
REMOTE_NAME = "YuE2-Studio-3.8.4-linux"    # unpacked there as <name>_from_github_dateDDMMYYtimeHHMM
REMOTE_EXE = "usr/bin/YuE2-Studio"
STUDIO_EXE = "YuE2-Studio.exe"
MARKER = "beat_tool_unpacked.json"         # what was unpacked, written once the unpack is checked
START_LOG = "beat_tool_start.log"          # what the studio prints once Start launches it, kept beside it
# YuE2 Studio's LoRA trainer on the rented machine (beat_remote.trainer_build_script says why it is built there): HOT-Step's
# ace-train at the commit studio 3.8.4 pins (engines/music-train-source.json at v3.8.4: release hotstep-tools-5ae17a66),
# so the studio and its trainer agree on every argument. The studio asks for about 11 GB of video memory to train.
TRAINER_REPO = "https://github.com/timoncool/HOT-Step-CPP.git"
TRAINER_COMMIT = "5ae17a666df7a6129c316f98b754fe0f17fe415f"
TRAINER_VRAM_GB = 11

# The steps, in the order things fail in. On this PC the studio downloads its own Windows trainer with its training
# pack (Studio parts); on a rented machine the trainer is a step of its own, before the studio starts.
SETUP_PC = ("where", "gpu", "cuda", "studio", "start", "models", "engine", "parts", "open")
SETUP_VAST = ("where", "gpu", "cuda", "studio", "trainer", "start", "models", "engine", "parts", "open")
ORDER = SETUP_VAST                         # every step there is
LABELS = {"dataset": "LoRA dataset", "where": "Where it runs", "gpu": "GPU", "cuda": "CUDA", "studio": "YuE2 Studio", "trainer": "LoRA trainer",
          "start": "Studio running", "models": "Models", "engine": "Engine on the card", "parts": "Studio parts",
          "open": "Open YuE2 Studio"}
# On the rented Linux machine the song engine computes through Vulkan: the studio ships its CUDA engine for Windows
# only and refuses the CUDA backend elsewhere (hardware.rs, lib.rs at v3.8.4). Score, stems and MIDI run on CUDA there.
REMOTE_LABELS = {"cuda": "CUDA and Vulkan"}
VAST_DIR = "vast"
# What each button does, and the step it belongs to. Stopping a rented machine is not a step: it runs at any time.
# Prepare everything and Open YuE2 Studio are answered on their own (Handler.do_POST).
ACTIONS = {"where_pc": "where", "vast_key": "where", "vast_forget": "where", "vast_offers": "where",
           "vast_rent": "where", "vast_connect": "where",
           "gpu": "gpu", "cuda": "cuda", "studio": "studio", "trainer": "trainer", "start": "start",
           "models_read": "models", "models_use": "models", "engine": "engine", "parts_read": "parts", "parts": "parts"}
# The parts of the studio it downloads by itself the first time you use them, downloaded here first: the separator
# (stems, and the vocals a LoRA's lyric timing needs), the training pack (the trainer's weights; on this PC the trainer
# too), Audio to MIDI (MuScriptor, the studio's default size) and the listening pack (describing songs by ear).
PARTS = ("separator", "training", "midi", "listen")
PART_LABELS = {"separator": "Stem separator (HT-Demucs)", "training": "Training pack (LoRA)",
               "midi": "Audio to MIDI (MuScriptor)", "listen": "Listening pack (MOSS-Music, tempo)"}
# Parts Open YuE2 Studio does not wait for: the listening pack only describes LoRA dataset songs by ear, and the
# ONNX Runtime its tempo model needs comes from github.com/microsoft/onnxruntime, which answered 404 on 23 Nov 2026.
OPTIONAL_PARTS = ("listen",)
PART_STALL = 90        # seconds with nothing downloading and the part still missing: the studio stopped without saying
MIN_FREE_END_GB = 10   # free disk left once everything is downloaded, for your songs, datasets and LoRA runs
CUT_AT = 60_000       # YuE2 Studio's MCP cuts a longer answer and sends it as plain text (mcp.rs LIMIT at v3.8.2)


class StudioError(RuntimeError):
    pass


class Refused(RuntimeError):
    """A click that cannot run now: another step is working, or the link before it is not connected."""


class StepProblem(ValueError):
    """What Open YuE2 Studio's last check found, and the step it belongs to."""

    def __init__(self, step, text):
        super().__init__(text)
        self.step = step


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def size_text(count) -> str:
    count = float(count or 0)
    return f"{count / 1e9:.2f} GB" if count >= 1e9 else f"{count / 1e6:.1f} MB"


def _money(value) -> str:
    return f"{_number(value):.2f}"


def _try(work):
    """What work answers, or None when it fails: a figure shown beside a click, never the click's own work."""
    try:
        return work()
    except (VastError, ValueError, OSError):
        return None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inside(path, folder) -> bool:
    path, folder = (os.path.normcase(os.path.abspath(p)) for p in (path, folder))
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def same_file(path, other) -> bool:
    return bool(path) and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(other))


def printed(log, offset, count):
    """The last lines written to a log after offset."""
    try:
        with open(log, "rb") as handle:
            handle.seek(max(offset, handle.seek(0, os.SEEK_END) - 65536))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line.strip()[:300] for line in text.splitlines() if line.strip()][-count:]


def written_since(folder, since, skip):
    """What was written in folder since a moment: how many files, how much, and the latest."""
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            path = Path(root, name)
            try:
                info = path.stat()
            except OSError:
                continue
            if info.st_mtime >= since and path != skip:
                found.append((info.st_mtime, path, info.st_size))
    if not found:
        return "nothing yet"
    _, latest, size = max(found, key=lambda entry: entry[0])
    return (f"{len(found)} file{'s' if len(found) != 1 else ''}, {size_text(sum(entry[2] for entry in found))}; "
            f"the latest is {latest.relative_to(folder)} ({size_text(size)})")


def stamp(now=None) -> str:
    return (now or dt.datetime.now()).strftime("date%d%m%ytime%H%M")


def now_text() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def need(answer, key, tool):
    """A field the next step cannot do without; its absence is named with the answer it is missing from."""
    value = answer.get(key) if isinstance(answer, dict) else None
    if value in (None, "", []):
        raise StudioError(f"{tool} answered without “{key}”: {json.dumps(answer, ensure_ascii=False)[:300]}")
    return value


def download(url, target, progress):
    """Fetch url into target; answer its size and SHA-256."""
    request = urllib.request.Request(url, headers={"User-Agent": "beat-tool"})
    digest, done = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done)
    except urllib.error.HTTPError as error:
        raise ValueError(f"GitHub answered HTTP {error.code} for {url}.") from None
    except (urllib.error.URLError, OSError) as error:
        raise ValueError(f"The download stopped: {getattr(error, 'reason', error)}") from None
    return done, digest.hexdigest()


def unpack(archive_path, folder):
    """Unpack into folder, which must be new; refuse a member that would land outside it."""
    folder = Path(folder)
    folder.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if not inside(folder / member.filename, folder):
                raise ValueError(f"The archive holds a path outside its folder ({member.filename}). Nothing was started.")
        archive.extractall(folder)
        return len(archive.infolist())


# ------------------------------------------------------------ a LoRA dataset folder, as you write it

# What YuE2 Studio's training takes from a folder (training.rs, lib.rs upload_training_files at v3.8.4): audio, and a
# same-named .txt read as its lyrics only. Every uploaded song comes in with an empty style, so Beat Tool sends the
# lyrics without the style and gives each song its style itself.
AUDIO_TYPES = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aiff", ".aif")
MIN_SONG_SECONDS = 10          # the trainer cuts songs into ten-second pieces; a shorter song is refused
STYLE_WORDS = (35, 70)         # the studio's guide: one English sentence of 35-70 words, ending in the tempo
SONGS_SUGGESTED = (5, 50)
DATASETS_DIR = "datasets"      # the clean copies sent to the studio (your audio: never in git)
STYLE_LINE = re.compile(r"^\s*style\s*:\s*(.*)$", re.I)
ENDS_IN_TEMPO = re.compile(r"\b\d{2,3}(?:\.\d+)?\s*BPM\.?\s*$", re.I)


def split_style(text):
    """A .txt as you write it: the lyrics, then a line "style:" and the style (on that line or the lines after it).
    Answers the lyrics and the style, the style as one line; no "style:" line, no style."""
    lyrics, style, inside = [], [], False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        found = None if inside else STYLE_LINE.match(line)
        if found:
            inside = True
            line = found.group(1)
        (style if inside else lyrics).append(line.strip() if inside else line.rstrip())
    return "\n".join(lyrics).strip(), " ".join(part for part in style if part).strip()


def section_tags(lyrics):
    """The section headings the studio keeps: "[Verse 1]" on its own line. A tag with a colon ("[Chorus: x]") or a
    time ("[00:12]") is taken away on upload as a tag of the file, not a section."""
    tags = []
    for line in (lyrics or "").splitlines():
        line = line.strip()
        if len(line) > 2 and line.startswith("[") and line.endswith("]"):
            tag = line[1:-1]
            if ":" not in tag and not tag[:1].isdigit():
                tags.append(line)
    return tags


def wav_seconds(path):
    """A WAV file's length from its header (PCM or float), or None for another kind of file."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            rate = block = None
            while True:
                chunk = handle.read(8)
                if len(chunk) < 8:
                    return None
                kind, size = chunk[:4], int.from_bytes(chunk[4:], "little")
                if kind == b"fmt ":
                    body = handle.read(size + (size & 1))
                    rate, block = int.from_bytes(body[4:8], "little"), int.from_bytes(body[12:14], "little")
                    continue
                if kind == b"data":
                    return size / (rate * block) if rate and block else None
                handle.seek(size + (size & 1), os.SEEK_CUR)
    except OSError:
        return None


def read_dataset_folder(folder):
    """Every song of a dataset folder as the studio will see it, and what stands in the way: problems stop the send,
    warnings only say what the studio's guide asks for."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"There is no folder {folder}.")
    files = {path.name: path for path in folder.iterdir() if path.is_file()}
    shared = ""
    if "style.txt" in {name.lower() for name in files}:
        shared = " ".join(Path(files[next(name for name in files if name.lower() == "style.txt")])
                          .read_text(encoding="utf-8-sig", errors="replace").split())
        shared = split_style(shared)[1] or shared
    texts = {path.stem: path for path in files.values() if path.suffix.lower() == ".txt" and path.name.lower() != "style.txt"}
    songs, warnings = [], []
    for path in sorted((p for p in files.values() if p.suffix.lower() in AUDIO_TYPES), key=lambda p: p.name.lower()):
        song = {"file": path.name, "stem": path.stem, "seconds": wav_seconds(path), "problems": [], "warnings": []}
        text = texts.get(path.stem)
        lyrics, style = split_style(text.read_text(encoding="utf-8-sig", errors="replace")) if text else ("", "")
        song.update(lyrics=lyrics, style=style or shared, style_from=("its .txt" if style else "style.txt" if shared else ""),
                    sections=section_tags(lyrics))
        if song["seconds"] is not None and song["seconds"] < MIN_SONG_SECONDS:
            song["problems"].append(f"{song['seconds']:.1f} s long: the studio refuses a song under {MIN_SONG_SECONDS} s "
                                    "(its trainer cuts songs into ten-second pieces)")
        if not text:
            song["problems"].append(f"no {path.stem}.txt beside it with its lyrics")
        elif not lyrics:
            song["problems"].append(f"{path.stem}.txt holds no lyrics before its style")
        elif not song["sections"]:
            song["problems"].append(f"no section tags in {path.stem}.txt: put [Verse 1], [Chorus] … on their own lines "
                                    "above the lines they head")
        if not song["style"]:
            song["problems"].append(f"no style: write it in {path.stem}.txt after a line \"style:\", or once for every "
                                    "song in style.txt")
        else:
            words = len(song["style"].split())
            if not STYLE_WORDS[0] <= words <= STYLE_WORDS[1]:
                song["warnings"].append(f"the style has {words} words; the studio's guide asks for one sentence of "
                                        f"{STYLE_WORDS[0]}-{STYLE_WORDS[1]}")
            if not ENDS_IN_TEMPO.search(song["style"]):
                song["warnings"].append("the style does not end in the song's own tempo (like \u201c…, 92 BPM.\u201d), as the studio's guide asks")
        songs.append(song)
    for stem in sorted(set(texts) - {song["stem"] for song in songs}):
        warnings.append(f"{texts[stem].name} has no audio file of the same name beside it, so it is left out")
    if not songs:
        raise ValueError(f"{folder} holds no song ({', '.join(AUDIO_TYPES)}).")
    if not SONGS_SUGGESTED[0] <= len(songs) <= SONGS_SUGGESTED[1]:
        warnings.append(f"{len(songs)} songs; the studio suggests {SONGS_SUGGESTED[0]}-{SONGS_SUGGESTED[1]}")
    return {"folder": str(folder), "name": folder.name, "songs": songs, "warnings": warnings,
            "problems": sum(len(song["problems"]) for song in songs)}


def newest_dataset_folder(desktop=None):
    """Your newest Desktop folder whose name begins with "dataset" and that holds audio, or None."""
    desktop = Path(desktop) if desktop else Path.home() / "Desktop"
    try:
        found = [path for path in desktop.iterdir() if path.is_dir() and path.name.lower().startswith("dataset")
                 and any(child.suffix.lower() in AUDIO_TYPES for child in path.iterdir())]
    except OSError:
        return None
    return str(max(found, key=lambda path: path.stat().st_mtime)) if found else None


class Studio:
    """YuE2 Studio's MCP server: JSON-RPC over HTTP, one request per POST, no browser origin."""

    def __init__(self, url=MCP_URL, timeout=70, endpoint=None):
        self.url, self.timeout = url, timeout
        # On a rented machine the MCP is reached through an SSH tunnel: endpoint answers its address here, opening the
        # tunnel again when it closed.
        self.endpoint = endpoint
        self.ids = itertools.count(1)
        self.last = None

    def rpc(self, method, params=None, timeout=None):
        if self.endpoint:
            try:
                self.url = self.endpoint()
            except RemoteError as error:
                raise StudioError(f"YuE2 Studio's MCP on the rented machine cannot be reached: {error}") from None
        body = json.dumps({"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params or {}}).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace").strip()[:600]
            error.close()
            raise StudioError(f"YuE2 Studio's MCP answered HTTP {error.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as error:
            raise StudioError(f"YuE2 Studio's MCP at {self.url} does not answer: {getattr(error, 'reason', error)}") from None
        try:
            message = json.loads(text)
        except ValueError:
            raise StudioError(f"YuE2 Studio's MCP answered something other than JSON: {text[:300]}") from None
        if message.get("error"):
            raise StudioError(f"{method}: {message['error'].get('message')}")
        return message.get("result") or {}

    def hello(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "beat-tool", "version": "1"}}, timeout=10)
        return result.get("serverInfo") or {}

    def tools(self):
        return {tool["name"]: tool for tool in self.rpc("tools/list").get("tools", [])}

    def call(self, name, arguments=None, timeout=None, text_ok=False):
        """A tool's answer as data. An answer the studio cut, or one that is not data, is an error that says so."""
        self.last = name
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        text = "".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
        if result.get("isError"):
            raise StudioError(f"{name}: {text}")
        if "structuredContent" in result:
            value = result["structuredContent"]
        else:
            try:
                value = json.loads(text)
            except ValueError:
                if text_ok:
                    return text
                if len(text) >= CUT_AT:
                    raise StudioError(f"{name}: YuE2 Studio cut its answer at {CUT_AT:,} characters, the most its MCP "
                                      f"sends, so it cannot be read. It begins: {text[:160]}") from None
                raise StudioError(f"{name}: YuE2 Studio answered text instead of data: {text[:300]}") from None
        if not isinstance(value, (dict, list)):
            raise StudioError(f"{name}: YuE2 Studio answered {json.dumps(value)[:300]} instead of data.")
        if isinstance(value, dict) and set(value) == {"error"}:
            raise StudioError(f"{name}: {value['error']}")
        return value


def new_step():
    return {"state": "open", "note": "", "error": "", "facts": {}, "data": {}, "key": None}


def soft(work):
    """A click on the Where card that changes nothing about where the work runs (a key, a search, a rent vast.ai
    refused): its failure is shown on the card, which keeps the state it had, so a wrong key does not take a connected
    chain apart."""
    @functools.wraps(work)
    def run(self, body):
        try:
            return work(self, body)
        except (ValueError, VastError, RemoteError, OSError) as error:
            self.soft_failure(error)
    return run


class App:
    def __init__(self, folder=HERE, studio=None, run=subprocess.run, popen=subprocess.Popen, fetch=download,
                 sleep=time.sleep, start_wait=180, engine_wait=600, vast_api=Vast, remote_api=Remote):
        self.folder = Path(folder)
        self.studio = studio or Studio()
        self.run, self.popen, self.fetch, self.sleep = run, popen, fetch, sleep
        self.start_wait, self.engine_wait = start_wait, engine_wait
        self.vast_api, self.remote_api = vast_api, remote_api      # vast.ai's API and the SSH link, replaceable in tests
        self.lock = threading.RLock()
        self.steps = {name: new_step() for name in ORDER}
        self.prior = {}
        self.busy = self.preparing = None
        self.part_stall = PART_STALL
        self.dataset = {"state": "open", "note": "", "error": "", "facts": {}, "data": {}}     # the LoRA dataset card
        self.gpu = self.exe = self.version = self.profile = None
        self.process = None
        # Where the work runs: this PC, or a machine rented on vast.ai, reached as self.remote over SSH.
        self.vast_dir = self.folder / VAST_DIR
        self.keys = KeyStore(self.vast_dir)
        self.local_studio = self.studio
        self.remote = self.remote_folder = self.remote_found = None
        self.stopping = False
        self.mode = "pc"
        try:
            self.mode = read_json(self.vast_dir / "compute.json").get("mode", "pc")
        except (OSError, ValueError):
            pass
        where = self.steps["where"]
        where["data"] = {"mode": self.mode, "key_saved": self.keys.path.is_file(), "instance": self.saved_instance()}
        if where["data"]["instance"] and where["data"]["key_saved"]:
            _try(lambda: self.sync_instance(self.vast()))
        if self.mode == "pc":
            where.update(state="ok", facts={"Runs on": "This PC: its graphics card computes every step"}, key="pc")

    # ------------------------------------------------------------ the chain

    def order(self):
        """The steps where the work runs now."""
        return SETUP_VAST if self.mode == "vast" else SETUP_PC

    def label(self, step):
        return (self.mode == "vast" and REMOTE_LABELS.get(step)) or LABELS.get(step, step)

    def missing_before(self, step):
        order = self.order()
        if step not in order or not order.index(step):
            return None
        before = order[order.index(step) - 1]
        return before if self.steps[before]["state"] != "ok" else None

    def claim(self, action):
        if action not in ACTIONS:
            raise Refused(f"There is no such step: {action}.")
        step = ACTIONS[action]
        with self.lock:
            self.take(step)
        return step

    def take(self, step, preparing=False):
        """The step marked working, if it may run now. Called with the lock held."""
        if self.preparing and not preparing:
            raise Refused(f"Wait: Prepare everything is working ({self.label(self.busy or self.preparing)} now).")
        if self.busy:
            raise Refused(f"Wait: {self.label(self.busy)} is still working.")
        if step not in self.order():
            raise Refused(f"{LABELS[step]} is a step on a rented machine only.")
        missing = self.missing_before(step)
        if missing:
            raise Refused(f"First {self.label(missing)}: that link is not connected yet.")
        self.busy = step
        self.prior[step] = self.steps[step]["state"]
        self.steps[step].update(state="working", note="", error="")

    def act(self, action, body=None):
        """Start a click's work in the background; answer the thread."""
        step = self.claim(action)
        return self.launch(step, getattr(self, "do_" + action), body or {})

    def launch(self, step, work, body):
        thread = threading.Thread(target=self._work, args=(step, work, body), daemon=True)
        thread.start()
        return thread

    def _work(self, step, work, body):
        try:
            self.run_step(step, work, body)
        finally:
            with self.lock:
                self.busy = None

    def run_step(self, step, work, body):
        """A step's work, its failure shown on its card word for word."""
        try:
            work(body)
            with self.lock:
                if self.steps[step]["state"] == "working":      # a read: it shows what is there, and connects nothing
                    self.steps[step].update(state="ok" if self.prior.get(step) == "ok" else "open", note="")
        except (KeyError, TypeError, AttributeError, IndexError) as error:
            self.failed(step, f"An answer or a saved record was not in the shape this tool expects "
                              f"({type(error).__name__}: {error}). The last YuE2 Studio call was "
                              f"{getattr(self.studio, 'last', None) or 'none yet'}.")
        except Exception as error:  # noqa: BLE001 - every failure is shown, word for word
            self.failed(step, error)

    # ------------------------------------------------------------ prepare everything

    def prepare_click(self, body=None):
        """Prepare everything: each step after Where it runs that is not verified yet, in order, as its own click runs
        it, until Open YuE2 Studio is the only step left, or a step fails: it turns red and the steps after it wait."""
        with self.lock:
            if self.preparing or self.busy:
                raise Refused(f"Wait: {self.label(self.busy or self.preparing)} is still working.")
            if self.steps["where"]["state"] != "ok":
                raise Refused("First Where it runs: use this PC, or connect to a rented machine.")
            self.preparing = "where"
        thread = threading.Thread(target=self._prepare, args=(dict(body or {}),), daemon=True)
        thread.start()
        return thread

    def _prepare(self, body):
        mode = self.mode
        try:
            for step in self.order()[1:-1]:
                if self.mode != mode:           # the rented machine was let go meanwhile
                    break
                if self.steps[step]["state"] == "ok":
                    continue
                with self.lock:
                    self.preparing = step
                    try:
                        self.take(step, preparing=True)
                    except Refused:
                        break
                try:
                    self.run_step(step, self.prepare_step, {**body, "step": step})
                finally:
                    with self.lock:
                        self.busy = None
                if self.steps[step]["state"] != "ok":
                    break
        finally:
            with self.lock:
                self.busy = self.preparing = None

    def prepare_step(self, body):
        """One step as Prepare everything runs it: the model set picked on the page, else the one preselected."""
        step = body["step"]
        if step == "models":
            self.do_models_read({})
            data = self.steps["models"]["data"]
            wanted = body.get("profile_id")
            readable = {option["id"] for option in data.get("options", []) if option["transcriber"]}
            self.do_models_use({"profile_id": wanted if wanted in readable else data.get("suggested")})
        else:
            getattr(self, "do_" + step)({})

    def note(self, step, text):
        with self.lock:
            self.steps[step]["note"] = text

    def later(self, step):
        return ORDER[ORDER.index(step) + 1:]

    def reset(self, steps):
        for name in steps:
            if self.steps[name]["state"] != "open" or self.steps[name]["key"] is not None:
                data = self.steps[name]["data"]
                self.steps[name] = dict(new_step(), data=data)

    def passed(self, step, facts, key, note=""):
        """Verified on this machine. A different result than before disconnects every link after it."""
        with self.lock:
            current = self.steps[step]
            if key is not None and key != current["key"]:
                self.reset(self.later(step))
            current.update(state="ok", facts=facts, error="", note=note, key=key if key is not None else current["key"])

    def failed(self, step, error):
        with self.lock:
            self.steps[step].update(state="failed", error=str(error), note="", facts={}, key=None)
            self.reset(self.later(step))

    # ------------------------------------------------------------ where the work runs

    def saved_instance(self):
        try:
            return read_json(self.vast_dir / "instance.json")
        except (OSError, ValueError):
            return None

    def sync_instance(self, vast):
        """Drops a saved instance record once vast.ai no longer has the instance."""
        record = self.saved_instance()
        if not record:
            self.where_data(instance=None)
            return None
        if vast.instance(record["id"]) is None:
            (self.vast_dir / "instance.json").unlink(missing_ok=True)
            self.disconnect()
            with self.lock:
                self.steps["where"]["data"]["instance"] = None
                if self.mode == "vast":
                    self.reset(ORDER)
                    self.set_mode("pc")
                    self.steps["where"].update(state="ok", key="pc", error="",
                                               facts={"Runs on": "This PC: its graphics card computes every step"})
            return None
        return record

    def where_data(self, **values):
        with self.lock:
            self.steps["where"]["data"].update(values)

    def soft_failure(self, error):
        with self.lock:
            self.steps["where"].update(state=self.prior.get("where") or "open", error=str(error), note="")

    def set_mode(self, mode):
        self.mode = mode
        write_json(self.vast_dir / "compute.json", {"mode": mode, "chosen": now_text()})
        self.where_data(mode=mode)

    def vast(self):
        try:
            key = self.keys.load()
        except OSError as error:
            raise ValueError(f"The saved vast.ai key cannot be read on this Windows account: {error}. Enter it again.") from None
        if not key:
            raise ValueError("Enter your vast.ai API key first (cloud.vast.ai - Account - Keys).")
        return self.vast_api(key, sleep=self.sleep)

    def disconnect(self):
        if self.remote:
            self.remote.close()
        self.remote = self.remote_folder = self.remote_found = None
        self.studio = self.local_studio

    def do_where_pc(self, _body):
        self.disconnect()
        self.set_mode("pc")
        rented = self.saved_instance()
        note = (f"vast.ai instance {rented['id']} is still rented and billing: stop renting it here when you are done with it."
                if rented else "")
        self.passed("where", {"Runs on": "This PC: its graphics card computes every step"}, key="pc", note=note)

    @soft
    def do_vast_key(self, body):
        key = str(body.get("key") or "").strip()
        if not key:
            raise ValueError("Paste your vast.ai API key (cloud.vast.ai - Account - Keys) into the field first.")
        if any(c.isspace() for c in key) or len(key) > 200:
            raise ValueError("That is not one vast.ai API key: it holds spaces or is too long.")
        vast = self.vast_api(key, sleep=self.sleep)
        user = vast.user()
        self.keys.save(key)
        self.sync_instance(vast)
        self.where_data(key_saved=True, credit=user.get("credit"), checked=now_text())
        self.where_data(message=f"The key works: vast.ai shows a credit of ${_money(user.get('credit'))}. It is kept encrypted "
                           f"for this Windows account in {VAST_DIR}\\key.dpapi.")

    @soft
    def do_vast_forget(self, _body):
        if self.saved_instance():
            _try(lambda: self.sync_instance(self.vast()))
        if self.saved_instance():
            raise ValueError(f"Instance {self.saved_instance()['id']} is rented: stop renting it first, because the key "
                             "is what stops it.")
        self.keys.forget()
        self.where_data(key_saved=False, credit=None, offers=None)
        self.where_data(message=f"The vast.ai key was deleted from {VAST_DIR}\\.")

    @soft
    def do_vast_offers(self, _body):
        """The machines worth renting, as beat_remote.offer_fits keeps them: one RTX 4090 or RTX 5090, more than 99.5 %
        reliable, at least 500 Mbps down and up, cheapest first."""
        vast = self.vast()
        self.sync_instance(vast)
        offers, seen = vast.offers()
        credit = vast.user().get("credit")
        rule = {"gpus": " or ".join(beat_remote.GPU_NAMES), "reliability": round(beat_remote.MIN_RELIABILITY * 100, 2),
                "mbps": beat_remote.MIN_MBPS}
        self.where_data(offers=offers, offers_seen=seen, offers_rule=rule, offers_at=now_text(), credit=credit)
        if not offers:
            raise ValueError(f"vast.ai lists no machine right now with one {rule['gpus']}, more than {rule['reliability']}% "
                             f"reliable, at least {rule['mbps']} Mbps down and up, driver {beat_remote.MIN_DRIVER}+ and "
                             f"{beat_remote.DISK_GB} GB of disk; {seen} offers came back before that check. Try again in a "
                             "few minutes: machines are rented and freed all the time.")

    def do_vast_rent(self, body):
        """What can go wrong before vast.ai has the order leaves the card as it was, with the error on it; once the
        machine is rented, every failure is the card's own, and the rented machine is never lost sight of."""
        try:
            record, vast = self.rent_machine(body)
        except (ValueError, VastError, RemoteError, OSError) as error:
            return self.soft_failure(error)
        self.set_mode("vast")
        self.where_data(instance=record)
        self.connect(vast, record, wait=1200)

    def rent_machine(self, body):
        vast = self.vast()
        if self.saved_instance():
            self.sync_instance(vast)
        if self.saved_instance():
            raise ValueError(f"Instance {self.saved_instance()['id']} is rented already: connect to it, or stop renting it "
                             "first. One machine at a time.")
        offer = next((o for o in self.steps["where"]["data"].get("offers") or [] if str(o["id"]) == str(body.get("offer"))), None)
        if not offer:
            raise ValueError("Choose a machine from the list first (Find machines).")
        _key, public = beat_remote.make_ssh_key(self.vast_dir, run=self.run)
        self.note("where", f"Renting offer {offer['id']} ({offer['gpu']}, ${offer['dph']}/h)…")
        try:
            instance_id = vast.rent(offer["id"], public)
        except VastError as error:
            if "no_such_ask" not in str(error) and "not available" not in str(error):
                raise
            raise ValueError(self.offer_gone(vast, offer)) from None
        record = {"id": instance_id, "offer": offer, "rented": now_text(), "rented_epoch": time.time(), "image": beat_remote.IMAGE}
        write_json(self.vast_dir / "instance.json", record)        # written at once: it can be stopped whatever happens next
        return record, vast

    def offer_gone(self, vast, offer):
        """An offer rented by someone else between Find machines and the click: nothing was rented. The list is searched
        again so the next click finds a machine that is there; answers what to tell."""
        told = (f"{offer['gpu']} at ${_number(offer['dph']):.3f}/h ({offer.get('where') or 'offer ' + str(offer['id'])}) was "
                "rented by someone else just before your click: vast.ai no longer offers it. Nothing was rented and "
                "nothing is billed.")
        try:
            offers, seen = vast.offers()
        except VastError:
            offers = [o for o in self.steps["where"]["data"].get("offers") or [] if str(o["id"]) != str(offer["id"])]
            seen = self.steps["where"]["data"].get("offers_seen")
            told += " The list shows the others found before; Find machines searches again."
        else:
            told += " The list has been searched again: pick another."
        self.where_data(offers=offers, offers_seen=seen, offers_at=now_text())
        return told

    def do_vast_connect(self, _body):
        record = self.saved_instance()
        if not record:
            raise ValueError("No vast.ai instance is rented by this tool: find a machine and rent it.")
        self.set_mode("vast")
        self.connect(self.vast(), record, wait=600)

    def connect(self, vast, record, wait):
        """Waits until vast.ai says the instance runs and its SSH lets this tool's key in."""
        self.disconnect()
        instance_id, started, gone_since = record["id"], time.monotonic(), None
        while True:
            seen = vast.instance(instance_id)
            if seen is None:
                (self.vast_dir / "instance.json").unlink(missing_ok=True)
                self.where_data(instance=None)
                raise ValueError(f"vast.ai no longer has instance {instance_id}: it was destroyed or ended, so it is not "
                                 "billed any more. Find a machine and rent another.")
            status = seen.get("actual_status")
            seconds = int(time.monotonic() - started)
            if status == "running" and beat_remote.ssh_endpoints(seen):
                break
            if status in ("exited", "offline", "unknown"):
                gone_since = gone_since or time.monotonic()
                if time.monotonic() - gone_since > 90:
                    raise ValueError(f"vast.ai reports instance {instance_id} as “{status}” ({seen.get('status_msg') or 'no message'}). "
                                     "It may still be billed: stop renting it, then rent another machine.")
            else:
                gone_since = None
            if time.monotonic() - started > wait:
                raise ValueError(f"Instance {instance_id} was not running within {wait // 60} minutes (vast.ai: "
                                 f"{status or 'being prepared'}, {seen.get('status_msg') or 'no message'}). It is still "
                                 "rented: connect again later, or stop renting it.")
            self.note("where", f"vast.ai: instance {instance_id} is {status or 'being prepared'}"
                               + (f" · {str(seen.get('status_msg')).strip()[:160]}" if seen.get("status_msg") else "")
                               + f" · {seconds} s")
            self.sleep(5)
        key, public = beat_remote.make_ssh_key(self.vast_dir, run=self.run)
        if hasattr(vast, "attach_ssh"):
            vast.attach_ssh(instance_id, public)
        known = self.vast_dir / "known_hosts"
        tried, remote = [], None
        while remote is None:
            for host, port in beat_remote.ssh_endpoints(seen):
                candidate = self.remote_api(host, port, key, known, run=self.run, popen=self.popen)
                if candidate.reachable():
                    remote = candidate
                    break
                tried.append(f"{host}:{port}")
            if remote is None:
                if time.monotonic() - started > wait:
                    raise ValueError(f"Instance {instance_id} runs, but its SSH did not let this tool's key in "
                                     f"(tried {', '.join(sorted(set(tried)))}). It is still rented: connect again, or "
                                     "stop renting it.")
                self.note("where", f"Instance {instance_id} runs; waiting for its SSH to let this tool in… "
                                   f"{int(time.monotonic() - started)} s")
                self.sleep(5)
                seen = vast.instance(instance_id) or seen
        self.remote = remote
        self.studio = Studio(endpoint=lambda: f"http://127.0.0.1:{remote.tunnel()}/mcp")
        offer = record.get("offer") or {}
        record.update(ssh=f"{remote.host}:{remote.port}", gpu=seen.get("gpu_name") or offer.get("gpu"),
                      dph=seen.get("dph_total") or offer.get("dph"))
        write_json(self.vast_dir / "instance.json", record)
        self.where_data(instance=record, credit=_try(lambda: vast.user().get("credit")))
        self.passed("where", {"Runs on": f"vast.ai instance {instance_id}: {record['gpu']}, "
                                         f"{offer.get('vram_gb', '?')} GB, {offer.get('where') or 'location not given'}",
                              "Price": f"${_money(record['dph'])}/h while it is rented (vast.ai's price for it)",
                              "Rented": record["rented"], "SSH": str(remote), "Image": record.get("image", "")},
                    key=f"vast|{instance_id}")

    def stop_renting(self):
        """Destroys the rented instance, at any time, even while a step works on it: vast.ai then stops billing it."""
        with self.lock:
            if self.stopping:
                raise Refused("Stopping the rented machine is already under way.")
            record = self.saved_instance()
            if not record:
                raise Refused("No vast.ai instance is rented by this tool.")
            self.stopping = True
            self.where_data(stopping=True)
        thread = threading.Thread(target=self._stop, args=(record,), daemon=True)
        thread.start()
        return thread

    def _stop(self, record):
        try:
            vast = self.vast()
            answer = vast.destroy(record["id"])
            already_gone = isinstance(answer, dict) and answer.get("already_gone")
            seen = None if already_gone else vast.instance(record["id"])
            (self.vast_dir / "instance.json").unlink(missing_ok=True)
            self.disconnect()
            spent = (time.time() - record.get("rented_epoch", time.time())) / 3600 * _number(record.get("dph") or
                                                                                              (record.get("offer") or {}).get("dph"))
            with self.lock:
                self.steps["where"]["data"].update(instance=None, credit=_try(lambda: vast.user().get("credit")))
                self.reset(ORDER)
                self.set_mode("pc")
                self.steps["where"].update(state="ok", key="pc", error="",
                                          facts={"Runs on": "This PC: its graphics card computes every step"})
                self.steps["where"]["note"] = (
                    f"vast.ai no longer had instance {record['id']} (it was already destroyed or ended), so it has been cleared."
                    if already_gone else
                    f"vast.ai destroyed instance {record['id']}: billing for it stops. It ran since {record.get('rented')}, "
                    f"about ${spent:.2f} at its price." + ("" if seen is None else
                                                          f" vast.ai still lists it as “{seen.get('actual_status')}” for now."))
        except Exception as error:  # noqa: BLE001 - shown word for word
            with self.lock:
                self.steps["where"]["error"] = (f"Stopping instance {record['id']} failed: {error}\nIt may still be billed: "
                                                "click Stop renting again, or destroy it at cloud.vast.ai/instances.")
        finally:
            with self.lock:
                self.stopping = False
                self.steps["where"]["data"]["stopping"] = False

    def on_remote(self):
        if not self.remote:
            raise ValueError("The rented machine is not connected: connect to it in step 0 first.")
        return self.remote

    def studio_open(self):
        """The address YuE2 Studio's own window answers at, for a browser on this PC: the same studio every step drives,
        not a second one. Its server serves its whole window at / beside the MCP at /mcp (the window it shows other
        computers and phones). On a rented machine that is the SSH tunnel to it this tool already holds (opened again
        when it closed), so the request arrives on that machine's own loopback and needs no access key. Not a step: it
        runs at any time, also while a step works."""
        if self.mode == "vast":
            where = f"on the rented machine (through the SSH tunnel to {self.on_remote()})"
            base = f"http://127.0.0.1:{self.remote.tunnel()}/"
        else:
            where = "on this PC"
            base = MCP_URL.rsplit("/mcp", 1)[0] + "/"
        try:
            with urllib.request.urlopen(base, timeout=15) as response:
                page = response.read(1 << 16).decode("utf-8", "replace")
        except (OSError, ValueError) as error:
            raise ValueError(f"YuE2 Studio does not answer {where} at {base} ({getattr(error, 'reason', error)}). "
                             "Start it in step 3 (Studio running), then open it again.") from None
        if "YuE2 Studio" not in page:
            raise ValueError(f"What answers {where} at {base} is not YuE2 Studio's window: {page[:200]!r}")
        return base

    # ------------------------------------------------------------ the LoRA trainer on the rented machine

    def trainer_exe(self):
        return f"{beat_remote.trainer_folder(TRAINER_COMMIT)}/{beat_remote.TRAINER_EXE}"

    def trainer_asset(self):
        """The trainer as the studio answering now counts it (runtime_status training_pack), or None when it cannot say:
        a figure shown beside Start, never a reason for Start to fail."""
        try:
            assets = self.studio.call("runtime_status", {"part": "training_pack"}).get("assets") or []
        except (StudioError, RemoteError, ValueError, OSError, AttributeError):
            return None
        return next((asset for asset in assets if isinstance(asset, dict) and asset.get("id") == "music-train"), None)

    def trainer_note(self, text):
        self.note("trainer", text)

    def machine_libraries(self, roots, lenient=()):
        """Every program and library under roots on the rented machine asked what it links and the machine lacks;
        what has a known package is installed there and asked again. A library still missing is an error, except for
        files under a lenient folder (the studio's bundled media plugins, which link sound and camera libraries the
        studio does not need): those are told. Returns (packages installed, what is told)."""
        try:
            lines = self.on_remote().sh(beat_remote.libraries_script(roots), timeout=1200).splitlines()
        except RemoteError as error:
            raise ValueError(f"Checking the libraries on the rented machine ended early: {error}") from None
        if not any(line.startswith("@@scanned") for line in lines):
            raise ValueError("Checking the libraries on the rented machine ended early. What it printed last:\n"
                             + "\n".join(lines[-8:]))
        installed = [package for line in lines if line.startswith("@@installed") for package in line[11:].split()]
        hard, soft = {}, {}
        for line in lines:
            if line.startswith("@@missing "):
                library, _, file = line[10:].partition(" ")
                lenient_file = any(file.startswith(prefix.rstrip("/") + "/") for prefix in lenient)
                (soft if lenient_file else hard).setdefault(library, []).append(file)

        def told(found):
            return "; ".join(f"{library} (needed by {files[0]}" + (f" and {len(files) - 1} more" if len(files) > 1 else "")
                             + ")" for library, files in sorted(found.items()))
        if hard:
            logs = [line[6:] for line in lines if line.startswith("@@log ")]
            raise ValueError("Libraries missing on the rented machine that Beat Tool has no package for: " + told(hard)
                             + ("\n" + "\n".join(logs) if logs else ""))
        return installed, told(soft)

    def do_trainer(self, _body):
        """The LoRA trainer on the rented machine, before the studio starts there, so the studio starts with it (a studio
        already running without it is stopped, and Start brings it back with it). Whichever way it came, everything
        it links must be on this machine before the step is green."""
        record, how = self.trainer_ready()
        self.trainer_note("Checking that this machine has every library the trainer links…")
        installed, _ = self.machine_libraries([beat_remote.trainer_folder(TRAINER_COMMIT)])
        if installed:
            how += f"; installed what it links here: {', '.join(installed)}"
        note = self.trainer_into_studio()
        facts = {"Trainer": self.trainer_exe(), "How": how,
                 "Built from": f"HOT-Step-CPP {TRAINER_COMMIT[:8]}, the commit YuE2 Studio {REMOTE_VERSION} pins",
                 "CUDA": f"{record.get('cuda', '?')}, architecture sm_{record.get('architectures', '?')}",
                 "ace-train SHA-256": record.get("exe_sha256", ""), "Built": record.get("built", "")}
        if record.get("says"):
            facts["It says"] = record["says"]
        self.passed("trainer", facts, key=f"{record.get('exe_sha256')}|{self.remote_folder}", note=note)

    def trainer_arch(self):
        card = self.gpu or {}
        if not card.get("compute"):
            raise ValueError("Check the GPU step first: the trainer is built for the rented card's CUDA architecture.")
        if float(card.get("vram_gb") or 0) < TRAINER_VRAM_GB:
            raise ValueError(f"The rented card ({card.get('name')}) has {card.get('vram_gb')} GB of video memory; YuE2 "
                             f"Studio's trainer needs about {TRAINER_VRAM_GB} GB. Rent a card with more.")
        return str(int(round(float(card["compute"]) * 10)))

    @staticmethod
    def said_json(lines, what):
        """The @@json record a remote script ends with, or its @@error and @@log lines as the error."""
        errors = [line[8:] for line in lines if line.startswith("@@error ")]
        logs = [line[6:] for line in lines if line.startswith("@@log ")]
        records = [line[7:] for line in lines if line.startswith("@@json ")]
        if errors or not records:
            told = errors or [f"The rented machine ended {what} without a record of it. What it printed last:"]
            raise ValueError("\n".join(told) + "\n\n" + "\n".join(logs or [line for line in lines if line.strip()][-10:]))
        return json.loads(records[-1])

    def trainer_ready(self):
        """The trainer on the rented machine: the one already there if its program matches its build record; else the
        one built on an earlier rented machine for the same CUDA architecture, sent from this PC and checked; else built
        there now (about a quarter of an hour), and a copy brought home, checked, for the next machine."""
        remote = self.on_remote()
        arch = self.trainer_arch()
        folder = beat_remote.trainer_folder(TRAINER_COMMIT)
        out = remote.sh(beat_remote.trainer_found_script(folder), timeout=120)
        record, exe_now = {}, None
        for line in out.splitlines():
            key, _, value = line.strip().partition(" ")
            if key == "@@record":
                record = _try(lambda: json.loads(value)) or {}
            elif key == "@@exe":
                exe_now = value
        if (record.get("commit") == TRAINER_COMMIT and record.get("architectures") == arch
                and exe_now and exe_now == record.get("exe_sha256")):
            return record, "already on the rented machine; its program matches its build record (SHA-256)"
        name = f"trainer-{TRAINER_COMMIT[:8]}-sm{arch}"
        cache, cache_record = self.vast_dir / f"{name}.tar.gz", self.vast_dir / f"{name}.json"
        saved = (_try(lambda: read_json(cache_record)) or {}) if cache.is_file() else {}
        if saved.get("tar_sha256") and sha256(cache) == saved["tar_sha256"]:
            self.trainer_note(f"Sending the trainer built on an earlier rented machine ({size_text(cache.stat().st_size)}) "
                              "to this one…")
            tarball = f"{folder}.tar.gz"
            remote.put(cache, tarball, saved["tar_sha256"])
            self.trainer_note("Unpacking the trainer there and checking it…")
            try:
                lines = remote.sh(beat_remote.trainer_unpack_script(tarball, folder, saved["exe_sha256"]), timeout=600).splitlines()
            except RemoteError as error:
                lines = str(error).splitlines()
            return self.said_json(lines, "unpacking the trainer"), f"sent from this PC ({cache.name}), checked by SHA-256"
        self.trainer_note("Building the trainer on the rented machine…")
        started = time.monotonic()
        lines = []

        def on_line(line):
            lines.append(line)
            if line.startswith("@@ "):
                self.trainer_note(line[3:] + f" · {int(time.monotonic() - started) // 60} min")
        try:
            remote.stream(beat_remote.trainer_build_script(TRAINER_REPO, TRAINER_COMMIT, arch, folder), on_line,
                          timeout=5400)
        except RemoteError as error:
            if not any(line.startswith("@@error ") for line in lines):
                raise ValueError(str(error)) from None
        record = self.said_json(lines, "building the trainer")
        minutes = max(1, round((time.monotonic() - started) / 60))
        self.trainer_note(f"Built in {minutes} min. Bringing a copy home for the next rented machine…")
        self.vast_dir.mkdir(parents=True, exist_ok=True)
        digest = remote.get(f"{folder}.tar.gz", cache, timeout=1800)      # through its own .part, checked on both ends
        write_json(cache_record, {**record, "tar_sha256": digest, "tarball": cache.name, "brought_home": now_text()})
        return record, f"built on this rented machine in {minutes} min; a checked copy is kept here as vast\\{cache.name}"

    def trainer_into_studio(self):
        """The studio there runs with the trainer, or is stopped so Start brings it back with it."""
        studio_exe = f"{self.remote_folder}/{REMOTE_EXE}"
        out = self.on_remote().sh(beat_remote.studio_trainer_script(studio_exe, self.trainer_exe()), timeout=60)
        if "@@none" in out:
            return "Studio running starts YuE2 Studio with this trainer (YUE_TRAIN_BIN), so its LoRA page can train."
        if "@@uses no" not in out:
            asset = self.trainer_asset()
            if asset is not None and not asset.get("installed"):
                raise ValueError("YuE2 Studio runs with YUE_TRAIN_BIN naming the trainer, but its training pack does not "
                                 f"count it installed (runtime_status: {asset}).")
            return "The YuE2 Studio running there already uses this trainer."
        self.trainer_note("YuE2 Studio runs without the trainer: stopping it, so Studio running brings it back with it…")
        self.on_remote().sh(beat_remote.stop_studio_script(studio_exe), timeout=120)
        with self.lock:
            self.reset(self.later("trainer"))
        return ("YuE2 Studio was running without the trainer, so it was stopped: Studio running starts it again with it "
                "(then Models and the engine as before).")

    # ------------------------------------------------------------ setup

    def do_gpu(self, _body):
        if self.mode == "vast":
            return self.gpu_remote()
        try:
            done = self.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise ValueError("nvidia-smi is not on this PC: no NVIDIA driver is installed.") from None
        except subprocess.TimeoutExpired:
            raise ValueError("nvidia-smi did not answer within 30 seconds.") from None
        if done.returncode != 0:
            raise ValueError(f"nvidia-smi ended with code {done.returncode}: {(done.stderr or done.stdout).strip()}")
        cards = []
        for line in done.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 3:
                cards.append({"name": parts[0], "vram_gb": round(int(parts[1]) / 1024, 1), "driver": parts[2]})
        if not cards:
            raise ValueError(f"nvidia-smi lists no graphics card: {done.stdout.strip() or '(nothing)'}")
        self.gpu = cards[0]
        facts = {"GPU": self.gpu["name"], "VRAM": f"{self.gpu['vram_gb']} GB", "Driver": self.gpu["driver"]}
        if len(cards) > 1:
            facts["Other cards"] = ", ".join(card["name"] for card in cards[1:])
        self.passed("gpu", facts, key=f"{self.gpu['name']}|{self.gpu['driver']}")

    # ------------------------------------------------------------ the setup steps on a rented machine

    def gpu_remote(self):
        """The rented machine's card, from nvidia-smi there, and what the studio needs of it: a driver and a compute
        capability its CUDA 13 build runs on, the CUDA 13 libraries in the image, and disk for everything it downloads
        (free space plus what Beat Tool's folder there already holds, so a check after the downloads asks the same)."""
        out = self.on_remote().sh(beat_remote.GPU_SCRIPT, timeout=120)
        cards, seen = [], {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("@@"):
                key, _, value = line[2:].partition(" ")
                seen[key] = value.strip()
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 4:
                try:
                    cards.append({"name": parts[0], "vram_gb": round(int(float(parts[1])) / 1024, 1), "driver": parts[2],
                                  "compute": beat_remote.compute_capability(parts[3])})
                except ValueError:
                    continue
        if not cards:
            raise ValueError("nvidia-smi on the rented machine lists no graphics card: " + (out.strip()[-300:] or "(nothing)"))
        card = cards[0]
        libs = seen.get("cuda", "").split()
        lacking = [lib for lib in ("libcudart.so.13", "libcublas.so.13", "libcufft.so.12") if lib not in libs]
        disk = int(seen["disk"]) if seen.get("disk", "").isdigit() else None
        ours = int(seen["ours"]) if seen.get("ours", "").isdigit() else 0
        problems = []
        if beat_remote.driver_major(card["driver"]) < beat_remote.MIN_DRIVER:
            problems.append(f"its driver {card['driver']} is older than {beat_remote.MIN_DRIVER}, which the studio's CUDA 13 "
                            "build needs")
        if card["compute"] < beat_remote.MIN_COMPUTE:
            problems.append(f"its card's compute capability {card['compute']} is below {beat_remote.MIN_COMPUTE}")
        if lacking:
            problems.append("its image holds no " + ", ".join(lacking))
        if disk is not None and disk + ours < beat_remote.MIN_FREE_GB:
            problems.append(f"only {disk} GB of disk is free" + (f" beside the {ours} GB Beat Tool's folder holds" if ours else "")
                            + f"; the studio, its models, the trainer and its parts want about {beat_remote.MIN_FREE_GB} GB")
        if problems:
            raise ValueError("This rented machine cannot run the studio: " + "; ".join(problems)
                             + ". Stop renting it and rent another.")
        self.gpu = card
        facts = {"GPU": card["name"], "VRAM": f"{card['vram_gb']} GB", "Driver": card["driver"],
                 "Compute capability": str(card["compute"]), "CUDA 13 libraries": ", ".join(libs),
                 "Disk free": (f"{disk} GB" + (f" (Beat Tool's folder holds {ours} GB more)" if ours else "")
                               if disk is not None else "not reported"),
                 "System": seen.get("os", "")}
        if len(cards) > 1:
            facts["Other cards"] = ", ".join(other["name"] for other in cards[1:])
        self.passed("gpu", facts, key=f"vast|{card['name']}|{card['driver']}")

    # ------------------------------------------------------------ CUDA (and Vulkan), before the studio

    def do_cuda(self, _body):
        """CUDA tested by using it (beat_remote.CUDA_PROBE) before anything of the studio is downloaded: the driver
        starts CUDA, the card takes, fills and gives back 256 MB. On a rented machine also the CUDA 13 libraries the
        studio's tools load, and Vulkan, which its song engine computes through there."""
        if self.mode == "vast":
            return self.cuda_remote()
        try:
            done = self.run([sys.executable, "-I", "-c", beat_remote.CUDA_PROBE], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, creationflags=beat_remote.NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ValueError("The CUDA test did not finish within 120 seconds: the card or its driver does not answer.") from None
        found = self.probe_answer((done.stdout or "").splitlines(), done.stderr)
        self.passed("cuda", self.cuda_facts(found), key=f"cuda|{found.get('name')}|{found.get('driver_cuda')}")

    def probe_answer(self, lines, stderr=""):
        """What CUDA_PROBE found, or its error with how far it got."""
        records = [line.strip()[7:] for line in lines if line.strip().startswith("@@json ")]
        if not records:
            said = "\n".join([line for line in lines if line.strip()][-8:] + (stderr or "").strip().splitlines()[-8:])
            raise ValueError(f"The CUDA test on {self.here()} ended without an answer. It printed:\n{said or '(nothing)'}")
        found = json.loads(records[-1])
        if found.get("error"):
            got = {key: value for key, value in found.items() if key in ("name", "driver_cuda", "compute", "vram_gb")}
            raise ValueError(f"CUDA does not work on {self.here()}: {found['error']}"
                             + (f"\nIt got as far as: {json.dumps(got, ensure_ascii=False)}" if got else "")
                             + ("\nStop renting this machine and rent another." if self.mode == "vast" else ""))
        return found

    @staticmethod
    def cuda_facts(found):
        facts = {"Card": f"{found.get('name')} · compute capability {found.get('compute')} · {found.get('vram_gb')} GB",
                 "Driver's CUDA": found.get("driver_cuda", ""), "On the card": found.get("memory", "")}
        for library, said in (found.get("runtime") or {}).items():
            facts[library] = said
        return facts

    def here(self):
        return "the rented machine" if self.mode == "vast" else "this PC"

    def vulkan_problem(self, devices, seen, vulkan):
        """Why Vulkan found no NVIDIA card on the rented machine, told apart: a machine without the card's Vulkan driver
        is one to stop renting; a driver that is there but cannot start is not the machine's fault."""
        others = ", ".join(device.get("deviceName", "?") for device in devices)
        start = ("Vulkan finds no NVIDIA card on the rented machine" + (f", only {others}" if others else "")
                 + ". The studio's song engine computes through Vulkan there, so it would run on the processor.\n")
        printed = "\nWhat vulkaninfo printed last:\n" + "\n".join(vulkan[-8:])
        if seen.get("glx", "0") in ("", "0"):
            why = (f" Beat Tool could not add it: {seen['nvglfail']}." if seen.get("nvglfail") else "")
            return (start + "The host gave the machine only the compute part of the NVIDIA driver, not its Vulkan part "
                            f"(libGLX_nvidia).{why} Stop renting it and rent another." + printed)
        if seen.get("nvgl"):
            start += f"Beat Tool added the driver's Vulkan part ({seen['nvgl']}), which the host had left out.\n"
        missing = seen.get("glxmissing", "").split() + ([] if seen.get("egl", "1") not in ("", "0") else ["libEGL.so.1"])
        if missing:
            return (start + f"The card's Vulkan driver is there, but what it needs is missing: {', '.join(missing)}. Beat "
                            "Tool installs these; here they did not install. The machine itself is not at fault." + printed)
        driver = (self.gpu or {}).get("driver", "?")
        return (start + f"The card's Vulkan driver (NVIDIA {driver}) is there and has what it links, but refuses to start. "
                        f"Vulkan driver file: {seen.get('icd') or 'none'}." + printed)

    def cuda_remote(self):
        """The CUDA test on the rented machine with the CUDA 13 libraries its image carries, then Vulkan: vulkaninfo must
        list the NVIDIA card, not only the processor's software device, or the song engine would compute on the
        processor. A machine that fails this is one to stop renting, before anything is downloaded onto it."""
        lines = []

        def on_line(line):
            lines.append(line)
            if line.startswith("@@ "):
                self.note("cuda", line[3:])
        try:
            self.on_remote().stream(beat_remote.compute_script(), on_line, timeout=900)
        except RemoteError as error:
            if not any(line.startswith(("@@error ", "@@json ")) for line in lines):
                raise ValueError(str(error)) from None
        errors = [line[8:] for line in lines if line.startswith("@@error ")]
        if errors:
            raise ValueError("\n".join(errors + [line[6:] for line in lines if line.startswith("@@log ")]))
        found = self.probe_answer(lines)
        if _number(found.get("driver_cuda")) < 13:
            raise ValueError(f"The rented machine's driver runs CUDA {found.get('driver_cuda')}; the studio's tools there are "
                             "CUDA 13 builds. Stop renting it and rent another.")
        seen = {}
        for line in lines:
            if line.startswith(("@@icd ", "@@glx ", "@@glxmissing ", "@@egl ", "@@nvgl ", "@@nvglfail ")):
                key, _, value = line[2:].partition(" ")
                seen[key] = value.strip()
        vulkan, inside_summary = [], False
        for line in lines:
            if line.startswith("@@vulkan-begin"):
                inside_summary = True
            elif line.startswith("@@vulkan-end"):
                inside_summary = False
            elif inside_summary:
                vulkan.append(line)
        devices = beat_remote.vulkan_devices(vulkan)
        cards = [device for device in devices if device.get("vendorID", "").lower() == "0x10de"
                 or "NVIDIA" in device.get("deviceName", "").upper()]
        if not cards:
            raise ValueError(self.vulkan_problem(devices, seen, vulkan))
        if len({device.get("deviceUUID") or device.get("deviceName") for device in cards}) < len(cards):
            raise ValueError("Vulkan lists the one NVIDIA card more than once (two files name its driver), so the song "
                             "engine could take it for two cards. Vulkan driver file: " + (seen.get("icd") or "none"))
        card = cards[0]
        facts = self.cuda_facts(found)
        facts["Vulkan"] = (f"{card.get('deviceName')} · {card.get('driverName', 'driver')} {card.get('driverInfo', '')}".strip()
                           + f" · Vulkan {card.get('apiVersion', '?')}")
        facts["Vulkan driver file"] = seen.get("icd") or "none"
        if seen.get("nvgl"):
            facts["Vulkan part of the driver"] = f"added by Beat Tool ({seen['nvgl']}): the host had left it out"
        self.passed("cuda", facts, key=f"vast|{found.get('name')}|{found.get('driver_cuda')}|{card.get('deviceName')}")

    def remote_studio_found(self):
        """The studio unpacked on the rented machine, if one is: its folder, its record, and its program's SHA-256."""
        out = self.on_remote().sh(beat_remote.studio_found_script(REMOTE_NAME + "_from_github_", MARKER), timeout=120)
        folder = exe = None
        record = {}
        for line in out.splitlines():
            key, _, value = line.strip().partition(" ")
            if key == "@@folder":
                folder = value
            elif key == "@@record":
                try:
                    record = json.loads(value)
                except ValueError:
                    record = {}
            elif key == "@@exe":
                exe = value
        return (folder, record, exe) if folder else None

    def studio_remote(self):
        """The Linux build of the studio, downloaded and unpacked on the rented machine itself (nothing of its 214 MB
        passes this PC), checked there against the pinned size and SHA-256."""
        remote = self.on_remote()
        found = self.remote_studio_found()
        if found:
            folder, record, exe = found
            if record.get("sha256") != LINUX_SHA256:
                raise ValueError(f"{folder} on the rented machine was unpacked from another AppImage (SHA-256 "
                                 f"{record.get('sha256')}), not the pinned {LINUX_ASSET}.")
            if not exe or exe != record.get("exe_sha256"):
                raise ValueError(f"The studio's program on the rented machine changed since it was unpacked: its SHA-256 "
                                 f"is {exe}, it was {record.get('exe_sha256')}.")
            self.remote_folder, self.remote_found = folder, PurePosixPath(folder).name
            self.passed("studio", {"Folder": self.remote_found, "Unpacked from": LINUX_ASSET,
                                   "Checked": f"{REMOTE_EXE} SHA-256, on the rented machine"}, key=f"vast|{folder}")
            return
        folder = f"{beat_remote.REMOTE_ROOT}/{REMOTE_NAME}_from_github_{stamp()}"
        said, errors = [], []

        def on_line(line):
            if line.startswith("@@error "):
                errors.append(line[8:])
            elif line.startswith("@@json "):
                said.append(line[7:])
            elif line.startswith("@@ "):
                self.note("studio", line[3:])
        self.note("studio", "Preparing the rented machine…")
        try:
            remote.stream(beat_remote.studio_script(LINUX_URL, LINUX_ASSET, LINUX_BYTES, LINUX_SHA256, folder, MARKER),
                          on_line, timeout=1800)
        except RemoteError as error:
            raise ValueError("\n".join(errors) or str(error)) from None
        if not said:
            raise ValueError("The rented machine finished unpacking the studio but wrote no record of it.")
        record = json.loads(said[-1])
        self.remote_folder, self.remote_found = folder, PurePosixPath(folder).name
        self.passed("studio", {"Folder": self.remote_found, "Files": f"{int(record.get('files') or 0):,}",
                               "Downloaded": f"{LINUX_BYTES:,} bytes, SHA-256 matches the pin (checked on the rented machine)",
                               "The AppImage": "deleted after the checked unpack"}, key=f"vast|{folder}")

    def remote_running(self):
        """Every YuE2 Studio program running on the rented machine: its PID, since when, and the file it runs from."""
        found = []
        for line in self.on_remote().sh(beat_remote.RUNNING_SCRIPT, timeout=60).splitlines():
            pid, _, rest = line.strip().partition("|")
            since, _, path = rest.partition("|")
            if pid.isdigit():
                found.append({"pid": int(pid), "since": since, "path": path})
        return found

    def remote_log_size(self, log):
        out = self.on_remote().sh(f"stat -c %s {shlex.quote(log)} 2>/dev/null || echo 0", timeout=60).strip()
        return int(out) if out.isdigit() else 0

    def remote_probe(self, pid, log, offset):
        """Whether the studio started there still lives, and the last line it printed since the click."""
        q = shlex.quote
        out = self.on_remote().sh(
            beat_remote.STUDIO_PIDS +
            f'if kill -0 {int(pid)} 2>/dev/null || [ -n "$(studio_pids)" ]; then echo "@@alive yes"; '
            f'else echo "@@alive no"; fi\n'
            f'if [ -f {q(log)} ]; then tail -c +{int(offset or 0) + 1} {q(log)} | tail -n 1 | cut -c1-300 | '
            f'sed "s/^/@@last /"; fi\n', timeout=60)
        alive, last = True, ""
        for line in out.splitlines():
            if line.startswith("@@alive "):
                alive = line.split()[1] == "yes"
            elif line.startswith("@@last "):
                last = line[7:].strip()
        return alive, last

    def remote_start_logs(self, log, offset):
        """Where to read why Start failed on the rented machine: what the studio printed since the click."""
        if offset is None:
            lines = ["This click started no studio, so no studio output was captured."]
        else:
            try:
                tail = self.remote.sh(f"tail -c +{int(offset) + 1} {shlex.quote(log)} | tail -n 8", timeout=60).strip().splitlines()
            except (RemoteError, AttributeError):
                tail = []
            lines = ["What YuE2 Studio printed since the click:" + ("\n  " + "\n  ".join(line[:300] for line in tail) if tail else " nothing.")]
        lines.append(f"Log to read, on the rented machine: {log}")
        lines.append("The studio's own logs are in its data folder (data/logs) beside its program there.")
        return "\n".join(lines)

    def start_remote(self):
        """The Start step on the rented machine: starts the studio unpacked there under a virtual display, unless one
        runs, and waits until its MCP answers through the SSH tunnel. A failure names the log to read."""
        folder = self.remote_folder
        if not folder:
            raise ValueError("The studio is not unpacked on the rented machine in this connection: click the YuE2 Studio "
                             "step first.")
        remote = self.on_remote()
        exe, log = f"{folder}/{REMOTE_EXE}", f"{folder}/{START_LOG}"
        clicked, offset, pid = time.monotonic(), None, None
        try:
            # the studio must run with the LoRA trainer (YUE_TRAIN_BIN), or its LoRA page cannot train: one running
            # without it is stopped here and started again with it, never left for later
            if "@@uses no" in remote.sh(beat_remote.studio_trainer_script(exe, self.trainer_exe()), timeout=60):
                self.start_phase("starting…", "YuE2 Studio runs there without the LoRA trainer: stopping it, to start it "
                                              "again with it…")
                remote.sh(beat_remote.stop_studio_script(exe), timeout=120)
            self.start_phase("starting…", "Asking whether a YuE2 Studio on the rented machine already answers…")
            try:
                info, last = self.studio.hello(), None
            except StudioError as error:
                info, last = None, error
            self.start_phase("starting…", "Looking for a YuE2 Studio already running on the rented machine…")
            running = self.remote_running()
            own = [p for p in running if p["path"] == exe]
            others = [p for p in running if p not in own]
            if own:
                pid = own[0]["pid"]
                how = f"already running, not started a second time: PID {pid}, running since {own[0]['since'] or '?'}"
                offset = self.remote_log_size(log)
            elif info is None and others:
                raise ValueError("Another YuE2 Studio is running on the rented machine, not the copy unpacked there, and "
                                 "its MCP does not answer: " + "; ".join(f"PID {p['pid']} from {p['path']}" for p in others)
                                 + ". A second one was not started.")
            elif info is None:
                self.start_phase("starting…", f"Starting {exe} on the rented machine…")
                out = remote.sh(beat_remote.launch_script(folder, log, trainer=self.trainer_exe()), timeout=120)
                started = re.search(r"@@pid (\d+) (\d+)", out)
                if not started:
                    raise ValueError("The rented machine did not say whether the studio started: " + out.strip()[-300:])
                pid, offset = int(started.group(1)), int(started.group(2))
                how = f"PID {pid}, started by this click"
            else:
                how = "something already answers on the rented machine's MCP port"
            deadline, polled, shown = clicked + self.start_wait, 0, ""
            while info is None:
                seconds = int(time.monotonic() - clicked)
                if polled % 4 == 0:           # one SSH call in four rounds, not one a second
                    alive, shown = self.remote_probe(pid, log, offset)
                    if not alive:
                        raise ValueError(f"YuE2 Studio (PID {pid}) closed {seconds} s after the click, before its MCP answered.")
                polled += 1
                if time.monotonic() > deadline:
                    raise ValueError(f"YuE2 Studio's MCP did not answer within {self.start_wait} seconds, while the studio "
                                     f"kept running ({how}). The MCP's last answer: {last}")
                facts = {"Studio": how}
                if shown:
                    facts["It printed last"] = shown
                facts["Studio output log"] = log
                facts["MCP so far"] = str(last)
                self.start_phase("waiting for the MCP…", f"Waiting for YuE2 Studio's MCP on the rented machine · {seconds} s", facts)
                self.sleep(1)
                try:
                    info = self.studio.hello()
                except StudioError as error:
                    last = error
            seconds = int(time.monotonic() - clicked)
            self.start_phase("checking…", f"The MCP answered after {seconds} s: checking it is version {REMOTE_VERSION}, "
                                          "keeping its data in the folder there…")
            version = info.get("version")
            if version != REMOTE_VERSION:
                raise ValueError(f"The YuE2 Studio answering on the rented machine is version {version}; this tool is pinned "
                                 f"to {REMOTE_VERSION} there. Moving the pin is your choice: REMOTE_TAG in beat_tool.py.")
            status = self.studio.call("models_status")
            data = status.get("data_directory")
            if not data or not str(data).replace("\\", "/").startswith(folder + "/"):
                raise ValueError(f"The YuE2 Studio answering on the rented machine keeps its data in {data}, not in "
                                 f"{folder}: it is another copy ({how}).")
            if status.get("portable") is not True:
                raise ValueError("The YuE2 Studio answering is not a portable copy (models_status portable is "
                                 f"{status.get('portable')}), so its models would go outside the folder it was unpacked in.")
            window = self.studio.call("studio_status").get("window_open")
            self.version = version
            trainer = self.trainer_asset()
            if trainer is not None and not trainer.get("installed"):
                raise ValueError(f"YuE2 Studio runs, but does not count the LoRA trainer installed (runtime_status: "
                                 f"{trainer}): it was not started with YUE_TRAIN_BIN naming {self.trainer_exe()}. Click "
                                 "LoRA trainer again, then Studio running.")
            self.passed("start", {"Version": version, "MCP": "on the rented machine, through an SSH tunnel to this PC",
                                  "Data and models": data, "Window open": "yes" if window else "no", "Studio": how,
                                  "Connected after": f"{seconds} s", "Studio output log": log,
                                  "LoRA trainer": ("not asked" if trainer is None else
                                                   f"installed: {self.trainer_exe()} (YUE_TRAIN_BIN)")},
                        key=f"vast|{version}|{data}",
                        note="YuE2 Studio was already running there, so it was not started a second time." if own else "")
        except Exception as error:  # noqa: BLE001 - every failure names where to read why
            told = error if isinstance(error, (ValueError, StudioError)) else f"{type(error).__name__}: {error}"
            raise ValueError(f"{told}\n\n{self.remote_start_logs(log, offset)}") from None
        finally:
            with self.lock:
                self.steps["start"]["data"].pop("phase", None)

    def engine_remote(self):
        """The engine's check on the rented machine. The studio ships its CUDA song engine for Windows only (hardware.rs
        at v3.8.4), so there the engine computes on the NVIDIA card through Vulkan; its log must name that card."""
        restarted, note = self.engine_backend("vulkan")
        self.wait_engine(restart=not restarted)
        try:
            cards = self.studio.call("system_gpus").get("cards") or []
        except StudioError:
            cards = []
        logs = self.studio.call("engine_logs", text_ok=True)
        lines = logs.get("lines", []) if isinstance(logs, dict) else str(logs).splitlines()
        name = (self.gpu or {}).get("name") or (cards[0].get("name", "") if cards else "")
        software = [line for line in lines if re.search(r"llvmpipe|lavapipe|swiftshader", line, re.I)]
        if software:
            raise ValueError("The engine found a software Vulkan device, not the graphics card:\n"
                             + "\n".join(line.strip() for line in software[:3]))
        hits = [line for line in lines if "ggml_vulkan" in line or "Vulkan0" in line or "Vulkan backend" in line
                or ("Vulkan" in line and ("NVIDIA" in line.upper() or (name and name in line)))]
        if not hits:
            raise ValueError("The engine runs, but its log names no NVIDIA Vulkan device, so the graphics card is not "
                             "confirmed. The engine log ends:\n" + "\n".join(lines[-8:]))
        self.passed("engine", {"Backend": "vulkan (engine_options_get)", "Card": name,
                               "Engine log": "\n".join(line.strip() for line in hits[:3])}, key=f"vulkan|{name}", note=note)

    def studio_found(self):
        for folder in sorted(self.folder.glob(STUDIO_NAME + "_from_github_date*"), key=lambda p: p.name, reverse=True):
            if (folder / MARKER).is_file():
                return folder
        return None

    def check_unpacked(self, folder):
        try:
            record = read_json(folder / MARKER)
        except (OSError, ValueError) as error:
            raise ValueError(f"{folder.name}\\{MARKER} cannot be read: {error}") from None
        if record.get("zip_sha256") != STUDIO_ZIP_SHA256:
            raise ValueError(f"{folder.name} was unpacked from another zip (SHA-256 {record.get('zip_sha256')}), "
                             f"not the pinned {STUDIO_ZIP}.")
        exe = folder / record.get("exe", "")
        if not exe.is_file():
            raise ValueError(f"{exe} is missing.")
        now = sha256(exe)
        if now != record.get("exe_sha256"):
            raise ValueError(f"{exe.name} changed since it was unpacked: its SHA-256 is {now}, it was "
                             f"{record.get('exe_sha256')}. An update may have replaced it.")
        return exe

    def do_studio(self, _body):
        if self.mode == "vast":
            return self.studio_remote()
        found = self.studio_found()
        if found:
            self.exe = self.check_unpacked(found)
            self.passed("studio", {"Folder": found.name, "Unpacked from": STUDIO_ZIP, "Checked": f"{STUDIO_EXE} SHA-256"},
                        key=str(self.exe))
            return
        part = self.folder / (STUDIO_ZIP + ".part")
        self.note("studio", f"Downloading {STUDIO_ZIP}…")
        size, digest = self.fetch(STUDIO_ZIP_URL, part, lambda done: self.note(
            "studio", f"Downloading {STUDIO_ZIP}: {size_text(done)} of {size_text(STUDIO_ZIP_BYTES)}"))
        if size != STUDIO_ZIP_BYTES or digest != STUDIO_ZIP_SHA256:
            part.unlink(missing_ok=True)
            raise ValueError(f"The download does not match the pin: {size:,} bytes, SHA-256 {digest}; expected "
                             f"{STUDIO_ZIP_BYTES:,} bytes, SHA-256 {STUDIO_ZIP_SHA256}. It was deleted; nothing was unpacked.")
        archive = part.with_name(STUDIO_ZIP)
        os.replace(part, archive)
        folder = self.folder / f"{STUDIO_NAME}_from_github_{stamp()}"
        if folder.exists():
            raise ValueError(f"{folder.name} already exists without a finished unpack. Remove it, then click again.")
        self.note("studio", f"Unpacking into {folder.name}…")
        count = unpack(archive, folder)
        exes = sorted(folder.glob(STUDIO_EXE)) or sorted(folder.glob("*/" + STUDIO_EXE))
        if not exes:
            raise ValueError(f"The zip holds no {STUDIO_EXE}; its top level is: "
                             + ", ".join(sorted(p.name for p in folder.iterdir())[:12]))
        exe = exes[0]
        write_json(folder / MARKER, {"zip": STUDIO_ZIP, "url": STUDIO_ZIP_URL, "zip_sha256": STUDIO_ZIP_SHA256,
                                     "exe": exe.relative_to(folder).as_posix(), "exe_sha256": sha256(exe),
                                     "files": count, "unpacked": now_text()})
        archive.unlink()
        self.exe = exe
        self.passed("studio", {"Folder": folder.name, "Files": f"{count:,}", "Downloaded": f"{size:,} bytes, SHA-256 matches the pin",
                               "The zip": "deleted after the checked unpack"}, key=str(exe))

    def start_phase(self, phase, note, facts=None):
        """What Start is doing now, on its card: the phase on the badge, one line, and what the studio is doing."""
        with self.lock:
            self.steps["start"].update(note=note, facts=facts or {})
            self.steps["start"]["data"]["phase"] = phase

    def studios_running(self):
        """Every YuE2 Studio process on this PC: its PID, since when, and the file it runs from."""
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | Where-Object Name -eq '"
                  + STUDIO_EXE + "' | ForEach-Object { '{0}|{1:HH:mm:ss}|{2}' -f $_.ProcessId, $_.CreationDate, $_.ExecutablePath }")
        try:
            done = self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Windows could not be asked which programs run, so a YuE2 Studio already running could not "
                             f"be ruled out and none was started: {error}") from None
        if done.returncode != 0:
            raise ValueError(f"Asking Windows which programs run ended with code {done.returncode}, so none was started: "
                             f"{(done.stderr or done.stdout).strip()[:400]}")
        found = []
        for line in done.stdout.splitlines():
            pid, _, rest = line.strip().partition("|")
            since, _, path = rest.partition("|")
            if pid.isdigit():
                found.append({"pid": int(pid), "since": since, "path": path})
        return found

    def studio_alive(self, pid):
        done = self.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30)
        return f'"{pid}"' in done.stdout

    @staticmethod
    def described(processes):
        return "; ".join(f"PID {p['pid']}, running since {p['since'] or '?'} from {p['path'] or 'a file Windows does not name'}"
                         for p in processes)

    @staticmethod
    def start_logs(log, offset):
        """Where to read why Start failed: what the studio printed since the click, and its own logs."""
        if offset is None:
            lines = [f"This click started no studio, so no studio output was captured. A studio Start launches writes "
                     f"its output to {log}."]
        else:
            tail = printed(log, offset, 8)
            lines = ["What YuE2 Studio printed since the click:" + ("\n  " + "\n  ".join(tail) if tail else " nothing."),
                     f"Log to read: {log}"]
        if (log.parent / "data" / "logs").is_dir():
            lines.append(f"The studio's own logs: {log.parent / 'data' / 'logs'}")
        return "\n".join(lines)

    def do_start(self, _body):
        """Starts the studio unpacked here, unless a studio already runs, and waits until its MCP answers. The card
        shows each moment: starting, waiting for the MCP with the seconds and what the studio is doing, connected. A
        failure names the log to read."""
        if self.mode == "vast":
            return self.start_remote()
        exe, clicked, since = self.exe, time.monotonic(), time.time() - 1
        log, offset = exe.parent / START_LOG, None          # offset: where this click's studio output begins in the log
        try:
            self.start_phase("starting…", f"Asking {MCP_URL} whether a YuE2 Studio already answers…")
            try:
                info, last = self.studio.hello(), None
            except StudioError as error:
                info, last = None, error
            self.start_phase("starting…", f"Looking for a {STUDIO_EXE} already running on this PC…")
            running = self.studios_running()
            own = [p for p in running if same_file(p["path"], exe)]
            others = [p for p in running if p not in own]
            if own:
                how = f"already running, not started a second time: {self.described(own)}"
                offset = log.stat().st_size if log.is_file() else None     # a studio an earlier click started writes on here
            elif info is None and others:
                raise ValueError(f"Another YuE2 Studio is running, not the copy unpacked here, and its MCP does not answer: "
                                 f"{self.described(others)}. A second one was not started: close it, then click Start again.")
            elif info is None:
                self.start_phase("starting…", f"Starting {exe}…")
                with open(log, "ab") as out:
                    out.write(f"\n=== {now_text()} Beat Tool starts {exe}\n".encode("utf-8"))
                    offset = out.tell()
                    # A console without a window, not none: YuE2-Studio.exe is a console program, and without a console
                    # every console program it runs about once a second would open, and close, a window of its own.
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    self.process = self.popen([str(exe)], cwd=str(exe.parent), creationflags=flags, close_fds=True,
                                              stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
                how = f"PID {self.process.pid}, started by this click"
            else:
                how = (f"another copy answers: {self.described(others)}" if others
                       else f"no {STUDIO_EXE} runs; something else answers on {MCP_URL}")
            deadline = clicked + self.start_wait
            while info is None:
                seconds = int(time.monotonic() - clicked)
                if own:
                    if not self.studio_alive(own[0]["pid"]):
                        raise ValueError(f"YuE2 Studio (PID {own[0]['pid']}) closed {seconds} s after the click, before its "
                                         "MCP answered.")
                elif (code := self.process.poll()) is not None:
                    raise ValueError(f"YuE2 Studio closed (exit code {code}) {seconds} s after the click, before its MCP "
                                     "answered.")
                if time.monotonic() > deadline:
                    raise ValueError(f"YuE2 Studio's MCP did not answer within {self.start_wait} seconds, while the studio "
                                     f"kept running ({how}). The MCP's last answer: {last}")
                facts = {"Studio": how, "In its folder since the click": written_since(exe.parent, since, log)}
                if offset is not None:
                    facts["It printed last"] = (printed(log, offset, 1) or ["nothing yet"])[-1]
                    facts["Studio output log"] = str(log)
                facts["MCP so far"] = str(last)
                self.start_phase("waiting for the MCP…", f"Waiting for YuE2 Studio's MCP at {MCP_URL} · {seconds} s", facts)
                self.sleep(1)
                try:
                    info = self.studio.hello()
                except StudioError as error:
                    last = error
            seconds = int(time.monotonic() - clicked)
            self.start_phase("checking…", f"The MCP answered after {seconds} s: checking it is version {STUDIO_VERSION}, "
                                          "keeping its data in the folder here…")
            version = info.get("version")
            if version != STUDIO_VERSION:
                raise ValueError(f"The YuE2 Studio answering on {MCP_URL} is version {version}; this tool is pinned to "
                                 f"{STUDIO_VERSION}. Moving the pin is your choice: STUDIO_TAG in beat_tool.py.")
            status = self.studio.call("models_status")
            data = status.get("data_directory")
            if not data or not inside(data, exe.parent):
                raise ValueError(f"The YuE2 Studio answering on {MCP_URL} keeps its data in {data}, not in "
                                 f"{exe.parent}: it is another copy ({how}). Close it, then click Start again.")
            if status.get("portable") is not True:
                raise ValueError("The YuE2 Studio answering is not a portable copy (models_status portable is "
                                 f"{status.get('portable')}), so its models would go outside this folder.")
            window = self.studio.call("studio_status").get("window_open")
            self.version = version
            facts = {"Version": version, "MCP": MCP_URL, "Data and models": data, "Window open": "yes" if window else "no",
                     "Studio": how, "Connected after": f"{seconds} s"}
            if offset is not None:
                facts["Studio output log"] = str(log)
            self.passed("start", facts, key=f"{version}|{data}",
                        note="YuE2 Studio was already running, so it was not started a second time." if own else "")
        except Exception as error:  # noqa: BLE001 - every failure names where to read why
            told = error if isinstance(error, (ValueError, StudioError)) else f"{type(error).__name__}: {error}"
            raise ValueError(f"{told}\n\n{self.start_logs(log, offset)}") from None
        finally:
            with self.lock:
                self.steps["start"]["data"].pop("phase", None)

    def model_options(self, catalog, status):
        components = {part["id"]: part for part in catalog.get("components", [])}
        have = set(status.get("installed_components") or []) | {
            part["id"] for part in status.get("components", []) if part.get("installed")}
        options = []
        for profile in catalog.get("profiles", []):
            if not profile.get("installable"):
                continue
            parts = [components.get(cid, {"id": cid, "bytes": 0}) for cid in profile.get("components", [])]
            options.append({
                "id": profile["id"], "label": profile.get("label", profile["id"]),
                "total_bytes": profile.get("total_bytes") or sum(part.get("bytes", 0) for part in parts),
                "missing_bytes": sum(part.get("bytes", 0) for part in parts if part["id"] not in have),
                "missing": [part["id"] for part in parts if part["id"] not in have],
                "files": [{"file": part.get("filename", part["id"]), "kind": part.get("kind", ""),
                           "bytes": part.get("bytes", 0), "installed": part["id"] in have} for part in parts],
                "transcriber": any("transcri" in str(part.get("kind", "")).lower()
                                   or str(part.get("filename", "")).lower().startswith("sheetsage") for part in parts),
                "recommended": profile["id"] == catalog.get("recommended_profile_id"),
            })
        options.sort(key=lambda option: option["total_bytes"])
        return options

    def do_models_read(self, _body):
        catalog, status = self.studio.call("models_catalog"), self.studio.call("models_status")
        options = self.model_options(catalog, status)
        if not options:
            raise ValueError("models_catalog offers no model set this computer can install.")
        covers = [option for option in options if option["transcriber"]]
        # the studio's own pick for this card when it carries SheetSage2, else the smallest set that does
        suggested = next((option for option in covers if option["recommended"]), None) or (covers or options)[0]
        runtime = status.get("engine_runtime") or {}
        with self.lock:
            self.steps["models"]["data"] = {
                "options": options, "suggested": suggested["id"],
                "selected": status.get("selected_profile_id"),
                "runtime_bytes": runtime.get("total_bytes", 0), "runtime_ready": runtime.get("ready")}

    def do_models_use(self, body):
        data = self.steps["models"]["data"]
        option = next((o for o in data.get("options", []) if o["id"] == body.get("profile_id")), None)
        if not option:
            raise ValueError("Choose a model set from the list first.")
        if not option["transcriber"]:
            raise ValueError(f"{option['label']} has no SheetSage2, so the studio could not read a recording into a "
                             "score (covers, instrumentals from a song).")
        if option["missing"]:
            self.studio.call("models_download", {"profile_id": option["id"]})
            while True:
                status = self.studio.call("models_status")
                have = set(status.get("installed_components") or []) | {
                    part["id"] for part in status.get("components", []) if part.get("installed")}
                missing = [cid for cid in option["missing"] if cid not in have]
                if not missing:
                    break
                job = status.get("active") or {}
                if job.get("status") in ("failed", "cancelled"):
                    raise ValueError(f"The model download {job['status']}: {job.get('error') or 'YuE2 Studio gave no reason.'}")
                if job.get("status") == "completed":
                    raise ValueError("YuE2 Studio says the download completed, but models_status still lists as "
                                     "missing: " + ", ".join(missing))
                self.note("models", f"Downloading {option['label']}: {size_text(job.get('downloaded_bytes'))} of "
                                    f"{size_text(job.get('total_bytes') or option['missing_bytes'])}")
                self.sleep(2)
        self.studio.call("models_select", {"profile_id": option["id"]})
        status = self.studio.call("models_status")
        files = status.get("profile_files") or {}
        if status.get("selected_profile_id") != option["id"]:
            raise ValueError(f"models_select did not take: the selected set is {status.get('selected_profile_id')}.")
        if status.get("ready") is not True:
            raise ValueError(f"YuE2 Studio says {option['label']} is not ready: models_status ready is {status.get('ready')}.")
        if not files.get("transcriber"):
            raise ValueError(f"{option['label']} is selected, but models_status names no transcriber (SheetSage2) for it.")
        self.profile = option
        self.passed("models", {"Set": option["label"], "Size": size_text(option["total_bytes"]),
                               "Backbone": Path(files.get("backbone", "")).name,
                               "Transcriber": Path(files["transcriber"]).name}, key=option["id"])

    def wait_engine(self, restart=True):
        deadline = time.monotonic() + self.engine_wait
        while True:
            status = self.studio.call("models_status")
            if status.get("engine_error"):
                error = status["engine_error"]
                raise ValueError("YuE2 Studio's engine did not start: "
                                 + (error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)))
            if status.get("engine_ready"):
                return status
            runtime = status.get("engine_runtime") or {}
            if runtime.get("error"):
                raise ValueError(f"The engine's CUDA libraries did not download: {runtime['error']}")
            if restart:
                self.studio.call("engine_restart")
                restart = False
            if time.monotonic() > deadline:
                raise ValueError(f"YuE2 Studio's engine was not ready within {self.engine_wait} seconds.")
            self.note("engine", f"Waiting for the engine… its CUDA libraries: {size_text(runtime.get('downloaded_bytes'))} "
                              f"of {size_text(runtime.get('total_bytes'))}" if runtime.get("downloading")
                      else "Waiting for the engine to start…")
            self.sleep(2)

    def engine_options(self):
        """The engine's launch options: engine_options_get answers them under "options", beside what it reports."""
        return need(self.studio.call("engine_options_get"), "options", "engine_options_get")

    def engine_backend(self, wanted):
        """The engine's backend set to wanted (cuda here, vulkan on a rented machine) and the engine restarted, when it is
        anything else: on "auto" it may fall back to the processor. Answers whether it was restarted, and a note."""
        options = self.engine_options()
        backend = options.get("backend")
        if backend == wanted:
            return False, ""
        self.note("engine", f'The engine\'s backend is "{backend}": setting it to "{wanted}" and restarting the engine…')
        self.studio.call("engine_options_set", dict(options, backend=wanted))
        now = self.engine_options().get("backend")
        if now != wanted:
            raise ValueError(f'engine_options_set did not take: the engine backend is still "{now}". Nothing was restarted.')
        self.studio.call("engine_restart")
        return True, f'The engine\'s backend was "{backend}"; it was set to "{wanted}" and the engine restarted.'

    def do_engine(self, _body):
        """The studio's own engine computing on the card: its backend pinned (cuda here, vulkan on a rented machine, set by
        this step when it is not), the engine started, and its own log naming the device."""
        if self.mode == "vast":
            return self.engine_remote()
        restarted, note = self.engine_backend("cuda")
        self.wait_engine(restart=not restarted)
        cards = self.studio.call("system_gpus").get("cards") or []
        if not cards:
            raise ValueError("YuE2 Studio sees no NVIDIA card: system_gpus lists none.")
        logs = self.studio.call("engine_logs", text_ok=True)      # a long log comes cut, as text: read its lines
        lines = logs.get("lines", []) if isinstance(logs, dict) else str(logs).splitlines()
        name = (self.gpu or {}).get("name") or cards[0].get("name", "")
        hits = [line for line in lines if "CUDA" in line and (
            name in line or "CUDA0" in line or "ggml_cuda" in line or "CUDA backend" in line)]
        if not hits:
            raise ValueError("The engine runs, but its log names no CUDA device, so CUDA is not confirmed. "
                             "The engine log ends:\n" + "\n".join(lines[-8:]))
        device = [line for line in lines if name and name in line and line not in hits][:1]
        self.passed("engine", {"Backend": "cuda (engine_options_get)", "Card": cards[0].get("name", ""),
                               "Engine log": "\n".join(line.strip() for line in hits[:2] + device)},
                    key=f"cuda|{cards[0].get('name')}", note=note)

    # ------------------------------------------------------------ the studio's parts, downloaded ahead

    def parts_status(self):
        """Each part the studio would otherwise download the first time it is used: whether it is ready, what it still
        has to download, and what it is, as the studio answers now."""
        separator, runtime = self.studio.call("separator_status"), self.studio.call("separator_runtime")
        training = self.studio.call("runtime_status", {"part": "training_pack"})
        midi = self.studio.call("midi_status")
        listen = self.studio.call("runtime_status", {"part": "training_listen"})

        def left(pack):
            whole = pack.get("set") or {}
            return max(0, int(whole.get("bytes") or 0) - int(whole.get("installed_bytes") or 0))
        model = separator.get("model") or {}
        size = midi.get("default_size") or "medium"
        chosen = next((entry for entry in midi.get("sizes", []) if entry.get("id") == size), {})
        parts = {
            "separator": {"ready": bool(model.get("installed")) and bool(separator.get("runtime_installed")),
                          "missing_bytes": sum(a.get("bytes", 0) for a in runtime.get("assets", []) if not a.get("installed")),
                          "detail": (model.get("label") or model.get("id") or "the separator")
                          + (f" on {separator['card']}" if separator.get("card") else " on the processor")},
            "training": {"ready": bool((training.get("set") or {}).get("ready")), "missing_bytes": left(training),
                         "can": training.get("card_trains") is not False,
                         "why": {"driver": "the card or its driver runs CUDA 12 only; the trainer is a CUDA 13 build",
                                 "nvidia": "training runs on an NVIDIA card only"}.get(training.get("card_needs"), ""),
                         "trainer": next((a for a in training.get("assets", []) if a.get("id") == "music-train"), None),
                         "detail": f"the trainer's weights and the vocal separator; a run wants about "
                                   f"{training.get('min_vram_gb') or TRAINER_VRAM_GB} GB of the card's memory"},
            "midi": {"ready": bool(midi.get("tool_installed")) and bool(chosen.get("installed")),
                     "missing_bytes": int(chosen.get("missing_bytes") or 0), "size": size, "problem": midi.get("error"),
                     "detail": f"MuScriptor {size} ({chosen.get('params', '?')} parameters), the studio's default size; "
                               "its weights are CC BY-NC 4.0, non-commercial"},
            "listen": {"ready": bool((listen.get("set") or {}).get("ready")), "missing_bytes": left(listen),
                       "detail": "MOSS-Music and Beat This!, which describe a LoRA dataset's songs by ear"},
        }
        for name in PARTS:
            parts[name]["label"] = PART_LABELS[name]
            parts[name]["optional"] = name in OPTIONAL_PARTS
        return parts

    def do_parts_read(self, _body):
        parts = self.parts_status()
        with self.lock:
            self.steps["parts"]["data"] = {"parts": [dict(parts[name], id=name) for name in PARTS]}
        return parts

    def do_parts(self, _body):
        """Every part not ready yet, downloaded by the studio one after another (it runs one download at a time), each
        followed until the studio counts it ready."""
        parts = self.do_parts_read({})
        skipped = {}          # optional parts that did not complete, and why
        for name in PARTS:
            part = parts[name]
            if part.get("problem"):
                raise ValueError(f"{part['label']} cannot run on {self.here()}: {part['problem']}")
            if part["ready"] or not part.get("can", True):
                continue
            self.note("parts", f"{part['label']}: asking YuE2 Studio to download {size_text(part['missing_bytes'])}…")
            try:
                getattr(self, "install_" + name)(part)
            except Exception as error:  # noqa: BLE001 - an optional part is told, not a stop
                if not part["optional"]:
                    raise
                skipped[name] = str(error)
                continue
            parts = self.do_parts_read({})
            if not parts[name]["ready"]:
                told = f"{part['label']} finished downloading, but YuE2 Studio does not count it ready. " + self.studio_said()
                if not part["optional"]:
                    raise ValueError(told)
                skipped[name] = told
        facts = {}
        for name in PARTS:
            part = parts[name]
            if part["ready"]:
                facts[part["label"]] = f"ready · {part['detail']}"
            elif not part.get("can", True):
                facts[part["label"]] = f"not on this card: {part.get('why') or 'the studio says it cannot train here'}"
            else:
                facts[part["label"]] = ("not complete, optional (Open YuE2 Studio does not wait for it): "
                                        + (skipped.get(name) or f"{size_text(part['missing_bytes'])} still missing"))
        with self.lock:
            for entry in self.steps["parts"]["data"].get("parts", []):
                if entry["id"] in skipped:
                    entry["skipped"] = skipped[entry["id"]]
        notes = []
        if self.mode == "vast" and self.remote_folder:
            # everything the studio downloaded runs on this machine: what it links must be here now, not found missing
            # in the middle of your work
            self.note("parts", "Checking that the machine has every library the studio's programs and the trainer link…")
            installed, plugins = self.machine_libraries(
                [self.remote_folder, beat_remote.trainer_folder(TRAINER_COMMIT)], lenient=[self.remote_folder + "/usr/lib"])
            facts["System libraries"] = "all there" + (f"; installed now: {', '.join(installed)}" if installed else "")
            if plugins:
                facts["Not needed, missing"] = plugins
        if not parts["training"].get("can", True):
            notes.append("LoRA training cannot run on this card.")
        if skipped:
            notes.append("Everything required is ready; " + ", ".join(PART_LABELS[name] for name in skipped)
                         + " did not complete (optional): the studio's Describe by ear may not work until it does. "
                           "Download what is missing tries again.")
        self.passed("parts", facts, key=None, note=" ".join(notes))

    def studio_said(self):
        """What YuE2 Studio printed last, where Beat Tool started it: its errors about a download go there."""
        try:
            if self.mode == "vast" and self.remote and self.remote_folder:
                log = f"{self.remote_folder}/{START_LOG}"
                tail = self.remote.sh(f"tail -n 8 {shlex.quote(log)} 2>/dev/null || true", timeout=60).strip().splitlines()
            elif self.exe:
                log = self.exe.parent / START_LOG
                tail = printed(log, 0, 8)
            else:
                return ""
        except (RemoteError, OSError):
            return ""
        return (f"What YuE2 Studio printed last ({log}):\n  " + "\n  ".join(line[:300] for line in tail)) if tail else ""

    def follow(self, label, read):
        """Waits for a download the studio runs. read answers (ready, the download at work). A download that fails, or
        nothing downloading for part_stall seconds while the part is still missing, is an error with what the studio
        printed."""
        quiet_since, failing = time.monotonic(), 0
        while True:
            ready, active = read()
            if ready:
                return
            # an error still shown from an earlier try is not this one's: it must stand three polls in a row
            failing = failing + 1 if isinstance(active, dict) and active.get("error") else 0
            if failing >= 3:
                raise ValueError(f"{label}: the download failed: {active['error']}")
            if failing:
                self.note("parts", f"{label}: the studio reports a failed download; checking it is this try's…")
            elif isinstance(active, dict) and not active.get("done"):
                quiet_since = time.monotonic()
                got, total = active.get("downloaded_bytes") or 0, active.get("total_bytes") or 0
                self.note("parts", f"{label}: downloading {active.get('asset_id') or ''} "
                                   + (f"{size_text(got)} of {size_text(total)}" if total else "…"))
            elif time.monotonic() - quiet_since > self.part_stall:
                raise ValueError(f"{label} stopped downloading without being ready, and YuE2 Studio gave no error. "
                                 + self.studio_said())
            else:
                self.note("parts", f"{label}: waiting for YuE2 Studio to go on…")
            self.sleep(2)

    def install_separator(self, part):
        """Each missing separator file in turn. The studio downloads one file at a time and refuses another while one
        runs (“another download is already running”), so the next starts only once nothing is downloading."""
        started = {}
        while True:
            runtime = self.studio.call("separator_runtime")
            left = [a for a in runtime.get("assets", []) if not a.get("installed")]
            active = runtime.get("active_download")
            if isinstance(active, dict) and active.get("error"):
                raise ValueError(f"{part['label']}: the download failed: {active['error']}")
            if not left:
                return
            following = left[0]
            label = str(following.get("label") or following.get("id"))
            waiting = f"{part['label']}: downloading " + ", ".join(str(a.get("label") or a.get("id")) for a in left)
            if isinstance(active, dict) and not active.get("done"):
                if active.get("total_bytes"):
                    waiting += f" · {size_text(active.get('downloaded_bytes'))} of {size_text(active['total_bytes'])}"
            elif following["id"] in started:
                if time.monotonic() - started[following["id"]] > self.part_stall:
                    raise ValueError(f"{label} stopped downloading without being installed, and YuE2 Studio gave no "
                                     "error. " + self.studio_said())
            else:
                try:
                    self.studio.call("separator_runtime_install", {"asset_id": following["id"]})
                    started[following["id"]] = time.monotonic()
                except StudioError as error:
                    if "another download is already running" not in str(error):
                        raise
                    waiting = "YuE2 Studio is downloading something else; the separator waits for it."
            self.note("parts", waiting)
            self.sleep(2)

    def install_training(self, part):
        """The training pack (training_pack_install): on this PC the trainer and its weights, on a rented machine the
        weights for the trainer the studio already runs with (YUE_TRAIN_BIN), which it counts installed."""
        if self.mode == "vast" and not (part.get("trainer") or {}).get("installed"):
            raise ValueError("YuE2 Studio does not count the LoRA trainer installed, so it would try to download the "
                             "Windows trainer and fail: it was not started with YUE_TRAIN_BIN. Click LoRA trainer, then "
                             "Studio running, again.")
        self.studio.call("training_pack_install")

        def read():
            status = self.studio.call("runtime_status", {"part": "training_pack"})
            return bool((status.get("set") or {}).get("ready")), status.get("active_download")
        self.follow(part["label"], read)

    def install_midi(self, part):
        self.studio.call("midi_install", {"size": part["size"]})

        def read():
            status = self.studio.call("midi_status")
            entry = next((s for s in status.get("sizes", []) if s.get("id") == part["size"]), {})
            return bool(status.get("tool_installed")) and bool(entry.get("installed")), status.get("download")
        self.follow(part["label"], read)

    def install_listen(self, part):
        self.studio.call("training_listen_pack_install")

        def read():
            status = self.studio.call("runtime_status", {"part": "training_listen"})
            return bool((status.get("set") or {}).get("ready")), status.get("active_download")
        self.follow(part["label"], read)

    # ------------------------------------------------------------ open YuE2 Studio: the last step

    def open_click(self):
        """Open YuE2 Studio: everything before it checked again on the studio as it runs now, then the address of its
        window. Only when every step before it is verified; a problem found turns the step it belongs to red (the steps
        after it wait again), and Prepare everything sets it right."""
        with self.lock:
            self.take("open")
        try:
            facts = self.ready_check()
            facts["Window"] = self.studio_open()
            self.passed("open", facts, key=None)
            return facts["Window"]
        except StepProblem as problem:
            if problem.step != "open":
                self.failed(problem.step, problem)
            self.failed("open", f"{self.label(problem.step)} is not ready any more: {problem}")
            raise ValueError(str(problem)) from None
        except Exception as error:  # noqa: BLE001 - shown word for word
            self.failed("open", error)
            raise ValueError(str(error)) from None
        finally:
            with self.lock:
                self.busy = None

    def ready_check(self):
        """The studio as it runs now: the pinned version, the model set chosen and ready, the engine on the card, the
        LoRA trainer (rented machine), every part ready, and disk left for your work."""
        try:
            info = self.studio.hello()
        except StudioError as error:
            raise StepProblem("start", f"YuE2 Studio does not answer: {error}") from None
        pinned = REMOTE_VERSION if self.mode == "vast" else STUDIO_VERSION
        if info.get("version") != pinned:
            raise StepProblem("start", f"the YuE2 Studio answering is version {info.get('version')}, not {pinned}.")
        status = self.studio.call("models_status")
        chosen = (self.profile or {}).get("id")
        if status.get("selected_profile_id") != chosen or status.get("ready") is not True:
            raise StepProblem("models", f"the studio's model set is {status.get('selected_profile_id')} (ready: "
                                        f"{status.get('ready')}), not {chosen}, ready.")
        wanted = "vulkan" if self.mode == "vast" else "cuda"
        backend = self.engine_options().get("backend")
        if backend != wanted:
            raise StepProblem("engine", f'the engine\'s backend is "{backend}", not "{wanted}".')
        if not status.get("engine_ready"):
            raise StepProblem("engine", "the engine is not running: " + str(status.get("engine_error") or "it is not ready."))
        facts = {"Studio": f"YuE2 Studio {info.get('version')} on {self.here()}",
                 "Model set": (self.profile or {}).get("label", chosen), "Engine": f"{backend}, ready"}
        parts = self.parts_status()
        if self.mode == "vast":
            trainer = parts["training"].get("trainer") or {}
            if not trainer.get("installed"):
                raise StepProblem("start", "the studio runs without the LoRA trainer (YUE_TRAIN_BIN).")
            facts["LoRA trainer"] = self.trainer_exe()
        missing = [part["label"] for part in parts.values()
                   if not part["ready"] and part.get("can", True) and not part["optional"]]
        if missing:
            raise StepProblem("parts", "not ready: " + ", ".join(missing) + ".")
        facts["Parts"] = ", ".join(part["label"] for part in parts.values() if part["ready"])
        incomplete = [part["label"] for part in parts.values() if not part["ready"] and part["optional"]]
        if incomplete:
            facts["Not complete (optional)"] = ", ".join(incomplete)
        free = self.disk_free_gb()
        if free is not None:
            if free < MIN_FREE_END_GB:
                raise StepProblem("open", f"only {free} GB of disk is free on {self.here()}, too little for your songs, "
                                          f"datasets and LoRA runs (at least {MIN_FREE_END_GB} GB).")
            facts["Disk free"] = f"{free} GB"
        return facts

    def disk_free_gb(self):
        try:
            if self.mode == "vast":
                out = self.on_remote().sh("df -BG --output=avail /root | tail -n1 | tr -d ' G'", timeout=60).strip()
                return int(out) if out.isdigit() else None
            return int(shutil.disk_usage(self.folder).free // 1024 ** 3)
        except (RemoteError, OSError, ValueError):
            return None

    # ------------------------------------------------------------ a LoRA dataset, sent with its styles

    def dataset_click(self, body):
        """The LoRA dataset card: check a folder (here, nothing sent), send it to the studio as a new dataset with every
        song's style, or give the styles to the songs of a dataset of that name already in the studio. Not a step of
        the chain; it holds the chain while it works."""
        kind, folder = str(body.get("do") or ""), str(body.get("folder") or "").strip().strip('"')
        if kind not in ("check", "send", "styles"):
            raise Refused(f"There is no such dataset action: {kind}.")
        if not folder:
            raise Refused("Write the dataset folder's path first (for example C:\\Users\\you\\Desktop\\dataset_…).")
        with self.lock:
            if self.preparing or self.busy:
                raise Refused(f"Wait: {self.label(self.busy or self.preparing)} is still working.")
            if kind != "check" and self.steps["start"]["state"] != "ok":
                raise Refused("First get YuE2 Studio running (Prepare everything): the dataset goes into that studio.")
            self.busy = "dataset"
            self.dataset = {"state": "working", "note": "Reading the folder…", "error": "", "facts": {},
                            "data": self.dataset.get("data") or {}}
        thread = threading.Thread(target=self._dataset_work, args=(kind, folder), daemon=True)
        thread.start()
        return thread

    def dataset_note(self, text):
        with self.lock:
            self.dataset["note"] = text

    def _dataset_work(self, kind, folder):
        try:
            read = read_dataset_folder(folder)
            with self.lock:
                self.dataset["data"] = read
            if read["problems"]:
                raise ValueError(f"{read['problems']} thing{'s' if read['problems'] != 1 else ''} to fix in the folder "
                                 "first: each is named under its song.")
            if kind == "check":
                facts, note = {"Folder": read["folder"], "Songs": f"{len(read['songs'])}, each with lyrics in sections "
                                                                    "and a style"}, "Ready to send."
            elif kind == "send":
                facts, note = self.dataset_send(read)
            else:
                facts, note = self.dataset_styles(read)
            with self.lock:
                self.dataset.update(state="ok", note=note, error="", facts=facts)
        except Exception as error:  # noqa: BLE001 - shown word for word
            with self.lock:
                self.dataset.update(state="failed", note="", error=str(error), facts={})
        finally:
            with self.lock:
                self.busy = None

    def studio_datasets(self):
        return self.studio.call("training_status").get("datasets") or []

    def dataset_items(self, dataset_id):
        answer = self.studio.call("dataset_get", {"dataset_id": dataset_id})
        return (answer.get("dataset") or answer).get("items") or []

    def dataset_send(self, read):
        """A new dataset named after the folder: the audio and the lyrics without their style lines, sent as a folder the
        studio reads (on a rented machine, copied there first, each file checked by its SHA-256), then every song's
        style written, and the studio asked whether every song is ready."""
        name = read["name"]
        if any(dataset.get("name") == name for dataset in self.studio_datasets()):
            raise ValueError(f"YuE2 Studio already has a dataset named {name}. To give its songs their styles, click Set the "
                             "styles in the studio; to send the folder again, delete that dataset in the studio first, "
                             "or rename the folder.")
        if not re.fullmatch(r"[^/\\:*?\"<>|\x00-\x1f]+", name) or name in (".", ".."):
            raise ValueError(f"The folder's name {name!r} cannot be a dataset's folder name: rename the folder.")
        staged = self.folder / DATASETS_DIR / name
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        for song in read["songs"]:
            shutil.copy2(Path(read["folder"]) / song["file"], staged / song["file"])
            (staged / f"{song['stem']}.txt").write_text(song["lyrics"] + "\n", encoding="utf-8")
        if self.mode == "vast":
            remote = self.on_remote()
            there = f"{beat_remote.REMOTE_ROOT}/{DATASETS_DIR}/{name}"
            remote.sh(f"rm -rf {shlex.quote(there)}", timeout=120)
            files = sorted(staged.iterdir())
            for number, path in enumerate(files, 1):
                self.dataset_note(f"Sending {path.name} to the rented machine ({number} of {len(files)})…")
                remote.put(path, f"{there}/{path.name}", sha256(path))
        else:
            there = str(staged)
        self.dataset_note(f"Making the dataset {name} in YuE2 Studio…")
        dataset_id = need(self.studio.call("dataset_create", {"name": name}), "id", "dataset_create")
        try:
            self.dataset_note(f"Adding {len(read['songs'])} songs to it…")
            self.studio.call("dataset_add_folder", {"dataset_id": dataset_id, "path": there}, timeout=1800)
            facts = self.dataset_give_styles(dataset_id, read)
        except Exception as error:
            try:
                self.studio.call("dataset_delete", {"dataset_id": dataset_id})
                gone = "The half-made dataset was taken out of the studio again."
            except StudioError as removal:
                gone = f"The half-made dataset {name} is still in the studio ({removal}): delete it there."
            raise ValueError(f"{error}\n{gone}") from None
        return {"Dataset": f"{name} (new in YuE2 Studio)", **facts}, "Sent. In the studio: LoRA · Training, open the dataset."

    def dataset_styles(self, read):
        """The styles of the folder given to the songs of the studio's dataset of the same name, as you uploaded it."""
        found = [dataset for dataset in self.studio_datasets() if dataset.get("name") == read["name"]]
        if not found:
            raise ValueError(f"YuE2 Studio has no dataset named {read['name']}: upload the folder there first, or click "
                             "Send to YuE2 Studio, which makes it.")
        if len(found) > 1:
            raise ValueError(f"YuE2 Studio has {len(found)} datasets named {read['name']}: delete or rename the others.")
        return {"Dataset": f"{read['name']} (already in YuE2 Studio)", **self.dataset_give_styles(found[0]["id"], read)}, \
            "Styles given. Reload the studio's page (F5): it does not see a change made from outside by itself."

    def dataset_give_styles(self, dataset_id, read):
        """Every song of the folder found in the studio's dataset by its file name, given its style; then the studio's
        own count of ready songs."""
        by_file = {song["file"].lower(): song for song in read["songs"]}
        by_stem = {song["stem"].lower(): song for song in read["songs"]}
        given, unknown = 0, []
        for item in self.dataset_items(dataset_id):
            source = str(item.get("source") or "").split(":", 1)[-1].replace("\\", "/").rsplit("/", 1)[-1].lower()
            song = by_file.get(source) or by_stem.get(str(item.get("title") or "").lower())
            if not song:
                unknown.append(str(item.get("title") or item.get("id")))
                continue
            song["matched"] = True
            if item.get("style") != song["style"]:
                self.dataset_note(f"Giving {song['file']} its style…")
                self.studio.call("dataset_song_update", {"dataset_id": dataset_id, "song_id": item["id"],
                                                         "style": song["style"]})
                given += 1
        missing = [song["file"] for song in read["songs"] if not song.pop("matched", False)]
        if missing:
            raise ValueError("These songs of the folder are not in the studio's dataset: " + ", ".join(missing))
        items = self.dataset_items(dataset_id)
        ready = [item for item in items if item.get("lyrics_state") == "done" and item.get("style_state") == "done"]
        if len(ready) != len(items):
            lacking = [f"{item.get('title')} (lyrics {item.get('lyrics_state')}, style {item.get('style_state')})"
                       for item in items if item not in ready]
            raise ValueError(f"YuE2 Studio counts {len(ready)} of {len(items)} songs ready; not ready: " + "; ".join(lacking))
        facts = {"Ready": f"{len(ready)} of {len(items)} songs (YuE2 Studio's own count)",
                 "Styles written now": str(given)}
        if unknown:
            facts["Also in the dataset"] = ", ".join(unknown) + " (not in the folder: left as they are)"
        return facts

    # ------------------------------------------------------------ the page

    def snapshot(self):
        with self.lock:
            order = self.order()
            state = {
                "order": order, "mode": self.mode, "labels": {name: self.label(name) for name in order},
                "steps": {name: {key: self.steps[name][key] for key in ("state", "note", "error", "facts", "data")}
                          for name in order},
                "busy": self.busy, "preparing": self.preparing,
                "can": {name: not self.busy and not self.preparing and not self.missing_before(name) for name in order},
                "dataset": {**self.dataset, "default": newest_dataset_folder(),
                            "can_send": not self.busy and not self.preparing and self.steps["start"]["state"] == "ok"},
            }
            if self.mode == "vast":
                state["pin"] = {"tag": REMOTE_TAG, "asset": LINUX_ASSET, "bytes": LINUX_BYTES, "url": LINUX_URL,
                                "sha256": LINUX_SHA256, "folder": f"{REMOTE_NAME}_from_github_{stamp()}"}
                state["studio_found"] = self.remote_found
            else:
                found = self.studio_found()
                state["pin"] = {"tag": STUDIO_TAG, "asset": STUDIO_ZIP, "bytes": STUDIO_ZIP_BYTES, "url": STUDIO_ZIP_URL,
                                "sha256": STUDIO_ZIP_SHA256, "folder": f"{STUDIO_NAME}_from_github_{stamp()}"}
                state["studio_found"] = found.name if found else None
            return state


class Handler(BaseHTTPRequestHandler):
    app: App = None

    def log_message(self, *_args):
        pass

    def host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def send(self, code, payload, kind="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "This tool answers only 127.0.0.1."})
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self.send(200, self.app.snapshot())
        return self.send(404, {"error": "Not here."})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not self.host_ok() or self.headers.get("X-Beat-Tool") != "1":
            if length <= 1 << 20:
                self.rfile.read(length)        # read what was sent, or Windows resets the answer away
            return self.send(403, {"error": "This tool answers only its own page."})
        action = urllib.parse.urlparse(self.path).path.removeprefix("/api/")
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if 0 < length <= 1 << 20 else {}
            if action == "vast_stop":
                self.app.stop_renting()
            elif action == "prepare":
                self.app.prepare_click(body)
            elif action == "open":
                return self.send(200, {"ok": True, "url": self.app.open_click()})
            elif action == "dataset":
                self.app.dataset_click(body)
            else:
                self.app.act(action, body)
        except Refused as error:
            return self.send(409, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - the page shows it word for word
            return self.send(400, {"error": str(error)})
        return self.send(200, {"ok": True})


class Server(ThreadingHTTPServer):
    # On Windows a port another program holds can still be bound unless the bind is exclusive; this tool must
    # notice instead, so that its page is never served beside someone else's.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(app, port=PORT):
    handler = type("BoundHandler", (Handler,), {"app": app})
    return Server(("127.0.0.1", port), handler)


def beat_tool_at(url) -> bool:
    """Whether what answers at url is this tool, already open."""
    try:
        with urllib.request.urlopen(url + "api/state", timeout=3) as response:
            state = json.loads(response.read())
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and "pin" in state and "order" in state


def port_answers(port) -> bool:
    """Whether something already listens on this port. Windows may let a second program bind a taken port, so the
    port is asked before it is bound."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def main():
    url = f"http://127.0.0.1:{PORT}/"
    taken = f"Port {PORT} on this PC is taken by another program, so Beat Tool cannot open there. Close that " \
            f"program, or set PORT at the top of beat_tool.py to a free port."
    if port_answers(PORT):
        if beat_tool_at(url):
            print(f"Beat Tool is already open: {url}")
            webbrowser.open(url)
            return 0
        print(taken)
        return 1
    try:
        app = App()
        server = serve(app)
    except OSError:
        print(taken)
        return 1
    print(f"Beat Tool is open at {url}\nClose this window to stop it (YuE2 Studio keeps running in its own window).")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    rented = app.saved_instance()
    app.disconnect()
    if rented:
        print(f"vast.ai instance {rented['id']} is still rented and billing: open Beat Tool again and click Stop renting, "
              "or destroy it at cloud.vast.ai/instances.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
