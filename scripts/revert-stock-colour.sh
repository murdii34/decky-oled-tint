#!/bin/sh
# Emergency revert: turn OLED Tint off, remove everything it set in gamescope
# (looks and the ReShade effect) and give Steam's colour vibrance back if the
# plugin was holding it. Safe to run any time, as the normal user (no sudo).
SETTINGS="$HOME/homebrew/settings/decky-oled-tint/settings.json"
SAVED=""
if [ -f "$SETTINGS" ]; then
    SAVED=$(python3 -I -c 'import json,sys
p = sys.argv[1]; d = json.load(open(p)); d["enabled"] = False
v = d.pop("vibrance_saved", None)
json.dump(d, open(p, "w"), indent=2)
print(v or "")' "$SETTINGS")
fi
for atom in GAMESCOPE_COLOR_LOOK_G22 GAMESCOPE_COLOR_LOOK_PQ GAMESCOPE_RESHADE_EFFECT; do
    DISPLAY=:0 xprop -root -remove "$atom"
done
if [ -n "$SAVED" ]; then
    DISPLAY=:0 xprop -root -f GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS 32c -set GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS "$SAVED"
fi
echo "Stock colour restored. Re-enable from the OLED Tint panel."
