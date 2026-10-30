"""Backend tests. Run from the repo root: python3 -m unittest discover -s tests"""
import atexit
import asyncio
import collections
import copy
import json
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Stand-in for the module Decky injects at runtime.
decky = types.ModuleType("decky")
decky.logger = logging.getLogger("decky-test")
sys.modules["decky"] = decky

import main as m  # noqa: E402

# Files the plugin shares with the camera tool and the overlay: never the
# real ones from a test (a stray state file stopped camera runs waiting for
# confirmations).
_TMP = tempfile.mkdtemp(prefix="oled-tint-tests-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
m.STATE_FILE = os.path.join(_TMP, "state.json")
m.OSD_TEXT = os.path.join(_TMP, "osd.txt")
m.SLIDER_LOG = os.path.join(_TMP, "slider.log")     # the camera tool reads it (handshake)

# Tests call _refresh back to back; the rate limit is tested on its own.
m.DEBOUNCE = 0.0
m.COMPILE_GAP = 0.0

N = None
L = len(m.LEVELS)


def at(level8, value):
    """A level list with one value set, at the level labelled level8 (8-bit)."""
    v = m._auto()
    v[[min(255, round(x * 256)) for x in m.LEVELS].index(level8)] = value
    return v


def neutral_curves():
    return (m._neutral(),) * len(m.KINDS)


def parse_cube(text):
    lines = text.splitlines()
    n = int(lines[1].split()[1])
    rows = [tuple(map(float, l.split())) for l in lines[2:]]
    return n, rows


def parse_fx(text):
    curves = []
    for c in "RGB":
        body = re.search(rf"Curve{c}\[\d+\] = {{ ([^}}]*) }}", text).group(1)
        curves.append([float(v) for v in body.split(",")])
    return curves


class FakeHelper:
    """Records xprop calls and answers reads with preset atoms."""

    def __init__(self):
        self.calls = []
        self.atoms = {}

    def run(self, args):
        self.calls.append(args)
        if "-set" in args:
            self.atoms[args[-2]] = args[-1]
            return m._Result(0, "")
        if "-remove" in args:
            self.atoms.pop(args[-1], None)
            return m._Result(0, "")
        out = "".join(f'{a}(UTF8_STRING) = "{v}"\n' if a.startswith(("GAMESCOPE_COLOR", "GAMESCOPE_RESHADE"))
                      else f"{a}(CARDINAL) = {v}\n" for a, v in self.atoms.items())
        return m._Result(0, out)

    def write(self, path, curves, kind, crush=0, dither=None, gain=1.0):
        m.write_output(path, curves, kind, crush, dither, gain)



def _load(path):
    with open(path) as f:
        return json.load(f)


def _dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _read(path):
    with open(path) as f:
        return f.read()

def make_plugin(tmp, steps=None, engine="look"):
    p = m.Plugin.__new__(m.Plugin)
    p.settings_path = os.path.join(tmp, "settings.json")
    p.lut_dir = tmp
    p.fx_dir = os.path.join(tmp, "fx")
    os.makedirs(p.fx_dir, exist_ok=True)
    p.settings_mtime = None
    p.revision = 0
    p.settings = {"enabled": True, "engine": engine,
                  "steps": steps or [{"b": b, **{k: m._auto() for k in m.KINDS}} for b in m.DEFAULT_STEPS],
                  "profile": "Eye", "profiles": {}}
    p.backlight = None
    p.display = ":0"
    p.helper = FakeHelper()
    p.preview = None
    p.applied = None
    p.slot = 0
    p.conflict = False
    p.external = False
    p.focused = None
    p.app_on = None
    p.fast_until = 0.0
    p.last_ping = 0.0
    p.current = p.current_l = p.current_h = m._neutral()
    p.backlight_now = None
    p.dithering = False
    p.vibrance_now = None
    p.overlay_open = False
    p.ready_since = -1e9          # gamescope long answering
    p._read_brightness = lambda: 40
    return p


class Ownership(unittest.TestCase):
    """Second external review: leave other filters' atoms alone, step aside
    fully on a conflict, give vibrance back when the correction goes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = make_plugin(self.tmp.name, engine="reshade")
        self.p.settings["steps"][0]["v"][0] = -40

    def tearDown(self):
        self.tmp.cleanup()

    def test_unload_keeps_a_foreign_filter(self):
        self.p.helper.atoms[m.LOOK_ATOMS[0]] = "/other/filter.cube"
        self.p.helper.atoms[m.RESHADE_ATOM] = "SomeoneElse.fx"
        self.p._clear_atoms()
        self.assertEqual(self.p.helper.atoms, {m.LOOK_ATOMS[0]: "/other/filter.cube", m.RESHADE_ATOM: "SomeoneElse.fx"})

    def test_unload_removes_our_output(self):
        self.p._refresh()
        self.assertIn(m.RESHADE_ATOM, self.p.helper.atoms)
        self.p._clear_atoms()
        self.assertNotIn(m.RESHADE_ATOM, self.p.helper.atoms)

    def test_conflict_takes_our_output_off(self):
        self.p._refresh()
        self.p.helper.atoms[m.LOOK_ATOMS[0]] = "/other/filter.cube"
        self.p.conflict = True
        self.p._refresh()
        self.assertIsNone(self.p.applied)
        self.assertNotIn(m.RESHADE_ATOM, self.p.helper.atoms)
        self.assertEqual(self.p.helper.atoms[m.LOOK_ATOMS[0]], "/other/filter.cube")

    def test_vibrance_back_when_correction_goes_with_menu_open(self):
        self.p.applied = None
        self.p.menu_look = True
        self.p.settings["vibrance_saved"] = "1056964608"
        self.p.vibrance_now = m.VIBRANCE_NATIVE
        self.p._save_settings = lambda: None
        self.p._sync_vibrance()
        self.assertEqual(self.p.vibrance_now, "1056964608")
        self.assertNotIn("vibrance_saved", self.p.settings)


class Sidebar(unittest.TestCase):
    """Sidebar overhaul: status line, off for this game, Compare, profiles."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = make_plugin(self.tmp.name, engine="reshade")
        self.p.settings["steps"][0]["v"][0] = -40
        self.p.wake = types.SimpleNamespace(set=lambda: None)

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, coro):
        return asyncio.run(coro)

    def test_snap_targets_land_on_the_right_side_of_the_dither_band(self):
        lo, hi = m.SNAP_ZONE
        tables = {"70": {"V": 64}, "450": {"V": 64}}
        bin_ = m.Plugin._bin
        self.assertEqual(m.dither_weight(tables, bin_(lo - 1), lo - 1)[1], 0.0)     # clean below: no dither
        self.assertEqual(m.dither_weight(tables, bin_(hi + 1), hi + 1)[1], 1.0)     # bad mode above: full dither

    def test_golden_falls_back_to_pair_trim(self):
        t = {"trim": "old", "trims": {"pair": "p", "still": "s"}}
        self.assertEqual(m.trim_for(t, "golden"), "p")
        t["trims"]["golden"] = "g"
        self.assertEqual(m.trim_for(t, "golden"), "g")
        self.assertEqual(m.trim_for({"trim": "old"}, "golden"), "old")

    def test_status_says_why_nothing_is_corrected(self):
        self.p._refresh()
        self.assertTrue(self.p._status()["text"].startswith("Correcting"))
        self.p.external = True
        self.assertEqual(self.p._status()["state"], "paused")
        self.p.external = False
        self.p.settings["enabled"] = False
        self.assertEqual(self.p._status()["text"], "Correction off")
        self.p.settings["enabled"] = True
        self.state(self.p.set_profile(m.FACTORY))
        self.assertIn("Factory", self.p._status()["text"])

    def test_gfx_focus_says_which_app_is_in_front(self):
        # Quick Access open over Silksong: FOCUSED_APP is Steam's UI, the
        # picture still the game's, whatever the menu's open/close timing.
        self.p.focused, self.p.focused_gfx = m.STEAM_UI_APP, "1030300"
        self.p.overlay_open, self.p.overlay_since, self.p.focus_since = True, 50.0, 10.0
        self.assertFalse(self.p._in_steam_ui())
        self.assertEqual(self.p._game(), "1030300")
        self.p.focused_gfx = m.STEAM_UI_APP               # the library, really in front
        self.assertTrue(self.p._in_steam_ui())
        self.assertIsNone(self.p._game())

    def test_off_for_this_game_also_behind_a_menu(self):
        self.p.focused = "1145360"
        self.p.last_game = "1145360"
        self.p._refresh()
        self.assertIsNotNone(self.p.applied)
        st = self.state(self.p.set_game_off(True, "Hades"))
        self.assertTrue(st["game_off"])
        self.assertIsNone(self.p.applied)
        self.assertEqual(self.p.settings["app_off"], {"1145360": "Hades"})
        # A menu opens over the game: Steam's UI gets the focus, still the game.
        self.p.overlay_open, self.p.overlay_since, self.p.focus_since = True, 10.0, 11.0
        self.p.focused = m.STEAM_UI_APP
        self.assertEqual(self.p._game(), "1145360")
        self.assertIsNone(self.p._target(40))
        # Steam's own UI is not a game.
        self.p.overlay_open = False
        self.assertIsNone(self.p._game())
        self.assertIsNotNone(self.p._target(40))

    def test_compare_switches_off_for_a_while(self):
        clock = [100.0]
        with mock.patch.object(m.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(m.asyncio, "get_event_loop", lambda: types.SimpleNamespace(time=lambda: clock[0])):
            self.state(self.p.compare(5))
            self.assertIsNone(self.p._target(40))
            self.assertIn("Comparing", self.p._status()["text"])
            clock[0] += 5.1
            self.assertIsNotNone(self.p._target(40))

    def test_profiles_new_duplicate_rename_delete(self):
        self.p._save_settings()
        self.state(self.p.profile_duplicate())
        self.assertEqual(self.p.settings["profile"], "Eye copy")
        self.assertEqual(self.p.settings["profiles"]["Eye copy"], self.p.settings["profiles"]["Eye"])
        self.state(self.p.profile_rename("  Night   time "))
        self.assertEqual(self.p.settings["profile"], "Night time")
        self.assertNotIn("Eye copy", self.p.settings["profiles"])
        self.state(self.p.profile_rename("Eye"))                 # taken: gets a free name
        self.assertEqual(self.p.settings["profile"], "Eye 2")
        self.state(self.p.profile_new())
        self.assertEqual(self.p.settings["profile"], "Custom")
        self.assertEqual(self.p.settings["steps"], m._factory())
        self.state(self.p.profile_delete())
        self.assertEqual(self.p.settings["profile"], m.FACTORY)
        self.assertNotIn("Custom", self.p.settings["profiles"])
        self.state(self.p.profile_rename("Nope"))                # Factory can't be renamed
        self.assertEqual(self.p.settings["profile"], m.FACTORY)

    def test_deleting_every_profile_doesnt_bring_eye_back(self):
        self.p.settings["profiles"] = {"Eye": m._factory()}
        self.p.settings["profile"] = "Eye"
        self.state(self.p.profile_delete())
        self.p.settings_mtime = None
        self.p._load_settings()
        self.assertEqual(self.p.settings["profiles"], {})
        self.assertEqual(self.p.settings["profile"], m.FACTORY)

    def test_settings_keep_import_backup_and_game_list(self):
        self.p.settings["dither_tables_backup"] = {"177": {"x": 1}}
        self.p.settings["app_off"] = {"42": "Game"}
        self.p._save_settings()
        q = make_plugin(self.tmp.name)
        q._load_settings()
        self.assertEqual(q.settings["dither_tables_backup"], {"177": {"x": 1}})
        self.assertEqual(q.settings["app_off"], {"42": "Game"})


class Review3(unittest.TestCase):
    def test_import_with_a_broken_table_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            good = {"V": 4, "L": {c: [0, 1, 2, 3, 4] for c in "RGB"}, "yref": [[4, 1.0]]}
            for bad in ({"L": good["L"], "yref": good["yref"]},              # no V
                        {**good, "trims": {"wobble": None}}):                  # unknown mode
                data = m.export_calibration("Cam", m._factory(), {"177": bad}, {})
                path = os.path.join(tmp, "in.json")
                _dump(data, path)
                before = copy.deepcopy(p.settings)
                res = asyncio.run(p.import_calibration(path))
                self.assertFalse(res["ok"])
                self.assertEqual(p.settings, before)

    def test_foreign_filter_on_one_look_atom_takes_our_other_one_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="look")
            p.settings["steps"][0]["v"][0] = -40
            p._refresh()
            ours = dict(p.helper.atoms)
            self.assertTrue(all(m.LOOK_ATOMS[i] in ours for i in (0, 1)))
            p.helper.atoms[m.LOOK_ATOMS[0]] = "/other/filter.cube"     # only G22 replaced
            atoms = p._read_atoms()
            # What the verify step does: a changed atom clears `applied`.
            foreign = [a for a, v in atoms.items() if v and not p._ours(a, v)]
            p.conflict = bool(foreign)
            p.owned_seen = any(v and p._ours(a, v) for a, v in atoms.items() if a in (*m.LOOK_ATOMS, m.RESHADE_ATOM))
            p.applied = None
            p._refresh()
            self.assertNotIn(m.LOOK_ATOMS[1], p.helper.atoms)               # our PQ look gone
            self.assertEqual(p.helper.atoms[m.LOOK_ATOMS[0]], "/other/filter.cube")

    def test_menu_over_a_game_keeps_its_own_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["app_effects"] = {"1030300": {"effect": "OledCRT.fx"}}
            p.focused, p.focused_gfx = m.STEAM_UI_APP, "1030300"     # Quick Access open
            self.assertEqual(p._app_effect(), {"effect": "OledCRT.fx"})
            p.focused_gfx = m.STEAM_UI_APP
            self.assertIsNone(p._app_effect())


class Review5(unittest.TestCase):
    GOOD = {"V": 8, "L": {c: list(range(9)) for c in "RGB"}, "yref": [[8, 1.0]]}

    def test_trim_codes_must_rise_inside_the_hand_over(self):
        ok = {"codes": [2, 4], "R": [1, 1], "G": [1, 1], "B": [1, 1]}
        self.assertTrue(m._table_ok("96", {**self.GOOD, "trims": {"pair": ok}}))
        for codes in ([4, 4], [4, 2], [0, 4], [2, 8]):                 # dup, falling, zero, at V
            t = {"codes": codes, "R": [1, 1], "G": [1, 1], "B": [1, 1]}
            self.assertFalse(m._table_ok("96", {**self.GOOD, "trims": {"pair": t}}))

    def test_import_of_a_malformed_file_reports_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            path = os.path.join(tmp, "in.json")
            _dump({"format": m.CALIBRATION_FORMAT, "version": 1, "steps": m._factory(),
                       "dither_tables": ["not", "a", "dict"]}, path)
            res = asyncio.run(p.import_calibration(path))
            self.assertFalse(res["ok"])
            self.assertIn("error", res)

    def test_imported_legacy_dither_mode_true_is_golden(self):
        data = m.export_calibration("Cam", m._factory(), {}, {})
        data["dither_temporal"] = True
        self.assertEqual(m.parse_calibration(data)[3]["dither_temporal"], "golden")

    def test_fallback_timeout_counts_as_a_stall(self):
        before = len(m.STALLS)
        with mock.patch.object(m.subprocess, "run", side_effect=m.subprocess.TimeoutExpired("xprop", 3)):
            self.assertIsNone(m._run(["xprop", "-root"], ":0"))
        self.assertEqual(len(m.STALLS), before + 1)
        m.STALLS.clear()
        m.STALLED_UNTIL = 0.0


class Review6(unittest.TestCase):
    def test_edits_for_a_profile_no_longer_active_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["profiles"] = {"A": m._factory(), "B": m._factory()}
            p.settings["profile"] = "B"
            p.settings["steps"] = m._factory()
            p._save_settings = lambda: None
            self.assertEqual(asyncio.run(p.set_tints([[1, 0, -5.0, "v"]], 1, "A")),
                             {"accepted": False, "profile": "B", "created": False})
            self.assertIsNone(p.settings["steps"][1]["v"][0])            # B untouched
            self.assertTrue(asyncio.run(p.set_tints([[1, 0, -5.0, "v"]], 1, "B"))["accepted"])
            self.assertEqual(p.settings["steps"][1]["v"][0], -5.0)

    def test_editing_factory_answers_with_the_new_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["profile"] = m.FACTORY
            p._save_settings = lambda: None
            self.assertEqual(asyncio.run(p.set_tints([[1, 0, -5.0, "v"]], 1, m.FACTORY)),
                             {"accepted": True, "profile": "Custom", "created": True})


class Review7(unittest.TestCase):
    def test_calibrated_steps_are_looked_up_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["dither_tables"] = {"310": {}, "96": {}}
            self.assertEqual(p._dither_key(310, m.Plugin._bin(310)), 310)    # bin alone: 321
            self.assertEqual(p._dither_key(95, m.Plugin._bin(95)), 96)
            self.assertEqual(p._dither_key(200, m.Plugin._bin(200)), m.Plugin._bin(200))

    def test_black_stays_black_in_the_shader(self):
        self.assertIn("t *= step(1e-6, o);", m.build_fx(m._neutral_curves() if hasattr(m, "_neutral_curves") else
                                                        (m._neutral(),) * len(m.KINDS)))

    def test_tables_with_another_hand_over_or_odd_keys_are_refused(self):
        a = {"V": 8, "L": {c: list(range(9)) for c in "RGB"}, "yref": [[8, 1.0]]}
        b = {**a, "V": 4, "L": {c: list(range(5)) for c in "RGB"}}
        self.assertEqual(set(m.consistent_tables({"96": a, "177": a, "310": b})), {"96", "177"})
        self.assertFalse(m._table_ok("0177", a))
        self.assertFalse(m._table_ok("177", {**a, "L": {**a["L"], "G": [None] + list(range(1, 9))}}))
        self.assertTrue(m._table_ok("177", {**a, "L": {**a["L"], "G": [0, -1, None, 3, 4, 5, 6, 7, 8]}}))


class Review8(unittest.TestCase):
    def test_save_keeps_what_the_camera_tool_wrote_meanwhile(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p._save_settings()                                  # file + base
            disk = _load(p.settings_path)
            disk["dither_tables"] = {"177": {"from": "camera"}}  # the camera tool writes
            time.sleep(0.01)
            _dump(disk, p.settings_path)
            os.utime(p.settings_path, (time.time() + 5, time.time() + 5))
            p.settings["dither"] = True                         # a UI toggle meanwhile
            p._save_settings()
            saved = _load(p.settings_path)
            self.assertEqual(saved["dither_tables"], {"177": {"from": "camera"}})
            self.assertTrue(saved["dither"])
            self.assertIsNone(p.settings_mtime)                 # read back next round

    def test_disabled_clears_an_effect_a_previous_instance_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.helper.atoms[m.RESHADE_ATOM] = m.FX_PREFIX + "previous.fx"
            p.settings["enabled"] = False
            p.owned_seen = True                                 # what the verify step saw
            p._refresh()
            self.assertNotIn(m.RESHADE_ATOM, p.helper.atoms)

    def test_steps_at_zero_or_not_finite_are_refused(self):
        bad0 = m._factory()
        bad0[0]["b"] = 0
        self.assertIsNone(m._parse_steps(bad0))
        badv = m._factory()
        badv[1]["v"][0] = float("inf")
        self.assertIsNone(m._parse_steps(badv))
        self.assertIsNotNone(m._parse_steps(m._factory()))


class AynStyle(unittest.TestCase):
    def test_gain_follows_the_measured_white(self):
        self.assertEqual(m.soft_gain(600, 543), 1.0)
        self.assertAlmostEqual(m.soft_gain(531, 531), 1.0)
        g = m.soft_gain(177, 543)
        self.assertTrue(0.4 < g < 0.5)                 # white at 177 is ~45% of 543's

    def test_held_at_the_floor_the_picture_is_dimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=177)
            p._read_brightness = lambda: m.SOFT_FLOOR
            p._refresh()
            self.assertTrue(0.4 < p.soft_now < 0.5)
            self.assertIsNotNone(p.applied)            # neutral curves, but dimmed: applied
            self.assertEqual(p.applied[0][5], ("L", m.gain_index(p.soft_now)))
            fx = _read(os.path.join(p.fx_dir, p.applied[1][m.RESHADE_ATOM]))
            self.assertNotIn("DitherW", fx)           # plain: no dither at the floor
            self.assertEqual(fx.count("technique G"), m.GAIN_STEPS)
            name, files = p.applied[1][m.RESHADE_ATOM], set(os.listdir(p.fx_dir))
            p.settings["soft_target"] = 120             # another gain: a technique switch, no new effect
            p.last_apply = 0.0
            p._refresh()
            self.assertEqual(p.applied[1][m.RESHADE_ATOM], name)
            self.assertEqual(set(os.listdir(p.fx_dir)), files)
            self.assertEqual(p.applied[0][5], ("L", m.gain_index(p.soft_now)))
            p.settings["soft_target"] = 900             # the user asks for more than the floor: nothing to do
            p._read_brightness = lambda: 900
            p._refresh()
            self.assertEqual(p.soft_now, 1.0)

    def test_handover_is_a_technique_switch_both_ways(self):
        # RESULTS 32 (video, 2026-10-30): slider 4% -> 5% (native 53 -> floor
        # dimming) flashed undimmed 543 for 130 ms-1 s, 5% -> 4% blacked out:
        # the crossing needed a new effect (native curves vs the floor's) and
        # waited for the compile throttle while the backlight had moved.
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=53)
            raw = [53]
            p._read_brightness = lambda: raw[0]
            p._refresh()                                # native bottom: the floor's effect, passing through
            self.assertIsNotNone(p.applied)
            self.assertFalse(p._soft_dimmed())
            self.assertEqual(p.applied[0][5], ("L", m.PASS_TECH))
            name, files = p.applied[1][m.RESHADE_ATOM], set(os.listdir(p.fx_dir))
            p.settings["soft_target"] = 57              # up: dimmed first, still at 53 ...
            p._refresh()
            dim = ("L", m.gain_index(m.soft_gain(57, m.SOFT_FLOOR)))
            self.assertEqual(p.applied[0][5], dim)
            self.assertTrue(p._soft_dimmed())          # the frontend may raise the backlight now
            raw[0] = m.SOFT_FLOOR                       # ... then the frontend raises the backlight
            p._refresh()
            self.assertEqual(p.applied[0][5], dim)
            p.settings["soft_target"] = 53              # down: the backlight drops ...
            raw[0] = 53
            p._refresh()
            self.assertEqual(p.applied[0][5], ("L", m.PASS_TECH))   # ... and the dimming lets go
            self.assertEqual(p.applied[1][m.RESHADE_ATOM], name)    # never a new effect: no compile
            self.assertEqual(set(os.listdir(p.fx_dir)), files)

    def test_pass_technique_leaves_the_picture_alone(self):
        fx = m.build_fx((m._neutral(),) * len(m.KINDS), gain=None)
        self.assertEqual(fx.count("technique G"), m.GAIN_STEPS)
        self.assertEqual(m.PASS_TECH, m.GAIN_STEPS)             # the technique after the ladder
        self.assertTrue(fx.rstrip().endswith("technique Pass { pass { VertexShader = VS; PixelShader = PSPass; } }"))
        self.assertIn("{ return float4(tex2D(ReShade::BackBuffer, uv).rgb, 1.0); }", fx)
        self.assertNotIn("technique Pass", m.build_fx((m._neutral(),) * len(m.KINDS)))

    def test_handover_down_passes_through_before_the_backlight_drops(self):
        # gamescope shows a technique switch ~3-4 frames after the atom, the
        # backlight follows within ~1: waiting for the drop blacked out for
        # 3-5 frames (stairs-video-t2). The pass-through goes first now.
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=57)
            p._save_settings = lambda: None
            p.wake = asyncio.Event()
            p._read_brightness = lambda: m.SOFT_FLOOR      # still at the floor
            p._refresh()
            self.assertEqual(p.applied[0][5], ("L", m.gain_index(m.soft_gain(57, m.SOFT_FLOOR))))
            name = p.applied[1][m.RESHADE_ATOM]
            asyncio.run(p.set_soft_target(53, "down"))
            self.assertEqual(p.applied[0][5], ("L", m.PASS_TECH))
            self.assertEqual(p.applied[1][m.RESHADE_ATOM], name)
            p.down_until = 0.0                              # the hint over, the backlight still up:
            p._refresh()                                    # back to the floor's own picture
            self.assertTrue(p.applied is None or p.applied[0][5] != ("L", m.PASS_TECH))

    def test_unsupported_device_pauses(self):
        with tempfile.TemporaryDirectory() as tmp:
            bl = os.path.join(tmp, "bl")
            os.makedirs(bl)
            with open(os.path.join(bl, "max_brightness"), "w") as f:
                f.write("4096")
            self.assertIsNone(m.unsupported_reason("AYN Odin 2 Portal", bl))
            self.assertIn("Odin 2 Mini", m.unsupported_reason("AYN Odin 2 Mini", bl))
            self.assertIsNotNone(m.unsupported_reason("AYN Odin 2 Portal", None))
            with open(os.path.join(bl, "max_brightness"), "w") as f:
                f.write("255")
            self.assertIn("0-255", m.unsupported_reason("AYN Odin 2 Portal", bl))
            p = make_plugin(tmp, engine="reshade")
            p.unsupported = "made for the AYN Odin 2 Portal, this is X"
            self.assertEqual(p._status()["state"], "paused")
            self.assertIn("Not supported", p._status()["text"])

    def test_floor_backlight_setting(self):
        # RESULTS 28: the band edge wandered above 543; the floor can move.
        self.assertEqual(m.floor_of({}), m.SOFT_FLOOR)
        self.assertEqual(m.floor_of({"floor_backlight": 600}), 600)
        self.assertEqual(m.floor_of({"floor_backlight": 400}), m.FLOOR_RANGE[0])    # never into the band
        self.assertEqual(m.floor_of({"floor_backlight": 5000}), m.FLOOR_RANGE[1])
        self.assertEqual(m.floor_of({"floor_backlight": "x"}), m.SOFT_FLOOR)
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=560, floor_backlight=600)
            p._read_brightness = lambda: 600
            p._refresh()
            self.assertAlmostEqual(p.soft_now, round(m.soft_gain(560, 600), 3))
            self.assertLess(p.soft_now, 1.0)
            p.settings.pop("floor_backlight")               # back at 543: 560 is above the floor
            p._read_brightness = lambda: 560
            p._refresh()
            self.assertEqual(p.soft_now, 1.0)

    def test_floor_dimming_keeps_the_flat_gain(self):
        # Review 2026-10-30: an anchor at the tables' hand-over code (64)
        # would leave everything below grey 64 undimmed.
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=177,
                              dither_tables={"177": pedestal_table(), "450": pedestal_table()})
            p._read_brightness = lambda: m.SOFT_FLOOR
            p._refresh()
            fx = _read(os.path.join(p.fx_dir, p.applied[1][m.RESHADE_ATOM]))
            self.assertIn("static const float Floor = 0.000000;", fx)
            self.assertNotIn("DitherW", fx)
            self.assertEqual(fx.count("technique G"), m.GAIN_STEPS)

    def test_build_scales_light(self):
        a = m.build_fx((m._neutral(),) * len(m.KINDS))
        b = m.build_fx((m._neutral(),) * len(m.KINDS), gain=0.25)
        self.assertNotEqual(a, b)
        self.assertIn(f"{0.25 ** (1 / 2.2):.6f}", b)    # white sample scaled

    def test_ladder_has_a_technique_per_gain(self):
        fx = m.build_fx((m._neutral(),) * len(m.KINDS), gain=None)
        self.assertEqual(fx.count("technique G"), m.GAIN_STEPS)
        self.assertIn("technique G0 ", fx)
        self.assertEqual(m.gain_index(1.0), 0)
        self.assertEqual(m.gain_index(0.5), round(-__import__("math").log(0.5) / __import__("math").log(m.GAIN_RATIO)))
        self.assertEqual(m.gain_index(1e-9), m.GAIN_STEPS - 1)


    def test_ladder_steps_are_fine_enough_to_slide_smoothly(self):
        # 2026-10-26: 7% light per step made a finger drag or D-pad hold jump visibly.
        self.assertLessEqual(m.GAIN_RATIO, 1.025)
        self.assertLess(m.GAIN_RATIO ** -(m.GAIN_STEPS - 1), 0.055)     # still reaches ~5% of the light

    def test_slider_log_writes_a_timestamped_line(self):
        with tempfile.TemporaryDirectory(prefix="oled-tint-tests-") as d:
            old, m.SLIDER_LOG = m.SLIDER_LOG, os.path.join(d, "slider.log")
            try:
                m._slider_log("F set 0.5")
                line = _read(m.SLIDER_LOG).split()
                self.assertGreater(float(line[0]), 1e9)
                self.assertEqual(" ".join(line[1:]), "F set 0.5")
                m.SLIDER_LOG = os.path.join(d, "no", "such", "dir")     # unwritable: never raises
                m._slider_log("x")
            finally:
                m.SLIDER_LOG = old


class Review10(unittest.TestCase):
    def test_failed_vibrance_restore_keeps_the_saved_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["vibrance_saved"] = "1056964608"
            p.vibrance_now = m.VIBRANCE_NATIVE
            p.helper = types.SimpleNamespace(run=lambda args: None)          # xprop failed
            p._sync_vibrance()
            self.assertEqual(p.settings["vibrance_saved"], "1056964608")   # kept for the retry

    def test_reimport_doesnt_overwrite_an_edited_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            path = os.path.join(tmp, "in.json")
            _dump(m.export_calibration("Cam", m._factory(), {}, {}), path)
            a = asyncio.run(p.import_calibration(path))["profile"]
            b = asyncio.run(p.import_calibration(path))["profile"]
            self.assertNotEqual(a, b)
            self.assertIn(a, p.settings["profiles"])

    def test_state_file_says_what_was_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            with mock.patch.object(m, "STATE_FILE", os.path.join(tmp, "state.json")):
                p.settings_mtime = 123.0
                p._publish_state()
                self.assertEqual(_load(os.path.join(tmp, "state.json"))["settings_mtime"], 123.0)


class Review11(unittest.TestCase):
    def test_missing_trim_blends_toward_identity(self):
        a = {"V": 8, "L": {c: list(range(9)) for c in "RGB"}, "yref": [[8, 1.0]],
             "trims": {"still": {"codes": [2, 4], "R": [2.0, 2.0], "G": [2.0, 2.0], "B": [2.0, 2.0]}}}
        b = {"V": 8, "L": {c: list(range(9)) for c in "RGB"}, "yref": [[8, 1.0]]}
        half = m._blend_tables(a, b, 0.5)["trims"]["still"]["R"][0]
        self.assertAlmostEqual(half, 2.0 ** 0.5, places=3)       # halfway in log, not 2.0
        self.assertAlmostEqual(m._blend_tables(a, b, 1.0)["trims"]["still"]["R"][0], 1.0, places=3)

    def test_deferred_apply_is_published_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings["steps"][0]["v"][0] = -40
            old = m.DEBOUNCE
            try:
                m.DEBOUNCE = 1000.0
                p.last_apply = time.monotonic()                 # just applied: this one waits
                p._refresh()
                self.assertTrue(p.deferred)
                p._publish_state()
                self.assertTrue(_load(m.STATE_FILE)["pending"])
            finally:
                m.DEBOUNCE = old


class Review12(unittest.TestCase):
    L = {c: list(range(17)) for c in "RGB"}

    def test_trims_on_different_grids_blend_by_code(self):
        a = {"V": 16, "L": self.L, "yref": [[16, 1.0]],
             "trims": {"pair": {"codes": [4, 8], "R": [4.0, 4.0], "G": [1, 1], "B": [1, 1]}}}
        b = {"V": 16, "L": self.L, "yref": [[16, 1.0]],
             "trims": {"pair": {"codes": [4, 6, 8], "R": [4.0, 4.0, 4.0], "G": [1, 1, 1], "B": [1, 1, 1]}}}
        t = m._blend_tables(a, b, 0.5)["trims"]["pair"]
        r = dict(zip(t["codes"], t["R"]))
        self.assertAlmostEqual(r[8], 4.0, places=3)             # grey 8 is 4 on both sides
        self.assertAlmostEqual(r[6], 4.0, places=3)

    def test_legacy_trim_survives_between_anchors(self):
        tr = {"codes": [4, 8], "R": [2.0, 2.0], "G": [2.0, 2.0], "B": [2.0, 2.0]}
        a = {"V": 16, "L": self.L, "yref": [[16, 1.0]], "trim": tr}
        b = {"V": 16, "L": self.L, "yref": [[16, 1.0]], "trim": tr}
        blended = m._blend_tables(a, b, 0.5)
        self.assertAlmostEqual(m._trim_at(m.trim_for(blended, "pair"), "R", 4, 16), 2.0, places=3)

    def test_unready_backend_reports_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.ready_since = None
            p.deferred = False
            p._refresh()
            self.assertTrue(p.deferred)


class Review14(unittest.TestCase):
    def test_failed_effect_write_is_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings["steps"][0]["v"][0] = -40
            run = p.helper.run
            p.helper.run = lambda args: None if "-set" in args else run(args)   # xprop fails
            p._refresh()
            self.assertIsNone(p.applied)
            self.assertTrue(p.deferred)

    def test_merge_carries_our_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["vibrance_saved"] = "1056964608"
            p._save_settings()
            disk = _load(p.settings_path)
            disk["dither_tables"] = {"177": {"from": "camera"}}
            _dump(disk, p.settings_path)
            os.utime(p.settings_path, (time.time() + 5, time.time() + 5))
            p.settings.pop("vibrance_saved")                   # given back meanwhile
            p._save_settings()
            saved = _load(p.settings_path)
            self.assertNotIn("vibrance_saved", saved)
            self.assertEqual(saved["dither_tables"], {"177": {"from": "camera"}})


class Review15(unittest.TestCase):
    def test_failed_cleanup_after_a_switch_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="look")
            p.settings["steps"][0]["v"][0] = -40
            p._refresh()                                             # look on screen
            self.assertIn(m.LOOK_ATOMS[0], p.helper.atoms)
            run = p.helper.run
            p.helper.run = lambda args: None if "-remove" in args else run(args)   # removals fail
            p.settings["engine"] = "reshade"
            p._refresh()
            self.assertTrue(p.deferred)                              # switch unfinished
            self.assertIn(m.LOOK_ATOMS[0], p.helper.atoms)
            p.helper.run = run                                       # removals work again
            p._refresh()
            self.assertFalse(p.deferred)
            self.assertNotIn(m.LOOK_ATOMS[0], p.helper.atoms)


class AynStyle2(unittest.TestCase):
    def test_only_inside_the_band(self):
        self.assertFalse(m.soft_wanted(50))          # below: the panel's own backlight
        self.assertTrue(m.soft_wanted(177))
        self.assertFalse(m.soft_wanted(600))

    def test_no_dither_in_ayn_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=177, dither=True, dither_tables={"177": pedestal_table()})
            p._read_brightness = lambda: 177                 # the slider passing through the band
            p.settings["steps"][2]["v"][9] = -40
            p._refresh()
            fx = _read(os.path.join(p.fx_dir, p.applied[1][m.RESHADE_ATOM]))
            self.assertNotIn("DitherW", fx)


class Review16(unittest.TestCase):
    def test_open_cleanup_doesnt_remove_the_engine_just_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="look")
            p.settings["steps"][0]["v"][0] = -40
            p._refresh()
            run = p.helper.run
            p.helper.run = lambda args: None if "-remove" in args else run(args)
            p.settings["engine"] = "reshade"
            p._refresh()                                     # look cleanup pending
            self.assertEqual(p.stale_engine, "look")
            p.helper.run = run
            p.settings["engine"] = "look"                    # back to look before cleanup worked
            p._refresh()
            self.assertTrue(all(a in p.helper.atoms for a in m.LOOK_ATOMS))
            self.assertIsNotNone(p.applied)


class AynStyle3(unittest.TestCase):
    def test_dims_from_wherever_the_backlight_is_above_the_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings.update(dimming="soft", soft_target=177)
            p._read_brightness = lambda: 600
            p._refresh()
            self.assertAlmostEqual(p.soft_now, m.soft_gain(177, 600), places=3)


class Overlay(unittest.TestCase):
    STEAM_OFF = "control=mangohud\nfsr_steam_sharpness=5\nno_display\n"
    STEAM_ON = "control=mangohud\nlegacy_layout=0\nfps\nframetime\n"

    def test_block_in_and_out_gives_steams_file_back(self):
        for steam in (self.STEAM_OFF, self.STEAM_ON):
            on = m.osd_merge(steam, True)
            self.assertIn(m.OSD_BEGIN, on)
            self.assertEqual(m.osd_merge(on, True), on)              # no second block
            self.assertEqual(m.osd_merge(on, False), steam)

    def test_steam_overlay_off_shows_the_block_alone(self):
        on = m.osd_merge(self.STEAM_OFF, True).splitlines()
        self.assertNotIn("no_display", on)
        self.assertIn("legacy_layout=0", on)
        self.assertNotIn("legacy_layout=0", m.osd_block(False))

    def test_steams_preset_keeps_its_layout(self):
        on = m.osd_merge("control=mangohud\npreset=2\n", True).splitlines()
        self.assertIn("preset=2", on)
        self.assertNotIn("horizontal", on)

    def test_steam_overlay_off_gets_our_strip(self):
        self.assertIn("horizontal", m.osd_merge(self.STEAM_OFF, True).splitlines())

    def test_colour_only_when_our_strip_is_alone(self):
        self.assertIn(f"text_color={m.OSD_COLORS['band']}", m.osd_merge(self.STEAM_OFF, True, "band"))
        self.assertNotIn("text_color", m.osd_merge("control=mangohud\npreset=2\n", True, "band"))

    def test_lines_and_config_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            cfg = os.path.join(tmp, "mangohud.cfg")
            _write(cfg, self.STEAM_OFF)
            p.mango_path, p.backlight_now = cfg, 177
            p.settings["osd"] = True
            with mock.patch.object(m, "OSD_TEXT", os.path.join(tmp, "osd.txt")):
                p._osd_sync()
                lines = _read(os.path.join(tmp, "osd.txt")).splitlines()
                self.assertEqual(len(lines), len(m.OSD_LABELS))
                self.assertIn("177", lines[1])
                self.assertIn(m.OSD_BEGIN, _read(cfg))
                p._osd_sync(force_off=True)
                self.assertEqual(_read(cfg), self.STEAM_OFF)


class Maths(unittest.TestCase):
    def test_levels(self):
        self.assertEqual([min(255, round(x * 256)) for x in m.LEVELS],
                         [4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 160, 192, 255])
        for x in m.LEVELS_V3:  # every old level still exists
            self.assertIn(x, m.LEVELS)

    def test_gain_directions_cancel(self):
        for t in (5, 30, 100, 200):
            self.assertAlmostEqual(m.tint_to_gain(-t) * m.tint_to_gain(t), 1.0)
            self.assertGreater(m.tint_to_gain(-t), 1)  # magenta lifts red/blue
            self.assertLess(m.tint_to_gain(t), 1)      # green lowers them
        self.assertEqual(m.tint_to_gain(0), 1)
        self.assertEqual(m.tint_to_gain(-1000), m.tint_to_gain(-m.MAX_TINT))  # clamped

    def test_corrections_keep_grey_luminance(self):
        for t, h in ((-60, 0), (60, 0), (-120, 40), (0, -70)):
            r, g, b = m.channel_scales(0.1, t, 0, h)
            y = sum(w * (0.1 * k) ** 2.2 for w, k in zip(m.LUMA, (r, g, b)))
            self.assertAlmostEqual(y, 0.1 ** 2.2)

    def test_tint_and_hue_ratios(self):
        r, g, b = m.channel_scales(0.1, -60)
        self.assertAlmostEqual(r, b)
        self.assertAlmostEqual(r / g, m.tint_to_gain(-60))
        r, g, b = m.channel_scales(0.1, -60, 0, 50)
        self.assertGreater(r, b)                     # + leans red
        self.assertAlmostEqual(r / b, 1.25 ** 2)
        self.assertAlmostEqual((r * b) ** 0.5 / g, m.tint_to_gain(-60))  # tint unchanged

    def test_lift_scales_all_channels(self):
        self.assertEqual(m.channel_scales(0.1, 0, 0), (1.0, 1.0, 1.0))
        for k in m.channel_scales(0.1, 0, -20):
            self.assertAlmostEqual(k, 0.8)
        self.assertEqual(m.channel_scales(0.1, 0, 999), m.channel_scales(0.1, 0, m.MAX_LIFT))

    def test_highlights_scale_down_instead_of_clipping(self):
        r, g, b = m.channel_scales(1.0, -30, 20, 30)
        self.assertAlmostEqual(max(r, g, b), 1.0)
        self.assertAlmostEqual((r * b) ** 0.5 / g, m.tint_to_gain(-30))

    def test_resolve_auto_rules(self):
        self.assertEqual(m.resolve(m._auto()), m._neutral())
        v = m._auto()
        v[0], v[3] = -40, -10
        r = m.resolve(v)
        self.assertEqual(r[:4], [-40, -30, -20, -10])   # linear between set points
        self.assertTrue(all(t == 0 for t in r[4:]))     # neutral above the last
        self.assertTrue(all(t == -20 for t in m.resolve(at(255, -20))))  # held below the first

    def test_neutral_outputs_are_identity(self):
        for pq in (False, True):
            n, rows = parse_cube(m.build_cube(neutral_curves(), pq))
            self.assertEqual(len(rows), n ** 3)
            for i, row in enumerate(rows):
                want = (i % n / (n - 1), i // n % n / (n - 1), i // (n * n) / (n - 1))
                for a, b in zip(row, want):
                    self.assertAlmostEqual(a, b, places=5)
        for curve in parse_fx(m.build_fx(neutral_curves())):
            self.assertEqual(len(curve), m.CURVE_SIZE)
            for i, v in enumerate(curve):
                self.assertAlmostEqual(v, i / (m.CURVE_SIZE - 1), places=5)

    def test_fx_resolves_levels_below_16(self):
        # 8 and 16 set differently: the shader curve keeps them apart.
        v = at(16, -40)
        v[[min(255, round(x * 256)) for x in m.LEVELS].index(8)] = -120
        curves = (m.resolve(v), m._neutral(), m._neutral())
        r, g, b = parse_fx(m.build_fx(curves))
        ratio = lambda i: r[i] / g[i]
        # Samples sit on 8-bit codes, levels on sixteenths: close, not equal.
        self.assertAlmostEqual(ratio(8), m.tint_to_gain(-120), delta=0.02)
        self.assertAlmostEqual(ratio(16), m.tint_to_gain(-40), delta=0.02)

    def test_crush_blacks_out_near_black_and_ramps_back(self):
        curves = (m.resolve(at(16, -100)), m._neutral(), m._neutral())
        plain = parse_fx(m.build_fx(curves))
        crushed = parse_fx(m.build_fx(curves, 12))
        for c in range(3):
            self.assertTrue(all(v == 0 for v in crushed[c][:13]))        # codes 0..12 off
            self.assertAlmostEqual(crushed[c][14], plain[c][14] * 0.5, places=5)  # halfway up the ramp
            self.assertEqual(crushed[c][16:], plain[c][16:])             # untouched above

    def test_brightness_interpolation(self):
        steps = [{"b": 40, "v": at(4, -40), "l": at(4, 10), "h": at(4, 20)},
                 {"b": 320, **{k: m._auto() for k in m.KINDS}}]
        self.assertEqual(m.interpolate(steps, 20)[0][0], -40)
        self.assertEqual(m.interpolate(steps, 20)[1][0], 10)
        self.assertEqual(m.interpolate(steps, 20)[2][0], 20)
        self.assertAlmostEqual(m.interpolate(steps, 113)[0][0], -20, delta=0.5)
        self.assertEqual(m.interpolate(steps, 4096), neutral_curves())


# The green subpixel at step 177 as the camera measured it: almost off at
# code 1, then a jump to a pedestal (codes between interpolated).
_G = {0: 0, 1: 10.7, 2: 262.4, 3: 280.3, 4: 294.5, 5: 308.1, 6: 324.2, 8: 351.7, 10: 392.6,
      12: 413.9, 16: 490.3, 20: 576.6, 24: 682.7, 32: 938.4}


def pedestal_table(V=32):
    import bisect
    ks = sorted(_G)
    def at(c):
        i = bisect.bisect_left(ks, c)
        if ks[i] == c:
            return _G[c]
        a, b = ks[i - 1], ks[i]
        return _G[a] + (_G[b] - _G[a]) * (c - a) / (b - a)
    L = [at(c) for c in range(V + 1)]
    yref = [[c, (c / 255) ** 2.2] for c in (4, 8, 16, 32, 64, 128, 255)]
    return {"V": V, "L": {"R": L, "G": L, "B": L}, "yref": yref}


class Review(unittest.TestCase):
    """The external review (2026-10-25)."""

    def test_editing_factory_doesnt_overwrite_an_existing_custom(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.settings["profiles"]["Custom"] = copy.deepcopy(p.settings["steps"])
            p.settings["profiles"]["Custom"][1]["v"][0] = -77.0
            p.settings["profile"] = m.FACTORY
            asyncio.run(p.set_tints([[1, 0, -5.0, "v"]], 1))
            self.assertEqual(p.settings["profile"], "Custom 2")
            self.assertEqual(p.settings["profiles"]["Custom"][1]["v"][0], -77.0)

    def test_crush_works_with_flat_curves(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            seen = []
            p.helper = types.SimpleNamespace(write=lambda path, curves, kind, crush=0, dither=None, gain=1.0: seen.append(crush),
                                             run=lambda args: None)
            p.settings.update(engine="reshade", crush=6)
            p._read_brightness = lambda: 177
            p._refresh()
            self.assertEqual(seen[-1:], [6])

    def test_open_menu_goes_stale_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.wake = types.SimpleNamespace(set=lambda: None)
            clock = [100.0]
            with mock.patch.object(m.time, "monotonic", lambda: clock[0]), \
                    mock.patch.object(m.asyncio, "get_event_loop", lambda: types.SimpleNamespace(time=lambda: clock[0])):
                asyncio.run(p.set_overlay(True))
                clock[0] += m.MENU_OPEN_HOLD + 0.01
                self.assertTrue(p._menu_engine())
                clock[0] += m.MENU_STALE + m.MENU_CLOSE_HOLD + m.ENGINE_SWITCH_GAP
                self.assertFalse(p._menu_engine())           # no confirmation: counts as closed

    def test_bad_settings_value_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            _dump({"enabled": False, "crush": "lots"}, p.settings_path)
            p.settings["enabled"] = True
            p.settings_mtime = None
            p._load_settings()
            self.assertTrue(p.settings["enabled"])            # not half-applied

    def test_broken_dither_table_is_dropped(self):
        good, short = pedestal_table(), pedestal_table()
        short["L"] = {c: v[:5] for c, v in short["L"].items()}
        bad_trim = dict(pedestal_table(), trim={"codes": [8], "R": [0.0], "G": [1.0], "B": [1.0]})
        self.assertTrue(m._table_ok("177", good))
        self.assertFalse(m._table_ok("177", short))
        self.assertFalse(m._table_ok("177", bad_trim))
        self.assertFalse(m._table_ok("x", good))


class Sharing(unittest.TestCase):
    def test_export_then_import_on_another_portal(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            src = make_plugin(a)
            src.settings["steps"][1]["v"][0] = -123.0
            src.settings["profile"] = "Camera"
            src.settings["profiles"]["Camera"] = copy.deepcopy(src.settings["steps"])
            src.settings["dither_tables"] = {"177": pedestal_table()}
            src.settings["shadow_curve"] = "g22"
            with mock.patch.object(src, "_downloads", lambda: a):
                path = asyncio.run(src.export_calibration())
            dst = make_plugin(b)
            res = asyncio.run(dst.import_calibration(path))
            self.assertTrue(res["ok"], res)
            self.assertEqual(dst.settings["profile"], "Camera (imported)")
            self.assertEqual(dst.settings["steps"][1]["v"][0], -123.0)
            self.assertIn("177", dst.settings["dither_tables"])
            self.assertEqual(dst.settings["shadow_curve"], "g22")

    def test_import_refuses_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            bad = os.path.join(tmp, "x.json")
            _dump({"hello": 1}, bad)
            res = asyncio.run(p.import_calibration(bad))
            self.assertFalse(res["ok"])
            self.assertIn("not an OLED Tint calibration", res["error"])


class Seeding(unittest.TestCase):
    def test_bundled_defaults_seed_a_first_start_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            bundled = os.path.join(tmp, "defaults.json")     # never next to the real main.py
            with mock.patch.object(m, "DEFAULTS_FILE", bundled):
                with open(bundled, "w") as f:
                    json.dump({"simple": True, "profile": "Friend", "profiles": {"Friend": m._factory()},
                               "steps": m._factory()}, f)
                p._seed_defaults()
                p._load_settings()
                self.assertTrue(p.settings["simple"])
                self.assertEqual(p.settings["profile"], "Friend")
                p.settings["simple"] = False
                p._save_settings()
                p._seed_defaults()                      # existing settings win
                p.settings_mtime = None
                p._load_settings()
                self.assertFalse(p.settings["simple"])


class Dither(unittest.TestCase):
    def test_average_light_follows_the_reference_curve(self):
        table = pedestal_table()
        lo, hi, fr = m.dither_tables(neutral_curves(), 0, table)
        L = table["L"]["G"]
        for code in (2, 4, 8, 12, 16, 24, 31):
            k, f = int(lo[1][code]), fr[1][code]
            mixed = L[k] * (1 - f) + L[int(hi[1][code])] * f
            want = L[32] * (code / 32) ** 2.2
            self.assertAlmostEqual(mixed, want, delta=want * 0.02 + 0.5, msg=f"code {code}")
        self.assertEqual(lo[1][40], -1.0)  # above V: not dithered
        self.assertEqual(lo[1][0], -1.0)   # black stays black

    def test_trim_scales_one_channel_and_fades_out_at_V(self):
        table = pedestal_table()
        base = m.dither_tables(neutral_curves(), 0, table)
        table["trim"] = {"codes": [8, 16], "R": [1.0, 1.0], "G": [0.5, 0.8], "B": [1.0, 1.0]}
        trimmed = m.dither_tables(neutral_curves(), 0, table)
        L = table["L"]["G"]
        light = lambda t, c: L[int(t[0][1][c])] * (1 - t[2][1][c]) + L[int(t[1][1][c])] * t[2][1][c]
        self.assertAlmostEqual(light(trimmed, 8), 0.5 * light(base, 8), delta=0.5)
        self.assertAlmostEqual(light(trimmed, 4), 0.5 * light(base, 4), delta=0.5)  # held below
        self.assertAlmostEqual(light(trimmed, 16), 0.8 * light(base, 16), delta=0.5)
        self.assertEqual([t[0] for t in trimmed], [t[0] for t in base])   # red untouched
        self.assertAlmostEqual(m._trim_at(table["trim"], "G", 32, 32), 1.0)

    def test_mixes_only_rungs_brighter_than_all_below(self):
        table = pedestal_table()
        L = list(table["L"]["G"])
        L[6] = L[4] * 0.9          # darker than code 4: useless
        L[7] = -1.0                # not measured
        table["L"] = {"R": L, "G": L, "B": L}
        self.assertNotIn(6, m._rungs(L, table["V"]))
        self.assertNotIn(7, m._rungs(L, table["V"]))
        lo, hi, fr = m.dither_tables(neutral_curves(), 0, table)
        for code in range(1, table["V"]):
            if lo[1][code] >= 0:
                self.assertNotIn(int(lo[1][code]), (6, 7))
                self.assertNotIn(int(hi[1][code]), (6, 7))
                mixed = L[int(lo[1][code])] * (1 - fr[1][code]) + L[int(hi[1][code])] * fr[1][code]
                want = L[table["V"]] * (code / table["V"]) ** 2.2
                self.assertAlmostEqual(mixed, want, delta=want * 0.02 + 0.5, msg=f"code {code}")

    def test_bluenoise_pngs_hold_every_rank_level_evenly(self):
        for cell in (1, 2, 4):
            w, h = 64, 64 // cell
            png = m.bluenoise_png(cell)
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", png[16:24]), (w, h))
            idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
            rows = zlib.decompress(idat)
            self.assertEqual(len(rows), h * (w + 1))
            self.assertTrue(all(rows[y * (w + 1)] == 0 for y in range(h)))    # filter byte: none
            counts = collections.Counter(b"".join(rows[y * (w + 1) + 1:(y + 1) * (w + 1)] for y in range(h)))
            self.assertEqual(set(counts), set(range(256)))
            self.assertEqual(set(counts.values()), {w * h // 256}, msg=f"cell {cell}")

    def test_bluenoise_is_written_beside_shaders_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            shaders = os.path.join(tmp, "reshade", "Shaders")
            os.makedirs(shaders)
            paths = m.write_bluenoise(shaders)
            self.assertIn(os.path.join(tmp, "reshade", "Textures", m.noise_file(4)), paths)
            mtimes = [os.path.getmtime(p) for p in paths]
            time.sleep(0.01)
            m.write_bluenoise(shaders)
            self.assertEqual([os.path.getmtime(p) for p in paths], mtimes)   # unchanged: not rewritten

    def test_temporal_dither_reads_the_frame_counter_only_when_asked(self):
        still = m.build_fx(neutral_curves(), 0, {"table": pedestal_table(), "w": 1.0})
        self.assertNotIn("framecount", still)
        golden = m.build_fx(neutral_curves(), 0, {"table": pedestal_table(), "w": 1.0, "temporal": "golden"})
        self.assertIn('uniform int FrameCount < source = "framecount"; >;', golden)
        self.assertIn("n = frac(n + 0.61803398875", golden)
        pair = m.build_fx(neutral_curves(), 0, {"table": pedestal_table(), "w": 1.0, "temporal": "pair"})
        self.assertIn("n = frac(n + 0.5 * float(FrameCount % 2));", pair)

    def test_dither_holds_still_in_steams_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            seen = []
            p.helper = types.SimpleNamespace(write=lambda path, curves, kind, crush=0, dither=None, gain=1.0: seen.append(dither),
                                             run=lambda args: None)
            p.settings.update(dither=True, dither_temporal="pair", dither_tables={"177": pedestal_table()})
            p.backlight_now = 177
            for focused, want in (("769", None), ("2333000", "pair")):
                p.focused = focused
                p.applied = None
                p._apply(neutral_curves(), "reshade")
                self.assertEqual(seen[-1]["temporal"], want, msg=focused)

    def test_vibrance_held_at_native_while_dithering_and_given_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            calls = []
            p.helper = types.SimpleNamespace(write=lambda *a, **k: None, run=lambda args: calls.append(args) or m._Result(0, ""))
            p.settings.update(dither=True, dither_tables={"177": pedestal_table()})
            p.backlight_now = 177
            p.vibrance_now = "1056964608"                         # 0.5, the user's
            p.applied = None
            p._apply([[-100.0] * 16, [0.0] * 16, [0.0] * 16], "reshade")
            p.dithering = True
            p._sync_vibrance()
            self.assertIn(["xprop", "-root", "-f", m.VIBRANCE_ATOM, "32c", "-set", m.VIBRANCE_ATOM, m.VIBRANCE_NATIVE], calls)
            self.assertEqual(_load(p.settings_path)["vibrance_saved"], "1056964608")
            # Steam moves the slider mid-hold: that's the new value to give back.
            p.vibrance_now = "0"
            p._sync_vibrance()
            self.assertEqual(p.settings["vibrance_saved"], "0")
            self.assertEqual(p.vibrance_now, m.VIBRANCE_NATIVE)
            # Dither off: back to the user's value, nothing saved any more.
            p.dithering = False
            p._sync_vibrance()
            self.assertEqual(calls[-1], ["xprop", "-root", "-f", m.VIBRANCE_ATOM, "32c", "-set", m.VIBRANCE_ATOM, "0"])
            self.assertNotIn("vibrance_saved", p.settings)
            # Already native and nothing saved: leave it alone.
            calls.clear()
            p.vibrance_now = m.VIBRANCE_NATIVE
            p.dithering = True
            p._sync_vibrance()
            self.assertEqual(calls, [])

    def test_vibrance_given_back_when_another_effect_takes_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            calls = []
            p.helper = types.SimpleNamespace(write=lambda *a, **k: None, run=lambda args: calls.append(args) or m._Result(0, ""))
            p.settings["vibrance_saved"] = "1056964608"
            p.vibrance_now = m.VIBRANCE_NATIVE
            p.dithering = True
            p.conflict = True
            p._refresh()
            self.assertEqual(calls[-1][-1], "1056964608")
            self.assertNotIn("vibrance_saved", p.settings)

    def test_menu_switch_waits_holds_and_rate_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.wake = types.SimpleNamespace(set=lambda: None)
            clock = [100.0]
            with mock.patch.object(m.time, "monotonic", lambda: clock[0]), \
                    mock.patch.object(m.asyncio, "get_event_loop", lambda: types.SimpleNamespace(time=lambda: clock[0])):
                asyncio.run(p.set_overlay(True))
                self.assertFalse(p._menu_engine())          # a flick of the menu: not yet
                clock[0] += m.MENU_OPEN_HOLD + 0.01
                self.assertTrue(p._menu_engine())           # open long enough: the look
                asyncio.run(p.set_overlay(False))
                clock[0] += 0.5
                self.assertTrue(p._menu_engine())           # closed only briefly: stay
                clock[0] += m.MENU_CLOSE_HOLD + 0.01
                self.assertFalse(p._menu_engine())          # closed for good: back to ReShade
                asyncio.run(p.set_overlay(True))
                clock[0] += m.MENU_OPEN_HOLD + 0.01
                self.assertFalse(p._menu_engine())          # within ENGINE_SWITCH_GAP: wait
                clock[0] += m.ENGINE_SWITCH_GAP + 0.01
                self.assertTrue(p._menu_engine())
                p.settings["menu_correction"] = False
                self.assertFalse(p._menu_engine())          # toggle off: never
                p.settings["menu_correction"] = True
                # The menu over a game hands the focus to Steam's UI: still the game.
                p.focused, p.focus_since = m.STEAM_UI_APP, clock[0] + 0.2
                clock[0] += 1.0
                self.assertTrue(p._menu_engine())
                # Steam's UI in front before the menu opened: its own menus, ReShade covers them.
                asyncio.run(p.set_overlay(False))
                clock[0] += m.MENU_CLOSE_HOLD + m.ENGINE_SWITCH_GAP + 0.01
                self.assertFalse(p._menu_engine())
                asyncio.run(p.set_overlay(True))
                clock[0] += m.MENU_OPEN_HOLD + m.ENGINE_SWITCH_GAP + 0.01
                self.assertFalse(p._menu_engine())

    def test_safe_mode_after_repeated_stalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            p.helper = types.SimpleNamespace(write=lambda *a, **k: None, run=lambda args: None)
            p.settings.update(dither=True, dither_tables={"177": pedestal_table()})
            p.backlight_now = 177
            saved = list(m.STALLS)
            try:
                m.STALLS[:] = [time.monotonic() - 5] * m.SAFE_STALLS
                with mock.patch.object(m, "STALLED_UNTIL", 0.0):
                    p._refresh()
                self.assertTrue(p.safe_mode)
                self.assertTrue(asyncio.run(p.get_state())["safe_mode"])
                asyncio.run(p.clear_safe_mode())
                self.assertFalse(p.safe_mode)
            finally:
                m.STALLS[:] = saved

    def test_nothing_set_until_gamescope_answers_steadily(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            calls = []
            p.helper = types.SimpleNamespace(write=lambda *a, **k: calls.append(a), run=lambda args: calls.append(args))
            p.settings["steps"][0]["v"][0] = -40
            p.ready_since = None
            p._refresh()
            self.assertEqual(calls, [])

    def test_xprop_timeout_pauses_the_plugin(self):
        h = m.Helper(":0")
        h.call = lambda req, timeout=5.0: {"ok": False, "error": "Command 'xprop' timed out after 3 seconds"}
        before = m.STALLED_UNTIL
        try:
            self.assertIsNone(h.run(["xprop", "-root"]))
            self.assertGreater(m.STALLED_UNTIL, time.monotonic())
        finally:
            m.STALLED_UNTIL = before

    def test_temporal_setting_reads_old_and_new_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            for stored, want in ((True, "golden"), ("pair", "pair"), ("golden", "golden"), ("nonsense", False), (False, False)):
                _dump({"dither_temporal": stored}, p.settings_path)
                p.settings_mtime = None
                p._load_settings()
                self.assertEqual(p.settings["dither_temporal"], want, msg=repr(stored))

    def test_each_dither_mode_takes_its_own_trim(self):
        table = pedestal_table()
        table["trim"] = {"codes": [8], "R": [1.0], "G": [1.0], "B": [1.0]}
        table["trims"] = {"pair": {"codes": [8], "R": [1.0], "G": [0.5], "B": [1.0]}}
        self.assertIs(m.trim_for(table, "pair"), table["trims"]["pair"])
        self.assertIs(m.trim_for(table, "still"), table["trim"])          # no own trim yet: the old one
        still = m.dither_tables(neutral_curves(), 0, table, "still")
        pair = m.dither_tables(neutral_curves(), 0, table, "pair")
        L = table["L"]["G"]
        light = lambda t: L[int(t[0][1][8])] * (1 - t[2][1][8]) + L[int(t[1][1][8])] * t[2][1][8]
        self.assertAlmostEqual(light(pair), 0.5 * light(still), delta=0.5)

    def test_shadow_curve_g22_targets_a_pure_power_law(self):
        table = pedestal_table()
        table["yref"] = [[c, (c / 255) ** 2.0] for c in (4, 8, 16, 32, 64, 128, 255)]   # a lifted panel
        L = table["L"]["G"]
        V = table["V"]
        for curve, gamma in (("g22", 2.2), ("native", 2.0)):
            lo, hi, fr = m.dither_tables(neutral_curves(), 0, table, "still", curve)
            for code in (4, 8, 16):
                mixed = L[int(lo[1][code])] * (1 - fr[1][code]) + L[int(hi[1][code])] * fr[1][code]
                want = L[V] * (code / V) ** gamma
                self.assertAlmostEqual(mixed, want, delta=want * 0.03 + 0.5, msg=f"{curve} {code}")

    def test_shader_has_dithering_only_when_asked(self):
        plain = m.build_fx(neutral_curves())
        self.assertNotIn("DitherW", plain)
        d = m.build_fx(neutral_curves(), 0, {"table": pedestal_table(), "w": 1.0})
        self.assertIn("static const float DLoG[256]", d)
        self.assertIn("dither(c.g, o.g, t.g, 1, pos.xy)", d)
        self.assertIn("DitherCell = 1.0;", d)
        self.assertIn(f'source = "{m.noise_file(1)}"', d)
        self.assertNotIn("ign(", d)
        cell = dict(pedestal_table(), cell=2)
        fx = m.build_fx(neutral_curves(), 0, {"table": cell, "w": 1.0})
        self.assertIn("DitherCell = 2.0;", fx)
        self.assertIn(f'source = "{m.noise_file(2)}"', fx)
        self.assertIn("Height = 32;", fx)
        odd = m.build_fx(neutral_curves(), 0, {"table": dict(pedestal_table(), cell=3), "w": 1.0})
        self.assertIn("DitherCell = 1.0;", odd)                  # no mask for 3: square cells

    def test_between_two_tables_a_blend_at_full_weight(self):
        a, b = pedestal_table(), pedestal_table()
        b["L"] = {c: [x * (1.5 if 0 < i < 16 else 1.0) for i, x in enumerate(row)] for c, row in b["L"].items()}
        tables = {"96": a, "177": b}
        self.assertEqual(m.dither_weight(tables, 96), (a, 1.0))           # at a step: its own table
        self.assertEqual(m.dither_weight(tables, 177), (b, 1.0))
        table, w = m.dither_weight(tables, 130)
        self.assertEqual(w, 1.0)
        for i in (2, 4, 8):                                               # in between, per code
            self.assertGreater(table["L"]["G"][i], a["L"]["G"][i])
            self.assertLess(table["L"]["G"][i], b["L"]["G"][i])
        self.assertIs(m.dither_weight(tables, 130)[0], table)             # cached: same object, same effect name
        self.assertEqual(m.dither_weight(tables, 600), (None, 0.0))       # outside the band: nothing

    def test_full_weight_across_the_bad_band_none_outside(self):
        tables = {"177": pedestal_table()}
        for b in (66, 92, 177, 384, 530):
            self.assertEqual(m.dither_weight(tables, b), (tables["177"], 1.0), msg=b)
        for b in (52, 62, 535, 545, 900):
            self.assertEqual(m.dither_weight(tables, b), (None, 0.0), msg=b)
        # Band membership on the raw backlight, whatever the bin says.
        self.assertEqual(m.dither_weight(tables, 549, raw=528)[1], 1.0)
        self.assertEqual(m.dither_weight(tables, 508, raw=535)[1], 0.0)

    def test_plugin_dithers_only_with_reshade_and_a_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp, engine="reshade")
            p.settings["steps"][2]["v"][9] = -40
            p.settings.update(dither=True, dither_tables={"177": pedestal_table()})
            p._read_brightness = lambda: 177
            p._refresh()
            name = p.applied[1][m.RESHADE_ATOM]
            self.assertIn("DitherW", _read(os.path.join(p.fx_dir, name)))
            p.settings["dither"] = False
            p._refresh()
            name = p.applied[1][m.RESHADE_ATOM]
            self.assertNotIn("DitherW", _read(os.path.join(p.fx_dir, name)))


class SettingsFile(unittest.TestCase):
    def test_migrates_10_level_layout_without_changing_the_curve(self):
        old = [-100.0, -42.0, N, -14.0, -9.0, -4.0, N, -2.0, N, N]
        old_l = [-30.0] + [N] * 9
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            with open(p.settings_path, "w") as f:
                json.dump({"enabled": True, "steps": [{"b": 52, "v": list(old), "l": list(old_l)}]}, f)
            p._load_settings()
            step = p.settings["steps"][0]
        self.assertEqual(len(step["v"]), L)
        self.assertEqual(step["h"], m._auto())
        saved = m.LEVELS[:]
        try:
            m.LEVELS[:] = m.LEVELS_V3
            a = [(m.tint_at(m.resolve(old), x / 255), m.tint_at(m.resolve(old_l), x / 255)) for x in range(256)]
        finally:
            m.LEVELS[:] = saved
        b = [(m.tint_at(m.resolve(step["v"]), x / 255), m.tint_at(m.resolve(step["l"]), x / 255))
             for x in range(256)]
        for x, y in zip(a, b):
            self.assertAlmostEqual(x[0], y[0])
            self.assertAlmostEqual(x[1], y[1])

    def test_guessed_steps_move_to_measured_ones(self):
        old = [{"b": b, "v": [float(b)] + [N] * 9, "l": [N] * 10} for b in (40, 100, 320, 800, 2000, 4096)]
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            with open(p.settings_path, "w") as f:
                json.dump({"enabled": True, "steps": old}, f)
            p._load_settings()
            got = {s["b"]: m.resolve(s["v"])[0] for s in p.settings["steps"]}
        self.assertEqual(sorted(got), m.DEFAULT_STEPS)
        self.assertEqual(got[96], 100.0)   # old 100 carried over
        self.assertEqual(got[310], 320.0)  # old 320 carried over
        for b in (52, 177, 543, 4096):
            self.assertEqual(got[b], 0.0)

    def test_unknown_layout_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            with open(p.settings_path, "w") as f:
                json.dump({"steps": [{"b": 40, "v": [1, 2, 3, 4, 5]}]}, f)
            p._load_settings()
            self.assertEqual(len(p.settings["steps"]), len(m.DEFAULT_STEPS))
            self.assertEqual(p.settings["steps"][0]["v"], m._auto())

    def test_values_survive_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            asyncio.run(p.set_tints([[0, 2, -7.5, "v"], [0, 1, 12, "l"], [0, 5, 999, "h"]], 0))
            q = make_plugin(tmp)
            q._load_settings()
            step = q.settings["steps"][0]
            self.assertEqual(step["v"][2], -7.5)
            self.assertEqual(step["l"][1], 12)
            self.assertEqual(step["h"][5], m.MAX_HUE)  # clamped
            self.assertIsNone(step["v"][0])


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = make_plugin(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, data):
        with open(self.p.settings_path, "w") as f:
            json.dump(data, f)
        self.p.settings_mtime = None
        self.p._load_settings()

    def test_existing_calibration_becomes_eye_profile(self):
        steps = m._factory()
        steps[2]["v"][3] = -150
        self.load({"enabled": True, "steps": steps})
        self.assertEqual(self.p.settings["profile"], "Eye")
        self.assertEqual(self.p.settings["profiles"]["Eye"][2]["v"][3], -150)

    def test_eye_profile_is_written_to_disk_at_once(self):
        steps = m._factory()
        steps[2]["v"][3] = -150
        self.load({"enabled": True, "steps": steps})
        on_disk = _load(self.p.settings_path)
        self.assertEqual(on_disk["profile"], "Eye")
        self.assertEqual(on_disk["profiles"]["Eye"][2]["v"][3], -150)

    def test_switching_profiles(self):
        eye, cam = m._factory(), m._factory()
        eye[2]["v"][3], cam[2]["v"][3] = -150, -40
        self.load({"steps": eye, "profile": "Eye", "profiles": {"Eye": eye, "Camera": cam}})
        state = asyncio.run(self.p.set_profile("Camera"))
        self.assertEqual(state["profiles"], ["Factory", "Camera", "Eye"])
        self.assertEqual(self.p.settings["steps"][2]["v"][3], -40)
        asyncio.run(self.p.set_profile("Factory"))
        self.assertEqual(self.p.settings["steps"], m._factory())
        asyncio.run(self.p.set_profile("Eye"))
        self.assertEqual(self.p.settings["steps"][2]["v"][3], -150)
        asyncio.run(self.p.set_profile("Nonsense"))  # ignored
        self.assertEqual(self.p.settings["profile"], "Eye")

    def test_edits_follow_the_active_profile(self):
        self.load({"steps": m._factory(), "profile": "Eye", "profiles": {"Eye": m._factory()}})
        asyncio.run(self.p.set_tints([[2, 3, -77, "v"]], 2))
        self.load(_load(self.p.settings_path))
        self.assertEqual(self.p.settings["profiles"]["Eye"][2]["v"][3], -77)

    def test_outside_edits_to_steps_reach_the_active_profile(self):
        cam = m._factory()
        steps = m._factory()
        steps[2]["v"][3] = -333  # written by the camera tool into the working steps
        self.load({"steps": steps, "profile": "Camera", "profiles": {"Eye": m._factory(), "Camera": cam}})
        asyncio.run(self.p.set_profile("Eye"))
        asyncio.run(self.p.set_profile("Camera"))
        self.assertEqual(self.p.settings["steps"][2]["v"][3], -333)

    def test_stale_profile_copy_is_written_back(self):
        steps = m._factory()
        steps[2]["v"][3] = -310
        self.load({"steps": steps, "profile": "Camera", "profiles": {"Camera": m._factory()}})
        on_disk = _load(self.p.settings_path)
        self.assertEqual(on_disk["profiles"]["Camera"][2]["v"][3], -310)

    def test_crush_is_saved_and_clamped(self):
        state = asyncio.run(self.p.set_crush(99))
        self.assertEqual(state["crush"], m.MAX_CRUSH)
        self.load(_load(self.p.settings_path))
        self.assertEqual(self.p.settings["crush"], m.MAX_CRUSH)

    def test_editing_factory_starts_custom(self):
        self.load({"steps": m._factory(), "profile": "Factory", "profiles": {"Eye": m._factory()}})
        asyncio.run(self.p.set_tints([[2, 3, -10, "v"]], 2))
        self.assertEqual(self.p.settings["profile"], "Custom")
        self.assertEqual(self.p.settings["profiles"]["Custom"][2]["v"][3], -10)
        asyncio.run(self.p.set_profile("Factory"))
        self.assertEqual(self.p.settings["steps"], m._factory())  # still untouched


class Applying(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = make_plugin(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sets(self):
        return [c[3] for c in self.p.helper.calls if "-set" in c]

    def removes(self):
        return [c[3] for c in self.p.helper.calls if "-remove" in c]

    def test_look_preview_writes_both_looks(self):
        # HDR output shows SDR content (Steam's UI) through the PQ look: an
        # SDR-only preview left the old PQ table on screen (fifth review).
        self.p.preview = {k: m._auto() for k in m.KINDS}
        asyncio.run(self.p.set_tints([[0, 0, -40, "v"]], 0))
        self.assertFalse(self.p.applied[0][2])
        self.assertEqual(set(self.sets()), set(m.LOOK_ATOMS))

    def test_look_writes_both_and_clears_when_neutral(self):
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        self.assertEqual(set(self.sets()), set(m.LOOK_ATOMS))
        self.p.settings["steps"][0]["v"][0] = None
        self.p._refresh()
        self.assertIsNone(self.p.applied)
        self.assertIn(m.LOOK_ATOMS[0], self.removes())

    def test_reshade_writes_effect_and_clears_look(self):
        self.p.helper.atoms[m.LOOK_ATOMS[0]] = os.path.join(self.p.lut_dir, "old.cube")   # our earlier look
        self.p.settings["engine"] = "reshade"
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        name = self.p.applied[1][m.RESHADE_ATOM]
        self.assertTrue(name.startswith(m.FX_PREFIX))
        self.assertIn(m.RESHADE_ATOM, self.sets())
        self.assertIn(m.LOOK_ATOMS[0], self.removes())
        with open(os.path.join(self.p.fx_dir, name)) as f:
            self.assertIn("technique OledTint", f.read())

    def test_crush_change_rebuilds_the_effect(self):
        self.p.settings["engine"] = "reshade"
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        first = self.p.applied[1][m.RESHADE_ATOM]
        self.p.settings["crush"] = 10
        self.p._refresh()
        self.assertNotEqual(self.p.applied[1][m.RESHADE_ATOM], first)

    def test_app_effect_takes_reshade_and_correction_moves_to_look(self):
        self.p.settings["engine"] = "reshade"
        self.p.settings["app_effects"] = {"2333000": {"effect": "OledVHS.fx", "technique": 5}}
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        self.assertIn(m.RESHADE_ATOM, self.p.applied[1])        # our effect, outside the game
        self.p.focused = "2333000"
        self.p._refresh()
        self.assertEqual(self.p.app_on["effect"], "OledVHS.fx")
        self.assertIn(m.LOOK_ATOMS[0], self.p.applied[1])       # correction via look
        sets = [(c[3], c[-1]) for c in self.p.helper.calls if "-set" in c]
        self.assertIn((m.RESHADE_ATOM, "OledVHS.fx"), sets)
        self.assertIn((m.RESHADE_TECHNIQUE_ATOM, "5"), sets)
        self.assertTrue(self.p._ours(m.RESHADE_ATOM, "OledVHS.fx"))
        self.p.focused = "769"                                   # back to Steam
        self.p._refresh()
        self.assertIsNone(self.p.app_on)
        self.assertTrue(self.p.applied[1][m.RESHADE_ATOM].startswith(m.FX_PREFIX))

    def test_swapping_an_app_effect_is_not_a_conflict(self):
        self.p.settings["app_effects"] = {"42": {"effect": "old.fx"}}
        self.p.focused = "42"
        self.p._refresh()
        self.p.settings["app_effects"] = {"42": {"effect": "new.fx"}}
        self.assertTrue(self.p._ours(m.RESHADE_ATOM, "old.fx"))
        self.p._refresh()
        self.assertEqual(self.p.app_on["effect"], "new.fx")

    def test_app_effect_stays_when_correction_is_neutral(self):
        self.p.settings["app_effects"] = {"42": {"effect": "x.fx"}}
        self.p.focused = "42"
        self.p._refresh()
        self.assertEqual(self.p.app_on["effect"], "x.fx")
        removed = [c[3] for c in self.p.helper.calls if "-remove" in c]
        self.assertNotIn(m.RESHADE_ATOM, removed)

    def test_effects_are_named_by_content_and_reused(self):
        self.p.settings["engine"] = "reshade"
        names = []
        for t in (-10, -20, -10):
            self.p.settings["steps"][0]["v"][0] = t
            self.p._refresh()
            names.append(self.p.applied[1][m.RESHADE_ATOM])
        self.assertNotEqual(names[0], names[1])
        self.assertEqual(names[0], names[2])                     # seen before: the same, compiled effect
        self.assertEqual(len(os.listdir(self.p.fx_dir)), 2)

    def test_old_effect_files_go_beyond_the_keep_limit(self):
        self.p.settings["engine"] = "reshade"
        with mock.patch.object(m, "FX_KEEP", 3):
            for t in range(-10, -70, -10):
                self.p.settings["steps"][0]["v"][0] = t
                self.p._refresh()
        self.assertEqual(len(os.listdir(self.p.fx_dir)), 3)

    def test_brightness_bins_and_slider_debounce(self):
        self.assertEqual(m.Plugin._bin(177), m.Plugin._bin(178))
        self.assertNotEqual(m.Plugin._bin(177), m.Plugin._bin(190))
        self.p.settings["engine"] = "reshade"
        self.p.fast_until = time.monotonic() + 10                 # the slider is moving
        self.p.last_apply = 0.0
        self.p.settings["steps"][0]["v"][0] = -10
        self.p._refresh()
        first = self.p.applied
        self.p.settings["steps"][0]["v"][0] = -20
        with mock.patch.object(m, "DEBOUNCE", 0.25):
            self.p._refresh()                                    # within DEBOUNCE: held back
            self.assertIs(self.p.applied, first)
            self.p.last_apply -= 0.25
        self.p._refresh()
        self.assertIsNot(self.p.applied, first)

    def test_switching_engine_moves_the_correction(self):
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        asyncio.run(self.p.set_engine("reshade"))
        self.assertIn(m.RESHADE_ATOM, self.p.applied[1])
        self.assertIn(m.LOOK_ATOMS[0], self.removes())
        asyncio.run(self.p.set_engine("look"))
        self.assertIn(m.LOOK_ATOMS[0], self.p.applied[1])
        self.assertIn(m.RESHADE_ATOM, self.removes())

    def test_reapplies_after_gamescope_restart(self):
        self.p.settings["engine"] = "reshade"
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        self.p.helper.atoms = {m.RESHADE_ATOM: ""}  # fresh gamescope, nothing set
        atoms = self.p._read_atoms()
        if any(atoms.get(a, "") != v for a, v in self.p.applied[1].items() if v):
            self.p.applied = None
        self.p._refresh()
        self.assertIsNotNone(self.p.applied)
        self.assertEqual(self.sets().count(m.RESHADE_ATOM), 2)

    def test_external_display_pauses_correction(self):
        self.p.settings["steps"][0]["v"][0] = -40
        self.p._refresh()
        self.p.helper.atoms = {m.LOOK_ATOMS[0]: self.p.applied[1][m.LOOK_ATOMS[0]], m.EXTERNAL_ATOM: 1}
        atoms = self.p._read_atoms()
        self.p.external = atoms.pop(m.EXTERNAL_ATOM, "0") == "1"
        self.assertTrue(self.p.external)
        self.p._refresh()
        self.assertIsNone(self.p.applied)

    def test_foreign_effect_counts_as_conflict(self):
        self.p.helper.atoms = {m.RESHADE_ATOM: "someone_elses.fx"}
        atoms = self.p._read_atoms()
        self.assertFalse(self.p._ours(m.RESHADE_ATOM, atoms[m.RESHADE_ATOM]))
        self.assertTrue(self.p._ours(m.RESHADE_ATOM, m.FX_PREFIX + "123.fx"))

    def test_foreign_look_is_left_alone(self):
        self.p.settings["steps"][0]["v"][0] = -40
        self.p.conflict = True
        self.p._refresh()
        self.assertIsNone(self.p.applied)
        self.assertEqual(self.sets(), [])


class Polling(unittest.TestCase):
    def test_brightness_event_wakes_loop_and_polls_fast(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                p = make_plugin(tmp)
                p.wake = asyncio.Event()
                refreshes = []
                p._refresh = lambda: refreshes.append(asyncio.get_event_loop().time())
                p._read_atoms = lambda: {}
                task = asyncio.create_task(p._loop())
                await asyncio.sleep(0.05)
                before = len(refreshes)
                await p.brightness_changed()
                await asyncio.sleep(0.35)  # well under IDLE_INTERVAL
                task.cancel()
                return len(refreshes) - before
        # Woken at once, then FAST_INTERVAL ticks: several refreshes in 0.35 s.
        self.assertGreaterEqual(asyncio.run(run()), 3)


class Revision(unittest.TestCase):
    def test_counts_each_new_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            self.assertEqual(p.revision, 0)
            p._load_settings()                      # no file yet: nothing to adopt
            self.assertEqual(p.revision, 0)
            with open(p.settings_path, "w") as f:
                json.dump(p.settings, f)
            p._load_settings()
            self.assertEqual(p.revision, 1)
            with open(p.settings_path, "w") as f:
                json.dump(p.settings, f)
            p._load_settings()
            self.assertEqual(p.revision, 2)

    def test_unchanged_file_is_not_counted_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            with open(p.settings_path, "w") as f:
                json.dump(p.settings, f)
            p._load_settings()
            first = p.revision
            p._load_settings()                      # same mtime: not re-read
            self.assertEqual(p.revision, first)

    def test_published_state_carries_the_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_plugin(tmp)
            with open(p.settings_path, "w") as f:
                json.dump(p.settings, f)
            p._load_settings()
            p._publish_state()
            with open(m.STATE_FILE) as f:
                self.assertEqual(json.load(f)["revision"], p.revision)


class PreviewTimeout(unittest.TestCase):
    def test_abandoned_preview_is_dropped(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                p = make_plugin(tmp)
                p.wake = asyncio.Event()
                p._refresh = lambda: None
                p._read_atoms = lambda: {}
                await p.wizard_begin()
                await p.wizard_step(0, True)
                self.assertIsNotNone(p.preview)
                p.last_ping -= m.PREVIEW_TIMEOUT + 1  # pings stopped long ago
                task = asyncio.create_task(p._loop())
                await asyncio.sleep(0.05)
                task.cancel()
                return p.preview
        self.assertIsNone(asyncio.run(run()))


class Helper(unittest.TestCase):
    def test_native_helper_writes_same_outputs(self):
        h = m.Helper(":0")
        h.PYTHON = sys.executable
        curves = (m.resolve(at(8, -30)), m.resolve(at(16, -15)), m.resolve(at(24, 20)))
        with tempfile.TemporaryDirectory() as tmp:
            for kind, build in (("g22", lambda: m.build_cube(curves, False)), ("fx", lambda: m.build_fx(curves))):
                path = os.path.join(tmp, "out." + kind)
                h.write(path, curves, kind)
                with open(path) as f:
                    self.assertEqual(f.read(), build())
            self.assertEqual(h.run(["true"]).returncode, 0)
            h.proc.kill()
            h.proc.wait()
            self.assertEqual(h.run(["true"]).returncode, 0)  # restarts itself
        h.stop()


if __name__ == "__main__":
    unittest.main()
