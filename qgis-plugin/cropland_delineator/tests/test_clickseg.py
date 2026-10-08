# 点选分割（SAM）无头测试：stub worker 协议 / 掩码多边形化 / 影像窗口 /
# 工具端到端（预览→补画/挖除进台账）/ 生命周期
# 运行：cd qgis-plugin/cropland_delineator && \
#   QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/share/qgis/python:../.. \
#   python3 tests/test_clickseg.py
import base64
import os
import sys
import tempfile
import time

WORK = tempfile.mkdtemp(prefix="wb_clickseg_")

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsRasterLayer, QgsField,
    QgsFeature, QgsGeometry, QgsPointXY, QgsMapSettings,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter,
)
from qgis.PyQt.QtCore import QMetaType, Qt, QThread
from qgis.gui import QgsMapCanvas

app = QgsApplication([], False)
app.initQgis()

sys.path.insert(0, "/home/luo/projects/202608/alpha/qgis-plugin")
from cropland_delineator import annotate, clickseg, map_tools

LOGS = []


def log(msg):
    LOGS.append(str(msg))
    print("   |", msg)


# ---------- 脚手架：渔网 1 格 1000×1000（EPSG:4550）+ 对齐影像 ----------

def poly(x0, y0, x1, y1):
    return QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x1, y0), QgsPointXY(x1, y1),
        QgsPointXY(x0, y1), QgsPointXY(x0, y0)]])


grid_mem = QgsVectorLayer("polygon?crs=EPSG:4550", "grid", "memory")
grid_mem.dataProvider().addAttributes(
    [QgsField("FID_1", QMetaType.Type.QString)])
grid_mem.updateFields()
gf = QgsFeature(grid_mem.fields())
gf.setAttributes(["seg_A"])
gf.setGeometry(poly(0, 0, 1000, 1000))
grid_mem.dataProvider().addFeature(gf)
grid_path = os.path.join(WORK, "grid.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
opts.layerName = "grid"
QgsVectorFileWriter.writeAsVectorFormatV3(
    grid_mem, grid_path, QgsProject.instance().transformContext(), opts)
grid = QgsVectorLayer(grid_path + "|layername=grid", "grid", "ogr")
QgsProject.instance().addMapLayer(grid)

# 影像：200×200 @5m 覆盖同一格（0..1000, 0..1000），格内两块不同灰度
from osgeo import gdal as _g, osr as _osr

import numpy as np

drv = _g.GetDriverByName("GTiff")
tif = os.path.join(WORK, "img.tif")
ds = drv.Create(tif, 200, 200, 3, _g.GDT_Byte)
ds.SetGeoTransform((0.0, 5.0, 0.0, 1000.0, 0.0, -5.0))
sr = _osr.SpatialReference()
sr.ImportFromEPSG(4550)
ds.SetProjection(sr.ExportToWkt())
a = np.full((200, 200, 3), 90, np.uint8)
a[40:120, 30:150] = (150, 160, 110)   # 目标块：行 200-600m、列 150-750m
for b in range(1, 4):
    ds.GetRasterBand(b).WriteArray(a[:, :, b - 1])
ds = None
raster = QgsRasterLayer(tif, "img", "gdal")
assert raster.isValid()
QgsProject.instance().addMapLayer(raster)


class FakeCanvas:
    def __init__(self):
        self._ms = QgsMapSettings()
        self._ms.setDestinationCrs(
            QgsCoordinateReferenceSystem("EPSG:4550"))

    def mapSettings(self):
        return self._ms

    def setExtent(self, ext):
        pass

    def refresh(self):
        pass


canvas = QgsMapCanvas()


class FakeIface:
    def mapCanvas(self):
        return canvas

    def activeLayer(self):
        return None


wb = annotate.AnnotationController(FakeIface(), log)
wb.dialog = type("D", (), {"grid_layer": staticmethod(lambda: grid)})()
assert wb.open_layer(os.path.join(WORK, "耕地标注.gpkg"))
wb.raster_layer = raster
grid.selectByIds([1])
wb.set_cell_from_selection()
assert wb.current_cell == "seg_A"


def cell_state():
    feats = list(wb.layer.getFeatures(wb._cell_filter()))
    return (len(feats), sum(f.geometry().area() for f in feats
                            if f.hasGeometry()))


def wait_until(cond, timeout=15.0, why=""):
    t0 = time.time()
    while not cond():
        app.processEvents()
        QThread.msleep(10)
        if time.time() - t0 > timeout:
            raise AssertionError(f"等待超时：{why}")


# ==== T1 环境发现：开发布局下能找到 nn/（.venv + worker + 权重） ====
nn_dir = clickseg.find_nn_dir()
assert nn_dir is not None and os.path.basename(nn_dir) == "nn", nn_dir
py, argv = clickseg.service_argv(nn_dir)
assert os.path.isfile(py) and argv[0].endswith("sam_click.py")
print(f"[1] nn/ 环境发现 ok（{nn_dir}）")

# ==== T2 stub worker：服务协议（embed→predict 循环、ready 事件、缓存） ====
stub = os.path.join(WORK, "stub_worker.py")
with open(stub, "w") as f:
    f.write('''
import base64, json, os, sys
import numpy as np

def reply(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()

reply({"ok": True, "event": "ready", "device": "stub",
       "pyhome": os.environ.get("PYTHONHOME")})
for line in sys.stdin:
    req = json.loads(line)
    if req["cmd"] == "embed":
        img = np.load(req["npy"])
        reply({"ok": True, "h": int(img.shape[0]), "w": int(img.shape[1])})
    elif req["cmd"] == "predict":
        pos = [p for p, l in zip(req["points"], req["labels"]) if l == 1]
        cs = [p[0] for p in pos]; rs = [p[1] for p in pos]
        c0 = max(int(min(cs)) - 15, 0); c1 = min(int(max(cs)) + 15, 4096)
        r0 = max(int(min(rs)) - 15, 0); r1 = min(int(max(rs)) + 15, 4096)
        m = np.zeros((r1, c1), bool)
        m[r0:r1, c0:c1] = True
        h, w = m.shape
        reply({"ok": True,
               "mask": base64.b64encode(np.packbits(m)).decode(),
               "score": 0.9, "h": h, "w": w})
''')
res = {}
# 模拟 QGIS（Windows）污染环境：worker 里不应看到 PYTHONHOME/PYTHONPATH
os.environ["PYTHONHOME"] = "/bogus/qgis/python"
os.environ["PYTHONPATH"] = "/bogus/qgis/libs"
svc = clickseg.SamClickService(
    sys.executable, [stub], log)
wait_until(lambda: svc.device == "stub", why="ready 事件")
del os.environ["PYTHONHOME"]
del os.environ["PYTHONPATH"]
assert svc._ready_pyhome is None or svc._ready_pyhome == "", \
    "worker 不应看到污染的 PYTHONHOME"
print("[2] 服务协议 ok（ready/embed/缓存命中/predict 掩码矩形；环境变量已隔离）")
npy_probe = os.path.join(WORK, "probe.npy")
np.save(npy_probe, np.zeros((64, 64, 3), np.uint8))
svc.embed(npy_probe, lambda r: res.__setitem__("embed", r), key="probe1")
wait_until(lambda: "embed" in res, why="embed 响应")
assert res["embed"]["ok"] and res["embed"]["h"] == 64
svc.embed(npy_probe, lambda r: res.__setitem__("embed2", r), key="probe1")
assert res["embed2"].get("cached") is True, "同 key 应直接走缓存"
svc.predict([[32, 32]], [1], True,
            lambda r: res.__setitem__("pred", r))
wait_until(lambda: "pred" in res, why="predict 响应")
m = clickseg.unpack_mask(res["pred"]["mask"], res["pred"]["h"], res["pred"]["w"])
assert m[17:47, 17:47].all() and not m[:15, :].any()

# ==== T3 掩码→多边形：矩形+洞、双块取含正点者、化简 ====
mask = np.zeros((64, 64), np.uint8)
mask[10:40, 10:50] = 1
mask[20:30, 20:30] = 0            # 中间挖洞
b64 = base64.b64encode(np.packbits(mask)).decode()
geoms = clickseg.mask_to_geometry(b64, 64, 64, 0.0, 640.0, 10.0, 10.0)
assert len(geoms) == 1, geoms
g = geoms[0]
ring = g.asPolygon()
assert ring and len(ring) == 2, "应有外环+内环（洞）"
exp_area = (30 * 40 - 10 * 10) * 100.0
assert abs(g.area() - exp_area) < 1e-6, (g.area(), exp_area)

mask2 = np.zeros((64, 64), np.uint8)
mask2[5:15, 5:15] = 1             # 左上小块
mask2[40:60, 40:60] = 1           # 右下大块
geoms2 = clickseg.mask_to_geometry(
    base64.b64encode(np.packbits(mask2)).decode(), 64, 64, 0.0, 640.0, 10.0, 10.0)
assert len(geoms2) == 2
picked = clickseg.pick_blob(geoms2, [QgsPointXY(100.0, 540.0)], 10.0)  # 左上块内（行10列10）
assert picked and abs(picked.area() - 10 * 10 * 100.0) < 1e-3, picked.area()
picked2 = clickseg.pick_blob([], [QgsPointXY(1, 1)], 10.0)
assert picked2 is None
print("[3] 掩码多边形化 ok（洞内环/双块含点选择/空掩码）")

# ==== T4 影像窗口：格 bbox → npy（像素对齐 + 2-98 拉伸） ====
meta = clickseg.build_window_npy(
    raster, poly(0, 0, 1000, 1000).boundingBox(),
    QgsCoordinateReferenceSystem("EPSG:4550"),
    os.path.join(WORK, "win.npy"), margin_px=0)
arr = np.load(os.path.join(WORK, "win.npy"))
assert (meta["h"], meta["w"]) == (200, 200) and arr.shape == (200, 200, 3)
assert meta["x0"] == 0.0 and meta["y0"] == 1000.0
assert meta["pw"] == 5.0 and meta["ph"] == 5.0
assert arr.dtype == np.uint8
print("[4] 影像窗口 ok（200×200 像元对齐，uint8 RGB）")

# ==== T5 工具端到端：点一下 → 掩码预览 → Enter 补画 / 挖除进台账 ====
# 把控制器的服务换成 stub（绕过真 GPU；窗口构建/坐标/台账全走真实代码）
wb_click = wb
svc_stub = svc
wb_click._sam = svc_stub

tool = map_tools.ClickSegTool(canvas, wb_click)
canvas.setMapTool(tool)
tool.set_lock(False)   # T5~T5c 先跑老行为（锁面回归见 T8+；3 键开关在 T12）

def do_click(x, y, label=1, alt=False):
    tool.add_point(QgsPointXY(x, y), label)
    wait_until(lambda: tool.mask_geom is not None,
               why=f"掩码预览({x},{y})")

# 目标块中心（行 200-600m、列 150-750m → 地图 x=350+, y=600- …取中心 x≈450,y≈400）
n0, area0 = cell_state()
do_click(450.0, 400.0)
assert tool.mask_geom is not None
# stub 掩码 = 正点 ±15px = ±75m → 375..525 × 325..475
bb = tool.mask_geom.boundingBox()
assert abs(bb.xMinimum() - 375.0) < 1e-6 and abs(bb.xMaximum() - 525.0) < 1e-6, bb.toString()
assert abs(bb.yMinimum() - 325.0) < 1e-6 and abs(bb.yMaximum() - 475.0) < 1e-6

# Backspace 退点 → 预览清空
tool.remove_last()
assert tool.points == [] and tool.mask_geom is None

# 补画落地（mode 默认 add；底板 1 块 + 补画 1 块）
do_click(450.0, 400.0)
tool._finish()
n1, area1 = cell_state()
assert (n1, round(area1 - area0)) == (2, 150 * 150), (n1, area1 - area0)
kinds = [r["kind"] for r in wb.ledger.cell_rows("seg_A")]
assert kinds == ["base", "add"], kinds

# 挖除落地（mode=erase）：补的块整块消失 + 底板打洞 = 共减 2×150×150
tool._set_mode("erase")
assert tool.mode == "erase"
do_click(450.0, 400.0)
tool._finish()
n2, area2 = cell_state()
assert (n2, round(area1 - area2)) == (1, 2 * 150 * 150), (n2, area1 - area2)
assert round(area0 - area2) == 150 * 150, "净效果=底板留一个 150×150 洞"
kinds = [r["kind"] for r in wb.ledger.cell_rows("seg_A")]
assert kinds == ["base", "add", "erase"], kinds
print("[5] 点选端到端 ok（预览对齐 / 退点 / 补画+挖除进台账）")

# ==== T5b Ctrl+Z 梯级：有落点先退点（不碰图层）；退光后才撤销上一笔落地 ====
from qgis.PyQt.QtCore import QEvent
from qgis.PyQt.QtGui import QKeyEvent
from qgis.PyQt.QtWidgets import QWidget


class _Iface2:
    def mapCanvas(self):
        return canvas

    def mainWindow(self):
        return _dmw

    def activeLayer(self):
        return None


_dmw = QWidget()
dock2 = annotate.AnnotateDock(_Iface2())
dock2.wb = wb  # 过滤器的"原生撤销"分支落到本控制器
_dmw.show()
dock2.show()
app.processEvents()


def press_ctrl_z(widget):
    app.postEvent(widget, QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Z,
        Qt.KeyboardModifier.ControlModifier, "z"))
    for _ in range(5):
        app.processEvents()


n_before, area_before = cell_state()          # T5 收尾：(1, 977500)
do_click(200.0, 200.0)
assert tool.has_stroke()
press_ctrl_z(dock2)                            # 侧栏过滤器路径
assert tool.points == [] and not tool.has_stroke(), "第一击应退掉落点"
assert cell_state() == (n_before, area_before), "退点不得动图层"

press_ctrl_z(dock2)                            # 无落点 → 撤销上一笔落地（erase）
n1b, a1b = cell_state()
assert (n1b, round(a1b)) == (2, 1022500), (n1b, a1b)

# 画布焦点路径（基类 keyPressEvent 梯级，过滤器失效时的兜底）
do_click(300.0, 300.0)
_ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier, "z")
tool.keyPressEvent(_ev)
assert tool.points == [] and (cell_state() == (n1b, a1b)), "基类路径同样先退点"
tool.keyPressEvent(_ev)                        # 退光 → wb.undo 撤销 add
assert cell_state() == (1, 1000000.0), cell_state()

# 右键=负点：add_point(label=0) 后预览仍能刷新（stub 只按正点出矩形）
tool.add_point(QgsPointXY(300.0, 300.0), 1)
wait_until(lambda: tool.mask_geom is not None, why="正点预览")
tool.add_point(QgsPointXY(310.0, 290.0), 0)
wait_until(lambda: tool.mask_geom is not None, why="负点后预览")
assert len(tool.points) == 2
tool._reset()
print("[5b] Ctrl+Z 梯级 ok（先退落点不动图层→退光才撤落地；基类路径同语义；负点预览）")

# ==== T5c ShortcutOverride 全量认领：原生撤销 QShortcut 作用于"当前活动
# 图层"，标注时活动层常是影像，放行=空操作（v0.8.11 实机事故：无落点按
# Ctrl+Z 毫无反应）。工具激活期间 Ctrl+Z 一律认领，按键回流同一梯级 ====
def override_ctrl_z():
    return QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z,
                     Qt.KeyboardModifier.ControlModifier, "z")

n0c, a0c = cell_state()
do_click(350.0, 350.0)
_ov = override_ctrl_z()
assert dock2.eventFilter(dock2, _ov) is True and _ov.isAccepted(), \
    "有落点：Ctrl+Z 的 ShortcutOverride 必须认领"
press_ctrl_z(dock2)   # 认领后按键以普通 KeyPress 回流同一梯级
assert tool.points == [] and cell_state() == (n0c, a0c), "只退点，不动图层"

_ov = override_ctrl_z()   # 无落点：同样认领（撤上一笔落地走标注层 undoStack）
assert dock2.eventFilter(dock2, _ov) is True and _ov.isAccepted()
press_ctrl_z(dock2)        # → elif 分支 wb.undo（测试环境无活动图层概念，直打栈）
assert cell_state() == (0, 0), cell_state()   # 台账只剩 base，撤空即证明撤销生效
press_ctrl_z(dock2)        # 栈空：不再崩、状态不变（人话日志路径）
assert cell_state() == (0, 0)
print("[5c] ShortcutOverride 全量认领 ok（有落点退点；无落点认领后 wb.undo 真撤销；栈空安全）")

# ==== 锁面（v0.10.0）：地块矢量对点选范围形成约束 ====
# T5c 结束时全部撤销（0,0）。重建底板 → 挖一条 20m 路缝 → 左右两块。

def feats_by_x():
    return sorted(wb.layer.getFeatures(wb._cell_filter()),
                  key=lambda f: f.geometry().boundingBox().xMinimum())


from qgis.core import QgsFeatureRequest  # noqa: E402


def geom_of(fid):
    """按 fid 取现几何（要素快照会过期：挖洞/切分都换几何）。"""
    f = next(x for x in wb.layer.getFeatures(
        QgsFeatureRequest().setFilterExpression("cell_id = 'seg_A'"))
        if x.id() == fid)
    return QgsGeometry(f.geometry())


def area_of(fid):
    return geom_of(fid).area()

tool.set_lock(True)
assert tool.lock
assert wb.set_cell_from_selection()      # 底板重建（此前已撤光）
assert cell_state() == (1, 1000000.0)
wb.apply_difference(poly(490, 0, 510, 1000), "erase")   # 路缝 → 左右两块
fL, fR = feats_by_x()
assert abs(fL.geometry().area() - 490000) < 1e-6
assert abs(fR.geometry().area() - 490000) < 1e-6

# ==== T8 锁面挖除：溢出掩码被裁进点中的地块，邻居分毫不动 ====
tool._set_mode("erase")
do_click(470.0, 400.0)                    # 点在左块内（贴路缝）
assert tool._face is not None and tool._face.id() == fL.id(), "锁面=左块"
assert any("已锁面" in m for m in LOGS)
assert wb._clickseg_key[1] == "face" and wb._clickseg_key[2] == fL.id()
# stub 掩码=点±15px(±75m)=395..545×325..475，跨过路缝伸进右块；
# 锁面裁剪后只留 395..490（左块的东界）×325..475
bb = tool.mask_geom.boundingBox()
assert abs(bb.xMinimum() - 395.0) < 1e-6 and abs(bb.xMaximum() - 490.0) < 1e-6, bb.toString()
assert abs(bb.yMinimum() - 325.0) < 1e-6 and abs(bb.yMaximum() - 475.0) < 1e-6
tool._finish()
assert cell_state()[0] == 2
assert abs(area_of(fR.id()) - 490000) < 1e-6, "右块必须分毫未动"
assert abs(area_of(fL.id()) - (490000 - 95 * 150)) < 1e-6
rows = wb.ledger.cell_rows("seg_A")
assert rows[-1]["kind"] == "erase"
# 台账记录的是裁剪后的 wkt（重放天然一致，不依赖当时地块形状）
gw = QgsGeometry.fromWkt(rows[-1]["geom"])
assert abs(gw.boundingBox().xMaximum() - 490.0) < 1e-6, "台账=裁剪后几何"
print("[8] 锁面挖除 ok（掩码裁进点中地块；邻居不动；台账记裁剪后 wkt）")

# ==== T9 补画只补空隙：贴左块洞边、贴右块路缝边，零重叠零缝 ====
tool._set_mode("add")
do_click(445.0, 400.0)                    # 左块洞内（T8 挖出的空隙）
# stub blob=370..520×325..475；空隙=洞(395..490)+路缝(490..510) → 395..510
bb = tool.mask_geom.boundingBox()
assert abs(bb.xMinimum() - 395.0) < 1e-6 and abs(bb.xMaximum() - 510.0) < 1e-6, bb.toString()
assert abs(bb.yMinimum() - 325.0) < 1e-6 and abs(bb.yMaximum() - 475.0) < 1e-6
tool._finish()
n9, a9 = cell_state()
assert n9 == 3 and abs(a9 - (980000 - 14250 + 115 * 150)) < 1e-6, (n9, a9)
adds = [f for f in wb.layer.getFeatures(wb._cell_filter())
        if f.attribute("source") == "add"]
assert len(adds) == 1, "本笔应只新增一个补块"
fnew = adds[0]
gL, gR2 = geom_of(fL.id()), geom_of(fR.id())   # 现几何（fL/fR 是旧快照）
assert fnew.geometry().intersection(gL).area() < 1e-6, "与左块零重叠"
assert fnew.geometry().intersection(gR2).area() < 1e-6, "与右块零重叠"
assert fnew.geometry().distance(gL) < 1e-9, "贴左块洞边零缝"
assert fnew.geometry().distance(gR2) < 1e-9, "贴右块西边零缝（跨路缝对接）"
print("[9] 补画裁空隙 ok（零重叠；两侧贴边零缝）")

# ==== T10 锁面窗口：bbox+自适应边距；同面 bbox 不变则复用缓存 ====
fR_feat = next(f for f in wb.layer.getFeatures(
    QgsFeatureRequest().setFilterExpression("cell_id = 'seg_A'"))
    if f.id() == fR.id())
ctx_f = wb.clickseg_context(fR_feat)
_npy, meta_f, _xf = ctx_f
# 右块 bbox 510..1000×0..1000，短边 98px → 边距 24px(120m)，裁到影像界内
assert abs(meta_f["x0"] - 390.0) < 1e-6 and meta_f["w"] == 122, meta_f
assert abs(meta_f["y0"] - 1000.0) < 1e-6 and meta_f["h"] == 200
assert wb.clickseg_context(fR_feat) is ctx_f, "同面未动边 → 窗口缓存复用"
wb.clickseg_context()
assert wb._clickseg_key[1] != "face", "无 face 参数 → 回整格窗口"
print("[10] 锁面窗口 ok（bbox+24px 边距=390 起；缓存复用；回格窗口）")

# ==== T11 拦截：空隙挖除 / 地块内补画 —— 写日志不落地 ====
wb.apply_difference(poly(600, 100, 700, 200), "erase")   # 右块内开洞
n11, a11 = cell_state()
tool._set_mode("erase")
tool.add_point(QgsPointXY(650.0, 150.0), 1)   # 点在新洞（空隙）里
assert tool.mask_geom is None and tool._blocked
assert any("挖除点在空隙" in m for m in LOGS)
assert cell_state() == (n11, a11), "拦下不得动图层"
tool._reset()
tool._set_mode("add")
tool.add_point(QgsPointXY(800.0, 500.0), 1)   # 点在右块实心内
assert tool.mask_geom is None and tool._blocked
assert any("补画点在了地块内部" in m for m in LOGS)
assert cell_state() == (n11, a11)
tool._reset()
print("[11] 拦截 ok（空隙挖除/地块内补画：人话提示、零副作用）")

# ==== T12 3 键开关：关锁恢复老行为（跨缝溢出照挖）；再开 ====
from qgis.PyQt.QtCore import QSettings as _QS

tool._set_mode("erase")
ev3 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_3,
                Qt.KeyboardModifier.NoModifier, "3")
tool.keyPressEvent(ev3)
assert not tool.lock and not _QS().value(
    "cropland_delineator/clickseg_lock", True, type=bool)
aL0, aR0 = area_of(fL.id()), area_of(fR.id())
tool.add_point(QgsPointXY(495.0, 600.0), 1)    # 点在路缝（空隙，y=600 两侧均实心）
wait_until(lambda: tool.mask_geom is not None, why="关锁老行为掩码")
bb = tool.mask_geom.boundingBox()
assert bb.xMinimum() < 490 and bb.xMaximum() > 510, "未裁剪=跨缝溢出"
assert tool._face is None
tool._finish()
assert area_of(fL.id()) < aL0 and area_of(fR.id()) < aR0, "老行为切到两侧邻居"
wb.undo()
assert abs(area_of(fL.id()) - aL0) < 1e-6 and abs(area_of(fR.id()) - aR0) < 1e-6
tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_3,
                             Qt.KeyboardModifier.NoModifier, "3"))
assert tool.lock
print("[12] 锁面开关 ok（关=老溢出行为可复现可撤销；开=QSettings 记住）")

# ==== T13 单选优先：标注层单选的要素作为锁面，压过点击所在地块 ====
wb.layer.select(fR.id())
tool._set_mode("erase")
tool.add_point(QgsPointXY(470.0, 400.0), 1)    # 点在 T9 补块内（非右块）
wait_until(lambda: tool.mask_geom is not None, why="单选锁面掩码")
assert tool._face is not None and tool._face.id() == fR.id(), "单选优先"
# 裁进右块：blob 395..545 ∩ 右 = 510..545×325..475
bb = tool.mask_geom.boundingBox()
assert abs(bb.xMinimum() - 510.0) < 1e-6 and abs(bb.xMaximum() - 545.0) < 1e-6, bb.toString()
aL1 = area_of(fL.id())
tool._finish()
assert abs(area_of(fL.id()) - aL1) < 1e-6, "左块与补块不受影响"
assert abs(area_of(fR.id()) - (aR0 - 35 * 150)) < 1e-6, "只有右块被削"
wb.layer.removeSelection()
print("[13] 单选优先 ok（锁面=选中的右块；裁剪/落地只动它）")

# ==== T14 密格性能 + 点哪补哪 + 差集预检（v0.10.1 朋友实机卡死回归） ====
# 右块中部造两块互不连通的空隙 A/B；blob 同罩 A、B 与中间地块
wb.apply_difference(poly(750, 100, 800, 150), "erase")
wb.apply_difference(poly(750, 300, 800, 350), "erase")
blob = poly(740, 90, 810, 360)
t0 = time.time()
clipped = wb.free_space_clip(blob, keep_at=QgsPointXY(775.0, 125.0))
el14 = time.time() - t0
parts14 = annotate.poly_parts(clipped) if not clipped.isEmpty() else []
assert len(parts14) == 1, f"点哪补哪：只留含点连通块，得 {len(parts14)}"
bb14 = clipped.boundingBox()
assert abs(bb14.xMinimum() - 750) < 1 and abs(bb14.xMaximum() - 800) < 1
assert abs(bb14.yMinimum() - 100) < 1 and abs(bb14.yMaximum() - 150) < 1, bb14
assert el14 < 3.0, f"空隙裁剪应走索引+并集（实测 {el14:.2f}s）"
# keep_at 不落在任何空隙 → 兜底取最大块（与 pick_blob 同哲学）
clipped2 = wb.free_space_clip(blob, keep_at=QgsPointXY(775.0, 225.0))
assert len(annotate.poly_parts(clipped2)) == 1
# 差集预检：擦子不碰任何地块 → 影响 0、不进台账、图层不动
rows_before = len(wb.ledger.cell_rows("seg_A"))
n14, a14 = cell_state()
wb.apply_difference(poly(2000, 2000, 2100, 2100), "erase")   # 格外远处
assert cell_state() == (n14, a14), "没碰到就不得动图层"
assert len(wb.ledger.cell_rows("seg_A")) == rows_before, "影响 0 不进台账"
print("[14] 密格性能+点哪补哪+差集预检 ok（索引+并集；blob 只补点中缝；0 影响零台账）")

# ==== T6 生命周期：关会话 → 进程退出 + 临时目录清理 ====
svc_pid_alive = svc_stub.alive()
assert svc_pid_alive
wb._shutdown_sam()
assert not svc_stub.alive(), "worker 进程应已退出"
assert wb._sam is None and wb._sam_tmp is None
wait_until(lambda: svc_stub._proc.poll() is not None, timeout=5, why="进程收尸")
print("[6] 生命周期 ok（shutdown 收进程、清临时目录）")

# ==== T7 符号链接安装路径：QGIS profile 软链进来也能找到 nn/（真坑回归） ====
# v0.7.1 实机事故：abspath 对软链路径只做词法规范化，../.. 折回 profile 下
# 不存在路径 → service_argv glob 扑空 IndexError。realpath 修复。
import os as _os

root = _os.path.join(WORK, "sym")
real_plugin = _os.path.join(root, "alpha", "qgis-plugin", "cropland_delineator")
real_nn = _os.path.join(root, "alpha", "nn")
_os.makedirs(_os.path.join(real_nn, ".venv", "bin"))
_os.makedirs(_os.path.join(real_nn, "ckpt"))
open(_os.path.join(real_nn, ".venv", "bin", "python"), "w").close()
open(_os.path.join(real_nn, "sam_click.py"), "w").close()
open(_os.path.join(real_nn, "ckpt", "sam_vit_b_01ec64.pth"), "w").close()
_os.makedirs(real_plugin)
profile_plugin = _os.path.join(root, "profile", "plugins", "cropland_delineator")
_os.makedirs(_os.path.dirname(profile_plugin))
_os.symlink(real_plugin, profile_plugin)

_saved_file = clickseg.__file__
try:
    clickseg.__file__ = _os.path.join(profile_plugin, "clickseg.py")
    got = clickseg.find_nn_dir()
    assert got == real_nn, f"软链路径应解析到真实 nn/：{got}"
    py, argv = clickseg.service_argv(got)
    assert py.startswith(real_nn) and argv[0].startswith(real_nn)

    # QSettings 指定优先于相对路径（朋友一行代码指路的机制）
    from qgis.PyQt.QtCore import QSettings

    nn2 = _os.path.join(WORK, "sym2")
    _os.makedirs(_os.path.join(nn2, ".venv", "bin"))
    _os.makedirs(_os.path.join(nn2, "ckpt"))
    open(_os.path.join(nn2, ".venv", "bin", "python"), "w").close()
    open(_os.path.join(nn2, "sam_click.py"), "w").close()
    open(_os.path.join(nn2, "ckpt", "sam_vit_b_x.pth"), "w").close()
    QSettings().setValue("cropland_delineator/nn_dir", nn2)
    try:
        assert clickseg.find_nn_dir() == nn2, "QSettings 指定应优先"
    finally:
        QSettings().remove("cropland_delineator/nn_dir")
    assert clickseg.find_nn_dir() == real_nn, "清除后回落相对路径"

    # Windows 布局 venv（.venv\Scripts\python.exe）也能被发现并取到
    nn3 = _os.path.join(WORK, "win_layout")
    _os.makedirs(_os.path.join(nn3, ".venv", "Scripts"))
    _os.makedirs(_os.path.join(nn3, "ckpt"))
    open(_os.path.join(nn3, ".venv", "Scripts", "python.exe"), "w").close()
    open(_os.path.join(nn3, "sam_click.py"), "w").close()
    open(_os.path.join(nn3, "ckpt", "sam_vit_b_x.pth"), "w").close()
    py3, argv3 = clickseg.service_argv(nn3)
    assert py3.endswith(("Scripts/python.exe", "Scripts\\python.exe")), py3
finally:
    clickseg.__file__ = _saved_file
print("[7] 软链安装路径 ok（realpath 解到真实 nn/；QSettings 指路优先）")

# ==== T6b 启动即崩：stderr 透传 + 人话提示，且不得有作用域错误 ====
# （v0.8.4 曾把 _err 泵三行错挤进 _died_early，log 脱离作用域 → NameError 弹窗）
svc._err  # 构造期就应存在 stderr 泵（结构断言）
boom = os.path.join(WORK, "boom_worker.py")
with open(boom, "w") as f:
    f.write("import sys\n"
            "sys.stderr.write('boom traceback line\\n'); sys.stderr.flush()\n"
            "sys.exit(1)\n")
svc2 = clickseg.SamClickService(sys.executable, [boom], log)
wait_until(lambda: any("启动即退出" in m for m in LOGS), why="崩溃提示")
wait_until(lambda: any("[SAM] boom" in m for m in LOGS), why="stderr 透传")
assert svc2.device is None
svc2.shutdown()
print("[6b] 启动即崩 ok（stderr 进日志 + 人话提示 + 无 NameError）")

import shutil
shutil.rmtree(WORK, ignore_errors=True)
from qgis.PyQt.QtCore import QSettings as _QS2  # noqa: E402
_QS2().remove("cropland_delineator/clickseg_lock")   # 不把测试态留给实机
print(f"\n点选分割 14 组测试通过 ✓  (工作目录已清理)")
sys.exit(0)
