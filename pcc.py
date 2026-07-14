#!/usr/bin/env python3
"""
Proton Command Center (PCC)
Per-game launch options, DLSS DLL management, and shader cache control
for Steam on Linux. Stdlib only. Run: python3 pcc.py  ->  http://localhost:8686
"""

import hashlib
import json
import os
import tempfile
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STARTED_AT = int(time.time())
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.9.7"
PORT = int(os.environ.get("PCC_PORT", "8686"))
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".local/share/proton-command-center"
DLL_LIBRARY = DATA_DIR / "dlls"        # dlls/<kind>/<version>/<name>.dll
BACKUP_DIR = DATA_DIR / "backups"      # backups/<appid>/<relpath>.pccbak
DATA_DIR.mkdir(parents=True, exist_ok=True)
DLL_LIBRARY.mkdir(parents=True, exist_ok=True)
_DEDUPE_ON_IMPORT = True  # dedupe runs lazily via dll_library()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

DLSS_KINDS = {
    "nvngx_dlss.dll":  {"kind": "sr",  "label": "DLSS Super Resolution"},
    "nvngx_dlssg.dll": {"kind": "fg",  "label": "DLSS Frame Generation"},
    "nvngx_dlssd.dll": {"kind": "rr",  "label": "DLSS Ray Reconstruction"},
}
KIND_TO_NAME = {v["kind"]: k for k, v in DLSS_KINDS.items()}

# NVIDIA's official DLSS SR repo ships the DLL in-tree.
NVIDIA_DLSS_REPO_API = "https://api.github.com/repos/NVIDIA/DLSS/contents/lib/Windows_x86_64/rel"

TASKS = {}  # task_id -> {status, progress, detail}
STATE_FILE = DATA_DIR / "state.json"
STATE_LOCK = threading.Lock()
CONFIG_FILE = DATA_DIR / "config.json"
ART_DIR = DATA_DIR / "art"
ART_DIR.mkdir(parents=True, exist_ok=True)
SGDB_API = "https://www.steamgriddb.com/api/v2"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg):
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=1))
    tmp.replace(CONFIG_FILE)
    try:
        CONFIG_FILE.chmod(0o600)  # API key lives here
    except OSError:
        pass


def _sgdb_get(path, key):
    req = urllib.request.Request(f"{SGDB_API}{path}", headers={
        "Authorization": f"Bearer {key}", "User-Agent": "proton-command-center"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _valid_image(data):
    return bool(data) and (data[:2] == b"\xff\xd8"            # JPEG
                           or data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG
                           or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _fetch_image(url):
    req = urllib.request.Request(url, headers={"User-Agent": "proton-command-center"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        ct = r.headers.get("Content-Type", "image/png").split(";")[0]
    if not _valid_image(data):
        return None
    if not ct.startswith("image/"):
        ct = "image/png"
    return data, ct


def _clean_game_name(name):
    """'Mortal Shell II - Open Beta™' -> 'Mortal Shell II' for name search."""
    name = re.sub(r"[™®©]", "", name or "")
    name = re.sub(r"\s*[-–—:(\[]?\s*(open beta|closed beta|beta|demo|"
                  r"playtest|early access|technical test)\s*[)\]]?\s*$",
                  "", name, flags=re.I)
    return name.strip()


ART_MISSES = {}  # appid -> timestamp of last failed lookup (avoid re-hammering)


def _sgdb_grids(path_base, key, t):
    """Grids at preferred dimensions first, then any static grid at all —
    many entries (esp. new/beta games) only have portrait or odd sizes."""
    for suffix in ("?dimensions=460x215,920x430&types=static", "?types=static"):
        try:
            data = _sgdb_get(path_base + suffix, key)
            grids = data.get("data") or []
            if grids:
                res = _fetch_image(grids[0]["url"])
                if res:
                    return res
                t.append(f"{path_base}: grid fetch invalid ({suffix})")
            else:
                t.append(f"{path_base}: no grids ({suffix})")
        except Exception as e:
            t.append(f"{path_base}: {e}")
    return None


def sgdb_art(appid, name=None, trace=None):
    """Resolve 460x215 art through a cascade covering beta/demo appids.
    Cached files are validated by magic bytes on every serve — corrupt
    entries from failed fetches self-delete and re-fetch. Misses are
    negative-cached for 10 minutes only."""
    t = trace if trace is not None else []
    for ext, ct in (("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")):
        cached = ART_DIR / f"{appid}.{ext}"
        if cached.is_file():
            data = cached.read_bytes()
            if _valid_image(data):
                t.append(f"disk cache hit ({ext})")
                return data, ct
            cached.unlink(missing_ok=True)   # poisoned entry — self-heal
            t.append(f"deleted corrupt cached {ext}")
    if time.time() - ART_MISSES.get(str(appid), 0) < 600:
        t.append("negative-cached (retries in <10 min)")
        return None

    def save(res):
        img, ct = res
        ext = {"image/jpeg": "jpg", "image/png": "png",
               "image/webp": "webp"}.get(ct, "png")
        (ART_DIR / f"{appid}.{ext}").write_bytes(img)
        return img, ct

    # 1. Steam CDN, server-side
    try:
        res = _fetch_image("https://cdn.cloudflare.steamstatic.com"
                           f"/steam/apps/{appid}/header.jpg")
        if res:
            t.append("steam CDN: ok")
            return save(res)
        t.append("steam CDN: invalid image body")
    except Exception as e:
        t.append(f"steam CDN: {e}")

    key = load_config().get("sgdb_api_key", "").strip()
    if not key:
        t.append("no SGDB key set")
    else:
        # 2. SGDB by Steam appid
        res = _sgdb_grids(f"/grids/steam/{appid}", key, t)
        if res:
            t.append("SGDB appid: ok")
            return save(res)
        # 3. SGDB by cleaned name
        clean = _clean_game_name(name)
        if not clean:
            t.append("no name provided for search")
        else:
            try:
                hits = _sgdb_get("/search/autocomplete/"
                                 + urllib.parse.quote(clean), key).get("data") or []
                if not hits:
                    t.append(f"SGDB name search '{clean}': no matches")
                for hit in hits[:3]:
                    res = _sgdb_grids(f"/grids/game/{hit['id']}", key, t)
                    if res:
                        t.append(f"SGDB name search '{clean}' -> "
                                 f"{hit.get('name', hit['id'])}: ok")
                        return save(res)
            except Exception as e:
                t.append(f"SGDB name search: {e}")

    ART_MISSES[str(appid)] = time.time()
    return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"compiled": {}}


def save_state(state):
    with STATE_LOCK:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE_FILE)


def driver_version():
    try:
        return Path("/sys/module/nvidia/version").read_text().strip()
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return "unknown"


# Fossilize file taxonomy inside steamapps/shadercache/<appid>/fozpipelinesv6/
#   steam_pipeline_cache.foz                        -> input (downloaded/captured)
#   steamapprun_pipeline_cache.<hash>.<n>.foz       -> input (runtime capture)
#   steamapp_pipeline_cache.foz                     -> input
#   steam_pipeline_cache_whitelist.foz              -> NOT input
#   replay_cache.<hash>.foz                         -> the replayer ledger (output)
# Pipelines live BOTH at the top level and inside steamapprun_pipeline_cache.<hash>/
# directories (one per GPU+driver), so classify by filename, never by path.
FOZ_INPUT_RE = re.compile(
    r"^(steam_pipeline_cache"
    r"|steamapp_pipeline_cache"
    r"|steamapprun_pipeline_cache\.[0-9a-f]+\.\d+)\.foz$", re.I)
FOZ_LEDGER_RE = re.compile(r"^replay_cache\.[0-9a-f]+\.foz$", re.I)


def find_foz(root, appid):
    """Replayable pipeline databases. Excludes whitelists, the replayer
    ledger, and driver caches (mesa_shader_cache_sf/**)."""
    out = []
    for lib in library_folders(root):
        base = lib / "shadercache" / str(appid) / "fozpipelinesv6"
        if not base.is_dir():
            continue
        for p in base.rglob("*.foz"):
            if FOZ_INPUT_RE.match(p.name) and p.stat().st_size > 0:
                out.append(str(p))
    return sorted(set(out))


def find_replayer_cache(root, appid):
    """Steam's replay ledger (replay_cache.<hash>.foz). Passing this to
    fossilize_replay records our work where Steam looks, so its own pass
    finds the pipelines already done. Newest wins if several exist."""
    hits = []
    for lib in library_folders(root):
        base = lib / "shadercache" / str(appid) / "fozpipelinesv6"
        if not base.is_dir():
            continue
        for p in base.rglob("*.foz"):
            if FOZ_LEDGER_RE.match(p.name):
                hits.append(p)
    if not hits:
        return None
    hits.sort(key=lambda p: p.stat().st_mtime)
    return str(hits[-1])


def foz_fingerprint(foz_files):
    if not foz_files:
        return None
    parts = []
    for f in foz_files:
        try:
            st = os.stat(f)
            parts.append(f"{f}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            pass
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()


def compiled_status(root, appid, state=None, drv=None):
    """compiled stays true as long as a compile is recorded and the driver
    hasn't changed — new pipeline data since the compile marks it 'outdated'
    (recompile recommended) rather than flipping the light off. Only a
    driver update or cache deletion clears it."""
    state = state or load_state()
    entry = state.get("compiled", {}).get(str(appid))
    if not entry:
        return {"compiled": False, "outdated": False}
    drv = drv or driver_version()
    if entry.get("driver") != drv:
        return {"compiled": False, "outdated": False,
                "stale_reason": "driver changed",
                "compiled_at": entry.get("compiled_at")}
    fp = foz_fingerprint(find_foz(root, appid))
    outdated = fp is not None and entry.get("fingerprint") != fp
    return {
        "compiled": True,
        "outdated": outdated,
        "compiled_at": entry.get("compiled_at"),
        "driver": entry.get("driver"),
        "stale_reason": "new pipeline data since compile" if outdated else None,
    }


def mark_compiled(root, appid):
    state = load_state()
    state.setdefault("compiled", {})[str(appid)] = {
        "fingerprint": foz_fingerprint(find_foz(root, appid)),
        "driver": driver_version(),
        "compiled_at": int(time.time()),
    }
    save_state(state)


def unmark_compiled(appid):
    state = load_state()
    if state.get("compiled", {}).pop(str(appid), None) is not None:
        save_state(state)


# --------------------------------------------------------------------------
# Steam discovery
# --------------------------------------------------------------------------

def steam_root():
    for p in [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
    ]:
        if (p / "steamapps").is_dir():
            return p.resolve()
    return None


def steam_running():
    try:
        out = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
        return out.returncode == 0
    except FileNotFoundError:
        return False


def shutdown_steam(timeout=60):
    """Ask Steam to exit gracefully and wait until it's gone.
    Graceful matters: Steam flushes localconfig.vdf on clean exit."""
    if not steam_running():
        return True
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH — close Steam manually")
    subprocess.Popen([exe, "-shutdown"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not steam_running():
            time.sleep(2)  # give it a moment to finish flushing files
            return True
        time.sleep(1)
    raise RuntimeError("Steam didn't close within 60s — close it manually and save again")


SESSION_ENV_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")


def session_env():
    """Launch env for GUI apps. The backend may have been started without
    DISPLAY/WAYLAND_DISPLAY (systemd, ssh, stale shell) which makes Steam
    fail with display errors — harvest the vars from the user's running
    graphical session processes instead."""
    env = dict(os.environ)
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    uid = os.getuid()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            p = Path("/proc") / pid
            try:
                if p.stat().st_uid != uid:
                    continue
                raw = (p / "environ").read_bytes()
            except OSError:
                continue
            found = {}
            for chunk in raw.split(b"\x00"):
                try:
                    k, _, v = chunk.decode(errors="ignore").partition("=")
                except Exception:
                    continue
                if k in SESSION_ENV_KEYS and v:
                    found[k] = v
            if found.get("DISPLAY") or found.get("WAYLAND_DISPLAY"):
                env.update(found)
                return env
    except OSError:
        pass
    return env


def _spawn_detached(cmd):
    """Launch GUI apps OUTSIDE our service cgroup. Without this, Steam and
    games become children of the backend's systemd unit: the service gets
    charged for their memory, and a service restart kills the game."""
    env = session_env()
    if shutil.which("systemd-run"):
        try:
            subprocess.Popen(["systemd-run", "--user", "--scope", "--collect",
                              "--quiet"] + cmd, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            pass
    subprocess.Popen(cmd, start_new_session=True, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def launch_steam():
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    return _spawn_detached([exe])


def launch_game(appid):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    return _spawn_detached([exe, f"steam://rungameid/{appid}"])


def library_folders(root):
    """All steamapps dirs across library folders."""
    libs = [root / "steamapps"]
    vdf = root / "steamapps/libraryfolders.vdf"
    if vdf.is_file():
        try:
            data = vdf_parse(vdf.read_text(errors="replace"))
            folders = ci_get(data, "libraryfolders") or {}
            for _, entry in folders.items():
                if isinstance(entry, dict):
                    p = entry.get("path")
                    if p:
                        sp = Path(p) / "steamapps"
                        if sp.is_dir() and sp.resolve() not in [l.resolve() for l in libs]:
                            libs.append(sp)
        except Exception:
            pass
    return libs


SKIP_APPIDS = {
    "228980",   # Steamworks Common Redistributables
    "1070560", "1391110", "1628350", "2180100", "4183110",  # SLR variants
    "1493710", "2348590", "2805730", "3175060", "3658110", "4628710",  # Proton
    "1887720", "961940", "1054830", "1113280", "1245040", "1420170",
    "2456610", "1161040",  # older Proton + EAC runtime
}
SKIP_NAME_RE = re.compile(
    r"^(proton|steam linux runtime|steamworks|pressure vessel|"
    r"steam client|steam sdk|dedicated server)", re.I)


def list_games(root):
    games = []
    seen = set()
    for lib in library_folders(root):
        for manifest in sorted(lib.glob("appmanifest_*.acf")):
            try:
                data = vdf_parse(manifest.read_text(errors="replace"))
            except Exception:
                continue
            app = ci_get(data, "AppState") or {}
            appid = app.get("appid")
            name = app.get("name")
            installdir = app.get("installdir")
            if not appid or appid in seen:
                continue
            seen.add(appid)
            if appid in SKIP_APPIDS or (name and SKIP_NAME_RE.match(name)):
                continue
            install_path = lib / "common" / (installdir or "")
            flags = int(ci_get(app, "StateFlags") or 0)
            downloaded = int(ci_get(app, "BytesDownloaded") or 0)
            to_download = int(ci_get(app, "BytesToDownload") or 0)
            # Only treat as "installing" when there is real pending work:
            # a missing/odd StateFlags must never mean 'forever downloading'.
            installing = (flags != 4 and to_download > 0
                          and downloaded < to_download)
            games.append({
                "appid": appid,
                "name": name or installdir or appid,
                "install_path": str(install_path),
                "installed": install_path.is_dir(),
                "fully_installed": not installing,
                "download_pct": round(100 * downloaded / to_download, 1)
                                if installing else None,
                "size_bytes": int(ci_get(app, "SizeOnDisk") or 0),
                "library": str(lib),
            })
    games.sort(key=lambda g: g["name"].lower())
    return games


# --------------------------------------------------------------------------
# VDF (text) parse / serialize — round-trip safe for localconfig.vdf
# --------------------------------------------------------------------------

def vdf_parse(text):
    i, n = 0, len(text)

    def skip_ws():
        nonlocal i
        while i < n:
            if text[i] in " \t\r\n":
                i += 1
            elif text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
            else:
                break

    def read_string():
        nonlocal i
        assert text[i] == '"'
        i += 1
        out = []
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                out.append(text[i:i + 2]); i += 2
            elif c == '"':
                i += 1
                return "".join(out)
            else:
                out.append(c); i += 1
        raise ValueError("unterminated string")

    def read_object():
        nonlocal i
        obj = {}
        while True:
            skip_ws()
            if i >= n:
                return obj
            if text[i] == "}":
                i += 1
                return obj
            if text[i] != '"':
                raise ValueError(f"expected key at byte {i}")
            key = read_string()
            skip_ws()
            if i < n and text[i] == "{":
                i += 1
                obj[key] = read_object()
            elif i < n and text[i] == '"':
                obj[key] = read_string()
            else:
                raise ValueError(f"expected value at byte {i}")

    skip_ws()
    result = {}
    while i < n:
        skip_ws()
        if i >= n:
            break
        key = read_string()
        skip_ws()
        if i < n and text[i] == "{":
            i += 1
            result[key] = read_object()
        else:
            result[key] = read_string()
    return result


def vdf_dump(obj, indent=0):
    pad = "\t" * indent
    out = []
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append(f'{pad}"{k}"\n{pad}{{\n')
            out.append(vdf_dump(v, indent + 1))
            out.append(f"{pad}}}\n")
        else:
            out.append(f'{pad}"{k}"\t\t"{v}"\n')
    return "".join(out)


def ci_get(d, key):
    """Case-insensitive dict get."""
    if not isinstance(d, dict):
        return None
    for k in d:
        if k.lower() == key.lower():
            return d[k]
    return None


def ci_ensure(d, key):
    for k in d:
        if k.lower() == key.lower():
            return d[k]
    d[key] = {}
    return d[key]


def find_localconfigs(root):
    """Every user's localconfig.vdf, newest first."""
    userdata = root / "userdata"
    if not userdata.is_dir():
        return []
    configs = list(userdata.glob("*/config/localconfig.vdf"))
    configs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return configs


def get_launch_options(root, appid):
    for cfg in find_localconfigs(root):
        try:
            data = vdf_parse(cfg.read_text(errors="replace"))
        except Exception:
            continue
        store = ci_get(data, "UserLocalConfigStore")
        apps = _apps_node(store)
        if apps:
            app = ci_get(apps, str(appid))
            if app:
                lo = ci_get(app, "LaunchOptions")
                if lo is not None:
                    return {"value": vdf_unescape(lo), "config": str(cfg)}
    cfgs = find_localconfigs(root)
    return {"value": "", "config": str(cfgs[0]) if cfgs else None}


def _apps_node(store):
    sw = ci_get(store, "Software")
    valve = ci_get(sw, "Valve")
    steam = ci_get(valve, "Steam")
    return ci_get(steam, "apps") or ci_get(steam, "Apps")


def vdf_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def vdf_unescape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")



def set_game_config(root, appid, launch_value=None, compat_tool=None,
                    close_steam=False):
    """Single-save: write launch options AND compat tool together, closing
    Steam once for both rather than twice."""
    result = {}
    if close_steam and steam_running():
        shutdown_steam()
        close_steam = False  # already down; downstream calls shouldn't retry
    if launch_value is not None:
        result["launch"] = set_launch_options(root, appid, launch_value,
                                               close_steam=False)
    if compat_tool is not None:
        result["compat"] = set_compat_tool(root, appid, compat_tool,
                                           close_steam=False)
    return {"saved": True, **result}


SHADER_ENV_VARS = {
    # Only vars that still do something on modern stock Proton (DXVK >= 2.7) are
    # included. DXVK_ASYNC and DXVK_STATE_CACHE were both removed upstream once
    # Vulkan GPL (graphics_pipeline_library) made them obsolete, so they are
    # deliberately omitted — setting them achieves nothing. What remains is the
    # NVIDIA driver-level shader disk cache, which is independent of DXVK and
    # genuinely persists compiled shaders across runs.
    "__GL_SHADER_DISK_CACHE": "1",
    "__GL_SHADER_DISK_CACHE_PATH": str(Path.home() / ".cache" / "nvidia-shaders"),
    "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP": "1",   # keep cache instead of purging on size
    "__GL_SHADER_DISK_CACHE_SIZE": "10737418240",  # 10 GiB ceiling
}


def read_environment():
    path = Path("/etc/environment")
    try:
        return path.read_text()
    except OSError:
        return ""


def environment_shader_status():
    txt = read_environment()
    present = {}
    for k in SHADER_ENV_VARS:
        m = re.search(rf"^{re.escape(k)}=(.*)$", txt, re.M)
        present[k] = m.group(1).strip().strip('"') if m else None
    return {"enabled": all(present[k] is not None for k in SHADER_ENV_VARS),
            "vars": present}


def set_environment_shaders(enable):
    """Add or remove the shader-cache env vars in /etc/environment via pkexec.
    Preserves every other line; only touches our keys."""
    Path(SHADER_ENV_VARS["__GL_SHADER_DISK_CACHE_PATH"]).mkdir(
        parents=True, exist_ok=True)
    txt = read_environment()
    lines = [l for l in txt.splitlines()
             if not any(l.strip().startswith(f"{k}=") for k in SHADER_ENV_VARS)]
    if enable:
        lines.append("# Proton Command Center - shader cache")
        for k, v in SHADER_ENV_VARS.items():
            lines.append(f'{k}="{v}"' if " " in v or "/" in v else f"{k}={v}")
    else:
        lines = [l for l in lines
                 if l.strip() != "# Proton Command Center - shader cache"]
    new = "\n".join(lines).rstrip() + "\n"

    # write via a temp file + pkexec cp (root-owned target)
    tmp = Path(tempfile.gettempdir()) / f"pcc-environment-{os.getpid()}"
    tmp.write_text(new)
    try:
        r = subprocess.run(["pkexec", "cp", str(tmp), "/etc/environment"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or
                               "pkexec was cancelled or failed")
    finally:
        tmp.unlink(missing_ok=True)
    return {"enabled": enable, "note": "Log out and back in for changes to apply."}


def set_launch_options(root, appid, value, close_steam=False):
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running. Close Steam first — it overwrites localconfig.vdf on exit.")
    configs = find_localconfigs(root)
    if not configs:
        raise RuntimeError("No localconfig.vdf found under userdata/")
    cfg = configs[0]
    data = vdf_parse(cfg.read_text(errors="replace"))
    store = ci_ensure(data, "UserLocalConfigStore")
    sw = ci_ensure(store, "Software")
    valve = ci_ensure(sw, "Valve")
    steam = ci_ensure(valve, "Steam")
    apps = None
    for k in steam:
        if k.lower() == "apps":
            apps = steam[k]
    if apps is None:
        apps = steam.setdefault("apps", {})
    app = ci_ensure(apps, str(appid))
    # remove existing LaunchOptions key regardless of case
    for k in list(app.keys()):
        if k.lower() == "launchoptions":
            del app[k]
    if value.strip():
        app["LaunchOptions"] = vdf_escape(value.strip())
    # timestamped backup, then atomic-ish write
    bak = cfg.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(cfg, bak)
    tmp = cfg.with_suffix(".vdf.pcc-tmp")
    tmp.write_text(vdf_dump(data))
    tmp.replace(cfg)
    return {"saved": True, "backup": str(bak), "config": str(cfg)}


# --------------------------------------------------------------------------
# DLSS DLL handling
# --------------------------------------------------------------------------

def pe_version(path):
    """Read file version from VS_FIXEDFILEINFO without dependencies.

    The 0xFEEF04BD signature can appear coincidentally in a DLL's data before
    the real version resource, yielding garbage like '46863.0.46863.4696'. So
    we scan ALL occurrences and accept only a block whose dwStrucVersion is a
    sane value and whose resulting version looks like a real DLSS version
    (major in a plausible range), preferring the highest valid one."""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    sig = struct.pack("<I", 0xFEEF04BD)
    best = None
    start = 0
    while True:
        idx = blob.find(sig, start)
        if idx < 0:
            break
        start = idx + 4
        if idx + 16 > len(blob):
            continue
        # dwStrucVersion (right after signature) is normally 0x00010000
        struc = struct.unpack_from("<I", blob, idx + 4)[0]
        if struc not in (0x00010000, 0x00000000, 0x00010001):
            continue
        ms, ls = struct.unpack_from("<II", blob, idx + 8)
        a, b, c, d = ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF
        # DLSS versions: major is small (1,2,3) or the DLSS4 scheme (310+),
        # never five digits. Reject implausible parses.
        if a > 999 or a == 0:
            continue
        cand = (a, b, c, d)
        if best is None or cand > best:
            best = cand
    if best is None:
        return None
    return f"{best[0]}.{best[1]}.{best[2]}.{best[3]}"


def scan_game_dlss(install_path):
    found = []
    base = Path(install_path)
    if not base.is_dir():
        return found
    # Some games ship a debug copy of the DLSS DLLs in a Development/ or Debug/
    # subfolder. Those are not loaded at runtime, so listing them just creates a
    # confusing duplicate entry. Skip them.
    SKIP_DIRS = {"development", "debug", "profile", "profiling"}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower() in DLSS_KINDS:
                p = Path(dirpath) / fn
                meta = DLSS_KINDS[fn.lower()]
                ver = pe_version(p)
                found.append({
                    "path": str(p),
                    "name": fn,
                    "kind": meta["kind"],
                    "label": meta["label"],
                    "version": ver,
                    "friendly": friendly_dlss(ver),
                    "size": p.stat().st_size,
                    "backed_up": _backup_path(p).exists(),
                })
    return found


def _backup_path(dll_path):
    p = Path(dll_path)
    h = re.sub(r"[^A-Za-z0-9]", "_", str(p))
    return BACKUP_DIR / f"{h}.pccbak"



def dedupe_dll_library():
    """One-time housekeeping: if two directories under a kind hold the same real
    DLL version (e.g. a garbage-named dir from the old parser plus a correctly
    named one), keep the correctly-named one and remove the rest. Safe to run on
    every startup."""
    if not DLL_LIBRARY.is_dir():
        return
    for kind_dir in DLL_LIBRARY.iterdir():
        if not kind_dir.is_dir():
            continue
        by_version = {}
        for vdir in kind_dir.iterdir():
            if not vdir.is_dir():
                continue
            dll = next(vdir.glob("*.dll"), None)
            if not dll:
                shutil.rmtree(vdir, ignore_errors=True)
                continue
            real = pe_version(dll) or vdir.name
            by_version.setdefault(real, []).append(vdir)
        for real, dirs in by_version.items():
            if len(dirs) < 2:
                continue
            # keep the dir whose name matches the real version, else the first
            keep = next((d for d in dirs if d.name == real), dirs[0])
            for d in dirs:
                if d != keep:
                    shutil.rmtree(d, ignore_errors=True)


def dll_library():
    dedupe_dll_library()
    out = []
    seen = set()
    for kind_dir in sorted(DLL_LIBRARY.iterdir()) if DLL_LIBRARY.is_dir() else []:
        if not kind_dir.is_dir():
            continue
        for ver_dir in sorted(kind_dir.iterdir()):
            dll = next(ver_dir.glob("*.dll"), None)
            if dll:
                # Prefer the version read from the DLL itself; the directory
                # name may be stale garbage from the old parser. Fall back to
                # the dir name only if the DLL can't be read.
                real = pe_version(dll) or ver_dir.name
                # Dedupe by (kind, real version): an old garbage-named dir and a
                # freshly-named dir can hold the same actual DLL version.
                key = (kind_dir.name, real)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "kind": kind_dir.name,
                    "version": real,
                    "friendly": friendly_dlss(real),
                    "path": str(dll),
                    "name": dll.name,
                })
    return out


def import_dll(src_path):
    p = Path(src_path).expanduser()
    if not p.is_file():
        raise RuntimeError(f"File not found: {p}")
    if p.name.lower() not in DLSS_KINDS:
        raise RuntimeError(f"Not a recognised DLSS DLL name: {p.name}")
    ver = pe_version(p) or "unknown"
    kind = DLSS_KINDS[p.name.lower()]["kind"]
    kind_root = DLL_LIBRARY / kind
    dest = kind_root / ver
    dest.mkdir(parents=True, exist_ok=True)
    # clear any stale file already in this version dir, then copy the new one
    for old in dest.glob("*.dll"):
        old.unlink()
    shutil.copy2(p, dest / p.name.lower())
    # Remove any OTHER directory for this kind that actually holds the SAME
    # version (e.g. a garbage-named dir from the old parser). Different real
    # versions are kept — downgrading stays possible.
    if kind_root.is_dir():
        for vdir in kind_root.iterdir():
            if not vdir.is_dir() or vdir.name == ver:
                continue
            other = next(vdir.glob("*.dll"), None)
            if other and (pe_version(other) or vdir.name) == ver:
                shutil.rmtree(vdir, ignore_errors=True)
    return {"kind": kind, "version": ver}


# NVIDIA publishes all three DLLs officially on GitHub:
#   SR  -> NVIDIA/DLSS            FG + RR -> NVIDIAGameWorks/Streamline
# We search each repo's file tree by name instead of hardcoding paths, so
# repo reorganisations don't break downloads.
DLL_SOURCES = {
    "sr": [("NVIDIA/DLSS", "nvngx_dlss.dll"),
           ("NVIDIAGameWorks/Streamline", "nvngx_dlss.dll")],
    "fg": [("NVIDIAGameWorks/Streamline", "nvngx_dlssg.dll")],
    "rr": [("NVIDIAGameWorks/Streamline", "nvngx_dlssd.dll")],
}


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, AttributeError):
        return (0,)


def friendly_dlss(version):
    """310.2.1.0 -> {'gen': 'DLSS 4', 'short': '310.2.1'};
    3.7.10.0 -> {'gen': 'DLSS 3', 'short': '3.7.10'}"""
    if not version:
        return {"gen": "DLSS", "short": "?"}
    parts = str(version).split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    short = ".".join(parts)
    major = version_tuple(version)[0]
    if major >= 310:
        gen = "DLSS 4"
    elif major == 3:
        gen = "DLSS 3"
    elif major == 2:
        gen = "DLSS 2"
    else:
        gen = "DLSS"
    return {"gen": gen, "short": short}


def _gh_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pcc",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


CANDIDATE_DLL_DIRS = [
    "bin/x64", "bin/x64/rel", "bin/x64/release", "bin/x64/development",
    "lib/Windows_x86_64/rel", "lib/Windows_x86_64",
    "sdk/bin/x64", "runtime/bin/x64",
]


def _gh_bytes(url, task=None):
    req = urllib.request.Request(url, headers={"User-Agent": "pcc"})
    with urllib.request.urlopen(req, timeout=300) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got, chunks = 0, []
        while True:
            c = r.read(262144)
            if not c:
                break
            chunks.append(c)
            got += len(c)
            if task and total:
                TASKS[task]["progress"] = int(got / total * 100)
    return b"".join(chunks)


def _resolve_lfs(repo, branch, path, data, task=None):
    """Large NVIDIA binaries are stored via Git LFS: the raw URL returns a
    small pointer file. media.githubusercontent serves the real content."""
    if data.startswith(b"version https://git-lfs"):
        return _gh_bytes(f"https://media.githubusercontent.com/media/"
                         f"{repo}/{branch}/{path}", task)
    return data


def _find_in_tree(repo, branch, fname):
    """Returns (paths, truncated). GitHub truncates trees for big repos like
    Streamline, so a miss with truncated=True is inconclusive."""
    tree = _gh_json(f"https://api.github.com/repos/{repo}"
                    f"/git/trees/{branch}?recursive=1")
    hits = [e["path"] for e in tree.get("tree", [])
            if e.get("type") == "blob"
            and (e["path"].lower().endswith("/" + fname)
                 or e.get("path", "").lower() == fname)]
    hits.sort(key=lambda p: ("rel" not in p.lower() and "bin" not in p.lower(),
                             "dev" in p.lower(), len(p)))
    return hits, bool(tree.get("truncated"))


def _probe_dirs(repo, branch, fname):
    """Contents-API probe of known DLL directories — works even when the
    tree listing is truncated."""
    for d in CANDIDATE_DLL_DIRS:
        try:
            entries = _gh_json(f"https://api.github.com/repos/{repo}"
                               f"/contents/{d}?ref={branch}")
        except Exception:
            continue
        if isinstance(entries, list):
            for e in entries:
                if e.get("name", "").lower() == fname:
                    return e["path"]
    return None


def _try_release_zip(repo, fname, task_id):
    """Last resort: pull the newest release asset zip and extract the DLL."""
    import zipfile
    import io
    rel = _gh_json(f"https://api.github.com/repos/{repo}/releases/latest")
    assets = rel.get("assets") or []
    assets.sort(key=lambda a: a.get("size", 0))     # smallest plausible first
    for a in assets:
        if not a.get("name", "").lower().endswith(".zip"):
            continue
        if a.get("size", 0) > 800 * 1024 * 1024:
            continue
        TASKS[task_id]["detail"] = f"Downloading release {a['name']}"
        data = _gh_bytes(a["browser_download_url"], task_id)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = [m for m in z.namelist()
                           if m.lower().endswith(fname)]
                members.sort(key=lambda m: ("rel" not in m.lower(),
                                            "dev" in m.lower(), len(m)))
                if members:
                    return z.read(members[0])
        except zipfile.BadZipFile:
            continue
    return None



DLSS_MANIFEST_URL = ("https://raw.githubusercontent.com/beeradmoore/"
                     "dlss-swapper-manifest-builder/refs/heads/main/manifest.json")

# Section names inside the manifest, verified from DLSS Swapper's wiki/source:
#   dlss = Super Resolution, dlss_g = Frame Generation, dlss_d = Ray Reconstruction
DLSS_MANIFEST_SECTION = {"sr": "dlss", "fg": "dlss_g", "rr": "dlss_d"}


def _manifest_latest(kind, task_id):
    """Fetch DLSS Swapper's manifest (the same one that tool refreshes every
    launch) and return (version, dll_bytes) for the newest STABLE entry of the
    requested kind. Covers SR/FG/RR — this is how the latest DLSS 4.x DLLs are
    found. Returns None on any failure so callers fall back to NVIDIA repos."""
    import zipfile, io
    section = DLSS_MANIFEST_SECTION.get(kind)
    if not section:
        return None
    try:
        manifest = _gh_json(DLSS_MANIFEST_URL)
    except Exception:
        return None
    entries = manifest.get(section) if isinstance(manifest, dict) else None
    if not entries:
        return None
    # entries carry a version_number (packed 64-bit) or a dotted version string
    def _key(e):
        vn = e.get("version_number")
        if isinstance(vn, int):
            return vn
        return version_tuple(e.get("version", "0"))
    best = max(entries, key=_key)
    dl = best.get("download_url")
    if not dl:
        return None
    TASKS[task_id]["detail"] = f"Manifest has {best.get('version')}, downloading"
    data = _gh_bytes(dl, task_id)
    fname = KIND_TO_NAME.get(kind)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [m for m in z.namelist() if m.lower().endswith(fname)]
            if members:
                return best.get("version"), z.read(members[0])
    except zipfile.BadZipFile:
        pass
    return None


def download_dlss(task_id, kind):
    """Fetch the requested DLL kind from NVIDIA's official repos. Strategy per
    repo: tree search -> directory probe (tree may be truncated) -> release
    zip. Raw downloads resolve Git LFS pointers automatically."""
    dll_name = KIND_TO_NAME.get(kind)
    label = {"sr": "Super Resolution", "fg": "Frame Generation",
             "rr": "Ray Reconstruction"}.get(kind, kind)
    TASKS[task_id] = {"status": "running", "progress": 0,
                      "detail": f"Looking for {label} DLL"}
    errors = []

    # PRIMARY: DLSS Swapper's manifest — refreshed constantly, carries the
    # newest SR/FG/RR versions (this is the fix for "not fetching the latest").
    try:
        TASKS[task_id]["detail"] = "Checking DLSS Swapper manifest"
        got = _manifest_latest(kind, task_id)
        if got:
            version, data = got
            tmp_final = DATA_DIR / dll_name
            try:
                tmp_final.write_bytes(data)
                if data[:2] == b"MZ" and pe_version(tmp_final):
                    info = import_dll(tmp_final)
                    fr = friendly_dlss(info["version"])
                    TASKS[task_id] = {"status": "done", "progress": 100,
                                      "detail": f"Added {fr['gen']} {label} "
                                                f"{fr['short']}"}
                    return
            finally:
                tmp_final.unlink(missing_ok=True)
    except Exception as e:
        errors.append(f"manifest: {e}")
    for repo, fname in DLL_SOURCES.get(kind, []):
        try:
            TASKS[task_id]["detail"] = f"Searching {repo}"
            branch = _gh_json(f"https://api.github.com/repos/{repo}")\
                .get("default_branch", "main")
            data = None
            hits, truncated = [], False
            try:
                hits, truncated = _find_in_tree(repo, branch, fname)
            except Exception:
                truncated = True
            path = hits[0] if hits else None
            if not path and truncated:
                TASKS[task_id]["detail"] = f"{repo}: large repo, probing folders"
                path = _probe_dirs(repo, branch, fname)
            if path:
                TASKS[task_id]["detail"] = f"Downloading {fname} from {repo}"
                data = _gh_bytes(f"https://raw.githubusercontent.com/"
                                 f"{repo}/{branch}/{path}", task_id)
                data = _resolve_lfs(repo, branch, path, data, task_id)
            if data is None:
                TASKS[task_id]["detail"] = f"{repo}: checking release assets"
                data = _try_release_zip(repo, fname, task_id)
            if data is None:
                errors.append(f"{repo}: {fname} not found")
                continue
            tmp_final = DATA_DIR / fname
            info = None
            try:
                tmp_final.write_bytes(data)
                if data[:2] != b"MZ" or not pe_version(tmp_final):
                    raise RuntimeError("downloaded file isn't a valid DLL")
                info = import_dll(tmp_final)
            finally:
                tmp_final.unlink(missing_ok=True)
            fr = friendly_dlss(info["version"])
            TASKS[task_id] = {"status": "done", "progress": 100,
                              "detail": f"Added {fr['gen']} {label} "
                                        f"{fr['short']} to library"}
            return
        except Exception as e:
            errors.append(f"{repo}: {e}")
    TASKS[task_id] = {
        "status": "error", "progress": 0,
        "detail": ("Couldn't fetch the DLL ("
                   + "; ".join(errors[:2]) + "). You can still download it "
                   "manually (e.g. TechPowerUp) and import it below.")}


def download_latest_sr(task_id):  # kept for compatibility
    download_dlss(task_id, "sr")


def swap_dll(game_dll_path, library_dll_path):
    game_dll = Path(game_dll_path)
    lib_dll = Path(library_dll_path)
    if not game_dll.is_file():
        raise RuntimeError(f"Game DLL missing: {game_dll}")
    if not lib_dll.is_file():
        raise RuntimeError(f"Library DLL missing: {lib_dll}")
    if game_dll.name.lower() != lib_dll.name.lower():
        raise RuntimeError("DLL type mismatch — refusing to swap different DLSS components")
    bak = _backup_path(game_dll)
    if not bak.exists():
        shutil.copy2(game_dll, bak)
    shutil.copy2(lib_dll, game_dll)
    return {"swapped": True, "new_version": pe_version(game_dll), "backup": str(bak)}


def restore_dll(game_dll_path):
    game_dll = Path(game_dll_path)
    bak = _backup_path(game_dll)
    if not bak.exists():
        raise RuntimeError("No backup exists for this DLL")
    shutil.copy2(bak, game_dll)
    return {"restored": True, "version": pe_version(game_dll)}


# --------------------------------------------------------------------------
# Owned library (community profile XML — no API key needed)
# --------------------------------------------------------------------------

def get_steamid64(root):
    """Most recent login's SteamID64 from config/loginusers.vdf."""
    lu = root / "config/loginusers.vdf"
    if not lu.is_file():
        return None
    try:
        data = vdf_parse(lu.read_text(errors="replace"))
    except Exception:
        return None
    users = ci_get(data, "users") or {}
    best, best_ts = None, -1
    for sid, meta in users.items():
        if not isinstance(meta, dict):
            continue
        if ci_get(meta, "MostRecent") == "1":
            return sid
        ts = int(ci_get(meta, "Timestamp") or 0)
        if ts > best_ts:
            best, best_ts = sid, ts
    return best


def fetch_owned_games(root, force=False):
    """All games the user owns, via the public community profile XML.
    Cached in state.json for 6 hours. Returns {'games': [...], 'error': str|None}."""
    state = load_state()
    cache = state.get("owned", {})
    if not force and cache.get("games") and \
            time.time() - cache.get("ts", 0) < 6 * 3600:
        return {"games": cache["games"], "error": None, "cached": True}
    sid = get_steamid64(root)
    if not sid:
        return {"games": cache.get("games", []),
                "error": "Couldn't find a Steam login in loginusers.vdf"}
    url = f"https://steamcommunity.com/profiles/{sid}/games?tab=all&xml=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "proton-command-center"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        tree = ET.fromstring(raw)
        if tree.find("error") is not None:
            return {"games": cache.get("games", []),
                    "error": tree.findtext("error")}
        games = []
        for g in tree.iter("game"):
            appid = g.findtext("appID")
            name = g.findtext("name")
            if appid and name:
                games.append({"appid": appid, "name": name})
        if not games:
            return {"games": cache.get("games", []),
                    "error": "Profile returned no games — set 'Game details' "
                             "to Public in Steam privacy settings"}
        state["owned"] = {"ts": int(time.time()), "games": games}
        save_state(state)
        return {"games": games, "error": None}
    except Exception as e:
        return {"games": cache.get("games", []),
                "error": f"Couldn't reach steamcommunity.com: {e}"}



def install_progress(root):
    """Cheap poll: manifest-only install state for every game."""
    out = []
    for g in list_games(root):
        out.append({
            "appid": g["appid"],
            "name": g["name"],
            "installed": g["installed"],
            "fully_installed": g["fully_installed"],
            "download_pct": g["download_pct"],
            "size_bytes": g["size_bytes"],
        })
    return out


def install_game(appid):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    return _spawn_detached([exe, f"steam://install/{appid}"])



# --------------------------------------------------------------------------
# Auto-tune: engine detection + curated tuning rules
# --------------------------------------------------------------------------
def detect_engine(install_path):
    """Identify the game engine from on-disk markers. Fast: bounded walk."""
    base = Path(install_path)
    ev, engine, dx12 = [], None, None
    if not base.is_dir():
        return {"engine": None, "dx12": None, "evidence": ["install dir missing"]}
    names, exes = set(), []
    ucas = pak = False
    for i, (dirpath, dirnames, filenames) in enumerate(os.walk(base)):
        if i > 400:
            break
        depth = len(Path(dirpath).relative_to(base).parts)
        if depth > 4:
            dirnames[:] = []
            continue
        for d in dirnames:
            names.add(d.lower())
        for f in filenames:
            fl = f.lower()
            names.add(fl)
            if fl.endswith(".exe"):
                exes.append(Path(dirpath) / f)
            if fl.endswith((".ucas", ".utoc")):
                ucas = True
            if fl.endswith(".pak"):
                pak = True
            if fl.startswith("re_chunk"):
                engine, _ = "re-engine", ev.append(f"{f} (RE Engine chunk)")
    if any(n.endswith("_data") for n in names) and "unityplayer.dll" in names:
        engine = "unity"
        ev.append("UnityPlayer.dll + *_Data folder")
    if "engine" in names and pak:
        engine = "unreal5" if ucas else "unreal4"
        ev.append("Engine/ + .pak" + (" + IoStore .ucas/.utoc (UE5-style)" if ucas else ""))
    if any(n.endswith("-win64-shipping.exe") for n in names) and not engine:
        engine = "unreal4"
        ev.append("*-Win64-Shipping.exe")
    if "gameinfo.gi" in names:
        engine, _ = "source2", ev.append("gameinfo.gi")
    elif "gameinfo.txt" in names:
        engine, _ = "source", ev.append("gameinfo.txt")
    if any(n.endswith(".pck") for n in names) and not engine:
        engine, _ = "godot", ev.append(".pck archive")
    if "data.win" in names and not engine:
        engine, _ = "gamemaker", ev.append("data.win")
    # DX12 vs DX11: scan the biggest exe for imported runtime names
    exes.sort(key=lambda e: e.stat().st_size, reverse=True)
    for exe in exes[:2]:
        try:
            blob = exe.read_bytes()[:12_000_000]
        except OSError:
            continue
        has12 = b"d3d12.dll" in blob or b"D3D12" in blob
        has11 = b"d3d11.dll" in blob or b"D3D11" in blob
        if has12:
            dx12 = True
            ev.append(f"{exe.name}: references D3D12")
            break
        if has11:
            dx12 = False
            ev.append(f"{exe.name}: references D3D11")
    return {"engine": engine, "dx12": dx12, "evidence": ev}


def gpu_vram_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return int(out.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


DEFAULT_TUNING_RULES = {
    "base_env": {"PROTON_ENABLE_NVAPI": "1", "PROTON_USE_NTSYNC": "1"},
    "base_wrappers": ["game-performance"],
    "vram_cap_below_mb": 12000,
    "engines": {
        "unreal4": {
            "env": {},
            "notes": ["UE4: PSO stutter is the usual hitching cause — precompile "
                      "shaders in the Cache tab before judging performance",
                      "If hitching persists, cap FPS a few frames below refresh "
                      "(frame pacing) and test with Frame Generation OFF"],
        },
        "unreal5": {
            "env": {},
            "notes": ["UE5: run shader precompile first — biggest single fix "
                      "for periodic hitching",
                      "Frame Generation can worsen UE5 frame pacing — A/B test "
                      "with the Benchmark tab",
                      "If traversal stutter remains, an fps cap (DXVK_FRAME_RATE) "
                      "slightly under refresh smooths delivery"],
        },
        "unity": {"env": {}, "notes": ["Unity titles are usually clean under "
                                       "Proton — Wayland + HDR safe to enable"]},
        "re-engine": {"env": {}, "notes": ["RE Engine runs well by default; "
                                           "enable HDR if your display supports it"]},
        "source": {"env": {}, "notes": []},
        "source2": {"env": {}, "notes": []},
        "godot": {"env": {}, "notes": []},
        "gamemaker": {"env": {}, "notes": []},
    },
    "name_overrides": [
        {"match": "stellar blade",
         "env": {"PROTON_ENABLE_HDR": "1", "DXVK_HDR": "1"},
         "desktop_env": {"SteamDeck": "0"},
         "vram_cap": True,
         "notes": ["Known VRAM-pressure title: memory cap applied to prevent "
                   "eviction hitching",
                   "Engine.ini pool-size tweaks help further (r.Streaming settings)"]},
        {"match": "mortal shell",
         "env": {},
         "vram_cap": True,
         "fps_cap_hint": True,
         "notes": ["Rhythmic hitching profile: precompile shaders, test with "
                   "FG off, VRAM cap applied",
                   "If hitching survives all three, capture a Benchmark run and "
                   "check stutter % before/after each change"]},
    ],
}


def load_tuning_rules():
    custom = DATA_DIR / "tuning_rules.json"
    if custom.is_file():
        try:
            return json.loads(custom.read_text())
        except Exception:
            pass
    return DEFAULT_TUNING_RULES




HANDHELD_DMI = ("jupiter", "galileo", "rog ally", "legion go", "ayaneo",
                "gpd win", "onexplayer", "steam deck")


def is_handheld():
    """Detect Steam Deck / handheld PCs via DMI so desktop-only fixes
    (like SteamDeck=0) are never applied on actual handhelds."""
    for f in ("/sys/devices/virtual/dmi/id/product_name",
              "/sys/devices/virtual/dmi/id/board_name"):
        try:
            name = Path(f).read_text().strip().lower()
            if any(h in name for h in HANDHELD_DMI):
                return True
        except OSError:
            continue
    return os.environ.get("SteamDeck") == "1"


def deck_verified(appid):
    """Valve's own Deck compatibility verdict — professionally tested,
    the highest-trust community-adjacent source. Cached 7 days."""
    state = load_state()
    cache = state.get("deckverified", {}).get(str(appid))
    if cache and time.time() - cache.get("ts", 0) < 7 * 86400:
        return cache.get("data")
    out = None
    try:
        req = urllib.request.Request(
            "https://store.steampowered.com/saleaction/"
            f"ajaxgetdeckappcompatibilityreport?nAppID={appid}&l=english",
            headers={"User-Agent": "proton-command-center"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        results = (data.get("results") or {})
        code = results.get("resolved_category")
        out = {"category": {3: "Verified", 2: "Playable",
                            1: "Unsupported", 0: "Unknown"}.get(code, "Unknown"),
               "code": code}
    except Exception:
        out = None
    state.setdefault("deckverified", {})[str(appid)] = {"ts": time.time(), "data": out}
    save_state(state)
    return out


def protondb_cached(appid):
    """Return a previously-fetched ProtonDB rating from state without ever
    hitting the network. Used to repopulate the badge when the app reopens, so
    a rating the user already checked stays visible. Returns None if never
    checked."""
    state = load_state()
    cache = state.get("protondb", {}).get(str(appid))
    if cache:
        return cache.get("data")
    return None


def protondb_summary(appid):
    """Community compatibility tier from ProtonDB (cached 24h)."""
    state = load_state()
    cache = state.get("protondb", {}).get(str(appid))
    if cache and time.time() - cache.get("ts", 0) < 86400:
        return cache.get("data")
    try:
        req = urllib.request.Request(
            f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json",
            headers={"User-Agent": "proton-command-center"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        out = {"tier": data.get("tier"), "confidence": data.get("confidence"),
               "total": data.get("total")}
    except Exception:
        out = None
    state.setdefault("protondb", {})[str(appid)] = {"ts": time.time(), "data": out}
    save_state(state)
    return out


def umu_fix_info(appid):
    """Does umu-protonfixes (the fix database GE-Proton applies automatically)
    have a fix script for this appid? Returns a summary if so. Cached 24h."""
    state = load_state()
    cache = state.get("umufix", {}).get(str(appid))
    if cache and time.time() - cache.get("ts", 0) < 86400:
        return cache.get("data")
    out = None
    try:
        url = ("https://raw.githubusercontent.com/Open-Wine-Components/"
               f"umu-protonfixes/main/gamefixes-steam/{appid}.py")
        req = urllib.request.Request(url, headers={"User-Agent": "pcc"})
        with urllib.request.urlopen(req, timeout=15) as r:
            src = r.read().decode(errors="replace")
        doc = ""
        m = re.search(r'"""(.*?)"""', src, re.S)
        if m:
            doc = " ".join(m.group(1).split())[:200]
        actions = []
        for verb in re.findall(r"util\.protontricks\(['\"](\w+)['\"]\)", src):
            actions.append(f"winetricks {verb}")
        for k, v in re.findall(r"util\.set_environment\(['\"]([\w_]+)['\"],\s*['\"]([^'\"]*)['\"]", src):
            actions.append(f"env {k}={v}")
        out = {"exists": True, "summary": doc, "actions": actions[:6],
               "url": f"https://github.com/Open-Wine-Components/umu-protonfixes/blob/main/gamefixes-steam/{appid}.py"}
    except urllib.error.HTTPError:
        out = {"exists": False}
    except Exception:
        out = None
    state.setdefault("umufix", {})[str(appid)] = {"ts": time.time(), "data": out}
    save_state(state)
    return out


def auto_tune(root, appid):
    games = {g["appid"]: g for g in list_games(root)}
    g = games.get(str(appid))
    if not g:
        raise RuntimeError("game not found")
    rules = load_tuning_rules()
    det = detect_engine(g["install_path"])
    env = dict(rules.get("base_env", {}))
    wrappers = list(rules.get("base_wrappers", []))
    notes = []
    reasons = [f"Engine: {det['engine'] or 'unknown'}"
               + (f" ({'DX12' if det['dx12'] else 'DX11'})" if det["dx12"] is not None else "")]
    eng = rules.get("engines", {}).get(det["engine"] or "", {})
    env.update(eng.get("env", {}))
    notes += eng.get("notes", [])
    vram = gpu_vram_mb()
    cap_applied = False
    name = g["name"].lower()
    handheld = is_handheld()
    for ov in rules.get("name_overrides", []):
        if ov["match"] in name:
            env.update(ov.get("env", {}))
            if ov.get("desktop_env") and not handheld:
                env.update(ov["desktop_env"])
                reasons.append("Desktop detected — anti-handheld-preset flags "
                               "applied (" + " ".join(f"{k}={v}" for k, v
                               in ov["desktop_env"].items()) + ")")
            notes += ov.get("notes", [])
            reasons.append(f"Known-game profile matched: '{ov['match']}'")
            if ov.get("vram_cap") and vram and vram <= rules.get("vram_cap_below_mb", 12000):
                env["DXVK_CONFIG"] = f"dxgi.maxDeviceMemory={max(2048, vram - 1024)}"
                cap_applied = True
    if not cap_applied and vram and vram <= 8500:
        env["DXVK_CONFIG"] = f"dxgi.maxDeviceMemory={max(2048, vram - 1024)}"
        reasons.append(f"{vram} MB VRAM detected — headroom cap applied to "
                       "prevent eviction stutter")
    pdb = protondb_summary(appid)
    umu = umu_fix_info(appid)
    deck = deck_verified(appid)
    if deck and pdb and pdb.get("tier"):
        good_pdb = pdb["tier"] in ("platinum", "gold")
        good_deck = deck.get("code") in (2, 3)
        if good_pdb == good_deck:
            reasons.append(f"Sources agree: ProtonDB {pdb['tier']} + "
                           f"Valve Deck report '{deck['category']}' — "
                           "community data corroborated")
        else:
            reasons.append(f"Sources disagree: ProtonDB says {pdb['tier']} "
                           f"but Valve's Deck report says {deck['category']} — "
                           "treat community launch-option tips with caution")
    if umu and umu.get("exists"):
        reasons.append("Community fix exists in umu-protonfixes — select "
                       "GE-Proton in the compatibility dropdown and it applies "
                       "automatically")
    if pdb and pdb.get("tier"):
        reasons.append(f"ProtonDB: {pdb['tier']} "
                       f"({pdb.get('total', '?')} reports)")
    parts = [f"{k}={v}" for k, v in env.items()] + wrappers + ["%command%"]
    return {
        "detection": det,
        "protondb": pdb,
        "umu_fix": umu,
        "deck_verified": deck,
        "handheld": handheld,
        "launch_string": " ".join(parts),
        "reasons": reasons + det["evidence"],
        "notes": notes,
        "vram_mb": vram,
        "precompile_recommended": (det["engine"] or "").startswith("unreal"),
    }


# --------------------------------------------------------------------------
# Compatibility tools (Proton version per game)
# --------------------------------------------------------------------------
OFFICIAL_COMPAT_TOOLS = [
    ("", "Steam default"),
    ("proton_experimental", "Proton Experimental"),
    ("proton_hotfix", "Proton Hotfix"),
    ("proton_9", "Proton 9.0"),
    ("proton_10", "Proton 10.0"),
]


def list_compat_tools(root):
    """Official Protons plus everything in every compatibilitytools.d that
    Steam scans — including system packages (CachyOS installs proton-cachyos
    to /usr/share/steam/compatibilitytools.d) and STEAM_EXTRA_COMPAT_TOOLS_PATHS."""
    tools = [{"name": n, "label": l, "custom": False}
             for n, l in OFFICIAL_COMPAT_TOOLS]
    seen = {t["name"] for t in tools}
    dirs = [
        root / "compatibilitytools.d",
        Path.home() / ".steam/root/compatibilitytools.d",
        Path("/usr/share/steam/compatibilitytools.d"),
        Path("/usr/local/share/steam/compatibilitytools.d"),
    ]
    for extra in os.environ.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "").split(":"):
        if extra:
            dirs.append(Path(extra))
    scanned = set()
    for d in dirs:
        try:
            rd = d.resolve()
        except OSError:
            continue
        if rd in scanned or not d.is_dir():
            continue
        scanned.add(rd)
        for tool_dir in sorted(d.iterdir()):
            vdf = tool_dir / "compatibilitytool.vdf"
            if not vdf.is_file():
                continue
            try:
                data = vdf_parse(vdf.read_text(errors="replace"))
                compat = ci_get(data, "compatibilitytools") or {}
                compat = ci_get(compat, "compat_tools") or {}
                for internal, meta in compat.items():
                    if internal not in seen:
                        seen.add(internal)
                        label = meta.get("display_name", internal) \
                            if isinstance(meta, dict) else internal
                        tools.append({"name": internal, "label": label,
                                      "custom": True})
            except Exception:
                continue
    return tools


def _config_vdf(root):
    return root / "config/config.vdf"


def _compat_mapping_node(data, create=False):
    store = (ci_ensure(data, "InstallConfigStore") if create
             else ci_get(data, "InstallConfigStore"))
    if store is None:
        return None
    sw = ci_ensure(store, "Software") if create else ci_get(store, "Software")
    if sw is None:
        return None
    valve = ci_ensure(sw, "Valve") if create else ci_get(sw, "Valve")
    if valve is None:
        return None
    steam = ci_ensure(valve, "Steam") if create else ci_get(valve, "Steam")
    if steam is None:
        return None
    return (ci_ensure(steam, "CompatToolMapping") if create
            else ci_get(steam, "CompatToolMapping"))


def get_compat_tool(root, appid):
    cfg = _config_vdf(root)
    if not cfg.is_file():
        return {"name": "", "source": None}
    try:
        data = vdf_parse(cfg.read_text(errors="replace"))
    except Exception:
        return {"name": "", "source": None}
    mapping = _compat_mapping_node(data) or {}
    entry = ci_get(mapping, str(appid))
    if isinstance(entry, dict):
        return {"name": entry.get("name", ""), "source": str(cfg)}
    return {"name": "", "source": str(cfg)}


def set_compat_tool(root, appid, tool_name, close_steam=False):
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running. Close Steam first — it "
                               "overwrites config.vdf on exit.")
    cfg = _config_vdf(root)
    if not cfg.is_file():
        raise RuntimeError(f"config.vdf not found at {cfg}")
    data = vdf_parse(cfg.read_text(errors="replace"))
    mapping = _compat_mapping_node(data, create=True)
    for k in list(mapping.keys()):          # drop existing entry, any case
        if k == str(appid):
            del mapping[k]
    if tool_name:
        mapping[str(appid)] = {"name": tool_name, "config": "",
                               "priority": "250"}
    bak = cfg.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(cfg, bak)
    tmp = cfg.with_suffix(".vdf.pcc-tmp")
    tmp.write_text(vdf_dump(data))
    tmp.replace(cfg)
    return {"saved": True, "tool": tool_name, "backup": str(bak)}



# --------------------------------------------------------------------------
# Full library (owned games) via Steam Web API
# --------------------------------------------------------------------------
def steamid64(root):
    """Most recent login from loginusers.vdf; falls back to config.vdf."""
    lu = root / "config/loginusers.vdf"
    try:
        data = vdf_parse(lu.read_text(errors="replace"))
        users = ci_get(data, "users") or {}
        best = None
        for sid, meta in users.items():
            if not sid.isdigit():
                continue
            if isinstance(meta, dict) and meta.get("MostRecent") == "1":
                return sid
            best = best or sid
        if best:
            return best
    except Exception:
        pass
    return None


def owned_games(root, force=False):
    key = load_config().get("steam_api_key", "").strip()
    if not key:
        raise RuntimeError("No Steam Web API key set — add one in settings "
                           "(free at steamcommunity.com/dev/apikey)")
    sid = steamid64(root)
    if not sid:
        raise RuntimeError("Couldn't detect your SteamID64 from loginusers.vdf")
    state = load_state()
    cache = state.get("owned_cache", {})
    if not force and cache.get("sid") == sid             and time.time() - cache.get("ts", 0) < 3600:
        return cache["games"]
    url = ("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
           f"?key={urllib.parse.quote(key)}&steamid={sid}"
           "&include_appinfo=1&include_played_free_games=1")
    req = urllib.request.Request(url, headers={"User-Agent": "pcc"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    apps = (data.get("response") or {}).get("games") or []
    out = [{"appid": str(a["appid"]), "name": a.get("name") or str(a["appid"])}
           for a in apps]
    out.sort(key=lambda g: g["name"].lower())
    state["owned_cache"] = {"sid": sid, "ts": time.time(), "games": out}
    save_state(state)
    return out



def install_progress(root):
    """Cheap poll: manifest-only install state for every game."""
    out = []
    for g in list_games(root):
        out.append({
            "appid": g["appid"],
            "name": g["name"],
            "installed": g["installed"],
            "fully_installed": g["fully_installed"],
            "download_pct": g["download_pct"],
            "size_bytes": g["size_bytes"],
        })
    return out


def install_game(appid):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    return _spawn_detached([exe, f"steam://install/{appid}"])


# --------------------------------------------------------------------------
# MangoHud benchmarks (ported from Stutterless)
# --------------------------------------------------------------------------
BENCH_DIR = DATA_DIR / "benchmarks"
BENCH_DIR.mkdir(parents=True, exist_ok=True)


def benchmark_launch_string(appid):
    folder = BENCH_DIR / str(appid)
    folder.mkdir(parents=True, exist_ok=True)  # MangoHud won't create it
    cfg = (f"output_folder={folder},autostart_log=1,log_duration=300,"
           f"benchmark_percentiles=AVG+1+0.1")
    return f"MANGOHUD=1 MANGOHUD_CONFIG={cfg} %command%"


def _parse_mangohud_csv(path):
    """MangoHud CSVs have two header sections: a system header (line 1-2)
    then the data-column header containing 'frametime'. Frametime is in
    microseconds; normalise to ms. Falls back to fps-only logs."""
    try:
        with open(path, "r", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return None
    if len(lines) < 4:
        return None
    ft_col, data_start = None, 0
    for i, ln in enumerate(lines):
        if "frametime" in ln.lower():
            cols = [c.strip().lower() for c in ln.split(",")]
            for idx, c in enumerate(cols):
                if c.startswith("frametime"):
                    ft_col = idx
            data_start = i + 1
            break
    frametimes = []

    def push(val):
        if val <= 0:
            return
        if val > 1e5:     # nanoseconds
            ms = val / 1e6
        elif val > 200:   # microseconds
            ms = val / 1e3
        else:             # already ms
            ms = val
        if 0.1 <= ms <= 1000:
            frametimes.append(ms)

    if ft_col is not None:
        for ln in lines[data_start:]:
            parts = ln.split(",")
            if len(parts) > ft_col:
                try:
                    push(float(parts[ft_col]))
                except ValueError:
                    pass
    else:  # very old MangoHud: fps in column 0
        for ln in lines:
            try:
                fps = float(ln.split(",")[0])
            except (ValueError, IndexError):
                continue
            if fps > 0:
                push(1000.0 / fps)
    if len(frametimes) < 20:
        return None
    return frametimes


def _analyse_frametimes(ft):
    n = len(ft)
    if n == 0:
        return None
    s = sorted(ft)
    avg_ft = sum(ft) / n
    k1 = max(1, n // 100)
    k01 = max(1, n // 1000)
    median = s[n // 2]
    stutters = sum(1 for x in ft if x > 2.0 * median)
    return {
        "frames": n,
        "avg_fps": round(1000.0 / avg_ft, 1),
        "low1_fps": round(1000.0 / (sum(s[-k1:]) / k1), 1),
        "low01_fps": round(1000.0 / (sum(s[-k01:]) / k01), 1),
        "stutter_count": stutters,
        "stutter_pct": round(100.0 * stutters / n, 2),
    }


def _downsample(series, target=200):
    """Bucket to ~target points using max() so stutter spikes survive."""
    n = len(series)
    if n <= target:
        return [round(x, 2) for x in series]
    bucket, out, i = n / target, [], 0.0
    while i < n:
        chunk = series[int(i):int(i + bucket) or int(i) + 1]
        if chunk:
            out.append(round(max(chunk), 2))
        i += bucket
    return out


def get_benchmark_data(root, appid):
    folder = BENCH_DIR / str(appid)
    result = {
        "has_mangohud": shutil.which("mangohud") is not None,
        "launch_string": benchmark_launch_string(appid),
        "folder": str(folder),
        "before": None, "after": None,
        "before_graph": None, "after_graph": None,
        "improvement_pct": None, "log_count": 0, "diag": [],
    }
    diag = result["diag"]
    logs = sorted(
        ((p, p.stat().st_mtime) for p in folder.rglob("*.csv")),
        key=lambda x: x[1]) if folder.is_dir() else []
    result["log_count"] = len(logs)
    if not logs:
        diag.append("No MangoHud logs yet — save the benchmark launch options, "
                    "play for a few minutes, and check back.")
        return result
    usable = []
    for p, mt in logs:
        ft = _parse_mangohud_csv(p)
        if ft:
            usable.append((p, mt, ft))
        else:
            diag.append(f"Couldn't parse {p.name} (too short — play longer).")
    if not usable:
        diag.append("Logs found but none had enough frametime data "
                    "(play at least ~30 seconds).")
        return result
    split = load_state().get("compiled", {}).get(str(appid), {}).get("compiled_at", 0)
    before = [u for u in usable if u[1] < split] if split else []
    after = [u for u in usable if u[1] >= split] if split else []
    if (not before or not after) and len(usable) >= 2:
        before, after = [usable[0]], [usable[-1]]
        diag.append("Using oldest log as 'before' and newest as 'after'.")
    elif len(usable) == 1:
        if split and usable[0][1] >= split:
            after = [usable[0]]
            diag.append("Only an 'after' run so far — nothing to compare against.")
        else:
            before = [usable[0]]
            diag.append("Only a 'before' run — compile, play again, then compare.")

    def analyse(u):
        if not u:
            return None, None
        ft = u[-1][2]
        return _analyse_frametimes(ft), _downsample(ft)

    result["before"], result["before_graph"] = analyse(before)
    result["after"], result["after_graph"] = analyse(after)
    if result["before"] and result["after"] and result["before"]["low1_fps"] > 0:
        result["improvement_pct"] = round(
            100.0 * (result["after"]["low1_fps"] - result["before"]["low1_fps"])
            / result["before"]["low1_fps"], 1)
        diag.append("Comparison ready.")
    return result


# --------------------------------------------------------------------------
# Shader cache
# --------------------------------------------------------------------------

def cache_info(root, appid):
    out = []
    for lib in library_folders(root):
        c = lib / "shadercache" / str(appid)
        if c.is_dir():
            size = 0
            files = 0
            for dirpath, _, filenames in os.walk(c):
                for fn in filenames:
                    try:
                        size += (Path(dirpath) / fn).stat().st_size
                        files += 1
                    except OSError:
                        pass
            foz = sorted(str(p) for p in c.rglob("*.foz"))
            out.append({"path": str(c), "size_bytes": size, "files": files, "foz": foz})
    return out


def clear_cache(root, appid, keep_recordings=True):
    """Default clears COMPILED artifacts but preserves fozpipelinesv6/
    recordings — they're the source data for precompiling and costly to
    regenerate. keep_recordings=False deletes everything."""
    cleared, kept = [], 0
    for entry in cache_info(root, appid):
        base = Path(entry["path"])
        if not keep_recordings:
            shutil.rmtree(base, ignore_errors=True)
            cleared.append(str(base))
            continue
        for child in base.iterdir():
            if child.name == "fozpipelinesv6":
                kept += sum(1 for _ in child.rglob("*.foz"))
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            cleared.append(str(child))
    unmark_compiled(appid)
    return {"cleared": cleared, "kept_recordings": kept}


def find_fossilize():
    exe = shutil.which("fossilize_replay") or shutil.which("fossilize-replay")
    if exe:
        return exe
    root = steam_root()
    if root:
        hits = list(root.glob("ubuntu12_64/fossilize_replay")) + \
               list(root.glob("steamapps/common/SteamLinuxRuntime*/**/fossilize_replay"))
        if hits:
            return str(hits[0])
    return None


def precompile_cache(task_id, root, appid, device_index=0):
    TASKS[task_id] = {"status": "running", "progress": 0, "detail": "Locating fossilize_replay"}
    exe = find_fossilize()
    if not exe:
        TASKS[task_id] = {"status": "error", "progress": 0,
                          "detail": "fossilize_replay not found (install fossilize or run once from Steam)"}
        return
    foz_files = find_foz(root, appid)
    if not foz_files:
        TASKS[task_id] = {"status": "error", "progress": 0,
                          "detail": "No .foz pipeline files found — launch the game once so Steam collects them"}
        return
    rcache = find_replayer_cache(root, appid)
    done = 0
    for foz in foz_files:
        TASKS[task_id]["detail"] = f"Replaying {Path(foz).name}"
        cmd = [exe, "--device-index", str(device_index),
               "--num-threads", str(max(1, (os.cpu_count() or 4) - 2))]
        if rcache:
            cmd += ["--replayer-cache", rcache]
        cmd.append(foz)
        try:
            subprocess.run(cmd, capture_output=True, timeout=3600)
        except Exception as e:
            TASKS[task_id] = {"status": "error", "progress": 0, "detail": f"{Path(foz).name}: {e}"}
            return
        done += 1
        TASKS[task_id]["progress"] = int(done / len(foz_files) * 100)
    mark_compiled(root, appid)
    TASKS[task_id] = {"status": "done", "progress": 100,
                      "detail": f"Replayed {done} pipeline database(s)"
                                + (" — results recorded in Steam's cache, so "
                                   "Steam should skip its own processing pass"
                                   if rcache else
                                   " — note: Steam hasn't created its "
                                   "pipeline ledger for this game yet; its "
                                   "first processing pass may still run")}


def precompile_all(task_id, root, device_index=0, skip_compiled=True):
    TASKS[task_id] = {"status": "running", "progress": 0, "detail": "Scanning library"}
    exe = find_fossilize()
    if not exe:
        TASKS[task_id] = {"status": "error", "progress": 0,
                          "detail": "fossilize_replay not found"}
        return
    state = load_state()
    drv = driver_version()
    todo = []
    for g in list_games(root):
        foz = find_foz(root, g["appid"])
        if not foz:
            continue
        if skip_compiled:
            st = compiled_status(root, g["appid"], state, drv)
            if st["compiled"] and not st.get("outdated"):
                continue
        todo.append((g, foz))
    if not todo:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": "Everything already compiled — nothing to do"}
        return
    threads = str(max(1, (os.cpu_count() or 4) - 2))
    total_files = sum(len(f) for _, f in todo)
    done_files = 0
    skipped = []
    for gi, (g, foz_files) in enumerate(todo):
        rcache = find_replayer_cache(root, g["appid"])
        game_ok = True
        for foz in foz_files:
            done_files += 1
            TASKS[task_id]["detail"] = (f'{g["name"]} — {Path(foz).name} '
                                        f'({done_files}/{total_files})')
            TASKS[task_id]["progress"] = int(done_files / total_files * 100)
            cmd = [exe, "--device-index", str(device_index),
                   "--num-threads", threads]
            if rcache:
                cmd += ["--replayer-cache", rcache]
            cmd.append(foz)
            try:
                # Per-file cap of 10 min. A stuck/huge foz no longer freezes the
                # whole batch — it's skipped and the run continues.
                subprocess.run(cmd, capture_output=True, timeout=600)
            except subprocess.TimeoutExpired:
                game_ok = False
                skipped.append(g["name"])
                TASKS[task_id]["detail"] = (f'{g["name"]} took too long — '
                                            f'skipped, continuing')
                continue
            except Exception:
                # a single bad file shouldn't kill the whole library run
                game_ok = False
                skipped.append(g["name"])
                continue
        if game_ok:
            mark_compiled(root, g["appid"])
    msg = f"Compiled {len(todo) - len(set(skipped))} game(s)"
    if skipped:
        uniq = list(dict.fromkeys(skipped))
        msg += f"; skipped {len(uniq)} (took too long): {', '.join(uniq[:3])}"
        if len(uniq) > 3:
            msg += f" +{len(uniq) - 3} more"
    TASKS[task_id] = {"status": "done", "progress": 100, "detail": msg}



# --------------------------------------------------------------------------
# Steam's own shader processing ("Processing Vulkan shaders" at launch)
# --------------------------------------------------------------------------
SHADER_KEY_RE = re.compile(r"shader|fossilize|precach", re.I)


def _walk_vdf(node, prefix=()):
    for k, v in (node or {}).items():
        if isinstance(v, dict):
            yield from _walk_vdf(v, prefix + (k,))
        else:
            yield prefix + (k,), v


def steam_shader_settings(root):
    """Find every shader-related key Steam has written, across its configs.
    Steam only persists these once you've touched them, so an empty result
    means 'still at defaults'."""
    out = []
    candidates = [root / "config/config.vdf"] + list(find_localconfigs(root))
    for cfg in candidates:
        if not cfg.is_file():
            continue
        try:
            data = vdf_parse(cfg.read_text(errors="replace"))
        except Exception:
            continue
        keys = [{"path": "/".join(kp), "key": kp[-1], "value": val}
                for kp, val in _walk_vdf(data)
                if SHADER_KEY_RE.search(kp[-1])]
        if keys:
            out.append({"file": str(cfg), "keys": keys})
    return {"files": out, "found": bool(out)}


def set_steam_shader_setting(root, file, path, value, close_steam=False):
    cfg = Path(file)
    if cfg.name not in ("config.vdf", "localconfig.vdf") or not cfg.is_file():
        raise RuntimeError("refusing to write an unexpected file")
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running — it overwrites its configs "
                               "on exit. Close it first.")
    data = vdf_parse(cfg.read_text(errors="replace"))
    parts = path.split("/")
    node = data
    for k in parts[:-1]:
        nxt = ci_get(node, k)
        if not isinstance(nxt, dict):
            raise RuntimeError(f"key path not found: {path}")
        node = nxt
    for k in list(node.keys()):
        if k.lower() == parts[-1].lower():
            node[k] = str(value)
            break
    else:
        raise RuntimeError(f"key not found: {path}")
    bak = cfg.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(cfg, bak)
    tmp = cfg.with_suffix(".vdf.pcc-tmp")
    tmp.write_text(vdf_dump(data))
    tmp.replace(cfg)
    return {"saved": True, "path": path, "value": str(value), "backup": str(bak)}



# --------------------------------------------------------------------------
# Hardware detection + MangoHud configuration
# --------------------------------------------------------------------------
MANGOHUD_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                   str(Path.home() / ".config"))) / "MangoHud"

FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
    "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/TTF/FiraCode-Regular.ttf",
    "/usr/share/fonts/TTF/Hack-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def find_font():
    for f in FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    for root_dir in ("/usr/share/fonts",):
        base = Path(root_dir)
        if base.is_dir():
            for p in base.rglob("*Mono*.ttf"):
                return str(p)
    return None


def _short_gpu_name(name):
    """Turn a full GPU string into a compact label for the overlay.
    'NVIDIA GeForce RTX 5070 Laptop GPU' -> 'RTX 5070'
    'AMD Radeon 860M Graphics'          -> 'Radeon 860M'."""
    if not name:
        return "GPU"
    n = name.strip()
    # NVIDIA: 'RTX/GTX <number>' plus optional 'Ti' and/or 'Super'
    m = re.search(r"\b(RTX|GTX)\s*(\d{3,4})\s*(Ti)?\s*(Super)?", n, re.I)
    if m and m.group(2):
        parts = [m.group(1).upper(), m.group(2)]
        if m.group(3):
            parts.append("Ti")
        if m.group(4):
            parts.append("Super")
        return " ".join(parts)
    m = re.search(r"Radeon\s+([A-Z]*\s*\d{3,4}\s*[A-Z]{0,2})", n, re.I)
    if m:
        return f"Radeon {m.group(1).strip()}"
    m = re.search(r"\bArc\s+([A-Z]?\d{3,4})", n, re.I)
    if m:
        return f"Arc {m.group(1)}"
    for junk in ("NVIDIA", "GeForce", "AMD", "Radeon", "Intel", "Graphics",
                 "Laptop", "GPU", "(R)", "(TM)"):
        n = re.sub(rf"\b{re.escape(junk)}\b", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n[:18] or "GPU"


def _short_cpu_name(name):
    """Compact CPU label. 'AMD Ryzen AI 9 365 w/ Radeon...' -> 'Ryzen AI 9 365'.
    'AMD Ryzen 7 7800X3D 8-Core...'     -> 'Ryzen 7 7800X3D'.
    '13th Gen Intel Core i7-13700K'     -> 'Core i7-13700K'."""
    if not name:
        return "CPU"
    # strip trademark markers up front so they don't break matching
    n = re.sub(r"\((?:R|TM)\)", "", name, flags=re.I).strip()
    # AMD Ryzen (incl. 'Ryzen AI 9 365')
    m = re.search(r"Ryzen\s+(AI\s+)?(\d+)\s+([\w-]+)", n, re.I)
    if m:
        ai = "AI " if m.group(1) else ""
        return f"Ryzen {ai}{m.group(2)} {m.group(3)}"
    # Intel Core i3/i5/i7/i9 and Core Ultra
    m = re.search(r"Core\s+(i[3579])-?(\w+)?", n, re.I)
    if m:
        suffix = f"-{m.group(2)}" if m.group(2) else ""
        return f"Core {m.group(1)}{suffix}"
    m = re.search(r"Core\s+Ultra\s+(\d)\s*(\w+)?", n, re.I)
    if m:
        suffix = f" {m.group(2)}" if m.group(2) else ""
        return f"Core Ultra {m.group(1)}{suffix}"
    # fallback: strip vendor/marketing, cut at 'with'/'w/'
    n = re.split(r"\bw(?:ith|/)\b", n, flags=re.I)[0]
    for junk in ("AMD", "Intel", "Processor", "CPU", "Gen"):
        n = re.sub(rf"\b{re.escape(junk)}\b", "", n, flags=re.I)
    n = re.sub(r"\d+(?:th|st|nd|rd)?\s*-?\s*Core.*", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n[:20] or "CPU"


def cpu_name():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "Unknown CPU"


def _nvidia_gpus():
    out = []
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,pci.bus_id,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=6)
        if r.returncode != 0:
            return out
        for line in r.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 2:
                continue
            name, bus = parts[0], parts[1]
            # 00000000:01:00.0 -> 0000:01:00.0 (MangoHud pci_dev format)
            m = re.search(r"([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):"
                          r"([0-9a-fA-F]{2})\.(\d)", bus)
            pci = f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}".lower() \
                if m else None
            out.append({"name": name, "vendor": "NVIDIA", "pci_dev": pci,
                        "vram_mb": int(parts[2].split()[0]) if len(parts) > 2
                                   and parts[2].split()[0].isdigit() else None,
                        "driver": parts[3] if len(parts) > 3 else None,
                        "discrete": True})
    except Exception:
        pass
    return out


def _drm_gpus():
    """AMD/Intel GPUs via sysfs, so iGPUs are named too."""
    out = []
    base = Path("/sys/class/drm")
    if not base.is_dir():
        return out
    seen = set()
    for card in sorted(base.glob("card[0-9]")):
        dev = card / "device"
        try:
            vendor = (dev / "vendor").read_text().strip()
            pci = os.path.basename(os.path.realpath(dev))
        except OSError:
            continue
        if pci in seen:
            continue
        seen.add(pci)
        vmap = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
        vname = vmap.get(vendor.lower())
        if not vname or vname == "NVIDIA":     # NVIDIA handled by nvidia-smi
            continue
        label = None
        try:
            label = (dev / "product_name").read_text().strip()
        except OSError:
            pass
        out.append({"name": label or f"{vname} GPU ({pci})", "vendor": vname,
                    "pci_dev": pci, "vram_mb": None, "driver": None,
                    "discrete": False})
    return out


def detect_hardware():
    gpus = _nvidia_gpus() + _drm_gpus()
    return {
        "cpu": cpu_name(),
        "cores": os.cpu_count(),
        "gpus": gpus,
        "hybrid": len(gpus) > 1,
        "font": find_font(),
        "mangohud": shutil.which("mangohud") is not None,
        "config_path": str(MANGOHUD_DIR / "MangoHud.conf"),
        "config_exists": (MANGOHUD_DIR / "MangoHud.conf").is_file(),
    }


# MangoHud 0.8.2 with legacy_layout=false draws a column per listed param.
# The look in the reference screenshot: horizontal single line, orange section
# headings, white values, grey unit labels, separators between GPU|CPU|FPS,
# dark rounded background, frametime graph beneath.
MANGOHUD_STYLE = {
    "horizontal": True,             # single-line layout like the reference
    "legacy_layout": False,
    "table_columns": 14,
    "background_alpha": 0.6,
    "round_corners": 8,
    "font_size": 22,
    "font_size_text": 22,
    "cellpadding_y": -0.03,
    # colours (hex, no #): orange headings, white values, grey dividers
    "gpu_color": "F09000",          # orange - matches the RTX label
    "cpu_color": "F09000",
    "vram_color": "F09000",
    "ram_color": "F09000",
    "engine_color": "F09000",
    "io_color": "FFFFFF",
    "frametime_color": "FFFFFF",
    "background_color": "0B0E11",
    "text_color": "FFFFFF",
    "media_player_color": "FFFFFF",
    "network_color": "FFFFFF",
    "separator_color": "3A444E",
    "battery_color": "FFFFFF",
    "wine_color": "F09000",
}

MANGOHUD_PRESETS = {
    # Order: CPU block, then GPU block, then FPS + frame-time graph.
    # gpu_name/cpu_name are intentionally omitted so the overlay doesn't print
    # the long device-name prefix (e.g. "NVIDIA GeForce RTX 5070 Laptop").
    "reference": ["cpu_name", "cpu_stats", "cpu_load_change", "cpu_temp",
                  "gpu_name", "gpu_stats", "gpu_load_change", "gpu_temp",
                  "vram", "fps", "frame_timing=1"],
    "minimal": ["fps", "frame_timing=1", "cpu_stats", "gpu_stats"],
    "standard": ["fps", "fps_color_change", "frame_timing=1", "cpu_name",
                 "cpu_stats", "cpu_temp", "cpu_load_change", "ram", "gpu_name",
                 "gpu_stats", "gpu_temp", "gpu_load_change", "vram"],
    "benchmark": ["fps", "fps_color_change", "frame_timing=1", "histogram",
                  "cpu_stats", "cpu_temp", "cpu_power", "cpu_load_change",
                  "ram", "swap", "gpu_stats", "gpu_temp", "gpu_power",
                  "gpu_load_change", "vram", "io_read", "io_write",
                  "vulkan_driver", "engine_version", "resolution",
                  "benchmark_percentiles=AVG,1,0.1"],
    "stutter": ["fps", "frame_timing=1", "histogram", "frametime",
                "cpu_stats", "cpu_load_change", "gpu_stats",
                "gpu_load_change", "throttling_status", "present_mode"],
}


def mangohud_config(preset="reference", hw=None, pin_gpu=None,
                    log_dir=None, toggle_key="Shift_R+F12"):
    hw = hw or detect_hardware()
    lines = [
        "### Generated by Proton Command Center",
        f"### CPU: {hw['cpu']}",
    ]
    for g in hw["gpus"]:
        vram = f", {g['vram_mb']} MB" if g.get("vram_mb") else ""
        lines.append(f"### GPU: {g['name']} ({g['vendor']}{vram})")
    lines.append("")

    # layout + style block (order-independent, so grouped for readability)
    if MANGOHUD_STYLE.get("horizontal"):
        lines.append("horizontal")
    lines.append("legacy_layout=false")
    for k, v in MANGOHUD_STYLE.items():
        if k in ("horizontal", "legacy_layout"):
            continue
        if isinstance(v, bool):
            if v:
                lines.append(k)
        else:
            lines.append(f"{k}={v}")
    if hw.get("font"):
        lines.append(f"font_file={hw['font']}")
    lines.append("text_outline")

    # Short custom labels so the overlay shows "Ryzen AI 9 365" / "RTX 5070"
    # instead of the full auto-detected marketing string.
    cpu_lbl = _short_cpu_name(hw.get("cpu"))
    if cpu_lbl:
        lines.append(f"cpu_text={cpu_lbl}")
    disc = next((g for g in hw["gpus"] if g.get("discrete")), None) \
        or (hw["gpus"][0] if hw["gpus"] else None)
    if disc:
        lines.append(f"gpu_text={_short_gpu_name(disc.get('name'))}")

    if hw["hybrid"]:
        target = pin_gpu or next((g["pci_dev"] for g in hw["gpus"]
                                  if g["discrete"] and g["pci_dev"]), None)
        if target:
            lines += ["", "### hybrid GPU: pin stats to the discrete card",
                      f"pci_dev={target}"]

    lines += [""] + MANGOHUD_PRESETS.get(preset, MANGOHUD_PRESETS["reference"])
    lines += ["", f"toggle_hud={toggle_key}", "toggle_logging=Shift_L+F2"]
    if log_dir:
        lines += [f"output_folder={log_dir}", "log_duration=300",
                  "autostart_log=0", "benchmark_percentiles=AVG,1,0.1"]
    return "\n".join(lines) + "\n"


def apply_mangohud_config(preset="reference", pin_gpu=None, log_dir=None):
    MANGOHUD_DIR.mkdir(parents=True, exist_ok=True)
    dest = MANGOHUD_DIR / "MangoHud.conf"
    backup = None
    if dest.is_file():
        backup = dest.with_suffix(f".conf.pcc-{int(time.time())}.bak")
        shutil.copy2(dest, backup)
    text = mangohud_config(preset, pin_gpu=pin_gpu, log_dir=log_dir)
    tmp = dest.with_suffix(".conf.pcc-tmp")
    tmp.write_text(text)
    tmp.replace(dest)
    return {"written": str(dest), "backup": str(backup) if backup else None,
            "preset": preset}



# --------------------------------------------------------------------------
# Game Mode (CachyOS Handheld / gamescope session)
# --------------------------------------------------------------------------
def detect_display_mode():
    """Best-effort current resolution + refresh rate for auto-filling the
    gamescope command. On KDE Wayland, kscreen-doctor reports the active mode
    (marked with '*'). Falls back to /sys/class/drm modes for resolution and a
    sane 60 Hz default. Returns {'width','height','refresh'} or None."""
    # 1) kscreen-doctor (KDE Wayland) — has both res AND refresh
    kd = shutil.which("kscreen-doctor")
    if kd:
        try:
            out = subprocess.run([kd, "-o"], capture_output=True, text=True,
                                 timeout=5).stdout
            # active mode looks like:  1:2560x1600@165.00*!  (star = current).
            # Refresh may carry decimals, so match them and round to int.
            m = re.search(r"(\d+)x(\d+)@(\d+(?:\.\d+)?)\*", out)
            if m:
                return {"width": int(m.group(1)), "height": int(m.group(2)),
                        "refresh": round(float(m.group(3)))}
        except Exception:
            pass
    # 2) /sys/class/drm current-mode fallback (resolution only, assume 60)
    try:
        for status in Path("/sys/class/drm").glob("*/status"):
            if status.read_text().strip() == "connected":
                modes = status.parent / "modes"
                if modes.is_file():
                    first = modes.read_text().splitlines()
                    if first:
                        w, h = first[0].split("x")
                        return {"width": int(w), "height": int(h),
                                "refresh": 60}
    except Exception:
        pass
    return None


def game_mode_available():
    """Detect whether a gamescope Game Mode session can be launched. CachyOS
    Handheld ships steamos-session-select plus a gamescope session file."""
    switcher = shutil.which("steamos-session-select")
    session = any(Path(d).is_file() for d in (
        "/usr/share/wayland-sessions/gamescope-session.desktop",
        "/usr/share/wayland-sessions/gamescope.desktop",
    ))
    return {"available": bool(switcher) and session,
            "switcher": switcher,
            "has_session": session}


def launch_game_mode():
    """Switch to the gamescope Game Mode session. NOTE: this ends the desktop
    session, so it also terminates this backend and the browser tab — it's a
    one-way switch. Return before the session actually tears down so the API
    call can respond."""
    info = game_mode_available()
    if not info["available"]:
        raise RuntimeError("Game Mode not available — steamos-session-select "
                           "or the gamescope session file is missing "
                           "(install cachyos-handheld / gamescope-session-cachyos).")
    # steamos-session-select runs as the user and triggers the switch. Fire it
    # detached so the HTTP response can flush before the desktop goes down.
    subprocess.Popen(["steamos-session-select", "gamescope"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"switching": True}



# --------------------------------------------------------------------------
# Backup / restore — export and re-import PCC's own data (survives reinstalls)
# --------------------------------------------------------------------------
def export_backup(dest_dir=None):
    """Bundle PCC's data (DLL library, backups, state, config/API keys,
    MangoHud config) into a single .tar.gz the user can keep and re-import
    after an OS reinstall. Returns the archive path."""
    import tarfile
    dest_dir = Path(dest_dir) if dest_dir else (Path.home() / "Downloads"
               if (Path.home() / "Downloads").is_dir() else Path.home())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = dest_dir / f"pcc-backup-{stamp}.tar.gz"
    mango = Path.home() / ".config/MangoHud/MangoHud.conf"
    with tarfile.open(archive, "w:gz") as tar:
        # everything under DATA_DIR except the transient art cache
        for item in DATA_DIR.iterdir():
            if item.name in ("artcache", "art_cache"):
                continue
            tar.add(item, arcname=f"pcc-data/{item.name}")
        if mango.is_file():
            tar.add(mango, arcname="mangohud/MangoHud.conf")
    return {"archive": str(archive), "size": archive.stat().st_size}


def restore_backup(archive_path):
    """Restore a PCC backup archive produced by export_backup. Existing data is
    overwritten by the archive's contents; anything not in the archive is left
    alone. MangoHud config is restored to its standard location."""
    import tarfile
    src = Path(archive_path).expanduser()
    if not src.is_file():
        raise RuntimeError(f"Backup not found: {src}")
    restored = {"data": 0, "mangohud": False}
    with tarfile.open(src, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("pcc-data/"):
                rel = name[len("pcc-data/"):]
                if not rel or ".." in rel:
                    continue
                target = DATA_DIR / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                if fobj:
                    target.write_bytes(fobj.read())
                    restored["data"] += 1
                elif member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
            elif name == "mangohud/MangoHud.conf":
                mdir = Path.home() / ".config/MangoHud"
                mdir.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                if fobj:
                    (mdir / "MangoHud.conf").write_bytes(fobj.read())
                    restored["mangohud"] = True
    return {"restored": True, **restored}



# --------------------------------------------------------------------------
# Proton compatibility tool management (GE-Proton install / update awareness)
# --------------------------------------------------------------------------
GE_PROTON_RELEASES = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
COMPAT_INSTALL_DIR = Path.home() / ".local/share/Steam/compatibilitytools.d"


def _installed_ge_versions():
    """Version dirs already present in the user compatibilitytools.d."""
    out = set()
    if COMPAT_INSTALL_DIR.is_dir():
        for d in COMPAT_INSTALL_DIR.iterdir():
            if d.is_dir():
                out.add(d.name)
    return out


def list_ge_proton(limit=10):
    """List recent GE-Proton releases from GitHub with an 'installed' flag.
    Cached 6h. This is the 'what's available / am I up to date' view."""
    state = load_state()
    cache = state.get("ge_releases")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        rels = cache["data"]
    else:
        try:
            data = _gh_json(GE_PROTON_RELEASES)
        except Exception as e:
            return {"error": str(e), "releases": []}
        rels = []
        for r in data[:limit]:
            # GE-Proton 11+ ships both x86_64 and aarch64 (ARM) tarballs.
            # The x86_64 asset is named like "GE-Proton11-1.tar.gz" (no arch
            # suffix); ARM is "GE-Proton11-1-aarch64.tar.gz". Pick x86_64 and
            # never the ARM build (which breaks on x64 — see GE issue #569).
            def _is_x86(a):
                n = a.get("name", "")
                return (n.endswith(".tar.gz")
                        and "aarch64" not in n
                        and "arm64" not in n
                        and not n.endswith(".sha512sum"))
            asset = next((a for a in r.get("assets", []) if _is_x86(a)), None)
            if not asset:
                continue
            rels.append({"tag": r["tag_name"],
                         "name": r["name"] or r["tag_name"],
                         "url": asset["browser_download_url"],
                         "size": asset.get("size", 0),
                         "published": r.get("published_at", "")[:10]})
        state["ge_releases"] = {"ts": now, "data": rels}
        save_state(state)
    installed = _installed_ge_versions()
    for r in rels:
        # GE tarballs extract to a dir named after the tag (e.g. GE-Proton9-27)
        r["installed"] = any(r["tag"] in name or name in r["tag"]
                             for name in installed)
    newest = rels[0]["tag"] if rels else None
    up_to_date = bool(newest and any(r["installed"] and r["tag"] == newest
                                     for r in rels))
    return {"releases": rels, "newest": newest, "up_to_date": up_to_date,
            "installed": sorted(installed)}


def install_ge_proton(task_id, url, tag):
    """Download a GE-Proton tarball and extract it into the user
    compatibilitytools.d. Steam picks it up on next launch."""
    import tarfile, io
    TASKS[task_id] = {"status": "running", "progress": 10,
                      "detail": f"Downloading {tag}"}
    try:
        data = _gh_bytes(url, task_id)
        TASKS[task_id] = {"status": "running", "progress": 80,
                          "detail": f"Extracting {tag}"}
        COMPAT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # safety: refuse absolute paths or traversal in members
            for m in tar.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    raise RuntimeError(f"unsafe path in archive: {m.name}")
            tar.extractall(COMPAT_INSTALL_DIR)
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed {tag} — restart Steam to use it"}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0,
                          "detail": f"{tag}: {e}"}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        root = steam_root()
        try:
            if self.path in ("/", "/index.html"):
                html = (APP_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/api/status":
                self._json({
                    "steam_root": str(root) if root else None,
                    "steam_running": steam_running(),
                    "fossilize": find_fossilize(),
                    "driver": driver_version(),
                    "version": VERSION,
                    "started_at": STARTED_AT,
                })
            elif self.path == "/api/games":
                if not root:
                    self._json({"error": "Steam not found"}, 500); return
                games = list_games(root)
                state = load_state()
                drv = driver_version()
                # launch options: one parse of the newest localconfig
                lo_appids = set()
                cfgs = find_localconfigs(root)
                if cfgs:
                    try:
                        data = vdf_parse(cfgs[0].read_text(errors="replace"))
                        apps = _apps_node(ci_get(data, "UserLocalConfigStore")) or {}
                        for aid, entry in apps.items():
                            if isinstance(entry, dict) and ci_get(entry, "LaunchOptions"):
                                lo_appids.add(aid)
                    except Exception:
                        pass
                dlss_seen = state.get("dlss_seen", {})
                for g in games:
                    try:
                        st = compiled_status(root, g["appid"], state, drv)
                        g["compiled"] = st["compiled"]
                        g["outdated"] = st.get("outdated", False)
                    except Exception:
                        g["compiled"] = g["outdated"] = False
                    g["has_launch_options"] = g["appid"] in lo_appids
                    g["has_cache"] = any(
                        (Path(lib) / "shadercache" / g["appid"]).is_dir()
                        for lib in [g["library"]])
                    g["has_dlss"] = bool(dlss_seen.get(g["appid"]))
                self._json({"games": games})
            elif m := re.match(r"^/api/game/(\d+)/launch_options$", self.path):
                self._json(get_launch_options(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/dlss$", self.path):
                games = {g["appid"]: g for g in list_games(root)}
                g = games.get(m.group(1))
                dlls = scan_game_dlss(g["install_path"]) if g else []
                state = load_state()
                state.setdefault("dlss_seen", {})[m.group(1)] = bool(dlls)
                save_state(state)
                self._json({"dlls": dlls})
            elif self.path == "/api/progress":
                self._json({"games": install_progress(root)})
            elif self.path == "/api/owned_games":
                self._json({"games": owned_games(root)})
            elif self.path == "/api/steam/shader_settings":
                self._json(steam_shader_settings(root))
            elif self.path == "/api/hardware":
                self._json(detect_hardware())
            elif self.path == "/api/game_mode":
                self._json(game_mode_available())
            elif self.path == "/api/display_mode":
                self._json(detect_display_mode() or {})
            elif self.path == "/api/backup/export":
                self._json(export_backup())
            elif self.path == "/api/proton/list":
                self._json(list_ge_proton())
            elif self.path == "/api/env/shaders":
                self._json(environment_shader_status())
            elif m := re.match(r"^/api/mangohud(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(1) or "")
                preset = (qs.get("preset") or ["standard"])[0]
                hw = detect_hardware()
                self._json({"hardware": hw, "preset": preset,
                            "preview": mangohud_config(preset, hw)})
            elif self.path == "/api/compat_tools":
                self._json({"tools": list_compat_tools(root)})
            elif m := re.match(r"^/api/game/(\d+)/compat_tool$", self.path):
                self._json(get_compat_tool(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/cache$", self.path):
                self._json({"caches": cache_info(root, m.group(1)),
                            "status": compiled_status(root, m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/protondb(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                if qs.get("cached"):
                    self._json(protondb_cached(m.group(1)) or {"tier": None,
                                                               "cached": True})
                else:
                    self._json(protondb_summary(m.group(1)) or {"tier": None})
            elif m := re.match(r"^/api/game/(\d+)/autotune$", self.path):
                self._json(auto_tune(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/benchmark$", self.path):
                self._json(get_benchmark_data(root, m.group(1)))
            elif m := re.match(r"^/api/owned(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(1) or "")
                force = (qs.get("refresh") or ["0"])[0] == "1"
                self._json(fetch_owned_games(root, force=force))
            elif self.path == "/api/dlss/library":
                self._json({"dlls": dll_library()})
            elif m := re.match(r"^/api/art_debug/(\d+)(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                gname = (qs.get("name") or [None])[0]
                ART_MISSES.pop(m.group(1), None)      # force a real attempt
                tr = []
                try:
                    res = sgdb_art(m.group(1), name=gname, trace=tr)
                    self._json({"resolved": bool(res), "trace": tr})
                except Exception as e:
                    self._json({"resolved": False, "trace": tr + [str(e)]})
            elif m := re.match(r"^/api/art/(\d+)(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                gname = (qs.get("name") or [None])[0]
                try:
                    res = sgdb_art(m.group(1), name=gname)
                except Exception:
                    res = None
                if not res:
                    self._json({"error": "no art"}, 404); return
                img, ct = res
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(img)
            elif self.path == "/api/settings":
                key = load_config().get("sgdb_api_key", "")
                skey = load_config().get("steam_api_key", "")
                self._json({"sgdb_api_key_set": bool(key.strip()),
                            "sgdb_api_key_hint": (key[:4] + "…") if key else "",
                            "steam_api_key_set": bool(skey.strip())})
            elif m := re.match(r"^/api/tasks/([\w-]+)$", self.path):
                self._json(TASKS.get(m.group(1), {"status": "unknown"}))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        root = steam_root()
        try:
            body = self._body()
            if m := re.match(r"^/api/game/(\d+)/save$", self.path):
                self._json(set_game_config(
                    root, m.group(1),
                    launch_value=body.get("launch_options"),
                    compat_tool=body.get("compat_tool"),
                    close_steam=bool(body.get("close_steam"))))
            elif m := re.match(r"^/api/game/(\d+)/launch_options$", self.path):
                self._json(set_launch_options(root, m.group(1), body.get("value", ""),
                                              close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/env/shaders":
                self._json(set_environment_shaders(bool(body.get("enable"))))
            elif self.path == "/api/game_mode/launch":
                self._json(launch_game_mode())
            elif self.path == "/api/backup/restore":
                self._json(restore_backup(body["archive"]))
            elif self.path == "/api/proton/install":
                tid = str(uuid.uuid4())
                threading.Thread(target=install_ge_proton,
                                 args=(tid, body["url"], body["tag"]),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/settings":
                cfg = load_config()
                if "sgdb_api_key" in body:
                    cfg["sgdb_api_key"] = str(body["sgdb_api_key"]).strip()
                if "steam_api_key" in body:
                    cfg["steam_api_key"] = str(body["steam_api_key"]).strip()
                save_config(cfg)
                self._json({"saved": True})
            elif m := re.match(r"^/api/game/(\d+)/install$", self.path):
                self._json({"installing": install_game(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/install$", self.path):
                self._json({"installing": install_game(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/launch$", self.path):
                self._json({"launched": launch_game(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/compat_tool$", self.path):
                self._json(set_compat_tool(root, m.group(1), body.get("name", ""),
                                           close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/art/reset":
                n = 0
                for f in ART_DIR.iterdir():
                    f.unlink(missing_ok=True)
                    n += 1
                ART_MISSES.clear()
                self._json({"cleared": n})
            elif self.path == "/api/steam/shader_settings":
                self._json(set_steam_shader_setting(
                    root, body["file"], body["path"], body["value"],
                    close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/mangohud/apply":
                self._json(apply_mangohud_config(
                    body.get("preset", "standard"),
                    pin_gpu=body.get("pin_gpu"),
                    log_dir=str(BENCH_DIR) if body.get("enable_logging") else None))
            elif self.path == "/api/steam/launch":
                self._json({"launched": launch_steam()})
            elif self.path == "/api/dlss/swap":
                self._json(swap_dll(body["game_dll"], body["library_dll"]))
            elif self.path == "/api/dlss/restore":
                self._json(restore_dll(body["game_dll"]))
            elif self.path == "/api/dlss/import":
                self._json(import_dll(body["path"]))
            elif self.path == "/api/dlss/download":
                kind = body.get("kind", "sr")
                if kind not in DLL_SOURCES:
                    self._json({"error": f"unknown kind {kind}"}, 400); return
                tid = str(uuid.uuid4())
                threading.Thread(target=download_dlss, args=(tid, kind),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/dlss/download_sr":
                tid = str(uuid.uuid4())
                threading.Thread(target=download_latest_sr, args=(tid,), daemon=True).start()
                self._json({"task": tid})
            elif m := re.match(r"^/api/game/(\d+)/cache/clear$", self.path):
                self._json(clear_cache(root, m.group(1),
                                       keep_recordings=body.get("keep_recordings", True)))
            elif self.path == "/api/precompile_all":
                tid = str(uuid.uuid4())
                threading.Thread(
                    target=precompile_all,
                    args=(tid, root, int(body.get("device_index", 0))),
                    kwargs={"skip_compiled": body.get("skip_compiled", True)},
                    daemon=True,
                ).start()
                self._json({"task": tid})
            elif m := re.match(r"^/api/game/(\d+)/cache/precompile$", self.path):
                tid = str(uuid.uuid4())
                threading.Thread(
                    target=precompile_cache,
                    args=(tid, root, m.group(1), int(body.get("device_index", 0))),
                    daemon=True,
                ).start()
                self._json({"task": tid})
            else:
                self._json({"error": "not found"}, 404)
        except RuntimeError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    root = steam_root()
    print(f"Proton Command Center  ->  http://localhost:{PORT}")
    print(f"Steam root: {root or 'NOT FOUND'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
