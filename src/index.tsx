import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  DropdownItem,
  Focusable,
  SliderField,
  Navigation,
  PanelSection,
  PanelSectionRow,
  Router,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { definePlugin, routerHook } from "@decky/api";
import { ReactNode, useEffect, useState } from "react";
import { FaPalette } from "react-icons/fa";

import { CALIBRATE_ROUTE, Calibrate } from "./Calibrate";
import { PATTERN_ROUTE, PRESETS, TestPattern } from "./TestPattern";
import {
  State,
  brightnessChanged,
  formatLevel,
  formatTint,
  getState,
  guess,
  setRawBrightness,
  setEnabled,
  setCrush,
  setDither,
  setDitherTemporal,
  setOverlay,
  setMenuCorrection,
  exportCalibration,
  clearSafeMode,
  setDitherUi,
  importCalibration,
  setShadowCurve,
  setEngine,
  setProfile,
  wizardEnd,
  compare,
  setGameOff,
  profileNew,
  profileDuplicate,
  profileRename,
  profileDelete,
  getBrightness,
  getSnap,
  setBandSnap,
  setOsd,
  setDimming,
  setSoftTarget,
  sliderLog,
  ownWrite,
  cancelBrightness,
  setRealSetter,
  setDirect,
  rawFor,
} from "./backend";

// The strongest correction currently applied, e.g. "30 magenta at grey 16".
function describe(s: State): string {
  let i = 0;
  s.current.forEach((t, j) => {
    if (Math.abs(t) > Math.abs(s.current[i])) i = j;
  });
  const t = s.current[i] ?? 0;
  return Math.abs(t) < 0.05 ? "neutral" : `${formatTint(t)} at grey ${formatLevel(s.levels[i])}`;
}

// The panel's own brightness control: Steam's slider (by fraction) and the
// calibrated steps as buttons. Advanced has a random level inside the bad
// band (log-uniform), for testing between and around the steps.
const BAND: [number, number] = [66, 540];

// Demo, two ways. Sweep: the backlight glides up and down across the range,
// slowly enough that the correction (one effect per ~8% of brightness, at
// most one per second) keeps up. Tour: at a few of the band's worst steps in
// turn, corrected for a while, then uncorrected (Compare) for as long. Both
// run in the background until switched off, or until their time or cycle
// limit. Settings are per device (localStorage); speed and hold can change
// while it runs (the phase carries on from where it is).
const DEMO = { tick: 100 };
// Sweep ranges: around the band, or the panel's whole range (raw backlight).
const DEMO_RANGES = { band: [50, 1000], full: [40, 4096] } as const;
// The speed, as seconds for one sweep up and down around the band (50-1000);
// the same speed over the full range just takes longer (it is a speed in
// log brightness, not a duration). 15 s outruns the correction on purpose
// (one new effect per ~8% of brightness, at most one a second): to see it lag.
const DEMO_PERIODS = [15, 30, 60, 90, 150, 300, 600];
const sweepSeconds = (cfg: { period: number; range: "band" | "full" }) => {
  const [lo, hi] = DEMO_RANGES[cfg.range] ?? DEMO_RANGES.band;
  const [blo, bhi] = DEMO_RANGES.band;
  return (cfg.period * Math.log(hi / lo)) / Math.log(bhi / blo);
};
const TOUR = [96, 177, 230, 310]; // the band's worst, where the difference shows most
const TOUR_SETTLE = 2; // seconds at a new brightness before it counts as corrected
type DemoCfg = {
  mode: "sweep" | "tour";
  range: "band" | "full";
  period: number;
  hold: number; // tour: seconds corrected, then as many uncorrected
  stopOn: boolean;
  stopMin: number;
  cyclesOn: boolean;
  cycles: number;
};
const DEMO_DEFAULT: DemoCfg = {
  mode: "sweep",
  range: "band",
  period: 150,
  hold: 4,
  stopOn: true,
  stopMin: 10,
  cyclesOn: false,
  cycles: 3,
};

function loadDemoCfg(): DemoCfg {
  try {
    return { ...DEMO_DEFAULT, ...JSON.parse(localStorage.getItem("oled-tint-demo") || "{}") };
  } catch {
    return { ...DEMO_DEFAULT };
  }
}

const demo = { cfg: loadDemoCfg(), phase: 0, started: 0, down: false, step: -1, compared: false, label: "" };
let demoTimer: ReturnType<typeof setInterval> | null = null;
const demoListeners = new Set<(on: boolean) => void>();

function setDemoCfg(change: Partial<DemoCfg>) {
  demo.cfg = { ...demo.cfg, ...change };
  try {
    localStorage.setItem("oled-tint-demo", JSON.stringify(demo.cfg));
  } catch {
    /* not remembered, fine */
  }
  demoListeners.forEach((f) => f(demoTimer !== null));
}

// In floor dimming mode the demo shows floor dimming: inside the band it asks for
// the brightness (the backlight stays at the floor and the picture dims),
// outside it sets the backlight itself. Requests only when the brightness
// moved a bin (~8%), the correction's own rhythm.
const demoSoft = { on: false, floor: 543, last: 0, atFloor: false, token: 0 };

async function demoDimming(on: boolean) {
  const token = ++demoSoft.token; // a stop before the answer comes voids it
  demoSoft.on = false;
  if (!on) return;
  const c = await getSnap().catch(() => null);
  if (token !== demoSoft.token || !demoTimer) return;
  if (c && c.dimming === "soft" && c.enabled) Object.assign(demoSoft, { on: true, floor: c.floor, last: 0, atFloor: false });
}

function demoBrightness(b: number, set: (b: number) => void) {
  cancelBrightness();
  if (!demoSoft.on) return set(b);
  if (b >= SOFT_FROM && b < demoSoft.floor) {
    if (demoSoft.last && Math.abs(Math.log(b / demoSoft.last)) < Math.log(1.08)) return;
    demoSoft.last = b;
    setSoftTarget(Math.round(b)).catch(() => {});
    if (!demoSoft.atFloor) {
      demoSoft.atFloor = true;
      ownWrite.at = Date.now();
      set(demoSoft.floor);
    }
  } else {
    demoSoft.atFloor = false;
    demoSoft.last = 0;
    setSoftTarget(Math.round(b)).catch(() => {});
    ownWrite.at = Date.now();
    set(b);
  }
}

function setDemo(on: boolean) {
  if (demoTimer) {
    clearInterval(demoTimer);
    demoTimer = null;
    demoDimming(false);
  }
  if (on) {
    demoDimming(true);
    demo.phase = 0;
    demo.started = Date.now();
    demo.step = -1;
    demoTimer = setInterval(() => {
      const { mode, hold, stopOn, stopMin, cyclesOn, cycles } = demo.cfg;
      if (stopOn && Date.now() - demo.started > stopMin * 60000) return setDemo(false);
      if (cyclesOn && demo.phase >= cycles) return setDemo(false);
      if (mode === "tour") return tourTick(hold);
      demo.phase += DEMO.tick / 1000 / sweepSeconds(demo.cfg);
      // Triangle wave in log brightness, eased at the turns.
      const p = demo.phase % 1;
      demo.down = p >= 0.5;
      const tri = p < 0.5 ? p * 2 : 2 - p * 2;
      const eased = 0.5 - 0.5 * Math.cos(Math.PI * tri);
      const [lo, hi] = DEMO_RANGES[demo.cfg.range] ?? DEMO_RANGES.band;
      let b = lo * Math.pow(hi / lo, eased);
      // Over the band's unstable edge, like the snap (which rests meanwhile).
      const [zlo, zhi] = snap.zone;
      if (snap.on && b >= zlo && b <= zhi + 0.5) b = demo.down ? zlo - 1 : zhi + 1;
      demoBrightness(b, (v) => setDirect(guess(v)));
    }, DEMO.tick);
  }
  demoListeners.forEach((f) => f(on));
}

// One tour step: TOUR_SETTLE s to reach the brightness and apply, `hold` s
// corrected, `hold` s uncorrected; one cycle is all of TOUR.
function tourTick(hold: number) {
  const stepLen = TOUR_SETTLE + 2 * hold;
  demo.phase += DEMO.tick / 1000 / (stepLen * TOUR.length);
  const pos = (demo.phase % 1) * TOUR.length;
  const i = Math.floor(pos);
  const t = (pos - i) * stepLen;
  const b = TOUR[i];
  if (i !== demo.step) {
    demo.step = i;
    demo.compared = false;
    demoBrightness(b, (v) => setRawBrightness(v, true));
  }
  if (!demo.compared && t >= TOUR_SETTLE + hold) {
    demo.compared = true;
    compare(hold).catch(() => {});
  }
  const phase = t < TOUR_SETTLE ? "settling" : t < TOUR_SETTLE + hold ? "corrected" : "uncorrected";
  const left = t < TOUR_SETTLE + hold ? TOUR_SETTLE + hold - t : stepLen - t;
  demo.label = `${b} · ${phase} · ${Math.ceil(left)} s`;
}

// Band-edge snap: at the band's bottom edge the panel picks its mode by the
// way it got there, so a backlight in SNAP's zone is stepped out of it, in
// the direction it was moving. Checked once the slider has rested a moment,
// so it doesn't fight a drag; not during the demo sweep.
const snap = {
  on: true,
  zone: [57, 69] as [number, number],
  last: null as number | null, // the last backlight seen outside the zone
  down: false,
  tries: 0,
  timer: 0 as any,
  busy: false,
};

function snapLater() {
  clearTimeout(snap.timer);
  snap.timer = setTimeout(async () => {
    if (demoTimer || snap.busy) return;
    // Fresh each time: a camera run switches it off in the settings.
    let soft: { floor: number } | null = null;
    try {
      const c = await getSnap();
      snap.on = c.on;
      snap.zone = c.zone;
      // Calibrating: the wizard sets its steps' own backlights; neither the
      // snap nor floor dimming may move them.
      if (c.calibrating) return;
      syncAyn(c);
      if (ayn.on) soft = { floor: c.floor };
    } catch {
      /* keep the last known */
    }
    if (soft) return softRequest(soft.floor);
    if (!snap.on) return;
    const raw = await getBrightness();
    if (raw === null) return;
    const [lo, hi] = snap.zone;
    if (raw >= lo && raw <= hi) {
      // Still inside after a snap (Steam's slider didn't land where asked):
      // the same way again, further out; never back and forth.
      if (snap.tries === 0) snap.down = snap.last !== null && snap.last > raw;
      if (snap.tries >= 3) return;
      const margin = 1 + 2 * snap.tries++;
      snap.busy = true;
      try {
        await setRawBrightness(snap.down ? lo - margin : hi + margin, true);
      } finally {
        snap.busy = false;
      }
      snapLater();
    } else {
      snap.last = raw;
      snap.tries = 0;
    }
  }, 400);
}

// floor dimming dimming: a brightness change (Steam's slider, the presets) is
// what's asked for; below the floor the backlight goes back to the floor and
// the backend dims the picture instead.
// When we last put the backlight back to the floor (ownWrite, shared with
// Calibrate): a readback near the floor right after is our own write; any
// other time it's a request (543 asked for while the target was 177 used to
// be ignored).

// The band floor dimming covers (as the backend's soft_wanted): below it the
// panel is clean at its own backlight.
const SOFT_FROM = 57;

async function softRequest(floor: number) {
  const raw = await getBrightness();
  if (raw === null) return;
  // At the floor: nothing to do. Steam's slider requests come through the
  // intercept; reading the floor back as a request reset the target (and the
  // slider) to 543 after a drop inside the band.
  if (raw >= floor * 0.97) return;
  await setSoftTarget(raw);
  if (raw >= SOFT_FROM && raw < floor) {
    snap.busy = true;
    try {
      await setRawBrightness(floor, true);
      ownWrite.at = Date.now();
    } finally {
      snap.busy = false;
    }
  }
}

// floor dimming intercept: Steam's SetBrightness wrapped, so a request inside
// the band never reaches the panel (no flash through the band's bad mode);
// it becomes the target and the backlight stays at the floor. Whether
// Steam's slider then stays where it was put is what this tries out.
const ayn = { holding: false, on: false, floor: 543, target: 0, pending: 0 as any, lastOut: 0, lastF: 0, floorAt: 0, checking: false };

// The backlight must sit at the floor for the dimming to be right (the
// ladder only reaches gain ~0.05, the backend dims from "above the band").
// Read, not inferred from earlier requests: after a request below the band,
// at start, after a pause, from a high backlight. Then Steam's slider store
// is set again, as the floor write's own event would pull it to the floor.
async function ensureFloor() {
  if (ayn.checking || Date.now() - ayn.floorAt < 1000) return;
  ayn.checking = true;
  try {
    const raw = await getBrightness();
    if (raw === null || !ayn.on) return;
    if (raw < ayn.floor * 0.97 || raw > ayn.floor * 1.3) {
      ayn.floorAt = Date.now();
      ownWrite.at = Date.now();
      setDirect(guess(ayn.floor));
      const f = ayn.lastF;
      setTimeout(() => ayn.on && showOnSlider(f), 120);
      setTimeout(() => ayn.on && showOnSlider(f), 400);
    }
  } finally {
    ayn.checking = false;
  }
}

// Whether floor dimming is in force now (the backend says: correction on, not
// off for this game, not calibrating). Leaving it gives the asked-for
// backlight back, else the screen would stay at the floor undimmed.
function syncAyn(c: { dimming: string; floor: number; enabled: boolean; active?: boolean; calibrating: boolean }) {
  const was = ayn.on;
  ayn.floor = c.floor;
  ayn.on = c.dimming === "soft" && c.enabled && c.active !== false;
  if (was && !ayn.on && !c.calibrating && !demoTimer) releaseAyn();
}

function releaseAyn() {
  clearTimeout(ayn.pending);
  clearTimeout(softSend.timer);
  if (ayn.target >= SOFT_FROM && ayn.target < ayn.floor) {
    ownWrite.at = Date.now();
    setRawBrightness(ayn.target, true).then(() => (ownWrite.at = Date.now()));
  }
}

// Slider drags: at most one target to the backend every 100 ms, the last
// one always sent (a trailing push).
const softSend = { at: 0, timer: 0 as any, last: 0 };
function softPush(raw: number) {
  softSend.last = raw;
  const wait = softSend.at + 50 - Date.now();
  clearTimeout(softSend.timer);
  const go = () => {
    softSend.at = Date.now();
    setSoftTarget(softSend.last).catch(() => {});
  };
  if (wait <= 0) go();
  else softSend.timer = setTimeout(go, wait);
}

// Steam's slider shows the last brightness *event*; a swallowed request sends
// none, so the slider fell back to the floor's position on release. Steam's
// own store behind it (SystemStoragStore, sic) is set to the asked-for value.
function showOnSlider(f: number) {
  try {
    (window as any).SystemStoragStore?.DisplayBrightness?.Set(f);
  } catch {
    /* Steam changed its store: the slider just falls back as before */
  }
}

// Crossing the band's bottom edge (slider 4% <-> 5%): backlight and dimming
// change together (RESULTS 32: the backlight ran ahead of the dimming, 543
// undimmed for up to 1 s going up, a blackout going down). Filmed
// (handover_sweep.py, 2026-10-30): the technique switch shows fast, the
// backlight a little later, and the two jitter by 1-2 frames, so they can't be
// made to meet exactly. Ordered so the leftover is always a short dip, never
// a flash: up, the dimming goes first and the backlight rises HANDOVER_LEAD ms
// after; down, the backlight drops first and the dimming lets go
// HANDOVER_LAG ms after.
const HANDOVER_LEAD = 80;
const HANDOVER_LAG = 60;
// Camera runs can try other values without a build.
const handoverLead = (): number => (window as any).OLED_HANDOVER_LEAD ?? HANDOVER_LEAD;
const handoverLag = (): number => (window as any).OLED_HANDOVER_LAG ?? HANDOVER_LAG;
function crossUp(f: number) {
  clearTimeout(softSend.timer);
  ayn.floorAt = Date.now(); // ensureFloor stays out of it
  ownWrite.at = Date.now();
  const rise = () => {
    if (!ayn.on) return;
    ownWrite.at = Date.now();
    ayn.floorAt = Date.now();
    setDirect(guess(ayn.floor));
    setTimeout(() => ayn.on && showOnSlider(f), 120);
    setTimeout(() => ayn.on && showOnSlider(f), 400);
  };
  // Only once the backend says the dimming is on screen (its effect may still
  // be waiting for the compile throttle); after 1.5 s the backlight rises anyway.
  const until = Date.now() + 1500;
  const ask = () =>
    setSoftTarget(ayn.target, "up").then(
      (st: any) => {
        if (!atFloor(ayn.target)) return; // back below meanwhile: the backlight stays
        if (st?.soft_dimmed || Date.now() > until) setTimeout(() => atFloor(ayn.target) && rise(), handoverLead());
        else setTimeout(ask, 100);
      },
      () => atFloor(ayn.target) && rise(),
    );
  ask();
}

// Down: the backlight drops first, the backend passes through after the lag.
function crossDown(raw: number, f: number) {
  clearTimeout(softSend.timer);
  setDirect(f);
  setTimeout(() => setSoftTarget(raw, "down").catch(() => {}), handoverLag());
}

const atFloor = (t: number) => t >= SOFT_FROM && t < ayn.floor;
const belowBand = (t: number) => t > 0 && t < SOFT_FROM;

function aynIntercept(f: number): boolean {
  if (!ayn.on || demoTimer) return false;
  const raw = rawFor(f);
  cancelBrightness();
  clearTimeout(ayn.pending);
  const was = ayn.target;
  if (raw >= SOFT_FROM && raw < ayn.floor) {
    ayn.holding = false;
    if (belowBand(was)) {
      ayn.target = raw;
      showOnSlider(f);
      ayn.lastF = f;
      crossUp(f);
      return true; // swallowed: crossUp raises the backlight once dimmed
    }
    ayn.target = raw;
    softPush(raw);
    ownWrite.at = Date.now();
    showOnSlider(f);
    ayn.lastF = f;
    ensureFloor();
    return true; // swallowed: the backlight stays at the floor
  }
  // Steam sends its last known value when a touch starts or the slider is
  // dropped (e.g. 100% when the drag began there), ~10 ms before the real
  // one. Passed on, that flashed on screen and its echo pulled the slider
  // out of the band. So the first request outside the band after a pause
  // is held back briefly; a following request voids it. Mid-drag requests
  // (a stream outside the band) go straight through.
  const now = Date.now();
  // Steam's D-pad repeats every ~50 ms, so a request within 120 ms of the last
  // one, or one arriving while a hold is pending, is a real one, not the stale value.
  const streaming = ayn.holding || now - ayn.lastOut < 120;
  ayn.lastOut = now;
  ayn.holding = false;
  if (!streaming) {
    ayn.holding = true;
    // The target waits with it: pushed at once, the stale value undimmed the
    // picture for a moment (the flash on a click in the band).
    ayn.pending = setTimeout(() => {
      ayn.holding = false;
      if (!ayn.on) return;
      ayn.target = raw;
      if (atFloor(was) && belowBand(raw)) return crossDown(raw, f);
      setDirect(f);
      softPush(raw);
    }, 60);
    return true;
  }
  ayn.target = raw;
  if (atFloor(was) && belowBand(raw)) {
    crossDown(raw, f);
    return true; // swallowed: crossDown drops the backlight itself
  }
  softPush(raw);
  return false;
}

function DemoToggle() {
  const [on, setOn] = useState(demoTimer !== null);
  const [, tick] = useState(0);
  useEffect(() => {
    const f = (v: boolean) => {
      setOn(v);
      tick((n) => n + 1);
    };
    demoListeners.add(f);
    const id = setInterval(() => demoTimer && tick((n) => n + 1), 1000);
    return () => {
      demoListeners.delete(f);
      clearInterval(id);
    };
  }, []);
  const c = demo.cfg;
  const idx = Math.max(0, DEMO_PERIODS.indexOf(c.period));
  const left = c.stopOn ? Math.max(0, c.stopMin * 60 - (Date.now() - demo.started) / 1000) : null;
  const status = on
    ? (c.mode === "tour" ? `${demo.label} · ` : "") +
      `cycle ${Math.floor(demo.phase) + 1}${c.cyclesOn ? ` of ${c.cycles}` : ""}` +
      (left !== null ? ` · stops in ${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}` : "")
    : c.mode === "tour"
      ? `At ${TOUR.join(", ")} in turn: corrected, then uncorrected, ${c.hold} s each.`
      : `Glides through ${c.range === "full" ? "the whole range, 40-4096" : "the band and around it, 50-1000"}, skipping its unstable edge.`;
  return (
    <>
      <PanelSectionRow>
        <ToggleField
          label={c.mode === "tour" ? "Demo: before and after" : "Demo: sweep brightness"}
          description={status}
          checked={on}
          onChange={(v) => setDemo(v)}
        />
      </PanelSectionRow>
      <Choice<"sweep" | "tour">
        label="Mode"
        value={c.mode}
        options={[["sweep", "Sweep"], ["tour", "Before / after"]]}
        onPick={(v) => {
          setDemoCfg({ mode: v });
          if (on) setDemo(true); // start over in the new mode
        }}
      />
      {c.mode === "tour" ? (
        <PanelSectionRow>
          <SliderField
            label={`Hold: ${c.hold} s corrected, ${c.hold} s uncorrected`}
            value={c.hold}
            min={2}
            max={10}
            step={1}
            onChange={(v) => setDemoCfg({ hold: v })}
          />
        </PanelSectionRow>
      ) : (
        <Choice<"band" | "full">
          label="Range"
          value={c.range}
          options={[
            ["band", "Around the band"],
            ["full", "Full 40-4096"],
          ]}
          onPick={(v) => setDemoCfg({ range: v })}
        />
      )}
      {c.mode !== "tour" && (
        <PanelSectionRow>
          <SliderField
            label={
              `Speed: ${c.period < 120 ? `${c.period} s` : `${c.period / 60} min`} per sweep around the band` +
              (c.range === "full" ? ` (full range: ${Math.round(sweepSeconds(c))} s)` : "") +
              (c.period < 30 ? ", faster than the correction" : "")
            }
            value={idx}
            min={0}
            max={DEMO_PERIODS.length - 1}
            step={1}
            notchCount={DEMO_PERIODS.length}
            onChange={(v) => setDemoCfg({ period: DEMO_PERIODS[v] })}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ToggleField label="Stop after a while" checked={c.stopOn} onChange={(v) => setDemoCfg({ stopOn: v })} />
      </PanelSectionRow>
      {c.stopOn && (
        <PanelSectionRow>
          <SliderField
            label={`After ${c.stopMin} min`}
            value={c.stopMin}
            min={1}
            max={60}
            step={1}
            onChange={(v) => setDemoCfg({ stopMin: v })}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ToggleField label="Stop after cycles" checked={c.cyclesOn} onChange={(v) => setDemoCfg({ cyclesOn: v })} />
      </PanelSectionRow>
      {c.cyclesOn && (
        <PanelSectionRow>
          <SliderField
            label={`After ${c.cycles} cycle${c.cycles === 1 ? "" : "s"}`}
            value={c.cycles}
            min={1}
            max={20}
            step={1}
            onChange={(v) => setDemoCfg({ cycles: v })}
          />
        </PanelSectionRow>
      )}
    </>
  );
}

function Brightness({ brightness }: { brightness: number | null }) {
  return (
    <>
      <PanelSectionRow>
        <SliderField
          label={`Brightness ${brightness ?? "?"}`}
          value={Math.round(guess(brightness ?? 40) * 100)}
          min={0}
          max={100}
          step={1}
          onChange={(v) => {
            cancelBrightness();
            SteamClient.System.Display.SetBrightness(v / 100);
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Focusable style={{ display: "flex", gap: "6px" }} flow-children="horizontal">
          {PRESETS.map((b) => (
            <DialogButton key={b} style={{ minWidth: 0, flex: 1, padding: "0 4px" }} onClick={() => setRawBrightness(b)}>
              {b}
            </DialogButton>
          ))}
        </Focusable>
      </PanelSectionRow>
    </>
  );
}

// Collapsible groups: the header is one full-width button (A and touch);
// which ones are open is remembered per device.
function useOpen(key: string): [boolean, () => void] {
  const id = `oled-tint-open-${key}`;
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(id) === "1";
    } catch {
      return false;
    }
  });
  const toggle = () => {
    setOpen(!open);
    try {
      localStorage.setItem(id, open ? "0" : "1");
    } catch {
      /* not remembered, fine */
    }
  };
  return [open, toggle];
}

function Group({ name, summary, children }: { name: string; summary?: string; children: ReactNode }) {
  const [open, toggle] = useOpen(name);
  return (
    <>
      <PanelSectionRow>
        <DialogButton
          style={{ display: "flex", justifyContent: "space-between", padding: "6px 12px", minWidth: 0 }}
          onClick={toggle}
        >
          <span>
            {open ? "▾" : "▸"} {name}
          </span>
          {!open && summary && <span style={{ opacity: 0.6, fontSize: "12px" }}>{summary}</span>}
        </DialogButton>
      </PanelSectionRow>
      {open && children}
    </>
  );
}

// A row of buttons, one per choice, the chosen one marked.
function Choice<T>({ label, value, options, onPick, disabled }: {
  label: string;
  value: T;
  options: [T, string][];
  onPick: (v: T) => void;
  disabled?: boolean;
}) {
  return (
    <PanelSectionRow>
      <div style={{ fontSize: "12px", opacity: disabled ? 0.4 : 0.7, margin: "2px 0" }}>{label}</div>
      <Focusable style={{ display: "flex", gap: "6px" }} flow-children="horizontal">
        {options.map(([v, text]) => (
          <DialogButton
            key={text}
            disabled={disabled}
            style={{
              minWidth: 0,
              flex: 1,
              padding: "0 4px",
              ...(v === value ? { background: "#1a9fff", color: "#fff" } : {}),
            }}
            onClick={() => onPick(v)}
          >
            {text}
          </DialogButton>
        ))}
      </Focusable>
    </PanelSectionRow>
  );
}

function ButtonRow({ buttons }: { buttons: [string, () => void, boolean?][] }) {
  return (
    <PanelSectionRow>
      <Focusable style={{ display: "flex", gap: "6px" }} flow-children="horizontal">
        {buttons.map(([text, run, disabled]) => (
          <DialogButton key={text} disabled={disabled} style={{ minWidth: 0, flex: 1, padding: "0 4px" }} onClick={run}>
            {text}
          </DialogButton>
        ))}
      </Focusable>
    </PanelSectionRow>
  );
}

function RenameModal({ name, closeModal, onDone }: { name: string; closeModal?: () => void; onDone: (n: string) => void }) {
  const [value, setValue] = useState(name);
  return (
    <ConfirmModal
      strTitle="Rename profile"
      strOKButtonText="Rename"
      closeModal={closeModal}
      onOK={() => onDone(value)}
    >
      <TextField value={value} onChange={(e) => setValue(e.target.value)} />
    </ConfirmModal>
  );
}

const STATUS_COLOUR = { on: "#59bf40", off: "#8b929a", paused: "#e8a33d" };

// Quick Access first, then the page: from the row buttons the menu stayed
// open and focused over the page (input went to the sidebar underneath).
const go = (route: string) => {
  setDemo(false); // it would move the backlight under the page's own steps
  Navigation.CloseSideMenus();
  setTimeout(() => {
    Navigation.Navigate(route);
    Navigation.CloseSideMenus();
  }, 150);
};

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [note, setNote] = useState("");

  useEffect(() => {
    let alive = true;
    const tick = () => getState().then((s) => alive && setState(s));
    tick();
    const id = setInterval(tick, 500);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!state) return null;
  const s = state;
  snap.on = s.band_snap;
  snap.zone = s.snap_zone;
  const factory = s.profile === s.profiles[0];
  const gameName = Router.MainRunningApp?.display_name ?? "this game";
  const tables = s.tables.steps.length
    ? `Panel tables: ${s.tables.steps.length} steps (${s.tables.steps.join(", ")}), measured ${s.tables.measured ?? "date unknown"}`
    : "No panel tables yet (camera measurement)";

  return (
    <PanelSection>
      <PanelSectionRow>
        <div style={{ fontSize: "13px", lineHeight: "18px" }}>
          <div>
            <span style={{ color: STATUS_COLOUR[s.status.state] }}>●</span> {s.status.text}
          </div>
          <div style={{ opacity: 0.6, fontSize: "12px" }}>
            Brightness {s.brightness ?? "?"} · {s.enabled ? describe(s) : "off"}
          </div>
        </div>
      </PanelSectionRow>
      {s.safe_mode && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description="gamescope stalled several times: dither and menu switching are paused."
            onClick={async () => setState(await clearSafeMode())}
          >
            Safe mode on: resume
          </ButtonItem>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ToggleField
          label="Correction"
          checked={s.enabled}
          onChange={async (v) => {
            setState(await setEnabled(v));
            // floor dimming holds the backlight at its floor: off gives the
            // asked-for brightness back (it stayed at the floor and looked
            // "corrected"), on takes it up again.
            if (s.dimming === "soft" && s.soft_target && s.soft_target >= SOFT_FROM && s.soft_target < s.soft_floor) {
              ownWrite.at = Date.now();
              await setRawBrightness(v ? s.soft_floor : s.soft_target, true);
              ownWrite.at = Date.now();
            }
          }}
        />
      </PanelSectionRow>
      <Brightness brightness={s.dimming === "soft" && s.soft_target ? s.soft_target : s.brightness} />
      <PanelSectionRow>
        <DropdownItem
          label="Profile"
          rgOptions={s.profiles.map((p) => ({ data: p, label: p }))}
          selectedOption={s.profile}
          onChange={async (o) => setState(await setProfile(o.data))}
        />
      </PanelSectionRow>
      {s.dither_ready && s.dimming !== "soft" && (
        <PanelSectionRow>
          <ToggleField
            label="Dither near-black"
            description="Holds colour vibrance at 100% while on."
            checked={s.dither}
            onChange={async (v) => setState(await setDither(v))}
          />
        </PanelSectionRow>
      )}
      <ButtonRow
        buttons={[
          ["Calibrate", () => go(CALIBRATE_ROUTE)],
          ["Test pattern", () => go(PATTERN_ROUTE)],
          ["Compare", async () => setState(await compare(5)), !s.enabled],
        ]}
      />
      {s.game && (
        <PanelSectionRow>
          <ToggleField
            label={`Off for ${gameName}`}
            checked={s.game_off}
            onChange={async (v) => setState(await setGameOff(v, gameName))}
          />
        </PanelSectionRow>
      )}

      <Group
        name="Shadows"
        summary={s.dither ? `${s.dither_temporal || "still"} · ${s.shadow_curve === "g22" ? "2.2" : "panel"}` : undefined}
      >
        <Choice
          label="Dither pattern"
          value={s.dither_temporal || "still"}
          options={[["still", "Still"], ["pair", "Pair"], ["golden", "Golden"]]}
          disabled={!s.dither}
          onPick={async (v) => setState(await setDitherTemporal(v === "still" ? false : (v as "pair" | "golden")))}
        />
        <Choice<"g22" | "native">
          label="Shadow curve"
          value={s.shadow_curve}
          options={[["native", "Panel"], ["g22", "Gamma 2.2"]]}
          disabled={!s.dither}
          onPick={async (v) => setState(await setShadowCurve(v))}
        />
        <PanelSectionRow>
          <SliderField
            label="Crush near-black"
            description={s.crush ? `Greys up to ${s.crush} go to true black.` : undefined}
            value={s.crush}
            min={0}
            max={s.max_crush}
            step={1}
            showValue
            onChange={async (v) => setState(await setCrush(v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "12px", opacity: 0.6 }}>{tables}</div>
        </PanelSectionRow>
      </Group>

      <Group name="Demo" summary={demoTimer ? "running" : undefined}>
        <DemoToggle />
      </Group>

      <Group name="Advanced">
      <Choice<"band" | "soft">
        label={
          s.dimming === "soft"
            ? `Dimming method: floor dimming, ${s.soft_target ?? "?"} via backlight ${s.soft_floor}` +
              (s.soft_gain < 1 ? ` (picture x${s.soft_gain.toFixed(2)})` : "")
            : "Dimming method (experimental)"
        }
        value={s.dimming}
        options={[
          ["band", "Dither correction"],
          ["soft", "Floor dimming"],
        ]}
        onPick={async (v) => {
          if (v === s.dimming) return; // picked again: keep the target (it was reset to the floor)
          const raw = s.brightness ?? s.soft_floor;
          if (v === "soft") {
            setState(await setDimming("soft", raw));
            if (raw >= SOFT_FROM && raw < s.soft_floor) {
              ownWrite.at = Date.now(); // our own write: not a request (it reset the target to 543)
              await setRawBrightness(s.soft_floor, true);
              ownWrite.at = Date.now();
            }
          } else {
            const back = s.dimming === "soft" ? (s.soft_target ?? raw) : raw;
            setState(await setDimming("band", 0));
            await setRawBrightness(back, true);
          }
        }}
      />
        <PanelSectionRow>
          <ToggleField
            label="Info overlay"
            description="In Steam's performance overlay, over games: brightness, what the correction does and why."
            checked={s.osd}
            onChange={async (v) => setState(await setOsd(v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Fine correction (ReShade)"
            description="Off: gamescope's coarser colour table, no dither."
            checked={s.engine === "reshade"}
            onChange={async (v) => setState(await setEngine(v ? "reshade" : "look"))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Skip the band edge"
            description={`Steps the brightness over ${s.snap_zone[0]}–${s.snap_zone[1]}, where the panel's mode is unpredictable.`}
            checked={s.band_snap}
            onChange={async (v) => setState(await setBandSnap(v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Correct Steam's menus"
            description="Over games, while a menu is open."
            checked={s.menu_correction}
            onChange={async (v) => setState(await setMenuCorrection(v))}
          />
        </PanelSectionRow>
        {s.dither && (
          <PanelSectionRow>
            <ToggleField
              label="Dither in Steam's own screens"
              description="Library, store, settings. Games always get it; a menu over a game is “Correct Steam's menus”."
              checked={s.dither_ui}
              onChange={async (v) => setState(await setDitherUi(v))}
            />
          </PanelSectionRow>
        )}
        <ButtonRow
          buttons={[
            [
              "Random brightness",
              () => {
                const [lo, hi] = BAND;
                setRawBrightness(Math.round(lo * Math.pow(hi / lo, Math.random())));
              },
            ],
          ]}
        />
        <PanelSectionRow>
          <div style={{ fontSize: "12px", opacity: 0.7, margin: "2px 0" }}>Profile “{s.profile}”</div>
        </PanelSectionRow>
        <ButtonRow
          buttons={[
            ["New", async () => setState(await profileNew())],
            ["Copy", async () => setState(await profileDuplicate())],
            [
              "Rename",
              () =>
                showModal(
                  <RenameModal name={s.profile} onDone={async (n) => setState(await profileRename(n))} />,
                ),
              factory,
            ],
            [
              "Delete",
              () =>
                showModal(
                  <ConfirmModal
                    strTitle={`Delete “${s.profile}”?`}
                    strDescription="Its calibration is gone for good; Factory becomes active."
                    strOKButtonText="Delete"
                    onOK={async () => setState(await profileDelete())}
                  />,
                ),
              factory,
            ],
          ]}
        />
        <ButtonRow
          buttons={[
            ["Export", async () => setNote(`Saved: ${await exportCalibration()}`)],
            [
              "Import",
              async () => {
                const r = await importCalibration("");
                setNote(r.ok ? `Imported as “${r.profile}” (${r.tables} panel tables)` : `Import failed: ${r.error}`);
                setState(await getState());
              },
            ],
          ]}
        />
        <PanelSectionRow>
          <div style={{ fontSize: "12px", opacity: 0.6 }}>
            {note || "Export saves the profile and panel tables to Downloads; Import takes the newest file there."}
          </div>
        </PanelSectionRow>
      </Group>
    </PanelSection>
  );
}

// Steam's Quick Access and main menus are their own windows, which ReShade
// doesn't reach (they stayed green next to the corrected game). Tell the
// backend whenever one opens or closes; it moves the correction to
// gamescope's look meanwhile. The windows appear some time after Steam
// starts, so look for them until both are found.
const MENU_TREES = ["QuickAccess-NA", "MainNavMenuContainer"];

function watchMenus(): () => void {
  const watched = new Map<Window, () => void>();
  let last: boolean | null = null;
  const report = () => {
    const open = [...watched.keys()].some((w) => !w.document.hidden);
    if (open !== last) {
      last = open;
      setOverlay(open).catch(() => {});
    }
  };
  const find = () => {
    const nav = (window as any).FocusNavController;
    for (const ctx of nav?.m_rgAllContexts ?? []) {
      for (const tree of ctx.m_rgGamepadNavigationTrees ?? []) {
        if (!MENU_TREES.includes(tree?.id)) continue;
        const w: Window | undefined = tree.m_Root?.m_element?.ownerDocument?.defaultView;
        if (w && !watched.has(w)) {
          const on = () => report();
          w.addEventListener("visibilitychange", on);
          watched.set(w, on);
        }
      }
    }
    report();
  };
  find();
  const timer = setInterval(() => {
    if (watched.size < MENU_TREES.length) find();
    // Keep confirming while a menu is open: the backend treats an open menu
    // it hasn't heard about for a while as closed (a missed event or reload).
    if (last) setOverlay(true).catch(() => {});
  }, 2000);
  return () => {
    clearInterval(timer);
    watched.forEach((on, w) => w.removeEventListener("visibilitychange", on));
    setOverlay(false).catch(() => {});
  };
}

export default definePlugin(() => {
  const stopMenus = watchMenus();
  routerHook.addRoute(CALIBRATE_ROUTE, Calibrate, { exact: true });
  routerHook.addRoute(PATTERN_ROUTE, TestPattern, { exact: true });
  // Wake the backend whenever Steam changes brightness, so it doesn't have to
  // poll fast all the time. Not in Decky's typings, hence the cast.
  const brightness = (SteamClient.System.Display as any).RegisterForBrightnessChanges?.(() => {
    brightnessChanged();
    snapLater();
  });
  getState().then((st) => {
    snap.on = st.band_snap;
    snap.zone = st.snap_zone;
    ayn.target = st.soft_target ?? 0;
    getSnap().then(syncAyn).catch(() => {});
  });
  // A game switch, the per-game off switch or a remote change aren't
  // brightness events: look now and then.
  const aynWatch = setInterval(() => getSnap().then(syncAyn).catch(() => {}), 2000);
  const display = SteamClient.System.Display as any;
  const original = display.SetBrightness.bind(display);
  setRealSetter(original);
  display.SetBrightness = (f: number) => {
    const swallowed = aynIntercept(f);
    sliderLog(`F set ${f.toFixed(4)} raw ${rawFor(f)} ${swallowed ? "swallowed" : "passed"} target ${ayn.target}`).catch(() => {});
    if (swallowed) return;
    return original(f);
  };

  return {
    name: "OLED Tint",
    titleView: <div className={staticClasses.Title}>OLED Tint</div>,
    content: <Content />,
    icon: <FaPalette />,
    onDismount() {
      stopMenus();
      clearInterval(aynWatch);
      if (ayn.on) {
        ayn.on = false;
        releaseAyn();
      }
      clearTimeout(ayn.pending);
      clearTimeout(softSend.timer);
      setDemo(false);
      routerHook.removeRoute(CALIBRATE_ROUTE);
      routerHook.removeRoute(PATTERN_ROUTE);
      brightness?.unregister();
      display.SetBrightness = original;
      setRealSetter((f) => SteamClient.System.Display.SetBrightness(f));
      clearTimeout(snap.timer);
      wizardEnd(); // never leave a calibration preview behind
    },
  };
});
