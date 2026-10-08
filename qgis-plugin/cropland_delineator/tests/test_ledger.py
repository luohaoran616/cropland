# 操作台账无头测试：记录链 / 回退重放 / 改宽重放 / OSM 分组 / 撤销对齐 / 存量基线
# 运行：cd qgis-plugin/cropland_delineator && \
#   QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/share/qgis/python python3 tests/test_ledger.py
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="wb_ledger_")

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsField, QgsFeature,
    QgsGeometry, QgsPointXY, QgsMapSettings, QgsCoordinateReferenceSystem,
    QgsVectorFileWriter, QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QMetaType, QSettings, Qt
app = QgsApplication([], False)
app.initQgis()

sys.path.insert(0, "/home/luo/projects/202608/alpha/qgis-plugin")
from cropland_delineator import annotate, ledger as ops_ledger

LOGS = []


def log(msg):
    LOGS.append(str(msg))
    print("   |", msg)


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


class FakeIface:
    def mapCanvas(self):
        return canvas

    def activeLayer(self):
        return None


canvas = FakeCanvas()
iface = FakeIface()
wb = annotate.AnnotationController(iface, log)


def poly(x0, y0, x1, y1):
    return QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x1, y0), QgsPointXY(x1, y1),
        QgsPointXY(x0, y1), QgsPointXY(x0, y0)]])


def cell_state(cid):
    # 真按 cid 过滤（勿用 wb._cell_filter——那按“当前格”过滤，cid 会被
    # 静默忽略，跨格断言全数失真）；旧测试全在“当前格=查询格”下跑，
    # 行为不变
    feats = list(wb.layer.getFeatures(
        QgsFeatureRequest().setFilterExpression(f"cell_id = '{cid}'")))
    return (len(feats),
            sum(f.geometry().area() for f in feats if f.hasGeometry()))


# ---- 渔网 2 格 1000x1000，EPSG:4550 ----
grid_mem = QgsVectorLayer("polygon?crs=EPSG:4550", "grid", "memory")
grid_mem.dataProvider().addAttributes(
    [QgsField("FID_1", QMetaType.Type.QString)])
grid_mem.updateFields()
for cid, ox in (("led_A", 0.0), ("led_B", 2000.0)):
    gf = QgsFeature(grid_mem.fields())
    gf.setAttributes([cid])
    gf.setGeometry(poly(ox, 0, ox + 1000, 1000))
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
sidecar = os.path.join(WORK, "耕地标注_ops.jsonl")
assert wb.ledger is not None and os.path.exists(sidecar)

# ==== T1 记录链：建底板 → 道路/框挖/补画/切分 各一步 ====
grid.selectByIds([1])   # led_A
wb.set_cell_from_selection()
assert wb.current_cell == "led_A"
rows = wb.ledger.cell_rows("led_A")
assert len(rows) == 1 and rows[0]["kind"] == "base", rows

wb.apply_road(QgsGeometry.fromPolylineXY(
    [QgsPointXY(100, 500), QgsPointXY(900, 500)]))     # 800m×25m
n, area_road = cell_state("led_A")
assert (n, round(area_road)) == (1, 980000), (n, area_road)

wb.apply_difference(poly(400, 100, 600, 300), "rect")  # 40000 m²
n, area_rect = cell_state("led_A")
assert (n, round(area_rect)) == (1, 940000), (n, area_rect)

wb.apply_add(poly(450, 150, 550, 250))                  # 10000 m²
n, area_add = cell_state("led_A")
assert (n, round(area_add)) == (2, 950000), (n, area_add)

wb.apply_split(QgsGeometry.fromPolylineXY(
    [QgsPointXY(-50, 700), QgsPointXY(1050, 700)]))
n, area_split = cell_state("led_A")
assert n == 3 and abs(area_split - 950000) < 1e-6, (n, area_split)

kinds = [r["kind"] for r in wb.ledger.cell_rows("led_A")]
assert kinds == ["base", "road", "rect", "add", "split"], kinds
widths = [r["params"].get("width") for r in wb.ledger.cell_rows("led_A")
          if r["kind"] == "road"]
assert widths == [25.0], widths
print("[1] 记录链 ok（base/road/rect/add/split 五步，几何均正确）")

# ==== T2 回退重放：回到任意步，面积逐级还原，台账截断 ====
assert wb.ledger_rollback("led_A", 2)
n, area = cell_state("led_A")
assert (n, round(area)) == (1, round(area_road)), (n, area)
assert wb.layer.undoStack().canUndo() is False, "回退后撤销栈应已作废"
assert wb.ledger_rollback("led_A", 1)
assert cell_state("led_A") == (1, 1000000.0), cell_state("led_A")
assert len(wb.ledger.cell_rows("led_A")) == 1
print("[2] 回退重放 ok（步2/步1 精确还原，后续步作废）")

# ==== T3 改宽重放（P2）：道路 25→40m，保留后续挖除步 ====
wb.apply_road(QgsGeometry.fromPolylineXY(
    [QgsPointXY(100, 500), QgsPointXY(900, 500)]))     # 重新记为步2
wb.apply_difference(poly(400, 100, 600, 300), "rect")  # 步3
assert wb.ledger_set_width("led_A", 2, 40.0)
n, area = cell_state("led_A")
assert (n, round(area)) == (1, 1_000_000 - 800 * 40 - 40_000), (n, area)
row2 = [r for r in wb.ledger.cell_rows("led_A") if r["seq"] == 2][0]
assert row2["params"]["width"] == 40.0
print("[3] 改宽重放 ok（25→40m，后续步保留）")

# ==== T4 OSM 分组：逐路入账同一步号，回退到 OSM 前 ====
roads = QgsVectorLayer("linestring?crs=EPSG:4550", "roads", "memory")
roads.dataProvider().addAttributes(
    [QgsField("highway", QMetaType.Type.QString)])
roads.updateFields()
for wkt, hw in (("LINESTRING(0 200, 1000 200)", "primary"),
                ("LINESTRING(800 0, 800 1000)", "residential")):
    f = QgsFeature(roads.fields())
    f.setAttributes([hw])
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    roads.dataProvider().addFeature(f)
_area_before_osm = cell_state("led_A")[1]
wb.apply_osm_roads(roads)
osm_rows = [r for r in wb.ledger.cell_rows("led_A") if r["kind"] == "osm"]
assert len(osm_rows) == 2
assert len({r["seq"] for r in osm_rows}) == 1, "同一次 OSM 挖除应共享步号"
removed = _area_before_osm - cell_state("led_A")[1]
# 大路带(25m) 与已挖的框挖缺口(400-600,100-300)重叠少挖 5000，小路 8m
assert 27_000 < removed < 28_200, removed
assert wb.ledger_rollback("led_A", 3)
assert abs(cell_state("led_A")[1] - _area_before_osm) < 1e-6
print(f"[4] OSM 分组 ok（2 条同一步号，挖除 {removed:,.0f} m²，可回退）")

# ==== T5 撤销对齐：Ctrl+Z/撤销一笔后台账同步丢弃 ====
_rows_before = len(wb.ledger.cell_rows("led_A"))
wb.apply_road(QgsGeometry.fromPolylineXY(
    [QgsPointXY(200, -50), QgsPointXY(200, 1050)]))
assert len(wb.ledger.cell_rows("led_A")) == _rows_before + 1
wb.undo()
assert len(wb.ledger.cell_rows("led_A")) == _rows_before, \
    "撤销后台账应同步丢弃该步"
assert abs(cell_state("led_A")[1] - _area_before_osm) < 1e-6
print("[5] 撤销对齐 ok（undo → 台账行同步丢弃，状态一致）")

# ==== T6 存量格隐式基线：无台账时期的历史拍快照，之后可回退 ====
grid.selectByIds([2])   # led_B
wb.set_cell_from_selection()
# 模拟"台账出现之前的编辑"：暂时禁用台账做一笔挖除
wb.ledger.rows = [r for r in wb.ledger.rows if r["cell"] != "led_B"]
wb.ledger.save()
wb.ledger = None
wb.apply_difference(poly(2300, 200, 2500, 400), "rect")
n_legacy, area_legacy = cell_state("led_B")            # 1e6 - 40000
assert round(area_legacy) == 960_000, area_legacy
wb.ledger = ops_ledger.Ledger(sidecar)
wb.apply_add(poly(2350, 250, 2450, 350))               # 触发隐式快照
rows = wb.ledger.cell_rows("led_B")
assert [r["kind"] for r in rows] == ["baseline", "add"], rows
assert len([r for r in rows if r["kind"] == "baseline"]) == n_legacy
assert wb.ledger_rollback("led_B", 1)                  # 回到快照 = 补画之前
assert cell_state("led_B") == (n_legacy, area_legacy), cell_state("led_B")
print("[6] 存量基线 ok（首笔前拍快照，快照之前不可回退、之后可以）")

# ==== T7 工作台 dock 整体构造（4 个应用级快捷键：E/N/M/R；Ctrl+Z 改走事件过滤器） ====
from qgis.PyQt.QtWidgets import QWidget
from qgis.gui import QgsMapCanvas

_mcv = QgsMapCanvas()


class _DlgIface:
    def __init__(self):
        self._mw = QWidget()

    def mapCanvas(self):
        return _mcv

    def mainWindow(self):
        return self._mw

    def activeLayer(self):
        return None


_dlg = annotate.AnnotateDock(_DlgIface())
assert len(_dlg._shortcuts) == 4, len(_dlg._shortcuts)
from qgis.PyQt.QtWidgets import QDockWidget, QGroupBox

assert isinstance(_dlg, QDockWidget)
assert _dlg.widget() is not None, "dock 内容未挂载"
folds = [g for g in _dlg.widget().findChildren(QGroupBox)
         if hasattr(g, "is_folded")]
assert len(folds) == 2 and all(g.is_folded() for g in folds), "默认折叠态"
for g in folds:
    g.set_folded(False)
assert all(not g.is_folded() for g in folds), "展开"
print("[7] 工作台 dock 构造 ok（快捷键 4 个；折叠组 2 个开合正常）")

# ==== T7b 键盘兜底过滤器：焦点不在画布时 Esc 取消 / Backspace·Ctrl+Z 退点 ====
from qgis.PyQt.QtCore import QEvent
from qgis.PyQt.QtGui import QKeyEvent
from qgis.PyQt.QtWidgets import QLineEdit

from cropland_delineator import map_tools


def _press(widget, key, mods=Qt.KeyboardModifier.NoModifier):
    app.postEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, mods))
    for _ in range(3):
        app.processEvents()


_tool = map_tools.MagneticSplitTool(_mcv, _dlg.wb, mode="split")
_mcv.setMapTool(_tool)
_dlg.iface.mainWindow().show()
_dlg.show()
app.processEvents()

_tool.anchors = [QgsPointXY(0.0, 0.0), QgsPointXY(1.0, 1.0), QgsPointXY(2.0, 2.0)]
assert _tool.has_stroke()
_press(_dlg, Qt.Key.Key_Escape)  # 事件发给侧栏本身：焦点不在画布也应取消
assert not _tool.has_stroke(), "Esc 应取消笔迹"

_tool.anchors = [QgsPointXY(0.0, 0.0), QgsPointXY(1.0, 1.0)]
_press(_dlg, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
assert len(_tool.anchors) == 1, "Ctrl+Z 应退一颗锚点"
_press(_dlg, Qt.Key.Key_Backspace)
assert len(_tool.anchors) == 0, "Backspace 应再退一颗"

_le = QLineEdit()
_dlg.widget().widget().layout().addWidget(_le)  # 放进侧栏滚动区主体：同窗口但属输入框
_tool.anchors = [QgsPointXY(0.0, 0.0), QgsPointXY(1.0, 1.0)]
_press(_le, Qt.Key.Key_Backspace)
assert len(_tool.anchors) == 2, "输入框内按键应放行（不打断打字）"

_dlg.hide()
app.processEvents()
_press(_dlg, Qt.Key.Key_Escape)
assert len(_tool.anchors) == 2, "侧栏隐藏时按键应放行"
_dlg.show()

_tool.anchors = []
_press(_dlg, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)  # 无笔迹→原生撤销分支（layer=None 早退，不崩即可）

# QWindow 等非控件目标：直接调过滤器不应抛 'QWindow' object has no attribute 'window'
from qgis.PyQt.QtGui import QWindow

_qw = QWindow()
_kev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
_tool.anchors = [QgsPointXY(0.0, 0.0)]
assert _dlg.eventFilter(_qw, _kev) is False, "非控件目标应原样放行"
assert len(_tool.anchors) == 1, "QWindow 目标的按键不应作用于笔迹"
print("[7b] 键盘兜底过滤器 ok（Esc 取消 / Ctrl+Z·Backspace 退点 / 输入框·隐藏态·QWindow 放行）")

# ==== T8 插件壳：dock 注册/勾选联动/卸载（Qt6 枚举与信号环冒烟） ====
from qgis.PyQt.QtWidgets import QMainWindow

from cropland_delineator import plugin as plugin_mod


class _PluginIface(_DlgIface):
    def __init__(self):
        super().__init__()
        self._mw = QMainWindow()

    def addPluginToVectorMenu(self, *a):
        pass

    def removePluginMenu(self, *a):
        pass

    def addToolBarIcon(self, *a):
        pass

    def removeToolBarIcon(self, *a):
        pass

    def addDockWidget(self, area, dock):
        self._mw.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self._mw.removeDockWidget(dock)


pi = _PluginIface()
pl = plugin_mod.CroplandDelineatorPlugin(pi)
pl.initGui()
assert pl.action_annotate.isCheckable()
# 先放一个模拟 AITracer 的侧栏（同名 dock），工作台应自动与之叠成标签
ai = QDockWidget("AITracer by LAD", pi._mw)
ai.setObjectName("aitracer_probe")
pi._mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, ai)
ai.show()
pl.action_annotate.trigger()  # 首次打开：建 dock 并停靠右区
dock = pl.annotate_dialog
assert isinstance(dock, QDockWidget) and not dock.isHidden()
assert ai in pi._mw.tabifiedDockWidgets(dock), "未与 AITracer 叠成标签"
ai.hide()
dock.hide()  # 模拟用户收起侧栏
assert not pl.action_annotate.isChecked(), "侧栏收起后菜单勾选未同步"
pl.action_annotate.trigger()  # 再勾：走复用分支重新显示
assert not dock.isHidden()
dock.close()  # 侧栏 X：closeEvent（保存会话）+ 勾选同步
assert not pl.action_annotate.isChecked()
pl.unload()  # 卸载不崩、引用断开；dock 应被真正销毁（reload 不残留）
assert pl.annotate_dialog is None
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt import sip

QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
assert pi._mw.findChild(QDockWidget, "cropland_annotate_dock") is None, \
    "unload 后 dock 不应残留在主窗口"
assert sip.isdeleted(dock), "dock C++ 对象应已销毁（防 reload 残留歧义快捷键）"
print("[8] 插件壳 dock 联动 ok（勾选↔显示、X 收起、AITracer 标签化、卸载即销毁）")

# ==== T9 图层复用：工程已有同源标注层时，再打开不得叠出重复层 ====
ANN = os.path.join(WORK, "耕地标注.gpkg")
wb2 = annotate.AnnotationController(iface, log)
wb2.dialog = wb.dialog
assert wb2.open_layer(ANN)
srcs = [L for L in QgsProject.instance().mapLayers().values()
        if isinstance(L, QgsVectorLayer) and L.isValid()
        and L.source().split("|")[0] == ANN]
assert len(srcs) == 1, f"出现重复层：{[L.name() for L in srcs]}"
assert wb2.layer.id() == srcs[0].id(), "应绑定工程中已有实例"
print("[9] 图层复用 ok（同源不再新建，控制器绑定既有实例）")

# ==== T10 单步删除：删中间一步，其余操作保留并重放 ====
grid.selectByIds([1])
wb.set_cell_from_selection()          # 先切到 led_A（清场要按它的过滤器删）
for f in list(wb.layer.getFeatures(wb._cell_filter())):
    wb.layer.deleteFeature(f.id())
wb.layer.commitChanges()
wb.layer.startEditing()
wb.ledger.reset_cell("led_A")
wb.set_cell_from_selection()          # 空格重进 → 重建底板记第 1 步
wb.apply_road(QgsGeometry.fromPolylineXY(
    [QgsPointXY(100, 500), QgsPointXY(900, 500)]))          # seq2 20,000
wb.apply_difference(poly(400, 100, 600, 300), "rect")        # seq3 40,000
wb.apply_add(poly(450, 150, 550, 250))                       # seq4 10,000
assert [r["kind"] for r in wb.ledger.cell_rows("led_A")] == \
    ["base", "road", "rect", "add"]

assert wb.ledger_delete_step("led_A", 3)                     # 删 rect
n, area = cell_state("led_A")
assert (n, round(area)) == (2, 990000), (n, area)            # 底板-路+补块
rows = wb.ledger.cell_rows("led_A")
assert [r["kind"] for r in rows] == ["base", "road", "add"]
assert [r["seq"] for r in rows] == [1, 2, 4], "seq 留洞不影响重放"

assert wb.ledger_delete_step("led_A", 4)                     # 删最后一步
n, area = cell_state("led_A")
assert (n, round(area)) == (1, 980000), (n, area)
assert not wb.ledger_delete_step("led_A", 1), "底板步不可删"
print("[10] 单步删除 ok（中间步删除后其余重放；底板受保护）")

# ==== T11 分层视图：底板/补块/扣除独立图层 + 图上选中即删 ====
wb.set_layer_views(True)
names = {"耕地·底板视图", "耕地·补块视图", "耕地·扣除视图"}
vls = {L.name(): L for L in QgsProject.instance().mapLayers().values()
       if L.name() in names}
assert len(vls) == 3, list(vls)
assert vls["耕地·底板视图"].featureCount() == 1
assert vls["耕地·补块视图"].featureCount() == 0
cut = vls["耕地·扣除视图"]
assert cut.featureCount() == 1, "road 缓冲成一条扣除区域"
f0 = next(iter(cut.getFeatures()))
assert f0["类型"] == "道路" and int(f0["步号"]) == 2 \
    and abs(float(f0["宽度m"]) - 25.0) < 1e-6, f0.attributes()

cut.selectAll()
assert wb.delete_selected_ops()
n, area = cell_state("led_A")
assert (n, round(area)) == (1, 1000000), (n, area)           # 路步被删
assert [r["kind"] for r in wb.ledger.cell_rows("led_A")] == ["base"]
assert cut.featureCount() == 0, "视图应已随删除刷新"
wb.set_layer_views(False)
assert not [L for L in QgsProject.instance().mapLayers().values()
            if L.name() in names], "关闭后图层应移除"
print("[11] 分层视图 ok（三类独立区域、选中删除一步、开关干净）")

# ==== T12 自动接续：新开工作台自动识别既有层，不产生重复 ====
wb.set_road_corner(False)     # 拐角按钮初始态需确定（QSettings 可能残留）
_dlg3 = annotate.AnnotateDock(_DlgIface())
for _ in range(30):
    app.processEvents()
assert _dlg3.wb.layer is not None, "工作台打开应自动接续标注层"
srcs = [L for L in QgsProject.instance().mapLayers().values()
        if isinstance(L, QgsVectorLayer) and L.isValid()
        and L.source().split("|")[0] == ANN]
assert len(srcs) == 1, "自动接续不得叠出重复层"
assert _dlg3.wb.current_cell == "led_A", "应恢复上次工作格"
print("[12] 自动接续 ok（识别既有层、恢复工作格、零重复）")

# ==== T13 OSM 接缝外延：错位断缝不得留菱形空白（v0.9.2 实机"拐弯小尖"） ====
# 实测缓存里相邻 OSM 短段端点错位 2~8m；旧逐段平头缓冲的接缝处
# 距路网 <半宽 的点不被覆盖 → 挖除后留下保留区小尖。
from qgis.core import QgsGeometry as _GG, QgsPointXY as _GP

_g1 = _GG.fromPolylineXY([_GP(100.0, 500.0), _GP(600.0, 500.0)])
_g2 = _GG.fromPolylineXY([_GP(603.0, 500.0), _GP(603.0, 100.0)])  # 3m 错位拐弯
_old = wb._buffer(_g1, 4.0).combine(wb._buffer(_g2, 4.0))
_new = wb._osm_buffer(_g1, 8.0).combine(wb._osm_buffer(_g2, 8.0))
# 缝外角点：距 g1 末端 (600,500) 约 2.9m < 半宽 4 → 理应被走廊盖住
for _px, _py in ((601.5, 502.5), (601.5, 497.5), (601.5, 500.0)):
    _p = _GG.fromPointXY(_GP(_px, _py))
    assert _new.contains(_p), f"({_px},{_py}) 应被外延缓冲盖住"
assert not _old.contains(_GG.fromPointXY(_GP(601.5, 502.5))), \
    "该点旧缓冲确实盖不住（证明断言有判别力）"
# 外延段本身：末端多出的半宽走廊（断头路代价，方向正确即可）
assert _new.contains(_GG.fromPointXY(_GP(103.0, 500.0)))   # 原线内
assert not _new.contains(_GG.fromPointXY(_GP(90.0, 500.0)))  # 外延=4m 止于 96
print("[13] OSM 接缝外延 ok（错位缝全覆盖；断头只多扣半宽）")

# ==== T14 笔刷网格：宽度档行必须在所有按钮行之下（v0.9.0 加第 9 键叠格翻车） ====
from qgis.PyQt.QtWidgets import QGridLayout

def _grid_rows(grid):
    btn_rows, lay_rows, n_btn = set(), set(), 0
    for i in range(grid.count()):
        it = grid.itemAt(i)
        row = grid.getItemPosition(i)[0]
        w = it.widget()
        if w is not None and w.metaObject().className() == "QPushButton":
            btn_rows.add(row)
            n_btn += 1
        elif it.layout() is not None:
            lay_rows.add(row)
    return btn_rows, lay_rows, n_btn

_hit = None
for g in _dlg3.findChildren(QGridLayout):
    b, l, n = _grid_rows(g)
    if n >= 8 and l:   # v0.9.7 起 8 键（框挖移除、多边形挖→磁力挖）
        _hit = (b, l)
        break
assert _hit, "找不到笔刷按钮网格"
b_rows, lay_rows = _hit
assert max(b_rows) < min(lay_rows), \
    f"宽度档（第 {sorted(lay_rows)} 行）与按钮（第 {sorted(b_rows)} 行）叠格"
from qgis.PyQt.QtWidgets import QPushButton as _QPB
_btns = _dlg3.findChildren(_QPB)
assert not any("框挖" in b.text() for b in _btns), "框挖按钮应已移除"
assert any("磁力挖" in b.text() for b in _btns), "磁力挖按钮应存在"
print(f"[14] 笔刷网格 ok（按钮 {len(b_rows)} 行，宽度档独立其下；框挖→磁力挖）")

# ==== T15 克隆主键冲突（v0.9.8：UNIQUE constraint failed: *.fid 修复） ====
# 机制：GPKG 首属性=fid，克隆拷贝属性会把原要素真实 fid 带进编辑缓冲，
# 提交时被当显式主键 INSERT → 撞既有行且整批回滚。旧测试从不真正
# commitChanges，所以一直没抓到。
assert wb._commit_layer("T15提交") is True
# 显式取 led_A 的底板：全层第一个要素可能是别的格（B 底板 fid 更小），
# 克隆它会让后面的跨格断言失真
feat0 = next(f for f in wb.layer.getFeatures(
    QgsFeatureRequest().setFilterExpression("cell_id = 'led_A'")))
assert feat0.attribute(0) is not None, "前提：GPKG 首属性应为 fid"
n0 = wb.layer.featureCount()
wb.layer.startEditing()   # commit 成功后 QGIS 停编辑（生产 save() 会重开）

clone = wb._clone_feature(feat0, poly(40, 40, 70, 70))
assert clone.attribute(0) is None, "_clone_feature 应置空主键属性"
wb.layer.addFeature(clone)

bad = QgsFeature(wb.layer.fields())          # 手工毒化：模拟旧版克隆携带真实 fid
bad.setAttributes(feat0.attributes())
bad.setGeometry(poly(45, 45, 75, 75))
wb.layer.addFeature(bad)
assert wb._commit_layer("T15提交2") is True, "缓冲区清洗应治愈携带旧主键的要素"
assert wb.layer.featureCount() == n0 + 2
assert any("已置空" in m for m in LOGS)
print("[15] 克隆主键冲突 ok（克隆置空主键 + 缓冲区清洗治愈坏克隆）")

# ==== T16 切格保存失败硬中止 + 死态自愈（v0.9.9） ====
# 机制：切格必须先把上一格落盘（撤销栈按格隔离）。老版把提交失败静默
# 吞掉硬切：探针实测 B 底板"已建立"却随坏缓冲丢弃、笔触全丢、图层停在
# 不可编辑的死态（实机"切格后旧格不能编辑"的根源）。现在失败=取消切格。
grid.selectByIds([1])                                 # led_A
assert wb.set_cell_from_selection() is True
wb.apply_difference(poly(300, 300, 400, 400), "erase")   # A 留一笔不保存
nA, aA = cell_state("led_A")
assert nA >= 1 and aA < 1000000

_real_commit = wb._commit_layer
def _fail_commit(what="保存"):
    log(f"[mock] {what} 失败（模拟文件被占用）")
    return False
wb._commit_layer = _fail_commit
nB_before = cell_state("led_B")[0]
grid.selectByIds([2])                                 # 想切 led_B
assert wb.set_cell_from_selection() is False, "保存失败必须取消切格"
assert wb.current_cell == "led_A", "中止后当前格不变"
assert cell_state("led_B")[0] == nB_before, "中止后 B 不得新增底板"
assert any("已取消切格" in m for m in LOGS)
wb._commit_layer = _real_commit

grid.selectByIds([2])                                 # 占用解除后重切应成功
assert wb.set_cell_from_selection() is True
assert wb.current_cell == "led_B" and wb.layer.isEditable()
assert cell_state("led_A") == (nA, aA), "中止期间 A 的笔触应原样保留并随重切落盘"
assert cell_state("led_B")[0] == nB_before

wb.save()                                             # B 落盘，制造干净停编态
assert wb.layer.commitChanges() or not wb.layer.isEditable()  # 停编（缓冲已空）
assert not wb.layer.isEditable(), "前提：已停编（死态）"
grid.selectByIds([1])
assert wb.set_cell_from_selection() is True
assert wb.layer.isEditable(), "死态自愈：切格应重启编辑"
assert wb.current_cell == "led_A"
print("[16] 切格硬中止 ok（失败取消切格、笔触保留、死态自愈、重切成功）")

# ==== T17 道路拐角样式：圆角/尖角可选（v0.9.9） ====
from qgis.core import QgsGeometry as _GG, QgsPointXY as _GP

def _nearest_vertex(g, x, y):
    best = 1e9
    for pt in g.vertices():
        d = ((pt.x() - x) ** 2 + (pt.y() - y) ** 2) ** 0.5
        best = min(best, d)
    return best

_L = _GG.fromPolylineXY([_GP(0.0, 0.0), _GP(100.0, 0.0), _GP(100.0, 100.0)])
_CORNER = (110.0, -10.0)      # 90° 拐弯的外角点：尖角必有顶点，圆角是弧
wb.set_road_corner(False)
assert wb.road_miter is False
r_geom = wb._buffer(_L, 10.0)
assert _nearest_vertex(r_geom, *_CORNER) > 1.0, "圆角拐弯处不应有顶点"
wb.set_road_corner(True)
assert wb.road_miter is True
m_geom = wb._buffer(_L, 10.0)
assert _nearest_vertex(m_geom, *_CORNER) < 0.01, "尖角拐弯处应有精确顶点"
assert abs(m_geom.area() - r_geom.area()) > 1.0, "两种拐角面积应不同"

# 台账记录 miter，重放保真（旧记录无 miter 键=圆角，兼容）
from qgis.core import QgsGeometry
wb.apply_road(_GG.fromPolylineXY([_GP(50.0, 600.0), _GP(500.0, 600.0)]))
_road_rows = [r for r in wb.ledger.cell_rows("led_A") if r["kind"] == "road"]
assert _road_rows and _road_rows[-1]["params"].get("miter") is True, \
    "台账应记录拐角样式"
assert any("尖角" in m for m in LOGS)

# UI：拐角按钮存在且联动
_corner_btns = [b for b in _dlg3.findChildren(_QPB) if "拐角" in b.text()]
assert len(_corner_btns) == 1, "应有且仅有一个拐角按钮"
_cb = _corner_btns[0]
assert _cb.text() == "拐角:圆", "对话框构建时全局为圆角（旧默认）"
_cb.setChecked(True)
assert wb.road_miter is True and _cb.text() == "拐角:尖"
_cb.setChecked(False)
wb.set_road_corner(False)                             # 还原全局默认
assert QSettings().value(
    "cropland_delineator/road_corner_miter", False, type=bool) is False
print("[17] 道路拐角 ok（圆/尖几何判别、台账记录、重放兼容旧记录、UI 联动）")

shutil.rmtree(WORK, ignore_errors=True)
print(f"\n全部 17 组台账测试通过 ✓  (工作目录已清理)")
sys.stdout.flush()
# QGIS offscreen 下解释器析构阶段随机段错误（测试已全过，干净树也复现），
# 直接绕过析构退出
os._exit(0)
