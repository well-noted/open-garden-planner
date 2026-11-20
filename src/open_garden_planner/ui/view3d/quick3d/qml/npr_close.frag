// Line & Wash, pass 3 (NprStyle.qml): the canopy mass, second half - the EROSION.
//
// The dilated depth of pass 2 eroded over the same radius: the farthest of a 3x3 of the
// mass buffer at massShare of the height. Together a closing: a crown's outline is its
// leaves' envelope, its holes filled, its size kept. The busy share is averaged over the
// same reach: a crown's rim leaves take their interior's crowd, a clean outline (a thin line
// through a block) is diluted further.
//
// out: r the closed reciprocal depth x fogFar, g the spread busy share

void MAIN()
{
    vec2 step = vec2(max(1.0, massShare * INPUT_SIZE.y)) / INPUT_SIZE;
    float closed = 1e9;
    float busy = 0.0;
    for (int j = -1; j <= 1; ++j) {
        for (int i = -1; i <= 1; ++i) {
            vec4 m = texture(massSampler, INPUT_UV + vec2(float(i), float(j)) * step);
            closed = min(closed, m.r);
            busy += m.g;
        }
    }
    FRAGCOLOR = vec4(closed, busy / 9.0, 0.0, 1.0);
}
