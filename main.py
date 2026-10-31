import asyncio
import copy
import fcntl
import glob
import json
import math
import os
import select
import shutil
import subprocess
import sys
import time
import base64
import hashlib
import struct
import zlib

try:
    import decky
except ImportError:  # running as the native helper, see _helper_main
    decky = None

# Calibration steps (raw backlight values), darkest first. Roughly log-spaced
# because the panel's useful range is crammed into the bottom of 0..max.
DEFAULT_STEPS = [52, 96, 177, 310, 543, 4096]
# Measured by eye on this panel (correction off): clean up to 52, green
# building to a peak at 177, gone again from 543. The steps sit on those
# boundaries, with 96 and 310 halfway (in log brightness) up and down the
# slopes; below the first step the first step's (neutral) values apply.
# The first, guessed layout; its values near the new steps carry over.
OLD_STEPS = {100: 96, 320: 310}
# Encoded grey levels (fractions) that get their own correction, densest in
# the darks where the green cast is worst. Labelled in 8-bit as
# 4 8 12 16 20 24 32 40 48 64 80 96 128 160 192 255. The ReShade path
# reproduces every one; the look fallback only resolves multiples of 1/16
# (gamescope samples looks on a 17-point grid and blends greys tetrahedrally,
# both tested by eye).
LEVELS = [k / 64 for k in (1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64)]
# The previous layout, carried over by matching levels. New levels start on
# auto, which sits on the line between neighbours: curves don't change.
LEVELS_V3 = [i / 16 for i in (1, 2, 3, 4, 5, 6, 8, 10, 12, 16)]
KINDS = ("v", "l", "h")  # tint, brightness (lift), red/blue balance (hue)
MAX_TINT = 400.0  # hard clamp, see tint_to_gain; -400 = red/blue five times green (the
                  # camera found 3x not enough for grey 16 at the green peak)
MAX_LIFT = 50.0   # brightness per level, percent of the encoded value
MAX_HUE = 100.0   # +100 = red 1.5x and blue /1.5 relative to each other
LIMITS = {"v": MAX_TINT, "l": MAX_LIFT, "h": MAX_HUE}
# Luma weights (Rec.709): balancing them keeps a grey's brightness while its
# tint is corrected.
LUMA = (0.2126, 0.7152, 0.0722)
LUT_SIZE = 17
CURVE_SIZE = 256  # ReShade curve samples per channel, one per 8-bit code
# "Crush near-black": greys up to this 8-bit code go to true black (OLED
# off), fading back in over CRUSH_RAMP codes. Below grey ~16 the panel glows
# green at low backlight however the colour is set (camera, step 177).
MAX_CRUSH = 32
CRUSH_RAMP = 4.0
SDR_WHITE_NITS = 203.0
# Steam tells the frontend when it changes brightness, which wakes us up and
# polls fast for a moment (sysfs lags Steam's event a little). Otherwise poll
# slowly as a fallback: the loop runs under x86 emulation, so idle polling
# costs battery.
FAST_INTERVAL = 0.1
FAST_WINDOW = 1.5
# Dragging Steam's brightness slider: the correction follows in bins of ~4%
# backlight (it changes slowly with brightness), each bin's effect is named
# by its content so gamescope compiles it once and reuses it, and while the
# slider moves a new effect goes out at most every DEBOUNCE seconds. (A new
# name per change made gamescope compile on every slider pixel: stutter.)
BRIGHTNESS_BIN = 1.08      # ~28 distinct corrections across the bad band (was 56 at 1.04)
DEBOUNCE = 0.25
# gamescope compiles a new ReShade effect inside its composite path, so it
# presents nothing while compiling; with a game running in the background a
# compile takes longer, and several in a row (brightness jumps and correction
# toggles on the test pattern) froze the screen. At most one per this many
# seconds; in between the newest state waits.
COMPILE_GAP = 1.0
# floor dimming slider drag (only the picture's gain changes): follow closer.
SOFT_DEBOUNCE = 0.1
SOFT_COMPILE_GAP = 0.4
FX_KEEP = 160        # effect files kept for gamescope to find again
# Every engine switch (look <-> ReShade) costs gamescope a shader compile and
# two colour tables; switching on every menu open/close, together with
# brightness changes, hung gamescope for ~25 s (2026-10-24 20:34). So: the
# menu has to stay open MENU_OPEN_HOLD before the look takes over and closed
# MENU_CLOSE_HOLD before ReShade comes back, at most one switch per
# ENGINE_SWITCH_GAP, and after an xprop timeout nothing for STALL_PAUSE.
MENU_OPEN_HOLD = 0.35
MENU_CLOSE_HOLD = 1.5
ENGINE_SWITCH_GAP = 2.0
STALL_PAUSE = 10.0
# The frontend repeats "menu open" every few seconds while it is; without that
# for MENU_STALE the menu counts as closed (a reload or a missed event).
MENU_STALE = 12.0
STALLED_UNTIL = 0.0
STALLS = []             # monotonic times of xprop timeouts (gamescope not answering)
# Safe mode: this many stalls within SAFE_WINDOW seconds and the plugin stops
# the costly parts (dither, menu switching) until it's restarted or the user
# turns safe mode off; the panel says so.
SAFE_STALLS = 3
SAFE_WINDOW = 120.0
# After start, gamescope must have answered for READY_AFTER seconds before
# the plugin sets anything (gamescope may still be starting, e.g. at boot).
READY_AFTER = 3.0
IDLE_INTERVAL = 1.0
VERIFY_INTERVAL = 2.0
# The calibration page pings while open; a preview with no ping for this long
# is abandoned (page closed without saying so, plugin reloaded, ...).
PREVIEW_TIMEOUT = 20.0

LOOK_ATOMS = ("GAMESCOPE_COLOR_LOOK_G22", "GAMESCOPE_COLOR_LOOK_PQ")
RESHADE_ATOM = "GAMESCOPE_RESHADE_EFFECT"
RESHADE_TECHNIQUE_ATOM = "GAMESCOPE_RESHADE_TECHNIQUE_IDX"
FX_PREFIX = "oled_tint_"
EXTERNAL_ATOM = "GAMESCOPE_DISPLAY_IS_EXTERNAL"
FOCUS_ATOM = "GAMESCOPE_FOCUSED_APP"
# The app whose picture is in front. With a Steam menu open over a game,
# FOCUSED_APP turns to Steam's UI (769) while this one stays the game; it's
# also the app ReShade's effect follows.
FOCUS_GFX_ATOM = "GAMESCOPE_FOCUSED_APP_GFX"
# Steam's colour vibrance (a CARDINAL holding a float's bits). gamescope maps
# SDR colours towards the panel's gamut with it, mixing channels, which the
# dither can't survive: it has to drive each subpixel exactly. While the
# dither is on, the plugin holds it at 1.0 (no mixing; native panel colours)
# and gives the user's value back afterwards.
VIBRANCE_ATOM = "GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS"
VIBRANCE_NATIVE = "1065353216"     # 1.0f


def _clean_env(display):
    # Decky's bundled runtime sets LD_LIBRARY_PATH, which can break system tools.
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    env["DISPLAY"] = display
    return env


def _stalled():
    """gamescope isn't answering: everything we'd send now only piles on. The
    plugin backs off for STALL_PAUSE; repeated stalls lead to safe mode."""
    global STALLED_UNTIL
    STALLED_UNTIL = time.monotonic() + STALL_PAUSE
    STALLS.append(time.monotonic())


def _run(args, display):
    try:
        return subprocess.run(args, env=_clean_env(display), capture_output=True,
                              text=True, timeout=3)
    except subprocess.TimeoutExpired:
        # The in-process fallback (helper broken) gets the same protection.
        decky.logger.error(f"{args[0]} timed out (fallback)")
        _stalled()
        return None
    except Exception as e:
        decky.logger.error(f"{args[0]} failed: {e}")
        return None


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_output(path, curves, kind, crush=0, dither=None, gain=1.0):
    """kind: "g22" / "pq" look cube, or "fx" ReShade effect (the only one that
    can dither)."""
    _write(path, build_fx(curves, crush, dither, gain) if kind == "fx" else build_cube(curves, kind == "pq", crush, gain))


def _helper_main():
    """Native-speed worker. Decky runs plugins under x86 emulation, where one
    LUT build plus xprop takes seconds; the system's ARM python does it in
    milliseconds. Speaks one JSON object per line on stdin/stdout."""
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if req["op"] == "write":
                write_output(req["path"], req["curves"], req["kind"], req.get("crush", 0), req.get("dither"),
                             req.get("gain", 1.0))
                res = {"ok": True}
            else:
                r = subprocess.run(req["args"], capture_output=True, text=True, timeout=3)
                res = {"ok": True, "rc": r.returncode, "out": r.stdout}
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()


class _Result:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


def _native_python():
    """Path to the host's ARM python. Under FEX, /usr/bin/python3 resolves to
    the emulated guest's x86 build; the booted ostree deployment under
    /sysroot isn't remapped, so it reaches the real one (and follows OS
    updates)."""
    try:
        with open("/proc/cmdline") as f:
            arg = next(a[7:] for a in f.read().split() if a.startswith("ostree="))
        path = os.path.realpath("/sysroot" + arg + "/usr/bin/python3")
        if os.access(path, os.X_OK):
            return path
    except Exception:
        pass
    return "/usr/bin/python3"


class Helper:
    PYTHON = _native_python()

    def __init__(self, display):
        self.display = display
        self.proc = None

    def _start(self):
        decky.logger.info(f"starting helper with {self.PYTHON}")
        if self.proc is not None:               # a dead helper's pipes
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        self.proc = subprocess.Popen(
            [self.PYTHON, "-I", os.path.abspath(__file__), "--helper"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=_clean_env(self.display))

    def call(self, req, timeout=5.0):
        """Returns the reply dict, or None if the helper is unusable."""
        for _ in range(2):  # one restart if it died
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
                if not select.select([self.proc.stdout], [], [], timeout)[0]:
                    raise TimeoutError("helper timed out")
                return json.loads(self.proc.stdout.readline())
            except Exception as e:
                decky.logger.error(f"helper: {e}")
                self.stop()
        return None

    def run(self, args):
        res = self.call({"op": "run", "args": args})
        if res is None:
            return _run(args, self.display)  # fall back to doing it ourselves
        if not res["ok"]:
            decky.logger.error(f"{args[0]} failed: {res['error']}")
            if "timed out" in str(res["error"]):
                _stalled()
            return None
        return _Result(res["rc"], res["out"])

    def write(self, path, curves, kind, crush=0, dither=None, gain=1.0):
        res = self.call({"op": "write", "path": path, "curves": [list(c) for c in curves],
                         "kind": kind, "crush": crush, "dither": dither, "gain": gain})
        if res is None or not res["ok"]:
            write_output(path, curves, kind, crush, dither, gain)

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except Exception:
                pass
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    pipe.close()
                except Exception:
                    pass
            self.proc = None


def tint_to_gain(tint):
    """Red/blue gain relative to green (gamma-2.2 encoded) for a tint.
    tint < 0 lifts red/blue (toward magenta), tint > 0 lowers them (toward
    green); the two directions are mirror images, -t and +t cancel out."""
    t = max(-MAX_TINT, min(MAX_TINT, tint)) / 100.0
    return 1.0 - t if t < 0 else 1.0 / (1.0 + t)


def channel_scales(x, tint, lift=0.0, hue=0.0):
    """(red, green, blue) multipliers for encoded level x.

    The tint sets the red/blue-to-green ratio and hue splits red from blue;
    then all channels move together so the grey's luminance stays where it
    was (lifting red/blue alone made already-too-bright darks brighter
    still). lift then brightens or darkens the grey on purpose. Near white,
    everything scales down to avoid clipping while keeping the balance."""
    k = tint_to_gain(tint)
    split = 1.0 + max(-MAX_HUE, min(MAX_HUE, hue)) / 200.0
    r, b = k * split, k / split
    s = (LUMA[0] * r ** 2.2 + LUMA[1] + LUMA[2] * b ** 2.2) ** (-1 / 2.2)
    lam = 1.0 + max(-MAX_LIFT, min(MAX_LIFT, lift)) / 100.0
    out = [r * s * lam, s * lam, b * s * lam]
    peak = max(out) * x
    if peak > 1.0:
        out = [c / peak for c in out]
    return tuple(out)


def crush_gain(x, crush):
    """1 normally; 0 for greys up to code `crush`, ramping back over CRUSH_RAMP."""
    if crush <= 0:
        return 1.0
    return max(0.0, min(1.0, (x * 255 - crush) / CRUSH_RAMP))


def curve_out(curves, x, crush=0, hold=0.0):
    """Corrected (r, g, b) for an encoded grey x, each channel independent.
    curves: resolved (tints, lifts, hues). Below `hold` the correction found
    at `hold` applies (used under dithering, which takes over the darks)."""
    if x <= 0:
        return 0.0, 0.0, 0.0
    at = max(x, hold)
    scales = channel_scales(at, *(tint_at(c, at) for c in curves))
    c = crush_gain(x, crush)
    return tuple(min(1.0, x * k) * c for k in scales)


def resolve(values):
    """Fill in auto (None) levels: linear between set neighbours, held below
    the first set level, neutral above the last (so the correction eases out
    over the next grid cell)."""
    known = [(i, v) for i, v in enumerate(values) if v is not None]
    if not known:
        return [0.0] * len(LEVELS)
    out = []
    for i, v in enumerate(values):
        if v is not None:
            out.append(float(v))
            continue
        lo = max((k for k in known if k[0] < i), default=None)
        hi = min((k for k in known if k[0] > i), default=None)
        if lo is None:
            out.append(hi[1])
        elif hi is None:
            out.append(0.0)
        else:
            f = (LEVELS[i] - LEVELS[lo[0]]) / (LEVELS[hi[0]] - LEVELS[lo[0]])
            out.append(lo[1] + f * (hi[1] - lo[1]))
    return out


def tint_at(tints, x):
    """Tint for encoded grey level x from fully resolved per-level tints:
    linear between LEVELS, held below the first."""
    if x <= LEVELS[0]:
        return tints[0]
    for x0, x1, t0, t1 in zip(LEVELS, LEVELS[1:], tints, tints[1:]):
        if x <= x1:
            return t0 + (x - x0) / (x1 - x0) * (t1 - t0)
    return tints[-1]


def _pq_eotf(e):
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    p = max(e, 0.0) ** (1 / m2)
    return (max(p - c1, 0.0) / (c2 - c3 * p)) ** (1 / m1)


def _pq_inv_eotf(y):
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    p = max(y, 0.0) ** m1
    return ((c1 + c2 * p) / (1 + c3 * p)) ** m2


def build_cube(curves, pq, crush=0, gain=1.0):
    """gain: the whole picture's light scaled by it (floor dimming dimming)."""
    n = LUT_SIZE
    axis = [i / (n - 1) for i in range(n)]
    chans = ([], [], [])
    for x in axis:
        if pq:
            # Correct HDR content by the light it actually emits: find the SDR
            # grey with the same luminance, and apply that grey's gain in
            # linear light (a gain k in gamma-2.2 space is k^2.2 in linear).
            y = _pq_eotf(x)
            eq = min(1.0, (y * 10000.0 / SDR_WHITE_NITS) ** (1 / 2.2))
            scales = channel_scales(eq, *(tint_at(c, eq) for c in curves)) if eq > 0 else (1.0,) * 3
            c = crush_gain(eq, crush) ** 2.2
            vals = [min(1.0, _pq_inv_eotf(y * k ** 2.2 * c * gain)) for k in scales]
        else:
            vals = [v * gain ** (1 / 2.2) for v in curve_out(curves, x, crush)]
        for c, v in zip(chans, vals):
            c.append(v)
    # Format each value once; formatting 15k floats per slider tick adds up.
    rs, gs, bs = ([f"{v:.6f}" for v in c] for c in chans)
    parts = ['TITLE "OLED Tint"', f"LUT_3D_SIZE {n}"]
    for b in bs:
        for g in gs:
            tail = f" {g} {b}"
            parts.append("\n".join([r + tail for r in rs]))
    return "\n".join(parts) + "\n"


FX_TEMPLATE = """// OLED Tint: generated by the Decky plugin, rewritten on every change.
// Per-channel correction curves, one sample per 8-bit code, with dithering
// so the corrected darks don't band.
namespace ReShade
{
    texture BackBufferTex : COLOR;
    sampler BackBuffer { Texture = BackBufferTex; };
}

// gamescope + freedreno crash binding an empty uniform buffer; keep one.
uniform float Strength = 1.0;
// The panel's grey floor: the dither table's hand-over code, the bottom of
// its banded range (0: no table, a uniform gain).
static const float Floor = %(floor).6f;

static const float CurveR[%(n)d] = { %(r)s };
static const float CurveG[%(n)d] = { %(g)s };
static const float CurveB[%(n)d] = { %(b)s };
%(dither_decl)s
float lookup(float x, int c)
{
    float p = saturate(x) * %(last)d.0;
    int i = min(int(p), %(last_minus)d);
    float f = p - i;
    float a = c == 0 ? CurveR[i] : c == 1 ? CurveG[i] : CurveB[i];
    float b = c == 0 ? CurveR[i + 1] : c == 1 ? CurveG[i + 1] : CurveB[i + 1];
    return lerp(a, b, f);
}

void VS(in uint id : SV_VertexID, out float4 pos : SV_Position, out float2 uv : TEXCOORD)
{
    uv.x = (id == 2) ? 2.0 : 0.0;
    uv.y = (id == 1) ? 2.0 : 0.0;
    pos = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
}

float4 Shade(float4 pos, float2 uv, float G)
{
    float3 c = tex2D(ReShade::BackBuffer, uv).rgb;
    float3 L = float3(lookup(c.r, 0), lookup(c.g, 1), lookup(c.b, 2));
    // Floor-anchored gain: sub-floor codes pass through untouched, codes
    // above are pulled toward the floor. A flat *G crushes the sub-floor
    // range below the panel's minimum and bands it.
    float3 o = min(L, Floor) + max(L - Floor, 0.0) * G;
    // Triangular dither, one 8-bit step wide.
    float n1 = frac(sin(dot(pos.xy, float2(12.9898, 78.233))) * 43758.5453);
    float n2 = frac(sin(dot(pos.xy, float2(39.3468, 11.135))) * 24634.6345);
    float3 t = o + (n1 + n2 - 1.0) / 255.0;
    // True black (and crushed greys) stay at zero: the noise alone would
    // light about one black pixel in eight at code 1.
    t *= step(1e-6, o);
%(dither_apply)s
    return float4(lerp(c, t, Strength), 1.0);
}

%(techniques)s"""


DITHER_DECL = """
// Near-black dithering. At low backlight every subpixel jumps from almost
// off (code 1) to a far too bright minimum (code 2+); in between there is
// nothing. Below code %(V)d each channel's light is set to what the panel
// gives at full brightness, made from the two nearest real levels mixed
// per pixel: per input code, the lower and upper output code and the share
// of pixels that get the upper one (-1: not dithered).
static const float DitherW = %(w).4f;
// The panel blurs patterns that change from one row to the next (it shares
// subpixels along its native rows, our vertical), not along a row: the
// pattern changes every pixel across and every DitherCell rows down.
static const float DitherCell = %(cell).1f;
%(frame_decl)s
static const float DLoR[256] = { %(lr)s };
static const float DLoG[256] = { %(lg)s };
static const float DLoB[256] = { %(lb)s };
static const float DHiR[256] = { %(hr)s };
static const float DHiG[256] = { %(hg)s };
static const float DHiB[256] = { %(hb)s };
static const float DFR[256] = { %(fr)s };
static const float DFG[256] = { %(fg)s };
static const float DFB[256] = { %(fb)s };

// Blue noise (void and cluster, made for the cell's shape on screen):
// thresholds spread with no clumps and no direction; a formula noise
// (interleaved gradient) showed a diagonal texture that turned with the
// share of lit pixels. Indexed in cells: 64 across, 64/DitherCell down.
texture DitherNoiseTex < source = "%(noise)s"; > { Width = 64; Height = %(noise_h)d; Format = R8; };
sampler DitherNoise { Texture = DitherNoiseTex; MagFilter = POINT; MinFilter = POINT; MipFilter = POINT;
                      AddressU = REPEAT; AddressV = REPEAT; };
float bn(float2 p) { return (tex2Dlod(DitherNoise, float4((floor(p) + 0.5) / float2(64.0, %(noise_h)d.0), 0.0, 0.0)).r * 255.0 + 0.5) / 256.0; }

float dither(float x, float plain, float tri, int ch, float2 pos)
{
    int k = int(saturate(x) * 255.0 + 0.5);
    float lo = ch == 0 ? DLoR[k] : ch == 1 ? DLoG[k] : DLoB[k];
    if (lo < 0.0)
        return tri;
    float f = ch == 0 ? DFR[k] : ch == 1 ? DFG[k] : DFB[k];
    // A different offset per channel, so subpixels don't switch together.
    float2 q = float2(pos.x, floor(pos.y / DitherCell));
    float n = bn(q + (ch == 0 ? float2(0.0, 0.0) : ch == 1 ? float2(21.0, 37.0) : float2(43.0, 11.0)));
%(frame_apply)s
    // Between steps, a share of pixels is dithered rather than each pixel
    // part-way: a part-way value lands between codes, which at low
    // backlight is already the pedestal.
    float m = bn(q.yx + float2(13.0 + 17.0 * ch, 29.0));
    float hi = ch == 0 ? DHiR[k] : ch == 1 ? DHiG[k] : DHiB[k];
    return m < DitherW ? (n < f ? hi : lo) / 255.0 : plain;
}
"""

DITHER_APPLY = """    t = float3(dither(c.r, o.r, t.r, 0, pos.xy), dither(c.g, o.g, t.g, 1, pos.xy),
               dither(c.b, o.b, t.b, 2, pos.xy));"""


def _yref_at(yref, v):
    """Reference grey luminance at (fractional) code v, log-log interpolated;
    below the first point a 2.2 power law."""
    pts = sorted((c, y) for c, y in yref if c > 0 and y > 0)
    if v <= pts[0][0]:
        return pts[0][1] * (v / pts[0][0]) ** 2.2
    for (c0, y0), (c1, y1) in zip(pts, pts[1:]):
        if v <= c1:
            t = math.log(v / c0) / math.log(c1 / c0)
            return math.exp(math.log(y0) + t * (math.log(y1) - math.log(y0)))
    return pts[-1][1]


def _trim_at(trim, name, code, V):
    """Camera-measured gain for one channel at input grey `code`: trim is
    {"codes": [...], "R": [...], "G": [...], "B": [...]} from a closed-loop
    run; log-interpolated between codes, held below the first, fading to 1
    at V where the dither hands over."""
    if not trim:
        return 1.0
    pts = sorted(zip(trim["codes"], trim[name])) + [(V, 1.0)]
    if code <= pts[0][0]:
        return pts[0][1]
    for (c0, g0), (c1, g1) in zip(pts, pts[1:]):
        if code <= c1:
            t = (code - c0) / (c1 - c0)
            return math.exp(math.log(g0) + t * (math.log(g1) - math.log(g0)))
    return 1.0


def _rungs(L, V):
    """The codes worth dithering between: measured (light >= 0; unmeasured
    codes are -1 or None) and brighter than every lower one. Through the
    shader the panel's light isn't monotonic in the code (10-bit output
    rounding), so neighbours can be useless."""
    rungs, best = [0], 0.0
    for j in range(1, V + 1):
        if L[j] is not None and L[j] >= 0 and L[j] > best:
            rungs.append(j)
            best = L[j]
    return rungs


# Dithering over time: the same spatial mask each frame, its thresholds
# shifted per frame so every cell is lit in the right share of frames, out of
# step with its neighbours, and the eye averages. gamescope's ReShade fills
# "framecount". "pair": two phases half a step apart, so a cell is always on,
# always off, or flips every frame, the fastest change there is (least
# flicker, also in peripheral vision). "golden": a golden-ratio step per frame
# averages over many frames (smoothest grain) but its slow beats can show as
# flicker at 60 fps.
TEMPORAL_MODES = ("pair", "golden")
# gamescope redraws Steam's own UI only when something changes (about once a
# second, or on touch), so a pattern that moves per frame would jump there
# instead of averaging: in Steam's UI the dither stays still.
STEAM_UI_APP = "769"
FRAME_DECL = """uniform int FrameCount < source = "framecount"; >;"""
FRAME_APPLY = {
    # framecount steps by 1 per game frame (measured: ~60 a second at 60 fps).
    # The eye merges the two alternating patterns into one finer, still one.
    "pair": """    n = frac(n + 0.5 * float(FrameCount % 2));""",
    "golden": """    n = frac(n + 0.61803398875 * float(FrameCount % 4096));""",
}


def trim_for(table, mode):
    """The camera trim for a dither mode ("still", "pair", "golden"): the
    panel loses light differently depending on how the pattern moves, so each
    mode gets its own closed loop (table["trims"][mode]); "trim" is from
    before there were modes. Golden without its own falls back to pair's:
    both move every frame, so they lose light alike."""
    trims = table.get("trims") or {}
    return trims.get(mode) or (trims.get("pair") if mode == "golden" else None) or table.get("trim")


# The dither's target below its hand-over code: "g22" a pure 2.2 power law
# (how games are graded), "native" the panel's own curve at full backlight,
# which lifts grey 8-24 by 13-46% over 2.2. Above the hand-over the panel is
# 2.2 within ~3% either way.
SHADOW_CURVES = ("g22", "native")


def dither_tables(curves, crush, table, mode="still", curve="native"):
    """Per channel and input code: (lower output code or -1, upper code,
    share of pixels that get the upper one). table: {"V": code, "L": {"R":
    [light per code 0..V], ...}, "yref": [[code, Y], ...], "trim": optional
    per-channel gains} from the camera."""
    V = int(table["V"])
    hold = V / 255
    ys = {}
    lo = ([], [], [])
    hi = ([], [], [])
    fr = ([], [], [])
    rungs = {name: _rungs(table["L"][name], V) for name in "RGB"}
    trim = trim_for(table, mode)
    for i in range(256):
        held = curve_out(curves, i / 255, crush, hold)
        for ch, name in enumerate("RGB"):
            L = table["L"][name]
            v = held[ch] * 255
            if not 0 < v < V:
                lo[ch].append(-1.0)
                hi[ch].append(-1.0)
                fr[ch].append(0.0)
                continue
            if v not in ys:
                ys[v] = ((v / V) ** 2.2 if curve == "g22"
                         else _yref_at(table["yref"], v) / _yref_at(table["yref"], V))
            target = L[V] * ys[v] * _trim_at(trim, name, i, V)
            rs = rungs[name]
            a = max([r for r in rs if L[r] <= target] or [0]) if target > 0 else 0
            up = [r for r in rs if r > a]
            b = up[0] if up else a
            span = (L[b] - L[a]) if b != a else 0
            lo[ch].append(float(a))
            hi[ch].append(float(b))
            fr[ch].append(max(0.0, min(1.0, (target - L[a]) / span)) if span > 0 else 0.0)
    return lo, hi, fr


# floor dimming dimming as techniques: one effect holds the gain ladder (each
# step GAIN_RATIO lighter than the last), the backend switches between them
# with GAMESCOPE_RESHADE_TECHNIQUE_IDX, which gamescope does without
# recompiling (a new effect per gain stuttered while sliding).
SLIDER_LOG = "/tmp/oled-slider.log"


def _slider_log(msg):
    try:
        with open(SLIDER_LOG, "a") as f:
            f.write(f"{time.time():.3f} {msg}\n")
    except OSError:
        pass


GAIN_RATIO = 1.02
GAIN_STEPS = 152         # 1.0 .. ~0.05 of the light, ~2% per step
# After the ladder: a technique that leaves the picture alone. Below the band
# floor dimming keeps its effect on screen on this one, so the hand-over to the
# floor and back is a technique switch, not a new effect: the compile throttle
# made it flash undimmed 543 for up to 1 s going up and black out going down
# (RESULTS 32).
PASS_TECH = GAIN_STEPS
PASS = "pass"            # the gain that selects it
# Hand-over down: the pass-through goes on screen before the frontend drops
# the backlight (gamescope shows a technique switch ~3-4 frames after the
# atom, the backlight follows Steam's call within ~1), so for this long the
# backlight counts as already below the band.
HANDOVER_WAIT = 1.0
# Floor dimming going off: how long the dimming waits at most for the frontend
# to drop the backlight from the floor (its poll is 2 s).
LET_GO_WAIT = 3.0


def gain_index(gain):
    return max(0, min(GAIN_STEPS - 1, round(-math.log(max(gain, 1e-6)) / math.log(GAIN_RATIO))))


def build_fx(curves, crush=0, dither=None, gain=1.0):
    """dither: {"table": ..., "w": 0..1} to dither near-black, or None.
    gain: the whole picture's light scaled by it (floor dimming dimming; a gain
    k in linear light is k^(1/2.2) on the encoded values); None: the whole
    gain ladder as techniques instead."""
    ladder = gain is None
    if ladder:
        gain = 1.0
    n = CURVE_SIZE
    hold = dither["table"]["V"] / 255 if dither and dither["w"] >= 1 else 0.0
    floor = dither["table"]["V"] / 255 if dither and dither.get("table") else 0.0
    g = gain ** (1 / 2.2)
    samples = [tuple(v * g for v in curve_out(curves, i / (n - 1), crush, hold)) for i in range(n)]
    cols = [", ".join(f"{s[c]:.6f}" for s in samples) for c in range(3)]
    decl, apply = "", ""
    if dither and dither["w"] > 0:
        lo, hi, fr = dither_tables(curves, crush, dither["table"], dither.get("temporal") or "still",
                                   dither.get("curve", "native"))
        fmt = lambda xs: ", ".join(f"{x:.4f}" for x in xs)
        cell = dither["table"].get("cell", 1)
        cell = cell if cell in BLUENOISE else 1
        decl = DITHER_DECL % {"V": dither["table"]["V"], "w": dither["w"],
                              "noise": noise_file(cell), "noise_h": 64 // cell, "cell": cell,
                              "frame_decl": FRAME_DECL if dither.get("temporal") else "",
                              "frame_apply": FRAME_APPLY.get(dither.get("temporal"), "") if dither.get("temporal") else "",
                              "lr": fmt(lo[0]), "lg": fmt(lo[1]), "lb": fmt(lo[2]),
                              "hr": fmt(hi[0]), "hg": fmt(hi[1]), "hb": fmt(hi[2]),
                              "fr": fmt(fr[0]), "fg": fmt(fr[1]), "fb": fmt(fr[2])}
        apply = DITHER_APPLY
    if ladder:
        techniques = "".join(
            f"float4 PS{k}(float4 pos : SV_Position, float2 uv : TEXCOORD) : SV_Target "
            f"{{ return Shade(pos, uv, {GAIN_RATIO ** (-k / 2.2):.6f}); }}\n"
            f"technique G{k} {{ pass {{ VertexShader = VS; PixelShader = PS{k}; }} }}\n"
            for k in range(GAIN_STEPS))
        techniques += ("float4 PSPass(float4 pos : SV_Position, float2 uv : TEXCOORD) : SV_Target "
                       "{ return float4(tex2D(ReShade::BackBuffer, uv).rgb, 1.0); }\n"
                       "technique Pass { pass { VertexShader = VS; PixelShader = PSPass; } }\n")
    else:
        techniques = ("float4 PS(float4 pos : SV_Position, float2 uv : TEXCOORD) : SV_Target "
                      "{ return Shade(pos, uv, 1.0); }\n"
                      "technique OledTint { pass { VertexShader = VS; PixelShader = PS; } }\n")
    return FX_TEMPLATE % {"techniques": techniques, "n": n, "last": n - 1, "last_minus": n - 2,
                          "r": cols[0], "g": cols[1], "b": cols[2], "floor": floor,
                          "dither_decl": decl, "dither_apply": apply}


# The dither's blue-noise masks, one per cell height: 64 cells across and
# 64/cell down (a 64x64-pixel tile), ranks quantised to 8 bits, made for
# the cell's shape on screen by scripts/make_bluenoise.py; zlib + base64.
# Written out as PNGs next to the shaders, since Decky won't take new files
# in the plugin dir.
BLUENOISE = {
    1: (
        "eNoBABD/79EwrIM9XY0CwJhKqeRVAEuGOHDuQawQbeK4chSDLLnmXS2PALKddkCmZXxJpw3vlGifI8IAsztvi8HrllVu"
        "r5F7TMTnDbfbRXQYyYAlkuvGFs6RBmKc/Io7lVrdQOwEh6TabPxZyhXgUSXOGNiCwz8H3YfvdOIh9ghGsTYC1jzqD50d"
        "YtR5L/yr4V8Q1mYze1/6KLLeMk28FvIlvpxsTcg6EbhBhSpnuI/wsZVSKnGwV7xILqJVkKpb1CJ++qIgY7D9RJUnp0+T"
        "HziJ+EO1nCC7oEJuyRl5zmSrTX4R0a4kdvCaG+Gn9gp3NRBp/KTiHPZ5D2fOF0LFdOadWMVFiMwtf85u8wjDac9TvqJ1"
        "C9LyUQnkhlOW5iuFDNPuMFv5kdZgTs9yN1bVR+eIyz4CYYgzypOz/HzcDi+JErZwDfFSwAM5s1WI6RN/8gMq51mCNo9o"
        "xSP0A65D9ppyPqWEGkQHqimOBbyUIaG1VSO5ktBKpuciTzedYrj4UT3tK9uXa+Kg7hrWMEefJrBskjrIHazYF6U8t3Jc"
        "2B1VuQHJ32+66IHE+kjof8lsBd58828n3wlxX8ME6yWTbabLkF6qOx1KdV6RqHG34V7TSuC4ZO5Gdf1VgNQtoYvENOdj"
        "j0wsm1Y2ZxegYxJA/zCcRhStWbuWP/CkiFnSQQjdH3sF04G5jijFQAb/HoQ5lht7CKOKDrgulekVTfsLaoGnI/8SxPIM"
        "1rB80Syv11uCw2bUN+kXgs4QeN4arYPxWzbA/lMU888M6YDaY06/CvTGM/lTKONOzwZmx3ivPuwY3EB6rVx1kSTmO1X1"
        "cJAZq+MLi6B2Q/4mtDNFx3Awu5lurD+SajJcn0+yJsqc5HOnUImvc8Oda4eyOZ4k2l6YyVKT0TLiP75QowGWuw5J8CY+"
        "VvgeyGOZUW30kVbkFk7ZCuYgpN+zhfsYazqKD0QtYdwgP9UWNfgf21nziQK9LHYOt2AEpB2E/GzKgibdxnWYvG+oMuQE"
        "v92mHb0Bn/aJJ35LyHgCQyJ0wt+m9nqx7ZMAumbsjWKrRX0Qt0ls7kWp9iffffFlzxQw7ENonDJZ6gLOgU2wjDgShWHZ"
        "eTJottGa72Mv9L3SN5QESiFbzhnDfv2cEEzNA8HnlCbRohyE12eMTMM6k0qyl1i4CveuFYZGKd8WX/N0SusoQanORRNa"
        "Oxm1jE6aD1bqZL3boDdwSiRWMrR75ZpxM1/+eTXAWQc6oBCrItgLe+Ee1Y1Let6/pf1om7om0qvElvsLh+Gh/HWn2Qvj"
        "a/h/ri6LdhHzjeek3mrTIjpXG6XGTgjfk/mtzelc+3K38GM/p3MuwiBmOFWNDMc/hAddF3FRsl8mfQbLK1WAO6QWQdQa"
        "/T/HWQa4OYMIkPe/gvDaEoezaEIXeC5wHog0U4oqvgf7Ydig9QjQdTTbVfmi2ok13RzG7Uu8YZLzuCPHi7lomVSqJZ3U"
        "Zh3HsD5eFa0qYj6b7yLYVI61R8LapgTSmed/SJYQQYWzJfGvkx14LEjuu512PY+rMuUVRHDdYPEs5wPCbu6EQ3z5UeRv"
        "odZIkLtwzDJ8nsbwAOOZE2X1RBtcyyLqulfgbZpOEmTrtM1uDCZh9QPVGm+e04YAnEkMdE+G3RM02heoMJURLO4BePkZ"
        "3ARRug45aipZfDu+grBzoTivh3EvGcw75XrCRAFZlKzQS4rAWoD3UCnB/jOykNKmOLJMlli75WPQeryLU8o0WqKC/GXh"
        "qYHQrv8i3FEu7g/ZTQHF8aNfBYrVMaOD4Dr/fxSiMOpAtwtqqlh6yloU6iF8+81vAokhrEffZRq0mOQrsT+OIUv0EI1K"
        "bp8Jz7uNbfxhmD+CvP6pHF32JsYRZivctm0Ppt6N0D0Z4Sb5apjEYw0qpO45VP8INqP1QoQOaesT08GYYTXIGLTthWVA"
        "GagtzR7dDVAqcUq1km9Qt6DpVzrPhiReMnnrkaVFiLY9Ud+Nv0Z5yZa+d5HWKHHRTb98WDN2C+ajd+A6WiCa9FTjiEh3"
        "p2XsmcbjC9cY8IdAexuV+0y/nPgEsWQOyDEC8hmrNvVfHd4PZOkWV8EF76wf16T1tkbOI1CTA8LZrzF+wg229zTPfBM6"
        "hGZDmy/TBcqqYgh62RVFw1Io3nHmlHrVZoAHtI6nSSu1QKuIY5w1jkQBiidrhvq+afJ/Q28I2GMom1gRuUbmqyP6wnWx"
        "WGv0J8PjM61gdpfwgLRPH8VXKZ7NTOczbPOH2nP4LuAUVv5u4F3Jpwc/G60toxf9upNB8XLgiyeXXM2PVQLpIN6WOIZJ"
        "bZgi6NA2G9c6n2usQe8QuiN30hLFWgSgHU3Nd6/HKp4V8lLel13US+VdjFAapskFR8Rs9Qd0NLqjO4pMDLvcFLX2Q4UL"
        "p2WQB/jRDYHZkV76m1WsJ5c702m8jz3mDYLAPnoxtG7siQ15zDXea+o0fqMX1a0/3B3wfWfJrP10oV2BAMZVu/1KyXpa"
        "jza0Zi9EfwA54nP9uoHpCaMiaE+pYtiSG8sRN8Ens5sAwSuJXb37Yy1Sg7+USA/kGV0uRiDqNdpplBl0K7XtJd1O/hfH"
        "6a3XwIpKE2AkUTj6fdD0NQXtsVb9f6RT92hC8XeuC9YcPZDnoBP/XLLUNZ7thNXBj1CpJ88+45oPQaYCwZVzog5qLFwc"
        "6Z/KjqvVXLkRl8WISiVrmULeBo7QFlySTPeZVLN4Ac9oNyRxjFO8bAenFW/5EYbysV6A2FZyh14lPuBQkPeje7U/BfBy"
        "FYkwSXAe4njHE9cpY694MOW1JMo2bugnykq1fuiexgPbIpdL41u2PsR0UgUzqCC95xj0z66EIb1BCNEnZtdIKcXin++y"
        "WaU99al0vfIexEmia9oQf8IUnmH2HkHYFEbsdz37yC6AIJTgL5zqzGn7kTVnnksJ6WDWdexUjPt5la5SZQB+KtIJiy1a"
        "AoY9m1v+CYo/8ZReRdiEMpWpYIq3XamFDmGk6s9ODGS8HItNCkSw3yx8ujakEZQ1wqgMM78S9zfXukT8Y8HalexQ3xDT"
        "gii9V6kqtvwHr3LuCcgs+Rox37mOQgRpsP6C2zxyudl3yQaN71R0/Sm0Zx5JzFzshG2ijCBvmhh9SB6tL7iRbjnM63MC"
        "3Xk4WMoZVNt7SW6Yyk1rJfbCfTGhI1eu9RKaJ1ijQscO24lHzuiE3XWcI0DNDupOxeE0ufNn0nldIehOnhqPS8gXpYvm"
        "QrmQH7PjBn3xEdiXVxjtRtICjjFh44P4umwlqGIcnVcEpDwW5a9XtixgqgeDWqUOPJ0I+b+oBWa2NvmYae4lbZwvYus6"
        "oVnAPqt2NLKIx2iZeO3JSak8HVbqj9Q4uvF+MPq+aIsD2Hb+h9NB9yHZcOXDjUk1hdn1fNJfK75D1Q7D9gDMhxbWLJJe"
        "zBvoTg3hObkoaRfCbtcyFXtL5HARxmWTUSbyQ5ccO6AVdbaUSYgrVBbXbxxaQyULroYRWqyIT36nclL9bLEJ90Obabwr"
        "qlsR1p6J9ASW/6HMAZcqqkXXCq3OecVk4cFT7ydezwG9/3i076PNkMCl70/izHb/MeMePLgkmjjgiCXcfgPzjnX8g03q"
        "MViydb5AYbb2XIvmIITtOQ2oK4UHbK+M5DWiZB2eO18tEOoxgmeWOSOfBmWVy2Do0gvER2Gqv1SlOMtGHMSsCnrOJEwK"
        "4ix4Hcs3crxMbqFe+Ui46THWRxOA70TM4AWXf7ZqUgHWGLTrTLzaQrEIjkpvgfAUdjQc5WQU3JszZOFAmeiIa6qQ3FCw"
        "B/2dLt8VitkaW5p9Hp/IV7Epi25LyPg+3Kr8xEZ7YosqgBr5eTD0qCe1jtD/l8OAs1Zx8ZAfuVsYx1P3Dz2gf9VkE1iv"
        "xihzsNM98Ltn+gh0wxLrrCFaDY8fcy+Q9QzG5qhsUaC9XBfePFgMT209CfcqqgBL14L6pTkfgMth8RpIjMjoeULuT48A"
        "cRRRKpE85ZtgOIPYpHvPTJ9gvCGgPFgC0jjkD9eUacGb47gi1Y1IzYffs2ovBnPUt58pdrMv5Ks6IZUJos0z/sKV4nzX"
        "qh9Q/LUKZyjrNcDxEt5V1XLylL4fjHFFLO8Dcy6Dqe9coxNhORuYzESS7DZO6gvFl2sDfvhfvW0eW6hBYq4YSGfJfy3N"
        "lvRRr2sFhjqsgTG0G0R87lWoyYGwTdL1Rxh2Mb/pffzDUvCvWxBrzotaO/pR07dNzjDzid56DdQv+L0Djt0USnc6xhiY"
        "47d1TP0Jjslioi3VBv4ZYI0gn2XL4gOSQB+hbwt4IdyB/hio3XkbjSqdDnelSQW4K+yMV4Oc7TexX6HlAInbXUMp0Rqc"
        "bOE1+BK0aUCZM+XIPrYKiU+madBXuzLXjDzBmi22RQWfwdpn4j/sGdmZZk+fyhE/ZCNSdfcmumqqI370plnovCRWrXlN"
        "5oDGVL1wE/p33zS7+Sjngxb0S6LtAk3mfmXyLl1DE4WtX4LDNvu/HG2z59Go48UMhNFH/zfHFZEHaDfOhQPbKZQO8h+F"
        "q0uRJFOYGntHDbCUYMQoabJezCLEirHslr7/MssiUXIQhkbwKHcLgBtFnDZbGJllsk3YfbOT7UKavmbRNqZj1ADcYq7F"
        "8l/YncpxPd8KhdYbkDuhE050DCVtTwGd85Gw5C/Uk1OhPMGSbvOw4XbUCOZwM/sfTw9w+hxSsndI5DGYPOwPfkAGsi5U"
        "/yS1dKlC+HPdWPrQPuDHqn/UZj0KylydagLM/mDbLlTLBI0rpVOLIaXAX+G9pzCD3gb2jxZ6vmrLLaXnj2nkhwGYUPEx"
        "WLoMrXuTKaOHWTfrF7nieEgj97U3gx2uCO6gImZH+MA77ctHAZqCImLNRKJsKMutVP0bjFJvGc1FH7ti28gViuiWK0fs"
        "D2q6BvYel0WFK6bajBVQ68NHcYlAuH7lsxKBbQ6WevM52UnpjxbtwFxB6AmhR7f02za1evChOnwpo2sGx2QetjnlU9Z9"
        "aMyuVvwRZcB7pW0mm+i/Xg/SL5ha2ya3VtNnFbRzBaxWNIedHHCHzyl4BJtdkglTyhL3RsDhPH/Wn2/LhBxCqSjhBHPI"
        "mTvyL9MH4FcTLfmVUHL0Pcme+jMdpMeILv/IfdkK+rncOGTtp0HFIfvTMI1lrpJZILNS/ooAW6v7lcZeij7rJE6xDFmT"
        "Q7GFz6d32yCuF4wDSWqG3ETuWJ5BaCSkRnYtUprCElvVhmpFpnXlI9oEcfObEDLaRuItcgo48xaeuWCQ6IHdv3T7NGdM"
        "ATzEYum+fOOxCpdjJg284BK3613TrwH1I47kMgzqvxZauEKAxTTQhmHCpiCTvlDRe7BO1H0K0R8+ZycPmxrlt5HygZ1I"
        "MV8ezTz2rH/SbZJUgzSVG4xsu31MtHOtkiyB8Q2g/U+oFETvd1f3ahTvoiTmaCn5N3C7nvWr2VTAfSXQViwJ1an+mVN4"
        "F8RL5zkh9dAGwfg34Fsw0Rn/QmDZT5nQLmQaeuK9KQn1DPhq"
    ),
    2: (
        "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"
    ),
    4: (
        "eNoBAAT/+1aeQx3QjW3eNbIElO1MhS67a9oWrk2D4zO8oEUZ74hyAttWtkGX5xVce783148h/1RwsNM5kiRrrfpPFsiL"
        "BOyqZjHxt3sOWPlAoMIid9FeB/+nPpIjzGbrCnhXtNcpp5E8/RvKbjCK3bFGB/JimbswEHzswUoGh+GiOFu/dCTWDOeD"
        "TxnGk9MtZobdFUWv5owoVHrYuRdInfeFOcgQbOJHXsKBCKvrVSejacwagOc+qVvQIZ108mIsyhevfvhBlVu43p0lc0f3"
        "pAC6VfBvMZrJC2HtpjRw3wG/UpAu6qJ2HrfzMpdE0noO+ZRNvy/YbAOO/FG5DjzYiJpI5QJszTD+iT6uD2DF6TqNIXfV"
        "qxJPhPy0Px6U0FqAKPqsYw/MSYjbBVitbyLkuzlkhxSg7bVEJX3gpl0ZxHb0KFOkvXkcS8pn5S+ffdkKS7ll7Jc9JsDa"
        "Y3YG6EmeuBTZQHOW7yO9ZTb8kct4Ekap88YrWHbWCpZmyy3wQoywCGTQO9+REvCYB3LAH1P7kKk0Gsl49FYNpo80vPZ+"
        "KVvIjTjjA1S1n4EZ4lAupdRfAJmA3xtLuP46o4ITVeS+bR6d+X8tV61F1oUq6rNfPNSDA2rgsUOGIs/maEsYrNkMcZv/"
        "YCbAee5AD8ZtroflH0L1abEvj8leB+lxrCrRjjtO3AG1x2kX/alRkgvNcxif9k3DLZRc8RGhN8V6k1XvK7c/zhqJq07X"
        "MWSc8gZXzjR5osMNUO6GH9WcRFrCEfR8p18m6pZ1NX4g3GWwMOZCjLsjZ+kJfrPOQG/6JQGo32OESuoJoWv5Er57KdyL"
        "SrQW+F2W2SlwPLp/+wSvNmzekB1B1LNXDcTkRqECylSC+xt1qtZQM5vhGFmtjUjSvTabHXLawiyRVj2w5WEdpcc4auOE"
        "AkbBpOgUUWeazyWB8k7BFZ5o9zGJI7Vu7zaZvhFfzz6TBfVtvoUs8Qtq438TXP6sRIIF7stwFJr3R30O05Iusv5aH3TV"
        "jzO07EgKpmK6O+J3CJXPWQ971I4eTGrdpyjpgrZJHGHaoT3IUa+KJ846mN5nHLZPjDXbvSRgo+hNbgjMqD+J8RNkwyyG"
        "2HEb85ArUsSqQ//oO1y4mwT2wTd1WQ2e/cV8MgDrlWgX+LxMCXftoipZxvpxA6o/iPMWw3k5nOIgUtF9rgZJ/Jw4sl3L"
        "AeGDaCKgis0rb+RHhBjVk7I94SFuUovSt0MggeCoXTXXirYQQnrmlRzTTmmzL9uOWhH4umUymOt0JsVTi90VcjKo9Uy8"
        "EHix/QxepcQnUPR8YxPHpOc6DJj1WHXJKgWX+2tT0STApTNh9oALx58n7kpzqwDNh0Ua4KNgCfC5QX/SIJli3Dc4tP4Q"
    ),
}


def noise_file(cell):
    return f"oled_tint_bluenoise_c{cell}.png"


def bluenoise_png(cell):
    """The mask for `cell` as an 8-bit greyscale PNG, 64 x 64/cell."""
    w, h = 64, 64 // cell
    raw = zlib.decompress(base64.b64decode(BLUENOISE[cell]))
    rows = b"".join(b"\x00" + raw[y * w:(y + 1) * w] for y in range(h))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def write_bluenoise(shaders_dir):
    """Put the masks in ReShade's Textures dir (beside Shaders) unless they're
    already there unchanged."""
    out = []
    for cell in BLUENOISE:
        path = os.path.join(os.path.dirname(shaders_dir.rstrip("/")), "Textures", noise_file(cell))
        png = bluenoise_png(cell)
        out.append(path)
        try:
            with open(path, "rb") as f:
                if f.read() == png:
                    continue
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, path)
    return out


# Part of every effect's name, so a plugin update never reuses an effect
# built by older code: the whole source, since the name is made from the
# inputs, not from the effect text (which the helper writes).
with open(os.path.abspath(__file__), "rb") as _f:
    FX_VERSION = hashlib.sha1(_f.read()).hexdigest()[:8]


# Where the panel needs the dither: the bad band, measured by a backlight
# Info overlay: lines in Steam's performance overlay (mangoapp, MangoHud
# 0.8). Steam writes mangoapp's config to a file named by MANGOHUD_CONFIGFILE
# in mangoapp's environment and mangoapp reloads it on change; our block goes
# at its end (custom_text = a label, exec = a command's output beside it),
# re-added when Steam rewrites the file, removed when the overlay is off.
# With Steam's overlay off (no_display) the block shows alone (legacy_layout
# 0: only listed items). The text itself is a small file read by those execs.
OSD_TEXT = "/tmp/oled-tint-osd.txt"
STATE_FILE = "/tmp/oled-tint-state.json"     # for the camera tool, see _publish_state
OSD_BEGIN, OSD_END = "# >>> oled-tint (OLED Tint's info overlay)", "# <<< oled-tint"
OSD_HIDDEN = "#oled-tint-was:no_display"
OSD_LABELS = ("Tint", "Backlight", "Effect")
# One thin strip like Steam's performance overlay level 2 (a horizontal bar
# across the top, photographed 2026-10-30). Steam's presets own the layout of
# the single mangoapp window, so with Steam's overlay on our items just
# follow its strip in its own style (short text, they run to the edge); with
# it off we draw the strip ourselves. ASCII and "·" only (the overlay font
# has no "●").
OSD_STYLE = ("horizontal", "width=1920", "font_size=38", "position=top-left",
             "background_alpha=0.5", "background_color=000000")


# Text colour of our strip by state (mangoapp has no colour for custom items
# beyond the one text_color, so the whole strip changes, as Steam's labels
# do per kind). Only when it's drawn alone: text_color would also recolour
# Steam's own values.
OSD_COLORS = {"dimming": "5AB4FF", "correcting": "4CD964", "band": "FF9F0A", "off": "9A9A9A"}


def osd_block(steam_off, color=None):
    lines = [OSD_BEGIN]
    if steam_off:
        lines += ["legacy_layout=0", *OSD_STYLE]
        if color:
            lines.append(f"text_color={OSD_COLORS.get(color, OSD_COLORS['off'])}")
    lines += [f"exec=sed -n {i}p {OSD_TEXT}" for i in range(1, len(OSD_LABELS) + 1)]
    return lines + [OSD_END]


def osd_merge(text, on, color=None):
    """Steam's mangoapp config with our block in it (on) or without it (off),
    no_display set aside while the block shows and given back after."""
    lines = text.splitlines()
    if OSD_BEGIN in lines and OSD_END in lines:
        a, b = lines.index(OSD_BEGIN), lines.index(OSD_END)
        lines = lines[:a] + lines[b + 1:]
    lines = ["no_display" if l == OSD_HIDDEN else l for l in lines]
    if on:
        steam_off = "no_display" in lines
        lines = [OSD_HIDDEN if l == "no_display" else l for l in lines] + osd_block(steam_off, color)
    return "\n".join(lines) + "\n"


# floor dimming dimming, for comparing by eye: the backlight held at SOFT_FLOOR
# (above the band, the panel's clean mode) and the picture darkened in the
# shader to the light the requested backlight would give. No dither: the
# plain approach. The light per backlight is the panel's white as measured
# (oled-measure sweep, 2026-10-24; not proportional to the value: 40 gives
# ~1/11 of 531, not 1/13).
SOFT_FLOOR = 543
# The panel's band edge wanders: on 2026-10-29 543 showed the in-band look
# (grey 4 green, RESULTS 28). `floor_backlight` in the settings moves the
# floor for a camera test (e.g. 600) without a new build; kept above the
# band and inside the measured white curve.
FLOOR_RANGE = (531, 698)


DEFAULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.json")   # a packaged build's bundled settings
SUPPORTED_MODEL = "AYN Odin 2 Portal"      # the panel every table was measured on
SUPPORTED_MAX = 4096                        # its backlight range (40-4096 raw)


def unsupported_reason(model, backlight_dir):
    """Why this device can't run the correction, in words for the sidebar, or
    None. The curves, dither tables and the band (66-530) are this panel's:
    on another panel they would tint, not fix."""
    if SUPPORTED_MODEL not in (model or ""):
        return f"made for the {SUPPORTED_MODEL}, this is {model or 'an unknown device'}"
    if not backlight_dir:
        return "no backlight control found"
    try:
        with open(os.path.join(backlight_dir, "max_brightness")) as f:
            top = int(f.read().strip())
    except (OSError, ValueError):
        return None             # can't tell: don't block the device it was made for
    if top != SUPPORTED_MAX:
        return f"backlight range 0-{top}, expected 0-{SUPPORTED_MAX} (another panel?)"
    return None


def floor_of(settings):
    try:
        v = int(settings.get("floor_backlight") or SOFT_FLOOR)
    except (TypeError, ValueError):
        return SOFT_FLOOR
    return max(FLOOR_RANGE[0], min(FLOOR_RANGE[1], v))
WHITE = [(40, 10617), (46, 11390), (53, 11652), (60, 13434), (69, 17472), (79, 20973), (91, 25661),
         (104, 29786), (119, 33924), (136, 41185), (156, 46394), (179, 52801), (205, 59522),
         (235, 66369), (269, 74800), (308, 82491), (353, 92379), (405, 101289), (464, 113128),
         (531, 114777), (609, 130229), (698, 148969)]


def white_at(b):
    """The panel's white light at backlight b (measured, log-interpolated)."""
    pts = WHITE
    if b <= pts[0][0]:
        return pts[0][1] * b / pts[0][0]
    for (b0, w0), (b1, w1) in zip(pts, pts[1:]):
        if b <= b1:
            f = math.log(b / b0) / math.log(b1 / b0)
            return math.exp(math.log(w0) + f * (math.log(w1) - math.log(w0)))
    return pts[-1][1] * b / pts[-1][0]


def soft_wanted(target, floor=SOFT_FLOOR):
    """floor dimming only inside the band: below it the panel is clean at its
    own backlight (and at 543 its darkest codes are relatively brighter: the
    bottom looked too bright), above it there's nothing to do."""
    return bool(target) and SNAP_ZONE[0] <= target < floor


def soft_gain(target, actual):
    """The picture's light scale that makes backlight `actual` look like
    `target`: 1 when the target isn't below it."""
    if not target or not actual or target >= actual:
        return 1.0
    return white_at(target) / white_at(actual)


# The bottom edge of the band isn't stable: band_edge.py (2026-10-25, grey 16
# at 56-72, up and down, twice) found 56-62 always clean, 70+ always in the
# bad mode, and 64-68 either, fresh each time (not hysteresis: on one pass up
# 66 was bad and 68 clean). The frontend steps the backlight out of this zone
# (band_snap), so the correction always knows the panel's mode. The edge
# wanders: on 2026-10-26 (snap certainly off) 56 was clean, 58-60 either and
# 62 already bad (grey 4 19x); the zone covers both days: to 56 or 70.
SNAP_ZONE = (57, 69)
# sweep (2026-10-24): grey 32 jumps from neutral to green between backlight 60
# and 69 and is neutral again from about 530. Outside it the panel is clean.
# Checked against the raw backlight (not the BRIGHTNESS_BIN bins: raw 540
# binned to 549, and a 310 calibration ran on a 310/450 blend at 321). The
# top is where the band ends by measurement (~530).
DITHER_BAND = (66, 530)       # 62 measured clean: the dither made it 0.4-0.85x too dark


# A calibration as one portable file: a profile's steps plus the panel's
# camera tables (dither floor, trims). Version 1.
CALIBRATION_FORMAT = "oled-tint-calibration"
EXPORT_NAME = "oled-tint-calibration.json"


def export_calibration(name, steps, tables, settings):
    return {
        "format": CALIBRATION_FORMAT, "version": 1,
        "name": name,
        "made": time.strftime("%Y-%m-%d %H:%M"),
        "device": _read_text("/sys/firmware/devicetree/base/model").strip("\x00 \n"),
        "steps": steps,
        "dither_tables": tables,
        "shadow_curve": settings.get("shadow_curve", "g22"),
        "dither_temporal": settings.get("dither_temporal") or False,
    }


def parse_calibration(data):
    """(name, steps, tables, extras) from an exported calibration, or raises
    ValueError with a reason a user can act on."""
    if not isinstance(data, dict) or data.get("format") != CALIBRATION_FORMAT:
        raise ValueError("not an OLED Tint calibration file")
    if int(data.get("version", 0)) > 1:
        raise ValueError("made by a newer plugin version; update the plugin")
    steps = _parse_steps(data.get("steps"))
    if not steps:
        raise ValueError("the file has no usable steps")
    tables = {str(k): v for k, v in (data.get("dither_tables") or {}).items()}
    bad = [k for k, v in tables.items() if not (isinstance(v, dict) and _table_ok(k, v))]
    if not bad and len(consistent_tables(tables)) != len(tables):
        bad = [k for k in tables if k not in consistent_tables(tables)]
    if bad:
        # All or nothing: a table that fails here would break every refresh
        # once saved.
        raise ValueError(f"panel table(s) {', '.join(sorted(bad))} incomplete or broken; nothing imported")
    name = str(data.get("name") or "Imported")[:40]
    extras = {k: data[k] for k in ("shadow_curve", "dither_temporal") if k in data}
    if extras.get("dither_temporal") is True:
        extras["dither_temporal"] = "golden"       # from before there were modes, as on load
    if "dither_temporal" in extras and extras["dither_temporal"] not in (False, *TEMPORAL_MODES):
        raise ValueError(f"unknown dither mode {extras['dither_temporal']!r}; nothing imported")
    return name, steps, tables, extras


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


class TableError(ValueError):
    pass


def _need(cond, what):
    # Explicit, not assert: asserts vanish under python -O (review 17).
    if not cond:
        raise TableError(what)


def _table_ok(key, t):
    """A camera dither table the plugin can use: complete per-code light for
    every channel up to the hand-over, a reference curve, positive trims;
    whole numbers where the shader divides or indexes, finite values
    everywhere. Anything else is dropped (logged) instead of breaking every
    refresh."""
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    whole = lambda x: isinstance(x, int) and not isinstance(x, bool)
    try:
        _need(isinstance(key, str) and key.isdigit() and str(int(key)) == key and int(key) > 0,
              "brightness key must be a plain number")
        _need(isinstance(t, dict), "not a table")
        V = t.get("V")
        _need(whole(V) and 4 <= V <= 255, "hand-over code V must be a whole number 4-255")
        _need(t.get("cell", 1) in BLUENOISE, "unsupported cell height")
        for ch in "RGB":
            Lc = t["L"][ch]
            _need(isinstance(Lc, list) and len(Lc) >= V + 1 and num(Lc[V]) and Lc[V] > 0, "light at V missing")
            # Black and the hand-over measured; codes between may be
            # unmeasured (-1 or None, skipped by _rungs), nothing else.
            _need(num(Lc[0]) and Lc[0] >= 0, "code 0 must be measured")
            _need(all(x is None or num(x) for x in Lc[:V + 1]), "light per code must be finite numbers")
        _need(all(num(c) and num(y) for c, y in t["yref"]), "reference must be finite numbers")
        _need(any(c > 0 and y > 0 for c, y in t["yref"]), "empty reference")
        _need(set(t.get("trims") or {}) <= {"still", *TEMPORAL_MODES}, "unknown dither mode in trims")
        for trim in [t.get("trim"), *(t.get("trims") or {}).values()]:
            if trim:
                codes = trim["codes"]
                _need(all(whole(c) for c in codes) and all(0 < a < b for a, b in zip(codes, codes[1:]))
                      and 0 < codes[0] and codes[-1] < V, "trim codes must rise strictly, whole, inside 1..V-1")
                for ch in "RGB":
                    _need(len(trim[ch]) == len(codes) and all(num(g) and g > 0 for g in trim[ch]),
                          "trim gains must be finite and positive")
        return True
    except (TableError, KeyError, TypeError, IndexError, ValueError, AttributeError) as e:
        decky.logger.error(f"dither table {key!r} unusable, ignored: {e!r}")
        return False


def consistent_tables(tables):
    """Tables that can be blended together: one hand-over code (V) for all;
    a minority with another V is dropped (logged)."""
    if not tables:
        return {}
    vs = [int(t["V"]) for t in tables.values()]
    common = max(set(vs), key=vs.count)
    for k, t in tables.items():
        if int(t["V"]) != common:
            decky.logger.error(f"dither table {k!r} has hand-over {t['V']}, the others {common}: ignored")
    return {k: t for k, t in tables.items() if int(t["V"]) == common}


def _blend_tables(a, b, f):
    """A table between two calibrated ones, f in 0..1 from a to b: each
    channel's light per code (normalised to the hand-over code, so only the
    shape is blended), the reference curve and every mode's trim, log-blended.
    Codes measured in only one of the two stay unmeasured."""
    V = int(a["V"])
    lerp = lambda x, y: math.exp(math.log(x) * (1 - f) + math.log(y) * f)
    L = {}
    for ch in "RGB":
        la, lb = a["L"][ch], b["L"][ch]
        na, nb = la[V], lb[V]
        row = []
        for x, y in zip(la, lb):
            if x is None or y is None or x < 0 or y < 0:
                row.append(-1.0)
            elif x == 0 or y == 0:
                row.append(0.0)
            else:
                row.append(lerp(x / na, y / nb) * lerp(na, nb))
        L[ch] = row
    # Every mode (a legacy "trim" stands in where a mode has none, as in
    # trim_for); both sides resampled on one grid of codes with _trim_at
    # (log-interpolated, 1 where a side has none): pairing samples by
    # position mixed unrelated codes when the grids differed (review 12).
    trims = {}
    for mode in ("still", *TEMPORAL_MODES):
        ta, tb = trim_for(a, mode), trim_for(b, mode)
        if not (ta or tb):
            continue
        codes = sorted(set((ta or {}).get("codes", [])) | set((tb or {}).get("codes", [])))
        trims[mode] = {"codes": codes, **{c: [lerp(_trim_at(ta, c, k, V), _trim_at(tb, c, k, V)) for k in codes]
                                          for c in "RGB"}}
    return {"V": V, "L": L, "yref": a["yref"], "cell": a.get("cell", 1), "trims": trims,
            "blend": [f, a.get("source"), b.get("source")]}


def dither_weight(tables, b, raw=None):
    """(table, weight) for backlight b: inside the bad band a table for this
    backlight, outside it none. Never a weight in between: that leaves a share
    of pixels undithered, on the bright green pedestal (a green speckle seen
    at 92 and 384). Between two calibrated steps the table is blended from
    both in log brightness: a neighbouring step's table alone didn't fit (at
    230 the 310 table left grey 4 3x too bright), the panel changes too fast
    with the backlight there. Beyond the outermost tables, the outermost."""
    steps = sorted(int(k) for k in (tables or {}) if int(k) > 0)
    x = raw if raw is not None else b          # band membership: the raw backlight
    if not steps or not b or not x or not DITHER_BAND[0] <= x <= DITHER_BAND[1]:
        return (None, 0.0)
    if b <= steps[0]:
        return (tables[str(steps[0])], 1.0)
    if b >= steps[-1]:
        return (tables[str(steps[-1])], 1.0)
    hi = next(s for s in steps if s >= b)
    lo = max(s for s in steps if s <= b)
    if hi == lo:
        return (tables[str(lo)], 1.0)
    f = math.log(b / lo) / math.log(hi / lo)
    key = (lo, hi, round(f, 3))
    cache = _BLEND_CACHE.get(id(tables))
    if cache is None or cache[0] is not tables:
        cache = (tables, {})
        _BLEND_CACHE.clear()
        _BLEND_CACHE[id(tables)] = cache
    if key not in cache[1]:
        cache[1][key] = _blend_tables(tables[str(lo)], tables[str(hi)], f)
    return (cache[1][key], 1.0)


_BLEND_CACHE = {}


def resolve_step(step):
    """(tints, lifts, hues) for one step, auto levels filled in."""
    return tuple(resolve(step.get(k) or _auto()) for k in KINDS)


def interpolate(steps, brightness):
    """Resolved (tints, lifts, hues), piecewise-linear in log(brightness)
    between steps."""
    pts = sorted(steps, key=lambda p: p["b"])
    if brightness <= pts[0]["b"]:
        return resolve_step(pts[0])
    if brightness >= pts[-1]["b"]:
        return resolve_step(pts[-1])
    lb = math.log(brightness)
    for lo, hi in zip(pts, pts[1:]):
        if lo["b"] <= brightness <= hi["b"]:
            if hi["b"] == lo["b"]:
                return resolve_step(lo)     # two steps at one brightness (hand-edited)
            f = (lb - math.log(lo["b"])) / (math.log(hi["b"]) - math.log(lo["b"]))
            return tuple(
                [a + f * (b - a) for a, b in zip(ca, cb)]
                for ca, cb in zip(resolve_step(lo), resolve_step(hi)))
    return (_neutral(),) * len(KINDS)


def _migrate_v3(old):
    """A level list from the 10-level layout, with the same resolved curve."""
    new = [old[LEVELS_V3.index(x)] if x in LEVELS_V3 else None for x in LEVELS]
    # Above the last set level a curve fades to neutral by the next level,
    # which is now closer: spell out the old, longer fade.
    last = max((i for i, v in enumerate(old) if v is not None), default=None)
    if last is not None and last + 1 < len(LEVELS_V3):
        x0, x1 = LEVELS_V3[last], LEVELS_V3[last + 1]
        for i, x in enumerate(LEVELS):
            if x0 < x < x1:
                new[i] = old[last] * (x1 - x) / (x1 - x0)
    return new


FACTORY = "Factory"


def _factory():
    return [{"b": b, **{k: _auto() for k in KINDS}} for b in DEFAULT_STEPS]


def _parse_steps(steps):
    """Validated steps from a settings file, older layouts migrated; None if
    unusable."""
    if not isinstance(steps, list) or not steps:
        return None
    steps = copy.deepcopy(steps)
    for p in steps:
        for k in KINDS:
            old = p.get(k) or [None] * len(LEVELS_V3)
            if len(old) == len(LEVELS_V3):
                p[k] = _migrate_v3(old)
    if sorted(int(p["b"]) for p in steps) == [40, 100, 320, 800, 2000, 4096]:
        kept = {OLD_STEPS[int(p["b"])]: p for p in steps if int(p["b"]) in OLD_STEPS}
        steps = [dict(kept[b], b=b) if b in kept else {"b": b, "v": _auto(), "l": _auto()}
                 for b in DEFAULT_STEPS]
        decky.logger.info("moved calibration to the measured brightness steps")
    try:
        parsed = [{"b": int(p["b"]),
                   **{k: [None if v is None else float(v) for v in p.get(k) or _auto()] for k in KINDS}}
                  for p in steps
                  if all(len(p.get(k) or _auto()) == len(LEVELS) for k in KINDS)]
    except (TypeError, ValueError, KeyError):
        return None
    # Interpolation works in log brightness: a step at 0 (or below) broke
    # every refresh; so did a non-finite value.
    if any(not 0 < p["b"] <= 65535 for p in parsed) or any(
            v is not None and not math.isfinite(v) for p in parsed for k in KINDS for v in p[k]):
        return None
    return parsed or None


def _neutral():
    return [0.0] * len(LEVELS)


def _auto():
    """A step with nothing set: every level follows its neighbours."""
    return [None] * len(LEVELS)


class Plugin:
    async def _main(self):
        self.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        self.lut_dir = decky.DECKY_PLUGIN_RUNTIME_DIR
        os.makedirs(self.lut_dir, exist_ok=True)
        self.settings_mtime = None
        self.revision = 0
        self.settings = {"enabled": True, "engine": "reshade", "steps": _factory(),
                         "profile": FACTORY, "profiles": {}}
        self.fx_dir = os.path.join(decky.DECKY_USER_HOME, ".local/share/gamescope/reshade/Shaders")
        os.makedirs(self.fx_dir, exist_ok=True)
        write_bluenoise(self.fx_dir)
        self._seed_defaults()
        self._load_settings()
        # A camera run that died may have left this on; it would make the
        # dither move (and jump) in Steam's UI.
        if self.settings.get("dither_temporal_ui"):
            self.settings["dither_temporal_ui"] = False
            self._save_settings()

        self.backlight = self._find_backlight()
        self.unsupported = unsupported_reason(
            _read_text("/sys/firmware/devicetree/base/model").strip("\x00 \n"), self.backlight)
        if self.unsupported:
            decky.logger.error(f"not supported: {self.unsupported}")
        self.display = ":0"
        self.helper = Helper(self.display)
        self.preview = None        # None = normal; a step dict, or {"v": None} (bypass) in wizard
        self.applied = None         # (key, atom values) currently set, or None when cleared
        self.slot = 0
        self.conflict = False
        # The calibration is for the built-in panel; leave docked screens alone.
        self.external = False
        # Per-game effects (settings "app_effects": {appid: {"effect", "technique"}})
        # take the single ReShade slot; the correction moves to the look meanwhile.
        self.focused = None
        self.app_on = None         # the app effect currently set, or None
        self.fast_until = 0.0
        self.last_ping = 0.0
        self.wake = asyncio.Event()
        self.current = _neutral()
        self.current_l = _neutral()
        self.current_h = _neutral()
        self.backlight_now = None
        self.dithering = False      # the effect on screen dithers (see _sync_vibrance)
        # Steam's Quick Access / main menu is open (the frontend tells us).
        # ReShade only reaches the focused app, so overlays stayed green; while
        # one is open the correction moves to gamescope's look, which covers
        # every layer (coarser, no dither: fine for a menu and the dimmed game
        # behind it), and back to ReShade when it closes.
        self.overlay_open = False
        self.vibrance_now = None    # last seen VIBRANCE_ATOM value
        self.task = asyncio.get_event_loop().create_task(self._loop())
        decky.logger.info(f"OLED Tint started, backlight={self.backlight}")

    async def _unload(self):
        if getattr(self, "task", None):
            self.task.cancel()
        self._osd_sync(force_off=True)
        self._clear_atoms(force=True)
        self.helper.stop()

    async def _uninstall(self):
        self._clear_atoms(force=True)
        for f in glob.glob(os.path.join(self.fx_dir, FX_PREFIX + "*.fx")):
            os.remove(f)
        textures = os.path.join(os.path.dirname(self.fx_dir.rstrip("/")), "Textures")
        for f in glob.glob(os.path.join(textures, "oled_tint_bluenoise*.png")):
            os.remove(f)

    # ---- settings ----

    def _seed_defaults(self):
        """First start of a packaged build: a defaults.json next to main.py
        (e.g. the Lite package's bundled profile) becomes the settings file."""
        bundled = DEFAULTS_FILE
        if os.path.exists(bundled) and not os.path.exists(self.settings_path):
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            shutil.copy(bundled, self.settings_path)
            decky.logger.info("first start: settings seeded from defaults.json")

    def _load_settings(self):
        try:
            mtime = os.path.getmtime(self.settings_path)
        except OSError:
            self.settings_error = None      # gone: nothing on disk left to keep
            return
        if mtime == self.settings_mtime:
            return
        try:
            with open(self.settings_path) as f:
                data = json.load(f)
            # Everything into `new` first and checked, then taken over at
            # once: a bad value halfway used to leave half the file applied.
            new = {}
            new["enabled"] = bool(data.get("enabled", True))
            new["engine"] = data.get("engine", "reshade") if data.get("engine", "reshade") in ("reshade", "look") else "reshade"
            new["simple"] = bool(data.get("simple", False))
            new["crush"] = max(0, min(MAX_CRUSH, int(data.get("crush", 0))))
            new["dither"] = bool(data.get("dither", False))
            t = data.get("dither_temporal", False)
            # Off, or a mode; true is from before there were modes.
            new["dither_temporal"] = ("golden" if t is True else t if t in TEMPORAL_MODES else False)
            new["dither_temporal_ui"] = bool(data.get("dither_temporal_ui", False))
            new["menu_correction"] = bool(data.get("menu_correction", True))
            new["band_snap"] = bool(data.get("band_snap", True))
            new["osd"] = bool(data.get("osd", False))
            new["dimming"] = "band" if data.get("dimming") == "band" else "soft"
            t = data.get("soft_target")
            new["soft_target"] = int(t) if isinstance(t, (int, float)) and 0 < t <= 65535 else None
            # Steam's UI: flat dark areas where the (still) dither shows as grain.
            new["dither_ui"] = bool(data.get("dither_ui", True))
            c = data.get("shadow_curve", "native")
            new["shadow_curve"] = c if c in SHADOW_CURVES else "native"
            # Camera-measured panel tables per backlight step (see oled-measure).
            if data.get("vibrance_saved") is not None:
                new["vibrance_saved"] = str(data["vibrance_saved"])
            new["dither_tables"] = consistent_tables({str(k): v for k, v in (data.get("dither_tables") or {}).items()
                                                      if _table_ok(k, v)})
            new["app_effects"] = {str(k): v for k, v in (data.get("app_effects") or {}).items()
                                            if isinstance(v, dict) and v.get("effect")}
            if isinstance(data.get("dither_tables_backup"), dict):
                new["dither_tables_backup"] = data["dither_tables_backup"]   # from an import
            # Games the correction is switched off for: {app id: name}.
            new["app_off"] = {str(k): str(v) for k, v in (data.get("app_off") or {}).items()}
            new["steps"] = _parse_steps(data.get("steps")) or self.settings["steps"]
            profiles = {name: parsed for name, steps in (data.get("profiles") or {}).items()
                        if name != FACTORY and (parsed := _parse_steps(steps))}
            if not profiles and "profiles" not in data:
                # Before profiles existed there was one calibration, set by eye.
                profiles = {"Eye": copy.deepcopy(new["steps"])}
            new["profiles"] = profiles
            active = data.get("profile")
            new["profile"] = active if active == FACTORY or active in profiles else next(iter(profiles), FACTORY)
            # The working steps are the truth for the active profile, also when
            # something else (the camera tool) edited the file.
            stale = False
            if new["profile"] != FACTORY:
                on_disk = _parse_steps((data.get("profiles") or {}).get(new["profile"]))
                stale = on_disk != new["steps"]
                profiles[new["profile"]] = copy.deepcopy(new["steps"])
            self.settings.update(new)
            self.settings_mtime = mtime
            self.settings_error = None
            self.settings_base = copy.deepcopy(self.settings)
            self.revision += 1
            if not data.get("profiles") or stale:
                # New Eye profile, or the file's copy of the active profile is
                # behind its working steps: write it out now.
                self._save_settings()
        except Exception as e:
            decky.logger.error(f"Bad settings file, keeping current, not writing it until it reads: {e}")
            self.settings_mtime = mtime
            self.settings_error = str(e)

    def _save_settings(self):
        if getattr(self, "settings_error", None):
            # Unreadable on disk (a hand edit gone wrong): writing would replace
            # it with what's in memory, at start the bare defaults, and the
            # profiles and camera tables were gone (review 10-31 #4). Read
            # again once fixed (new mtime) or deleted.
            decky.logger.warning(f"settings not saved, the file doesn't read: {self.settings_error}")
            return
        # The active profile follows every edit; Factory stays as it is.
        if self.settings["profile"] != FACTORY:
            self.settings["profiles"][self.settings["profile"]] = copy.deepcopy(self.settings["steps"])
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        # The camera tool writes this file too: a shared lock around writes
        # and a temp file of our own (a shared one let either truncate the
        # other's). See oled-measure/plugin.py.
        fd = os.open(self.settings_path + ".lock", os.O_RDONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                newer = (self.settings_mtime is not None
                         and os.path.getmtime(self.settings_path) != self.settings_mtime)
            except OSError:
                newer = False
            if newer:
                # Written meanwhile (the camera tool): keep what it wrote and
                # put only our own changes since the last read on top.
                try:
                    with open(self.settings_path) as f:
                        disk = json.load(f)
                    base = getattr(self, "settings_base", {})
                    mine = {k: v for k, v in self.settings.items() if base.get(k) != v}
                    gone = [k for k in base if k not in self.settings]   # removed by us since
                    disk.update(copy.deepcopy(mine))
                    for k in gone:
                        disk.pop(k, None)
                    self.settings = disk
                    decky.logger.info(f"settings changed on disk meanwhile: merged ours ({', '.join(sorted(mine)) or 'nothing'}) into it")
                except (OSError, ValueError) as e:
                    decky.logger.error(f"settings merge failed, ours written: {e}")
            tmp = f"{self.settings_path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.settings, f, indent=2)
            os.replace(tmp, self.settings_path)
            # After a merge, read it back on the next round (validated).
            self.settings_mtime = None if newer else os.path.getmtime(self.settings_path)
            self.settings_base = copy.deepcopy(self.settings)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ---- hardware ----

    def _find_backlight(self):
        for d in sorted(glob.glob("/sys/class/backlight/*")):
            if os.path.exists(os.path.join(d, "brightness")):
                return d
        return None

    def _read_brightness(self):
        if not self.backlight:
            return None
        try:
            with open(os.path.join(self.backlight, "brightness")) as f:
                return int(f.read().strip())
        except Exception:
            return None

    # ---- gamescope ----

    def _read_atoms(self):
        r = self.helper.run(["xprop", "-root", *LOOK_ATOMS, RESHADE_ATOM, EXTERNAL_ATOM, FOCUS_ATOM, FOCUS_GFX_ATOM,
                              VIBRANCE_ATOM])
        out = {}
        if r is None or r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            name, _, rest = line.partition("(")
            if "=" in rest:
                out[name.strip()] = rest.split("=", 1)[1].strip().strip('"')
        return out

    def _set_atoms(self, paths):
        """True when every write went through. paths may be shorter than
        LOOK_ATOMS: unlisted atoms are left alone."""
        ok = True
        for atom, path in zip(LOOK_ATOMS, paths):
            r = self.helper.run(["xprop", "-root", "-f", atom, "8u", "-set", atom, path])
            ok = ok and r is not None and r.returncode == 0
        return ok

    def _remove_ours(self, names):
        """Remove those of the atoms that hold our output; another filter's
        stay. True when nothing of ours is left. If gamescope doesn't answer,
        nothing is removed (ownership unknown: a foreign look could go,
        review 17) and it's tried again later."""
        atoms = self._read_atoms()
        if atoms is None:
            return False
        ok = True
        for atom in names:
            if not (atoms.get(atom) and self._ours(atom, atoms[atom])):
                continue
            r = self.helper.run(["xprop", "-root", "-remove", atom])
            ok = ok and r is not None and r.returncode == 0
        return ok

    def _clear_look(self):
        return self._remove_ours(LOOK_ATOMS)

    def _clear_reshade(self):
        return self._remove_ours((RESHADE_ATOM,))

    def _finish_switch(self):
        """The other engine's output off after a switch; until that has
        worked the switch stays pending and is retried every round (both
        engines stacked otherwise, review 15)."""
        stale = getattr(self, "stale_engine", None)
        if not stale:
            return True
        ok = self._clear_look() if stale == "look" else self._clear_reshade()
        if ok:
            self.stale_engine = None
        return ok

    def _clear_atoms(self, force=False):
        """Our output off. Until that has worked the state stays as it is
        (pending, vibrance still held). Forced (unload): a few tries; if our
        effect still can't be removed, the vibrance stays held (it would mix
        an active dither's channels) and its saved value is given back at
        the next start (review 18)."""
        ok = self._remove_ours((*LOOK_ATOMS, RESHADE_ATOM))
        for _ in range(3 if force and not ok else 0):
            time.sleep(0.3)
            ok = self._remove_ours((*LOOK_ATOMS, RESHADE_ATOM))
            if ok:
                break
        if not ok and not force:
            self.deferred = True
            return False
        if not ok:
            decky.logger.error("our effect couldn't be removed on unload; vibrance left held")
            self.applied = self.app_on = None
            return False
        self.applied = None
        self.app_on = None
        self.dithering = False
        self.menu_look = False
        self._sync_vibrance()
        return True

    def _sync_vibrance(self):
        """Hold vibrance at 1.0 while the dither is on screen; otherwise give
        back the value it had (kept in the settings across restarts)."""
        saved = self.settings.get("vibrance_saved")
        now = self.vibrance_now
        # Opening a menu moves the correction to the look for a moment; keep
        # holding the vibrance meanwhile, or the game's colours would jump.
        # Only while our correction is on screen: turning it off (or unloading)
        # with a menu open gives the vibrance back right away.
        holding = self.dithering or (getattr(self, "menu_look", False) and saved is not None and bool(self.applied))
        if holding and not self.external:
            if now is not None and now != VIBRANCE_NATIVE:
                # Set by the user or Steam (also mid-hold): that's the value to
                # give back later.
                self.settings["vibrance_saved"] = now
                self._save_settings()
                decky.logger.info(f"vibrance {now} held at 1.0 while dithering")
                self.helper.run(["xprop", "-root", "-f", VIBRANCE_ATOM, "32c", "-set", VIBRANCE_ATOM, VIBRANCE_NATIVE])
                self.vibrance_now = VIBRANCE_NATIVE
        elif saved is not None:
            if now == VIBRANCE_NATIVE:
                r = self.helper.run(["xprop", "-root", "-f", VIBRANCE_ATOM, "32c", "-set", VIBRANCE_ATOM, str(saved)])
                if r is None or r.returncode != 0:
                    return          # kept: tried again next round (it was lost on a failed xprop)
                self.vibrance_now = str(saved)
                decky.logger.info(f"vibrance back to {saved}")
            self.settings.pop("vibrance_saved", None)
            self._save_settings()

    def _ours(self, atom, value):
        if atom == RESHADE_ATOM:
            effects = {e["effect"] for e in (self.settings.get("app_effects") or {}).values()}
            # The one set right now counts too, even if the settings just
            # swapped it for another.
            if self.app_on:
                effects.add(self.app_on["effect"])
            return value.startswith(FX_PREFIX) or value in effects
        return value.startswith(self.lut_dir + os.sep)

    def _app_effect(self):
        # The game in front: with a Steam menu open, FOCUSED_APP is Steam's
        # UI while the game (and its effect) is still what's shown.
        return (self.settings.get("app_effects") or {}).get(str(self._game()))

    def _set_app_effect(self, app):
        if self.app_on == app:
            return
        r1 = self.helper.run(["xprop", "-root", "-f", RESHADE_TECHNIQUE_ATOM, "32c",
                              "-set", RESHADE_TECHNIQUE_ATOM, str(app.get("technique", 0))])
        r2 = self.helper.run(["xprop", "-root", "-f", RESHADE_ATOM, "8u", "-set", RESHADE_ATOM, app["effect"]])
        if not all(r is not None and r.returncode == 0 for r in (r1, r2)):
            self.deferred = True        # not cached: both again next round (review 17)
            return
        self.app_on = app
        if self.applied and self.applied[0][1] == "reshade":
            self.applied = None    # our effect was just replaced
        decky.logger.info(f"app {self._game()}: effect {app['effect']}, correction via look")

    def _fx_name(self, curves, crush, dither, gain=1.0):
        h = hashlib.sha1(FX_VERSION.encode())
        h.update(json.dumps([curves, crush] + (["ladder"] if gain is None else [round(gain, 4)] if gain != 1.0 else []),
                            separators=(",", ":")).encode())
        if dither:
            h.update(json.dumps([dither["w"], dither.get("temporal"), dither.get("curve"), dither["table"]],
                                sort_keys=True, separators=(",", ":")).encode())
        return f"{FX_PREFIX}{h.hexdigest()[:20]}.fx"

    def _apply(self, curves, engine=None, sdr_only=False, gain=1.0):
        """gain: the whole picture's light scaled by it (floor dimming dimming);
        PASS: the ladder's pass-through technique."""
        engine = engine or self.settings.get("engine", "reshade")
        passthru = gain == PASS
        gain = 1.0 if passthru else round(gain, 3)
        # Both looks also while calibrating: with HDR output gamescope shows
        # SDR content (Steam's UI too) through the PQ one, so an SDR-only
        # preview showed the old PQ table.
        crush = self.settings.get("crush", 0)
        dither = None
        if engine == "reshade" and self.settings.get("dither") and self.backlight_now \
                and self.settings.get("dimming") != "soft" \
                and not getattr(self, "safe_mode", False) \
                and (self.settings.get("dither_ui", True) or not self._in_steam_ui()):
            table, w = dither_weight(self.settings.get("dither_tables"), self.backlight_now,
                                     getattr(self, "backlight_raw", None))
            if table and w > 0.01:
                temporal = self.settings.get("dither_temporal") or None
                if self._in_steam_ui() and not self.settings.get("dither_temporal_ui"):
                    temporal = None     # "dither_temporal_ui": for camera runs (animated patches)
                dither = {"table": table, "w": round(w, 2), "temporal": temporal,
                          "curve": self.settings.get("shadow_curve", "native")}
        # Floor dimming keeps the flat gain (Floor 0). A floor-anchored gain
        # (4cbb40c) never ran here (no table at the floor backlight) and must
        # not: the tables' hand-over code is 64, so nothing below grey 64 would
        # dim - at target 57 the darks 9x, white 2.2x too bright (review
        # 2026-10-30). build_fx still takes a floor from a w=0 table.
        dkey = (dither["w"], id(dither["table"]), dither["temporal"], dither["curve"]) if dither else None
        # floor dimming on the ReShade engine: the gain is a technique of one
        # effect (see GAIN_RATIO), switched without a compile.
        ladder = engine == "reshade" and self.settings.get("dimming") == "soft" and (
                dither is None or dither["w"] == 0.0)
        tech = (PASS_TECH if passthru else gain_index(gain)) if ladder else 0
        key = (tuple(tuple(round(t, 1) for t in c) for c in curves), engine, sdr_only, crush, dkey,
               ("L", tech) if ladder else gain)
        if ladder and self.preview is None and self.applied and self.applied[0][:-1] == key[:-1] \
                and isinstance(self.applied[0][-1], tuple) and self.applied[0][-1] != key[-1]:
            self.deferred = True        # pending until it's on screen
            r = self.helper.run(["xprop", "-root", "-f", RESHADE_TECHNIQUE_ATOM, "32c",
                                 "-set", RESHADE_TECHNIQUE_ATOM, str(tech)])
            if r is not None and r.returncode == 0:
                _slider_log(f"B technique {tech} gain {gain:.3f} target {self.settings.get('soft_target')}")
                self.applied = (key,) + tuple(self.applied[1:])
                self.deferred = not self._finish_switch()
            else:
                self.fast_until = max(getattr(self, "fast_until", 0), time.monotonic() + 1.0)    # tried again next round
            return
        if self.applied and self.applied[0] == key:
            # Already on screen; an unfinished switch is retried here.
            self.deferred = not self._finish_switch()
            return
        # Until it's on screen this round's change counts as pending (the
        # camera tool waits for it, see _publish_state).
        self.deferred = True
        t0 = time.monotonic()
        # floor dimming drag: only the gain differs from what's on screen, so
        # follow the slider closer than a new correction would.
        gain_only = bool(self.applied and self.applied[0][:-1] == key[:-1] and self.applied[0][-1] != gain)
        debounce = SOFT_DEBOUNCE if gain_only else DEBOUNCE
        compile_gap = SOFT_COMPILE_GAP if gain_only else COMPILE_GAP
        if self.preview is None and t0 - getattr(self, "last_apply", 0) < debounce:
            # At most one change per DEBOUNCE, whatever moves it (Steam's own
            # slider doesn't tell us it's moving): the loop comes back in
            # 0.1 s and applies where things have settled.
            self.fast_until = max(getattr(self, "fast_until", 0), t0 + 1.0)
            return
        self.last_apply = t0
        if engine == "reshade" and self.preview is None and t0 - getattr(self, "last_compile", 0) < compile_gap:
            self.last_apply = 0.0      # not applied: try again on the next loop round
            self.fast_until = max(getattr(self, "fast_until", 0), t0 + compile_gap + 0.5)
            return
        # A cleanup still open for the engine about to be installed is void:
        # it would delete what goes on screen now (review 16).
        if getattr(self, "stale_engine", None) == engine:
            self.stale_engine = None
        if engine == "reshade":
            self.last_compile = t0
            # gamescope caches effects by name: the name is the content, so a
            # correction seen before is reused, already compiled.
            name = self._fx_name(key[0], crush, dither, None if ladder else gain)
            path = os.path.join(self.fx_dir, name)
            if os.path.exists(path):
                os.utime(path)
            else:
                self.helper.write(path, key[0], "fx", crush, dither, None if ladder else gain)
            t1 = time.monotonic()
            r1 = self.helper.run(["xprop", "-root", "-f", RESHADE_TECHNIQUE_ATOM, "32c",
                                  "-set", RESHADE_TECHNIQUE_ATOM, str(tech)])
            r2 = self.helper.run(["xprop", "-root", "-f", RESHADE_ATOM, "8u", "-set", RESHADE_ATOM, name])
            if not all(r is not None and r.returncode == 0 for r in (r1, r2)):
                # Not on screen: keep what was there (the old look stays, the
                # change stays pending) and try again next round (review 14).
                self.last_apply = 0.0
                return
            if self.applied is None or self.applied[0][1] != "reshade":
                self.stale_engine = "look"
            # Keep the most recently used ones; the oldest go.
            old = sorted(glob.glob(os.path.join(self.fx_dir, FX_PREFIX + "*.fx")), key=os.path.getmtime)[:-FX_KEEP]
            for f in old:
                os.remove(f)
            # The table rides along: while it's alive its id (in the key) can't
            # be handed to a reloaded table that differs.
            self.applied = (key, {RESHADE_ATOM: name}, dither["table"] if dither else None)
            self.deferred = not self._finish_switch()
        else:
            # Alternate file names so gamescope always sees a new path and reloads.
            self.slot ^= 1
            paths = []
            for kind in ("g22", "pq"):
                if kind == "pq" and sdr_only:
                    paths.append(self.applied[1].get(LOOK_ATOMS[1], "") if self.applied else "")
                    continue
                path = os.path.join(self.lut_dir, f"look_{kind}_{self.slot}.cube")
                self.helper.write(path, key[0], kind, crush, None, gain)
                paths.append(path)
            t1 = time.monotonic()
            if not self._set_atoms([p for p in paths if p] if sdr_only else paths):
                self.last_apply = 0.0       # not on screen: pending, again next round
                return
            if (self.applied is None or self.applied[0][1] != "look") and not self.app_on:
                self.stale_engine = "reshade"
            self.applied = (key, dict(zip(LOOK_ATOMS, paths)))
            self.deferred = not self._finish_switch()
        decky.logger.info(f"applied {engine} {key[0]}: build {(t1 - t0) * 1000:.0f} ms, "
                          f"xprop {(time.monotonic() - t1) * 1000:.0f} ms")

    @staticmethod
    def _bin(brightness):
        if not brightness:
            return brightness
        k = round(math.log(brightness) / math.log(BRIGHTNESS_BIN))
        return round(BRIGHTNESS_BIN ** k)

    def _dither_key(self, raw, b):
        """The backlight the dither tables are looked up at: a calibrated
        step's own when the raw backlight is within half a bin of it, else
        the bin."""
        if not raw:
            return b
        half = math.log(BRIGHTNESS_BIN) / 2
        for k in (self.settings.get("dither_tables") or {}):
            if abs(math.log(raw / int(k))) <= half:
                return int(k)
        return b

    def _target(self, brightness):
        if self.preview is not None:
            return None if self.preview["v"] is None else resolve_step(self.preview)
        if not self.settings["enabled"] or brightness is None:
            return None
        if time.monotonic() < getattr(self, "compare_until", 0.0):
            return None         # Compare: a few seconds of the panel as it is
        if self._game() in (self.settings.get("app_off") or {}):
            return None
        return interpolate(self.settings["steps"], brightness)

    def _refresh(self):
        now = time.monotonic()
        if now < STALLED_UNTIL:
            self.deferred = True    # nothing applied this round: not "read and done"
            return      # gamescope stopped answering; let it catch up
        if getattr(self, "ready_since", None) is None or now - self.ready_since < READY_AFTER:
            self.deferred = True
            return      # gamescope hasn't answered steadily yet (starting up)
        recent = [t for t in STALLS if now - t < SAFE_WINDOW]
        if len(recent) >= SAFE_STALLS and not getattr(self, "safe_mode", False):
            self.safe_mode = True
            decky.logger.error(f"{len(recent)} gamescope stalls in {SAFE_WINDOW:.0f} s: safe mode (no dither, no menu switching)")
        raw = self._read_brightness()
        b = self._bin(raw)
        # Dithering follows the backlight; in the wizard, the step shown. A
        # backlight within half a bin of a calibrated table is that table
        # exactly (the bins alone put 310 at 321, a blend).
        shown = (self.preview or {}).get("b")
        self.backlight_raw = shown or raw
        self.backlight_now = shown or self._dither_key(raw, b)
        # floor dimming: the backlight held at the floor (by the frontend), the
        # picture dimmed to the requested level.
        was_dimming = getattr(self, "soft_now", 1.0) < 1.0 and bool(self.applied)
        self.soft_now = 1.0
        self.soft_pass = False
        target = self.settings.get("soft_target")
        floor = floor_of(self.settings)
        soft = self.settings.get("dimming") == "soft" and not shown and raw
        going_down = time.monotonic() < getattr(self, "down_until", 0.0)
        # The frontend about to raise the backlight to the floor from inside
        # the band (floor dimming back on, Compare over, a start): dimmed for
        # the floor first, as from below it (review 10-31 #5).
        going_up = time.monotonic() < getattr(self, "up_until", 0.0) and raw < floor * 0.97
        below = soft and (raw < SNAP_ZONE[0] or going_down or going_up) \
            and self.settings.get("engine", "reshade") == "reshade" and not self._app_effect()
        if soft and target and soft_wanted(target, floor) and raw >= floor * 0.97 and not going_down:
            # Dimmed from wherever the backlight is, as long as it's above
            # the band: with Steam's slider intercepted the last request that
            # got through can leave it at e.g. 560, not exactly the floor
            # (nothing was dimmed then).
            self.soft_now = round(soft_gain(target, raw), 3)
        elif below and target and soft_wanted(target, floor):
            # On the way up from below the band: dimmed for the floor before
            # the frontend raises the backlight to it (a dip, never the flash
            # of the floor undimmed: RESULTS 32).
            self.soft_now = round(soft_gain(target, floor), 3)
        elif below:
            self.soft_pass = True   # the floor's effect stays, passing through
        # While floor dimming holds the panel at the floor, the colour correction
        # is the floor's: taken from the backlight passing by on its way down,
        # every bin of the ramp was a new effect to compile (4 in 6 s, found by
        # the UI smoke test 2026-10-30); a slider drag stuttered.
        curves = self._target(self._bin(floor) if self.soft_now < 1.0 or self.soft_pass else b)
        # Floor dimming going off (correction off, an "Off for <game>" game in
        # front, Compare) with the backlight still at the floor: the dimming
        # stays until the frontend has dropped the backlight to the target (it
        # follows get_snap's "active" within 2 s), as crossDown: a short dip,
        # never the floor undimmed. At most LET_GO_WAIT (no frontend).
        holding = curves is None and soft and raw >= floor * 0.97 and self.soft_now < 1.0
        if not holding:
            self.letgo_from = None
        elif getattr(self, "letgo_from", None) is None and was_dimming:
            self.letgo_from = now
        if holding and getattr(self, "letgo_from", None) is not None and now - self.letgo_from < LET_GO_WAIT:
            curves = interpolate(self.settings["steps"], self._bin(floor))
        self.current, self.current_l, self.current_h = curves or (_neutral(),) * len(KINDS)
        if self.conflict:
            # Another look (e.g. a Steam colour filter) is active; stay out of
            # its way (take our own output off, so the two don't stack), and
            # don't keep holding the vibrance for it.
            if self.applied or self.app_on or getattr(self, "owned_seen", False):
                self._clear_atoms()
                self.owned_seen = False
            else:
                self.dithering = False
                self._sync_vibrance()
            return
        app = self._app_effect()
        if app:
            self._set_app_effect(app)
        elif self.app_on:
            self._clear_reshade()       # while it still counts as ours
            self.app_on = None
            self.applied = None
        idle = curves is not None and max(abs(t) for c in curves for t in c) < 0.05 \
            and getattr(self, "soft_now", 1.0) == 1.0 and not getattr(self, "soft_pass", False)
        if idle and (self.settings.get("crush", 0) or (
                self.settings.get("dither") and dither_weight(self.settings.get("dither_tables"), self.backlight_now,
                                                              getattr(self, "backlight_raw", None))[0])):
            idle = False        # flat curves, but crush or the dither still have work to do
        if self.external or getattr(self, "unsupported", None) or curves is None or idle:
            # Also what a previous instance left on screen (an interrupted
            # restart): `applied` is only what this one set.
            if self.applied or getattr(self, "owned_seen", False):
                ok = self._clear_look()
                if not self.app_on:
                    ok = self._clear_reshade() and ok
                if not ok:
                    # Still on screen: keep saying so (published state, the
                    # vibrance hold) and try again next round (review 17).
                    self.deferred = True
                    return
                self.applied = None
                self.owned_seen = False
            self.deferred = False
        else:
            # Both looks, also for menus: the output runs in HDR, where
            # gamescope uses the PQ look for SDR content too (writing only the
            # G22 one left the menus and Steam's UI uncorrected, green).
            self._apply(curves, "look" if app or self._menu_engine() else None, gain=PASS if getattr(self, "soft_pass", False) else getattr(self, "soft_now", 1.0))
        # On screen and dithering: our ReShade effect, built with a dither.
        self.dithering = bool(self.applied and self.applied[0][1] == "reshade" and self.applied[0][4])
        self._sync_vibrance()

    async def _loop(self):
        last_verify = 0.0
        loop = asyncio.get_event_loop()
        while True:
            try:
                self._load_settings()
                now = loop.time()
                if self.preview is not None and now - self.last_ping > PREVIEW_TIMEOUT:
                    decky.logger.info("calibration page went quiet, leaving preview")
                    self.preview = None
                if now - last_verify > VERIFY_INTERVAL:
                    last_verify = now
                    atoms = self._read_atoms()
                    if atoms is None:
                        self.ready_since = None
                    elif getattr(self, "ready_since", None) is None:
                        self.ready_since = time.monotonic()
                    if atoms is not None:
                        self.external = atoms.pop(EXTERNAL_ATOM, "0") == "1"
                        focused = atoms.pop(FOCUS_ATOM, None)
                        self.focused_gfx = atoms.pop(FOCUS_GFX_ATOM, None)
                        if focused != self.focused:
                            self.focus_since = time.monotonic()
                        self.focused = focused
                        if focused and str(focused) != STEAM_UI_APP:
                            self.last_game = str(focused)
                        self.vibrance_now = atoms.pop(VIBRANCE_ATOM, None)
                        if self.app_on and atoms.get(RESHADE_ATOM) != self.app_on["effect"]:
                            self.app_on = None   # gamescope restarted or someone else set it
                        foreign = [a for a, v in atoms.items() if v and not self._ours(a, v)]
                        self.conflict = bool(foreign)
                        # Our output still on screen, whatever `applied` says
                        # (cleared just below when any atom differs): the
                        # conflict branch must take it off.
                        self.owned_seen = any(v and self._ours(a, v) for a, v in atoms.items()
                                              if a in (*LOOK_ATOMS, RESHADE_ATOM))
                        # gamescope restarted (or someone cleared us): reapply.
                        if self.applied and any(atoms.get(a, "") != v
                                                for a, v in self.applied[1].items() if v):
                            self.applied = None
                self._refresh()
                self._osd_sync()
                self._publish_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"loop error: {e}")
            fast = self.preview is not None or loop.time() < self.fast_until
            try:
                await asyncio.wait_for(self.wake.wait(), FAST_INTERVAL if fast else IDLE_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    # ---- frontend API ----

    async def get_state(self):
        b = self._read_brightness()
        return {
            "enabled": self.settings["enabled"],
            "steps": self.settings["steps"],
            "levels": [round(x * 100, 2) for x in LEVELS],
            "brightness": b,
            "current": [round(t, 1) for t in self.current],
            "current_l": [round(t, 1) for t in self.current_l],
            "current_h": [round(t, 1) for t in self.current_h],
            "max_lift": MAX_LIFT,
            "max_hue": MAX_HUE,
            "engine": self.settings.get("engine", "reshade"),
            "crush": self.settings.get("crush", 0),
            "dither": self.settings.get("dither", False),
            "dither_temporal": self.settings.get("dither_temporal", False),
            "shadow_curve": self.settings.get("shadow_curve", "native"),
            "menu_correction": self.settings.get("menu_correction", True),
            "band_snap": self.settings.get("band_snap", True),
            "osd": self.settings.get("osd", False),
            "dimming": self.settings.get("dimming", "soft"),
            "soft_target": self.settings.get("soft_target"),
            "soft_floor": floor_of(self.settings),
            "soft_gain": getattr(self, "soft_now", 1.0),
            "soft_dimmed": self._soft_dimmed(),
            "snap_zone": list(SNAP_ZONE),
            "safe_mode": getattr(self, "safe_mode", False),
            "dither_ui": self.settings.get("dither_ui", True),
            "dither_ready": bool(self.settings.get("dither_tables")),
            "max_crush": MAX_CRUSH,
            "profile": self.settings["profile"],
            "profiles": [FACTORY] + sorted(self.settings["profiles"]),
            "simple": self.settings.get("simple", False),
            "conflict": self.conflict,
            "external": self.external,
            "max_tint": MAX_TINT,
            "status": self._status(),
            "game": self._game(),
            "game_off": self._game() in (self.settings.get("app_off") or {}),
            "tables": self._tables_info(),
        }

    def _game(self):
        """The game in front, also while a menu over it has the focus; None in
        Steam's own UI."""
        gfx = getattr(self, "focused_gfx", None)
        if gfx:
            return None if str(gfx) == STEAM_UI_APP else str(gfx)
        if self.focused and str(self.focused) != STEAM_UI_APP:
            return str(self.focused)
        if self.focused and not self._in_steam_ui():
            return getattr(self, "last_game", None)
        return None

    def _status(self):
        """What the correction does right now, and if nothing, why: one line
        for the top of the sidebar. state: on / off / paused."""
        compare = getattr(self, "compare_until", 0.0) - time.monotonic()
        if getattr(self, "settings_error", None):
            return {"state": "paused", "text": "settings.json doesn't read: changes not saved"}
        if self.preview is not None:
            return {"state": "on", "text": "Calibrating"}
        if not self.settings["enabled"]:
            return {"state": "off", "text": "Correction off"}
        if compare > 0:
            return {"state": "off", "text": f"Comparing: off for {compare:.0f} s"}
        if getattr(self, "unsupported", None):
            return {"state": "paused", "text": f"Not supported: {self.unsupported}"}
        if self.external:
            return {"state": "paused", "text": "Paused: external display"}
        if self.conflict:
            return {"state": "paused", "text": "Paused: another colour filter is on"}
        game = self._game()
        if game in (self.settings.get("app_off") or {}):
            return {"state": "off", "text": "Off for this game"}
        if not self.applied:
            if self.settings["profile"] == FACTORY and not self.settings.get("dither"):
                return {"state": "off", "text": "Factory profile: nothing to correct"}
            return {"state": "on", "text": "Clean range: nothing to correct"}
        parts = ["Correcting", "Steam's UI" if self._in_steam_ui() else "game"]
        if getattr(self, "soft_now", 1.0) < 1.0:
            parts[0] = f"floor dimming {self.settings.get('soft_target')} via {floor_of(self.settings)} (x{self.soft_now:.2f})"
        if self.app_on:
            parts.append("coarse (game's own effect)")
        elif self.applied[0][1] == "look":
            parts.append("coarse (menu open)" if getattr(self, "menu_look", False) else "coarse (colour table)")
        if self.dithering:
            parts.append(f"dither {self.settings.get('dither_temporal') or 'still'}")
        if getattr(self, "safe_mode", False):
            parts.append("safe mode")
        return {"state": "on", "text": " · ".join(parts)}

    def _mango_config(self):
        """Path of the config Steam writes for mangoapp, or None; looked up
        again every 10 s at most (mangoapp restarts with Steam)."""
        cached, when = getattr(self, "mango_path", None), getattr(self, "mango_when", -1e9)
        if (cached and os.path.exists(cached)) or time.monotonic() - when < 10:
            return cached
        self.mango_when = time.monotonic()
        self.mango_path = self._find_mango_config()
        return self.mango_path

    def _find_mango_config(self):
        for comm in glob.glob("/proc/[0-9]*/comm"):
            try:
                with open(comm) as f:
                    if f.read().strip() != "mangoapp":
                        continue
                with open(os.path.join(os.path.dirname(comm), "environ"), "rb") as f:
                    env = f.read().split(b"\0")
            except OSError:
                continue
            for kv in env:
                if kv.startswith(b"MANGOHUD_CONFIGFILE="):
                    return kv.split(b"=", 1)[1].decode()
        return None

    def _osd_lines(self):
        b = getattr(self, "backlight_raw", None) or self.backlight_now
        top = getattr(self, "backlight_max", None)
        if top is None and self.backlight:
            try:
                with open(os.path.join(self.backlight, "max_brightness")) as f:
                    top = self.backlight_max = int(f.read())
            except (OSError, ValueError):
                top = self.backlight_max = 0
        st = self._status()
        lo, hi = DITHER_BAND
        where = ("snap zone" if b and SNAP_ZONE[0] <= b <= SNAP_ZONE[1] else
                 "in band" if b and lo <= b <= hi else "")
        soft = getattr(self, "soft_now", 1.0)
        line1 = (f"{self.settings.get('soft_target')} via {floor_of(self.settings)} x{soft:.2f}"
                 if self.applied and soft < 1.0 else self._osd_strength()
                 if self.applied and st["state"] == "on" else "off")
        self.osd_color = ("band" if where else "dimming" if self.applied and soft < 1.0 else
                          "correcting" if self.applied and st["state"] == "on" else "off")
        line2 = f"{b or '?'}/{top or '?'}" + (f" {100 * b / top:.1f}%" if b and top else "") + (f" {where}" if where else "")
        eng = ("colour table" if self.applied and self.applied[0][1] == "look" else
               "shader" if self.applied else "no filter")
        line3 = eng
        if self.dithering:
            line3 += " dither"
        if self.vibrance_now == VIBRANCE_NATIVE and self.settings.get("vibrance_saved") is not None:
            line3 += " vibrance held"
        return [f"{label} {text}" for label, text in zip(OSD_LABELS, (line1, line2, line3))]

    def _osd_strength(self):
        """How much the colour correction does right now, in words: below
        0.5 % of light it is tiny, nothing you would see."""
        top = max(abs(t) for c in (self.current, self.current_l, self.current_h) for t in c)
        return f"tiny correction ({top:.1f}%)" if top < 0.5 else f"correcting (up to {top:.1f}%)"

    def _publish_state(self):
        """For the camera tool: which settings this loop has read and what is
        on screen, so a measurement waits for its change to be taken up
        rather than guessing from the log."""
        effect = (self.applied or (None, {}))[1].get(RESHADE_ATOM)
        if effect != getattr(self, "state_effect", None):
            self.state_effect, self.effect_since = effect, time.time()
        state = {"settings_mtime": self.settings_mtime, "revision": self.revision, "effect": effect,
                 "effect_since": getattr(self, "effect_since", None),
                 "pending": bool(getattr(self, "deferred", False)), "applied": bool(self.applied),
                 "backlight": getattr(self, "backlight_raw", None)}
        if state != getattr(self, "state_out", None):
            try:
                with open(STATE_FILE + ".tmp", "w") as f:
                    json.dump(state, f)
                os.chmod(STATE_FILE + ".tmp", 0o644)
                os.replace(STATE_FILE + ".tmp", STATE_FILE)
                self.state_out = state
            except OSError:
                pass

    def _osd_sync(self, force_off=False):
        on = bool(self.settings.get("osd")) and not force_off
        if on:
            text = "\n".join(self._osd_lines()) + "\n"
            if text != getattr(self, "osd_text", None):
                try:
                    with open(OSD_TEXT + ".tmp", "w") as f:
                        f.write(text)
                    os.chmod(OSD_TEXT + ".tmp", 0o644)
                    os.replace(OSD_TEXT + ".tmp", OSD_TEXT)
                    self.osd_text = text
                except OSError as e:
                    decky.logger.error(f"overlay text: {e}")
        path = self._mango_config()
        if not path:
            return
        try:
            with open(path) as f:
                old = f.read()
        except OSError:
            return
        new = osd_merge(old, on, getattr(self, "osd_color", None))
        if new != old:
            try:
                # In place: mangoapp watches this very file (and Steam owns it).
                with open(path, "r+") as f:
                    f.seek(0)
                    f.write(new)
                    f.truncate()
                decky.logger.info(f"info overlay {'added to' if on else 'removed from'} {path}")
            except OSError as e:
                decky.logger.error(f"overlay config: {e}")

    def _tables_info(self):
        """Steps and the oldest measurement date of the camera tables."""
        tables = self.settings.get("dither_tables") or {}
        dates = sorted(t["measured"] for t in tables.values() if isinstance(t.get("measured"), str))
        return {"steps": sorted(int(k) for k in tables), "measured": dates[0] if dates else None}

    async def set_enabled(self, enabled: bool):
        self.settings["enabled"] = bool(enabled)
        self._save_settings()
        return await self.get_state()

    async def set_simple(self, on: bool):
        self.settings["simple"] = bool(on)
        self._save_settings()
        return await self.get_state()

    async def set_dither(self, on: bool):
        self.settings["dither"] = bool(on)
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def set_dither_temporal(self, mode):
        """mode: False/None (off), True (pair), or one of TEMPORAL_MODES."""
        self.settings["dither_temporal"] = ("pair" if mode is True else mode if mode in TEMPORAL_MODES else False)
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def set_shadow_curve(self, curve: str):
        self.settings["shadow_curve"] = curve if curve in SHADOW_CURVES else "native"
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def set_menu_correction(self, on: bool):
        self.settings["menu_correction"] = bool(on)
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def set_osd(self, on: bool):
        self.settings["osd"] = bool(on)
        self._save_settings()
        self._osd_sync()
        return await self.get_state()

    async def get_snap(self):
        """The band-edge snap's switch and zone, read fresh by the frontend
        before every snap (the camera tool switches it off for its runs), and
        the dimming mode (floor dimming takes brightness changes as requests).
        active: floor dimming holds the backlight now; not while comparing (the
        frontend shows the panel at the target then, review 10-31 #2).
        soft_target: the backend's, for the frontend to take back up after a
        pause (a camera run sets it in the file)."""
        self._load_settings()
        return {"on": self.settings.get("band_snap", True), "zone": list(SNAP_ZONE),
                "dimming": self.settings.get("dimming", "soft"), "floor": floor_of(self.settings),
                "calibrating": self.preview is not None, "enabled": bool(self.settings["enabled"]),
                "active": bool(self.settings["enabled"]) and not getattr(self, "unsupported", None)
                and self._game() not in (self.settings.get("app_off") or {})
                and time.monotonic() >= getattr(self, "compare_until", 0.0),
                "soft_target": self.settings.get("soft_target")}

    async def set_dimming(self, mode: str, target: int = 0):
        """"band" (our correction) or "soft" (floor dimming); target: the
        brightness asked for (soft only)."""
        self.settings["dimming"] = "soft" if mode == "soft" else "band"
        if target:
            self.settings["soft_target"] = max(1, int(target))
        self._save_settings()
        self.fast_until = asyncio.get_event_loop().time() + 3
        self.wake.set()
        return await self.get_state()

    def _soft_dimmed(self):
        """The floor dimming's gain for the current target is on screen (the
        frontend raises the backlight to the floor only then: RESULTS 32)."""
        gain = getattr(self, "soft_now", 1.0)
        return bool(gain < 1.0 and self.applied and not getattr(self, "deferred", False)
                    and self.applied[0][-1] == ("L", gain_index(gain)))

    async def set_soft_target(self, target: int, handover: str = None):
        """handover (the frontend crossing the band's bottom edge, RESULTS 32):
        "up": dim for the floor now, before the frontend raises the backlight;
        "down": pass through now, before the frontend drops the backlight.
        Both act at once instead of in the next loop round; the frontend
        moves the backlight a lead time later (gamescope's switch is slower)."""
        self.settings["soft_target"] = max(1, int(target))
        self._save_settings()
        self.fast_until = asyncio.get_event_loop().time() + 3
        self.down_until = time.monotonic() + HANDOVER_WAIT if handover == "down" else 0.0
        if handover == "up":
            # Asked again every 100 ms until dimmed; a slider push meanwhile
            # keeps it (the backlight is still on its way up).
            self.up_until = time.monotonic() + HANDOVER_WAIT
        elif handover == "down":
            self.up_until = 0.0
        if handover in ("up", "down"):
            self._refresh()
        self.wake.set()
        return await self.get_state()

    async def set_band_snap(self, on: bool):
        self.settings["band_snap"] = bool(on)
        self._save_settings()
        return await self.get_state()

    async def set_dither_ui(self, on: bool):
        self.settings["dither_ui"] = bool(on)
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def clear_safe_mode(self):
        self.safe_mode = False
        STALLS.clear()
        self._refresh()
        return await self.get_state()

    async def slider_log(self, line: str):
        """Slider diagnostics: the frontend's SetBrightness calls, timestamped,
        into a file next to the backend's own technique switches."""
        _slider_log(str(line)[:200])
        return True

    async def set_overlay(self, open: bool):
        """The frontend: Steam's Quick Access or main menu opened/closed. The
        loop decides when to act on it (see MENU_*_HOLD)."""
        self.overlay_seen = time.monotonic()
        if bool(open) != self.overlay_open:
            self.overlay_open = bool(open)
            self.overlay_since = time.monotonic()
            self.fast_until = asyncio.get_event_loop().time() + MENU_CLOSE_HOLD + 1
            self.wake.set()
        return self.overlay_open

    def _in_steam_ui(self):
        """Steam's own UI is in front, not a game (a menu opened over a game
        hands gamescope's focus to Steam's UI; that doesn't count)."""
        gfx = getattr(self, "focused_gfx", None)
        if gfx:
            # Says it directly. (The timing guess below failed with the
            # sidebar open a while: its dropdowns close and reopen the menu
            # window, which made a game behind it look like Steam's UI.)
            return str(gfx) == STEAM_UI_APP
        if str(self.focused) != STEAM_UI_APP:
            return False
        if getattr(self, "overlay_open", False) or getattr(self, "menu_look", False):
            # Steam's UI got the focus after the menu opened: the menu's doing.
            return getattr(self, "focus_since", 0.0) < getattr(self, "overlay_since", 0.0) - 0.5
        return True

    def _menu_engine(self):
        """Whether the look should cover the menus now: the menu state, held
        long enough, and not switching more often than ENGINE_SWITCH_GAP."""
        if not self.settings.get("menu_correction", True) or getattr(self, "safe_mode", False):
            self.menu_look = False      # else a stale one holds the vibrance (review 10-31 #3)
            return False
        if self.settings.get("dimming") == "soft":
            # floor dimming: the dimming is the ReShade ladder, which follows the
            # game behind a menu. The look isn't applied there (tested
            # 2026-10-26: a 30% table over a game with the sidebar open
            # changed nothing), so switching to it froze the dimming.
            self.menu_look = False
            return False
        want, since = getattr(self, "overlay_open", False), getattr(self, "overlay_since", 0.0)
        if want and time.monotonic() - getattr(self, "overlay_seen", since) > MENU_STALE:
            want = False        # the frontend stopped confirming: a missed close
        # In Steam's own UI the menus are part of the focused app: ReShade, with
        # the dither, already covers them, and the look (no dither, a coarse
        # grid) made them greener. Only a game leaves them uncovered. Opening
        # the menu over a game moves gamescope's focus to Steam's UI too, so
        # that only counts if Steam's UI had the focus before the menu opened.
        if self._in_steam_ui():
            want = False
        held = time.monotonic() - since
        current = getattr(self, "menu_look", False)
        if want != current and held >= (MENU_OPEN_HOLD if want else MENU_CLOSE_HOLD) \
                and time.monotonic() - getattr(self, "engine_switched", 0.0) >= ENGINE_SWITCH_GAP:
            self.menu_look = want
            self.engine_switched = time.monotonic()
        return getattr(self, "menu_look", False)

    def _downloads(self):
        return os.path.join(decky.DECKY_USER_HOME, "Downloads")

    async def export_calibration(self):
        """Write the active profile and the panel tables to ~/Downloads; returns
        the path (to send to another Portal)."""
        name = self.settings["profile"]
        data = export_calibration(name, copy.deepcopy(self.settings["steps"]),
                                  self.settings.get("dither_tables") or {}, self.settings)
        os.makedirs(self._downloads(), exist_ok=True)
        path = os.path.join(self._downloads(), EXPORT_NAME)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
        decky.logger.info(f"calibration '{name}' exported to {path}")
        return path

    async def import_calibration(self, source: str = ""):
        """Import from a URL (http/https) or a file path; empty: the newest
        oled-tint-calibration*.json in ~/Downloads. The profile becomes active
        (named after the file's, with "(imported)"); the panel tables replace
        the current ones, which are kept in the settings as a backup."""
        try:
            if source.startswith(("http://", "https://")):
                import urllib.request
                with urllib.request.urlopen(source, timeout=15) as r:
                    data = json.loads(r.read(4 * 1024 * 1024).decode())
            else:
                path = source or max(glob.glob(os.path.join(self._downloads(), "oled-tint-calibration*.json")),
                                     key=os.path.getmtime, default="")
                if not path:
                    raise ValueError(f"no {EXPORT_NAME} in Downloads")
                with open(path) as f:
                    data = json.load(f)
            name, steps, tables, extras = parse_calibration(data)
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:      # any other malformed file: say so, don't crash
            decky.logger.error(f"import failed: {e!r}")
            return {"ok": False, "error": f"not a usable calibration file ({type(e).__name__})"}
        # A fresh name each time: re-importing must not overwrite edits made
        # to an earlier import.
        profile = self._free_name(f"{name} (imported)")
        self.settings["profiles"][profile] = steps
        self.settings["profile"] = profile
        self.settings["steps"] = copy.deepcopy(steps)
        if tables:
            if self.settings.get("dither_tables"):
                self.settings["dither_tables_backup"] = self.settings["dither_tables"]
            self.settings["dither_tables"] = tables
        self.settings.update({k: v for k, v in extras.items()
                              if k != "shadow_curve" or v in SHADOW_CURVES})
        self._save_settings()
        self._refresh()
        decky.logger.info(f"calibration '{name}' imported as '{profile}' ({len(tables)} tables)")
        return {"ok": True, "profile": profile, "tables": len(tables)}

    async def set_crush(self, value: int):
        self.settings["crush"] = max(0, min(MAX_CRUSH, int(value)))
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def set_engine(self, engine: str):
        self.settings["engine"] = "look" if engine == "look" else "reshade"
        self._save_settings()
        self._refresh()
        return await self.get_state()

    async def set_game_off(self, on: bool, name: str = ""):
        """Correction off (or back on) for the game in front."""
        game = self._game()
        if game:
            off = self.settings.setdefault("app_off", {})
            if on:
                off[game] = str(name or game)
            else:
                off.pop(game, None)
            self._save_settings()
            self._refresh()
        return await self.get_state()

    async def compare(self, seconds: float = 5):
        """The panel uncorrected for a few seconds, then back."""
        self.compare_until = time.monotonic() + max(1.0, min(15.0, float(seconds)))
        self.fast_until = asyncio.get_event_loop().time() + float(seconds) + 2
        self._refresh()
        self.wake.set()
        return await self.get_state()

    def _free_name(self, base):
        name, n = base, 1
        while name == FACTORY or name in self.settings["profiles"]:
            n += 1
            name = f"{base} {n}"
        return name

    def _switch_profile(self, name, steps):
        self.settings["profiles"][name] = copy.deepcopy(steps)
        self.settings["profile"] = name
        self.settings["steps"] = copy.deepcopy(steps)
        self.preview = None
        self._save_settings()
        self._refresh()

    async def profile_new(self):
        """A new, empty profile (no correction yet), made active."""
        self._switch_profile(self._free_name("Custom"), _factory())
        return await self.get_state()

    async def profile_duplicate(self):
        self._switch_profile(self._free_name(f"{self.settings['profile']} copy"), self.settings["steps"])
        return await self.get_state()

    async def profile_rename(self, new: str):
        old, new = self.settings["profile"], " ".join(str(new).split())[:40]
        if old == FACTORY or not new or new == old:
            return await self.get_state()
        if new == FACTORY or new in self.settings["profiles"]:
            new = self._free_name(new)
        self.settings["profiles"][new] = self.settings["profiles"].pop(old)
        self.settings["profile"] = new
        self._save_settings()
        return await self.get_state()

    async def profile_delete(self):
        """Delete the active profile (not Factory); Factory becomes active."""
        name = self.settings["profile"]
        if name != FACTORY:
            self.settings["profiles"].pop(name, None)
            self.settings["profile"] = FACTORY
            self.settings["steps"] = _factory()
            self.preview = None
            self._save_settings()
            self._refresh()
        return await self.get_state()

    async def set_profile(self, name: str):
        if name == FACTORY:
            self.settings["steps"] = _factory()
        elif name in self.settings["profiles"]:
            self.settings["steps"] = copy.deepcopy(self.settings["profiles"][name])
        else:
            return await self.get_state()
        self.settings["profile"] = name
        self._save_settings()
        if self.preview is not None:
            self.preview = None  # the calibration page reloads with the new steps
        self._refresh()
        return await self.get_state()

    async def set_tints(self, changes: list, shown: int, profile: str = ""):
        """changes: [[step, level, value or None, kind], ...] with kind "v"
        (tint), "l" (brightness) or "h" (red/blue); shown: the wizard's
        current step, whose values become the live preview; profile: the one
        the edits were made in. Returns {"accepted", "profile", "created"}:
        rejected edits (another profile active) say so, so the page reloads
        instead of relabelling its queue (review 17)."""
        if profile and profile != self.settings["profile"]:
            # Made in a profile that's no longer active (switched meanwhile):
            # applying them here would overwrite another calibration.
            decky.logger.info(f"{len(changes)} edit(s) for profile {profile!r} dropped, {self.settings['profile']!r} is active")
            return {"accepted": False, "profile": self.settings["profile"], "created": False}
        created = self.settings["profile"] == FACTORY
        if self.settings["profile"] == FACTORY:
            # Factory stays untouched: editing it starts a profile of its own,
            # under a name not taken yet (an existing Custom isn't overwritten).
            self.settings["profile"] = self._free_name("Custom")
        for step, level, value, kind in changes:
            limit = LIMITS[kind]
            # None puts the level back on auto.
            self.settings["steps"][step][kind][level] = (
                None if value is None else max(-limit, min(limit, float(value))))
        self._save_settings()
        if self.preview is not None:
            self.preview = self._preview_of(shown)
            self._refresh()
        return {"accepted": True, "profile": self.settings["profile"], "created": created}

    async def wizard_ping(self):
        self.last_ping = asyncio.get_event_loop().time()

    async def wizard_begin(self):
        await self.wizard_ping()
        return await self.get_state()

    async def brightness_changed(self):
        """Called by the frontend on Steam's brightness events."""
        self.fast_until = asyncio.get_event_loop().time() + FAST_WINDOW
        self.wake.set()

    async def get_brightness(self):
        return self._read_brightness()

    # The frontend moves the backlight through Steam's own API: Decky strips
    # our groups and polkit refuses us outside the user's session, so the
    # backend can't write it.
    def _preview_of(self, index):
        p = self.settings["steps"][index]
        return {"b": p["b"], **{k: list(p[k]) for k in KINDS}}

    async def wizard_step(self, index: int, corrected: bool):
        await self.wizard_ping()
        self.preview = self._preview_of(index) if corrected else {"v": None}
        self._refresh()

    async def wizard_end(self):
        self.preview = None


if __name__ == "__main__" and "--helper" in sys.argv:
    _helper_main()
