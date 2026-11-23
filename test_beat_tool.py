"""Beat Tool's setup, against stand-ins for the studio, vast.ai and the rented machine: nothing real is started,
rented or downloaded (the CUDA test runs for real only where this PC has an NVIDIA driver).

Run: python -B -m unittest test_beat_tool -v
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import beat_remote
import beat_tool
from beat_tool import App, StudioError


class FakeStudio:
    """YuE2 Studio's MCP as Start sees it: hello answers after a number of tries, or never."""

    def __init__(self, answers_after=0, version=beat_tool.STUDIO_VERSION, data=None, gate=None):
        self.answers_after, self.version, self.data, self.gate = answers_after, version, data, gate
        self.hellos, self.phases, self.app, self.last = 0, [], None, None

    def hello(self):
        self.phases.append(self.app.steps["start"]["data"].get("phase"))
        if self.gate:
            self.gate.wait(5)
        self.hellos += 1
        if self.answers_after is None or self.hellos <= self.answers_after:
            raise StudioError(f"YuE2 Studio's MCP at {beat_tool.MCP_URL} does not answer: [WinError 10061] refused")
        return {"name": "yue2-studio", "version": self.version}

    def call(self, name, arguments=None, timeout=None, text_ok=False):
        self.last = name
        if name == "models_status":
            return {"data_directory": self.data, "portable": True}
        if name == "studio_status":
            return {"window_open": True}
        raise AssertionError(f"Start called {name}")


class FakeProcess:
    def __init__(self, pid, exit_code=None):
        self.pid, self.exit_code = pid, exit_code

    def poll(self):
        return self.exit_code


class StartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.exe = self.folder / "YuE2-Studio-3.8.2_from_github_date281026time2343" / beat_tool.STUDIO_EXE
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"MZ")
        self.log = self.exe.parent / beat_tool.START_LOG
        self.running = []          # what Windows lists: (pid, path)
        self.alive = set()
        self.windows_fails = False
        self.launched = []
        self.exit_code = None

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, command, **_kwargs):
        if command[0] == "powershell":
            if self.windows_fails:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Get-CimInstance : Access denied")
            lines = [f"{pid}|01:00:00|{path}" for pid, path in self.running]
            return types.SimpleNamespace(returncode=0, stdout="\n".join(lines), stderr="")
        if command[0] == "tasklist":
            pid = int(command[2].split()[-1])
            return types.SimpleNamespace(returncode=0, stdout=f'"{beat_tool.STUDIO_EXE}","{pid}"' if pid in self.alive else "",
                                         stderr="")
        raise AssertionError(f"Start ran {command}")

    def fake_popen(self, command, **kwargs):
        self.launched.append((command, kwargs["cwd"]))
        self.flags = kwargs["creationflags"]
        return FakeProcess(4242, self.exit_code)

    def app(self, studio, start_wait=5):
        studio.data = studio.data or str(self.exe.parent / "data")
        app = App(folder=self.folder, studio=studio, run=self.fake_run, popen=self.fake_popen,
                  sleep=lambda _seconds: time.sleep(0.005), start_wait=start_wait)
        studio.app = app
        app.exe = self.exe
        for step in ("gpu", "studio"):
            app.steps[step]["state"] = "ok"
        return app

    def click(self, app):
        app.act("start").join(10)
        return app.snapshot()["steps"]["start"]

    # ------------------------------------------------------------ connected

    def test_not_running_starts_one_studio_and_shows_each_phase(self):
        studio = FakeStudio(answers_after=3)
        start = self.click(self.app(studio))
        self.assertEqual(start["state"], "ok", start["error"])
        self.assertEqual(self.launched, [([str(self.exe)], str(self.exe.parent))])
        # a console with no window, so the console programs the studio runs open no window either
        self.assertTrue(self.flags & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(self.flags & subprocess.DETACHED_PROCESS)
        self.assertEqual(studio.phases, ["starting…"] + ["waiting for the MCP…"] * 3)
        self.assertIn("PID 4242, started by this click", start["facts"]["Studio"])
        self.assertEqual(start["facts"]["Studio output log"], str(self.log))
        self.assertIn("Beat Tool starts", self.log.read_text(encoding="utf-8"))
        self.assertNotIn("phase", start["data"])

    def test_own_studio_already_running_is_not_started_again(self):
        self.running = [(777, str(self.exe))]
        start = self.click(self.app(FakeStudio()))
        self.assertEqual(start["state"], "ok", start["error"])
        self.assertEqual(self.launched, [])
        self.assertIn("already running", start["note"])
        self.assertIn("PID 777", start["facts"]["Studio"])

    # ------------------------------------------------------------ red, with where to read why

    def test_another_copy_running_is_refused(self):
        self.running = [(999, r"C:\Other\YuE2-Studio\YuE2-Studio.exe")]
        start = self.click(self.app(FakeStudio(answers_after=None)))
        self.assertEqual(start["state"], "failed")
        self.assertEqual(self.launched, [])
        self.assertIn("Another YuE2 Studio is running, not the copy unpacked here", start["error"])
        self.assertIn(r"PID 999, running since 01:00:00 from C:\Other", start["error"])

    def test_windows_cannot_list_programs(self):
        self.windows_fails = True
        start = self.click(self.app(FakeStudio(answers_after=None)))
        self.assertEqual(start["state"], "failed")
        self.assertEqual(self.launched, [])
        self.assertIn("ended with code 1, so none was started: Get-CimInstance : Access denied", start["error"])

    def test_studio_closes_before_its_mcp_answers(self):
        self.exit_code = 3221225781
        start = self.click(self.app(FakeStudio(answers_after=None)))
        self.assertEqual(start["state"], "failed")
        self.assertIn("YuE2 Studio closed (exit code 3221225781)", start["error"])
        self.assertIn(f"Log to read: {self.log}", start["error"])

    def test_own_studio_closes_before_its_mcp_answers(self):
        self.running = [(777, str(self.exe))]
        start = self.click(self.app(FakeStudio(answers_after=None)))
        self.assertEqual(start["state"], "failed")
        self.assertEqual(self.launched, [])
        self.assertIn("YuE2 Studio (PID 777) closed", start["error"])

    def test_mcp_never_answers(self):
        start = self.click(self.app(FakeStudio(answers_after=None), start_wait=0.1))
        self.assertEqual(start["state"], "failed")
        self.assertEqual(len(self.launched), 1)
        self.assertIn("did not answer within 0.1 seconds, while the studio kept running", start["error"])
        self.assertIn("[WinError 10061] refused", start["error"])
        self.assertIn(f"Log to read: {self.log}", start["error"])

    def test_another_version_answers(self):
        start = self.click(self.app(FakeStudio(version="3.9.0")))
        self.assertEqual(start["state"], "failed")
        self.assertIn("is version 3.9.0; this tool is pinned to 3.8.2", start["error"])

    def test_studio_keeping_its_data_elsewhere_answers(self):
        start = self.click(self.app(FakeStudio(data=r"C:\Users\Someone\AppData\Roaming\YuE2-Studio")))
        self.assertEqual(start["state"], "failed")
        self.assertIn(r"keeps its data in C:\Users\Someone\AppData\Roaming\YuE2-Studio", start["error"])

    # ------------------------------------------------------------ the click, as the page sends it

    def test_click_reaches_the_backend_and_shows_starting_at_once(self):
        gate = threading.Event()
        app = self.app(FakeStudio(gate=gate))
        server = beat_tool.serve(app, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(headers):
            request = urllib.request.Request(base + "/api/start", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())

        try:
            self.assertEqual(post({"X-Beat-Tool": "1"}), (200, {"ok": True}))
            deadline = time.monotonic() + 2
            while True:
                with urllib.request.urlopen(base + "/api/state", timeout=5) as response:
                    start = json.loads(response.read())["steps"]["start"]
                if start["data"].get("phase") or time.monotonic() > deadline:
                    break
                time.sleep(0.01)
            self.assertEqual((start["state"], start["data"].get("phase")), ("working", "starting…"))
            self.assertEqual(post({"X-Beat-Tool": "1"}), (409, {"error": "Wait: Studio running is still working."}))
            self.assertEqual(post({}), (403, {"error": "This tool answers only its own page."}))
            gate.set()
            deadline = time.monotonic() + 10
            while app.busy and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(app.snapshot()["steps"]["start"]["state"], "ok")
        finally:
            gate.set()
            server.shutdown()
            server.server_close()


class FakeWindowServer:
    """YuE2 Studio's server as a browser sees it: its window's page at /."""

    def __init__(self, page=b"<title>YuE2 Studio - Local AI Songs with Editable Scores</title>"):
        page_bytes = page

        class Handler(beat_tool.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)

        self.server = beat_tool.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeTunnel:
    """The SSH link to a rented machine, as Open YuE2 Studio uses it: its tunnel's local port."""

    def __init__(self, port):
        self.port, self.asked = port, 0

    def __str__(self):
        return "root@203.0.113.7:40000"

    def tunnel(self):
        self.asked += 1
        return self.port


class OpenStudioTest(unittest.TestCase):
    """Open YuE2 Studio: the studio's own window at the address its server answers, on this PC or through the tunnel."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = FakeWindowServer()
        self.app = App(folder=Path(self.temp.name), studio=FakeStudio())
        self.mcp_url = beat_tool.MCP_URL

    def tearDown(self):
        beat_tool.MCP_URL = self.mcp_url
        self.window.close()
        self.temp.cleanup()

    def test_on_this_pc_opens_the_studio_server_beside_its_mcp(self):
        beat_tool.MCP_URL = f"http://127.0.0.1:{self.window.port}/mcp"
        self.assertEqual(self.app.mode, "pc")
        self.assertEqual(self.app.studio_open(), f"http://127.0.0.1:{self.window.port}/")

    def test_on_a_rented_machine_opens_it_through_the_tunnel(self):
        self.app.mode, self.app.remote = "vast", FakeTunnel(self.window.port)
        self.assertEqual(self.app.studio_open(), f"http://127.0.0.1:{self.window.port}/")
        self.assertEqual(self.app.remote.asked, 1)

    def test_rented_machine_not_connected(self):
        self.app.mode = "vast"
        with self.assertRaisesRegex(ValueError, "connect to it in step 0 first"):
            self.app.studio_open()

    def test_nothing_answers(self):
        port = self.window.port
        self.window.close()
        self.window = FakeWindowServer()          # tearDown closes this one
        self.app.mode, self.app.remote = "vast", FakeTunnel(port)
        with self.assertRaisesRegex(ValueError, rf"YuE2 Studio does not answer on the rented machine \(through the "
                                                rf"SSH tunnel to root@203.0.113.7:40000\) at http://127.0.0.1:{port}/"):
            self.app.studio_open()

    def test_something_else_answers(self):
        self.window.close()
        self.window = FakeWindowServer(page=b"<title>Some other server</title>")
        beat_tool.MCP_URL = f"http://127.0.0.1:{self.window.port}/mcp"
        with self.assertRaisesRegex(ValueError, "is not YuE2 Studio's window"):
            self.app.studio_open()

    def test_open_is_refused_until_everything_before_it_is_verified_and_while_a_step_works(self):
        beat_tool.MCP_URL = f"http://127.0.0.1:{self.window.port}/mcp"
        server = beat_tool.serve(self.app, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(headers):
            request = urllib.request.Request(base + "/api/open", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())

        try:
            self.assertEqual(post({}), (403, {"error": "This tool answers only its own page."}))
            self.assertEqual(post({"X-Beat-Tool": "1"}),
                             (409, {"error": "First Studio parts: that link is not connected yet."}))
            for step in beat_tool.SETUP_PC[:-1]:
                self.app.steps[step]["state"] = "ok"
            self.app.busy = "engine"
            self.assertEqual(post({"X-Beat-Tool": "1"}), (409, {"error": "Wait: Engine on the card is still working."}))
            self.app.busy = None
            self.app.ready_check = lambda: {"Studio": "checked"}
            self.assertEqual(post({"X-Beat-Tool": "1"}),
                             (200, {"ok": True, "url": f"http://127.0.0.1:{self.window.port}/"}))
            self.assertEqual(self.app.steps["open"]["state"], "ok")
            self.assertIsNone(self.app.busy)
        finally:
            server.shutdown()
            server.server_close()


TRAINER_RECORD = {"commit": beat_tool.TRAINER_COMMIT, "architectures": "86", "cuda": "13.0", "exe_sha256": "ab" * 32,
                  "files": 30, "says": "ace-train: usage", "built": "2026-11-08 03:30:00"}


class TrainerStudio:
    """The studio's MCP as Add the LoRA trainer asks it: whether its training pack counts the trainer installed."""

    def __init__(self, installed=True):
        self.installed, self.last = installed, None

    def call(self, name, arguments=None, timeout=None, text_ok=False):
        self.last = name
        if name == "runtime_status" and arguments == {"part": "training_pack"}:
            return {"assets": [{"id": "music-train", "installed": self.installed}]}
        raise AssertionError(f"Add the LoRA trainer called {name}")


class FakeTrainerRemote:
    """The rented machine as Add the LoRA trainer sees it: what each script it is sent answers."""

    def __init__(self, found=None, running="none", build_lines=(), tar=b"the trainer, packed", libraries="@@scanned 12\n"):
        self.found, self.running, self.build_lines, self.tar = found, running, list(build_lines), tar
        self.libraries = libraries
        self.calls, self.sent, self.script = [], None, ""

    def __str__(self):
        return "root@203.0.113.7:40000"

    def sh(self, script, timeout=120):
        if "@@scanned" in script:
            self.calls.append("libraries")
            self.libraries_script = script
            return self.libraries
        if "@@record" in script and beat_remote.TRAINER_RECORD in script:
            self.calls.append("found")
            if not self.found:
                return "@@none\n"
            return f"@@record {json.dumps(self.found)}\n@@exe {self.found['exe_sha256']}\n"
        if "@@uses" in script:
            self.calls.append("uses")
            return {"none": "@@none\n", "yes": "@@uses yes 77\n", "no": "@@uses no 77\n"}[self.running]
        if "@@stopped" in script:
            self.calls.append("stop")
            return "@@stopped\n"
        if "tar -xzf" in script:
            self.calls.append("unpack")
            return f"@@json {json.dumps(TRAINER_RECORD)}\n"
        raise AssertionError(f"Add the LoRA trainer sent {script[:200]}")

    def stream(self, script, on_line, timeout=3600):
        self.calls.append("build")
        self.script = script
        for line in self.build_lines:
            on_line(line)
        if any(line.startswith("@@error ") for line in self.build_lines):
            raise beat_remote.RemoteError("On the rented machine the setup ended with code 6")
        return self.build_lines

    def put(self, local, path, digest, timeout=3600):
        self.calls.append("put")
        self.sent = (Path(local).read_bytes(), path, digest)
        return True

    def get(self, path, target, timeout=3600):
        self.calls.append("get")
        Path(target).write_bytes(self.tar)
        return hashlib.sha256(self.tar).hexdigest()


class TrainerTest(unittest.TestCase):
    """Add the LoRA trainer: found there, sent from home, or built there and brought home; the studio then runs with it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.studio = TrainerStudio()
        self.app = App(folder=self.folder, studio=self.studio)
        self.app.mode = "vast"
        self.app.remote_folder = "/root/beat_tool/YuE2-Studio-3.8.4-linux_from_github_date081126time0330"
        self.app.gpu = {"name": "NVIDIA RTX A6000", "vram_gb": 48.0, "driver": "595.84", "compute": 8.6}
        for step in ("where", "gpu", "cuda", "studio", "start"):
            self.app.steps[step]["state"] = "ok"
        self.cache = self.folder / "vast" / f"trainer-{beat_tool.TRAINER_COMMIT[:8]}-sm86.tar.gz"

    def tearDown(self):
        self.temp.cleanup()

    def click(self, remote):
        self.app.remote = remote
        self.app.act("trainer").join(10)
        self.assertIsNone(self.app.busy)
        return self.app.snapshot()["steps"]["trainer"]

    def test_comes_before_the_studio_starts(self):
        order = beat_tool.SETUP_VAST
        self.assertLess(order.index("trainer"), order.index("start"))
        self.assertNotIn("trainer", beat_tool.SETUP_PC)

    def test_refused_on_this_pc(self):
        self.app.mode = "pc"
        with self.assertRaisesRegex(beat_tool.Refused, "a step on a rented machine only"):
            self.app.act("trainer")

    def test_refused_before_the_studio_step(self):
        self.app.remote = FakeTrainerRemote()
        self.app.steps["studio"]["state"] = "open"
        with self.assertRaisesRegex(beat_tool.Refused, "First YuE2 Studio: that link is not connected yet"):
            self.app.act("trainer")

    def test_built_there_brought_home_and_a_studio_without_it_stopped(self):
        remote = FakeTrainerRemote(running="no", build_lines=[
            "@@ Building ace-train with 36 processors: [812/1450] compile steps", f"@@json {json.dumps(TRAINER_RECORD)}"])
        trainer = self.click(remote)
        self.assertEqual(trainer["state"], "ok", trainer["error"])
        self.assertEqual(remote.calls, ["found", "build", "get", "libraries", "uses", "stop"])
        self.assertIn(beat_tool.TRAINER_COMMIT, remote.script)
        self.assertIn("-DCMAKE_CUDA_ARCHITECTURES=86", remote.script)
        self.assertIn(beat_remote.CMAKE_SHA256, remote.script)
        self.assertEqual(self.cache.read_bytes(), remote.tar)
        saved = json.loads(self.cache.with_suffix("").with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tar_sha256"], hashlib.sha256(remote.tar).hexdigest())
        self.assertEqual(saved["exe_sha256"], TRAINER_RECORD["exe_sha256"])
        self.assertIn("a checked copy is kept here", trainer["facts"]["How"])
        self.assertIn("Studio running starts it again with it", trainer["note"])
        self.assertEqual(self.app.steps["start"]["state"], "open")      # the studio stopped: Start checks it again
        self.assertEqual(self.app.steps["studio"]["state"], "ok")

    def test_a_copy_from_home_is_sent_not_built_again(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_bytes(b"built before")
        digest = hashlib.sha256(b"built before").hexdigest()
        beat_tool.write_json(self.cache.with_suffix("").with_suffix(".json"), {**TRAINER_RECORD, "tar_sha256": digest})
        remote = FakeTrainerRemote(running="none")
        trainer = self.click(remote)
        self.assertEqual(trainer["state"], "ok", trainer["error"])
        self.assertEqual(remote.calls, ["found", "put", "unpack", "libraries", "uses"])
        self.assertEqual(remote.sent, (b"built before", beat_remote.trainer_folder(beat_tool.TRAINER_COMMIT) + ".tar.gz", digest))
        self.assertIn("sent from this PC", trainer["facts"]["How"])
        self.assertIn("Studio running starts YuE2 Studio with this trainer", trainer["note"])
        self.assertEqual(self.app.steps["start"]["state"], "open")     # a trainer newly verified: Start checks again

    def test_already_there_and_the_running_studio_uses_it(self):
        remote = FakeTrainerRemote(found=TRAINER_RECORD, running="yes")
        trainer = self.click(remote)
        self.assertEqual(trainer["state"], "ok", trainer["error"])
        self.assertEqual(remote.calls, ["found", "libraries", "uses"])
        self.assertIn("already uses this trainer", trainer["note"])
        self.assertEqual(self.studio.last, "runtime_status")

    def sent_from_home(self, libraries):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_bytes(b"built before")
        beat_tool.write_json(self.cache.with_suffix("").with_suffix(".json"),
                             {**TRAINER_RECORD, "tar_sha256": hashlib.sha256(b"built before").hexdigest()})
        remote = FakeTrainerRemote(running="none", libraries=libraries)
        return remote, self.click(remote)

    def test_a_trainer_from_home_gets_what_it_links_on_a_fresh_machine(self):
        # built where the compiler had brought OpenMP; a fresh RTX 4090 lacked libgomp.so.1 (23 Nov 2026)
        remote, trainer = self.sent_from_home("@@installed libgomp1\n@@scanned 14\n")
        self.assertEqual(trainer["state"], "ok", trainer["error"])
        self.assertIn("installed what it links here: libgomp1", trainer["facts"]["How"])
        self.assertIn(beat_remote.trainer_folder(beat_tool.TRAINER_COMMIT), remote.libraries_script)
        self.assertIn("libgomp.so.1=libgomp1", remote.libraries_script)

    def test_a_library_no_package_brings_keeps_the_trainer_red(self):
        folder = beat_remote.trainer_folder(beat_tool.TRAINER_COMMIT)
        remote, trainer = self.sent_from_home(f"@@missing libweird.so.3 {folder}/libggml-cpu-zen4.so\n@@scanned 14\n")
        self.assertEqual(trainer["state"], "failed")
        self.assertIn("Libraries missing on the rented machine that Beat Tool has no package for: libweird.so.3 "
                      f"(needed by {folder}/libggml-cpu-zen4.so)", trainer["error"])
        self.assertNotIn("uses", remote.calls)

    def test_a_trainer_already_there_is_checked_too(self):
        # a first try that stopped at a missing library leaves the trainer unpacked: the next must not pass it unchecked
        remote = FakeTrainerRemote(found=TRAINER_RECORD, running="none",
                                   libraries="@@missing libgomp.so.1 /root/beat_tool/trainer-5ae17a66/ace-train\n@@scanned 14\n")
        trainer = self.click(remote)
        self.assertEqual(trainer["state"], "failed")
        self.assertIn("libgomp.so.1 (needed by /root/beat_tool/trainer-5ae17a66/ace-train)", trainer["error"])

    def test_a_check_that_ends_early_is_not_a_pass(self):
        remote = FakeTrainerRemote(found=TRAINER_RECORD, running="none", libraries="Connection reset\n")
        trainer = self.click(remote)
        self.assertEqual(trainer["state"], "failed")
        self.assertIn("Checking the libraries on the rented machine ended early", trainer["error"])

    def test_a_trainer_for_another_card_is_built_again(self):
        remote = FakeTrainerRemote(found={**TRAINER_RECORD, "architectures": "89"},
                                   build_lines=[f"@@json {json.dumps(TRAINER_RECORD)}"])
        self.assertEqual(self.click(remote)["state"], "ok")
        self.assertIn("build", remote.calls)

    def test_a_failed_build_shows_its_log(self):
        remote = FakeTrainerRemote(build_lines=["@@ Configuring ace-train…", "@@log CMake Error at CMakeLists.txt:153",
                                                "@@error CMake could not configure the trainer; its log is /root/x.log"])
        trainer = self.click(remote)
        self.assertEqual(trainer["state"], "failed")
        self.assertIn("CMake could not configure the trainer", trainer["error"])
        self.assertIn("CMake Error at CMakeLists.txt:153", trainer["error"])
        self.assertFalse(self.cache.exists())

    def test_a_card_too_small_is_refused(self):
        self.app.gpu = {**self.app.gpu, "name": "RTX 3060 Ti", "vram_gb": 8.0}
        trainer = self.click(FakeTrainerRemote())
        self.assertEqual(trainer["state"], "failed")
        self.assertIn("needs about 11 GB", trainer["error"])

    def test_the_studio_starts_with_the_trainer(self):
        script = beat_remote.launch_script("/root/beat_tool/studio", "/root/beat_tool/studio/log", trainer="/root/t/ace-train")
        self.assertIn("export YUE_TRAIN_BIN=/root/t/ace-train", script)
        self.assertNotIn("YUE_TRAIN_BIN", beat_remote.launch_script("/root/beat_tool/studio", "/root/beat_tool/studio/log"))

    @unittest.skipUnless(shutil.which("bash"), "bash is not on this PC")
    def test_every_script_is_valid_bash(self):
        folder = beat_remote.trainer_folder(beat_tool.TRAINER_COMMIT)
        scripts = {
            "build": beat_remote.trainer_build_script(beat_tool.TRAINER_REPO, beat_tool.TRAINER_COMMIT, "86", folder),
            "unpack": beat_remote.trainer_unpack_script(folder + ".tar.gz", folder, "ab" * 32),
            "found": beat_remote.trainer_found_script(folder),
            "uses": beat_remote.studio_trainer_script("/root/s/usr/bin/YuE2-Studio", folder + "/ace-train"),
            "stop": beat_remote.stop_studio_script("/root/s/usr/bin/YuE2-Studio"),
            "launch": beat_remote.launch_script("/root/s", "/root/s/log", trainer=folder + "/ace-train"),
            "gpu": beat_remote.GPU_SCRIPT,
            "compute": beat_remote.compute_script(),
            "libraries": beat_remote.libraries_script(["/root/beat_tool/s", folder]),
            "studio": beat_remote.studio_script(beat_tool.LINUX_URL, beat_tool.LINUX_ASSET, beat_tool.LINUX_BYTES,
                                                beat_tool.LINUX_SHA256, "/root/beat_tool/s", beat_tool.MARKER),
        }
        for name, script in scripts.items():
            done = subprocess.run([shutil.which("bash"), "-n"], input=script.encode(), capture_output=True)
            self.assertEqual(done.returncode, 0, f"{name}: {done.stderr.decode(errors='replace')}")


# ------------------------------------------------------------ Find machines: RTX 4090 / 5090, reliable, fast both ways

def offer(**changes):
    return {"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1, "gpu_ram": 24564, "dph_total": 0.5, "geolocation": "Norway, NO",
            "reliability": 0.998, "inet_down": 900.0, "inet_up": 880.0, "driver_version": "595.99.02", "cuda_max_good": 13.2,
            "compute_cap": 890, "disk_space": 400, "cpu_cores_effective": 16, "cpu_ram": 64000, **changes}


class OffersTest(unittest.TestCase):
    def search(self, by_card):
        """vast.ai answering each card's search with that card's offers."""
        asked = []

        def urlopen(request, timeout):
            query = json.loads(request.data)
            asked.append((query, request.full_url))
            return io.BytesIO(json.dumps({"offers": by_card.get(query["gpu_name"]["eq"], [])}).encode())
        return beat_remote.Vast("key", urlopen=urlopen).offers(), asked

    def test_asks_vast_for_4090_and_5090_only_more_than_99_5_reliable_and_fast_both_ways(self):
        _found, asked = self.search({})
        self.assertEqual([query["gpu_name"] for query, _url in asked], [{"eq": "RTX 4090"}, {"eq": "RTX 5090"}])
        for query, url in asked:
            self.assertTrue(url.endswith("/bundles/"))
            self.assertEqual(query["reliability"], {"gt": 0.995})
            self.assertEqual((query["inet_down"], query["inet_up"]), ({"gte": 500}, {"gte": 500}))
            self.assertEqual((query["disk_space"], query["allocated_storage"]), ({"gte": 120}, 120))
            self.assertEqual(query["num_gpus"], {"eq": 1})
            self.assertNotIn("gpu_ram", query)

    def test_keeps_only_the_machines_that_fit(self):
        fours = [offer(id=1), offer(id=3, gpu_name="RTX 3090"), offer(id=4, gpu_name="RTX PRO 4000"),
                 offer(id=5, reliability=0.995), offer(id=6, reliability=0.9949), offer(id=7, inet_up=88),
                 offer(id=8, inet_down=499), offer(id=9, driver_version="575.57.08"), offer(id=10, num_gpus=2)]
        fives = [offer(id=2, gpu_name="RTX 5090", gpu_ram=32607, compute_cap=1200, inet_up=500, dph_total=0.3)]
        (shown, seen), _asked = self.search({"RTX 4090": fours, "RTX 5090": fives})
        self.assertEqual([entry["id"] for entry in shown], [2, 1])             # cheapest first, across both cards
        self.assertEqual(seen, 10)
        self.assertEqual((shown[1]["down_mbps"], shown[1]["up_mbps"], shown[1]["reliability"]), (900, 880, 99.8))
        self.assertEqual(shown[0]["vram_gb"], 31.8)

    def test_dearer_5090s_are_not_crowded_out_by_4090s(self):
        fours = [offer(id=100 + n, dph_total=0.4 + n / 1000) for n in range(40)]
        fives = [offer(id=200 + n, gpu_name="RTX 5090", dph_total=0.7 + n / 1000) for n in range(3)]
        (shown, _seen), _asked = self.search({"RTX 4090": fours, "RTX 5090": fives})
        self.assertEqual(sum(entry["gpu"] == "RTX 4090" for entry in shown), 12)
        self.assertEqual(sum(entry["gpu"] == "RTX 5090" for entry in shown), 3)

    def test_the_page_is_told_the_rule(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        class FakeVast:
            def __init__(self, key, sleep=None):
                pass

            def offers(self):
                return [beat_remote.offer_view(offer())], 1

            def user(self):
                return {"credit": 12.5}

            def instance(self, _id):
                return None
        app = App(folder=Path(temp.name), studio=FakeStudio(), vast_api=FakeVast)
        app.keys.load = lambda: "key"
        app.act("vast_offers").join(10)
        data = app.steps["where"]["data"]
        self.assertEqual(app.steps["where"]["error"], "")
        self.assertEqual(data["offers_rule"], {"gpus": "RTX 4090 or RTX 5090", "reliability": 99.5, "mbps": 500})
        self.assertEqual(len(data["offers"]), 1)

    def test_a_machine_rented_by_someone_else_first_rents_nothing_and_the_list_is_searched_again(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        searched = []

        class FakeVast:
            def __init__(self, key, sleep=None):
                pass

            def offers(self):
                searched.append(1)
                return [beat_remote.offer_view(offer(id=9))], 1

            def rent(self, offer_id, public):
                raise beat_remote.VastError(
                    f'vast.ai answered HTTP 400 to PUT /asks/{offer_id}/: {{"success":false,"error":"invalid_args",'
                    f'"msg":"error 404/3603: no_such_ask  Instance type by id {offer_id} is not available."}}')

            def instance(self, _id):
                return None
        app = App(folder=Path(temp.name), studio=FakeStudio(), vast_api=FakeVast)
        app.keys.load = lambda: "key"
        (app.vast_dir).mkdir(parents=True, exist_ok=True)
        for name in ("beat_tool_ed25519", "beat_tool_ed25519.pub"):       # this tool's SSH key, made before
            (app.vast_dir / name).write_text("ssh-ed25519 AAAA beat-tool\n", encoding="ascii")
        app.steps["where"]["data"]["offers"] = [beat_remote.offer_view(offer(id=61040417))]
        app.act("vast_rent", {"offer": "61040417"}).join(10)
        where = app.steps["where"]
        self.assertIn("was rented by someone else just before your click", where["error"])
        self.assertIn("Nothing was rented and nothing is billed. The list has been searched again", where["error"])
        self.assertEqual([entry["id"] for entry in where["data"]["offers"]], [9])
        self.assertEqual(searched, [1])
        self.assertFalse((app.vast_dir / "instance.json").exists())
        self.assertEqual((app.mode, where["state"]), ("pc", "ok"))


# ------------------------------------------------------------ the rented machine's GPU step: disk for everything

class FakeShell:
    """A rented machine that answers every script with the same text."""

    def __init__(self, out):
        self.out, self.scripts = out, []

    def __str__(self):
        return "root@203.0.113.7:40000"

    def sh(self, script, timeout=120):
        self.scripts.append(script)
        return self.out


class GpuRemoteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(folder=Path(self.temp.name), studio=FakeStudio())
        self.app.mode = "vast"
        self.app.steps["where"]["state"] = "ok"

    def tearDown(self):
        self.temp.cleanup()

    def gpu(self, disk, ours):
        self.app.remote = FakeShell("NVIDIA GeForce RTX 4090, 24564, 595.99.02, 8.9\n"
                                    "@@cuda libcublas.so.13 libcudart.so.13 libcufft.so.12 \n"
                                    f"@@disk {disk}\n@@ours {ours}\n@@os Ubuntu 22.04.5 LTS\n")
        self.app.act("gpu").join(10)
        return self.app.steps["gpu"]

    def test_a_fresh_machine_with_room(self):
        gpu = self.gpu(110, "")
        self.assertEqual(gpu["state"], "ok", gpu["error"])
        self.assertEqual(gpu["facts"]["Disk free"], "110 GB")
        self.assertIn("du -s -BG /root/beat_tool", self.app.remote.scripts[0])

    def test_what_beat_tool_already_holds_there_counts(self):
        gpu = self.gpu(40, 35)
        self.assertEqual(gpu["state"], "ok", gpu["error"])
        self.assertIn("Beat Tool's folder holds 35 GB more", gpu["facts"]["Disk free"])

    def test_too_little_disk_is_refused(self):
        gpu = self.gpu(50, 0)
        self.assertEqual(gpu["state"], "failed")
        self.assertIn("only 50 GB of disk is free", gpu["error"])
        self.assertIn("Stop renting it and rent another", gpu["error"])


# ------------------------------------------------------------ CUDA (and Vulkan) tested by using them, before the studio

PROBE = {"driver_cuda": "13.2", "cards": 1, "name": "NVIDIA GeForce RTX 4090", "compute": "8.9", "vram_gb": 23.5,
         "memory": "256 MB taken on the card, filled there and read back",
         "runtime": {"libcudart.so.13": "CUDA 13.0, sees 1 card(s)", "libcublas.so.13": "loads", "libcufft.so.12": "loads"}}
VULKAN_CARD = ["GPU0:", "\tapiVersion         = 1.4.312", "\tvendorID           = 0x10de",
               "\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU", "\tdeviceName         = NVIDIA GeForce RTX 4090",
               "\tdriverName         = NVIDIA", "\tdriverInfo         = 595.99.02"]
VULKAN_SOFTWARE = ["GPU1:", "\tvendorID           = 0x10005", "\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU",
                   "\tdeviceName         = llvmpipe (LLVM 15.0.7, 256 bits)"]


class FakeComputeRemote:
    """The rented machine as the CUDA step sees it: what compute_script prints there."""

    def __init__(self, lines):
        self.lines, self.script = lines, ""

    def __str__(self):
        return "root@203.0.113.7:40000"

    def stream(self, script, on_line, timeout=3600):
        self.script = script
        for line in self.lines:
            on_line(line)
        return self.lines


class CudaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(folder=Path(self.temp.name), studio=FakeStudio())
        self.app.steps["gpu"]["state"] = "ok"

    def tearDown(self):
        self.temp.cleanup()

    def test_comes_before_the_studio_on_this_pc_and_on_a_rented_machine(self):
        for order in (beat_tool.SETUP_PC, beat_tool.SETUP_VAST):
            self.assertEqual(order[:4], ("where", "gpu", "cuda", "studio"))
            self.assertEqual(order[-1], "open")

    def on_pc(self, stdout, stderr=""):
        asked = []

        def run(command, **kwargs):
            asked.append(command)
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)
        self.app.run = run
        self.app.act("cuda").join(10)
        self.assertEqual(asked[0][:4], [sys.executable, "-I", "-c", beat_remote.CUDA_PROBE])
        return self.app.steps["cuda"]

    def test_on_this_pc(self):
        cuda = self.on_pc("@@json " + json.dumps({**PROBE, "name": "NVIDIA GeForce RTX 3060", "runtime": {}}) + "\n")
        self.assertEqual(cuda["state"], "ok", cuda["error"])
        self.assertIn("NVIDIA GeForce RTX 3060 · compute capability 8.9", cuda["facts"]["Card"])
        self.assertEqual(cuda["facts"]["On the card"], PROBE["memory"])

    def test_on_this_pc_a_cuda_error_is_red(self):
        cuda = self.on_pc("@@json " + json.dumps({"driver_cuda": "13.2", "name": "NVIDIA GeForce RTX 3060",
                                                  "error": "cuMemAlloc (256 MB of the card's memory) failed: CUDA error 2 (out of memory)"}))
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("CUDA does not work on this PC: cuMemAlloc", cuda["error"])
        self.assertIn("It got as far as", cuda["error"])
        self.assertEqual(self.app.steps["studio"]["state"], "open")

    def test_on_this_pc_no_answer(self):
        cuda = self.on_pc("", "Traceback (most recent call last):\nOSError: [WinError 126]")
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("ended without an answer", cuda["error"])
        self.assertIn("WinError 126", cuda["error"])

    def on_rented(self, lines):
        self.app.mode = "vast"
        self.app.steps["where"]["state"] = "ok"
        self.app.remote = FakeComputeRemote(lines)
        self.app.act("cuda").join(10)
        return self.app.steps["cuda"]

    def test_rented_machine_with_cuda_and_vulkan_on_the_card(self):
        cuda = self.on_rented(["@@ Installing what the test needs: python3 vulkan-tools…", "@@icd /usr/share/vulkan/icd.d/nvidia_icd.json",
                               "@@glx 2", "@@ Testing CUDA on the card…", "@@json " + json.dumps(PROBE),
                               "@@vulkan-begin", "==========", "Devices:", *VULKAN_CARD, *VULKAN_SOFTWARE, "@@vulkan-end 0"])
        self.assertEqual(cuda["state"], "ok", cuda["error"])
        self.assertIn("NVIDIA GeForce RTX 4090 · NVIDIA 595.99.02", cuda["facts"]["Vulkan"])
        self.assertEqual(cuda["facts"]["libcudart.so.13"], "CUDA 13.0, sees 1 card(s)")
        self.assertEqual(self.app.snapshot()["labels"]["cuda"], "CUDA and Vulkan")
        for library in beat_remote.RUNTIME_LIBRARIES:
            self.assertIn(library, self.app.remote.script)

    def test_rented_machine_whose_vulkan_finds_only_the_processor(self):
        cuda = self.on_rented(["@@icd none", "@@glx 0", "@@json " + json.dumps(PROBE), "@@vulkan-begin", *VULKAN_SOFTWARE,
                               "@@vulkan-end 0"])
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("Vulkan finds no NVIDIA card on the rented machine, only llvmpipe", cuda["error"])
        self.assertIn("only the compute part of the NVIDIA driver, not its Vulkan part (libGLX_nvidia). Stop renting it",
                      cuda["error"])

    def test_the_drivers_vulkan_part_the_host_left_out_is_added(self):
        # a rented RTX 4090 whose host mounted only the compute part of driver 595.91.07 (23 Nov 2026)
        cuda = self.on_rented(["@@ The host gave this machine only the compute part of NVIDIA driver 595.91.07; adding its Vulkan part…",
                               "@@nvgl libnvidia-gl 595.91.07 from NVIDIA's CUDA repository",
                               "@@icd /usr/share/vulkan/icd.d/nvidia_icd.json", "@@glx 1", "@@glxmissing ", "@@egl 1",
                               "@@json " + json.dumps(PROBE), "@@vulkan-begin", *VULKAN_CARD, "@@vulkan-end 0"])
        self.assertEqual(cuda["state"], "ok", cuda["error"])
        self.assertEqual(cuda["facts"]["Vulkan part of the driver"],
                         "added by Beat Tool (libnvidia-gl 595.91.07 from NVIDIA's CUDA repository): the host had left it out")

    def test_the_drivers_vulkan_part_that_cannot_be_added_says_why(self):
        cuda = self.on_rented(["@@nvglfail neither NVIDIA's CUDA repository nor NVIDIA's download site has driver 595.91.07 "
                               "(curl: (22) The requested URL returned error: 404)", "@@icd none", "@@glx 0",
                               "@@json " + json.dumps(PROBE), "@@vulkan-begin", *VULKAN_SOFTWARE, "@@vulkan-end 0"])
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("Beat Tool could not add it: neither NVIDIA's CUDA repository nor NVIDIA's download site has "
                      "driver 595.91.07", cuda["error"])
        self.assertIn("Stop renting it and rent another", cuda["error"])

    def test_the_vulkan_part_is_taken_at_exactly_the_drivers_version(self):
        script = beat_remote.compute_script()
        self.assertIn("if ! have 'libGLX_nvidia.so.0'; then", script)
        self.assertIn("nvidia-smi --query-gpu=driver_version", script)
        self.assertIn('awk -F\'|\' -v v="$v-"', script)
        self.assertIn("pick libnvidia-gl \"libnvidia-gl-${v%%.*}\"", script)
        self.assertIn("https://download.nvidia.com/XFree86/Linux-x86_64/$v/$run", script)
        # what the host mounted is never written over
        self.assertIn('[ ! -e "/usr/lib/x86_64-linux-gnu/$lib.so.$v" ]', script)
        self.assertLess(script.index("libGLX_nvidia.so.0'; then\n  v="), script.index("@@icd "))

    def test_a_driver_that_lacks_libegl_is_not_the_machines_fault(self):
        # driver 615 opens libEGL.so.1 before it starts Vulkan and refuses without it (rented RTX 4090, 23 Nov 2026)
        cuda = self.on_rented(["@@icd /etc/vulkan/icd.d/nvidia_icd.json", "@@glx 1", "@@glxmissing ", "@@egl 0",
                               "@@json " + json.dumps(PROBE), "@@vulkan-begin",
                               "ERROR: [Loader Message] Code 0 : loader_scanned_icd_add: Could not get 'vkCreateInstance' "
                               "via 'vk_icdGetInstanceProcAddr' for ICD libGLX_nvidia.so.0",
                               "ERROR at ./vulkaninfo/vulkaninfo.h:649:vkCreateInstance failed with ERROR_INCOMPATIBLE_DRIVER",
                               "@@vulkan-end 1"])
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("what it needs is missing: libEGL.so.1", cuda["error"])
        self.assertIn("The machine itself is not at fault", cuda["error"])
        self.assertNotIn("Stop renting", cuda["error"])

    def test_one_card_listed_twice_is_refused(self):
        cuda = self.on_rented(["@@icd /etc/vulkan/icd.d/nvidia_icd.json", "@@glx 1", "@@json " + json.dumps(PROBE),
                               "@@vulkan-begin", *VULKAN_CARD, *[line.replace("GPU0", "GPU1") for line in VULKAN_CARD],
                               "@@vulkan-end 0"])
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("lists the one NVIDIA card more than once", cuda["error"])

    def test_the_machine_gets_what_the_cards_vulkan_driver_needs_before_the_test(self):
        script = beat_remote.compute_script()
        for package in ("libxext6", "libx11-6", "libglvnd0", "libegl1", "vulkan-tools", "python3"):
            self.assertIn(package, script)
        self.assertNotIn("if ! ls /usr/share/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.json", script)
        self.assertNotIn("if ! ls /usr/share/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.json",
                         beat_remote.studio_script("u", "n", 1, "s", "/root/beat_tool/f", "m"))

    def test_rented_machine_whose_cuda_fails(self):
        cuda = self.on_rented(["@@json " + json.dumps({"error": "cuInit (the driver starting CUDA) failed: CUDA error 999"})])
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("CUDA does not work on the rented machine: cuInit", cuda["error"])
        self.assertIn("Stop renting this machine", cuda["error"])

    def test_rented_machine_whose_packages_do_not_install(self):
        cuda = self.on_rented(["@@log E: Unable to locate package vulkan-tools",
                               "@@error The packages for the test ( python3 vulkan-tools) did not install on the rented machine."])
        self.assertEqual(cuda["state"], "failed")
        self.assertIn("did not install", cuda["error"])
        self.assertIn("Unable to locate package vulkan-tools", cuda["error"])

    def test_vulkan_summary_is_read_device_by_device(self):
        devices = beat_remote.vulkan_devices(["Instance Extensions: count = 20", *VULKAN_CARD, *VULKAN_SOFTWARE])
        self.assertEqual([device["deviceName"] for device in devices],
                         ["NVIDIA GeForce RTX 4090", "llvmpipe (LLVM 15.0.7, 256 bits)"])

    @unittest.skipUnless(sys.platform == "win32" and Path(r"C:\Windows\System32\nvcuda.dll").is_file(),
                         "no NVIDIA driver on this PC")
    def test_the_probe_for_real_on_this_pc(self):
        done = subprocess.run([sys.executable, "-I", "-c", beat_remote.CUDA_PROBE], capture_output=True, text=True,
                              timeout=120)
        found = json.loads([line for line in done.stdout.splitlines() if line.startswith("@@json ")][-1][7:])
        self.assertNotIn("error", found)
        self.assertEqual(found["memory"], "256 MB taken on the card, filled there and read back")


# ------------------------------------------------------------ Prepare everything

class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(folder=Path(self.temp.name), studio=FakeStudio())
        self.assertEqual((self.app.mode, self.app.steps["where"]["state"]), ("pc", "ok"))
        self.ran, self.fail, self.gate = [], set(), None
        for step in ("gpu", "cuda", "studio", "start", "engine", "parts"):
            setattr(self.app, "do_" + step, self.work(step))
        self.app.do_models_read = lambda _body: self.app.steps["models"].__setitem__("data", {
            "options": [{"id": "light", "transcriber": True}, {"id": "quality", "transcriber": True},
                        {"id": "tiny", "transcriber": False}], "suggested": "light"})

        def use(body):
            self.ran.append("models:" + body["profile_id"])
            self.app.passed("models", {}, key=body["profile_id"])
        self.app.do_models_use = use

    def tearDown(self):
        if self.gate:
            self.gate.set()
        self.temp.cleanup()

    def work(self, step):
        def run(_body):
            if self.gate and step == "studio":
                self.gate.wait(5)
            self.ran.append(step)
            if step in self.fail:
                raise ValueError(f"{step} broke")
            self.app.passed(step, {}, key=step)
        return run

    def test_runs_every_step_in_order_until_only_open_is_left(self):
        self.app.prepare_click({"profile_id": "quality"}).join(10)
        self.assertEqual(self.ran, ["gpu", "cuda", "studio", "start", "models:quality", "engine", "parts"])
        state = self.app.snapshot()
        self.assertIsNone(state["preparing"])
        self.assertTrue(state["can"]["open"])
        self.assertEqual(state["steps"]["open"]["state"], "open")      # opening stays your click

    def test_verified_steps_are_not_run_again(self):
        for step in ("gpu", "cuda"):
            self.app.steps[step]["state"] = "ok"
        self.app.prepare_click().join(10)
        self.assertEqual(self.ran, ["studio", "start", "models:light", "engine", "parts"])

    def test_a_model_set_that_cannot_read_a_recording_falls_back_to_the_preselected_one(self):
        self.app.prepare_click({"profile_id": "tiny"}).join(10)
        self.assertIn("models:light", self.ran)

    def test_stops_at_the_first_step_that_fails(self):
        self.fail = {"engine"}
        self.app.prepare_click().join(10)
        self.assertEqual(self.ran[-1], "engine")
        state = self.app.snapshot()
        self.assertEqual(state["steps"]["engine"]["state"], "failed")
        self.assertEqual(state["steps"]["engine"]["error"], "engine broke")
        self.assertEqual(state["steps"]["parts"]["state"], "open")
        self.assertFalse(state["can"]["open"])
        self.assertIsNone(state["busy"])

    def test_other_clicks_wait_while_it_prepares(self):
        self.gate = threading.Event()
        thread = self.app.prepare_click()
        deadline = time.monotonic() + 5
        while self.app.busy != "studio" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.app.snapshot()["preparing"], "studio")
        with self.assertRaisesRegex(beat_tool.Refused, r"Prepare everything is working \(YuE2 Studio now\)"):
            self.app.act("gpu")
        with self.assertRaisesRegex(beat_tool.Refused, "YuE2 Studio is still working"):
            self.app.prepare_click()
        with self.assertRaisesRegex(beat_tool.Refused, "Prepare everything is working"):
            self.app.open_click()
        self.gate.set()
        thread.join(10)
        self.assertEqual(self.ran[-1], "parts")

    def test_refused_before_where_it_runs(self):
        self.app.steps["where"]["state"] = "open"
        with self.assertRaisesRegex(beat_tool.Refused, "First Where it runs"):
            self.app.prepare_click()


# ------------------------------------------------------------ Studio parts, downloaded ahead

class PartsStudio:
    """The studio's downloadable parts: each one asked for downloads over two polls, then counts ready; a part in
    stall never moves."""

    def __init__(self, ready=(), stall=(), card_trains=True, trainer_installed=True, fails=(), stale=()):
        self.ready, self.stall, self.asked, self.polls = set(ready), set(stall), [], {}
        self.card_trains, self.trainer_installed, self.last = card_trains, trainer_installed, None
        self.fails, self.stale = set(fails), set(stale)      # failing every try; showing an old failure at first

    def progress(self, part):
        failed = {"asset_id": "onnxruntime", "downloaded_bytes": 0, "total_bytes": 11306877, "done": True,
                  "error": "ONNX Runtime: https://github.com/microsoft/onnxruntime/releases/download/v1.30.0/"
                           "onnxruntime-linux-x64-1.30.0.tgz answered 404 Not Found"}
        if part in self.fails and part not in self.ready:
            return failed
        if part in self.ready or part not in self.asked or part in self.stall:
            return None
        if part in self.stale:
            self.stale.discard(part)
            return failed                   # one poll still shows the earlier try's failure
        self.polls[part] = self.polls.get(part, 0) + 1
        if self.polls[part] >= 2:
            self.ready.add(part)
            return {"asset_id": part, "downloaded_bytes": 10, "total_bytes": 10, "done": True}
        return {"asset_id": part, "downloaded_bytes": 5, "total_bytes": 10, "done": False}

    def call(self, name, arguments=None, timeout=None, text_ok=False):
        self.last = name
        installs = {"separator_runtime_install": "separator", "training_pack_install": "training",
                    "midi_install": "midi", "training_listen_pack_install": "listen"}
        if name in installs:
            self.asked.append(installs[name])
            return {"started": True}
        if name == "separator_status":
            done = "separator" in self.ready
            return {"model": {"id": "htdemucs", "label": "HT-Demucs", "installed": done}, "runtime_installed": done,
                    "card": "NVIDIA GeForce RTX 4090"}
        if name == "separator_runtime":
            active = self.progress("separator")
            return {"assets": [{"id": "htdemucs", "label": "HT-Demucs", "bytes": 300_000_000,
                                "installed": "separator" in self.ready}], "active_download": active}
        if name == "runtime_status":
            part = {"training_pack": "training", "training_listen": "listen"}[arguments["part"]]
            active, done = self.progress(part), part in self.ready
            answer = {"assets": [], "set": {"bytes": 8_600_000_000, "installed_bytes": 8_600_000_000 if done else 0,
                                            "ready": done}, "active_download": active}
            if part == "training":
                answer.update(card_trains=self.card_trains, card_needs=None if self.card_trains else "driver",
                              min_vram_gb=11, assets=[{"id": "music-train", "installed": self.trainer_installed}])
            return answer
        if name == "midi_status":
            active, done = self.progress("midi"), "midi" in self.ready
            return {"tool_installed": done, "default_size": "medium", "download": active,
                    "sizes": [{"id": "medium", "params": "307M", "installed": done, "missing_bytes": 0 if done else 1_228_144_472}]}
        raise AssertionError(f"Studio parts called {name}")


class PartsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(folder=Path(self.temp.name), studio=PartsStudio(), sleep=lambda _seconds: None)
        self.app.part_stall = 0.05
        for step in beat_tool.SETUP_PC[:beat_tool.SETUP_PC.index("parts")]:
            self.app.steps[step]["state"] = "ok"

    def tearDown(self):
        self.temp.cleanup()

    def click(self, studio):
        self.app.studio = studio
        self.app.act("parts").join(10)
        return self.app.steps["parts"]

    def test_downloads_each_missing_part_in_turn(self):
        studio = PartsStudio(ready={"separator"})
        parts = self.click(studio)
        self.assertEqual(parts["state"], "ok", parts["error"])
        self.assertEqual(studio.asked, ["training", "midi", "listen"])
        self.assertTrue(all(value.startswith("ready") for value in parts["facts"].values()), parts["facts"])
        self.assertTrue(self.app.snapshot()["can"]["open"])

    def test_nothing_is_downloaded_when_everything_is_there(self):
        studio = PartsStudio(ready={"separator", "training", "midi", "listen"})
        self.assertEqual(self.click(studio)["state"], "ok")
        self.assertEqual(studio.asked, [])

    def test_a_download_that_stops_without_a_word_is_red(self):
        parts = self.click(PartsStudio(ready={"separator", "training"}, stall={"midi"}))
        self.assertEqual(parts["state"], "failed")
        self.assertIn("Audio to MIDI (MuScriptor) stopped downloading without being ready", parts["error"])

    def test_the_listening_pack_failing_is_told_and_does_not_block(self):
        # ONNX Runtime's own release on GitHub answered 404 on 23 Nov 2026: the studio cannot complete this pack
        studio = PartsStudio(ready={"separator", "training", "midi"}, fails={"listen"})
        parts = self.click(studio)
        self.assertEqual(parts["state"], "ok", parts["error"])
        fact = parts["facts"]["Listening pack (MOSS-Music, tempo)"]
        self.assertIn("not complete, optional (Open YuE2 Studio does not wait for it)", fact)
        self.assertIn("onnxruntime-linux-x64-1.30.0.tgz answered 404 Not Found", fact)
        self.assertIn("Describe by ear may not work", parts["note"])
        listen = next(entry for entry in parts["data"]["parts"] if entry["id"] == "listen")
        self.assertTrue(listen["optional"])
        self.assertIn("404 Not Found", listen["skipped"])
        self.assertTrue(self.app.snapshot()["can"]["open"])

    def test_a_required_part_failing_still_stops_the_step(self):
        parts = self.click(PartsStudio(ready={"separator", "listen"}, fails={"training"}))
        self.assertEqual(parts["state"], "failed")
        self.assertIn("Training pack (LoRA): the download failed", parts["error"])

    def test_an_earlier_tries_failure_still_shown_is_not_this_tries(self):
        studio = PartsStudio(ready={"separator", "training", "listen"}, stale={"midi"})
        parts = self.click(studio)
        self.assertEqual(parts["state"], "ok", parts["error"])
        self.assertIn("ready", parts["facts"]["Audio to MIDI (MuScriptor)"])

    def test_a_card_that_cannot_train_is_named_and_does_not_block(self):
        studio = PartsStudio(ready={"separator", "midi", "listen"}, card_trains=False)
        parts = self.click(studio)
        self.assertEqual(parts["state"], "ok", parts["error"])
        self.assertNotIn("training", studio.asked)
        self.assertIn("not on this card: the card or its driver runs CUDA 12 only", parts["facts"]["Training pack (LoRA)"])

    def test_on_a_rented_machine_the_studio_must_run_with_the_trainer(self):
        self.app.mode = "vast"
        for step in beat_tool.SETUP_VAST[:beat_tool.SETUP_VAST.index("parts")]:
            self.app.steps[step]["state"] = "ok"
        studio = PartsStudio(ready={"separator"}, trainer_installed=False)
        parts = self.click(studio)
        self.assertEqual(parts["state"], "failed")
        self.assertIn("does not count the LoRA trainer installed", parts["error"])
        self.assertNotIn("training", studio.asked)

    def on_rented(self, libraries):
        self.app.mode = "vast"
        self.app.remote_folder = "/root/beat_tool/studio"
        for step in beat_tool.SETUP_VAST[:beat_tool.SETUP_VAST.index("parts")]:
            self.app.steps[step]["state"] = "ok"
        remote = FakeTrainerRemote(libraries=libraries)
        self.app.remote = remote
        return remote, self.click(PartsStudio(ready={"separator", "training", "midi", "listen"}))

    def test_on_a_rented_machine_what_the_studio_links_is_there_before_open(self):
        remote, parts = self.on_rented("@@installed libasound2\n"
                                       "@@missing libdc1394.so.25 /root/beat_tool/studio/usr/lib/gstreamer-1.0/libgstdc1394.so\n"
                                       "@@scanned 664\n")
        self.assertEqual(parts["state"], "ok", parts["error"])
        self.assertEqual(remote.calls, ["libraries"])
        self.assertIn("/root/beat_tool/studio", remote.libraries_script)
        self.assertIn(beat_remote.trainer_folder(beat_tool.TRAINER_COMMIT), remote.libraries_script)
        self.assertEqual(parts["facts"]["System libraries"], "all there; installed now: libasound2")
        self.assertIn("libdc1394.so.25 (needed by /root/beat_tool/studio/usr/lib/gstreamer-1.0/libgstdc1394.so)",
                      parts["facts"]["Not needed, missing"])

    def test_on_a_rented_machine_a_studio_program_missing_a_library_is_red(self):
        _, parts = self.on_rented("@@missing libweird.so.3 /root/beat_tool/studio/runtimes/engine/yue-server\n@@scanned 664\n")
        self.assertEqual(parts["state"], "failed")
        self.assertIn("libweird.so.3 (needed by /root/beat_tool/studio/runtimes/engine/yue-server)", parts["error"])
        self.assertFalse(self.app.snapshot()["can"]["open"])

    def test_the_first_step_installs_what_the_studio_and_trainer_link(self):
        script = beat_remote.compute_script()
        for library, package in beat_remote.MACHINE_PACKAGES.items():
            self.assertIn(f'have {library} || need="$need {package}"', script)
        self.assertEqual(beat_remote.MACHINE_PACKAGES["libgomp.so.1"], "libgomp1")


# ------------------------------------------------------------ Open YuE2 Studio: the last check

class ReadyStudio(PartsStudio):
    def __init__(self, backend="cuda", engine_ready=True, **parts):
        super().__init__(**{"ready": {"separator", "training", "midi", "listen"}, **parts})
        self.backend, self.engine_ready = backend, engine_ready

    def hello(self):
        return {"name": "yue2-studio", "version": beat_tool.STUDIO_VERSION}

    def call(self, name, arguments=None, timeout=None, text_ok=False):
        if name == "models_status":
            return {"selected_profile_id": "quality", "ready": True, "engine_ready": self.engine_ready}
        if name == "engine_options_get":
            return {"options": {"backend": self.backend}}
        return super().call(name, arguments, timeout, text_ok)


class OpenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(folder=Path(self.temp.name), studio=ReadyStudio())
        self.app.profile = {"id": "quality", "label": "Quality Q8_0"}
        self.app.studio_open = lambda: "http://127.0.0.1:8791/"
        for step in beat_tool.SETUP_PC[:-1]:
            self.app.steps[step]["state"] = "ok"

    def tearDown(self):
        self.temp.cleanup()

    def test_everything_ready_opens_the_studio(self):
        self.assertEqual(self.app.open_click(), "http://127.0.0.1:8791/")
        opened = self.app.steps["open"]
        self.assertEqual(opened["state"], "ok")
        self.assertEqual(opened["facts"]["Engine"], "cuda, ready")
        self.assertIn("Training pack (LoRA)", opened["facts"]["Parts"])
        self.assertIn("GB", opened["facts"]["Disk free"])
        self.assertIsNone(self.app.busy)

    def test_an_engine_back_on_auto_turns_its_step_red_and_open_waits_again(self):
        self.app.studio = ReadyStudio(backend="auto")
        with self.assertRaisesRegex(ValueError, 'the engine\'s backend is "auto", not "cuda"'):
            self.app.open_click()
        self.assertEqual(self.app.steps["engine"]["state"], "failed")
        self.assertEqual(self.app.steps["parts"]["state"], "open")
        self.assertIn("Engine on the card is not ready any more", self.app.steps["open"]["error"])
        self.assertFalse(self.app.snapshot()["can"]["open"])

    def test_a_part_gone_turns_parts_red(self):
        self.app.studio = ReadyStudio(ready={"separator", "training", "listen"})
        with self.assertRaisesRegex(ValueError, r"not ready: Audio to MIDI"):
            self.app.open_click()
        self.assertEqual(self.app.steps["parts"]["state"], "failed")

    def test_an_incomplete_listening_pack_does_not_keep_the_studio_closed(self):
        self.app.studio = ReadyStudio(ready={"separator", "training", "midi"})
        self.assertEqual(self.app.open_click(), "http://127.0.0.1:8791/")
        self.assertEqual(self.app.steps["open"]["facts"]["Not complete (optional)"], "Listening pack (MOSS-Music, tempo)")

    def test_a_studio_gone_turns_start_red(self):
        studio = ReadyStudio()

        def gone():
            raise StudioError("YuE2 Studio's MCP at http://127.0.0.1:8791/mcp does not answer: refused")
        studio.hello = gone
        self.app.studio = studio
        with self.assertRaises(ValueError):
            self.app.open_click()
        self.assertEqual(self.app.steps["start"]["state"], "failed")
        self.assertEqual(self.app.steps["models"]["state"], "open")


# ------------------------------------------------------------ a LoRA dataset folder, sent with its styles

STYLE = ("A contemporary R&B song with a chill, late-night mood: a smooth, intimate lead vocal sings regretful, heartfelt "
         "lyrics about losing someone over a relaxed groove, with soft, atmospheric, spacious production and a laid-back, "
         "moody feel from start to finish, 84 BPM.")


def write_wav(path, seconds, rate=48000, float32=True):
    """A silent WAV of a length, float (format 3, as FL Studio renders) or 16-bit PCM."""
    channels, bits = 2, 32 if float32 else 16
    data = bytes(int(seconds * rate) * channels * bits // 8)
    fmt = struct.pack("<HHIIHH", 3 if float32 else 1, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE" +
                           b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data)


class DatasetFolderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "dataset_date231126time0815"
        self.folder.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def song(self, stem, text, seconds=20.0):
        write_wav(self.folder / f"{stem}.wav", seconds)
        if text is not None:
            (self.folder / f"{stem}.txt").write_text(text, encoding="utf-8")

    def test_the_txt_as_you_wrote_it(self):
        lyrics, style = beat_tool.split_style("I can never find the time\r\nThe silence talking\r\n\r\nstyle:\r\n\r\n"
                                              "contemporary rnb late night chill\r\n")
        self.assertEqual((lyrics, style), ("I can never find the time\nThe silence talking", "contemporary rnb late night chill"))
        self.assertEqual(beat_tool.split_style("[Chorus]\nla la\nStyle: slow soul, 70 BPM"), ("[Chorus]\nla la", "slow soul, 70 BPM"))
        self.assertEqual(beat_tool.split_style("[Verse 1]\nno style here"), ("[Verse 1]\nno style here", ""))

    def test_section_tags_as_the_studio_keeps_them(self):
        self.assertEqual(beat_tool.section_tags("[Verse 1]\nwords\n[Chorus: hook]\n[00:12.30]\n [Chorus] \nmore"),
                         ["[Verse 1]", "[Chorus]"])

    def test_wav_length_from_its_header(self):
        write_wav(self.folder / "f.wav", 12.5)
        write_wav(self.folder / "p.wav", 3.0, rate=44100, float32=False)
        self.assertAlmostEqual(beat_tool.wav_seconds(self.folder / "f.wav"), 12.5, places=3)
        self.assertAlmostEqual(beat_tool.wav_seconds(self.folder / "p.wav"), 3.0, places=3)
        (self.folder / "x.mp3").write_bytes(b"ID3\x04")
        self.assertIsNone(beat_tool.wav_seconds(self.folder / "x.mp3"))

    def test_a_folder_ready_to_send(self):
        for stem in "12345":
            self.song(stem, f"[Verse 1]\nline of {stem}\n\nstyle:\n\n{STYLE}\n")
        read = beat_tool.read_dataset_folder(self.folder)
        self.assertEqual((read["problems"], read["warnings"]), (0, []))
        self.assertEqual(read["name"], "dataset_date231126time0815")
        self.assertEqual(read["songs"][0]["lyrics"], "[Verse 1]\nline of 1")
        self.assertEqual((read["songs"][0]["style"], read["songs"][0]["style_from"]), (STYLE, "its .txt"))

    def test_one_style_txt_for_every_song_and_a_songs_own_style_first(self):
        for stem in "1234":
            self.song(stem, f"[Chorus]\nline {stem}\n")
        self.song("5", "[Chorus]\nline 5\nstyle: its own style, slow and warm, 70 BPM\n")
        (self.folder / "style.txt").write_text(STYLE + "\n", encoding="utf-8")
        read = beat_tool.read_dataset_folder(self.folder)
        self.assertEqual(read["problems"], 0)
        self.assertEqual([song["style_from"] for song in read["songs"]], ["style.txt"] * 4 + ["its .txt"])
        self.assertEqual(read["songs"][0]["style"], STYLE)
        self.assertIn("the style has 8 words", read["songs"][4]["warnings"][0])

    def test_what_stands_in_the_way_is_named_under_its_song(self):
        self.song("1", "[Verse 1]\nok\nstyle: " + STYLE)
        self.song("2", "just words, no sections\nstyle: " + STYLE)
        self.song("3", None)
        self.song("4", "[Chorus]\nshort one\nstyle: " + STYLE, seconds=6.5)
        self.song("5", "[Chorus]\nno style\n")
        (self.folder / "6.txt").write_text("[Verse 1]\nan orphan", encoding="utf-8")
        read = beat_tool.read_dataset_folder(self.folder)
        problems = {song["file"]: " ".join(song["problems"]) for song in read["songs"]}
        self.assertEqual(problems["1.wav"], "")
        self.assertIn("no section tags in 2.txt", problems["2.wav"])
        self.assertIn("no 3.txt beside it", problems["3.wav"])
        self.assertIn("6.5 s long: the studio refuses a song under 10 s", problems["4.wav"])
        self.assertIn("no style", problems["5.wav"])
        self.assertIn("6.txt has no audio file of the same name", read["warnings"][0])
        self.assertEqual(read["problems"], 5)

    def test_the_newest_dataset_folder_on_the_desktop(self):
        desktop = Path(self.temp.name)
        self.song("1", "[Verse 1]\nx")
        (desktop / "dataset_empty").mkdir()
        (desktop / "notes").mkdir()
        self.assertEqual(beat_tool.newest_dataset_folder(desktop), str(self.folder))


class DatasetStudio:
    """YuE2 Studio's training datasets as Beat Tool's dataset card uses them: adding a folder reads it as an upload
    does (lyrics from each .txt, every style empty); files_at answers what lies at a path on the studio's computer."""

    def __init__(self, files_at=None, existing=None, add_fails=False):
        self.datasets, self.calls, self.add_fails, self.last = {}, [], add_fails, None
        self.files_at = files_at or (lambda path: {p.name: p.read_bytes() for p in Path(path).iterdir()})
        for name, items in (existing or {}).items():
            self.datasets["old-" + name] = {"id": "old-" + name, "name": name, "items": items}

    def call(self, name, arguments=None, timeout=None, text_ok=False):
        self.last, arguments = name, arguments or {}
        self.calls.append((name, dict(arguments)))
        if name == "training_status":
            return {"datasets": [{"id": d["id"], "name": d["name"]} for d in self.datasets.values()]}
        if name == "dataset_create":
            self.datasets["ds1"] = {"id": "ds1", "name": arguments["name"], "items": []}
            return {"id": "ds1", "name": arguments["name"], "items": []}
        if name == "dataset_add_folder":
            if self.add_fails:
                raise StudioError("dataset_add_folder: 1.wav: the song is too short")
            files = self.files_at(arguments["path"])
            for file in sorted(f for f in files if f.endswith(".wav")):
                stem = file[:-4]
                lyrics = files.get(stem + ".txt", b"").decode()
                self.datasets[arguments["dataset_id"]]["items"].append({
                    "id": "song-" + stem, "title": stem, "source": "file:" + file, "lyrics": lyrics.strip(), "style": "",
                    "lyrics_state": "done" if beat_tool.section_tags(lyrics) else "found", "style_state": "wanted"})
            return self.datasets[arguments["dataset_id"]]
        if name == "dataset_get":
            return self.datasets[arguments["dataset_id"]]
        if name == "dataset_song_update":
            item = next(i for i in self.datasets[arguments["dataset_id"]]["items"] if i["id"] == arguments["song_id"])
            item["style"], item["style_state"] = arguments["style"], "done" if arguments["style"].strip() else "wanted"
            return self.datasets[arguments["dataset_id"]]
        if name == "dataset_delete":
            del self.datasets[arguments["dataset_id"]]
            return {"deleted": True}
        raise AssertionError(f"the dataset card called {name}")


class DatasetSendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.folder = self.home / "Desktop" / "dataset_date231126time0815"
        self.folder.mkdir(parents=True)
        for stem, tag in zip("12345", ["[Verse 1]", "[Chorus]", "[Chorus]", "[Verse 2]", "[Chorus]"]):
            write_wav(self.folder / f"{stem}.wav", 12 + int(stem))
            (self.folder / f"{stem}.txt").write_text(f"{tag}\nwords of {stem}\n\nstyle:\n\n{STYLE}\n", encoding="utf-8")
        self.app = App(folder=self.home / "beattool")
        self.app.steps["start"]["state"] = "ok"

    def tearDown(self):
        self.temp.cleanup()

    def click(self, studio, what="send"):
        self.app.studio = studio
        self.app.dataset_click({"do": what, "folder": str(self.folder)}).join(10)
        self.assertIsNone(self.app.busy)
        return self.app.snapshot()["dataset"]

    def test_sent_with_every_style_and_the_studio_counts_all_ready(self):
        studio = DatasetStudio()
        card = self.click(studio)
        self.assertEqual(card["state"], "ok", card["error"])
        self.assertEqual(card["facts"]["Ready"], "5 of 5 songs (YuE2 Studio's own count)")
        self.assertEqual(card["facts"]["Styles written now"], "5")
        items = studio.datasets["ds1"]["items"]
        self.assertTrue(all(item["style"] == STYLE for item in items))
        self.assertTrue(all("style" not in item["lyrics"] for item in items))         # the style lines stayed home
        staged = self.app.folder / "datasets" / "dataset_date231126time0815"
        self.assertEqual(sorted(p.name for p in staged.iterdir()), sorted(p.name for p in self.folder.iterdir()))
        self.assertEqual((staged / "1.txt").read_text(encoding="utf-8"), "[Verse 1]\nwords of 1\n")
        self.assertIn("words of 1\n\nstyle:", (self.folder / "1.txt").read_text(encoding="utf-8"))   # yours untouched
        add = next(arguments for name, arguments in studio.calls if name == "dataset_add_folder")
        self.assertEqual(add["path"], str(staged))

    def test_a_dataset_of_that_name_already_in_the_studio_is_not_sent_twice(self):
        card = self.click(DatasetStudio(existing={"dataset_date231126time0815": []}))
        self.assertEqual(card["state"], "failed")
        self.assertIn("already has a dataset named dataset_date231126time0815", card["error"])
        self.assertIn("Set the styles in the studio", card["error"])

    def test_a_failed_add_takes_the_half_made_dataset_out_again(self):
        studio = DatasetStudio(add_fails=True)
        card = self.click(studio)
        self.assertEqual(card["state"], "failed")
        self.assertIn("the song is too short", card["error"])
        self.assertIn("taken out of the studio again", card["error"])
        self.assertEqual(studio.datasets, {})

    def test_the_styles_given_to_a_dataset_you_uploaded_yourself(self):
        uploaded = [{"id": f"u{stem}", "title": f"Song {stem}", "source": f"file:dataset_date231126time0815/{stem}.wav",
                     "lyrics": "[Chorus]\nx", "style": "", "lyrics_state": "done", "style_state": "wanted"} for stem in "12345"]
        uploaded.append({"id": "u9", "title": "extra", "source": "file:9.wav", "lyrics": "[Chorus]\ny", "style": "slow",
                         "lyrics_state": "done", "style_state": "done"})
        studio = DatasetStudio(existing={"dataset_date231126time0815": uploaded})
        card = self.click(studio, "styles")
        self.assertEqual(card["state"], "ok", card["error"])
        self.assertTrue(all(item["style"] == STYLE for item in uploaded[:5]))
        self.assertEqual(uploaded[5]["style"], "slow")
        self.assertEqual(card["facts"]["Ready"], "6 of 6 songs (YuE2 Studio's own count)")
        self.assertIn("extra (not in the folder: left as they are)", card["facts"]["Also in the dataset"])
        self.assertIn("F5", card["note"])
        self.assertNotIn("dataset_create", [name for name, _ in studio.calls])

    def test_on_a_rented_machine_the_copy_goes_there_first(self):
        sent = {}

        class Shell:
            def __str__(self):
                return "root@203.0.113.7:40000"

            def sh(self, script, timeout=120):
                self.script = script
                return ""

            def put(self, local, path, digest, timeout=3600):
                self.assert_digest = (hashlib.sha256(Path(local).read_bytes()).hexdigest() == digest)
                sent[path] = Path(local).read_bytes()
                return True
        self.app.mode, self.app.remote = "vast", Shell()
        there = "/root/beat_tool/datasets/dataset_date231126time0815"
        studio = DatasetStudio(files_at=lambda path: {k.rsplit("/", 1)[1]: v for k, v in sent.items() if k.startswith(path + "/")})
        card = self.click(studio)
        self.assertEqual(card["state"], "ok", card["error"])
        self.assertEqual(sorted(sent), sorted(f"{there}/{name}" for name in [f"{s}.{e}" for s in "12345" for e in ("txt", "wav")]))
        self.assertTrue(self.app.remote.assert_digest)
        self.assertEqual(self.app.remote.script, f"rm -rf {there}")
        self.assertEqual(next(a for n, a in studio.calls if n == "dataset_add_folder")["path"], there)

    def test_problems_in_the_folder_stop_it_before_anything_is_sent(self):
        (self.folder / "2.txt").write_text("no sections\nstyle: " + STYLE, encoding="utf-8")
        studio = DatasetStudio()
        card = self.click(studio)
        self.assertEqual(card["state"], "failed")
        self.assertIn("1 thing to fix in the folder first", card["error"])
        self.assertEqual(studio.calls, [])

    def test_check_needs_no_studio_and_send_waits_for_it(self):
        self.app.steps["start"]["state"] = "open"
        with self.assertRaisesRegex(beat_tool.Refused, "First get YuE2 Studio running"):
            self.app.dataset_click({"do": "send", "folder": str(self.folder)})
        card = self.click(DatasetStudio(), "check")
        self.assertEqual(card["state"], "ok", card["error"])
        self.assertEqual(len(card["data"]["songs"]), 5)
        self.assertFalse(card["can_send"])


if __name__ == "__main__":
    unittest.main()
