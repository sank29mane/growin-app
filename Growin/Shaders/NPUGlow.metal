#include <metal_stdlib>
using namespace metal;

[[ stitchable ]] half4 npuGlow(float2 position, half4 color, float time) {
    // Generate a pulsing effect over time
    float glowPhase = (sin(time * 2.0) * 0.5) + 0.5;

    // Add a pulsing blue/cyan tint simulating NPU processing
    half4 glowColor = half4(0.0, 0.6, 1.0, 0.4 * glowPhase);

    // Mix the original color with the glow color
    return half4(
        color.r + glowColor.r * glowColor.a,
        color.g + glowColor.g * glowColor.a,
        color.b + glowColor.b * glowColor.a,
        color.a
    );
}
