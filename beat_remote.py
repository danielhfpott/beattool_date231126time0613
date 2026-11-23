"""Beat Tool on a rented vast.ai machine: the vast.ai API, the key kept for this Windows account, and the SSH link.

The page and this backend stay on this PC. On vast.ai runs YuE2 Studio's own Linux build (the AppImage of the pinned
release), started under a virtual display, so every model computes on the rented card. Its MCP server answers on the
rented machine's loopback only; this PC reaches it through an SSH tunnel, so nothing of the studio faces the internet
and the studio lets the calls through as calls from its own computer (remote.rs: loopback peer, loopback Host, no
forwarding header). Files go up and come home over the same SSH, each one checked by its SHA-256 on both ends.

Everything this keeps stays in the folder's vast\\ subfolder: the API key (encrypted with Windows DPAPI, readable only by
this Windows account), the SSH key made for this tool, the rented machine's host key, and the instance rented.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VAST_API = "https://console.vast.ai/api/v0"
# Ubuntu 22.04 (glibc 2.35, what the studio's Linux build is built on) with NVIDIA's CUDA 13.0 runtime libraries:
# audio.cpp's Linux CUDA 13 server (SheetSage2, HT-Demucs, MuScriptor) imports libcudart.so.13, libcublas.so.13 and
# libcufft.so.12 and carries none of them (read from its ELF header, release server-2fd98400).
IMAGE = "nvidia/cuda:13.0.3-runtime-ubuntu22.04"
# The disk rented with the machine. 60 GB left 27 GB free once the studio and a model set were on it (6 Nov 2026), too
# little for the training files (8.6 GB), the listening pack (about 10 GB), the trainer's build and your datasets.
DISK_GB = 120
# What the GPU step wants free for Beat Tool's folder: free space plus what that folder already holds, so a check made
# after the downloads asks the same as one made on the fresh machine.
MIN_FREE_GB = 70
REMOTE_ROOT = "/root/beat_tool"
REMOTE_MCP_PORT = 8791
DISPLAY = ":99"
# The studio picks audio.cpp's CUDA 13 build on compute capability 7.5 and newer with driver 580 or newer
# (hardware.rs cuda_build); the image above carries CUDA 13's libraries only, so those are the machines offered.
MIN_DRIVER = 580
MIN_COMPUTE = 7.5
# The machines Find machines offers: one RTX 4090 or RTX 5090, more than 99.5 % reliable, with at least 500 Mbps both
# down and up as vast.ai measured them. On 23 Nov 2026 most such hosts sat on a 1 Gbps line; the slow ones measured
# 90-300 Mbps.
GPU_NAMES = ("RTX 4090", "RTX 5090")
MIN_RELIABILITY = 0.995
MIN_MBPS = 500
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class VastError(RuntimeError):
    pass


class RemoteError(RuntimeError):
    pass


# ------------------------------------------------------------ the key, for this Windows account only

class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("The key is kept with Windows DPAPI, which only Windows has.")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_uint32, ctypes.POINTER(_Blob)]
    function.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data, len(data))
    source, result = _Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p)), _Blob()
    if protect:
        ok = function(ctypes.byref(source), "Beat Tool vast.ai key", None, None, None, 1, ctypes.byref(result))
    else:
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise OSError(f"Windows DPAPI refused ({ctypes.GetLastError()}).")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


class KeyStore:
    """The vast.ai API key, encrypted for this Windows account in vast\\key.dpapi."""

    def __init__(self, folder):
        self.path = Path(folder) / "key.dpapi"

    def save(self, key: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(_dpapi(key.encode("utf-8"), protect=True))

    def load(self):
        try:
            return _dpapi(self.path.read_bytes(), protect=False).decode("utf-8")
        except FileNotFoundError:
            return None

    def forget(self):
        self.path.unlink(missing_ok=True)


# ------------------------------------------------------------ vast.ai

def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def driver_major(text) -> int:
    head = str(text or "").split(".")[0]
    return int(head) if head.isdigit() else 0


def compute_capability(value) -> float:
    """vast.ai names it as 860 for 8.6 in its answers; a plain 8.6 is read as it is."""
    number = _number(value)
    return round(number / 100, 2) if number > 20 else number


class Vast:
    """vast.ai's REST API with the user's key as a bearer token. The key is never put into an error."""

    def __init__(self, key, urlopen=urllib.request.urlopen, sleep=time.sleep):
        self.key, self.urlopen, self.sleep = key, urlopen, sleep

    def call(self, method, path, body=None, timeout=40):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json", "User-Agent": "beat-tool"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(5):
            request = urllib.request.Request(VAST_API + path, data=data, method=method, headers=headers)
            try:
                with self.urlopen(request, timeout=timeout) as response:
                    text = response.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace").strip()[:500]
                error.close()
                if error.code in (429, 502, 503, 504) and attempt < 4:
                    self.sleep(2 + 2 * attempt)
                    continue
                auth_bad = error.code in (401, 403)
                if not auth_bad and detail.startswith("{"):
                    try:
                        auth_bad = json.loads(detail).get("error") == "auth_error"
                    except ValueError:
                        pass
                if auth_bad:
                    raise VastError(f"vast.ai refused the API key (HTTP {error.code}): {detail}") from None
                raise VastError(f"vast.ai answered HTTP {error.code} to {method} {path}: {detail}") from None
            except (urllib.error.URLError, OSError) as error:
                raise VastError(f"vast.ai does not answer ({method} {path}): {getattr(error, 'reason', error)}") from None
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise VastError(f"vast.ai answered {method} {path} with something other than JSON: {text[:300]}") from None

    def user(self):
        answer = self.call("GET", "/users/current")
        if isinstance(answer, dict) and answer.get("success") is False:
            raise VastError(f"vast.ai refused the API key: {answer.get('msg') or json.dumps(answer)[:300]}")
        return answer

    def offers(self, each=12):
        """On-demand machines with one RTX 4090 or RTX 5090, more than 99.5 % reliable, fast both ways, that this setup
        runs on: the cheapest of each card (one search per card, so the dearer 5090s are not crowded out by 4090s),
        cheapest first."""
        shown, seen = [], 0
        for gpu in GPU_NAMES:
            query = {"verified": {"eq": True}, "external": {"eq": False}, "rentable": {"eq": True}, "rented": {"eq": False},
                     "num_gpus": {"eq": 1}, "gpu_name": {"eq": gpu}, "cuda_max_good": {"gte": 13.0},
                     "compute_cap": {"gte": int(MIN_COMPUTE * 100)}, "disk_space": {"gte": DISK_GB},
                     "inet_down": {"gte": MIN_MBPS}, "inet_up": {"gte": MIN_MBPS}, "reliability": {"gt": MIN_RELIABILITY},
                     "direct_port_count": {"gte": 1}, "type": "on-demand", "allocated_storage": DISK_GB,
                     "order": [["dph_total", "asc"]], "limit": 64}
            answer = self.call("POST", "/bundles/", query)
            offers = answer.get("offers") if isinstance(answer, dict) else None
            if not isinstance(offers, list):
                raise VastError(f"vast.ai's offer search answered without a list of offers: {json.dumps(answer)[:300]}")
            seen += len(offers)
            # what the image and the studio need, checked here too: the search's own filters are vast.ai's to apply
            shown += [offer_view(offer) for offer in offers if offer_fits(offer)][:each]
        shown.sort(key=lambda view: view["dph"])
        return shown, seen

    def attach_ssh(self, instance_id, public_key):
        """Attaches this tool's SSH key to the rented instance only (never to the user's vast.ai account), so vast.ai's
        SSH proxy and sshd let it in alongside onstart."""
        try:
            self.call("POST", f"/instances/{int(instance_id)}/ssh/", {"ssh_key": public_key.strip()})
        except VastError:
            pass

    def rent(self, offer_id, public_key, label="beat-tool"):
        """The offer rented with the image above. The SSH key made for this tool is added to the machine's root
        account by onstart and attached to the instance, so the user's own vast.ai keys are neither needed nor changed."""
        key_line = public_key.strip()
        onstart = ("mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                   f"grep -qxF {shlex.quote(key_line)} /root/.ssh/authorized_keys 2>/dev/null || "
                   f"echo {shlex.quote(key_line)} >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys")
        # vast.ai spells direct+proxy SSH as "ssh_direc ssh_proxy" (vastai/api/instances.py resolve_runtype).
        body = {"client_id": "me", "image": IMAGE, "disk": DISK_GB, "runtype": "ssh_direc ssh_proxy",
                "label": label, "onstart": onstart,
                # graphics: the NVIDIA container runtime then mounts the card's Vulkan driver, which the studio's
                # Linux engine computes with
                "env": {"NVIDIA_DRIVER_CAPABILITIES": "all"}}
        answer = self.call("PUT", f"/asks/{int(offer_id)}/", body)
        contract = answer.get("new_contract") if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or not answer.get("success") or not contract:
            raise VastError(f"vast.ai did not rent offer {offer_id}: {json.dumps(answer)[:400]}")
        contract = int(contract)
        self.attach_ssh(contract, key_line)
        return contract

    def instance(self, instance_id):
        """The instance's record, or None when vast.ai no longer has it."""
        try:
            answer = self.call("GET", f"/instances/{int(instance_id)}/?owner=me")
        except VastError as error:
            if "answered HTTP 404" in str(error) or "no_such_instance" in str(error):
                return None
            raise
        record = answer.get("instances") if isinstance(answer, dict) else None
        if isinstance(record, list):
            record = record[0] if record else None
        if isinstance(record, dict) and record.get("actual_status") == "destroyed":
            return None
        return record or None

    def destroy(self, instance_id):
        try:
            answer = self.call("DELETE", f"/instances/{int(instance_id)}/", {})
        except VastError as error:
            if "answered HTTP 404" in str(error) or "no_such_instance" in str(error):
                return {"success": True, "already_gone": True}
            raise
        if isinstance(answer, dict) and answer.get("success") is False:
            if answer.get("error") == "no_such_instance":
                return {"success": True, "already_gone": True}
            raise VastError(f"vast.ai did not destroy instance {instance_id}: {json.dumps(answer)[:300]}")
        return answer


def offer_fits(offer):
    """Whether an offer is one Find machines shows: the card, its reliability and its speeds, and what the image and
    the studio need of its driver and card."""
    return (offer.get("gpu_name") in GPU_NAMES and _number(offer.get("num_gpus"), 1) == 1
            and _number(offer.get("reliability")) > MIN_RELIABILITY
            and _number(offer.get("inet_down")) >= MIN_MBPS and _number(offer.get("inet_up")) >= MIN_MBPS
            and driver_major(offer.get("driver_version")) >= MIN_DRIVER
            and compute_capability(offer.get("compute_cap")) >= MIN_COMPUTE)


def offer_view(offer):
    return {"id": offer.get("id"), "gpu": offer.get("gpu_name") or "?", "vram_gb": round(_number(offer.get("gpu_ram")) / 1024, 1),
            "dph": round(_number(offer.get("dph_total")), 4), "where": offer.get("geolocation") or "",
            "reliability": round(_number(offer.get("reliability")) * 100, 3), "down_mbps": int(_number(offer.get("inet_down"))),
            "up_mbps": int(_number(offer.get("inet_up"))),
            "driver": str(offer.get("driver_version") or ""), "driver_major": driver_major(offer.get("driver_version")),
            "cuda": offer.get("cuda_max_good"), "compute": compute_capability(offer.get("compute_cap")),
            "disk_gb": int(_number(offer.get("disk_space"))), "cpu": offer.get("cpu_name") or "",
            "cores": offer.get("cpu_cores_effective") or offer.get("cpu_cores"), "ram_gb": round(_number(offer.get("cpu_ram")) / 1024),
            "machine": offer.get("machine_id")}


def ssh_endpoints(record):
    """Where the instance's SSH answers: the machine's own address and port first (runtype ssh_direc ssh_proxy), vast.ai's
    SSH proxy second."""
    found = []
    ports = (record or {}).get("ports") or {}
    direct = ports.get("22/tcp") if isinstance(ports, dict) else None
    if record.get("public_ipaddr") and isinstance(direct, list) and direct and direct[0].get("HostPort"):
        found.append((str(record["public_ipaddr"]).strip(), int(direct[0]["HostPort"])))
    if record.get("ssh_host") and record.get("ssh_port"):
        found.append((str(record["ssh_host"]).strip(), int(record["ssh_port"])))
    return found


# ------------------------------------------------------------ SSH

def make_ssh_key(folder, run=subprocess.run):
    """This tool's own SSH key (ed25519, no passphrase), made once with Windows' OpenSSH."""
    folder = Path(folder)
    key = folder / "beat_tool_ed25519"
    if not key.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        try:
            done = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "beat-tool", "-f", str(key)],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        except FileNotFoundError:
            raise RemoteError("ssh-keygen is not on this PC: Windows' OpenSSH client is not installed "
                              "(Settings - Apps - Optional features - OpenSSH Client).") from None
        if done.returncode != 0 or not key.is_file():
            raise RemoteError(f"ssh-keygen ended with code {done.returncode}: {(done.stderr or done.stdout).strip()}")
    public = key.with_name(key.name + ".pub")
    if not public.is_file():
        raise RemoteError(f"{public} is missing beside the SSH key.")
    return key, public.read_text(encoding="ascii").strip()


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Remote:
    """One rented machine, as root over SSH with this tool's key. Every command is a bash script sent on stdin;
    paths are quoted for bash, so names with spaces travel as they are."""

    def __init__(self, host, port, key_file, known_hosts, run=subprocess.run, popen=subprocess.Popen):
        self.host, self.port = host, int(port)
        self.key_file, self.known_hosts = Path(key_file), Path(known_hosts)
        self.tunnel_log = self.known_hosts.with_name("tunnel.log")
        self.run, self.popen = run, popen
        self.tunnel_process, self.tunnel_port = None, None

    def __str__(self):
        return f"root@{self.host}:{self.port}"

    def options(self):
        return ["-i", str(self.key_file), "-p", str(self.port), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts.as_posix()}",
                "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "-o", "LogLevel=ERROR"]

    def command(self, *remote):
        return ["ssh", *self.options(), f"root@{self.host}", *remote]

    def sh(self, script, timeout=120, stdin=None):
        """Runs a bash script there; answers what it printed. A non-zero end is an error with its last words."""
        try:
            done = self.run(self.command("bash -s"), input=script.encode("utf-8"), capture_output=True,
                            timeout=timeout, creationflags=NO_WINDOW)
        except FileNotFoundError:
            raise RemoteError("ssh is not on this PC: Windows' OpenSSH client is not installed.") from None
        except subprocess.TimeoutExpired:
            raise RemoteError(f"The rented machine ({self}) did not finish within {timeout} s.") from None
        out = done.stdout.decode("utf-8", "replace")
        if done.returncode != 0:
            err = done.stderr.decode("utf-8", "replace").strip()
            tail = "\n".join((err or out).strip().splitlines()[-12:])
            raise RemoteError(f"On the rented machine ({self}) the command ended with code {done.returncode}:\n{tail}")
        return out

    def stream(self, script, on_line, timeout=3600):
        """Runs a long bash script there, handing each line it prints to on_line as it comes."""
        process = self.popen(self.command("bash -s"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        process.stdin.write(script.encode("utf-8"))
        process.stdin.close()
        lines, late = [], threading.Event()

        def overdue():          # a machine that hangs without printing ends too
            late.set()
            process.kill()
        watchdog = threading.Timer(timeout, overdue)
        watchdog.daemon = True
        watchdog.start()
        try:
            for raw in iter(process.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                lines.append(line)
                on_line(line)
            code = process.wait()
        finally:
            watchdog.cancel()
        if late.is_set():
            raise RemoteError(f"The rented machine ({self}) did not finish within {timeout} s:\n" + "\n".join(lines[-8:]))
        if code != 0:
            raise RemoteError(f"On the rented machine ({self}) the setup ended with code {code}:\n" + "\n".join(lines[-15:]))
        return lines

    def reachable(self):
        try:
            return self.sh("echo beat-tool-ok", timeout=40).strip().endswith("beat-tool-ok")
        except RemoteError:
            return False

    # ---- files

    def sha256(self, path):
        out = self.sh(f"if [ -f {shlex.quote(path)} ]; then sha256sum -- {shlex.quote(path)}; fi", timeout=600).split()
        return out[0] if out else None

    def is_file(self, path):
        return self.sh(f"[ -f {shlex.quote(path)} ] && echo yes || echo no", timeout=60).strip() == "yes"

    def read_text(self, path):
        return self.sh(f"cat -- {shlex.quote(path)}", timeout=120)

    def put(self, local, path, digest, timeout=3600):
        """local sent to path there, unless a file with this SHA-256 is there already; checked there after."""
        if self.sha256(path) == digest:
            return False
        folder = path.rsplit("/", 1)[0]
        part = path + ".part"
        script = f"mkdir -p {shlex.quote(folder)} && cat > {shlex.quote(part)}"
        with open(local, "rb") as handle:
            try:
                done = self.run(self.command(script), stdin=handle, capture_output=True, timeout=timeout,
                                creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                raise RemoteError(f"Sending {Path(local).name} to the rented machine did not finish within {timeout} s.") from None
        if done.returncode != 0:
            raise RemoteError(f"Sending {Path(local).name} to the rented machine ended with code {done.returncode}: "
                              f"{done.stderr.decode('utf-8', 'replace').strip()[-400:]}")
        there = self.sha256(part)
        if there != digest:
            raise RemoteError(f"{Path(local).name} arrived on the rented machine with SHA-256 {there}, not {digest}.")
        self.sh(f"mv -f -- {shlex.quote(part)} {shlex.quote(path)}", timeout=60)
        return True

    def get(self, path, target, timeout=3600):
        """path there copied to target here; answers its SHA-256, the same on both ends."""
        digest = self.sha256(path)
        if not digest:
            raise RemoteError(f"{path} is not on the rented machine.")
        target = Path(target)
        part = target.with_name(target.name + ".part")
        with open(part, "wb") as out:
            try:
                done = self.run(self.command(f"cat -- {shlex.quote(path)}"), stdout=out, stderr=subprocess.PIPE,
                                timeout=timeout, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                raise RemoteError(f"Bringing {path} home did not finish within {timeout} s.") from None
        if done.returncode != 0:
            part.unlink(missing_ok=True)
            raise RemoteError(f"Bringing {path} home ended with code {done.returncode}: "
                              f"{done.stderr.decode('utf-8', 'replace').strip()[-400:]}")
        here = file_sha256(part)
        if here != digest:
            part.unlink(missing_ok=True)
            raise RemoteError(f"{path} came home with SHA-256 {here}; on the rented machine it is {digest}.")
        os.replace(part, target)
        return digest

    # ---- the tunnel to the studio's MCP

    def tunnel(self):
        """127.0.0.1:<free port> here forwarded to the studio's MCP on the rented machine's loopback; answers the
        local port. A tunnel that died is opened again."""
        if self.tunnel_process and self.tunnel_process.poll() is None:
            return self.tunnel_port
        port = free_port()
        with open(self.tunnel_log, "ab") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} tunnel 127.0.0.1:{port} -> {self}:{REMOTE_MCP_PORT}\n".encode())
            log.flush()
            process = self.popen(["ssh", *self.options(), "-N", "-o", "ExitOnForwardFailure=yes",
                                  "-L", f"127.0.0.1:{port}:127.0.0.1:{REMOTE_MCP_PORT}", f"root@{self.host}"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                 creationflags=NO_WINDOW)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                said = self.tunnel_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
                raise RemoteError(f"The SSH tunnel to the rented machine closed at once (code {process.returncode}): "
                                  + " / ".join(said))
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            process.kill()
            raise RemoteError("The SSH tunnel to the rented machine did not open within 25 s.")
        self.tunnel_process, self.tunnel_port = process, port
        return port

    def close(self):
        if self.tunnel_process and self.tunnel_process.poll() is None:
            self.tunnel_process.kill()
        self.tunnel_process = None


# ------------------------------------------------------------ what runs on the rented machine

GPU_SCRIPT = r"""
set -u
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader,nounits || exit 7
echo "@@cuda $(ldconfig -p | grep -oE 'libcudart\.so\.13|libcublas\.so\.13|libcufft\.so\.12' | sort -u | tr '\n' ' ')"
echo "@@disk $(df -BG --output=avail /root | tail -n1 | tr -d ' G')"
echo "@@ours $(du -s -BG """ + REMOTE_ROOT + r""" 2>/dev/null | cut -f1 | tr -d G)"
echo "@@os $(. /etc/os-release && echo "$PRETTY_NAME")"
"""

# CUDA tested by using it, not by finding its files: the NVIDIA driver's CUDA library (libcuda; nvcuda.dll on Windows)
# starts CUDA, names the card, takes 256 MB of its memory, has the card fill it and reads it back; then each runtime
# library named on the command line is loaded (libcudart answers its version and how many cards it sees). The same
# program runs on this PC and on a rented machine. It prints one @@json line: what it found, or "error" and how far it got.
CUDA_PROBE = r'''
import ctypes
import json
import sys

found = {}


def answer(error=None):
    if error:
        found["error"] = error
    print("@@json " + json.dumps(found), flush=True)
    sys.exit(0)


def load(names):
    said = ""
    for name in names:
        try:
            return ctypes.CDLL(name), name
        except OSError as error:
            said = str(error)
    return None, said


cuda, said = load(["nvcuda.dll"] if sys.platform == "win32" else ["libcuda.so.1", "libcuda.so"])
if cuda is None:
    answer("The NVIDIA driver's CUDA library cannot be loaded: " + said)
P, I, U, S = ctypes.POINTER, ctypes.c_int, ctypes.c_uint64, ctypes.c_size_t
for name, args in {"cuInit": [ctypes.c_uint], "cuDriverGetVersion": [P(I)], "cuDeviceGetCount": [P(I)],
                   "cuDeviceGet": [P(I), I], "cuDeviceGetName": [P(ctypes.c_char), I, I],
                   "cuDeviceGetAttribute": [P(I), I, I], "cuDeviceTotalMem_v2": [P(S), I],
                   "cuDevicePrimaryCtxRetain": [P(ctypes.c_void_p), I], "cuCtxSetCurrent": [ctypes.c_void_p],
                   "cuMemAlloc_v2": [P(U), S], "cuMemsetD8_v2": [U, ctypes.c_ubyte, S], "cuCtxSynchronize": [],
                   "cuMemcpyDtoH_v2": [ctypes.c_void_p, U, S], "cuGetErrorString": [I, P(ctypes.c_char_p)]}.items():
    function = getattr(cuda, name)
    function.argtypes, function.restype = args, I


def check(code, what):
    if code:
        text = ctypes.c_char_p()
        cuda.cuGetErrorString(code, ctypes.byref(text))
        answer(f"{what} failed: CUDA error {code} ({(text.value or b'no description').decode(errors='replace')})")


check(cuda.cuInit(0), "cuInit (the driver starting CUDA)")
version = I()
check(cuda.cuDriverGetVersion(ctypes.byref(version)), "cuDriverGetVersion")
found["driver_cuda"] = f"{version.value // 1000}.{version.value % 1000 // 10}"
count = I()
check(cuda.cuDeviceGetCount(ctypes.byref(count)), "cuDeviceGetCount")
found["cards"] = count.value
if not count.value:
    answer("CUDA starts, but sees no card.")
device = I()
check(cuda.cuDeviceGet(ctypes.byref(device), 0), "cuDeviceGet")
name = ctypes.create_string_buffer(256)
check(cuda.cuDeviceGetName(name, 256, device), "cuDeviceGetName")
found["name"] = name.value.decode(errors="replace")
major, minor, total = I(), I(), S()
check(cuda.cuDeviceGetAttribute(ctypes.byref(major), 75, device), "cuDeviceGetAttribute")
check(cuda.cuDeviceGetAttribute(ctypes.byref(minor), 76, device), "cuDeviceGetAttribute")
found["compute"] = f"{major.value}.{minor.value}"
check(cuda.cuDeviceTotalMem_v2(ctypes.byref(total), device), "cuDeviceTotalMem")
found["vram_gb"] = round(total.value / 1024 ** 3, 1)
context = ctypes.c_void_p()
check(cuda.cuDevicePrimaryCtxRetain(ctypes.byref(context), device), "cuDevicePrimaryCtxRetain (a CUDA context on the card)")
check(cuda.cuCtxSetCurrent(context), "cuCtxSetCurrent")
size, memory = 256 << 20, U()
check(cuda.cuMemAlloc_v2(ctypes.byref(memory), size), "cuMemAlloc (256 MB of the card's memory)")
check(cuda.cuMemsetD8_v2(memory, 0x5A, size), "cuMemsetD8 (the card filling it)")
check(cuda.cuCtxSynchronize(), "cuCtxSynchronize (waiting for the card)")
back = (ctypes.c_ubyte * 4096)()
check(cuda.cuMemcpyDtoH_v2(ctypes.addressof(back), memory.value + size - 4096, 4096), "cuMemcpyDtoH (reading it back)")
if any(byte != 0x5A for byte in back):
    answer("The card's memory read back other bytes than the card filled it with.")
found["memory"] = "256 MB taken on the card, filled there and read back"
found["runtime"] = {}
for library in sys.argv[1:]:
    loaded, said = load([library])
    if loaded is None:
        answer(f"{library} cannot be loaded: {said}")
    if library.startswith("libcudart"):
        runtime, cards = I(), I()
        loaded.cudaRuntimeGetVersion.argtypes = loaded.cudaGetDeviceCount.argtypes = [P(I)]
        loaded.cudaRuntimeGetVersion.restype = loaded.cudaGetDeviceCount.restype = I
        if loaded.cudaRuntimeGetVersion(ctypes.byref(runtime)) or loaded.cudaGetDeviceCount(ctypes.byref(cards)):
            answer(f"{library} loads, but does not answer its version or how many cards it sees.")
        found["runtime"][library] = f"CUDA {runtime.value // 1000}.{runtime.value % 1000 // 10}, sees {cards.value} card(s)"
    else:
        found["runtime"][library] = "loads"
answer()
'''

# What the studio's libraries load on the rented machine: audio.cpp's CUDA 13 server (SheetSage2, HT-Demucs, MuScriptor).
RUNTIME_LIBRARIES = ("libcudart.so.13", "libcublas.so.13", "libcufft.so.12")
VULKAN_ICD = '{"file_format_version":"1.0.0","ICD":{"library_path":"libGLX_nvidia.so.0","api_version":"1.3"}}'


def compute_script():
    """The rented machine's CUDA and Vulkan tested before anything of the studio is downloaded: CUDA_PROBE with the
    runtime libraries, and vulkaninfo, since the studio's song engine computes through Vulkan there. Installs what the
    test needs (python3, vulkan-tools, the Vulkan loader) and, where the NVIDIA runtime mounted the card's Vulkan
    driver without the file that names it, writes that file. Prints @@ lines, one @@json line from the probe and the
    vulkaninfo summary between @@vulkan-begin and @@vulkan-end."""
    q = shlex.quote
    return f"""
set -u
export DEBIAN_FRONTEND=noninteractive
# grep -c reads all of ldconfig's answer (see studio_script)
have() {{ [ "$(ldconfig -p | grep -c -- "$1")" -gt 0 ]; }}
need=""
command -v python3 >/dev/null 2>&1 || need="$need python3"
command -v vulkaninfo >/dev/null 2>&1 || need="$need vulkan-tools"
have 'libvulkan.so.1' || need="$need libvulkan1"
if [ -n "$need" ]; then
  echo "@@ Installing what the test needs:$need…"
  if ! {{ apt-get update -q && apt-get install -y -q --no-install-recommends $need; }} </dev/null > /tmp/beat_tool_test_packages.log 2>&1; then
    tail -n 8 /tmp/beat_tool_test_packages.log | sed 's/^/@@log /'
    echo "@@error The packages for the test ($need) did not install on the rented machine."
    exit 0
  fi
fi
if ! ls /usr/share/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.json >/dev/null 2>&1 && have 'libGLX_nvidia.so.0'; then
  mkdir -p /usr/share/vulkan/icd.d
  printf '%s\\n' {q(VULKAN_ICD)} > /usr/share/vulkan/icd.d/nvidia_icd.json
  echo "@@ Wrote /usr/share/vulkan/icd.d/nvidia_icd.json for the card's Vulkan driver"
fi
icd=$(ls /usr/share/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.json 2>/dev/null | head -n1)
echo "@@icd ${{icd:-none}}"
echo "@@glx $(ldconfig -p | grep -c 'libGLX_nvidia.so.0')"
echo "@@ Testing CUDA on the card…"
python3 - {" ".join(q(name) for name in RUNTIME_LIBRARIES)} <<'BEAT_TOOL_CUDA_PROBE'
{CUDA_PROBE}
BEAT_TOOL_CUDA_PROBE
echo "@@ Asking Vulkan which devices it finds…"
echo "@@vulkan-begin"
timeout 60 vulkaninfo --summary </dev/null 2>&1 | head -n 200
echo "@@vulkan-end ${{PIPESTATUS[0]}}"
"""


def vulkan_devices(lines):
    """The devices vulkaninfo --summary lists, each with the fields it prints for it (deviceName, deviceType,
    vendorID, driverName…)."""
    devices, current = [], None
    for line in lines:
        text = line.strip()
        if re.fullmatch(r"GPU\d+:", text):
            current = {}
            devices.append(current)
        elif current is not None and "=" in text:
            key, _, value = text.partition("=")
            current[key.strip()] = value.strip()
    return devices


def studio_script(url, name, size, sha256, folder, marker):
    """Installs what the studio's window needs, downloads the pinned AppImage on the rented machine, checks its size
    and SHA-256 there, and unpacks it as a portable copy (portable.flag beside the program: its models, library and
    logs stay in that folder). Prints @@ lines for the backend and one @@json line at the end."""
    q = shlex.quote
    return f"""
set -euo pipefail
root={q(REMOTE_ROOT)}
mkdir -p "$root" && cd "$root"
# grep -c reads all of ldconfig's answer: grep -q would end the pipe early and, under pipefail, make a library that is
# there look missing.
have() {{ [ "$(ldconfig -p | grep -c -- "$1")" -gt 0 ]; }}
if ! command -v Xvfb >/dev/null 2>&1 || ! have 'libwebkit2gtk-4.1.so.0' || ! have 'libvulkan.so.1'; then
  echo "@@ Installing the packages the studio's window needs: Xvfb, GTK, WebKitGTK, the Vulkan loader…"
  export DEBIAN_FRONTEND=noninteractive
  # </dev/null: this script arrives on bash's standard input, which no command below may read
  apt-get update -q </dev/null
  apt-get install -y -q --no-install-recommends xvfb xauth libvulkan1 libwebkit2gtk-4.1-0 libgtk-3-0 ca-certificates curl \
    fonts-dejavu-core procps </dev/null
fi
# The NVIDIA runtime mounts the card's Vulkan driver (libGLX_nvidia) with the graphics capability; some hosts leave out
# the file that names it to the Vulkan loader, which is then written here.
if ! ls /usr/share/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.json >/dev/null 2>&1 && have 'libGLX_nvidia.so.0'; then
  mkdir -p /usr/share/vulkan/icd.d
  printf '{{"file_format_version":"1.0.0","ICD":{{"library_path":"libGLX_nvidia.so.0","api_version":"1.3"}}}}\\n' > /usr/share/vulkan/icd.d/nvidia_icd.json
  echo "@@ Wrote /usr/share/vulkan/icd.d/nvidia_icd.json for the card's Vulkan driver"
fi
if [ -e {q(folder)} ]; then echo "@@error {q(folder)} already exists without a finished unpack"; exit 3; fi
echo "@@ Downloading {name} on the rented machine…"
curl -fL --retry 3 --silent --show-error -o {q(name)}.part {q(url)} </dev/null
got=$(stat -c %s {q(name)}.part)
sum=$(sha256sum {q(name)}.part | cut -d' ' -f1)
if [ "$got" != {q(str(size))} ] || [ "$sum" != {q(sha256)} ]; then
  rm -f {q(name)}.part
  echo "@@error The download does not match the pin: $got bytes, SHA-256 $sum; expected {size} bytes, SHA-256 {sha256}. It was deleted; nothing was unpacked."
  exit 4
fi
mv -f {q(name)}.part {q(name)}
chmod +x {q(name)}
echo "@@ Unpacking {name}…"
rm -rf squashfs-root
./{q(name)} --appimage-extract >/dev/null </dev/null
mv squashfs-root {q(folder)}
touch {q(folder)}/usr/bin/portable.flag
exe_sum=$(sha256sum {q(folder)}/usr/bin/YuE2-Studio | cut -d' ' -f1)
files=$(find {q(folder)} -type f | wc -l)
rm -f {q(name)}
printf '{{"asset":"%s","url":"%s","sha256":"%s","exe":"usr/bin/YuE2-Studio","exe_sha256":"%s","files":%s,"unpacked":"%s"}}\\n' \
  {q(name)} {q(url)} {q(sha256)} "$exe_sum" "$files" "$(date '+%Y-%m-%d %H:%M:%S')" > {q(folder)}/{q(marker)}
echo "@@json $(cat {q(folder)}/{q(marker)})"
"""


def studio_found_script(prefix, marker):
    return f"""
set -u
for d in $(ls -d {shlex.quote(REMOTE_ROOT)}/{shlex.quote(prefix)}* 2>/dev/null | sort -r); do
  if [ -f "$d/{marker}" ]; then
    echo "@@folder $d"
    echo "@@record $(cat "$d/{marker}")"
    if [ -f "$d/usr/bin/YuE2-Studio" ]; then echo "@@exe $(sha256sum "$d/usr/bin/YuE2-Studio" | cut -d' ' -f1)"; fi
    exit 0
  fi
done
echo "@@none"
"""


# Every YuE2 Studio process there, found by the program file it runs: AppRun starts it as plain "YuE2-Studio" (no path
# in its command line, seen on a rented machine on 8 Nov 2026), so matching the command line finds nothing.
STUDIO_PIDS = r"""
studio_pids() { for p in /proc/[0-9]*; do case "$(readlink "$p/exe" 2>/dev/null)" in */usr/bin/YuE2-Studio) echo "${p#/proc/}";; esac; done; }
"""

RUNNING_SCRIPT = STUDIO_PIDS + r"""
for pid in $(studio_pids); do
  exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
  echo "$pid|$(ps -o lstart= -p "$pid" | awk '{print $4}')|$exe"
done
true
"""


def launch_script(folder, log, trainer=None):
    """Starts the studio there in its own session under a virtual display (Xvfb), its output appended to its log;
    prints its PID and where this start's output begins in the log. With the LoRA trainer built there (trainer: its
    path), the studio is started with YUE_TRAIN_BIN naming it, so its LoRA page can train (see trainer_build_script)."""
    q = shlex.quote
    with_trainer = (f'if [ -x {q(trainer)} ]; then export YUE_TRAIN_BIN={q(trainer)}; echo "@@trainer {trainer}"; fi\n'
                    if trainer else "")
    return f"""
set -eu
cd {q(folder)}
if ! pgrep -x Xvfb >/dev/null 2>&1; then
  setsid Xvfb {DISPLAY} -screen 0 1600x1000x24 -nolisten tcp > {q(REMOTE_ROOT)}/xvfb.log 2>&1 < /dev/null &
  sleep 1
fi
{with_trainer}printf '\\n=== %s Beat Tool starts %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$PWD/AppRun" >> {q(log)}
offset=$(stat -c %s {q(log)})
DISPLAY={DISPLAY} NO_AT_BRIDGE=1 WEBKIT_DISABLE_COMPOSITING_MODE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 LIBGL_ALWAYS_SOFTWARE=1 \
  setsid ./AppRun >> {q(log)} 2>&1 < /dev/null &
echo "@@pid $! $offset"
"""


# ------------------------------------------------------------ YuE2 Studio's LoRA trainer, built for Linux there
#
# YuE2 Studio publishes its LoRA trainer (HOT-Step's ace-train, shipped as music-train) as a Windows build only, and its
# Linux build refuses that download ("is a Windows build and is not available on this platform yet": downloads.rs,
# refuse_foreign_runtime). The studio runs whatever trainer YUE_TRAIN_BIN names, counts the trainer installed when that
# file is there, and then downloads only the trainer's weights from Hugging Face (training.rs: trainer, installed,
# install_pack; read at v3.8.4 and v3.8.5). HOT-Step builds ace-train for Linux itself (release.yml: ubuntu-22.04, CUDA
# 13.1, ace-train in every Linux archive). So it is built on the rented machine from the commit the studio pins
# (engines/music-train-source.json), with the flags of the studio's own Windows build of it
# (scripts/build-hotstep-tools.ps1) and the Linux steps of its Linux build of the same repository
# (scripts/build-midi-runtime-linux.sh), for the rented card's CUDA architecture.
CMAKE_URL = "https://github.com/Kitware/CMake/releases/download/v3.31.6/cmake-3.31.6-linux-x86_64.tar.gz"
CMAKE_SHA256 = "5a1133ff103c71eb5120e2cc3de922733e7d8a26a98ae716397e8676adb367bf"
TRAINER_EXE = "ace-train"
TRAINER_RECORD = "beat_tool_trainer.json"


def trainer_folder(commit):
    """Where the trainer built from this commit lives on the rented machine: one folder, the binary and its libraries."""
    return f"{REMOTE_ROOT}/trainer-{commit[:8]}"


def trainer_build_script(repository, commit, architectures, folder):
    """Builds ace-train there: the compiler and CUDA's development files for the image's CUDA, CMake (pinned, checked),
    HOT-Step-CPP at commit with its ggml fork and the VST3 SDK parts it compiles, then ace-train with ggml's CUDA backend
    for architectures and ggml's processor backends. Copies the binary and its libraries (found through $ORIGIN) into
    folder, checks that every library resolves, records it, and packs folder as folder.tar.gz to bring home. Prints @@
    lines while it works, @@log lines from a failed build, and one @@json line at the end."""
    q = shlex.quote
    tarball = folder + ".tar.gz"
    return f"""
set -euo pipefail
root={q(REMOTE_ROOT)}
mkdir -p "$root" && cd "$root"
export DEBIAN_FRONTEND=noninteractive
cuda=$(ls -d /usr/local/cuda-[0-9]* 2>/dev/null | sort -V | tail -n1 || true)
if [ -z "$cuda" ]; then echo "@@error The image has no /usr/local/cuda-<version>: no CUDA to build the trainer against."; exit 3; fi
ver=${{cuda##*cuda-}}; pkg=${{ver/./-}}
echo "@@ Installing the build tools: the compiler, Ninja, git and CUDA $ver's compiler and libraries…"
if ! {{ apt-get update -q && apt-get install -y -q --no-install-recommends build-essential git ninja-build \\
    ca-certificates curl cuda-minimal-build-$pkg libcublas-dev-$pkg cuda-driver-dev-$pkg; }} \\
    </dev/null > "$root/trainer-packages.log" 2>&1; then
  tail -n 15 "$root/trainer-packages.log" | sed 's/^/@@log /'
  echo "@@error The build tools did not install; the log is $root/trainer-packages.log on the rented machine."
  exit 9
fi
export PATH="$cuda/bin:$PATH"
cmake="$root/cmake-3.31.6/bin/cmake"
if [ ! -x "$cmake" ]; then
  echo "@@ Downloading CMake 3.31.6…"
  curl -fL --retry 3 --silent --show-error -o cmake.tar.gz.part {q(CMAKE_URL)} </dev/null
  sum=$(sha256sum cmake.tar.gz.part | cut -d' ' -f1)
  if [ "$sum" != {q(CMAKE_SHA256)} ]; then rm -f cmake.tar.gz.part; echo "@@error CMake's download has SHA-256 $sum, not the pinned {CMAKE_SHA256}."; exit 4; fi
  mkdir -p cmake-3.31.6 && tar -xzf cmake.tar.gz.part -C cmake-3.31.6 --strip-components=1 && rm -f cmake.tar.gz.part
fi
work="$root/hot-step-src"
echo "@@ Fetching HOT-Step-CPP at {commit[:8]} with its ggml fork…"
if [ ! -d "$work/.git" ]; then git clone --quiet {q(repository)} "$work" </dev/null; fi
git -C "$work" fetch --quiet origin {q(commit)} </dev/null || git -C "$work" fetch --quiet origin </dev/null
git -C "$work" checkout --quiet --detach {q(commit)}
git -C "$work" submodule sync --quiet
git -C "$work" submodule update --init --recursive engine/ggml </dev/null
git -C "$work" submodule update --init engine/vendor/vst3sdk </dev/null
git -C "$work/engine/vendor/vst3sdk" submodule update --init base cmake pluginterfaces public.sdk </dev/null
build="$work/engine/build-train"
echo "@@ Configuring ace-train for CUDA $ver, architecture {architectures}…"
if ! "$cmake" -S "$work/engine" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release \\
    -DGGML_VULKAN=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF -DGGML_CCACHE=OFF \\
    -DGGML_CUDA=ON -DGGML_CUDA_NCCL=OFF {q("-DCMAKE_CUDA_ARCHITECTURES=" + architectures)} \\
    "-DCMAKE_CUDA_COMPILER=$cuda/bin/nvcc" -DHOT_STEP_ORT_PATHS=OFF '-DCMAKE_BUILD_RPATH=$ORIGIN' \\
    > "$root/trainer-configure.log" 2>&1; then
  tail -n 25 "$root/trainer-configure.log" | sed 's/^/@@log /'
  echo "@@error CMake could not configure the trainer; its log is $root/trainer-configure.log on the rented machine."
  exit 5
fi
variants=$(sed -n 's/^build \\(ggml-cpu-[a-z0-9_]*\\): phony.*/\\1/p' "$build/build.ninja" | sort -u | tr '\\n' ' ')
echo "@@ Building ace-train with $(nproc) processors; CUDA's kernels take most of the time…"
set +e
"$cmake" --build "$build" --target ace-train ggml-cuda $variants -j "$(nproc)" > "$root/trainer-build.log" 2>&1 &
pid=$!
while kill -0 "$pid" 2>/dev/null; do
  sleep 15
  step=$(grep -o '^\\[[0-9]*/[0-9]*\\]' "$root/trainer-build.log" | tail -n1)
  echo "@@ Building ace-train with $(nproc) processors: ${{step:-starting}} compile steps"
done
wait "$pid"; code=$?
set -e
if [ "$code" -ne 0 ]; then
  grep -n -i -E 'error|fatal' "$root/trainer-build.log" | head -n 20 | sed 's/^/@@log /'
  echo "@@error ace-train did not build (code $code); the whole log is $root/trainer-build.log on the rented machine."
  exit 6
fi
exe=$(find "$build" -maxdepth 2 -type f -name {TRAINER_EXE} | head -n1)
if [ -z "$exe" ]; then echo "@@error The build finished but wrote no {TRAINER_EXE}."; exit 7; fi
rm -rf {q(folder)}.part && mkdir -p {q(folder)}.part
cp "$exe" {q(folder)}.part/{TRAINER_EXE}
find "$build" -maxdepth 3 -name 'lib*.so*' -exec cp -a {{}} {q(folder)}.part/ \\;
missing=$(LD_LIBRARY_PATH={q(folder)}.part ldd {q(folder)}.part/{TRAINER_EXE} | grep 'not found' || true)
if [ -n "$missing" ]; then echo "$missing" | sed 's/^/@@log /'; echo "@@error {TRAINER_EXE} was built, but libraries it needs are missing."; exit 8; fi
ls {q(folder)}.part/libggml-cuda.so* >/dev/null 2>&1 || {{ echo "@@error The build wrote no libggml-cuda.so: the trainer would not compute on the card."; exit 8; }}
said=$(cd {q(folder)}.part && LD_LIBRARY_PATH=. timeout 60 ./{TRAINER_EXE} --help 2>&1 | head -n 1 | tr -d '"\\\\' | cut -c1-160 || true)
rm -rf {q(folder)} && mv {q(folder)}.part {q(folder)}
exe_sum=$(sha256sum {q(folder)}/{TRAINER_EXE} | cut -d' ' -f1)
files=$(find {q(folder)} -type f | wc -l)
printf '{{"commit":"%s","architectures":"%s","cuda":"%s","exe_sha256":"%s","files":%s,"says":"%s","built":"%s"}}\\n' \\
  {q(commit)} {q(architectures)} "$ver" "$exe_sum" "$files" "$said" "$(date '+%Y-%m-%d %H:%M:%S')" > {q(folder)}/{TRAINER_RECORD}
echo "@@ Packing the trainer to bring it home…"
tar -czf {q(tarball)}.part -C "$root" {q(folder.rsplit("/", 1)[1])} && mv -f {q(tarball)}.part {q(tarball)}
echo "@@json $(cat {q(folder)}/{TRAINER_RECORD})"
"""


def trainer_unpack_script(tarball, folder, exe_sha256):
    """Unpacks a trainer brought from home into folder there and checks its program against the SHA-256 it was built
    with; prints @@json with its record."""
    q = shlex.quote
    return f"""
set -euo pipefail
cd {q(REMOTE_ROOT)}
rm -rf {q(folder)}
tar -xzf {q(tarball)} -C {q(REMOTE_ROOT)}
sum=$(sha256sum {q(folder)}/{TRAINER_EXE} | cut -d' ' -f1)
if [ "$sum" != {q(exe_sha256)} ]; then echo "@@error The unpacked {TRAINER_EXE} has SHA-256 $sum, not {exe_sha256}."; exit 3; fi
missing=$(LD_LIBRARY_PATH={q(folder)} ldd {q(folder)}/{TRAINER_EXE} | grep 'not found' || true)
if [ -n "$missing" ]; then echo "$missing" | sed 's/^/@@log /'; echo "@@error Libraries {TRAINER_EXE} needs are missing on this machine."; exit 4; fi
echo "@@json $(cat {q(folder)}/{TRAINER_RECORD})"
"""


def trainer_found_script(folder):
    """The trainer already there, if one is: its record and its program's SHA-256 now."""
    q = shlex.quote
    return f"""
if [ -f {q(folder)}/{TRAINER_RECORD} ] && [ -x {q(folder)}/{TRAINER_EXE} ]; then
  echo "@@record $(cat {q(folder)}/{TRAINER_RECORD})"
  echo "@@exe $(sha256sum {q(folder)}/{TRAINER_EXE} | cut -d' ' -f1)"
else
  echo "@@none"
fi
"""


def studio_trainer_script(exe, trainer):
    """Whether the studio running from exe there was started with YUE_TRAIN_BIN naming trainer: @@uses yes|no per
    process, or @@none when it does not run."""
    q = shlex.quote
    return STUDIO_PIDS + f"""
found=0
for pid in $(studio_pids); do
  [ "$(readlink /proc/$pid/exe 2>/dev/null || true)" = {q(exe)} ] || continue
  found=1
  if tr '\\0' '\\n' < /proc/$pid/environ 2>/dev/null | grep -qx {q("YUE_TRAIN_BIN=" + trainer)}; then
    echo "@@uses yes $pid"
  else
    echo "@@uses no $pid"
  fi
done
[ "$found" = 1 ] || echo "@@none"
"""


def stop_studio_script(exe):
    """Stops the studio running from exe there, with everything in its session (the engine it started included), so
    the next Start starts it afresh; prints @@stopped."""
    q = shlex.quote
    return STUDIO_PIDS + f"""
mine() {{ for pid in $(studio_pids); do [ "$(readlink /proc/$pid/exe 2>/dev/null || true)" = {q(exe)} ] && echo "$pid"; done; true; }}
for pid in $(mine); do
  sid=$(ps -o sid= -p "$pid" | tr -d ' ')
  if [ -n "$sid" ]; then pkill -TERM -s "$sid" 2>/dev/null || true; else kill -TERM "$pid" 2>/dev/null || true; fi
done
for i in $(seq 1 30); do
  [ -n "$(mine)" ] || break
  sleep 1
done
for pid in $(mine); do
  sid=$(ps -o sid= -p "$pid" | tr -d ' ')
  if [ -n "$sid" ]; then pkill -KILL -s "$sid" 2>/dev/null || true; else kill -KILL "$pid" 2>/dev/null || true; fi
done
sleep 1
if [ -n "$(mine)" ]; then echo "@@error YuE2 Studio ($(mine | tr '\\n' ' ')) is still running after it was told to stop."; exit 3; fi
echo "@@stopped"
"""
