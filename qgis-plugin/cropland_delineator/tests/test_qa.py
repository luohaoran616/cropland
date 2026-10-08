# F4 QA 无头测试：漏画空洞 / 暗斑提示（边界套合度实测无区分度已撤下，见决策 0006）
# 运行：cd qgis-plugin/cropland_delineator && \
#   QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/share/qgis/python python3 tests/test_qa.py
import math
import os
import shutil
import sys
import tempfile

import numpy as np

WORK = tempfile.mkdtemp(prefix="wb_qa_")

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsField, QgsFeature,
    QgsGeometry, QgsPointXY, QgsMapSettings, QgsRasterLayer,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsRectangle,
)
from qgis.PyQt.QtCore import QMetaType

app = QgsApplication([], False)
app.initQgis()

sys.path.insert(0, "/home/luo/projects/202608/alpha/qgis-plugin")
from cropland_delineator import annotate

LOGS = []


def log(msg):
    LOGS.append(str(msg))
    print("   |", msg)


class FakeCanvas:
    def __init__(self):
        self._ms = QgsMapSettings()
        self._ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4550"))

    def mapSettings(self):
        return self._ms

    def setExtent(self, ext):
        pass

    def refresh(self):
        pass


class FakeIface:
    def mapCanvas(self):
        return canvas

    def activeLayer(self):
        return None


canvas = FakeCanvas()
wb = annotate.AnnotationController(FakeIface(), log)

# ---- 合成影像：弱对比田块拼布 + 亮色矩形轮廓(800..1100,200..500) + 水体(1200..1400,200..400)
from osgeo import gdal, osr

rng = np.random.default_rng(11)
size = 320
img = np.full((size, size), 90, np.uint8)
for _ in range(48):
    x0, y0 = rng.integers(0, size - 40, 2)
    w, h = rng.integers(20, 70, 2)
    img[y0:y0 + h, x0:x0 + w] = rng.integers(65, 125)
noise_mask = np.ones_like(img, bool)
img = np.clip(img.astype(np.int16) + rng.integers(-5, 6, img.shape), 0, 255)
img = img.astype(np.uint8)
# 水体（纯 5，不加噪）
img[240:280, 240:280] = 5
# 亮色矩形轮廓 px x160..220, y220..280 = 地图 (800..1100, 200..500)
img[220:222, 160:222] = 235
img[279:281, 160:222] = 235
img[220:282, 160:162] = 235
img[220:282, 220:222] = 235

tif = os.path.join(WORK, "img.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, size, size, 3, gdal.GDT_Byte)
ds.SetGeoTransform((0, 5, 0, 1600, 0, -5))
sr = osr.SpatialReference()
sr.ImportFromEPSG(4550)
ds.SetProjection(sr.ExportToWkt())
for b in (1, 2, 3):
    ds.GetRasterBand(b).WriteArray(img)
ds = None
raster = QgsRasterLayer(tif, "合成影像")
assert raster.isValid()
QgsProject.instance().addMapLayer(raster)

# ---- 渔网 1 格 + 标注层 ----
grid_mem = QgsVectorLayer("polygon?crs=EPSG:4550", "grid", "memory")
grid_mem.dataProvider().addAttributes(
    [QgsField("FID_1", QMetaType.Type.QString)])
grid_mem.updateFields()
gf = QgsFeature(grid_mem.fields())
gf.setAttributes(["qa_cell"])
gf.setGeometry(QgsGeometry.fromPolygonXY([[
    QgsPointXY(0, 0), QgsPointXY(1600, 0), QgsPointXY(1600, 1600),
    QgsPointXY(0, 1600), QgsPointXY(0, 0)]]))
grid_mem.dataProvider().addFeature(gf)
gp = os.path.join(WORK, "grid.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
opts.layerName = "grid"
QgsVectorFileWriter.writeAsVectorFormatV3(
    grid_mem, gp, QgsProject.instance().transformContext(), opts)
grid = QgsVectorLayer(gp + "|layername=grid", "grid", "ogr")
QgsProject.instance().addMapLayer(grid)
wb.dialog = type("D", (), {"grid_layer": staticmethod(lambda: grid)})()
assert wb.open_layer(os.path.join(WORK, "耕地标注.gpkg"))
grid.selectByIds([1])
wb.set_cell_from_selection()
assert wb.current_cell == "qa_cell"


def add_parcel(x0, y0, x1, y1):
    g = QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x1, y0), QgsPointXY(x1, y1),
        QgsPointXY(x0, y1), QgsPointXY(x0, y0)]])
    wb.apply_add(g)


# ==== T1 漏画：无洞 → 有洞 ====
assert wb.qa_holes(666.7) == []
sq = QgsGeometry.fromPolygonXY([[
    QgsPointXY(700, 700), QgsPointXY(900, 700), QgsPointXY(900, 900),
    QgsPointXY(700, 900), QgsPointXY(700, 700)]])
wb.apply_difference(sq, "erase")
holes = wb.qa_holes(666.7)
assert len(holes) == 1 and abs(holes[0].area() - 200 * 200) < 200, \
    (len(holes), holes and holes[0].area())
lyr = QgsProject.instance().mapLayersByName("漏画候选")
assert lyr and lyr[0].featureCount() == 1
wb.undo()  # 撤销挖除，恢复整格底板
holes = wb.qa_holes(666.7)
assert holes == [], "撤销后不应再有洞"
print("[1] 漏画检查 ok（发现 200×200 空洞 → 图层 → 撤销后清零）")

# ==== T2 边界套合度：已撤下（负结果，决策 0006） ====
# 实机校准（668 块真实手标 vs 随机方块 vs 平移 3px 对照）三种统计量
# （绝对梯度阈值 / 相对边缘度 / 边缘方向一致性）均无区分度
# （真实贴边率 0.56 vs 随机 0.52），撤下以免 54% 误报训练用户忽略 QA。

# ==== T3 暗斑：覆盖水体的地块被点名 ====
add_parcel(800, 200, 1100, 500)    # 普通地块（贴亮线，不应点名）
add_parcel(1200, 200, 1400, 400)   # 恰好是水体（60.0 亩）
dark = wb.qa_dark(p_lo=2.0)
assert dark, "水体地块应被点名"
mu_dark = {round(mu) for _l, _f, mu in dark}
assert 60 in mu_dark, mu_dark
for mu in mu_dark:
    assert mu in (60,), f"普通地块不应被点名：{mu_dark}"
print(f"[3] 暗斑提示 ok（点名 {sorted(mu_dark)} 亩的地块，普通地块未误报）")

shutil.rmtree(WORK, ignore_errors=True)
print("\nQA 两件套（漏画+暗斑）测试通过 ✓（工作目录已清理）")
sys.exit(0)
