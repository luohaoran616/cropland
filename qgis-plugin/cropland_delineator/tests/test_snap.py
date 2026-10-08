# 笔刷捕捉回归测试：QGIS 4.2 snapPoint 静默不吸附根因 + v0.9.6 直查修复
# 运行：cd qgis-plugin/cropland_delineator && \
#   QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/share/qgis/python python3 tests/test_snap.py
#
# 根因（v0.9.6 修复）：QGIS 4.2 起 QgsMapMouseEvent.snapPoint() 内部是
# relaxed 非阻塞查询，捕捉索引未就绪时静默返回不吸附；笔刷每次落笔都
# 编辑图层令索引反复重建，靠它几乎吸不住。修复 = _map_point 直接调
# snappingUtils().snapToMap()（落点场景阻塞补建索引），悬停 relaxed。
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="wb_snap_")

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsFeature,
    QgsGeometry, QgsPointXY, QgsRectangle, QgsTolerance, Qgis,
)
from qgis.gui import QgsMapCanvas, QgsMapMouseEvent
from qgis.PyQt.QtCore import QEvent, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent
from qgis.PyQt.QtWidgets import QApplication

app = QgsApplication([], False)
app.initQgis()

sys.path.insert(0, "/home/luo/projects/202608/alpha/qgis-plugin")
from cropland_delineator import annotate, map_tools

LOGS = []


def log(msg):
    LOGS.append(str(msg))
    print("   |", msg)


class FakeCanvas:
    def mapSettings(self):
        return None


class FakeIface:
    def mapCanvas(self):
        return FakeCanvas()

    def activeLayer(self):
        return None


wb = annotate.AnnotationController(FakeIface(), log)

# ==== T1 _enable_snapping：工程捕捉配置完整落位（setTypeFlag 级联）====
wb._enable_snapping()
cfg = QgsProject.instance().snappingConfig()
assert cfg.enabled(), "工程捕捉应已开启"
assert cfg.tolerance() == 12 and cfg.units() == QgsTolerance.UnitType.Pixels
assert int(cfg.mode()) in (int(Qgis.SnappingMode.AllLayers), 2), cfg.mode()
if hasattr(cfg, "typeFlag"):
    tf = int(cfg.typeFlag())
    assert tf & int(Qgis.SnappingType.Vertex), hex(tf)
    assert tf & int(Qgis.SnappingType.Segment), hex(tf)
print("[1] _enable_snapping 配置 ok（AllLayers · 顶点+线段 · 12px）")

# ==== T2 根因复现：冷索引下 relaxed 查询返回无效（不吸附）====
vl = QgsVectorLayer("Polygon?crs=EPSG:32651", "ref", "memory")
f = QgsFeature()
f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(200, 200), QgsPointXY(400, 200),
                                          QgsPointXY(400, 400), QgsPointXY(200, 400),
                                          QgsPointXY(200, 200)]]))
vl.dataProvider().addFeatures([f])
vl.updateExtents()
QgsProject.instance().addMapLayer(vl)

canvas = QgsMapCanvas()
canvas.setLayers([vl])
canvas.setExtent(QgsRectangle(100, 100, 500, 500))
canvas.resize(600, 600)
canvas.refresh()
app.processEvents()
# 独立画布不自动跟随工程捕捉配置（真实 QGIS 会话由 app 层同步），手动灌入
utils = canvas.snappingUtils()
utils.setConfig(cfg)
m_relaxed = utils.snapToMap(QgsPointXY(205, 202), None, True)
assert not m_relaxed.isValid(), "冷索引下 relaxed 应无效（即 snapPoint 静默失效的根因）"
print("[2] 根因复现 ok：冷索引 relaxed 查询 = INVALID（旧 e.snapPoint() 因此吸不住）")

# ==== T3 核心回归：无预热分发落点事件，必须吸附到顶点 (200,200) ====


class WbStub:
    road_widths = [20.0, 8.0]

    def log(self, *a):
        pass

    def set_road_preset(self, i):
        pass

    def buffer_preview(self, g):
        return None

    def to_layer(self, g):
        return g

    def apply_road(self, g):
        pass

    def apply_split(self, g):
        pass


tool = map_tools.PolylineTool(canvas, WbStub(), "line", "red", "road")
canvas.setMapTool(tool)
m2p = canvas.mapSettings().mapToPixel()
qp = m2p.transform(205.0, 202.0)   # 离顶点 (200,200) 约 8px < 12px 容差
target = canvas.viewport() or canvas
for typ, b1, b2 in (
    (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
    (QEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
):
    QApplication.instance().sendEvent(
        target, QMouseEvent(typ, QPointF(qp.x(), qp.y()), b1, b2,
                            Qt.KeyboardModifier.NoModifier))
app.processEvents()
p = tool.pts[0]
assert p is not None, "落点未采集"
assert abs(p.x() - 200.0) < 1e-6 and abs(p.y() - 200.0) < 1e-6, \
    f"无预热落点应吸附到 (200,200)，实测 ({p.x():.2f},{p.y():.2f})"
print("[3] 无预热落点吸附 ok：(205,202) → (200,200)（旧代码落原始点）")

# ==== T4 捕捉标记：吸附时显示、无匹配时隐藏 ====


def fake_event(x, y):
    qp2 = m2p.transform(x, y)
    return QgsMapMouseEvent(canvas, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(qp2.x(), qp2.y()),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


snapped = tool._map_point(fake_event(205, 202))
assert abs(snapped.x() - 200.0) < 1e-6, "已预热后悬停也应吸附"
assert tool._snap_ind is not None and tool._snap_ind.isVisible(), "吸附处应显示捕捉标记"
far = tool._map_point(fake_event(2000, 2000), relaxed=True)
# 手构事件像素取整（QPoint int）在远离画布处有 ~1 像素（0.67 单位）舍入
assert abs(far.x() - 2000.0) < 1.0 and abs(far.y() - 2000.0) < 1.0, \
    f"无匹配处应返回原始点，实测 ({far.x():.2f},{far.y():.2f})"
assert not tool._snap_ind.isVisible(), "无匹配处应隐藏捕捉标记"
print("[4] 捕捉标记 ok：吸附显示 / 无匹配隐藏（relaxed 悬停不阻塞）")

# ==== T5 cancel/deactivate 收走标记 ====
tool.cancel()
assert not tool._snap_ind.isVisible(), "取消笔时应隐藏捕捉标记"
print("[5] 取消收尾 ok：标记随笔取消隐藏")

# ==== T6 多边形笔刷预览：第一点落定后即显示到光标的连线 ====
def _vcount(geom):
    if geom is None or geom.isEmpty():
        return 0
    return sum(1 for _ in geom.vertices())


poly = map_tools.PolygonTool(canvas, WbStub(), "add")
poly.pts = [QgsPointXY(200.0, 200.0)]
poly._refresh_preview(QgsPointXY(300.0, 260.0))
n1 = _vcount(poly.rb.asGeometry())
assert n1 >= 2, f"一点+光标即应有连线预览，实测 {n1} 个顶点"
poly.pts.append(QgsPointXY(300.0, 260.0))
poly._refresh_preview(QgsPointXY(260.0, 320.0))
n2 = _vcount(poly.rb.asGeometry())
assert n2 >= 3, f"两点+光标应有闭合轮廓预览，实测 {n2} 个顶点"
print("[6] 多边形笔刷预览 ok：首点即连线（旧代码第 2 点前无预览）")

shutil.rmtree(WORK, ignore_errors=True)
print("\n全部 6 组测试通过 ✓  (工作目录已清理)")
sys.exit(0)
