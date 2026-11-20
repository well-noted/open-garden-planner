// Line & Wash, pass 3 (NprStyle.qml): wash on paper, ink on top.
//
// The wash is pigment on paper: thinned towards the paper (washLift) and multiplied onto it,
// with a paper grain that settles where the pigment is dense. The distance fades to paper
// (paper fog), the sky IS the paper. The ink of pass 1 is laid on last, faded where edges
// crowd (a 3x3 count: foliage would otherwise turn into black noise).
//
// The environment's tone mapping is None at exposure 1 while the style is on, so this pass
// writes the picture itself: sRGB-encoded values.

float hash(vec2 p)
{
    // a fixed integer-free hash: the same paper on every frame and every machine
    p = fract(p * vec2(0.1031, 0.1030));
    p += dot(p, p.yx + 33.33);
    return fract((p.x + p.y) * p.x);
}

float grain(vec2 cell)
{
    vec2 i = floor(cell);
    vec2 f = fract(cell);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

vec3 toLinear(vec3 s)
{
    return pow(s, vec3(2.2));
}

vec3 toSrgb(vec3 l)
{
    return pow(clamp(l, 0.0, 1.0), vec3(1.0 / 2.2));
}

void MAIN()
{
    vec4 wash = texture(washSampler, INPUT_UV);
    vec4 e = texture(edgeSampler, INPUT_UV);
    // crowding: how many of the 3x3 neighbours carry ink
    vec2 px = vec2(max(1.0, edgeShare * OUTPUT_SIZE.y)) / INPUT_SIZE;
    // and the pen's width: the line is the strongest ink within a pixel's reach, so a
    // one-pixel edge reads as a drawn stroke (inkSpread of the neighbours' ink)
    vec2 one = 1.0 / INPUT_SIZE;
    float crowd = 0.0;
    float around = 0.0;
    for (int j = -1; j <= 1; ++j) {
        for (int i = -1; i <= 1; ++i) {
            vec4 n = texture(edgeSampler, INPUT_UV + vec2(float(i), float(j)) * px);
            crowd += step(0.5, max(n.r, n.g));
            vec4 m = texture(edgeSampler, INPUT_UV + vec2(float(i), float(j)) * one);
            around = max(around, max(m.r, m.g * creaseInk));
        }
    }
    float busy = crowd / 9.0;
    float ink = max(max(e.r, e.g * creaseInk), inkSpread * around)
                * (1.0 - smoothstep(busyCut, busyCut + 0.3, busy));

    vec3 paper = toLinear(paperColor);
    vec3 pigment = mix(wash.rgb, vec3(1.0), washLift);
    float g = grain(INPUT_UV * OUTPUT_SIZE / max(1.0, grainShare * OUTPUT_SIZE.y));
    float g2 = grain(INPUT_UV * OUTPUT_SIZE / max(1.0, 7.0 * grainShare * OUTPUT_SIZE.y) + 17.0);
    float density = 1.0 - wash.a;                       // dense pigment where the band is dark
    vec3 c = paper * pigment * (1.0 - grainStrength * density * (0.6 * g + 0.4 * g2));
    // the distance fades to the paper, the sky is the paper: the wash ends in a ragged
    // edge (horizontal streaks of a smooth two-octave noise shift it), as a brush runs
    // dry, not as a haze
    vec2 stroke = INPUT_UV * OUTPUT_SIZE / OUTPUT_SIZE.y;
    float streak = 0.65 * grain(stroke * vec2(12.0, 60.0)) + 0.35 * grain(stroke * vec2(31.0, 150.0) + 5.0);
    float fogRaw = smoothstep(fogNear / fogFar, 1.0, e.b);
    float fogT = smoothstep(0.3, 0.7, fogRaw + 0.3 * (streak - 0.5));
    c = mix(c, paper * (1.0 - 0.3 * grainStrength * g), max(fogT, e.a));
    // the pen lifts with the distance: far edges are paper, as the ground there is
    c = mix(c, toLinear(inkColor), ink * (1.0 - e.a) * (1.0 - fogT));
    FRAGCOLOR = vec4(toSrgb(c), 1.0);
}
