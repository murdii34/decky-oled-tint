#!/bin/bash
# Build an installable "OLED Tint (Lite)" zip: the plugin with a bundled
# profile (the panel opens in Simple view). Install with Decky's
# developer mode -> "Install plugin from ZIP".
#   scripts/package-lite.sh [profile name] [settings file]
set -e
cd "$(dirname "$0")/.."
PROFILE="${1:-Camera}"
SETTINGS="${2:-$HOME/homebrew/settings/decky-oled-tint/settings.json}"
OUT=out/lite
rm -rf "$OUT" && mkdir -p "$OUT/decky-oled-tint/dist"
pnpm build >/dev/null
cp plugin.json package.json main.py LICENSE README.md "$OUT/decky-oled-tint/"
cp dist/index.js "$OUT/decky-oled-tint/dist/"
cp docs/LITE-GUIDE.md "$OUT/LITE-GUIDE.md"   # next to the zip, to send along
python3 -I - "$SETTINGS" "$PROFILE" "$OUT/decky-oled-tint/defaults.json" <<'PY'
import json, sys
src, name, dest = sys.argv[1:]
d = json.load(open(src))
steps = d["profiles"][name]
shared = f"{name} (shared)"
# The panel tables come along but the dither starts off: they're measured on
# this panel, and another one may differ (switch it on under Advanced, Simple view off).
tables = d.get("dither_tables") or {}
json.dump({"enabled": True, "engine": "reshade", "simple": True, "crush": 0, "dither": False,
           "dither_temporal": "pair", "shadow_curve": d.get("shadow_curve", "g22"), "menu_correction": True,
           "dither_tables": tables, "profile": shared, "profiles": {shared: steps}, "steps": steps},
          open(dest, "w"), indent=1)
print(f"bundled profile {name!r} as {shared!r}, {len(tables)} panel tables (dither off)")
PY
python3 -I -c "import shutil; shutil.make_archive('out/oled-tint-lite', 'zip', '$OUT', 'decky-oled-tint')"
cp docs/LITE-GUIDE.md out/
echo "built out/oled-tint-lite.zip ($(du -h out/oled-tint-lite.zip | cut -f1)) and out/LITE-GUIDE.md"
