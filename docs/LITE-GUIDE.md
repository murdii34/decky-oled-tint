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

Below about 20% brightness the panel itself goes green. OLED Tint keeps the
real backlight just above that range and darkens the picture instead
(**Floor dimming**, on by default): the brightness slider works as usual, the
screen stays neutral. Below the very bottom of the slider the panel's own
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

## Photos for comparison

Phone cameras "fix" colours on their own, which hides exactly what we want
to see. If you can:

- Dark room, phone on something steady, same distance each time.
- Lock exposure and white balance (iPhone: a manual camera app or ProRAW).
- One photo with correction **on**, one **off**, without moving anything.
  The brightness and on/off state are printed at the top of the pattern.

The most useful comparison is on/off on the same device.

## Going further (optional)

**Dither correction** (under **Advanced**, experimental) rebuilds the darkest greys
from the panel's real levels and fixes the last green glow. Its tables were
measured on another Portal, so on yours it may come out a little too bright or
too dark near black; switch it off if anything looks off. Under **Shadows**,
the dither pattern **Pair** gives the smoothest grain in games.

**Compare** switches the correction off for 5 seconds, for a quick before and
after. **Off for this game** appears while a game runs.

**Import** (under **Advanced**) loads an `oled-tint-calibration.json` from
Downloads, for when you're sent a newer one.

## Comparison photos with a phone

A phone evens out brightness on its own, so every shot looks the same. Lock
it:

1. Dark room, no flash. Test pattern (sidebar -> Test pattern), brightness
   about 20%.
2. Lock the exposure. iPhone: long-press the screen in the camera app until
   "AE/AF LOCK" shows. Android: Pro mode, ISO ~400, shutter ~1/30 s, white
   balance fixed (Daylight / 5500 K), manual focus.
3. HDR and night mode off.
4. One photo with the correction off, one with it on: same spot (rest the
   phone on something), same locked settings. Optionally again at 10% and
   40%.

What matters is that both photos use identical settings, not their quality.

## Turning it off

The switch in the panel, or uninstall the plugin in Decky's settings. It
removes its effect when it's unloaded.
