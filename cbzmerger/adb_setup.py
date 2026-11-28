"""Locating, installing and connecting to adb."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from .constants import ADB_URL
from .ui import ask, Bar, emit_line, err, info, ok, paint, warn, warn_plain
from .adb import Adb


NO_DEVICE_HELP = [
    "No device found. On your phone:",
    "  1. Enable Developer options (Settings > About phone > tap 'Build number' 7 times)",
    "  2. Turn on USB debugging (Settings > Developer options)",
    "  3. Connect the phone to this PC with a USB cable",
    "  4. Unlock the phone and tap 'Allow' on the 'Allow USB debugging?' prompt",
    "  5. Run this command again",
]


def _adb_name():
    return "adb.exe" if os.name == "nt" else "adb"


def tools_dir():
    """Private folder for the auto-installed platform-tools (PATH is never touched)."""
    env = os.environ.get("CBZ_TOOLS_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "yukimi-to-koodo"


def find_adb():
    exe = shutil.which("adb")
    if exe:
        return exe
    local = tools_dir() / "platform-tools" / _adb_name()
    if local.is_file():
        return str(local)
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            cand = Path(root) / "platform-tools" / _adb_name()
            if cand.is_file():
                return str(cand)
    return None


def install_adb():
    """Download Google's platform-tools and unpack them into tools_dir().
    Returns the adb path, or None on failure."""
    osname = "windows" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    url = os.environ.get("CBZ_ADB_URL") or ADB_URL.format(os=osname)
    dest = tools_dir()
    zpath = dest / "platform-tools.zip"
    part = zpath.with_name(zpath.name + ".part")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        info(f"Downloading platform-tools from {paint(url, 'bold')}")
        with urllib.request.urlopen(url, timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            bar = Bar(total, "Downloading")
            finished = False
            try:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    bar.advance(len(chunk))
                finished = True
            finally:
                bar.close() if finished else bar.abort()
        part.replace(zpath)

        with zipfile.ZipFile(zpath) as z:
            bad = z.testzip()
            if bad:
                raise zipfile.BadZipFile(f"corrupt download ({bad})")
            if not any(n.replace("\\", "/").startswith("platform-tools/") for n in z.namelist()):
                raise zipfile.BadZipFile("unexpected archive layout")
            target = dest / "platform-tools"
            if target.exists():
                shutil.rmtree(target)
            info("Unpacking...")
            z.extractall(dest)  # zipfile strips absolute paths and '..' components
        adb_path = dest / "platform-tools" / _adb_name()
        if not adb_path.is_file():
            raise RuntimeError("adb was not found inside the downloaded archive")
        if os.name != "nt":
            adb_path.chmod(0o755)
        r = subprocess.run([str(adb_path), "version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            raise RuntimeError("the downloaded adb would not start: " + (r.stderr or r.stdout).strip())
        ok(f"adb installed to {paint(adb_path.parent, 'bold')} (your PATH was not changed)")
        return str(adb_path)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as e:
        err(f"could not install adb automatically: {e}")
        return None
    finally:
        part.unlink(missing_ok=True)
        zpath.unlink(missing_ok=True)


def offer_adb_install(auto):
    """adb is missing: ask (or just do it with --install-adb). Returns adb path or None."""
    warn("adb (Android platform-tools) was not found on this PC.")
    if not auto:
        try:
            ans = ask("Download and install it now from Google (~10 MB, no admin rights needed)? [Y/n]:")
        except EOFError:
            print()
            ans = "n"
        if ans.strip().lower() not in ("", "y", "yes"):
            warn("Install platform-tools yourself (https://developer.android.com/tools/releases/platform-tools) "
                 "and add it to PATH, or re-run and answer Y. Use --install-adb to skip this question.")
            return None
    return install_adb()


def parse_devices(text):
    devices, seen_header = [], False
    for line in text.replace("\r", "").splitlines():
        line = line.strip()
        if line.lower().startswith("list of devices"):
            seen_header = True
            continue
        if not seen_header or not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def connect_adb(serial=None, interactive=False, auto_install=False):
    exe = find_adb() or offer_adb_install(auto_install)
    if not exe:
        return None
    info(f"Checking for devices: {paint('adb devices', 'bold')}")
    try:
        r = subprocess.run([exe, "devices"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except subprocess.TimeoutExpired:
        err("`adb devices` timed out. Try `adb kill-server` and run this again.")
        return None
    devices = parse_devices(r.stdout)
    ready = [s for s, st in devices if st == "device"]

    if serial:
        if serial not in ready:
            err(f"device '{serial}' is not connected/ready. Seen: "
                + (", ".join(f"{s} ({st})" for s, st in devices) or "none"))
            return None
        chosen = serial
    elif len(ready) == 1:
        chosen = ready[0]
    elif len(ready) > 1:
        if not interactive:
            err("more than one device connected; pick one with --serial: " + ", ".join(ready))
            return None
        print(paint("Several devices are connected:", "bold"))
        for i, s in enumerate(ready, 1):
            print(f"  {paint(str(i), 'cyan')}. {s}")
        while True:
            c = ask("Which one?").strip()
            if c.isdigit() and 1 <= int(c) <= len(ready):
                chosen = ready[int(c) - 1]
                break
    else:
        states = {st for _, st in devices}
        if "unauthorized" in states:
            err("A device is connected but not authorized. Unlock the phone, tap 'Allow' on the "
                "'Allow USB debugging?' prompt (tick 'Always allow'), then try again.")
        elif "offline" in states:
            err("The device shows as offline. Unplug and reconnect the cable, re-allow the USB "
                "debugging prompt, or run `adb kill-server`, then try again.")
        else:
            emit_line(paint(NO_DEVICE_HELP[0], "red", "bold"), sys.stderr)
            for line in NO_DEVICE_HELP[1:]:
                warn_plain(line)
        return None
    ok(f"Device connected: {paint(chosen, 'bold')}")
    return Adb(exe, chosen)
