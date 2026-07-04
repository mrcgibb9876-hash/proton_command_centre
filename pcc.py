#!/usr/bin/env python3
"""
Proton Command Center (PCC)
Per-game launch options, DLSS DLL management, and shader cache control
for Steam on Linux. Stdlib only. Run: python3 pcc.py  ->  http://localhost:8686
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STARTED_AT = int(time.time())
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.2.1"
PORT = 8686
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".local/share/proton-command-center"
DLL_LIBRARY = DATA_DIR / "dlls"        # dlls/<kind>/<version>/<name>.dll
BACKUP_DIR = DATA_DIR / "backups"      # backups/<appid>/<relpath>.pccbak
DATA_DIR.mkdir(parents=True, exist_ok=True)
DLL_LIBRARY.mkdir(parents=True, exist_ok=True)
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


def find_foz(root, appid):
    foz = []
    for lib in library_folders(root):
        c = lib / "shadercache" / str(appid)
        if c.is_dir():
            foz += sorted(str(p) for p in c.rglob("*.foz")
                          if "fozpipelines" in str(p.parent)
                          and "whitelist" not in p.name.lower())
    return foz


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


def launch_steam():
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    subprocess.Popen([exe], start_new_session=True, env=session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def launch_game(appid):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    subprocess.Popen([exe, f"steam://rungameid/{appid}"],
                     start_new_session=True, env=session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


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
            flags = int(app.get("StateFlags") or 0)
            downloaded = int(ci_get(app, "BytesDownloaded") or 0)
            to_download = int(ci_get(app, "BytesToDownload") or 0)
            games.append({
                "appid": appid,
                "name": name or installdir or appid,
                "install_path": str(install_path),
                "installed": install_path.is_dir(),
                "fully_installed": flags == 4,
                "download_pct": round(100 * downloaded / to_download, 1)
                                if to_download and flags != 4 else None,
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
    """Read file version from VS_FIXEDFILEINFO without dependencies."""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    sig = struct.pack("<I", 0xFEEF04BD)
    idx = blob.find(sig)
    if idx < 0 or idx + 16 > len(blob):
        return None
    ms, ls = struct.unpack_from("<II", blob, idx + 8)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def scan_game_dlss(install_path):
    found = []
    base = Path(install_path)
    if not base.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.lower() in DLSS_KINDS:
                p = Path(dirpath) / fn
                meta = DLSS_KINDS[fn.lower()]
                found.append({
                    "path": str(p),
                    "name": fn,
                    "kind": meta["kind"],
                    "label": meta["label"],
                    "version": pe_version(p),
                    "friendly": friendly_dlss(pe_version(p)),
                    "size": p.stat().st_size,
                    "backed_up": _backup_path(p).exists(),
                })
    return found


def _backup_path(dll_path):
    p = Path(dll_path)
    h = re.sub(r"[^A-Za-z0-9]", "_", str(p))
    return BACKUP_DIR / f"{h}.pccbak"


def dll_library():
    out = []
    for kind_dir in sorted(DLL_LIBRARY.iterdir()) if DLL_LIBRARY.is_dir() else []:
        if not kind_dir.is_dir():
            continue
        for ver_dir in sorted(kind_dir.iterdir()):
            dll = next(ver_dir.glob("*.dll"), None)
            if dll:
                out.append({
                    "kind": kind_dir.name,
                    "version": ver_dir.name,
                    "friendly": friendly_dlss(ver_dir.name),
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
    dest = DLL_LIBRARY / kind / ver
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest / p.name.lower())
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
        "detail": ("Couldn't fetch from NVIDIA's repos ("
                   + "; ".join(errors[:2]) + "). You can still download the "
                   "DLL manually (e.g. TechPowerUp) and import it below.")}


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


def install_game(appid):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    subprocess.Popen([exe, f"steam://install/{appid}"],
                     start_new_session=True, env=session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


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


def install_game(appid):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    subprocess.Popen([exe, f"steam://install/{appid}"],
                     start_new_session=True, env=session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


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
    done = 0
    for foz in foz_files:
        TASKS[task_id]["detail"] = f"Replaying {Path(foz).name}"
        try:
            subprocess.run(
                [exe, "--device-index", str(device_index),
                 "--num-threads", str(max(1, (os.cpu_count() or 4) - 2)), foz],
                capture_output=True, timeout=3600,
            )
        except Exception as e:
            TASKS[task_id] = {"status": "error", "progress": 0, "detail": f"{Path(foz).name}: {e}"}
            return
        done += 1
        TASKS[task_id]["progress"] = int(done / len(foz_files) * 100)
    mark_compiled(root, appid)
    TASKS[task_id] = {"status": "done", "progress": 100,
                      "detail": f"Replayed {done} pipeline database(s)"}


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
    for gi, (g, foz_files) in enumerate(todo):
        for foz in foz_files:
            TASKS[task_id]["detail"] = f'{g["name"]} — {Path(foz).name}'
            try:
                subprocess.run([exe, "--device-index", str(device_index),
                                "--num-threads", threads, foz],
                               capture_output=True, timeout=3600)
            except Exception as e:
                TASKS[task_id] = {"status": "error", "progress": 0,
                                  "detail": f'{g["name"]}: {e}'}
                return
        mark_compiled(root, g["appid"])
        TASKS[task_id]["progress"] = int((gi + 1) / len(todo) * 100)
    TASKS[task_id] = {"status": "done", "progress": 100,
                      "detail": f"Compiled {len(todo)} game(s)"}


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
            elif self.path == "/api/owned_games":
                self._json({"games": owned_games(root)})
            elif self.path == "/api/compat_tools":
                self._json({"tools": list_compat_tools(root)})
            elif m := re.match(r"^/api/game/(\d+)/compat_tool$", self.path):
                self._json(get_compat_tool(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/cache$", self.path):
                self._json({"caches": cache_info(root, m.group(1)),
                            "status": compiled_status(root, m.group(1))})
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
            if m := re.match(r"^/api/game/(\d+)/launch_options$", self.path):
                self._json(set_launch_options(root, m.group(1), body.get("value", ""),
                                              close_steam=bool(body.get("close_steam"))))
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
