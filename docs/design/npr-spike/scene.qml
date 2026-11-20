import QtQuick
import QtQuick3D
import QtQuick3D.Helpers
Item {
    id: bench
    width: 640; height: 360
    property int pictureWidth: 640
    property int pictureHeight: 360
    property int effectOn: 0     // 0 none, 1 npr
    property real probe: 0
    property string aa: "msaa"
    View3D {
        objectName: "bench"
        width: bench.pictureWidth; height: bench.pictureHeight
        explicitTextureWidth: bench.pictureWidth; explicitTextureHeight: bench.pictureHeight
        camera: camera
        environment: ExtendedSceneEnvironment {
            backgroundMode: SceneEnvironment.Color; clearColor: "#8fb4d8"
            tonemapMode: SceneEnvironment.TonemapModeLinear
            antialiasingMode: bench.aa === "msaa" ? SceneEnvironment.MSAA : SceneEnvironment.NoAA
            antialiasingQuality: SceneEnvironment.High
            effects: bench.effectOn ? [npr] : []
        }
        NprSpike { id: npr; mode: bench.probe }
        DirectionalLight { rotation: Quaternion.fromEulerAngles(-50, -30, 0); brightness: 1.2 }
        PerspectiveCamera { id: camera; fieldOfView: 40; clipNear: 5; clipFar: 1600000; position: Qt.vector3d(0, 150, 600); rotation: Quaternion.fromEulerAngles(-12, 0, 0) }
        Model { source: "#Rectangle"; rotation: Quaternion.fromEulerAngles(-90, 0, 0); scale: Qt.vector3d(40, 40, 1)
                materials: PrincipledMaterial { baseColor: "#6a9b4a" } }
        Model { source: "#Sphere"; y: 100; x: -120; scale: Qt.vector3d(2, 2, 2)
                materials: PrincipledMaterial { baseColor: "#c8c8c8" } }
        Model { source: "#Cube"; y: 60; x: 130; z: -150; scale: Qt.vector3d(1.2, 1.2, 1.2)
                materials: PrincipledMaterial { baseColor: "#b07a50" } }
    }
}
