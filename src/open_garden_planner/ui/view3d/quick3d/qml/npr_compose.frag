// Line & Wash, pass 5 (NprStyle.qml): wash on paper, ink on top.
//
// The wash is pigment on paper: thinned towards the paper (washLift), laid unevenly (its
// density blooms by washMottle over washMottleShare of the height) and multiplied onto it,
// with a paper grain and a granulation - clumped specks where the pigment settled in the
// paper's hollows - that both settle where the pigment is dense. The distance fades to paper
// (paper fog), the sky IS the paper.
//
// Where the picture is foliage the wash itself is averaged over a small ring
// (foliageSoftenShare): a crown reads as a few soft washes, not as leaf-level noise.
//
// The ink is laid on last, in two hands. Where the picture is calm (the closed buffer's
// busy share under busyCut) the fine lines of pass 1: an outline widened to its pen
// (silhouetteShare, a ring of eight taps), a crease or step a pixel's stroke. Where it is
// busy (foliage) the leaves draw nothing of their own and the CLOSED depth's outline is
// drawn instead: one line round the crown's mass, from the second difference of the closed
// reciprocal depth, on its near side, so it lies on the crown's edge, not in the sky; and
// only where what lies beyond it is not foliage too, so a crown's inner layers draw nothing.
//
// The environment's tone mapping is None at exposure 1 while the style is on, so this pass
// writes the picture itself: sRGB-encoded values. Grain, granulation and blooms are
// anchored to the screen, as paper under a camera (design 3.4); deterministic hashes.

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
    vec4 m = texture(closeSampler, INPUT_UV);
    float sky = step(0.999, e.b);
    // ---- the ink ----
    // calm: the fine lines; busy: the crown's mass outline
    float leafy = smoothstep(0.5 * busyCut, busyCut, m.g);
    float calm = 1.0 - leafy;
    // foliage as a few soft washes, not leaf-level colour noise: the wash averaged over a
    // ring (foliageSoftenShare) where the picture is leafy
    vec2 soft = vec2(max(1.0, foliageSoftenShare * OUTPUT_SIZE.y)) / INPUT_SIZE;
    vec4 pooled = wash;
    for (int k = 0; k < 6; ++k) {
        float a = float(k) * 1.04719755 + 0.3;
        pooled += texture(washSampler, INPUT_UV + vec2(cos(a), sin(a)) * soft);
    }
    wash = mix(wash, pooled / 7.0, leafy);
    // the pen's width: a crease takes ink from its 3x3 at one pixel (inkSpread of it), an
    // outline from a ring at its pen's radius
    vec2 one = 1.0 / INPUT_SIZE;
    float around = 0.0;
    for (int j = -1; j <= 1; ++j) {
        for (int i = -1; i <= 1; ++i) {
            vec4 n = texture(edgeSampler, INPUT_UV + vec2(float(i), float(j)) * one);
            around = max(around, max(n.r, n.g * creaseInk));
        }
    }
    vec2 pen = vec2(max(1.0, silhouetteShare * OUTPUT_SIZE.y)) / INPUT_SIZE;
    float ring = 0.0;
    for (int k = 0; k < 8; ++k) {
        float a = float(k) * 0.78539816;
        ring = max(ring, texture(edgeSampler, INPUT_UV + vec2(cos(a), sin(a)) * pen).r);
    }
    float fine = max(max(e.r, e.g * creaseInk), inkSpread * max(around, ring));
    // the mass outline: the closed reciprocal depth's relative second difference
    vec2 reach = vec2(max(1.0, 0.35 * massShare * OUTPUT_SIZE.y)) / INPUT_SIZE;
    float w0 = max(m.r, 1e-4);
    float strongest = 0.0;
    float nearSide = 0.0;
    vec2 away = vec2(0.0);   // the direction of the farther side of the strongest jump
    vec2 dirs[4] = vec2[4](vec2(1.0, 0.0), vec2(0.0, 1.0), vec2(0.7071, 0.7071), vec2(0.7071, -0.7071));
    for (int k = 0; k < 4; ++k) {
        vec2 o = dirs[k] * reach;
        float wa = texture(closeSampler, INPUT_UV + o).r;
        float wb = texture(closeSampler, INPUT_UV - o).r;
        float sd = (wa + wb - 2.0 * w0) / w0;
        if (abs(sd) > strongest) {
            strongest = abs(sd);
            nearSide = sd < 0.0 ? 1.0 : 0.0;
            away = wa < wb ? dirs[k] : -dirs[k];
        }
    }
    // only the crown's OUTLINE: what lies beyond the jump (past the dilation's reach) must
    // not be foliage itself - a back layer of leaves seen between near ones draws nothing
    vec2 beyond = away * vec2(1.5 * massShare * OUTPUT_SIZE.y) / INPUT_SIZE;
    float open = 1.0 - smoothstep(0.5 * busyCut, busyCut,
                                  texture(massSampler, INPUT_UV + beyond).g);
    float mass = massInk * smoothstep(massThresh, 3.0 * massThresh, strongest) * nearSide
                 * open * step(1e-3, m.r);
    float ink = max(fine * calm, mass * leafy);

    // ---- the wash on paper ----
    vec3 paper = toLinear(paperColor);
    // the wash laid unevenly: two octaves of blooms move its lift towards the paper
    vec2 sheet = INPUT_UV * OUTPUT_SIZE / max(1.0, washMottleShare * OUTPUT_SIZE.y);
    float bloom = 0.65 * grain(sheet + 3.0) + 0.35 * grain(2.7 * sheet + 11.0) - 0.5;
    float lift = clamp(washLift + washMottle * bloom, 0.0, 1.0);
    vec3 pigment = mix(wash.rgb, vec3(1.0), lift);
    float g = grain(INPUT_UV * OUTPUT_SIZE / max(1.0, grainShare * OUTPUT_SIZE.y));
    float g2 = grain(INPUT_UV * OUTPUT_SIZE / max(1.0, 7.0 * grainShare * OUTPUT_SIZE.y) + 17.0);
    // granulation: specks where a two-octave noise passes a threshold, clumped, not even
    vec2 cell = INPUT_UV * OUTPUT_SIZE / max(1.0, granulationShare * OUTPUT_SIZE.y);
    float gr = 0.6 * grain(cell + 29.0) + 0.4 * grain(2.3 * cell + 41.0);
    float specks = smoothstep(0.6, 0.85, gr);
    float density = 1.0 - wash.a;                       // dense pigment where the band is dark
    vec3 c = paper * pigment * (1.0 - grainStrength * density * (0.6 * g + 0.4 * g2))
             * (1.0 - granulation * specks * (0.35 + 0.65 * density));
    // the distance fades to the paper, the sky is the paper: the wash ends in a ragged
    // edge (horizontal streaks of a smooth two-octave noise shift it), as a brush runs
    // dry, not as a haze
    vec2 stroke = INPUT_UV * OUTPUT_SIZE / OUTPUT_SIZE.y;
    float streak = 0.65 * grain(stroke * vec2(12.0, 60.0)) + 0.35 * grain(stroke * vec2(31.0, 150.0) + 5.0);
    float fogRaw = smoothstep(fogNear / fogFar, 1.0, e.b);
    float fogT = smoothstep(0.3, 0.7, fogRaw + 0.3 * (streak - 0.5));
    c = mix(c, paper * (1.0 - 0.3 * grainStrength * g), max(fogT, sky));
    // the pen lifts with the distance: far edges are paper, as the ground there is
    c = mix(c, toLinear(inkColor), clamp(ink, 0.0, 1.0) * (1.0 - sky) * (1.0 - fogT));
    FRAGCOLOR = vec4(toSrgb(c), 1.0);
}
