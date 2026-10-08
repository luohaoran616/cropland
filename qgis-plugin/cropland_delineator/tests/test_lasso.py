# F1 磁力套索无头测试：代价面寻路 + 合成 GeoTIFF 路径跟随 + 端到端切分
# 运行：cd qgis-plugin/cropland_delineator && \
#   QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/share/qgis/python python3 tests/test_lasso.py
import math
import os
import shutil
import sys
import tempfile
import time

import numpy as np

WORK = tempfile.mkdtemp(prefix="wb_lasso_")

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsField, QgsFeature,
    QgsGeometry, QgsPointXY, QgsMapSettings, QgsRasterLayer,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsRectangle,
)
from qgis.PyQt.QtCore import QMetaType

app = QgsApplication([], False)
app.initQgis()

sys.path.insert(0, "/home/luo/projects/202608/alpha/qgis-plugin")
from cropland_delineator import annotate, lasso

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
iface = FakeIface()
wb = annotate.AnnotationController(iface, log)


def curve_y(x):
    return 160 + int(60 * math.sin(2 * math.pi * (x - 20) / 280.0))


# ==== T1 均匀代价面：近似直线 ====
f = lasso.GridPathFinder(np.full((240, 240), 0.5, np.float32))
t0 = time.time()
pts = f.find((10, 20), (210, 20))
dt = time.time() - t0
assert pts and pts[0] == (10, 20) and pts[-1] == (210, 20), pts[:3]
assert max(abs(y - 20) for _, y in pts) <= 2, "均匀代价下应近乎直线"
assert abs(len(pts) - 201) <= 12
print(f"[1] 均匀代价直线 ok（{len(pts)} 点，{dt*1000:.0f}ms）")

# ==== T2 合成正弦低代价走廊：refine 精确跟随 ====
H = W = 320
cost = np.full((H, W), 0.9, np.float32)
truth = {x: curve_y(x) for x in range(20, 301)}
for x, y in truth.items():
    cost[max(0, y - 1):y + 2, x] = 0.02
f2 = lasso.GridPathFinder(cost)
t0 = time.time()
pts = f2.find((20, curve_y(20)), (300, curve_y(300)))
dt = time.time() - t0
devs = [abs(y - curve_y(x)) for x, y in pts]
assert len(pts) > 100, "路径点数过少"
assert max(devs) <= 3 and np.mean(devs) < 1.0, \
    f"应贴着走廊走：max={max(devs):.1f} mean={np.mean(devs):.2f}"
c_curve = f2.path_cost(pts)
line_px = [(x, 160) for x in range(20, 301)]  # 两端同高（y=160），真·直线采样
c_line = f2.path_cost(line_px)
assert c_curve < c_line * 0.8, (c_curve, c_line)
print(f"[2] 正弦走廊 refine ok（max 偏差 {max(devs)}px，"
      f"均值 {np.mean(devs):.2f}px，{len(pts)} 点，{dt*1000:.0f}ms）")

# ==== T3 粗网格 fast 预览：松但可用 ====
pts_fast = f2.find((20, curve_y(20)), (300, curve_y(300)), refine=False)
assert pts_fast and len(pts_fast) > 20
devs = [abs(y - curve_y(x)) for x, y in pts_fast]
assert max(devs) <= 12 and np.mean(devs) < 4, \
    f"fast 预览应大致贴线：max={max(devs)} mean={np.mean(devs):.2f}"
print(f"[3] fast 粗网格预览 ok（max 偏差 {max(devs)}px）")

# ==== T4 同点/不可达 ====
assert f.find((50, 50), (50, 50)) == [(50, 50)]
blocked = np.full((40, 40), 0.5, np.float32)
blocked[5:35, 19:21] = 0.5  # 占位（无掩膜时 A* 总能绕行，验证不崩即可）
fb = lasso.GridPathFinder(blocked)
assert fb.find((2, 20), (38, 20)) is not None
print("[4] 边界情况 ok")

# ==== T5 合成 GeoTIFF → LassoEngine 沿亮线走 ====
from osgeo import gdal, osr

rng = np.random.default_rng(7)
size = 320
img = np.full((size, size), 90, np.uint8)
for _ in range(48):  # 弱对比地块拼布（模拟田块色差）
    x0, y0 = rng.integers(0, size - 40, 2)
    w, h = rng.integers(20, 70, 2)
    img[y0:y0 + h, x0:x0 + w] = rng.integers(65, 125)
img = np.clip(img.astype(np.int16) + rng.integers(-5, 6, img.shape), 0, 255)
img = img.astype(np.uint8)
for x in range(20, 301):  # 亮色田间路（2px 宽，对比 ~+130）
    y = curve_y(x)
    img[y - 1:y + 1, x] = 235
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

eng = lasso.LassoEngine(
    raster, QgsRectangle(0, 0, 1600, 1600),
    QgsCoordinateReferenceSystem("EPSG:4550"))
assert eng.pixel_size() == 5.0
assert eng.shape == (size, size)
p0, p1 = eng.to_map(20, curve_y(20)), eng.to_map(300, curve_y(300))
t0 = time.time()
path = eng.path(p0, p1)
dt = time.time() - t0
assert path and len(path) > 80
px = [eng.to_px(p) for p in path]
devs = [abs(r - curve_y(c)) for c, r in px]
assert max(devs) <= 5 and np.mean(devs) < 2.5, \
    f"亮线路径跟随失败：max={max(devs)} mean={np.mean(devs):.2f}"
fast = eng.path(p0, p1, fast=True)
assert fast and len(fast) > 30
assert eng.path(eng.to_map(-30, 160), p1) is None, "窗外点应返回 None"
print(f"[5] GeoTIFF 引擎 ok（max 偏差 {max(devs)}px，均值 "
      f"{np.mean(devs):.2f}px，{len(path)} 点，{dt*1000:.0f}ms）")

# ==== T6 简化：去像素抖动不变形 ====
sim = lasso.simplify_pts(path, 1.2 * 5.0)
assert 2 <= len(sim) <= len(path)
g_raw = QgsGeometry.fromPolylineXY(path)
d_max = max(QgsGeometry.fromPointXY(p).distance(g_raw) for p in sim)
assert d_max < 12.0, f"简化偏移过大：{d_max:.1f} m"  # ≤ ~2px
print(f"[6] 简化 ok（{len(path)} → {len(sim)} 点，最大偏移 {d_max:.1f} m）")

# ==== T7 assemble：缺段走直线兜底 ====
A, B, C = (QgsPointXY(0, 0), QgsPointXY(10, 0), QgsPointXY(10, 10))
mid = QgsPointXY(5, 0)
out = lasso.assemble([A, B, C], {0: [A, mid, B]})
assert [(p.x(), p.y()) for p in out] == [(0, 0), (5, 0), (10, 0), (10, 10)]
out = lasso.assemble([A, B], {})
assert [(p.x(), p.y()) for p in out] == [(0, 0), (10, 0)]
print("[7] assemble ok")

# ==== T8 控制器端到端：自动探测影像 → 引擎 → 切分两块 ====
grid_mem = QgsVectorLayer("polygon?crs=EPSG:4550", "grid", "memory")
grid_mem.dataProvider().addAttributes(
    [QgsField("FID_1", QMetaType.Type.QString)])
grid_mem.updateFields()
gf = QgsFeature(grid_mem.fields())
gf.setAttributes(["cell_1600"])
gf.setGeometry(QgsGeometry.fromPolygonXY([[
    QgsPointXY(0, 0), QgsPointXY(1600, 0), QgsPointXY(1600, 1600),
    QgsPointXY(0, 1600), QgsPointXY(0, 0)]]))
grid_mem.dataProvider().addFeature(gf)
grid_path = os.path.join(WORK, "grid.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
opts.layerName = "grid"
QgsVectorFileWriter.writeAsVectorFormatV3(
    grid_mem, grid_path, QgsProject.instance().transformContext(), opts)
grid = QgsVectorLayer(grid_path + "|layername=grid", "grid", "ogr")
QgsProject.instance().addMapLayer(grid)
wb.dialog = type("D", (), {"grid_layer": staticmethod(lambda: grid)})()
assert wb.open_layer(os.path.join(WORK, "耕地标注.gpkg"))
grid.selectByIds([1])
wb.set_cell_from_selection()
assert wb.current_cell == "cell_1600"
feats = list(wb.layer.getFeatures(wb._cell_filter()))
assert len(feats) == 1 and abs(feats[0].geometry().area() - 1600 * 1600) < 1

wb.raster_layer = None  # 强制走自动探测
eng2, xf = wb.lasso_context()
assert eng2 is not None and xf is None  # 画布 CRS=4550 与栅格一致
assert eng2.shape == (size, size)
p0 = eng2.to_map(20, curve_y(20))
p1 = eng2.to_map(300, curve_y(300))
path = eng2.path(p0, p1)
assert path and len(path) > 80
# 工具收笔逻辑同款：简化 → 两端外推到刚出地块（不外推切不开）
base_geom = feats[0].geometry()
inside = lambda p: base_geom.intersects(QgsGeometry.fromPointXY(p))
line = QgsGeometry.fromPolylineXY(
    lasso.grow_ends(lasso.simplify_pts(path, 6.0), inside, 10.0))
wb.apply_split(line)
feats = list(wb.layer.getFeatures(wb._cell_filter()))
total = sum(x.geometry().area() for x in feats)
assert len(feats) == 2, f"亮线贯穿整格应切成 2 块，实际 {len(feats)}"
assert abs(total - 1600 * 1600) < 1.0, total
wb.undo()
feats = list(wb.layer.getFeatures(wb._cell_filter()))
assert len(feats) == 1, "切分应一步撤销"
# 无影像时优雅退化
wb.raster_layer = None
QgsProject.instance().removeMapLayer(raster.id())
wb._lasso_key = None
eng3, _ = wb.lasso_context()
assert eng3 is None
print("[8] 控制器端到端 ok（切分→撤销→无影像退化）")

# ==== T9 有界计算窗口：瓦片对齐 + 预览即提交 + 超窗退化 ====
pa = eng2.to_map(30, curve_y(30))
pb = eng2.to_map(290, curve_y(290))
wpts, win = eng2.path_window(pa, pb)
assert wpts and len(wpts) > 80 and win is not None, "窗口内应有路径"
# 窗口瓦片对齐且包含两端点
c0, r0 = eng2.to_px(pa)
c1, r1 = eng2.to_px(pb)
wx0, wy0, wx1, wy1 = win
assert wx0 % eng2.TILE == 0 and wy0 % eng2.TILE == 0, "窗口须对齐瓦片网格"
assert wx0 <= min(c0, c1) and max(c0, c1) < wx1
assert wy0 <= min(r0, r1) and max(r0, r1) < wy1
# 窗口 → 地图坐标正确（画格子框用）
bx0, by0, bx1, by1 = eng2.window_bbox(win)
assert (bx0, by0, bx1, by1) == (eng2.x0 + wx0 * eng2.pw,
                                eng2.y0 - wy1 * eng2.ph,
                                eng2.x0 + wx1 * eng2.pw,
                                eng2.y0 - wy0 * eng2.ph)
# 路径精度与全格走廊法同档（跟正弦亮线走）
devs = [abs(eng2.to_px(p)[1] - curve_y(eng2.to_px(p)[0])) for p in wpts]
assert max(devs) <= 5 and np.mean(devs) < 2.5, (max(devs), np.mean(devs))
# 幂等：同端点重算结果逐点一致（预览看到的=点击采纳的）
wpts2, win2 = eng2.path_window(pa, pb)
assert wpts2 == wpts and win2 == win, "path_window 须确定"
# 超窗：CAP 调小 → (None, None)，调用方退走廊法/直线
eng2.CAP = 10
assert eng2.path_window(pa, pb) == (None, None)
eng2.CAP = 1280
# 端点越出引擎窗口 → (None, None)
assert eng2.path_window(eng2.to_map(-5, 160), pb) == (None, None)
print("[9] 有界窗口（瓦片对齐/预览即提交/超窗退化）ok")

# ==== T10 大地块外推：grow_ends 步长自适应（整格底板 >800m 也要能切） ====
import math as _math
# 场景：3000m 半径的大地块（缝隙切分期的整格底板），锚点在正中
def inside_big(p):
    return _math.hypot(p.x(), p.y()) < 3000.0
line_pts = [QgsPointXY(-200.0, 0.0), QgsPointXY(200.0, 0.0)]
grown = lasso.grow_ends(line_pts, inside_big, 10.0)
# 两端须推出界外（旧实现 800m 封顶会困在里面 → "线没有真正穿过任何地块"）
assert grown[0].x() <= -3000.0 and grown[-1].x() >= 3000.0, \
    (grown[0].x(), grown[-1].x())
# 且只是刚出界，不至于跨过远处地块（3000~3600m 间出界即可）
assert grown[0].x() > -3100.0 and grown[-1].x() < 3100.0, \
    (grown[0].x(), grown[-1].x())
# 锚点本就在界外（>3000m）：只多走一步，不无谓外推
out_line = [QgsPointXY(3010.0, 0.0), QgsPointXY(3020.0, 0.0)]
grown2 = lasso.grow_ends(out_line, inside_big, 10.0)
assert abs(grown2[-1].x() - 3030.0) < 0.1, grown2[-1].x()
assert abs(grown2[0].x() - 3000.0) < 0.1, grown2[0].x()
print("[10] 大地块外推（3000m 半径推出界/界外只走一步）ok")

# ==== T11 磁力道路：mode='road' 收笔=按档宽缓冲差集，端点不外推 ====
from qgis.gui import QgsMapCanvas
from cropland_delineator import map_tools
mcv = QgsMapCanvas()
tool = map_tools.MagneticSplitTool(mcv, wb, "road")
assert tool.mode == "road"
f0 = next(iter(wb.layer.getFeatures(wb._cell_filter())))
area_before = f0.geometry().area()          # 整格 1600×1600
tool._engine = None                          # 无引擎直线模式（收笔路径共用）
tool.anchors = [QgsPointXY(300.0, 800.0), QgsPointXY(1300.0, 800.0)]
tool.segs = {}
tool._do_finish()
erased = area_before - sum(
    x.geometry().area() for x in wb.layer.getFeatures(wb._cell_filter()))
# 默认大路档 25m 全宽 × 长 1000m = 25000；Flat 端帽不外推（外推会扣 1600×25）
assert abs(erased - 25000.0) < 200.0, erased
assert not tool.anchors and not tool.segs, "收笔后应清空笔状态"
assert any("[道路]" in m for m in LOGS)
wb.undo()                                    # 一步撤销
n_after_undo = len(list(wb.layer.getFeatures(wb._cell_filter())))
assert n_after_undo == 1
print("[11] 磁力道路 ok（收笔=按档宽缓冲差集、端点不外推、可一步撤销）")

# ==== T12 采纳竞态修复：点击落在建议线终点 → 采纳到的必须是预览路径本身 ====
class _FakeEngine:
    def pixel_size(self):
        return 5.0

launches = []
tool2 = map_tools.MagneticSplitTool(mcv, wb, "road")
tool2._engine = _FakeEngine()
tool2._launch = lambda *a: launches.append(a)   # 隔离后台线程
tool2._on_left_click(QgsPointXY(100.0, 100.0))  # 第一颗锚点
assert launches == [] and len(tool2.anchors) == 1
# 模拟预览线程已完成：建议线 + 其终点
suggest = [QgsPointXY(100.0, 100.0), QgsPointXY(140.0, 130.0),
           QgsPointXY(180.0, 160.0)]
tool2.preview = list(suggest)
tool2._pv_target = QgsPointXY(180.0, 160.0)
# 点击落在终点上（距 1.4m < 5m 容差）——旧实现先清 preview 再采纳，
# 采到空表 → assemble 走直线兜底（用户所见"落直线"即此）
tool2._on_left_click(QgsPointXY(181.0, 161.0))
assert tool2.segs.get(0) == suggest, "应原样采纳预览路径"
assert launches == [], "命中预览终点不应触发重算"
full = lasso.assemble(tool2.anchors, tool2.segs)
assert any(abs(p.x() - 140.0) < 1e-6 and abs(p.y() - 130.0) < 1e-6
           for p in full), "整线应含建议线中间点而非两点直线"
# 点击远离预览终点 → 走重算分支，不采纳
tool2.preview = [QgsPointXY(180.0, 160.0), QgsPointXY(300.0, 300.0)]
tool2._pv_target = QgsPointXY(500.0, 500.0)
tool2._on_left_click(QgsPointXY(181.0, 161.0))
assert len(launches) == 1 and launches[0][2] == 1, "未命中应重算段号 1"
assert 1 not in tool2.segs
print("[12] 采纳竞态 ok（终点命中=原样采纳预览；未命中=重算）")

# ==== T13 落点撤回：has_stroke 判据 + 退点恢复到上一落点 ====
assert len(tool2.anchors) == 3 and tool2.has_stroke()
tool2.remove_last()
assert len(tool2.anchors) == 2 and tool2.has_stroke()
assert 1 not in tool2.segs, "退点应连带撤掉该段的采纳结果"
tool2.remove_last()
tool2.remove_last()
assert not tool2.anchors and not tool2.has_stroke(), "退光后不应再算落点中"
# 基类判据：折线/多边形工具按顶点数
poly_t = map_tools.PolylineTool(mcv, wb, "line", "yellow", "road")
assert not poly_t.has_stroke()
poly_t.pts = [QgsPointXY(0, 0)]
assert poly_t.has_stroke()
print("[13] 落点撤回 ok（has_stroke 判据 + 退点清段/退光归零）")

# ==== T14 神经网络边界证据：无亮度反差的边界被证据救回 ====
size2 = 320
flat = np.full((size2, size2), 100, np.uint8)   # 完全均匀 → 梯度全零
tif2 = os.path.join(WORK, "flat.tif")
_ds = gdal.GetDriverByName("GTiff").Create(tif2, size2, size2, 3, gdal.GDT_Byte)
_ds.SetGeoTransform((0, 5, 0, 1600, 0, -5))
_sr = osr.SpatialReference()
_sr.ImportFromEPSG(4550)
_ds.SetProjection(_sr.ExportToWkt())
for _b in (1, 2, 3):
    _ds.GetRasterBand(_b).WriteArray(flat)
_ds = None
raster2 = QgsRasterLayer(tif2, "均匀影像")
QgsProject.instance().addMapLayer(raster2)


def road_y(x):
    return 150 + int(70 * math.sin(2 * math.pi * (x - 20) / 300.0))


rect14 = QgsRectangle(0, 0, 1600, 1600)
crs14 = QgsCoordinateReferenceSystem("EPSG:4550")
eng_no = lasso.LassoEngine(raster2, rect14, crs14)
assert not eng_no.used_evidence
p0m = eng_no.to_map(20, road_y(20))
p1m = eng_no.to_map(300, road_y(300))
devs_no = [abs(r - road_y(c))
           for c, r in (eng_no.to_px(p) for p in eng_no.path(p0m, p1m))]
assert max(devs_no) > 40, "均匀影像无证据时应付近直线（大幅偏离真实路）"

ev = np.zeros((size2, size2), np.float32)
for x in range(20, 301):
    ev[road_y(x) - 2:road_y(x) + 3, x] = 0.9
eng_ev = lasso.LassoEngine(raster2, rect14, crs14, evidence=ev)
assert eng_ev.used_evidence
devs_ev = [abs(r - road_y(c))
           for c, r in (eng_ev.to_px(p) for p in eng_ev.path(p0m, p1m))]
assert max(devs_ev) <= 6 and np.mean(devs_ev) < 3.0, \
    (max(devs_ev), float(np.mean(devs_ev)))

# 形状不符的证据安全退化（不融合也不炸）；全零证据不伤梯度路径
eng_bad = lasso.LassoEngine(raster2, rect14, crs14, evidence=ev[:100, :100])
assert not eng_bad.used_evidence
raster1b = QgsRasterLayer(tif, "合成影像B")   # 原图层已被 T8 移出工程
eng_fuse = lasso.LassoEngine(
    raster1b, QgsRectangle(0, 0, 1600, 1600), crs14,
    evidence=np.zeros((size, size), np.float32))
assert eng_fuse.used_evidence
devs_fuse = [abs(r - curve_y(c)) for c, r in
             (eng_fuse.to_px(p) for p in eng_fuse.path(
                 eng_fuse.to_map(20, curve_y(20)),
                 eng_fuse.to_map(300, curve_y(300))))]
assert max(devs_fuse) <= 5, max(devs_fuse)

# load_evidence 读写往返 + 坏文件静默退化 + 路径约定
import json as _json
np.savez_compressed(os.path.join(WORK, "e1.npz"), ev=ev)
with open(os.path.join(WORK, "e1.json"), "w") as _f:
    _json.dump({"x0": 0.0, "y0": 1600.0, "pw": 5.0, "ph": 5.0}, _f)
ev_rt, meta_rt = lasso.load_evidence(os.path.join(WORK, "e1.npz"))
assert ev_rt is not None and ev_rt.shape == ev.shape and meta_rt["pw"] == 5.0
assert lasso.load_evidence(os.path.join(WORK, "不存在.npz")) == (None, None)
assert lasso.evidence_path(raster2, "c_1").endswith(
    os.path.join("边缘证据", "c_1.npz"))
QgsProject.instance().removeMapLayer(raster2.id())
print(f"[14] 边界证据 ok（无反差路救回：无证 max={max(devs_no)}px → "
      f"有证 max={max(devs_ev)}px；融合不伤梯度/坏证据退化）")

# ==== T15 磁力补画 mode='poly'：描一圈成面 + 闭合段补算 + 1/2 补画/挖除 ====
mp = map_tools.MagneticSplitTool(mcv, wb, "poly")
assert mp.mode == "poly" and mp.poly_mode == "add"

# 1) 直线兜底落地为补画（无引擎，缺段全走直线）：正方形 400×400
mp._engine = None
mp.anchors = [QgsPointXY(200.0, 200.0), QgsPointXY(600.0, 200.0),
              QgsPointXY(600.0, 600.0), QgsPointXY(200.0, 600.0)]
mp.segs = {}
feats = list(wb.layer.getFeatures(wb._cell_filter()))
assert len(feats) == 1 and abs(
    sum(f.geometry().area() for f in feats) - 1600 * 1600) < 1.0
mp._do_finish()
feats = list(wb.layer.getFeatures(wb._cell_filter()))
assert len(feats) == 2 and abs(
    sum(f.geometry().area() for f in feats)
    - (1600 * 1600 + 400 * 400)) < 1.0, "补画应新增 400×400 一块"
assert [r["kind"] for r in wb.ledger.cell_rows("cell_1600")][-1] == "add"
assert not mp.anchors and not mp.segs, "落地后笔状态应清空"
assert any("[补画]" in m for m in LOGS)

# 2) 挖除模式：同一方块 → 补的整块消失 + 底板打洞 = 净减 2×160000
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QKeyEvent

mp._set_poly_mode("erase")
assert mp.poly_mode == "erase"
mp.anchors = [QgsPointXY(200.0, 200.0), QgsPointXY(600.0, 200.0),
              QgsPointXY(600.0, 600.0), QgsPointXY(200.0, 600.0)]
mp.segs = {}
mp._do_finish()
feats = list(wb.layer.getFeatures(wb._cell_filter()))
assert len(feats) == 1, "补的块整块被扣掉，只剩带洞底板"
assert abs(sum(f.geometry().area() for f in feats)
           - (1600 * 1600 - 400 * 400)) < 1.0
print("[15a] 磁力补画落地 ok（直线兜底补画 +400² / 挖除净减 2×400²）")

# 3) 1/2 按键换模式（补画/挖除）
mp5 = map_tools.MagneticSplitTool(mcv, wb, "poly")
k2 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_2,
               Qt.KeyboardModifier.NoModifier, "2")
mp5.keyPressEvent(k2)
assert mp5.poly_mode == "erase" and k2.isAccepted()
k1 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_1,
               Qt.KeyboardModifier.NoModifier, "1")
mp5.keyPressEvent(k1)
assert mp5.poly_mode == "add"

# 4) 闭合段流：收笔先补算末→首段，回来后自动落地（含段中点非直线证据）
mp2 = map_tools.MagneticSplitTool(mcv, wb, "poly")
mp2._engine = _FakeEngine()
launches2 = []
mp2._launch = lambda *a: launches2.append(a)
mp2.anchors = [QgsPointXY(200.0, 200.0), QgsPointXY(600.0, 200.0),
               QgsPointXY(200.0, 600.0)]
mp2._finish()
assert len(launches2) == 1 and launches2[0][2] == 2, \
    "收笔应收笔即补算闭合段（idx=末顶点号）"
assert mp2._pending_finish and len(mp2.anchors) == 3
close_pts = [(200.0, 600.0), (200.0, 400.0), (200.0, 200.0)]
mp2._on_done({"gen": mp2._gen, "idx": 2, "pts": close_pts,
              "win": None, "target": (200.0, 200.0)})
assert not mp2.anchors, "闭合段回来应自动落地并清笔"
feats = list(wb.layer.getFeatures(wb._cell_filter()))
assert len(feats) == 2 and abs(
    sum(f.geometry().area() for f in feats)
    - (1600 * 1600 - 400 * 400 + 80000)) < 1.0, \
    "三角形补画 +80000（含闭合段中点，面积不变）"

# 5) 退点代际：撤顶点作废在途闭合段（迟到结果不得落进旧槽）
mp3 = map_tools.MagneticSplitTool(mcv, wb, "poly")
mp3._engine = _FakeEngine()
g0 = mp3._gen
mp3.anchors = [QgsPointXY(0, 0), QgsPointXY(10, 0), QgsPointXY(0, 10)]
mp3._pending_finish = True
mp3.remove_last()
assert mp3._gen == g0 + 1 and not mp3._pending_finish
assert len(mp3.anchors) == 2 and mp3.has_stroke()
mp3._on_done({"gen": g0, "idx": 2, "pts": [(1, 1)],
              "win": None, "target": (0, 0)})
assert 2 not in mp3.segs, "过期代际的闭合段结果必须丢弃"

# 6) 顶点不足守卫
mp6 = map_tools.MagneticSplitTool(mcv, wb, "poly")
mp6.anchors = [QgsPointXY(0, 0), QgsPointXY(10, 0)]
mp6._finish()
assert len(mp6.anchors) == 2 and any(
    "至少点 3 个顶点" in m for m in LOGS)
print("[15b] 磁力补画交互 ok（1/2 换模式 / 闭合段补算自动落地 / 退点作废在途段 / 顶点守卫）")

# ==== T16 证据严格次级：宽晕带不得劫持中等强度梯度线（v0.9.1 实机教训） ====
# 场景：真实边界=清晰但非饱和的梯度线（cost 0.35），SAM 证据=贴线的宽晕带
# （高斯形、峰值 0.75、中心偏 4px——模糊生成物的典型形态）。
# 旧纯 min 融合：晕带中心 1−0.75=0.25 < 0.35 → 路径被拉进晕带（吸附变松）；
# 新三闸融合：晕带代价 ≥0.35 下限+压裙 → 路径死贴梯度线。
h3, w3 = 320, 320
xs3 = np.arange(w3)
ys3 = 160 + (40 * np.sin(2 * np.pi * xs3 / 640.0)).astype(int)
cost_g = np.full((h3, w3), 0.90, np.float32)     # 中等纹理背景
for x, y in zip(xs3, ys3):
    cost_g[y - 1:y + 2, x] = 0.35                # 清晰但非饱和的梯度线
ev3 = np.zeros((h3, w3), np.float32)
for x, y in zip(xs3, ys3):
    for dy in range(-8, 9):
        ev3[min(max(y + 4 + dy, 0), h3 - 1), x] = \
            float(np.exp(-(dy / 4.0) ** 2)) * 0.75
p0f, p1f = (5, int(ys3[5])), (300, int(ys3[300]))

finder_old = lasso.GridPathFinder(np.minimum(cost_g, 1.0 - ev3))
dev_old = max(abs(r - ys3[c]) for c, r in finder_old._astar(p0f, p1f))

ev3n = np.clip((ev3 - 0.45) / 0.55, 0.0, 1.0) ** 1.3
ev3_cost = 0.35 + (1.0 - ev3n) * 0.65
finder_new = lasso.GridPathFinder(np.minimum(cost_g, ev3_cost))
dev_new = max(abs(r - ys3[c]) for c, r in finder_new._astar(p0f, p1f))
assert dev_new <= 2, f"新融合应死贴梯度线，实测偏差 {dev_new}px"
assert dev_old > dev_new, (dev_old, dev_new)
print(f"[16] 证据严格次级 ok（晕带劫持 {dev_old}px → 锁线 {dev_new}px；"
      "T14 盲区救回仍有效）")

# ==== T17 磁力挖：poly_default='erase' 默认挖除（v0.9.7 多边形挖升级） ====
me = map_tools.MagneticSplitTool(mcv, wb, "poly", poly_default="erase")
assert me.mode == "poly" and me.poly_mode == "erase", "默认即挖除"
assert me.rb_color == "red", "初始橡皮带应为红（挖除）"

# 1) 直线兜底落地为挖除：在纯净底板区扣 400×400（避开 T15 已挖/已补区）
me._engine = None
me.anchors = [QgsPointXY(1000.0, 1000.0), QgsPointXY(1400.0, 1000.0),
              QgsPointXY(1400.0, 1400.0), QgsPointXY(1000.0, 1400.0)]
me.segs = {}
_before = sum(f.geometry().area()
              for f in wb.layer.getFeatures(wb._cell_filter()))
me._do_finish()
_after = sum(f.geometry().area()
             for f in wb.layer.getFeatures(wb._cell_filter()))
assert abs((_before - _after) - 400 * 400) < 1.0, \
    f"磁力挖应净扣 400×400，实测 {_before - _after:.0f}"
assert [r["kind"] for r in wb.ledger.cell_rows("cell_1600")][-1] == "erase"
assert any("[挖除]" in m for m in LOGS)
assert not me.anchors and not me.segs, "落地后笔状态应清空"

# 2) 1/2 仍可切换回补画
k1 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_1,
               Qt.KeyboardModifier.NoModifier, "1")
me.keyPressEvent(k1)
assert me.poly_mode == "add"
print("[17] 磁力挖 ok（默认红带挖除、净扣 400²、kind=erase、1/2 可切换）")

shutil.rmtree(WORK, ignore_errors=True)
print(f"\n全部 17 组测试通过 ✓  (工作目录已清理)")
sys.exit(0)
