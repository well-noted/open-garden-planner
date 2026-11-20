// Line & Wash, pass 4 (NprStyle.qml): the wash.
//
// 1. The depth of field folded in (design Q5): while blurShare > 0, a 16-tap disc gather of
//    the scene's linear colour, its radius the share of the picture's height that
//    PhotoBlur's sigma would be, scaled by how far the pixel lies out of the sharp band.
// 2. The exposure and a filmic curve (the environment's tone mapping stands down while the
//    style is on), so the bands below are cut in DISPLAY lightness.
// 3. Tonal bands: the lightness cut into `bands` steps with a soft edge, the chroma kept.
//    Where a step crosses a SMOOTH surface (a lawn's slow change of value, not a leaf's or
//    an object's edge) the pigment pools along it as a wash's edge dries: a rim bandRim
//    darker, bandRimShare of the height wide, measured in pixels with fwidth.
// 4. Pigment pools where the colour changes: darker against a 4-tap cross of its neighbours.
//
// out: rgb the wash, display-linear 0..1 (before the paper); a its band lightness 0..1

float viewDepth(vec2 uv)
{
    float near = CAMERA_PROPERTIES.x;
    float far = CAMERA_PROPERTIES.y;
    float ndc = 2.0 * texture(DEPTH_TEXTURE, uv).r - 1.0;
    return 2.0 * near * far / (far + near - ndc * (far - near));
}

vec3 filmic(vec3 x)
{
    // the ACES fit (Narkowicz 2015): close to Qt's filmic in the mid tones
    x = max(x, vec3(0.0));
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}

vec3 scene(vec2 uv)
{
    return filmic(texture(sceneSampler, uv).rgb * exposure);
}

float luma(vec3 c)
{
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}

void MAIN()
{
    vec3 c;
    if (blurShare > 0.0) {
        float offset = viewDepth(INPUT_UV) - focusDistance;
        float amount = clamp((abs(offset) - 0.5 * focusRange) / max(focusRange, 1.0), 0.0, 1.0);
        vec2 radius = vec2(2.0 * blurShare * amount * OUTPUT_SIZE.y) / INPUT_SIZE;
        vec3 sum = texture(sceneSampler, INPUT_UV).rgb;
        for (int k = 0; k < 16; ++k) {
            float a = float(k) * 2.39996323;          // the golden angle: an even disc
            float r = sqrt((float(k) + 0.5) / 16.0);
            sum += texture(sceneSampler, INPUT_UV + vec2(cos(a), sin(a)) * r * radius).rgb;
        }
        c = filmic(sum / 17.0 * exposure);
    } else {
        c = scene(INPUT_UV);
    }
    // tonal bands in display lightness (gamma 2.2 stands in for the sRGB curve)
    float L = max(luma(c), 1e-5);
    float lightness = pow(L, 1.0 / 2.2);
    float q = lightness * bands;
    float stepped = (floor(q) + smoothstep(0.5 - bandSoft, 0.5 + bandSoft, fract(q))) / bands;
    float Lq = pow(clamp(stepped, 0.0, 1.0), 2.2);
    vec3 w = c * (Lq / L);
    // the dried rim: how many pixels this one lies from the step (q crosses n + 0.5), only
    // where q changes slowly across the pixel (fwidth: uniform control flow, before any branch)
    float dq = fwidth(q);
    float fromStep = abs(fract(q) - 0.5) / max(dq, 1e-5);
    float rim = (1.0 - smoothstep(0.0, max(1.0, bandRimShare * OUTPUT_SIZE.y), fromStep))
                * (1.0 - smoothstep(0.02, 0.12, dq));
    w *= 1.0 - bandRim * rim;
    w = mix(vec3(luma(w)), w, washSaturation);
    // pigment pools where the colour changes
    vec2 px = vec2(max(1.0, 2.0 * edgeShare * OUTPUT_SIZE.y)) / INPUT_SIZE;
    vec3 around = 0.25 * (scene(INPUT_UV + vec2(px.x, 0.0)) + scene(INPUT_UV - vec2(px.x, 0.0))
                          + scene(INPUT_UV + vec2(0.0, px.y)) + scene(INPUT_UV - vec2(0.0, px.y)));
    w *= 1.0 - edgeDarken * clamp(length(c - around) * 4.0, 0.0, 1.0);
    FRAGCOLOR = vec4(clamp(w, 0.0, 1.0), stepped);
}
