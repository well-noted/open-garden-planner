import QtQuick
import QtQuick3D
Effect {
    property real edgeShare: 0.002
    property real depthThresh: 0.02
    property real bands: 4.0
    property real mode: 0.0
    Shader { id: edgesShader; stage: Shader.Fragment; shader: Qt.resolvedUrl("npr_edges.frag") }
    Shader { id: composeShader; stage: Shader.Fragment; shader: Qt.resolvedUrl("npr_compose.frag") }
    Buffer { id: edgeBuffer; name: "nprEdges"; format: Buffer.RGBA8; textureFilterOperation: Buffer.Linear
             textureCoordOperation: Buffer.ClampToEdge; bufferFlags: Buffer.None; sizeMultiplier: 1.0 }
    readonly property TextureInput edgeSampler: TextureInput { texture: Texture {} }
    passes: [
        Pass { shaders: [ edgesShader ]; output: edgeBuffer },
        Pass { shaders: [ composeShader ]
               commands: [ BufferInput { buffer: edgeBuffer; sampler: "edgeSampler" } ] }
    ]
}
