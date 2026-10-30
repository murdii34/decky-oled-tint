import { Focusable, GamepadButton, Navigation } from "@decky/ui";
import { ReactNode, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  Kind,
  Step,
  formatHue,
  formatLevel,
  formatLift,
  formatTint,
  resolve,
  setProfile,
  setRawBrightness,
  ownWrite,
  getState,
  setTints,
  to8bit,
  wizardBegin,
  wizardEnd,
  wizardPing,
  wizardStep,
} from "./backend";


const ROW = (n: number) => ({
  display: "grid",
  gridTemplateColumns: `repeat(${n}, minmax(0, 1fr))`,
  gap: "8px",
});

export const CALIBRATE_ROUTE = "/oled-tint/calibrate";

// 8-bit grey levels from near-black to three-quarter grey: every calibrated
// level up to 192, plus 8/24/40, which fall between gamescope's grid points
// and can only be judged (they're blended from their neighbours).
// White (255) is at the end of the gradient bar, kept small so a big bright
// patch doesn't spoil your eyes' dark adaptation.
const RAMP = [4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 160, 192];

export const grey = (v: number) => `rgb(${v},${v},${v})`;
// Neutral UI greys, given in percent for readability.
export const ui = (pct: number) => grey(Math.round((pct / 100) * 255));


// How much a change drags the set points 1 and 2 positions away.
const DRAG = [0.35, 0.1];

// Tap targets: a swatch selects its level; in-between swatches (8/24/40)
// select the grid point just above them, which is what controls them most
// (8 entirely). The gradient bar selects the level nearest the tap, so the
// white end reaches 255. Black is the untouched reference.
function Pattern({
  selected,
  levels8,
  onSelect,
}: {
  selected: number | null;
  levels8: number[];
  onSelect: (index: number) => void;
}) {
  const above = (v: number) => levels8.findIndex((l) => l >= v);
  const nearest = (v: number) =>
    levels8.reduce((best, l, i) => (Math.abs(l - v) < Math.abs(levels8[best] - v) ? i : best), 0);
  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: "8px" }}>
      <div style={{ flex: 1, minHeight: 0, display: "flex", gap: "6px" }}>
        <Swatch color="#000" label="0" outline />
        {RAMP.map((v) => (
          <Swatch
            key={v}
            color={grey(v)}
            label={`${v}`}
            marked={v === selected}
            onClick={() => onSelect(above(v))}
          />
        ))}
      </div>
      <div
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          onSelect(nearest(((e.clientX - r.left) / r.width) * 255));
        }}
        style={{
          height: "24px",
          flexShrink: 0,
          borderRadius: "4px",
          background: "linear-gradient(to right, #000, #fff)",
        }}
      />
    </div>
  );
}

export function Swatch({
  color,
  label,
  outline,
  marked,
  dim,
  onClick,
}: {
  color: string;
  label: string;
  outline?: boolean;
  marked?: boolean;
  dim?: boolean;
  onClick?: () => void;
}) {
  // Indicators stay off the patch itself: a light edge touching a dark grey
  // changes how dark and how tinted it looks. The selection ring is held
  // away from the patch by a black gap, and in-between swatches are told
  // apart by width, not by any marking on them.
  const selectable = !!onClick && !dim;
  return (
    <div
      onClick={onClick}
      style={{
        flex: dim ? 0.6 : 1,
        minWidth: 0,
        display: "flex",
        flexDirection: "column",
        textAlign: "center",
      }}
    >
      <div
        style={{
          flex: 1,
          minHeight: 0,
          padding: "3px",
          borderRadius: "7px",
          boxSizing: "border-box",
          border: `2px solid ${marked ? ui(50) : "transparent"}`,
          display: "flex",
        }}
      >
        <div
          style={{
            flex: 1,
            background: color,
            borderRadius: "4px",
            boxSizing: "border-box",
            border: outline ? `1px solid ${ui(12)}` : "none",
          }}
        />
      </div>
      <div
        style={{
          height: marked ? "4px" : "2px",
          margin: marked ? "2px 4px 0" : "3px 8px 1px",
          borderRadius: "2px",
          background: marked ? ui(65) : selectable ? ui(22) : "transparent",
        }}
      />
      <div
        style={{
          fontSize: "10px",
          fontWeight: marked ? 700 : 400,
          color: ui(marked ? 80 : dim ? 22 : 45),
          marginTop: "2px",
        }}
      >
        {label}
      </div>
    </div>
  );
}

// Colour keys at the slider's two ends, readable in peripheral vision while
// you look at the swatches. Small and muted: any coloured area nudges your
// eyes' adaptation, which is what you're judging with.
const KEY = (color: string) => (
  <span
    style={{
      display: "inline-block",
      width: "10px",
      height: "10px",
      borderRadius: "2px",
      background: color,
      verticalAlign: "middle",
    }}
  />
);

function SliderEnds() {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
      <span>
        {KEY("rgb(150,55,150)")} <span style={{ marginLeft: "4px" }}>← magenta</span>
      </span>
      <span>
        <span style={{ marginRight: "4px" }}>green →</span> {KEY("rgb(55,140,55)")}
      </span>
    </div>
  );
}

// Same idea for the brightness slider: a dark and a light key.
// And for red/blue balance.
function HueEnds() {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
      <span>
        {KEY("rgb(60,80,160)")} <span style={{ marginLeft: "4px" }}>← blue</span>
      </span>
      <span>
        <span style={{ marginRight: "4px" }}>red →</span> {KEY("rgb(160,60,55)")}
      </span>
    </div>
  );
}

function BrightnessEnds() {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
      <span>
        {KEY(ui(12))} <span style={{ marginLeft: "4px" }}>← darker</span>
      </span>
      <span>
        <span style={{ marginRight: "4px" }}>brighter →</span> {KEY(ui(70))}
      </span>
    </div>
  );
}

// Follows target smoothly: quick to grow, slow to shrink, so the chart
// makes room fast when a value goes off-scale but doesn't pump while dragging.
function useEased(target: number, upMs = 120, downMs = 700): number {
  const [value, setValue] = useState(target);
  const current = useRef(target);
  useEffect(() => {
    let frame = 0;
    let prev = performance.now();
    const tick = (now: number) => {
      const dt = now - prev;
      prev = now;
      const c = current.current;
      const tau = target > c ? upMs : downMs;
      const next = target + (c - target) * Math.exp(-dt / tau);
      current.current = Math.abs(next - target) < 0.05 ? target : next;
      setValue(current.current);
      if (current.current !== target) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target]);
  return value;
}

// Equalizer-style view of the correction: one column per grey level, up is
// magenta, down is green; the dashed line is brightness (up = brighter).
// Greys only, so it can't throw off your judgement.
function Curve({
  steps,
  step,
  levels,
  level,
}: {
  steps: Step[];
  step: number;
  levels: number[];
  level: number;
}) {
  const W = 220;
  const H = 86;
  const pad = 8;
  const curves = steps.map((s) => resolve(s.v, levels));
  const lifts = resolve(steps[step].l, levels);
  // Fit the largest value with some headroom, never tighter than ±20 so
  // small tweaks still read as small.
  const peak = Math.max(...curves.flat().map(Math.abs));
  const range = useEased(Math.max(20, Math.min(400, peak * 1.15)));
  // Brightness gets its own scale (never tighter than ±10); brighter is up.
  const liftRange = useEased(Math.max(10, Math.min(50, Math.max(...lifts.map(Math.abs)) * 1.15)));
  const yl = (t: number) => H / 2 - (t / liftRange) * (H / 2 - pad);
  const x = (i: number) => pad + (i * (W - 2 * pad)) / (levels.length - 1);
  // Negative (magenta) plots upward.
  const y = (t: number) => H / 2 + (t / range) * (H / 2 - pad);
  const path = (c: number[]) => c.map((t, i) => `${i ? "L" : "M"}${x(i)},${y(t)}`).join(" ");

  return (
    <svg width={W} height={H} style={{ flexShrink: 0, display: "block" }}>
      <line x1={pad} x2={W - pad} y1={H / 2} y2={H / 2} stroke={ui(22)} strokeDasharray="3 3" />
      <text x={W - pad} y={10} fill={ui(35)} fontSize="9" textAnchor="end">
        magenta {Math.round(range)}
      </text>
      <text x={W - pad} y={H - 3} fill={ui(35)} fontSize="9" textAnchor="end">
        green {Math.round(range)}
      </text>
      {curves.map((c, i) =>
        i === step ? null : <path key={i} d={path(c)} fill="none" stroke={ui(16)} strokeWidth="1" />,
      )}
      {lifts.some((t) => Math.abs(t) >= 0.05) && (
        <path
          d={lifts.map((t, i) => `${i ? "L" : "M"}${x(i)},${yl(t)}`).join(" ")}
          fill="none"
          stroke={ui(40)}
          strokeWidth="1"
          strokeDasharray="4 3"
        />
      )}
      <path d={path(curves[step])} fill="none" stroke={ui(55)} strokeWidth="1.5" />
      {curves[step].map((t, i) => {
        const set = steps[step].v[i] !== null;
        return (
          <circle
            key={i}
            cx={x(i)}
            cy={y(t)}
            r={i === level ? 4.5 : set ? 3 : 2.5}
            fill={set ? ui(75) : "#000"}
            stroke={i === level ? ui(90) : ui(55)}
            strokeWidth={i === level ? 1.5 : 1}
          />
        );
      })}
    </svg>
  );
}

// The page draws its own controls: Steam's slider and button components bring
// their own controller handling, which fights the page's navigation.
const SEL = `0 0 0 2px ${ui(70)}`;

function Slider({
  ends,
  value,
  limit,
  selected,
  onSelect,
  onChange,
}: {
  ends: ReactNode;
  value: number;
  limit: number;
  selected: boolean;
  onSelect: () => void;
  onChange: (v: number) => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  const fromPointer = (clientX: number) => {
    const r = track.current!.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    onChange(Math.round(-limit + f * 2 * limit));
  };
  const pos = ((value + limit) / (2 * limit)) * 100;
  return (
    <div
      style={{
        padding: "3px 8px 5px",
        borderRadius: "4px",
        boxShadow: selected ? SEL : "none",
        fontSize: "12px",
        color: ui(75),
      }}
    >
      {ends}
      <div
        ref={track}
        onPointerDown={(e) => {
          onSelect();
          e.currentTarget.setPointerCapture(e.pointerId);
          fromPointer(e.clientX);
        }}
        onPointerMove={(e) => {
          if (e.currentTarget.hasPointerCapture(e.pointerId)) fromPointer(e.clientX);
        }}
        style={{ position: "relative", height: "22px", touchAction: "none", cursor: "pointer" }}
      >
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: "9px",
            height: "4px",
            borderRadius: "2px",
            background: ui(25),
          }}
        />
        {/* Centre (neutral) mark */}
        <div style={{ position: "absolute", left: "50%", top: "5px", width: "2px", height: "12px", background: ui(40) }} />
        <div
          style={{
            position: "absolute",
            left: `calc(${pos}% - 8px)`,
            top: "3px",
            width: "16px",
            height: "16px",
            borderRadius: "8px",
            background: ui(selected ? 95 : 75),
          }}
        />
      </div>
    </div>
  );
}

function Btn({
  children,
  height,
  selected,
  disabled,
  dim,
  onClick,
}: {
  children: ReactNode;
  height: number;
  selected?: boolean;
  disabled?: boolean;
  dim?: boolean;
  onClick: () => void;
}) {
  return (
    <div
      onClick={disabled ? undefined : onClick}
      style={{
        height: `${height}px`,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minWidth: 0,
        overflow: "hidden",
        whiteSpace: "nowrap",
        borderRadius: "4px",
        background: selected ? ui(30) : ui(14),
        boxShadow: selected ? SEL : "none",
        color: ui(disabled ? 30 : dim ? 55 : 88),
        fontSize: height > 40 ? "15px" : "14px",
        cursor: disabled ? "default" : "pointer",
      }}
    >
      {children}
    </div>
  );
}

type Cell = { step: number; level: number; kind: Kind; v: number | null };

export function Calibrate() {
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [levels, setLevels] = useState<number[]>([]);
  const [maxTint, setMaxTint] = useState(100);
  const [maxLift, setMaxLift] = useState(50);
  const [maxHue, setMaxHue] = useState(100);
  const [step, setStep] = useState(0);
  const [level, setLevel] = useState(0);
  const [corrected, setCorrected] = useState(true);
  const [drag, setDrag] = useState(true);
  // Self-made navigation: row 0 tint, 1 red/blue, 2 brightness, 3 grey level, 4 actions.
  const [row, setRow] = useState(0);
  const [col, setCol] = useState(0);
  // Each entry is one undoable action: the values it overwrote.
  const [undo, setUndo] = useState<Cell[][]>([]);
  const [profile, setProfileName] = useState("");
  const [profiles, setProfiles] = useState<string[]>([]);

  // Only the newest value per slider goes to the backend; while it's busy
  // building a LUT, intermediate drag positions are dropped instead of queued.
  const pending = useRef(new Map<string, [number, number, number | null, Kind]>());
  const sending = useRef(false);
  const stepRef = useRef(0);
  stepRef.current = step;
  // The profile the queued edits belong to: sent with every batch (the
  // backend drops a batch for a profile no longer active), and no edits are
  // taken while switching profiles.
  const profileRef = useRef("");
  const switching = useRef(false);
  const flush = async () => {
    if (sending.current) return;
    sending.current = true;
    try {
      while (pending.current.size) {
        const batch = [...pending.current.values()];
        pending.current.clear();
        const r = await setTints(batch, stepRef.current, profileRef.current);
        if (!r.accepted) {
          // Another profile became active meanwhile: what's queued belongs
          // to the old one. Drop it and show the active profile's values.
          pending.current.clear();
          const st = await getState();
          profileRef.current = st.profile;
          setSteps(st.steps);
          setProfileName(st.profile);
          setProfiles(st.profiles);
          setUndo([]);
          break;
        }
        if (r.created) profileRef.current = r.profile; // editing Factory started it
      }
      // Editing Factory starts a new profile (Custom, Custom 2, ...): show it.
      const s = await getState();
      setProfileName(s.profile);
      setProfiles(s.profiles);
      profileRef.current = s.profile;
    } finally {
      sending.current = false;
    }
    // An edit that came in while getState() was out found `sending` set and
    // left it to us: send it now, or leaving the page would lose it.
    if (pending.current.size) flush();
  };

  useEffect(() => {
    let restore: number | null = null;
    wizardBegin().then((s) => {
      restore = s.brightness;
      setSteps(s.steps);
      setLevels(s.levels);
      setMaxTint(s.max_tint);
      setMaxLift(s.max_lift);
      setMaxHue(s.max_hue);
      setProfileName(s.profile);
      setProfiles(s.profiles);
      profileRef.current = s.profile;
    });
    // Keep the backend's preview alive; it drops it if the pings stop.
    const ping = setInterval(() => wizardPing(), 5000);
    return () => {
      clearInterval(ping);
      wizardEnd();
      if (restore !== null) {
        // AYN-style: the backlight was the floor, the request its target;
        // give back the floor as our own write, so it isn't taken for a
        // new request (that replaced the target with 543).
        ownWrite.at = Date.now();
        setRawBrightness(restore, true).then(() => (ownWrite.at = Date.now()));
      }
    };
  }, []);

  useEffect(() => {
    if (steps) wizardStep(step, corrected);
  }, [step, corrected, steps === null]);

  useEffect(() => {
    if (steps) setRawBrightness(steps[step].b, true);
  }, [step, steps === null]);

  // Plugin code runs in Steam's background context, so `document` isn't the
  // window on screen. An anchor element tells us which one is, and the page
  // is portalled into its body: that puts it above Steam's header and footer
  // bars, which sit on top of anything inside the route.
  const anchor = useRef<HTMLDivElement>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  useEffect(() => setHost(anchor.current?.ownerDocument.body ?? null), []);

  if (!steps) return <div ref={anchor} />;

  const write = (changes: Cell[]) => {
    if (switching.current) return;
    setSteps((prev) =>
      prev!.map((s, i) => {
        const apply = (kind: Kind) =>
          s[kind].map((t, j) => {
            const c = changes.find((c) => c.step === i && c.level === j && c.kind === kind);
            return c ? c.v : t;
          });
        return { ...s, v: apply("v"), l: apply("l"), h: apply("h") };
      }),
    );
    if (!corrected) setCorrected(true);
    for (const c of changes) {
      pending.current.set(`${c.step}:${c.level}:${c.kind}`, [c.step, c.level, c.v, c.kind]);
    }
    flush();
  };

  // Move the selected level; with drag on, set neighbours follow part of the
  // way. Auto neighbours already follow, so they're left on auto.
  const update = (kind: Kind) => (v: number) => {
    const limit = limits[kind];
    const delta = v - resolvedOf[kind][level];
    const changes: Cell[] = [{ step, level, kind, v }];
    if (drag) {
      DRAG.forEach((w, d) => {
        for (const j of [level - d - 1, level + d + 1]) {
          const t = cur[kind][j];
          if (t === undefined || t === null) continue;
          changes.push({ step, level: j, kind, v: Math.max(-limit, Math.min(limit, t + delta * w)) });
        }
      });
    }
    write(changes);
  };

  // Put levels back on auto, remembering what was there so it can be undone.
  const reset = (cells: { step: number; level: number }[]) => {
    const old = cells
      .flatMap((c) => (["v", "l", "h"] as Kind[]).map((kind) => ({ ...c, kind, v: steps[c.step][kind][c.level] })))
      .filter((c) => c.v !== null);
    if (!old.length) return;
    setUndo([...undo, old]);
    write(old.map((c) => ({ ...c, v: null })));
  };

  const undoLast = () => {
    const last = undo[undo.length - 1];
    if (!last) return;
    setUndo(undo.slice(0, -1));
    write(last);
    // Show what came back.
    setStep(last[0].step);
    setLevel(last[0].level);
  };

  const levelCells = (st: number) => levels.map((_, j) => ({ step: st, level: j }));

  const cur = steps[step];
  const resolved = resolve(cur.v, levels);
  const resolvedL = resolve(cur.l, levels);
  const resolvedH = resolve(cur.h, levels);
  const resolvedOf = { v: resolved, l: resolvedL, h: resolvedH };
  const limits = { v: maxTint, l: maxLift, h: maxHue };
  const auto = (kind: Kind) => (cur[kind][level] === null ? "auto: " : "");
  const last = steps.length - 1;

  const nudge = (kind: Kind, d: number) => {
    const limit = limits[kind];
    const now = resolvedOf[kind][level];
    update(kind)(Math.max(-limit, Math.min(limit, Math.round(now) + d)));
  };

  // Next profile in the list; the sliders reload with its values, and undo
  // history (which belongs to the old one) goes.
  const nextProfile = async () => {
    if (switching.current) return;
    switching.current = true;
    try {
      // Everything edited so far goes to the profile it was made in first.
      while (sending.current || pending.current.size) {
        if (!sending.current) await flush();
        else await new Promise((r) => setTimeout(r, 30));
      }
      const name = profiles[(profiles.indexOf(profile) + 1) % profiles.length];
      const s = await setProfile(name);
      profileRef.current = s.profile;
      // Its steps may differ (an import): stay on a step it has, at that
      // step's own brightness.
      const i = Math.min(step, s.steps.length - 1);
      setSteps(s.steps);
      setStep(i);
      setProfileName(s.profile);
      setProfiles(s.profiles);
      setUndo([]);
      setRawBrightness(s.steps[i].b, true);
      wizardStep(i, corrected);
    } finally {
      switching.current = false;
    }
  };

  const actions: { label: string; run: () => void; disabled?: boolean }[] = [
    { label: `Profile: ${profile}`, run: nextProfile },
    { label: "◀ Darker", run: () => step > 0 && setStep(step - 1), disabled: step === 0 },
    { label: `A/B: ${corrected ? "corrected" : "original"}`, run: () => setCorrected(!corrected) },
    { label: "Reset step", run: () => reset(levelCells(step)) },
    { label: "Reset all", run: () => reset(steps.flatMap((_, i) => levelCells(i))) },
    step < last
      ? { label: "Brighter ▶", run: () => setStep(step + 1) }
      : { label: "Save & finish", run: () => Navigation.NavigateBack() },
  ];

  const page = (
    <div
      className="oled-cal"
      style={{
        position: "fixed",
        inset: 0,
        // Above Steam's header (6000) and footer (7000).
        zIndex: 7500,
        background: "#000",
        padding: "10px 24px 4px",
        boxSizing: "border-box",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <Pattern
        selected={corrected ? to8bit(levels[level]) : null}
        levels8={levels.map(to8bit)}
        onSelect={setLevel}
      />

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          margin: "8px 0 0",
          color: ui(55),
          fontSize: "13px",
          flexShrink: 0,
        }}
      >
        <span>
          Step {step + 1}/{steps.length} · brightness {cur.b} · grey {formatLevel(levels[level])}
        </span>
        <span>
          {corrected
            ? `${auto("v")}${formatTint(resolved[level])} · ${auto("h")}${formatHue(resolvedH[level])} · ` +
              `${auto("l")}${formatLift(resolvedL[level])}`
            : "UNCORRECTED (B side)"}
        </span>
      </div>

      <Focusable
        // The page's only focusable element. Steam's own navigation can't move
        // focus around a page portalled outside its tree, so all controller
        // input lands here and the page navigates itself: five rows (tint,
        // red/blue, brightness, grey level, actions), ↑/↓ between rows, ←/→ within.
        preferredFocus
        noFocusRing
        onActivate={() => {}}
        onCancel={() => Navigation.NavigateBack()}
        style={{ display: "flex", flexDirection: "column", gap: "6px", flexShrink: 0, outline: "none" }}
        onButtonDown={(evt) => {
          const b = evt.detail.button;
          const nav = [GamepadButton.DIR_UP, GamepadButton.DIR_DOWN, GamepadButton.DIR_LEFT, GamepadButton.DIR_RIGHT, GamepadButton.OK];
          if (nav.includes(b)) {
            evt.preventDefault();
            evt.stopPropagation();
          }
          // The page takes all input, so B has to be handled here too.
          if (b === GamepadButton.CANCEL) {
            evt.preventDefault();
            evt.stopPropagation();
            Navigation.NavigateBack();
          } else if (b === GamepadButton.DIR_UP) setRow(Math.max(0, row - 1));
          else if (b === GamepadButton.DIR_DOWN) setRow(Math.min(4, row + 1));
          else if (b === GamepadButton.DIR_LEFT || b === GamepadButton.DIR_RIGHT) {
            const d = b === GamepadButton.DIR_RIGHT ? 1 : -1;
            if (row === 0) nudge("v", d);
            else if (row === 1) nudge("h", d);
            else if (row === 2) nudge("l", d);
            else if (row === 3) setLevel(Math.max(0, Math.min(levels.length - 1, level + d)));
            else setCol(Math.max(0, Math.min(actions.length - 1, col + d)));
          } else if (b === GamepadButton.OK && row === 4 && !evt.detail.is_repeat) actions[col].run();
          // Bumpers only step between levels; finishing stays a deliberate press.
          else if (b === GamepadButton.BUMPER_LEFT && step > 0) setStep(step - 1);
          else if (b === GamepadButton.BUMPER_RIGHT && step < last) setStep(step + 1);
          else if (b === GamepadButton.TRIGGER_LEFT && level > 0) setLevel(level - 1);
          else if (b === GamepadButton.TRIGGER_RIGHT && level < levels.length - 1) setLevel(level + 1);
          else if (b === GamepadButton.RSTICK_CLICK) reset([{ step, level }]);
          else if (b === GamepadButton.LSTICK_CLICK) undoLast();
          else if (b === GamepadButton.SECONDARY) setDrag(!drag);
          // Hold Y for a quick look at the uncorrected panel.
          else if (b === GamepadButton.OPTIONS && !evt.detail.is_repeat) setCorrected(false);
        }}
        onButtonUp={(evt) => {
          if (evt.detail.button === GamepadButton.OPTIONS) setCorrected(true);
        }}
      >
        <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
          <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: "6px" }}>
            <div style={{ background: "rgba(255,255,255,0.05)", borderRadius: "6px", padding: "4px 10px" }}>
              <Slider
                ends={<SliderEnds />}
                value={resolved[level]}
                limit={maxTint}
                selected={row === 0}
                onSelect={() => setRow(0)}
                onChange={(v) => update("v")(v)}
              />
              <Slider
                ends={<HueEnds />}
                value={resolvedH[level]}
                limit={maxHue}
                selected={row === 1}
                onSelect={() => setRow(1)}
                onChange={(v) => update("h")(v)}
              />
              <Slider
                ends={<BrightnessEnds />}
                value={resolvedL[level]}
                limit={maxLift}
                selected={row === 2}
                onSelect={() => setRow(2)}
                onChange={(v) => update("l")(v)}
              />
            </div>
            <div style={ROW(levels.length)}>
              {levels.map((p, i) => (
                <Btn
                  key={p}
                  height={30}
                  dim={i !== level}
                  selected={row === 3 && i === level}
                  onClick={() => setLevel(i)}
                >
                  {formatLevel(p)}
                  {cur.v[i] !== null || cur.l[i] !== null || cur.h[i] !== null ? "•" : ""}
                </Btn>
              ))}
            </div>
          </div>
          <Curve steps={steps} step={step} levels={levels} level={level} />
        </div>
        <div style={ROW(actions.length)}>
          {actions.map((a, i) => (
            <Btn key={a.label} height={50} disabled={a.disabled} selected={row === 4 && i === col} onClick={a.run}>
              {a.label}
            </Btn>
          ))}
        </div>
      </Focusable>
      {/* Stands in for Steam's footer, which this page covers. */}
      <div
        style={{
          display: "flex",
          justifyContent: "center",
          gap: "14px",
          marginTop: "4px",
          fontSize: "10px",
          color: ui(38),
          flexShrink: 0,
          whiteSpace: "nowrap",
        }}
      >
        <span>↑↓ row · ←→ adjust · A press</span>
        <span>L1/R1 step</span>
        <span>L2/R2 grey</span>
        <span>X drag {drag ? "on" : "off"}</span>
        <span>hold Y original</span>
        <span>R3 auto</span>
        {undo.length > 0 && <span>L3 undo</span>}
        <span>B done</span>
      </div>
    </div>
  );

  return <div ref={anchor}>{host && createPortal(page, host)}</div>;
}
