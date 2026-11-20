float linZ(vec2 uv) {
    float near = CAMERA_PROPERTIES.x; float far = CAMERA_PROPERTIES.y;
    float ndc = 2.0 * texture(DEPTH_TEXTURE, uv).r - 1.0;
    return 2.0 * near * far / (far + near - ndc * (far - near));
}
void MAIN() {
    vec2 px = max(1.0, edgeShare * INPUT_SIZE.y) / INPUT_SIZE;
    float z[9]; int k = 0;
    for (int j = -1; j <= 1; ++j) for (int i = -1; i <= 1; ++i) { z[k] = log(linZ(INPUT_UV + vec2(i, j) * px)); ++k; }
    float gx = (z[2] + 2.0*z[5] + z[8]) - (z[0] + 2.0*z[3] + z[6]);
    float gy = (z[6] + 2.0*z[7] + z[8]) - (z[0] + 2.0*z[1] + z[2]);
    float sd = max(max(abs(z[0]+z[8]-2.0*z[4]), abs(z[2]+z[6]-2.0*z[4])), max(abs(z[3]+z[5]-2.0*z[4]), abs(z[1]+z[7]-2.0*z[4])));
    float eD = smoothstep(depthThresh, depthThresh * 2.0, sd);
    float sky = step(0.9999, texture(DEPTH_TEXTURE, INPUT_UV).r);
    float d = texture(DEPTH_TEXTURE, INPUT_UV).r;
    // channel g carries the raw depth value for the range probe
    FRAGCOLOR = vec4(eD * (1.0 - sky), d, sky, 1.0);
}
