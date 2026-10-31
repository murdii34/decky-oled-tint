# OLED Tint Lite: quick guide

Fixes the green tint and glowing blacks the Odin 2 Portal's screen shows at
low brightness. This package carries a calibration measured on another
Portal with a camera. Panels vary a little, so tell us how it looks on yours.

## Needs

- Odin 2 Portal on **Armada OS** with **Decky Loader** installed.

## Install

1. Copy `oled-tint-lite.zip` onto the Portal (e.g. into Downloads).
2. Quick Access → Decky (plug icon) → gear icon → **enable Developer mode**.
3. In Decky's settings → **Developer** → **Install plugin from ZIP** → pick
   the zip.
4. Quick Access → Decky → **OLED Tint**.

## How it dims

Across the lower part of the brightness slider (roughly 6-38%, worst around
20%) the panel itself goes green. OLED Tint keeps the real backlight just above
that range and darkens the picture instead (**Floor dimming**, on by default):
the brightness slider works as usual, the screen stays neutral. Below the very bottom of the slider the panel's own
backlight takes over again. The method can be switched under **Advanced**
(**Dither correction**, experimental).

## Compare

1. In OLED Tint, press **Test pattern**.
2. Press **R1** to step brightness to **177** (the worst point for these
   panels). 96 and 310 are the other presets.
3. Press **Y** to switch the correction on and off and watch the dark
   swatches (4 to 48).
4. **B** goes back.

What you should see: with correction **off**, the dark greys look green and
too bright; with it **on**, much more neutral. The very darkest swatches
(4 to 12) may stay slightly off: below that point the panel can't make
small amounts of light.

## Comparison photos

1. Dark room. Rest the phone on something steady.
2. Phone camera: HDR off, night mode off, flash off.
3. Lock exposure and white balance.
   - iPhone: long-press the screen until "AE/AF LOCK" shows.
   - Android: Pro mode, ISO 400, 1/30 s, white balance 5500 K, manual focus.
4. OLED Tint → **Test pattern**.
5. **R1** until the top reads **177**.
6. **Y** until the top shows correction off. Photo.
7. **Y** until it shows on. Photo. Don't move the phone in between.
8. Optional: repeat 5-7 at **96** and **310**.
9. **B** exits. Send both photos.

## Going further (optional)

The panel opens in **Simple view**: Correction, brightness, Test pattern,
Info overlay. Switch **Simple view** off for everything else.

**Dither correction** (under **Advanced**, experimental) rebuilds the darkest greys
from the panel's real levels and fixes the last green glow. Its tables were
measured on another Portal, so on yours it may come out a little too bright or
too dark near black; switch it off if anything looks off.

**Compare** switches the correction off for 5 seconds, for a quick before and
after. **Off for this game** appears while a game runs.

**Import** (under **Advanced**) loads an `oled-tint-calibration.json` from
Downloads, for when you're sent a newer one.

## Turning it off

The switch in the panel, or uninstall the plugin in Decky's settings. It
removes its effect when it's unloaded.
