// Line & Wash, pass 2 (NprStyle.qml): the canopy mass, first half - a DILATION.
//
// The buffer is half the picture each way. Each of its pixels looks at a 5x5 grid less its
// corners (a disc) of the full picture's depth, massShare of the height round it, and keeps
// the NEAREST (the largest reciprocal depth): every gap between leaves smaller than the
// square fills with the leaves round it. npr_close.frag erodes it back, so a crown keeps
// its size and only loses its holes (a morphological closing of the depth).
//
// It also counts the block's busy share (pass 1's alpha: leaf-level depth jumps): a calm
// wall or roof edge is a thin line through the block (well under a third of it), a crown a
// crowd.
//
// out: r the nearest reciprocal depth x fogFar (1 at the fog's far end, 0 the sky),
//      g the busy share of the block

float viewDepth(vec2 uv)
{
    float near = CAMERA_PROPERTIES.x;
    float far = CAMERA_PROPERTIES.y;
    float ndc = 2.0 * texture(DEPTH_TEXTURE, uv).r - 1.0;
    return 2.0 * near * far / (far + near - ndc * (far - near));
}

void MAIN()
{
    // the square's step in the FULL picture's uv (INPUT_SIZE: the scene's size)
    vec2 step = vec2(max(1.0, 0.5 * massShare * INPUT_SIZE.y)) / INPUT_SIZE;
    float nearest = 0.0;
    float busy = 0.0;
    for (int j = -2; j <= 2; ++j) {
        for (int i = -2; i <= 2; ++i) {
            if (abs(i) + abs(j) == 4)
                continue;   // the corners: a disc, so a crown's closed outline has no square's corners
            vec2 uv = INPUT_UV + vec2(float(i), float(j)) * step;
            float raw = texture(DEPTH_TEXTURE, uv).r;
            float w = raw >= 0.99999 ? 0.0 : fogFar / viewDepth(uv);
            nearest = max(nearest, w);
            busy += texture(edgeSampler, uv).a;
        }
    }
    FRAGCOLOR = vec4(nearest, busy / 21.0, 0.0, 1.0);
}
