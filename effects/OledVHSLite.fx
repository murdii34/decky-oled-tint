// VHS without its NTSC codec pass, by far the most expensive (21-tap loop
// with trigonometry per pixel). Tape noise, distortions and the dirty CRT
// look stay.
#define sNoiseMode_TextureName "film_noise1.png"
#include "VHS.fx"

technique OledVHSLite
{
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS3; }   // tape noise
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS4; }   // VCR distortion
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS5; }   // VHS distortion
    pass { VertexShader = PostProcessVS; PixelShader = PS_VHS6; }   // dirty CRT
}
