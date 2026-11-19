void MAIN() {
    vec3 c = texture(INPUT, INPUT_UV).rgb;
    vec4 e = texture(edgeSampler, INPUT_UV);
    float L = max(dot(c, vec3(0.2126, 0.7152, 0.0722)), 1e-5);
    float q = log2(L) * bands;
    float Lq = exp2((floor(q) + 0.5) / bands);
    vec3 w = c * (Lq / L);
    if (mode > 0.5) { FRAGCOLOR = vec4(e.r, e.g, e.b, 1.0); return; }   // probe: edge, depth, sky
    FRAGCOLOR = vec4(mix(w, vec3(0.02), e.r), 1.0);
}
