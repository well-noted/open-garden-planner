// Line & Wash, pass 1 (NprStyle.qml): where the ink goes.
//
// The ink comes from the depth alone. On a plane the reciprocal of the view depth is linear
// in screen space, so its SECOND difference is zero on every plane, whatever its slope: the
// sloped ground and a flat wall draw no ink (the spike's Sobel on log depth fired on 65 % of
// a plain ground plane). Where two surfaces meet at a fold the second difference is small
// (a crease); where one surface ends in front of another it is large (a silhouette). Both are
// relative to the centre's reciprocal depth, so a near and a far edge weigh alike.
//
// out: r silhouette ink, g crease ink, b view depth / fogFar, a sky (no geometry)

float viewDepth(vec2 uv)
{
    float near = CAMERA_PROPERTIES.x;
    float far = CAMERA_PROPERTIES.y;
    float ndc = 2.0 * texture(DEPTH_TEXTURE, uv).r - 1.0;
    return 2.0 * near * far / (far + near - ndc * (far - near));
}

void MAIN()
{
    vec2 px = vec2(max(1.0, edgeShare * OUTPUT_SIZE.y)) / INPUT_SIZE;
    float raw = texture(DEPTH_TEXTURE, INPUT_UV).r;
    float sky = step(0.99999, raw);
    float z0 = viewDepth(INPUT_UV);
    float w0 = 1.0 / z0;
    float strongest = 0.0;   // the largest relative second difference of the four directions
    float nearSide = 0.0;    // and whether the centre lies on the NEARER side of it
    vec2 dirs[4] = vec2[4](vec2(1.0, 0.0), vec2(0.0, 1.0), vec2(1.0, 1.0), vec2(1.0, -1.0));
    for (int k = 0; k < 4; ++k) {
        vec2 o = dirs[k] * px;
        float wa = 1.0 / viewDepth(INPUT_UV + o);
        float wb = 1.0 / viewDepth(INPUT_UV - o);
        float sd = (wa + wb - 2.0 * w0) / w0;
        if (abs(sd) > strongest) {
            strongest = abs(sd);
            // negative: the neighbours are farther on average, the centre is the near rim
            nearSide = sd < 0.0 ? 1.0 : 0.0;
        }
    }
    float silhouette = smoothstep(silhouetteThresh, 2.0 * silhouetteThresh, strongest) * nearSide;
    float crease = smoothstep(creaseThresh, 2.0 * creaseThresh, strongest)
                   * (1.0 - smoothstep(silhouetteThresh, 2.0 * silhouetteThresh, strongest));
    float keep = 1.0 - sky;
    FRAGCOLOR = vec4(silhouette * keep, crease * keep, clamp(z0 / fogFar, 0.0, 1.0), sky);
}
