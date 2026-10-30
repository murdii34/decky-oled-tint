import { Focusable, GamepadButton, Navigation } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Swatch, grey, ui } from "./Calibrate";
import { State, getState, setDither, setDitherTemporal, setEnabled, setRawBrightness } from "./backend";

export const PATTERN_ROUTE = "/oled-tint/pattern";

// Brightness presets: the start, peak and end of this panel's green band.
export const PRESETS = [96, 177, 310];

const RAMP = [4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192];

// The dark ramp: one exact code per bar (a CSS gradient may be dithered by the
// browser), across the dither's hand-over at 64.
const DARK_TOP = 96;
const DARK_MARKS = [16, 32, 48, 64, 80];

// Full-screen grey swatches for comparing panels (e.g. phone photos of two
// Portals): the brightness and correction state are in the picture itself.
export function TestPattern() {
  const [state, setState] = useState<State | null>(null);
  const [preset, setPreset] = useState(-1);
  const anchor = useRef<HTMLDivElement>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);

  useEffect(() => setHost(anchor.current?.ownerDocument.body ?? null), []);
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

  const go = (i: number) => {
    const p = Math.max(0, Math.min(PRESETS.length - 1, i));
    setPreset(p);
    setRawBrightness(PRESETS[p]);
  };

  const page = (
    <Focusable
      preferredFocus
      noFocusRing
      onActivate={() => {}}
      onCancel={() => Navigation.NavigateBack()}
      onButtonDown={(evt) => {
        const b = evt.detail.button;
        if (b === GamepadButton.CANCEL) {
          evt.preventDefault();
          Navigation.NavigateBack();
        } else if (b === GamepadButton.BUMPER_LEFT) go(preset < 0 ? 0 : preset - 1);
        else if (b === GamepadButton.BUMPER_RIGHT) go(preset + 1);
        else if (b === GamepadButton.OPTIONS && state && !evt.detail.is_repeat)
          setEnabled(!state.enabled).then(setState);
        else if (b === GamepadButton.SECONDARY && state?.dither_ready && !evt.detail.is_repeat)
          setDither(!state.dither).then(setState);
        else if (b === GamepadButton.TRIGGER_RIGHT && state?.dither && !evt.detail.is_repeat)
          // off -> pair -> golden -> off, to compare
          setDitherTemporal(!state.dither_temporal ? "pair" : state.dither_temporal === "pair" ? "golden" : false).then(
            setState,
          );
      }}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 7500,
        background: "#000",
        padding: "16px 24px",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        gap: "12px",
        outline: "none",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", color: ui(60), fontSize: "15px" }}>
        <span>
          Brightness {state?.brightness ?? "?"} ·{" "}
          {/* What's on screen (the backend's status), not just the switch: a
              photo's label must not say ON for a paused or idle correction. */}
          <b style={{ color: ui(85) }}>{state?.status?.text ?? (state?.enabled ? "Correction on" : "Correction off")}</b>
          {state?.enabled ? ` · ${state.profile}` : ""}
          {state?.dither_ready ? (
            <>
              {" "}· Dither{" "}
              <b style={{ color: ui(85) }}>
                {!state.dither
                  ? "OFF"
                  : !state.enabled
                    ? "(needs correction)"
                    : state.dither_temporal
                      ? `ON, over time (${state.dither_temporal})`
                      : "ON"}
              </b>
            </>
          ) : null}
        </span>
        <span style={{ color: ui(35) }}>
          L1/R1 brightness {PRESETS.join("/")} · Y correction{state?.dither_ready ? " · X dither" : ""}
          {state?.dither ? " · R2 over time: off/pair/golden" : ""} · B back
        </span>
      </div>
      <div style={{ flex: 1, minHeight: 0, display: "flex", gap: "8px" }}>
        <Swatch color="#000" label="0" outline />
        {RAMP.map((v) => (
          <Swatch key={v} color={grey(v)} label={`${v}`} />
        ))}
      </div>
      <div style={{ position: "relative", height: "64px", display: "flex" }}>
        {Array.from({ length: DARK_TOP + 1 }, (_, v) => (
          <div key={v} style={{ flex: 1, background: grey(v) }} />
        ))}
        {DARK_MARKS.map((v) => (
          <div
            key={v}
            style={{
              position: "absolute",
              left: `${((v + 0.5) / (DARK_TOP + 1)) * 100}%`,
              bottom: "-14px",
              transform: "translateX(-50%)",
              color: ui(40),
              fontSize: "10px",
            }}
          >
            {v}
          </div>
        ))}
      </div>
      <div style={{ height: "28px", marginTop: "10px", borderRadius: "4px", background: "linear-gradient(to right, #000, #fff)" }} />
    </Focusable>
  );

  return <div ref={anchor}>{host && createPortal(page, host)}</div>;
}
