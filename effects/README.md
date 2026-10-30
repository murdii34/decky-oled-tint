# Per-game effects

Wrappers for third-party ReShade shaders, used through the plugin's
`app_effects` setting (`{appid: {"effect": file, "technique": index}}`).
gamescope runs one technique per effect, so each wrapper chains the passes it
wants into a single technique (index 5 here: after the five in VHS.fx).

Install into `~/.local/share/gamescope/reshade/`:

| File | From |
|---|---|
| `Shaders/VHS.fx` | https://github.com/Matsilagi/RSRetroArch (`Shaders/VHS.fx`, IDDQD/Matsilagi; credit the original authors) |
| `Shaders/ReShade.fxh` | https://github.com/crosire/reshade-shaders (slim branch) |
| `Textures/film_noise1.png` | RSRetroArch `Textures/` (stands in for the unpublished VHS_N1.jpg) |
| `Shaders/OledVHS.fx`, `Shaders/OledVHSLite.fx` | this folder |

OledVHSLite drops the NTSC codec pass (a 21-tap loop with trigonometry per
pixel): on the Portal that's the difference between 30 and 60 fps in
Silver Pines (appid 2333000).
