// Render styles WP9: the 3D "Line & Wash" look (NprSpec, core/scene3d/look.py) as ONE
// multi-pass Effect, so it takes one slot of the environment's effect chain (PF-E3D-21:
// with glow on, two user effects ahead of Qt's scene effect break the chain; the NPR look
// has glow and SSR off and folds the depth of field in instead of chaining PhotoBlur).
//
//  pass 1 npr_edges.frag   -> edgeBuffer (RGBA8): ink from the SECOND difference of the
//                             depth's reciprocal (zero on every plane, so sloped ground
//                             draws no ink; docs/design/npr-spike/FINDINGS.md), split into
//                             silhouettes (r) and creases (g); b the view depth as a share
//                             of the paper fog's far distance; a the sky
//  pass 2 npr_wash.frag    -> washBuffer (RGBA16F): the optional depth of field, then the
//                             exposure and a filmic curve, the luminance cut into tonal bands
//                             (chroma kept) and pigment pooled where the colour changes
//  pass 3 npr_compose.frag -> the screen: wash over paper, paper fog, sky as paper, grain,
//                             ink - suppressed where edges crowd (foliage)
//
// It runs where every user effect runs (ogp-3d-renderer section 22): on the LINEAR scene
// colour before Qt's exposure and tone mapping. While it is on, Garden.qml sets the
// environment's tone mapping to None at exposure 1, so what pass 3 writes is the picture
// (sRGB-encoded here), and the paper and ink are exactly the table's colours.
//
// Every width and frequency is a share of the picture's height, so the pane, a 4K capture
// and a CI frame look alike. Deterministic: the grain is a hash of the pixel, no time,
// no random source.
//
// No user-visible strings here (tests/unit/test_quick3d_qml.py).
import QtQuick
import QtQuick3D

Effect {
    id: nprStyle

    // ---- NprSpec (core/scene3d/look.py), by quick3d/look.py npr_properties ----
    property real edgeShare: 0.0015       // ink step as a share of the picture's height
    property real silhouetteThresh: 0.03  // relative 2nd difference of 1/depth: silhouettes
    property real creaseThresh: 0.006     // ... and creases (folds, wall corners)
    property real creaseInk: 0.55         // a crease's ink against a silhouette's 1
    property real inkSpread: 0.85         // the pen's width: ink taken from the strongest neighbour
    property real busyCut: 0.35           // share of crowded neighbours where ink fades out
    property real bands: 4.0              // tonal bands from paper to ink
    property real bandSoft: 0.06          // the soft step between two bands, 0..0.5
    property real edgeDarken: 0.35        // pigment pooled where the wash's colour changes
    property real washLift: 0.15          // the wash thinned towards the paper
    property real washSaturation: 1.0    // chroma of the wash
    property real grainShare: 0.0016      // paper grain cell as a share of the height
    property real grainStrength: 0.12     // the grain's darkening at full pigment
    property vector3d paperColor: Qt.vector3d(0.9568627451, 0.9333333333, 0.8745098039)   // sRGB
    property vector3d inkColor: Qt.vector3d(0.1647058824, 0.1411764706, 0.1137254902)   // sRGB (#2a241d)
    property real fogNear: 6000.0         // cm: the paper fog starts ...
    property real fogFar: 14000.0         // ... and the far ground is paper here
    property real exposure: 1.0           // the look's exposure x the photo's
    // ---- the depth of field folded in (Q5): Garden.qml's photo properties ----
    property real focusDistance: 600.0
    property real focusRange: 100.0
    property real blurShare: 0.0

    readonly property TextureInput sceneSampler: TextureInput { texture: Texture {} }
    readonly property TextureInput edgeSampler: TextureInput { texture: Texture {} }
    readonly property TextureInput washSampler: TextureInput { texture: Texture {} }

    Shader {
        id: edgesShader
        stage: Shader.Fragment
        shader: Qt.resolvedUrl("npr_edges.frag")
    }
    Shader {
        id: washShader
        stage: Shader.Fragment
        shader: Qt.resolvedUrl("npr_wash.frag")
    }
    Shader {
        id: composeShader
        stage: Shader.Fragment
        shader: Qt.resolvedUrl("npr_compose.frag")
    }

    Buffer {
        id: edgeBuffer
        name: "nprEdges"
        format: Buffer.RGBA8
        textureFilterOperation: Buffer.Nearest
        textureCoordOperation: Buffer.ClampToEdge
        bufferFlags: Buffer.None
        sizeMultiplier: 1.0
    }
    Buffer {
        id: washBuffer
        name: "nprWash"
        format: Buffer.RGBA16F
        textureFilterOperation: Buffer.Linear
        textureCoordOperation: Buffer.ClampToEdge
        bufferFlags: Buffer.None
        sizeMultiplier: 1.0
    }

    passes: [
        Pass {
            shaders: [ edgesShader ]
            output: edgeBuffer
        },
        Pass {
            shaders: [ washShader ]
            output: washBuffer
            commands: [ BufferInput { sampler: "sceneSampler" } ]
        },
        Pass {
            shaders: [ composeShader ]
            commands: [
                BufferInput { buffer: washBuffer; sampler: "washSampler" },
                BufferInput { buffer: edgeBuffer; sampler: "edgeSampler" }
            ]
        }
    ]
}
