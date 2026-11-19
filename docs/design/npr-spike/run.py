import sys, os
import numpy as np
from PyQt6.QtCore import QUrl, QSize, QCoreApplication
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQuick import QQuickView, QQuickItem, QQuickWindow, QSGRendererInterface
import time
app = QGuiApplication(sys.argv)
here = os.path.dirname(os.path.abspath(__file__))
def pump(ms):
    t = time.time()
    while (time.time() - t) * 1000 < ms:
        app.processEvents()
def shot(props, name):
    v = QQuickView(); v.setResizeMode(QQuickView.ResizeMode.SizeViewToRootObject)
    v.setInitialProperties(props); v.setSource(QUrl.fromLocalFile(here + "/scene.qml"))
    if v.errors(): print("ERR", [e.toString() for e in v.errors()]); return None
    v.show(); t = time.time()
    while not (v.isExposed() and v.isSceneGraphInitialized()) and time.time() - t < 20: pump(10)
    pump(200)
    api = QQuickWindow.graphicsApi()
    item = v.rootObject().findChild(QQuickItem, "bench")
    r = item.grabToImage(QSize(props["pictureWidth"], props["pictureHeight"])); v.grabWindow()
    QCoreApplication.sendPostedEvents(r, 0); img = r.image(); img.save(here + f"/{name}.png")
    w, h = img.width(), img.height()
    img = img.convertToFormat(img.Format.Format_RGBA8888)
    a = np.frombuffer(img.constBits().asarray(w*h*4), np.uint8).reshape(h, img.bytesPerLine()//4, 4)[:, :w]
    v.close(); pump(50)
    return a.astype(float), api
for aa in ("msaa", "none"):
    for size in ((640, 360), (1920, 1080)):
        base = dict(pictureWidth=size[0], pictureHeight=size[1], aa=aa)
        plain, api = shot({**base, "effectOn": 0}, f"plain_{aa}_{size[1]}")
        npr, _ = shot({**base, "effectOn": 1}, f"npr_{aa}_{size[1]}")
        pr, _ = shot({**base, "effectOn": 1, "probe": 1.0}, f"probe_{aa}_{size[1]}")
        edge = pr[..., 0]; depth = pr[..., 1]; sky = pr[..., 2]
        print(f"api={api} aa={aa} {size}: edge_frac={np.mean(edge>128):.4f} sky_frac={np.mean(sky>128):.3f} "
              f"depth_raw_min={depth.min():.0f} max={depth.max():.0f} "
              f"mean_abs_diff_vs_plain={np.mean(np.abs(npr-plain)):.1f}")
        # orientation: sky is top of picture -> top rows should be sky
        print("   top-row sky:", np.mean(sky[:size[1]//10] > 128).round(2), "bottom-row sky:", np.mean(sky[-size[1]//10:] > 128).round(2))
