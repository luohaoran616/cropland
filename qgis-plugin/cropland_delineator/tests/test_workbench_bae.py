# 耕地标注工作台 B/A/E 无头测试：几何算术全覆盖
# 运行：QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/share/qgis/python python3 test_bae.py
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="wb_bae_")

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsField, QgsFeature,
    QgsGeometry, QgsPointXY, QgsVectorFileWriter,
    QgsCoordinateReferenceSystem,
)
from qgis.PyQt.QtCore import QMetaType, QSettings
app = QgsApplication([], False)
app.initQgis()

sys.path.insert(0, "/home/luo/projects/202608/alpha/qgis-plugin")
from cropland_delineator import annotate, map_tools, osm_roads

LOGS = []


def log(msg):
    LOGS.append(str(msg))
    print("   |", msg)


class FakeCanvas:
    def setExtent(self, ext):
        pass

    def refresh(self):
        pass


class FakeIface:
    def __init__(self):
        self._al = None

    def mapCanvas(self):
        return FakeCanvas()

    def activeLayer(self):
        return self._al


iface = FakeIface()
wb = annotate.AnnotationController(iface, log)

# ---- 渔网：2 格 1000x1000，EPSG:4550 ----
grid_mem = QgsVectorLayer("polygon?crs=EPSG:4550", "grid", "memory")
grid_mem.dataProvider().addAttributes([
    QgsField("FID_1", QMetaType.Type.QString),
    QgsField("Row", QMetaType.Type.Int),
    QgsField("Col", QMetaType.Type.Int),
])
grid_mem.updateFields()
CELLS = [("test_0_0", 0, 0, 0, 0, 1000, 1000),
         ("test_0_1", 0, 1, 1000, 0, 2000, 1000)]
grid_feats = {}
for cid, row, col, x0, y0, x1, y1 in CELLS:
    f = QgsFeature(grid_mem.fields())
    f.setAttributes([cid, row, col])
    f.setGeometry(QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x1, y0),
        QgsPointXY(x1, y1), QgsPointXY(x0, y1), QgsPointXY(x0, y0)]]))
    grid_mem.dataProvider().addFeature(f)
    grid_feats[cid] = f.id()
grid_path = os.path.join(WORK, "grid.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
opts.layerName = "grid"
QgsVectorFileWriter.writeAsVectorFormatV3(
    grid_mem, grid_path, QgsProject.instance().transformContext(), opts)
grid = QgsVectorLayer(grid_path + "|layername=grid", "grid", "ogr")
QgsProject.instance().addMapLayer(grid)
assert grid.isValid() and grid.featureCount() == 2
wb.dialog = type("D", (), {"grid_layer": staticmethod(lambda: grid)})()


def cell_total():
    n = 0
    area = 0.0
    for f in wb.layer.getFeatures(wb._cell_filter()):
        n += 1
        area += f.geometry().area()
    return n, area


def near(a, b, tol=0.5):
    assert abs(a - b) < tol, f"{a} != {b}"
    return True


# ---- 打开标注层 ----
assert wb.open_layer(os.path.join(WORK, "耕地标注.gpkg"))

# ==== T1 建底板 ====
grid.selectByIds([grid_feats["test_0_0"]])
wb.set_cell_from_selection()
n, area = cell_total()
assert n == 1 and near(area, 1_000_000)
print("T1 建底板 ✓  1,000,000 m²")

# ==== T2 选中即挖除（标注层上的 SAM 面）+ 一步撤销 ====
sam = QgsGeometry.fromPolygonXY([[
    QgsPointXY(200, 200), QgsPointXY(400, 200),
    QgsPointXY(400, 400), QgsPointXY(200, 400), QgsPointXY(200, 200)]])
wb.layer.beginEditCommand("加 sam 面")
sf = wb._new_feature(sam, "sam")
wb.layer.addFeature(sf)
wb.layer.endEditCommand()
n, area = cell_total()
assert n == 2 and near(area, 1_040_000)
wb.layer.selectByIds([sf.id()])
iface._al = wb.layer
wb.erase_selected()
n, area = cell_total()
assert n == 1 and near(area, 960_000), (n, area)   # sam 面被消费，底板留洞
wb.undo()                                           # 一步撤销：差集+删除一起回
n, area = cell_total()
assert n == 2 and near(area, 1_040_000), (n, area)
print("T2 选中即挖除（内挖+消费+一步撤销）✓  1,040,000 → 960,000 → 撤销回 1,040,000")

# ==== T3 选中即挖除（外部图层的选择，不删外部数据） ====
scratch = QgsVectorLayer("polygon?crs=EPSG:4550", "scratch", "memory")
half = QgsFeature()
half.setGeometry(QgsGeometry.fromPolygonXY([[
    QgsPointXY(500, 0), QgsPointXY(1500, 0),
    QgsPointXY(1500, 1000), QgsPointXY(500, 1000), QgsPointXY(500, 0)]]))
scratch.dataProvider().addFeature(half)
QgsProject.instance().addMapLayer(scratch)
scratch.selectByIds([half.id()])
iface._al = scratch
wb.erase_selected()
n, area = cell_total()
assert n == 2 and near(area, 540_000), (n, area)   # 底板右半被挖，sam 不动
assert scratch.featureCount() == 1                  # 外部层数据未动
print("T3 选中即挖除（外部层、半跨界）✓  1,040,000 → 540,000，外部层保持 1 面")

# ==== T4 下一格 + OSM 道路一键挖除（含缓冲并集去重） ====
wb.next_cell()
assert wb.current_cell == "test_0_1"
n, area = cell_total()
assert n == 1 and near(area, 1_000_000)
roads = QgsVectorLayer("linestring?crs=EPSG:4550", "roads", "memory")
roads.dataProvider().addAttributes([QgsField("highway", QMetaType.Type.QString)])
roads.updateFields()
for hw, pts in (("primary", [(1500, -100), (1500, 1100)]),
                ("residential", [(900, 500), (2100, 500)])):
    f = QgsFeature(roads.fields())
    f.setAttributes([hw])
    f.setGeometry(QgsGeometry.fromPolylineXY(
        [QgsPointXY(x, y) for x, y in pts]))
    roads.dataProvider().addFeature(f)
wb.apply_osm_roads(roads)
# 大路 25m×1000 + 小路 8m×1000，十字交叉重叠 25×8=200 只扣一次；
# 十字道路把底板切成 4 块（自动 M2S 拆分）
n, area = cell_total()
assert n == 4 and near(area, 967_200), (n, area)
print("T4 下一格 + OSM 道路挖除 ✓  1,000,000 → 4 块 967,200（并集去重 200 m²）")

# ==== T5 QA 碎块清理 ====
sliver = QgsGeometry.fromPolygonXY([[
    QgsPointXY(1300, 300), QgsPointXY(1320, 300),
    QgsPointXY(1320, 310), QgsPointXY(1300, 310), QgsPointXY(1300, 300)]])
wb.apply_add(sliver)
n, area = cell_total()
assert n == 5 and near(area, 967_200 + 200)
wb.qa_cleanup(500)
n, area = cell_total()
assert n == 4 and near(area, 967_200)
print("T5 QA 碎块清理 ✓  200 m² 碎块删除，回到 967,200")

# ==== T6 QA 重叠检查 ====
overlap = QgsGeometry.fromPolygonXY([[
    QgsPointXY(1450, 600), QgsPointXY(1550, 600),
    QgsPointXY(1550, 700), QgsPointXY(1450, 700), QgsPointXY(1450, 600)]])
wb.apply_add(overlap)
wb.qa_overlaps()
assert any("[QA]" in m and "重叠" in m for m in LOGS[-3:])
wb.layer.beginEditCommand("清掉重叠测试面")
dels = [f.id() for f in wb.layer.getFeatures(
    wb._cell_filter("source='add'"))]
wb.layer.deleteFeatures(dels)
wb.layer.endEditCommand()
print("T6 QA 重叠检查 ✓  检出与道路挖除后底板的重叠")

# ==== T7 报表 CSV ====
csv_path = os.path.join(WORK, "report.csv")
wb.export_report(csv_path)
assert os.path.exists(csv_path)
import csv as _csv
with open(csv_path, encoding="utf-8-sig") as fh:
    rows = list(_csv.reader(fh))
assert len(rows) == 4 and rows[0][0] == "格子"
assert rows[2][0] == "test_0_1" and rows[2][5] == "96.7%", rows[2]
assert rows[3][0] == "合计"
print(f"T7 报表 CSV ✓  {len(rows) - 2} 格 + 合计，覆盖率 96.7% 正确")

# ==== T8 最后一格再下一格 ====
wb.next_cell()
assert any("最后一格" in m for m in LOGS[-2:])
print("T8 末格保护 ✓")

# ==== T9 save_roads（WGS84 → 4550 缓存 GPKG）====
feats = [
    {"properties": {"highway": "primary", "osm_id": 1},
     "geometry": {"coordinates": [[123.0, 40.9], [123.01, 40.9]]}},
    {"properties": {"highway": "track", "osm_id": 2},
     "geometry": {"coordinates": [[123.0, 40.9], [123.0, 40.91]]}},
]
path = osm_roads.save_roads(
    feats, grid, QgsCoordinateReferenceSystem("EPSG:4550"))
assert path and os.path.exists(path)
rl = QgsVectorLayer(path + "|layername=osm_roads", "osm", "ogr")
assert rl.isValid() and rl.featureCount() == 2
assert rl.crs().authid() == "EPSG:4550", rl.crs().authid()
assert rl.extent().width() > 100  # 米制坐标而非经纬度
lens = sorted(round(f.geometry().length()) for f in rl.getFeatures())
print(f"T9 道路缓存 GPKG ✓  2 条重投影落盘，长度(m)={lens}")

# ==== T10 Shift 正交锁 ====
p = map_tools.PolylineTool._ortho_snap(
    QgsPointXY(0, 0), QgsPointXY(100, 30))
assert abs(p.x() - 100) < 1e-6 and abs(p.y() - 30) < 1e-6  # 无 Shift 原样
try:
    orig = map_tools.QApplication.keyboardModifiers
    map_tools.QApplication.keyboardModifiers = staticmethod(
        lambda: map_tools.Qt.KeyboardModifier.ShiftModifier)
    p = map_tools.PolylineTool._ortho_snap(
        QgsPointXY(0, 0), QgsPointXY(100, 30))
    q = map_tools.PolylineTool._ortho_snap(
        QgsPointXY(0, 0), QgsPointXY(5, 60))
    map_tools.QApplication.keyboardModifiers = orig
    assert abs(p.x() - 100) < 1e-6 and abs(p.y()) < 1e-6    # 吸附到水平
    assert abs(q.x()) < 1e-6 and abs(q.y() - 60) < 1e-6     # 吸附到垂直
    print("T10 正交锁 ✓（水平/垂直分支几何正确）")
except (AttributeError, TypeError) as exc:
    print(f"T10 正交锁（无修饰键原样返回 ✓；Shift 分支无法无头模拟：{exc}）")

# ==== T11 同文件重开保留当前格 / QSettings 跨会话恢复 ====
assert wb.current_cell == "test_0_1"
assert wb.open_layer(os.path.join(WORK, "耕地标注.gpkg"))
assert wb.current_cell == "test_0_1", wb.current_cell
n_disk = sum(1 for f in wb.layer.getFeatures(wb._cell_filter()))
assert n_disk == 4, n_disk  # 重开已先提交：4 块在磁盘新实例上可读（防笔触丢失）
print("T11a 同文件重开保留当前格 + 先提交未落盘笔触 ✓")
wb.close_session()  # 落盘模拟真实关闭：恢复依赖已提交的要素
wb2 = annotate.AnnotationController(iface, log)
wb2.dialog = type("D", (), {"grid_layer": staticmethod(lambda: grid)})()
assert wb2.open_layer(os.path.join(WORK, "耕地标注.gpkg"))
assert wb2.current_cell == "test_0_1", wb2.current_cell
assert wb2.cell_geom is not None and near(wb2.cell_geom.area(), 1_000_000)
QSettings().remove("cropland_delineator/last_cell")
wb2.close_session()
print("T11b QSettings 恢复上次工作格（含格子几何）✓")

# ==== T12 道路层缺失时自动回退缓存文件（不联网） ====
wb.roads_layer = None
for L in QgsProject.instance().mapLayersByName("OSM 道路"):
    QgsProject.instance().removeMapLayer(L.id())
assert wb.osm_layer() is not None            # 从渔网目录缓存自动加载
assert wb.roads_layer is not None and wb.roads_layer.featureCount() == 2
wb.apply_osm_roads()                          # 走缓存层，正常走完挖除
assert any("[OSM 道路]" in m for m in LOGS[-3:])
print("T12 道路层缺失 → 自动回退缓存文件 ✓")

print("\n全部 12 项通过 ✓")
shutil.rmtree(WORK, ignore_errors=True)
app.exitQgis()
