#!/usr/bin/env python3
"""Proton Command Center test suite. Stdlib only, no Steam required:
builds a mock Steam install in a temp dir. Run:  python3 tests/test_pcc.py"""

import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pcc  # noqa: E402


def make_mock_steam(base: Path) -> Path:
    root = base / "Steam"
    (root / "steamapps/common/TestGame/Engine").mkdir(parents=True)
    (root / "userdata/12345678/config").mkdir(parents=True)
    (root / "steamapps/shadercache/12345/fozpipelinesv6").mkdir(parents=True)

    (root / "steamapps/appmanifest_12345.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"12345"\n\t"name"\t\t"Test Game"\n'
        '\t"installdir"\t\t"TestGame"\n\t"SizeOnDisk"\t\t"52428800"\n}\n')
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{root}"\n\t}}\n}}\n')
    (root / "userdata/12345678/config/localconfig.vdf").write_text(
        '"UserLocalConfigStore"\n{\n\t"friends"\n\t{\n'
        '\t\t"VoiceReceiveVolume"\t\t"0.75"\n\t}\n'
        '\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
        '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"12345"\n\t\t\t\t\t{\n'
        '\t\t\t\t\t\t"LaunchOptions"\t\t"PROTON_USE_NTSYNC=1 %command%"\n'
        '\t\t\t\t\t\t"playtime"\t\t"120"\n'
        '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')

    # fake DLSS DLL with VS_FIXEDFILEINFO signature, version 310.3.0.0
    blob = (b"MZ" + b"\x00" * 200 + struct.pack("<I", 0xFEEF04BD)
            + struct.pack("<I", 0x00010000)
            + struct.pack("<II", (310 << 16) | 3, 0) + b"\x00" * 100)
    (root / "steamapps/common/TestGame/Engine/nvngx_dlss.dll").write_bytes(blob)
    (root / "steamapps/shadercache/12345/fozpipelinesv6/steam_pipeline_cache.foz").write_bytes(b"foz")
    return root


class PCCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = make_mock_steam(base)
        # isolate app data
        pcc.DATA_DIR = base / "appdata"
        pcc.DLL_LIBRARY = pcc.DATA_DIR / "dlls"
        pcc.BACKUP_DIR = pcc.DATA_DIR / "backups"
        pcc.STATE_FILE = pcc.DATA_DIR / "state.json"
        pcc.CONFIG_FILE = pcc.DATA_DIR / "config.json"
        pcc.ART_DIR = pcc.DATA_DIR / "art"
        for d in (pcc.DLL_LIBRARY, pcc.BACKUP_DIR, pcc.ART_DIR):
            d.mkdir(parents=True, exist_ok=True)
        pcc.steam_running = lambda: False
        pcc.ART_MISSES.clear()
        pcc.driver_version = lambda: "580.65.06"

    def tearDown(self):
        self.tmp.cleanup()

    # ---- library / VDF ----
    def test_list_games(self):
        games = pcc.list_games(self.root)
        self.assertEqual(games[0]["appid"], "12345")
        self.assertEqual(games[0]["name"], "Test Game")
        self.assertTrue(games[0]["installed"])

    def test_launch_options_roundtrip(self):
        self.assertEqual(pcc.get_launch_options(self.root, "12345")["value"],
                         "PROTON_USE_NTSYNC=1 %command%")
        new = "PROTON_ENABLE_WAYLAND=1 game-performance %command% -dx12"
        pcc.set_launch_options(self.root, "12345", new)
        self.assertEqual(pcc.get_launch_options(self.root, "12345")["value"], new)

    def test_vdf_preserves_unrelated_keys(self):
        pcc.set_launch_options(self.root, "12345", "mangohud %command%")
        data = pcc.vdf_parse(
            (self.root / "userdata/12345678/config/localconfig.vdf").read_text())
        store = data["UserLocalConfigStore"]
        self.assertEqual(store["friends"]["VoiceReceiveVolume"], "0.75")
        self.assertEqual(
            store["Software"]["Valve"]["Steam"]["apps"]["12345"]["playtime"], "120")

    def test_vdf_escaped_quotes(self):
        tricky = 'WINEDLLOVERRIDES="dinput8=n,b" %command%'
        pcc.set_launch_options(self.root, "12345", tricky)
        self.assertEqual(pcc.get_launch_options(self.root, "12345")["value"], tricky)

    def test_refuses_while_steam_running_without_flag(self):
        pcc.steam_running = lambda: True
        with self.assertRaises(RuntimeError):
            pcc.set_launch_options(self.root, "12345", "x %command%")

    def test_auto_close_steam(self):
        calls = []
        state = {"running": True}
        pcc.steam_running = lambda: state["running"]
        pcc.shutdown_steam = lambda timeout=60: (calls.append(1), state.update(running=False))
        r = pcc.set_launch_options(self.root, "12345", "x %command%", close_steam=True)
        self.assertTrue(r["saved"])
        self.assertEqual(len(calls), 1)

    # ---- DLSS ----
    def test_pe_version(self):
        dll = self.root / "steamapps/common/TestGame/Engine/nvngx_dlss.dll"
        self.assertEqual(pcc.pe_version(dll), "310.3.0.0")

    def test_scan_swap_restore(self):
        game_dir = self.root / "steamapps/common/TestGame"
        dlls = pcc.scan_game_dlss(game_dir)
        self.assertEqual(dlls[0]["kind"], "sr")
        info = pcc.import_dll(dlls[0]["path"])
        lib = pcc.DLL_LIBRARY / "sr" / info["version"] / "nvngx_dlss.dll"
        s = pcc.swap_dll(dlls[0]["path"], str(lib))
        self.assertTrue(s["swapped"])
        r = pcc.restore_dll(dlls[0]["path"])
        self.assertTrue(r["restored"])

    def test_swap_refuses_type_mismatch(self):
        game_dll = self.root / "steamapps/common/TestGame/Engine/nvngx_dlss.dll"
        wrong = pcc.DLL_LIBRARY / "nvngx_dlssg.dll"
        wrong.write_bytes(b"x")
        with self.assertRaises(RuntimeError):
            pcc.swap_dll(str(game_dll), str(wrong))

    # ---- compile state ----
    def test_compile_state_persists_and_invalidates(self):
        appid = "12345"
        self.assertFalse(pcc.compiled_status(self.root, appid)["compiled"])
        pcc.mark_compiled(self.root, appid)
        self.assertTrue(pcc.compiled_status(self.root, appid)["compiled"])
        # survives "restart" (fresh read from disk)
        self.assertTrue(
            pcc.compiled_status(self.root, appid, pcc.load_state())["compiled"])
        # driver change invalidates
        pcc.driver_version = lambda: "595.20.01"
        st = pcc.compiled_status(self.root, appid)
        self.assertFalse(st["compiled"])
        self.assertEqual(st["stale_reason"], "driver changed")
        pcc.driver_version = lambda: "580.65.06"
        # foz change flags outdated but keeps compiled
        foz = self.root / "steamapps/shadercache/12345/fozpipelinesv6/steam_pipeline_cache.foz"
        foz.write_bytes(b"changed")
        os.utime(foz, (time.time() + 10, time.time() + 10))
        st = pcc.compiled_status(self.root, appid)
        self.assertTrue(st["compiled"])
        self.assertTrue(st["outdated"])
        # clear cache unmarks
        pcc.mark_compiled(self.root, appid)
        pcc.clear_cache(self.root, appid)
        self.assertFalse(pcc.compiled_status(self.root, appid)["compiled"])

    # ---- SGDB ----
    def test_sgdb_fetch_and_cache(self):
        pcc.save_config({"sgdb_api_key": "k3y"})
        calls = []

        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self.headers = payload, {"Content-Type": ct}
            def read(self): return self.payload
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            if "steamstatic.com" in req.full_url:
                raise OSError("404")           # CDN miss -> cascade continues
            if "/grids/steam/" in req.full_url:
                assert req.headers.get("Authorization") == "Bearer k3y"
                return FakeResp(json.dumps(
                    {"data": [{"url": "https://x/img.png"}]}).encode())
            return FakeResp(b"\x89PNG\r\n\x1a\n" + b"realdata", ct="image/png")

        pcc.urllib.request.urlopen = fake_urlopen
        img, ct = pcc.sgdb_art("777")
        self.assertEqual(ct, "image/png")
        pcc.sgdb_art("777")  # cache hit — no new network calls
        self.assertEqual(len(calls), 3)  # CDN miss + grids + image

    def test_sgdb_no_key_returns_none(self):
        pcc.save_config({"sgdb_api_key": ""})
        def cdn_down(req, timeout=0):
            raise OSError("404")
        pcc.urllib.request.urlopen = cdn_down
        self.assertIsNone(pcc.sgdb_art("888"))

    # ---- benchmarks (ported from Stutterless) ----
    def test_mangohud_csv_and_analysis(self):
        p = Path(self.tmp.name) / "log.csv"
        rows = ["os,cpu,gpu,ram,kernel,driver", "x,x,x,x,x,x",
                "fps,frametime,cpu_load"]
        rows += [f"120,{8300 if i % 50 else 45000},50" for i in range(200)]
        p.write_text("\n".join(rows))
        ft = pcc._parse_mangohud_csv(p)
        self.assertGreaterEqual(len(ft), 190)
        an = pcc._analyse_frametimes(ft)
        self.assertGreater(an["avg_fps"], 90)
        self.assertGreaterEqual(an["stutter_count"], 3)
        ds = pcc._downsample(ft, target=40)
        self.assertLessEqual(len(ds), 45)
        self.assertGreater(max(ds), 40)  # spikes preserved

    def test_smart_cache_clear_keeps_recordings(self):
        cache = self.root / "steamapps/shadercache/12345"
        (cache / "compiled_artifact.foz").write_bytes(b"compiled")
        r = pcc.clear_cache(self.root, "12345", keep_recordings=True)
        self.assertTrue(
            (cache / "fozpipelinesv6/steam_pipeline_cache.foz").exists())
        self.assertFalse((cache / "compiled_artifact.foz").exists())
        self.assertEqual(r["kept_recordings"], 1)

    def test_foz_classification_real_layout(self):
        """Pipelines live inside steamapprun_pipeline_cache.<hash>/ dirs too;
        whitelists, the replay ledger, and driver caches are never replayed."""
        fp = self.root / "steamapps/shadercache/12345/fozpipelinesv6"
        h = fp / "steamapprun_pipeline_cache.3554f158047e28bf"
        h.mkdir(parents=True, exist_ok=True)
        (h / "steam_pipeline_cache.foz").write_bytes(b"x")
        (h / "steamapprun_pipeline_cache.2db00e3ed998437d.1.foz").write_bytes(b"x")
        (h / "steam_pipeline_cache_whitelist.foz").write_bytes(b"w")
        (h / "replay_cache.8652dd52d95f2f26.foz").write_bytes(b"L")
        rad = (self.root / "steamapps/shadercache/12345/mesa_shader_cache_sf/a/RADV")
        rad.mkdir(parents=True)
        (rad / "foz_cache.foz").write_bytes(b"d")
        foz = pcc.find_foz(self.root, "12345")
        names = [Path(f).name for f in foz]
        self.assertIn("steamapprun_pipeline_cache.2db00e3ed998437d.1.foz", names)
        self.assertTrue(all("whitelist" not in n for n in names))
        self.assertTrue(all(not n.startswith("replay_cache") for n in names))
        self.assertTrue(all("mesa_shader_cache" not in f for f in foz))
        self.assertTrue(pcc.find_replayer_cache(self.root, "12345")
                        .endswith("replay_cache.8652dd52d95f2f26.foz"))

    def test_skip_list(self):
        self.assertIn("1493710", pcc.SKIP_APPIDS)
        self.assertTrue(pcc.SKIP_NAME_RE.match("Steam Linux Runtime 3.0"))

    def test_download_sr_part_file_bug(self):
        """Regression: downloader must not hand a .part-named file to import."""
        import struct as st
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 4, 0) + b"\x00" * 100)

        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Type": ct,
                                "Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    return self.payload
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIA/DLSS"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps({"tree": [
                    {"path": "lib/Windows_x86_64/rel/nvngx_dlss.dll",
                     "type": "blob"}]}).encode())
            return FakeResp(blob, ct="application/octet-stream")

        pcc.urllib.request.urlopen = fake
        pcc.download_latest_sr("task1")
        self.assertEqual(pcc.TASKS["task1"]["status"], "done",
                         pcc.TASKS["task1"])

    # ---- compat tools / launch / status semantics ----
    def _mock_config_vdf(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/config.vdf").write_text(
            '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n'
            '\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n')

    def test_compat_tool_roundtrip(self):
        self._mock_config_vdf()
        td = self.root / "compatibilitytools.d/ge"
        td.mkdir(parents=True)
        (td / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n\t"compat_tools"\n\t{\n'
            '\t\t"GE-Proton10-4"\n\t\t{\n\t\t\t"display_name"\t\t'
            '"GE-Proton 10-4"\n\t\t}\n\t}\n}\n')
        names = [t["name"] for t in pcc.list_compat_tools(self.root)]
        self.assertIn("GE-Proton10-4", names)
        pcc.set_compat_tool(self.root, "12345", "GE-Proton10-4")
        self.assertEqual(pcc.get_compat_tool(self.root, "12345")["name"],
                         "GE-Proton10-4")
        pcc.set_compat_tool(self.root, "12345", "")
        self.assertEqual(pcc.get_compat_tool(self.root, "12345")["name"], "")

    def test_compiled_survives_new_pipeline_data(self):
        pcc.mark_compiled(self.root, "12345")
        foz = (self.root /
               "steamapps/shadercache/12345/fozpipelinesv6/steam_pipeline_cache.foz")
        foz.write_bytes(b"grown")
        os.utime(foz, (time.time() + 5, time.time() + 5))
        st = pcc.compiled_status(self.root, "12345")
        self.assertTrue(st["compiled"])       # purple light stays on
        self.assertTrue(st["outdated"])       # but flags recompile

    def test_session_env_no_crash_without_display(self):
        env = pcc.session_env()
        self.assertIsInstance(env, dict)

    # ---- friendly versions + multi-repo downloader ----
    def test_friendly_dlss_versions(self):
        self.assertEqual(pcc.friendly_dlss("310.2.1.0"),
                         {"gen": "DLSS 4", "short": "310.2.1"})
        self.assertEqual(pcc.friendly_dlss("3.7.10.0")["gen"], "DLSS 3")
        self.assertTrue(pcc.version_tuple("310.4.0.0")
                        > pcc.version_tuple("310.2.1.0"))

    def test_download_dlss_tree_search(self):
        import struct as st
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 4, 0) + b"\x00" * 100)

        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Type": ct,
                                "Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    return self.payload
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIAGameWorks/Streamline"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps({"tree": [
                    {"path": "bin/x64/nvngx_dlssg.dll", "type": "blob"}]}).encode())
            return FakeResp(blob, ct="application/octet-stream")

        pcc.urllib.request.urlopen = fake
        pcc.download_dlss("t_fg", "fg")
        self.assertEqual(pcc.TASKS["t_fg"]["status"], "done",
                         pcc.TASKS["t_fg"])

    # ---- hardened downloader + system compat dirs ----
    def _fake_resp_class(self):
        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Type": ct,
                                "Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    return self.payload
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return FakeResp

    def test_download_truncated_tree_and_lfs(self):
        import struct as st
        FakeResp = self._fake_resp_class()
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 5, 0) + b"\x00" * 100)
        lfs = b"version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n"

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIAGameWorks/Streamline"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps(
                    {"tree": [], "truncated": True}).encode())
            if "/contents/bin/x64?" in u:
                return FakeResp(json.dumps(
                    [{"name": "nvngx_dlssg.dll",
                      "path": "bin/x64/nvngx_dlssg.dll"}]).encode())
            if "/contents/" in u:
                raise OSError("404")
            if "raw.githubusercontent" in u:
                return FakeResp(lfs, ct="application/octet-stream")
            if "media.githubusercontent" in u:
                return FakeResp(blob, ct="application/octet-stream")
            raise OSError("unexpected " + u)

        pcc.urllib.request.urlopen = fake
        pcc.download_dlss("t_lfs", "fg")
        self.assertEqual(pcc.TASKS["t_lfs"]["status"], "done",
                         pcc.TASKS["t_lfs"])

    def test_download_release_zip_fallback(self):
        import struct as st
        import io
        import zipfile
        FakeResp = self._fake_resp_class()
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 5, 0) + b"\x00" * 100)
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("sdk/bin/x64/nvngx_dlssd.dll", blob)
        zb = zbuf.getvalue()

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIAGameWorks/Streamline"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps(
                    {"tree": [], "truncated": True}).encode())
            if "/contents/" in u:
                raise OSError("404")
            if "/releases/latest" in u:
                return FakeResp(json.dumps({"assets": [
                    {"name": "sdk.zip", "size": len(zb),
                     "browser_download_url": "https://gh/sdk.zip"}]}).encode())
            if "sdk.zip" in u:
                return FakeResp(zb, ct="application/zip")
            raise OSError("unexpected " + u)

        pcc.urllib.request.urlopen = fake
        pcc.download_dlss("t_zip", "rr")
        self.assertEqual(pcc.TASKS["t_zip"]["status"], "done",
                         pcc.TASKS["t_zip"])

    def test_compat_tools_extra_paths(self):
        extra = Path(self.tmp.name) / "sys-compat/proton-cachyos"
        extra.mkdir(parents=True)
        (extra / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n\t"compat_tools"\n\t{\n'
            '\t\t"proton-cachyos"\n\t\t{\n\t\t\t"display_name"\t\t'
            '"Proton-CachyOS"\n\t\t}\n\t}\n}\n')
        os.environ["STEAM_EXTRA_COMPAT_TOOLS_PATHS"] = str(extra.parent)
        try:
            names = [t["name"] for t in pcc.list_compat_tools(self.root)]
            self.assertIn("proton-cachyos", names)
        finally:
            del os.environ["STEAM_EXTRA_COMPAT_TOOLS_PATHS"]

    # ---- owned library ----
    def test_steamid_and_owned_games(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/loginusers.vdf").write_text(
            '"users"\n{\n\t"76561198012345678"\n\t{\n'
            '\t\t"MostRecent"\t\t"1"\n\t}\n}\n')
        self.assertEqual(pcc.steamid64(self.root), "76561198012345678")
        pcc.save_config({"steam_api_key": "K"})
        FakeResp = self._fake_resp_class()

        def fake(req, timeout=0):
            return FakeResp(json.dumps({"response": {"games": [
                {"appid": 42, "name": "Owned Game"}]}}).encode())

        pcc.urllib.request.urlopen = fake
        owned = pcc.owned_games(self.root)
        self.assertEqual(owned[0]["name"], "Owned Game")

    def test_owned_games_requires_key(self):
        pcc.save_config({"steam_api_key": ""})
        with self.assertRaises(RuntimeError):
            pcc.owned_games(self.root)

    # ---- auto-tune ----
    def test_detect_engine_and_autotune(self):
        g = self.root / "steamapps/common/TestGame"
        (g / "Engine").mkdir(exist_ok=True)
        paks = g / "Game/Content/Paks"
        paks.mkdir(parents=True)
        (paks / "global.ucas").write_bytes(b"x")
        (paks / "pak0.pak").write_bytes(b"x")
        exe = g / "Game/Binaries/Win64"
        exe.mkdir(parents=True)
        (exe / "Game-Win64-Shipping.exe").write_bytes(
            b"MZ" + b"\x00" * 50 + b"d3d12.dll")
        det = pcc.detect_engine(g)
        self.assertEqual(det["engine"], "unreal5")
        self.assertTrue(det["dx12"])
        pcc.gpu_vram_mb = lambda: 8188
        r = pcc.auto_tune(self.root, "12345")
        self.assertIn("PROTON_ENABLE_NVAPI=1", r["launch_string"])
        self.assertIn("dxgi.maxDeviceMemory=7164", r["launch_string"])
        self.assertTrue(r["precompile_recommended"])

    def test_replayer_cache_integration(self):
        """Our replay must write into Steam's ledger so Steam skips its own
        'Processing Vulkan shaders' pass — and never replay the ledger as input."""
        base = self.root / "steamapps/shadercache/12345/fozpipelinesv6"
        ledger_dir = base / "steamapprun_pipeline_cache.abc123"
        ledger_dir.mkdir()
        (ledger_dir / "steam_pipeline_cache.foz").write_bytes(b"src")
        ledger = ledger_dir / "replay_cache.deadbeef.foz"
        ledger.write_bytes(b"ledger")
        srcs = pcc.find_foz(self.root, "12345")
        self.assertTrue(any("steamapprun_pipeline_cache.abc123" in s for s in srcs))
        self.assertEqual(pcc.find_replayer_cache(self.root, "12345"),
                         str(ledger))
        calls = []
        pcc.find_fossilize = lambda: "/usr/bin/fossilize_replay"
        real = pcc.subprocess.run
        pcc.subprocess.run = lambda cmd, **kw: (
            calls.append(cmd), type("R", (), {"returncode": 0})())[1]
        try:
            pcc.precompile_cache("tt", self.root, "12345", 0)
        finally:
            pcc.subprocess.run = real
        self.assertTrue(all("--replayer-cache" in c for c in calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
