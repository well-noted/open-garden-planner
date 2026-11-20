// Render styles WP9: the 3D "Line & Wash" look (NprSpec, core/scene3d/look.py) as ONE
// multi-pass Effect, so it takes one slot of the environment's effect chain (PF-E3D-21:
// with glow on, two user effects ahead of Qt's scene effect break the chain; the NPR look
// has glow and SSR off and folds the depth of field in instead of chaining PhotoBlur).
//
//  pass 1 npr_edges.frag   -> edgeBuffer (RGBA8): ink from the SECOND difference of the
//                             depth's reciprocal (zero on every plane, so sloped ground
//                             draws no ink; docs/design/npr-spike/FINDINGS.md), split into
//                             silhouettes (r) and creases / steps (g); b the view depth as a
//                             share of the paper fog's far distance (1: sky); a "busy", a
//                             leaf-level depth jump
//  pass 2 npr_mass.frag    -> massBuffer (RGBA16F, half the picture each way): the
//                             depth's reciprocal DILATED over the mass radius (the nearest
//                             leaf of a block) and the block's busy share
//  pass 3 npr_close.frag   -> closeBuffer (the same size): the dilation ERODED back - the
//                             depth closed, a crown with its gaps filled - and the busy
//                             share spread, so the crown's rim counts as foliage too
//  pass 4 npr_wash.frag    -> washBuffer (RGBA16F): the optional depth of field, then the
//                             exposure and a filmic curve, the luminance cut into tonal bands
//                             (chroma kept) with a dried rim on smooth surfaces, and pigment
//                             pooled where the colour changes
//  pass 5 npr_compose.frag -> the screen: wash over paper, unevenly laid and granulated,
//                             paper fog, sky as paper, grain; the ink: fine lines where the
//                             picture is calm, the closed depth's outline where it is busy
//                             (foliage: one line round the crown, not one per leaf)
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
    property real stepThresh: 0.0005      // ... and steps (a frame proud of its wall) ...
    property real stepContrast: 0.3      // ... where the colour changes too (log2 luminance)
    property real creaseInk: 0.55         // a crease's ink against a silhouette's 1
    property real inkSpread: 0.85         // the pen's width: ink taken from the strongest neighbour
    property real silhouetteShare: 0.0012 // an outline's half width as a share of the height
    property real busyThresh: 0.008       // a leaf-level depth jump (relative 2nd difference)
    property real busyCut: 0.25            // share of busy pixels where the mass outline takes over
    property real massShare: 0.008        // the radius the leaves' depth is closed over
    property real massThresh: 0.015        // the closed depth's outline threshold
    property real massInk: 1.0            // the mass outline's ink
    property real foliageSoftenShare: 0.004 // foliage's wash averaged over this radius
    property real bands: 5.0              // tonal bands from paper to ink
    property real bandSoft: 0.12          // the soft step between two bands, 0..0.5
    property real bandRim: 0.3            // a band's dried edge on a smooth surface
    property real bandRimShare: 0.0012    // ... its width as a share of the height
    property real edgeDarken: 0.35        // pigment pooled where the wash's colour changes
    property real washLift: 0.2          // the wash thinned towards the paper
    property real washSaturation: 0.85    // chroma of the wash
    property real grainShare: 0.0016      // paper grain cell as a share of the height
    property real grainStrength: 0.08     // the grain's darkening at full pigment
    property real granulation: 0.2        // pigment settled in the paper's hollows
    property real granulationShare: 0.0045 // ... its specks' cell as a share of the height
    property real washMottle: 0.14         // the wash laid unevenly: density variation
    property real washMottleShare: 0.18   // ... over blooms of this share of the height
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
    readonly property TextureInput massSampler: TextureInput { texture: Texture {} }
    readonly property TextureInput closeSampler: TextureInput { texture: Texture {} }

    Shader {
        id: edgesShader
        stage: Shader.Fragment
        shader: Qt.resolvedUrl("npr_edges.frag")
    }
    Shader {
        id: massShader
        stage: Shader.Fragment
        shader: Qt.resolvedUrl("npr_mass.frag")
    }
    Shader {
        id: closeShader
        stage: Shader.Fragment
        shader: Qt.resolvedUrl("npr_close.frag")
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
    // the canopy mass: half the picture each way (a quarter of its pixels); its
    // radius is a share of the height (massShare), so the closing is alike at every size
    Buffer {
        id: massBuffer
        name: "nprMass"
        format: Buffer.RGBA16F
        textureFilterOperation: Buffer.Linear
        textureCoordOperation: Buffer.ClampToEdge
        bufferFlags: Buffer.None
        sizeMultiplier: 0.5
    }
    Buffer {
        id: closeBuffer
        name: "nprClose"
        format: Buffer.RGBA16F
        textureFilterOperation: Buffer.Linear
        textureCoordOperation: Buffer.ClampToEdge
        bufferFlags: Buffer.None
        sizeMultiplier: 0.5
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
            shaders: [ massShader ]
            output: massBuffer
            commands: [ BufferInput { buffer: edgeBuffer; sampler: "edgeSampler" } ]
        },
        Pass {
            shaders: [ closeShader ]
            output: closeBuffer
            commands: [ BufferInput { buffer: massBuffer; sampler: "massSampler" } ]
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
                BufferInput { buffer: edgeBuffer; sampler: "edgeSampler" },
                BufferInput { buffer: closeBuffer; sampler: "closeSampler" },
                BufferInput { buffer: massBuffer; sampler: "massSampler" }
            ]
        }
    ]
}
