// All five passes of VHS.fx (IDDQD / Matsilagi, RSRetroArch) as one technique:
// gamescope runs a single technique per effect. The original noise texture
// isn't published; RSRetroArch's film noise stands in for it.
#define sNoiseMode_TextureName "film_noise1.png"
#include "VHS.fx"

technique OledVHS
{
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS; }    // NTSC colours
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS3; }   // tape noise
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS4; }   // VCR distortion
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS5; }   // VHS distortion
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS6; }   // dirty CRT
}
