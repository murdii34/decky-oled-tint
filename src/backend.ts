import { callable } from "@decky/api";

export interface Step {
  b: number;
  v: (number | null)[]; // one tint per grey level; null = auto
  l: (number | null)[]; // one brightness change (percent) per grey level
  h: (number | null)[]; // one red/blue balance per grey level
}

export type Kind = "v" | "l" | "h";

export interface State {
  enabled: boolean;
  steps: Step[];
  levels: number[]; // grey levels in percent, matching Step.v
  brightness: number | null;
  current: number[];
  conflict: boolean;
  external: boolean;
  max_tint: number;
  max_lift: number;
  current_l: number[];
  max_hue: number;
  current_h: number[];
  engine: "reshade" | "look";
  profile: string;
  crush: number; // 8-bit grey level up to which everything goes to black; 0 = off
  dither: boolean;
  dither_temporal: false | "pair" | "golden"; // the dither's pattern changes every frame
  shadow_curve: "g22" | "native"; // the dither's target: gamma 2.2, or the panel's own curve
  menu_correction: boolean; // correct Steam's menus (via the look) while one is open
  band_snap: boolean; // step the backlight over the band's unstable bottom edge
  osd: boolean; // info lines in Steam's performance overlay
  dimming: "band" | "soft"; // our band correction, or AYN-style (backlight held at soft_floor, picture dimmed)
  soft_target: number | null; // AYN-style: the brightness asked for (raw backlight)
  soft_floor: number;
  soft_gain: number; // AYN-style: the picture's light scale now (1 = not dimming)
  snap_zone: [number, number]; // that edge, raw backlight, inclusive
  safe_mode: boolean; // gamescope stalled repeatedly: dither and menu switching paused
  dither_ui: boolean; // dither in Steam's own UI too (still there, so it can show as grain)
  simple: boolean; // Lite package: just the on/off switch
  dither_ready: boolean; // camera tables exist for at least one step
  max_crush: number;
  profiles: string[]; // "Factory" first: no correction, never edited
  status: { state: "on" | "off" | "paused"; text: string }; // what the correction does now, or why not
  game: string | null; // app id of the game in front (also behind a menu); null in Steam's UI
  game_off: boolean; // correction switched off for that game
  tables: { steps: number[]; measured: string | null }; // camera tables: steps, oldest measurement date
}

export const getState = callable<[], State>("get_state");
export const setEnabled = callable<[enabled: boolean], State>("set_enabled");
// profile: the one the edits were made in (the backend drops them if another
// is active now). accepted false: dropped, the page must reload; created:
// editing Factory started this profile.
export const setTints = callable<
  [changes: [number, number, number | null, Kind][], shown: number, profile: string],
  { accepted: boolean; profile: string; created: boolean }
>(
  "set_tints",
);
export const wizardBegin = callable<[], State>("wizard_begin");
export const wizardStep = callable<[index: number, corrected: boolean], void>("wizard_step");
export const wizardEnd = callable<[], void>("wizard_end");
export const wizardPing = callable<[], void>("wizard_ping");
export const brightnessChanged = callable<[], void>("brightness_changed");
export const getBrightness = callable<[], number | null>("get_brightness");

let brightnessSeq = 0;

// Steam slider fraction -> raw backlight, measured on this device. Readbacks
// are merged in, so later jumps land in one step.
const curve: [number, number][] = [
  [0, 40], [0.01, 43], [0.05, 57], [0.1, 80], [0.15, 113], [0.2, 159],
  [0.25, 223], [0.3, 313], [0.35, 440], [0.4, 617], [0.45, 867],
  [0.5, 1217], [0.75, 2332], [1, 4096],
];

function learn(f: number, raw: number) {
  // A readback that breaks the curve's ordering is stale (it belongs to an
  // earlier request); learning it would send every later guess astray.
  const below = curve.filter(([cf]) => cf < f).pop();
  const above = curve.find(([cf]) => cf > f);
  if ((below && raw < below[1]) || (above && raw > above[1])) return;
  const i = curve.findIndex(([cf]) => cf >= f);
  if (i >= 0 && curve[i][0] === f) curve[i][1] = raw;
  else curve.splice(i < 0 ? curve.length : i, 0, [f, raw]);
}

// Interpolate in log(raw), where the curve is close to linear.
export function guess(target: number): number {
  const t = Math.log(Math.max(target, 1));
  for (let i = 1; i < curve.length; i++) {
    const [f0, r0] = curve[i - 1];
    const [f1, r1] = curve[i];
    if (target <= r1 || i === curve.length - 1) {
      const l0 = Math.log(r0);
      const l1 = Math.log(r1);
      const k = l1 === l0 ? 0 : (t - l0) / (l1 - l0);
      return Math.min(1, Math.max(0, f0 + k * (f1 - f0)));
    }
  }
  return 0.5;
}

// Steam's slider fraction maps to the raw backlight through a curve we only
// know by measurement, so jump to the predicted spot and nudge if it's off.
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// A newer brightness choice (the plugin's slider, the demo, Steam's slider
// through the intercept) cancels a preset's delayed adjustment, which used to
// put the older value back (review 17/18).
export function cancelBrightness() {
  brightnessSeq++;
}

// Steam's own SetBrightness, kept before the AYN-style intercept wraps it:
// the plugin's internal writes (the floor, the demo) go straight to it.
let realSet: (f: number) => void = (f) => SteamClient.System.Display.SetBrightness(f);
export function setRealSetter(f: (f: number) => void) {
  realSet = f;
}
export function setDirect(f: number) {
  realSet(f);
}

// Steam slider fraction for a raw backlight, and back (the measured curve).
export function rawFor(f: number): number {
  for (let i = 1; i < curve.length; i++) {
    const [f0, r0] = curve[i - 1];
    const [f1, r1] = curve[i];
    if (f <= f1 || i === curve.length - 1) {
      const k = f1 === f0 ? 0 : (f - f0) / (f1 - f0);
      return Math.round(Math.exp(Math.log(r0) + k * (Math.log(r1) - Math.log(r0))));
    }
  }
  return 4096;
}

// direct: the plugin's own write (bypasses the AYN-style intercept).
export async function setRawBrightness(target: number, direct = false): Promise<void> {
  const set = direct ? realSet : (f: number) => SteamClient.System.Display.SetBrightness(f);
  const seq = ++brightnessSeq;
  const stale = () => seq !== brightnessSeq;
  const close = (raw: number) => Math.abs(raw - target) <= Math.max(1, target * 0.01);
  for (let i = 0; i < 4; i++) {
    const f = guess(target);
    set(f);
    // Give Steam a moment so the readback reflects this request, not the last.
    await sleep(60);
    if (stale()) return;
    const raw = await getBrightness();
    if (raw === null || stale()) return;
    learn(f, raw);
    if (close(raw)) break;
  }
  // When requests come in fast, check once more after things settle.
  await sleep(400);
  if (stale()) return;
  const raw = await getBrightness();
  if (raw !== null && !stale() && !close(raw)) {
    set(guess(target));
  }
}

// Negative = toward magenta (red/blue lifted), positive = toward green.
export function formatTint(v: number): string {
  if (Math.abs(v) < 0.05) return "neutral";
  return `${Math.abs(v).toFixed(0)} ${v < 0 ? "magenta" : "green"}`;
}

// Grey level as an 8-bit label: levels are sixty-fourths, shown as clean
// multiples of 16 (6.25% -> "16", 62.5% -> "160"), white as 255. The exact
// code values (159.4 and so on) differ by under one step.
export function to8bit(p: number): number {
  return Math.min(255, Math.round((p / 100) * 256));
}

export const setEngine = callable<[engine: "reshade" | "look"], State>("set_engine");
export const setProfile = callable<[name: string], State>("set_profile");
export const setCrush = callable<[value: number], State>("set_crush");
export const setDither = callable<[on: boolean], State>("set_dither");
export const setOverlay = callable<[open: boolean], boolean>("set_overlay");
export const setMenuCorrection = callable<[on: boolean], State>("set_menu_correction");
export const exportCalibration = callable<[], string>("export_calibration");
export const clearSafeMode = callable<[], State>("clear_safe_mode");
export const setDitherUi = callable<[on: boolean], State>("set_dither_ui");
export const importCalibration = callable<[source: string], { ok: boolean; error?: string; profile?: string; tables?: number }>(
  "import_calibration",
);
export const setShadowCurve = callable<[curve: "g22" | "native"], State>("set_shadow_curve");
export const setDitherTemporal = callable<[mode: boolean | "pair" | "golden"], State>("set_dither_temporal");
export const setSimple = callable<[on: boolean], State>("set_simple");
export const setOsd = callable<[on: boolean], State>("set_osd");
export const getSnap = callable<
  [],
  { on: boolean; zone: [number, number]; dimming: "band" | "soft"; floor: number; calibrating: boolean; enabled: boolean }
>(
  "get_snap",
);
// When the plugin itself last moved the backlight (AYN-style floor writes,
// Calibrate's restore): a readback near then is ours, not a user request.
export const ownWrite = { at: 0 };

export const setDimming = callable<[mode: "band" | "soft", target: number], State>("set_dimming");
export const sliderLog = callable<[line: string], boolean>("slider_log");
export const setSoftTarget = callable<[target: number], State>("set_soft_target");
export const setBandSnap = callable<[on: boolean], State>("set_band_snap");
export const setGameOff = callable<[on: boolean, name: string], State>("set_game_off");
export const compare = callable<[seconds: number], State>("compare");
export const profileNew = callable<[], State>("profile_new");
export const profileDuplicate = callable<[], State>("profile_duplicate");
export const profileRename = callable<[name: string], State>("profile_rename");
export const profileDelete = callable<[], State>("profile_delete");

// Positive = red/blue balance leans red, negative = leans blue.
export function formatHue(v: number): string {
  if (Math.abs(v) < 0.05) return "red/blue ±0";
  return `${Math.abs(v).toFixed(0)} ${v > 0 ? "red" : "blue"}`;
}

export function formatLift(v: number): string {
  if (Math.abs(v) < 0.05) return "brightness ±0";
  return `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(0)}% brightness`;
}

export function formatLevel(p: number): string {
  return `${to8bit(p)}`;
}

// Mirror of the backend's resolve(): auto levels follow their set
// neighbours (linear in level), hold below the first, neutral above the last.
export function resolve(v: (number | null)[], levels: number[]): number[] {
  const known = v.flatMap((t, i) => (t === null ? [] : [[i, t] as const]));
  if (!known.length) return v.map(() => 0);
  return v.map((t, i) => {
    if (t !== null) return t;
    const lo = known.filter(([k]) => k < i).pop();
    const hi = known.find(([k]) => k > i);
    if (!lo) return hi![1];
    if (!hi) return 0;
    const f = (levels[i] - levels[lo[0]]) / (levels[hi[0]] - levels[lo[0]]);
    return lo[1] + f * (hi[1] - lo[1]);
  });
}
