"""耕地标注工作台：把"整格底板 − 道路 − 城镇 − 缝隙切分"的减法标注流程
变成画布上的即时笔刷，每个笔触一步撤销，自动落盘到 GPKG。

数据模型（annotation 层，与渔网同 CRS）：
  cell_id  格子编号（优先取渔网 FID_1 字段，如 海城市_2_4）
  source   base=整格底板 / road=道路笔刷差集后的残余 / erase=框/多边形挖除后
           的残余 / add=手画补块 / model=模型候选接纳（P1）
  created  创建时间
每个要素保持单部件；差集/切分产生多部件时立即拆分成多个要素。
"""

import math
import os
import shutil
import tempfile
from datetime import datetime

import numpy as np

from qgis.PyQt.QtCore import Qt, QEvent, QMetaType, QDateTime, QTimer, QSettings
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QScrollArea,
    QFrame,
    QWidget,
    QVBoxLayout,
    QGridLayout,
    QFormLayout,
    QHBoxLayout,
    QPushButton,
    QDoubleSpinBox,
    QGroupBox,
    QLineEdit,
    QListWidget,
    QToolButton,
    QPlainTextEdit,
    QLabel,
    QFileDialog,
    QMessageBox,
    QButtonGroup,
    QInputDialog,
)
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsExpression,
    QgsWkbTypes,
    QgsFillSymbol,
    QgsCategorizedSymbolRenderer,
    QgsSingleSymbolRenderer,
    QgsRendererCategory,
    QgsSnappingConfig,
    QgsTolerance,
    Qgis,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel

from . import clickseg
from . import map_tools
from . import osm_roads
from . import lasso
from . import ledger as ops_ledger


def poly_parts(geom):
    """把（多）多边形拆成单部件多边形几何列表；空几何返回空表。"""
    if geom is None or geom.isEmpty():
        return []
    if geom.isMultipart():
        polys = geom.asMultiPolygon()
    else:
        polys = [geom.asPolygon()]
    out = []
    for rings in polys:
        if rings:
            out.append(QgsGeometry.fromPolygonXY(rings))
    return out


def _unary_union(geoms):
    """几何列表求并（优先 GEOS unaryUnion，失败退级联合并）。"""
    if not geoms:
        return QgsGeometry()
    try:
        u = QgsGeometry.unaryUnion(geoms)
        if u is not None and not u.isEmpty():
            return u
    except (AttributeError, TypeError):
        pass
    u = geoms[0]
    for g in geoms[1:]:
        u = u.combine(g)
    return u


def _is_polygon_layer(layer):
    try:  # QGIS 4：Qgis.GeometryType
        return layer.geometryType() == Qgis.GeometryType.Polygon
    except AttributeError:  # QGIS 3：QgsWkbTypes
        return layer.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry


class AnnotationController:
    """标注层、当前格子、几何运算与统计。工具类通过它落每一笔。"""

    SOURCE_COLORS = {
        "base": ("255,170,0", "整格底板"),
        "add": ("0,200,80", "手画补块"),
        "model": ("60,140,255", "模型候选"),
        "sam": ("180,80,255", "SAM 接纳"),
    }

    def __init__(self, iface, log_fn):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.log = log_fn
        self.layer = None
        self.path = None
        self.current_cell = None
        self.cell_geom = None          # 图层 CRS 下的格子范围
        self.road_widths = [25.0, 8.0]  # [大路, 小路] 全宽（米）
        self.road_active = 0
        self.roads_layer = None        # OSM 道路层（拉取后指向它）
        self.raster_layer = None       # 磁力切分用的影像（None=自动探测）
        self._lasso = None             # 当前格的磁力引擎缓存
        self._lasso_key = None
        self._lasso_xf = None
        self._warned_geographic = False
        # 操作台账（可重放历史）：.gpkg 旁的 <名>_ops.jsonl
        self.ledger = None
        self._ledger_sess = 0          # 撤销栈会话号（commit/切格/回放后递增）
        self._undo_prev = 0            # 上次撤销栈位（检测 Ctrl+Z 对齐台账）
        # SAM 点选分割（决策 0013）：常驻 GPU worker + 每格影像窗口缓存
        self._sam = None
        self._sam_missing_logged = False
        self._sam_tmp = None           # clickseg_ 前缀临时目录（npy）
        self._clickseg_ctx = None      # (npy, meta, xf画布→影像)
        self._clickseg_key = None
        self._clickseg_bailed = None   # 上次未就绪原因（防刷屏）
        # 分层视图（底板/补块/扣除，派生自台账，选中可单步删除）
        self._views_on = False
        self._view_layers = {}

    # ---------- 图层 ----------

    def open_layer(self, path):
        path = os.path.abspath(path)
        if not path.endswith(".gpkg"):
            path += ".gpkg"
        # 重开同一文件：先提交旧层的未落盘笔触再移除实例，并保留当前格
        reopen = self.path == path and self.layer is not None
        if reopen:
            try:
                if self.layer.isEditCommandActive():
                    self.layer.endEditCommand()
                if self.layer.isEditable():
                    self.layer.commitChanges()
            except Exception:
                pass
            try:
                QgsProject.instance().removeMapLayer(self.layer.id())
            except Exception:
                pass
            prev_cell = self.current_cell
        else:
            prev_cell = None
        if os.path.exists(path):
            layer = None
            # 工程里已有同源标注层（重启 QGIS / 重开工作台的常见情形）：
            # 直接复用，不再叠加一个同名重复层
            for L in QgsProject.instance().mapLayers().values():
                if (isinstance(L, QgsVectorLayer) and L.isValid()
                        and L.source().split("|")[0] == path
                        and "layername=annotation" in L.source()):
                    layer = L
                    break
            if layer is None:
                layer = QgsVectorLayer(
                    path + "|layername=annotation", "耕地标注", "ogr")
                if not layer.isValid():
                    # 文件存在但没有 annotation 层：按新建处理（追加新层）
                    layer = None
        else:
            layer = None

        if layer is None:
            layer = self._create_layer(path)
            if layer is None:
                self.log(f"[错误] 无法建立标注层：{path}")
                return False
            self.log(f"[i] 已新建标注文件：{path}")
        elif reopen:
            self.log(f"[i] 已重新打开标注文件：{path}")
        else:
            self.log(f"[i] 已打开标注文件：{path}（复用工程中已有的图层）"
                     if layer.id() in QgsProject.instance().mapLayers()
                     else f"[i] 已打开标注文件：{path}")
        self.path = path
        self.layer = layer
        self.ledger = ops_ledger.Ledger(
            path[: -len(".gpkg")] + "_ops.jsonl")
        self._ledger_sess += 1
        self._undo_prev = 0
        layer.undoStack().indexChanged.connect(self._on_undo_index)
        self._style_layer()
        if self.layer.crs().isGeographic():
            self.log("[警告] 标注层 CRS 是地理坐标系，道路宽度（米）将无法使用")
        if self.layer.id() not in QgsProject.instance().mapLayers():
            QgsProject.instance().addMapLayer(self.layer)
        # 认领桥接：AITracer 等外部工具接纳进来的多边形没有 cell_id，
        # 落层后自动归属当前格子（source='sam'），纳入统计与减法笔刷
        self.layer.featureAdded.connect(
            lambda fid, L=self.layer: self._on_feature_added(L, fid))
        self._enable_snapping()
        self.layer.startEditing()
        self.current_cell = prev_cell
        self.cell_geom = None
        if prev_cell is None:
            self._restore_last_cell()
        return True

    def _restore_last_cell(self):
        """跨会话恢复上次工作格（QGIS/对话框重开后不必重新选格）。"""
        saved = QSettings().value(
            "cropland_delineator/last_cell", "", type=str)
        if not saved:
            return
        path, _, cell_id = saved.rpartition("|")
        if path != self.path or not cell_id:
            return
        self.current_cell = cell_id
        n = sum(1 for _ in self.layer.getFeatures(self._cell_filter()))
        if n == 0:
            self.current_cell = None  # 上次的格子在文件里没有要素，不恢复
            return
        self._load_cell_geom(cell_id)
        self.zoom_to_cell()
        self.log(f"[i] 已恢复上次工作格 {cell_id}（{n} 个要素）— 按 N 跳下一格")
        self.report_stats()

    def _load_cell_geom(self, cell_id):
        """按 cell_id 从渔网取格子几何（转到标注层 CRS）。"""
        grid = self._grid_layer()
        if grid is None:
            return
        for f in grid.getFeatures():
            if self._cell_id(f, f.geometry()) == cell_id:
                g = QgsGeometry(f.geometry())
                xform = QgsCoordinateTransform(
                    grid.crs(), self.layer.crs(), QgsProject.instance())
                g.transform(xform)
                self.cell_geom = g
                return

    def _create_layer(self, path):
        grid = self._grid_layer()
        crs = grid.crs() if grid is not None else QgsProject.instance().crs()
        mem = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "annotation", "memory")
        mem.dataProvider().addAttributes([
            QgsField("cell_id", QMetaType.Type.QString),
            QgsField("source", QMetaType.Type.QString),
            QgsField("created", QMetaType.Type.QDateTime),
        ])
        mem.updateFields()
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = "annotation"
        if os.path.exists(path):
            opts.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
        else:
            opts.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        err, _, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
            mem, path, QgsProject.instance().transformContext(), opts)
        if err != QgsVectorFileWriter.WriterError.NoError:
            return None
        return QgsVectorLayer(path + "|layername=annotation", "耕地标注", "ogr")

    def _style_layer(self):
        cats = []
        for key, (rgb, label) in self.SOURCE_COLORS.items():
            sym = QgsFillSymbol.createSimple({
                "color": f"{rgb},45",
                "outline_color": f"{rgb},220",
                "outline_width": "0.6",
            })
            cats.append(QgsRendererCategory(key, sym, label))
        renderer = QgsCategorizedSymbolRenderer("source", cats)
        # source 没匹配到的（如道路差集残余，仍是 base/add 原 source）走默认色
        default_sym = QgsFillSymbol.createSimple({
            "color": "255,170,0,45", "outline_color": "255,170,0,220",
            "outline_width": "0.6"})
        renderer.setSourceSymbol(default_sym)
        self.layer.setRenderer(renderer)
        self.layer.triggerRepaint()

    def _on_feature_added(self, layer, fid):
        if layer is not self.layer or self.current_cell is None:
            return
        # 延迟到外部工具的编辑命令结束之后再补属性
        QTimer.singleShot(0, lambda: self._adopt_feature(fid))

    def _adopt_feature(self, fid):
        if self.layer is None or self.current_cell is None:
            return
        try:
            feat = self.layer.getFeature(fid)
        except Exception:
            return
        if not feat.isValid() or feat["cell_id"] not in (None, ""):
            return  # 工作台自己加的要素已带属性
        fields = self.layer.fields()
        changes = {
            fields.indexOf("cell_id"): self.current_cell,
            fields.indexOf("source"): "sam",
            fields.indexOf("created"): QDateTime.currentDateTime(),
        }
        if self.layer.isEditable():
            self.layer.changeAttributeValues(fid, changes)
        else:
            self.layer.dataProvider().changeAttributeValues({fid: changes})
        self.log("[SAM] 已认领外来多边形 → 当前格子")
        self.report_stats()

    def try_select_aitracer_target(self):
        """尽力把 AITracer 的输出层指向本标注层（插件没开就跳过）。"""
        try:
            from qgis import utils
            inst = utils.plugins.get("aitracer")
            dock = getattr(inst, "_dock", None)
            dock.select_layer(self.layer)
            self.log("[i] 已将 AITracer 输出层指向「耕地标注」")
        except Exception:
            pass

    def _enable_snapping(self):
        proj = QgsProject.instance()
        cfg = proj.snappingConfig()
        cfg.setEnabled(True)
        for mode in (
            lambda: Qgis.SnappingMode.AllLayers,
            lambda: QgsSnappingConfig.SnappingMode.AllLayers,
        ):
            try:
                cfg.setMode(mode())
                break
            except (AttributeError, TypeError):
                continue
        # setType 需要 flags 对象（QGIS4: Qgis.SnappingTypes / QGIS3: SnappingTypeFlag）
        for flags in (
            lambda: Qgis.SnappingTypes(
                int(Qgis.SnappingType.Vertex) | int(Qgis.SnappingType.Segment)),
            lambda: (QgsSnappingConfig.SnappingTypeFlag.Vertex
                     | QgsSnappingConfig.SnappingTypeFlag.Segment),
        ):
            try:
                cfg.setType(flags())
                break
            except (AttributeError, TypeError):
                continue
        cfg.setUnits(QgsTolerance.UnitType.Pixels)
        cfg.setTolerance(12)
        proj.setSnappingConfig(cfg)
        self.log("[i] 已开启全图层吸附（顶点+线段，12px）")

    # ---------- 格子 ----------

    def _grid_layer(self):
        dlg = getattr(self, "dialog", None)
        return dlg.grid_layer() if dlg is not None else None

    def set_cell_from_selection(self):
        if self.layer is None:
            self.log("[提示] 请先打开标注层")
            return
        grid = self._grid_layer()
        if grid is None:
            self.log("[提示] 请先选择渔网图层")
            return
        sel = grid.selectedFeatures()
        if not sel:
            self.log("[提示] 请先在渔网图层选中一个格子（用第 1 个选中要素）")
            return
        feat = sel[0]
        geom = QgsGeometry(feat.geometry())
        xform = QgsCoordinateTransform(
            grid.crs(), self.layer.crs(), QgsProject.instance())
        geom.transform(xform)

        cell_id = self._cell_id(feat, geom)
        # 切格前把上一格的编辑落盘，保证撤销栈按格隔离
        if self.layer.isEditCommandActive():
            self.layer.endEditCommand()
        if self.layer.isEditable():
            self.layer.commitChanges()
            self.layer.startEditing()
        self._ledger_sess += 1   # 栈已清空：撤销对齐换新会话号
        self._undo_prev = 0

        self.current_cell = cell_id
        self.cell_geom = geom
        QSettings().setValue(
            "cropland_delineator/last_cell", f"{self.path}|{cell_id}")
        try:  # 渔网也记下：重启后工作台自动接续要用
            QSettings().setValue("cropland_delineator/last_grid", grid.source())
        except Exception:
            pass

        existing = list(self.layer.getFeatures(self._cell_filter("source='base'")))
        if existing:
            self.log(f"[i] 当前格子：{cell_id}（底板已存在，继续标注）")
        else:
            self.layer.beginEditCommand("建底板")
            self.layer.addFeature(self._new_feature(geom, "base"))
            self.layer.endEditCommand()
            self.log(f"[i] 当前格子：{cell_id}（底板已建立）")
            # 重建底板=该格从头开始：台账清零后记第 1 步
            if self.ledger is not None:
                self.ledger.reset_cell(cell_id)
                self._ledger_record("base", geom.asWkt(),
                                    {"source": "base"})
        self.zoom_to_cell()
        self.report_stats()

    def _cell_id(self, feat, geom):
        fields = [f.name() for f in feat.fields()]
        for name in ("FID_1", "FID", "id", "ID"):
            if name in fields and feat[name] not in (None, ""):
                return str(feat[name])
        c = geom.centroid().asPoint()
        return f"cell_{int(c.x())}_{int(c.y())}"

    def zoom_to_cell(self):
        if self.cell_geom is None:
            return
        ext = QgsRectangle(self.cell_geom.boundingBox())
        ext.scale(1.05)
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    # ---------- 通用 ----------

    def _cell_filter(self, extra=None):
        expr = f"cell_id = '{self.current_cell}'"
        if extra:
            expr += f" AND {extra}"
        return QgsFeatureRequest().setFilterExpression(expr)

    def to_layer(self, geom):
        src = self.canvas.mapSettings().destinationCrs()
        dst = self.layer.crs()
        if src.authid() != dst.authid():
            geom = QgsGeometry(geom)
            geom.transform(QgsCoordinateTransform(
                src, dst, QgsProject.instance()))
        return geom

    def _new_feature(self, geom, source):
        f = QgsFeature(self.layer.fields())
        f.setAttribute("cell_id", self.current_cell)
        f.setAttribute("source", source)
        f.setAttribute("created", QDateTime.currentDateTime())
        f.setGeometry(geom)
        return f

    def _clone_feature(self, feat, geom):
        f = QgsFeature(self.layer.fields())
        f.setAttributes(feat.attributes())
        f.setGeometry(geom)
        return f

    # ---------- 道路宽度档 ----------

    def road_width(self):
        return self.road_widths[self.road_active]

    def set_road_preset(self, idx):
        self.road_active = idx
        self.log(f"[i] 道路宽度档：{'大路' if idx == 0 else '小路'} "
                 f"{self.road_width():.0f} m")

    def nudge_road_width(self, delta):
        self.road_widths[self.road_active] = max(
            1.0, self.road_widths[self.road_active] + delta)
        self.log(f"[i] 道路宽度：{self.road_width():.0f} m")

    def buffer_preview(self, line_map):
        """地图 CRS 下的缓冲预览（项目 CRS 为米制时才有效）。"""
        if self.canvas.mapSettings().destinationCrs().isGeographic():
            return None
        return self._buffer(line_map, self.road_width() / 2.0)

    def _buffer(self, line, half_width):
        try:  # QGIS 4：5 参版含 miterLimit
            return line.buffer(
                half_width, 8, Qgis.EndCapStyle.Flat,
                Qgis.JoinStyle.Round, 2.0)
        except TypeError:
            return line.buffer(half_width, 8)

    # ---------- 几何运算（每笔一个编辑命令 = 一步撤销） ----------

    def _require_cell(self):
        if self.layer is None or self.current_cell is None:
            self.log("[提示] 当前格未设定：在渔网选中一个格子后点"
                     "「选中格子 → 建底板」，或按 N 跳到下一格")
            return False
        return True

    def apply_road(self, line_layer):
        if not self._require_cell():
            return
        self._ledger_prepare()
        buf = self._buffer(line_layer, self.road_width() / 2.0)
        n = self._difference(buf)
        if n:
            self.log(f"[道路] 宽 {self.road_width():.0f} m，影响 {n} 个要素")
            self._ledger_record("road", line_layer.asWkt(),
                                {"width": self.road_width()})
        self.report_stats()

    def apply_difference(self, eraser, tag):
        if not self._require_cell():
            return
        self._ledger_prepare()
        n = self._difference(eraser)
        if n:
            self.log(f"[挖除] 影响 {n} 个要素")
            self._ledger_record(tag, eraser.asWkt())
        self.report_stats()

    def _difference(self, eraser):
        self.layer.beginEditCommand("减法笔刷")
        n = self._run_difference(eraser)
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        return n

    def _run_difference(self, eraser):
        """在已开启的编辑命令内做差集；调用方负责 begin/endEditCommand。"""
        affected = 0
        feats = list(self.layer.getFeatures(self._cell_filter()))
        for feat in feats:
            g = feat.geometry()
            new = g.difference(eraser)
            parts = poly_parts(new)
            if not parts:
                self.layer.deleteFeature(feat.id())
                affected += 1
                continue
            if len(parts) == 1 and not new.isMultipart():
                self.layer.changeGeometry(feat.id(), new)
            else:
                # 挖完变成多块：拆成多个单部件要素（继承原 source）
                self.layer.deleteFeature(feat.id())
                for p in parts:
                    self.layer.addFeature(self._clone_feature(feat, p))
            affected += 1
        return affected

    def apply_add(self, geom_layer):
        if not self._require_cell():
            return
        clipped = geom_layer.intersection(self.cell_geom) \
            if self.cell_geom is not None else geom_layer
        if not poly_parts(clipped):
            self.log("[提示] 与格子不相交，已忽略")
            return
        self._ledger_prepare()
        self.layer.beginEditCommand("补画")
        n = self._run_add(geom_layer)
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        self.log(f"[补画] 新增 {n} 个要素")
        if n:
            self._ledger_record("add", geom_layer.asWkt())
        self.report_stats()

    def _run_add(self, geom_layer):
        """编辑命令内补画（裁回格子），返回新增块数。重放共用。"""
        clipped = geom_layer.intersection(self.cell_geom) \
            if self.cell_geom is not None else geom_layer
        parts = poly_parts(clipped)
        for p in parts:
            self.layer.addFeature(self._new_feature(p, "add"))
        return len(parts)

    def apply_split(self, line_layer):
        if not self._require_cell():
            return
        self._ledger_prepare()
        self.layer.beginEditCommand("切分")
        made, touched = self._run_split(line_layer)
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        if made:
            self.log(f"[切分] 分开了 {made} 处")
            self._ledger_record("split", line_layer.asWkt())
        elif touched:
            self.log(f"[切分] 线碰到 {touched} 个地块但没切开："
                     "线两端都在同一地块内部或贴着边界走，请让线横穿地块本体")
        else:
            self.log("[切分] 线不经过任何地块——沿已挖除的道路画线时，"
                     "两侧本就是空隙，无需再切")
        self.report_stats()

    def _run_split(self, line_layer):
        """编辑命令内切分当前格全部相交地块；返回 (切开处数, 碰到数)。重放共用。"""
        pts = line_layer.asPolyline()
        if len(pts) < 2:
            return 0, 0
        made = 0
        touched = 0
        feats = list(self.layer.getFeatures(self._cell_filter()))
        for feat in feats:
            g = QgsGeometry(feat.geometry())
            if not g.intersects(line_layer):
                continue
            touched += 1
            new_geoms = []
            try:  # QGIS 4：返回 (结果码, 新几何列表, 拓扑点)
                rc, new_geoms, _topo = g.splitGeometry(pts, False, True)
            except (TypeError, ValueError):  # QGIS 3 旧签名
                rc = g.splitGeometry(pts, new_geoms, False)
            if rc != 0 or not new_geoms:
                continue
            # g 为切后的一侧，new_geoms 为另一侧
            pieces = poly_parts(g)
            for x in new_geoms:
                pieces.extend(poly_parts(x))
            self.layer.deleteFeature(feat.id())
            for p in pieces:
                self.layer.addFeature(self._clone_feature(feat, p))
            made += len(new_geoms)
        return made, touched

    # ---------- B：选中即挖除（SAM/手画面当精确橡皮，快捷键 E） ----------

    def erase_selected(self):
        """把选中的多边形当精确橡皮：对当前格全部要素做差集，随后删除
        这些被选面（被选面来自标注层时删除，来自外层只清选择不动数据）。
        一次编辑命令 = 一步撤销。AITracer 接纳的紫色面、手画补块均可。"""
        if not self._require_cell():
            return
        src = None
        feats = []
        al = self.iface.activeLayer() if self.iface is not None else None
        if isinstance(al, QgsVectorLayer) and al.selectedFeatureCount() > 0 \
                and _is_polygon_layer(al):
            src, feats = al, list(al.getSelectedFeatures())
        elif self.layer.selectedFeatureCount() > 0:
            src, feats = self.layer, list(self.layer.getSelectedFeatures())
        geoms = []
        for f in feats:
            if f.hasGeometry() and not f.geometry().isEmpty():
                geoms.append(QgsGeometry(f.geometry()))
        if not geoms:
            self.log("[提示] 先选中要当橡皮的多边形（任意多边形图层均可，"
                     "AITracer 接纳的紫色面也行），再按 E")
            return
        if src.crs() != self.layer.crs():
            xform = QgsCoordinateTransform(
                src.crs(), self.layer.crs(), QgsProject.instance())
            for i, g in enumerate(geoms):
                geoms[i].transform(xform)
        eraser = _unary_union(geoms)
        if eraser.isEmpty():
            self.log("[错误] 被选面合并失败（可能自相交），请重画")
            return
        self.layer.beginEditCommand("选中挖除")
        n = self._run_difference(eraser)
        if src is self.layer:
            self.layer.deleteFeatures([f.id() for f in feats])
        self.layer.endEditCommand()
        src.removeSelection()
        self.layer.triggerRepaint()
        self.log(f"[选中挖除] {len(feats)} 个被选面 → 影响 {n} 个要素")
        self.report_stats()

    # ---------- F1：磁力切分（影像引擎懒建缓存） ----------

    def _find_raster(self):
        """绑定优先；否则在项目里找与当前格相交面积最大的栅格。"""
        if self.raster_layer is not None and self.raster_layer.isValid():
            return self.raster_layer
        if self.layer is None or self.cell_geom is None:
            return None
        rect = self.cell_geom.boundingBox()
        best, best_area = None, 0.0
        for L in QgsProject.instance().mapLayers().values():
            if not isinstance(L, QgsRasterLayer) or not L.isValid():
                continue
            try:
                ext = QgsRectangle(L.extent())
                if L.crs() != self.layer.crs():
                    ext = QgsCoordinateTransform(
                        L.crs(), self.layer.crs(),
                        QgsProject.instance()).transformBoundingBox(ext)
                inter = ext.intersect(rect)
                area = inter.width() * inter.height()
            except Exception:
                continue
            if area > best_area:
                best, best_area = L, area
        if best is not None:
            self.raster_layer = best
            self.log(f"[磁力] 自动绑定影像：{best.name()}")
        return best

    def lasso_context(self):
        """(引擎, 画布→栅格 CRS 变换)；影像/格未就绪返回 (None, None)。

        引擎按（影像, 当前格）缓存，换格自动重建。
        """
        if self.layer is None or self.current_cell is None \
                or self.cell_geom is None:
            return None, None
        raster = self._find_raster()
        if raster is None:
            return None, None
        # 边界证据（离线 NN 生成，可有可无）：路径+修改时间进缓存键，
        # 用户中途放入/更新证据文件后引擎自动重建
        ev_path = lasso.evidence_path(raster, self.current_cell)
        ev_mtime = None
        if ev_path is not None and os.path.exists(ev_path):
            try:
                ev_mtime = int(os.path.getmtime(ev_path))
            except OSError:
                ev_mtime, ev_path = None, None
        elif ev_path is not None and os.path.isdir(os.path.dirname(ev_path)):
            # 部署过证据（目录在）但该格没生成：说一声，别让"没融合"无迹可查
            self.log(f"[磁力] 该格暂无边界证据（边缘证据/ 里没有 "
                     f"{self.current_cell}.npz），走纯梯度")
        key = (raster.id(), self.current_cell, ev_mtime)
        if self._lasso_key == key and self._lasso is not None:
            return self._lasso, self._lasso_xf
        ev, ev_meta = lasso.load_evidence(ev_path) if ev_path else (None, None)
        try:
            eng = lasso.LassoEngine(
                raster, self.cell_geom.boundingBox(), self.layer.crs(),
                evidence=ev)
        except Exception as exc:
            self.log(f"[磁力] 影像窗口建立失败：{exc}")
            self._lasso, self._lasso_key = None, None
            return None, None
        if ev is not None:
            if not eng.used_evidence:
                self.log("[磁力] 边界证据网格与影像窗口不一致，已忽略（请重跑证据生成）")
            elif not (abs(float(ev_meta.get("x0", eng.x0)) - eng.x0) < 1e-3
                      and abs(float(ev_meta.get("pw", eng.pw)) - eng.pw) < 1e-6
                      and abs(float(ev_meta.get("y0", eng.y0)) - eng.y0) < 1e-3):
                # 形状恰好相同但原点对不上（裁格用了别的范围）：丢弃防错位
                try:
                    eng = lasso.LassoEngine(
                        raster, self.cell_geom.boundingBox(), self.layer.crs())
                    self.log("[磁力] 边界证据原点与影像窗口不符，已忽略")
                except Exception:
                    pass
            else:
                self.log("[磁力] 已融合边界证据（神经网络）")
        xf = None
        mcrs = self.canvas.mapSettings().destinationCrs()
        if mcrs != eng.crs:
            xf = QgsCoordinateTransform(mcrs, eng.crs, QgsProject.instance())
        self._lasso, self._lasso_key, self._lasso_xf = eng, key, xf
        self.log(f"[磁力] 影像窗口就绪 {eng.shape[1]}×{eng.shape[0]} px"
                 f"（{raster.name()}，像元 {eng.pixel_size():.1f} m）")
        return eng, xf

    # ---------- SAM 点选分割（自研；worker 见 nn/sam_click.py，决策 0013） ----------

    def sam_click(self):
        """常驻 SAM 服务；环境缺失（找不到 nn/）时提示一次并返回 None。"""
        if self._sam is not None and self._sam.alive():
            return self._sam
        nn_dir = clickseg.find_nn_dir()
        if nn_dir is None:
            if not self._sam_missing_logged:
                self.log("[点选] 未找到 nn/ 推理环境（需 .venv 与 "
                         "ckpt/sam_vit_b*.pth），点选分割不可用；"
                         "可用环境变量 CROPLAND_NN_DIR 指定位置")
                self._sam_missing_logged = True
            return None
        py, argv = clickseg.service_argv(nn_dir)
        try:
            self._sam = clickseg.SamClickService(py, argv, self.log)
        except Exception as exc:
            self.log(f"[点选] SAM 服务启动失败：{exc!r}")
            return None
        return self._sam

    def clickseg_context(self):
        """(npy路径, 窗口meta, xf画布→影像CRS)；影像/格未就绪返回 (None,None,None)。

        窗口按（影像, 当前格）缓存，换格自动重建（npy 覆写、worker 重新编码）。
        未就绪的原因会写日志（同因只说一次），现场不再有静默失败。
        """
        if self.layer is None:
            return None, None, None
        if self.current_cell is None or self.cell_geom is None:
            self._clickseg_bail("当前没有激活格——先在渔网里选中格子建底板")
            return None, None, None
        raster = self._find_raster()
        if raster is None:
            self._clickseg_bail("项目里没有与当前格相交的影像图层")
            return None, None, None
        self._clickseg_bailed = None  # 恢复后允许下次再提示
        key = (raster.id(), self.current_cell)
        if self._clickseg_key == key and self._clickseg_ctx is not None:
            return self._clickseg_ctx
        if self._sam_tmp is None:
            self._sam_tmp = tempfile.mkdtemp(prefix="clickseg_")
        npy = os.path.join(self._sam_tmp, "cell.npy")
        try:
            meta = clickseg.build_window_npy(
                raster, self.cell_geom.boundingBox(), self.layer.crs(), npy)
        except Exception as exc:
            self.log(f"[点选] 影像窗口建立失败：{exc}")
            self._clickseg_ctx, self._clickseg_key = None, None
            return None, None, None
        xf = None
        mcrs = self.canvas.mapSettings().destinationCrs()
        if mcrs != raster.crs():
            xf = QgsCoordinateTransform(
                mcrs, raster.crs(), QgsProject.instance())
        self._clickseg_key = key
        self._clickseg_ctx = (npy, meta, xf)
        self.log(f"[点选] 影像窗口就绪 {meta['w']}×{meta['h']} px"
                 f"（{raster.name()}，像元 {meta['pw']:.1f} m）")
        return self._clickseg_ctx

    def _clickseg_bail(self, reason):
        """未就绪原因只报一次（防连点刷屏），原因变化时重报。"""
        if reason != self._clickseg_bailed:
            self.log(f"[点选] {reason}")
            self._clickseg_bailed = reason

    def _shutdown_sam(self):
        """关会话时释放：GPU 常驻进程 + clickseg 临时目录 + 窗口缓存。"""
        if self._sam is not None:
            try:
                self._sam.shutdown()
            except Exception:
                pass
            self._sam = None
        if self._sam_tmp is not None:
            shutil.rmtree(self._sam_tmp, ignore_errors=True)
            self._sam_tmp = None
        self._clickseg_ctx, self._clickseg_key = None, None
        self._clickseg_bailed = None

    # ---------- A：OSM 道路先验（画路 → 拉路） ----------

    def osm_layer(self):
        """道路层逐级回退：拉取结果 → 工程「OSM 道路」层 → 渔网目录缓存文件。"""
        L = getattr(self, "roads_layer", None)
        if L is not None and L.isValid():
            return L
        for L in QgsProject.instance().mapLayersByName("OSM 道路"):
            return L
        # 工程里也没有：直接加载渔网目录缓存（不联网，几万条也就一次 GPKG 读）
        grid = self._grid_layer()
        cache = osm_roads.cache_path(grid) if grid is not None else None
        if cache and os.path.exists(cache):
            dlg = getattr(self, "dialog", None)
            if dlg is not None and hasattr(dlg, "_load_roads"):
                dlg._load_roads(cache)
            else:  # 无对话框（脚本/测试）：取层但不入工程
                vl = QgsVectorLayer(
                    cache + "|layername=osm_roads", "OSM 道路", "ogr")
                if not vl.isValid():
                    return None
                self.roads_layer = vl
                self.log(f"[OSM] 已自动加载道路缓存：{cache}")
            return self.roads_layer
        return None

    def apply_osm_roads(self, roads=None):
        """含 highway 字段的线图层按大路/小路档宽度缓冲成道路面，
        从当前格全部要素一次性挖除。宽度沿用上方大路/小路档。"""
        if not self._require_cell():
            return
        roads = roads if roads is not None else self.osm_layer()
        if roads is None:
            self.log("[提示] 先点「拉取道路」，或确认工程里有「OSM 道路」层")
            return
        xform = None
        if roads.crs() != self.layer.crs():
            xform = QgsCoordinateTransform(
                roads.crs(), self.layer.crs(), QgsProject.instance())
        bufs = []
        roads_wkt = []  # (wkt, 全宽) 逐路记台账：重放不依赖缓存文件还在不在
        n_major = n_minor = 0
        for f in roads.getFeatures():
            if not f.hasGeometry() or f.geometry().isEmpty():
                continue
            g = QgsGeometry(f.geometry())
            if xform is not None:
                g.transform(xform)
            major = str(f["highway"] or "") in osm_roads.MAJOR_CLASSES
            width = self.road_widths[0 if major else 1]
            buf = self._buffer(g, width / 2.0)
            if buf is not None and not buf.isEmpty():
                bufs.append(buf)
                roads_wkt.append((g.asWkt(), width))
            if major:
                n_major += 1
            else:
                n_minor += 1
        if not bufs:
            self.log("[提示] 道路层里没有可用线要素")
            return
        self._ledger_prepare()
        eraser = _unary_union(bufs)
        if eraser.isEmpty():
            self.log("[错误] 道路缓冲合并失败")
            return
        before = sum(f.geometry().area() for f in
                     self.layer.getFeatures(self._cell_filter()))
        self.layer.beginEditCommand("OSM 道路挖除")
        self._run_difference(eraser)
        self.layer.endEditCommand()
        after_feats = list(self.layer.getFeatures(self._cell_filter()))
        after = sum(f.geometry().area() for f in after_feats)
        removed = before - after
        self.layer.triggerRepaint()
        if removed < 1.0:
            self.log("[OSM 道路] 没有新的变化——道路缓冲与当前格地块不相交，"
                     "或这批道路已经挖除过")
        else:
            self.log(f"[OSM 道路] 大路 {n_major} 条×{self.road_widths[0]:.0f} m、"
                     f"小路 {n_minor} 条×{self.road_widths[1]:.0f} m → "
                     f"挖除 {removed:,.0f} m²（{removed / 666.6667:.1f} 亩），"
                     f"现为 {len(after_feats)} 块")
            seq = None
            for wkt, width in roads_wkt:
                seq = self._ledger_record("osm", wkt,
                                          {"width": width}, seq=seq)
        self.report_stats()

    # ---------- E：格子导航 / 进度 / 收尾 QA ----------

    def _grid_cells(self):
        """[(cell_id, row, col, fid)] 按行列排序；渔网缺失返回 None。"""
        grid = self._grid_layer()
        if grid is None:
            return None
        names = [f.name() for f in grid.fields()]

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        cells = []
        for f in grid.getFeatures():
            row = _num(f["Row"]) if "Row" in names else 0.0
            col = _num(f["Col"]) if "Col" in names else 0.0
            cells.append((self._cell_id(f, f.geometry()), row, col, f.id()))
        cells.sort(key=lambda c: (c[1], c[2], c[3]))
        return cells

    def next_cell(self):
        """切到渔网行列顺序的下一格：保存当前格 → 建底板 → 缩放。快捷键 N。"""
        cells = self._grid_cells()
        if not cells:
            self.log("[提示] 请先选择渔网图层")
            return
        idx = -1
        if self.current_cell is not None:
            ids = [c[0] for c in cells]
            if self.current_cell in ids:
                idx = ids.index(self.current_cell)
        if idx + 1 >= len(cells):
            self.log("[i] 已是最后一格")
            return
        grid = self._grid_layer()
        grid.selectByIds([cells[idx + 1][3]])
        self.set_cell_from_selection()

    def cell_stats(self):
        """全部格子聚合：{cell_id: (块数, 面积 m², 有底板)}。"""
        agg = {}
        if self.layer is None:
            return agg
        for f in self.layer.getFeatures():
            cid = f["cell_id"]
            if cid in (None, ""):
                continue
            n, area, has_base = agg.get(cid, (0, 0.0, False))
            if f.hasGeometry():
                area += f.geometry().area()
            agg[cid] = (n + 1, area, has_base or f["source"] == "base")
        return agg

    def qa_cleanup(self, min_area):
        """删除当前格面积小于阈值的碎块（一步撤销）。"""
        if not self._require_cell():
            return
        feats = list(self.layer.getFeatures(self._cell_filter()))
        victims = [(f.id(), f.geometry().area()) for f in feats
                   if f.hasGeometry() and f.geometry().area() < min_area]
        if not victims:
            self.log(f"[QA] 没有小于 {min_area:g} m² 的碎块")
            return
        lost = sum(a for _fid, a in victims)
        self.layer.beginEditCommand("QA 碎块清理")
        self.layer.deleteFeatures([fid for fid, _a in victims])
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        self.log(f"[QA] 已删 {len(victims)} 个碎块，"
                 f"共 {lost:,.0f} m²（{lost / 666.6667:.2f} 亩），可撤销")
        self.report_stats()

    def qa_overlaps(self, min_area=1.0):
        """检查当前格内地块两两重叠，只报告不动手。"""
        if not self._require_cell():
            return
        feats = list(self.layer.getFeatures(self._cell_filter()))
        pairs = 0
        total = 0.0
        for i in range(len(feats)):
            gi = feats[i].geometry()
            for j in range(i + 1, len(feats)):
                gj = feats[j].geometry()
                if gi is None or gj is None or not gi.intersects(gj):
                    continue
                inter = gi.intersection(gj)
                if not inter.isEmpty() and inter.area() > min_area:
                    pairs += 1
                    total += inter.area()
        if pairs:
            self.log(f"[QA] 发现 {pairs} 对重叠地块，"
                     f"总重叠 {total:,.1f} m²（{total / 666.6667:.2f} 亩）— "
                     f"请手工处理或用「选中挖除」修边")
        else:
            self.log("[QA] 无重叠地块 ✓")

    # ---------- F4：QA 三件套（对照验收三查：漏画 / 多画 / 边界不吻合） ----------

    @staticmethod
    def _rings(g):
        """面的外环列表（多部件展开）。"""
        if g is None or g.isEmpty():
            return []
        if g.isMultipart():
            return [poly[0] for poly in g.asMultiPolygon() if poly]
        p = g.asPolygon()
        return [p[0]] if p else []

    def _ring_samples(self, g, eng, reducer, shrink=1.0, grid=None):
        """沿外环按半像元步长采样，取 3×3 邻域 reducer（min/mean）。
        grid 默认梯度代价面；暗斑检查传 eng.lum。"""
        arr = eng.finder.cost if grid is None else grid
        h, w = eng.shape
        step = eng.pixel_size() * 0.5
        vals = []
        for ring in self._rings(g):
            pts = ring
            if shrink != 1.0:  # 向质心收缩（内缩环带≈地块内部）
                cx = sum(p.x() for p in ring) / len(ring)
                cy = sum(p.y() for p in ring) / len(ring)
                pts = [QgsPointXY(cx + (p.x() - cx) * shrink,
                                  cy + (p.y() - cy) * shrink) for p in ring]
            for a, b in zip(pts, pts[1:] + [pts[0]]):
                d = math.hypot(b.x() - a.x(), b.y() - a.y())
                if d <= 0:
                    continue
                n = max(1, int(d / step))
                for i in range(n + 1):
                    t = i / n
                    x = a.x() + (b.x() - a.x()) * t
                    y = a.y() + (b.y() - a.y()) * t
                    c, r = eng.to_px(QgsPointXY(x, y))
                    c = min(max(c, 1), w - 2)
                    r = min(max(r, 1), h - 2)
                    patch = arr[r - 1:r + 2, c - 1:c + 2]
                    vals.append(float(reducer(patch)))
        return vals

    def qa_holes(self, min_area=666.7):
        """漏画检查：当前格减去全部地块 → 大于阈值的空洞做成「漏画候选」
        内存图层。注意其中也含道路/城镇等有意挖除的洞，需目视判别。"""
        if not self._require_cell():
            return []
        geoms = [f.geometry() for f in
                 self.layer.getFeatures(self._cell_filter())]
        rest = QgsGeometry(self.cell_geom)
        if geoms:
            rest = self.cell_geom.difference(_unary_union(geoms))
        holes = [p for p in poly_parts(rest) if p.area() >= min_area]
        holes.sort(key=lambda g: -g.area())
        for L in QgsProject.instance().mapLayersByName("漏画候选"):
            QgsProject.instance().removeMapLayer(L.id())
        if holes:
            vl = QgsVectorLayer(
                "polygon?crs=" + self.layer.crs().authid(),
                "漏画候选", "memory")
            vl.dataProvider().addAttributes([
                QgsField("cell_id", QMetaType.Type.QString),
                QgsField("area_mu", QMetaType.Type.Double)])
            vl.updateFields()
            try:
                from qgis.core import QgsFillSymbol
                vl.setRenderer(QgsSingleSymbolRenderer(
                    QgsFillSymbol.createSimple({
                        "color": "255,0,0,30",
                        "outline_color": "255,0,0,255",
                        "outline_width": "0.6"})))
            except Exception:
                pass
            f = QgsFeature(vl.fields())
            for g in holes:
                f.setAttribute("cell_id", self.current_cell)
                f.setAttribute("area_mu", round(g.area() / 666.6667, 2))
                f.setGeometry(g)
                vl.dataProvider().addFeature(f)
            vl.updateExtents()
            QgsProject.instance().addMapLayer(vl)
        total = sum(g.area() for g in holes)
        if holes:
            self.log(f"[QA][漏画] {len(holes)} 个空洞 ≥ {min_area:,.0f} m²，"
                     f"共 {total / 666.6667:,.1f} 亩，最大 "
                     f"{holes[0].area() / 666.6667:,.1f} 亩"
                     "（已加「漏画候选」图层，含道路/城镇等有意挖除，请目视判别）")
        else:
            self.log(f"[QA][漏画] 无 ≥ {min_area:,.0f} m² 的空洞 ✓")
        return holes

    def qa_dark(self, p_lo=3.0):
        """疑似水体/阴影提示：外环内缩 0.85 环带的平均亮度低于全窗低
        分位的地块列出（只提示不删，验收查『多画：水体』）。"""
        if not self._require_cell():
            return []
        eng, _xf = self.lasso_context()
        if eng is None:
            self.log("[QA][暗斑] 没有可用影像，跳过")
            return []
        thr = float(np.percentile(eng.lum, p_lo))
        results = []
        for feat in self.layer.getFeatures(self._cell_filter()):
            g = feat.geometry()
            vals = self._ring_samples(
                g, eng, np.mean, shrink=0.85, grid=eng.lum)
            if len(vals) < 8:
                continue
            mean_lum = sum(vals) / len(vals)
            if mean_lum <= thr:
                results.append((mean_lum, feat.id(), g.area() / 666.6667))
        if results:
            results.sort()
            for lum_v, fid, mu in results[:8]:
                self.log(f"[QA][暗斑]⚠ fid={fid} 内带亮度 {lum_v:.0f}"
                         f"（全窗 P{p_lo:g}={thr:.0f}），疑似水体/阴影（{mu:,.1f} 亩）— 请目视")
        else:
            self.log(f"[QA][暗斑] 无明显暗斑地块 ✓（阈值 P{p_lo:g}={thr:.0f}）")
        return results

    def export_report(self, path):
        """逐格报表 CSV：块数 / 面积 / 亩 / 覆盖率。"""
        grid = self._grid_layer()
        cells = self._grid_cells()
        if grid is None or not cells:
            self.log("[提示] 请先选择渔网图层")
            return
        xform = None
        if grid.crs() != self.layer.crs():
            xform = QgsCoordinateTransform(
                grid.crs(), self.layer.crs(), QgsProject.instance())
        agg = self.cell_stats()
        rows = []
        tot_n = tot_area = tot_cell = 0.0
        for cid, _r, _c, fid in cells:
            gf = grid.getFeature(fid)
            g = QgsGeometry(gf.geometry())
            if xform is not None:
                g.transform(xform)
            cell_area = g.area() if not g.isEmpty() else 0.0
            n, area, _hb = agg.get(cid, (0, 0.0, False))
            rows.append([cid, n, round(area, 1),
                         round(area / 666.6667, 2), round(cell_area, 1),
                         f"{area / cell_area * 100:.1f}%" if cell_area else "-"])
            tot_n += n
            tot_area += area
            tot_cell += cell_area
        rows.append(["合计", int(tot_n), round(tot_area, 1),
                     round(tot_area / 666.6667, 2), round(tot_cell, 1),
                     f"{tot_area / tot_cell * 100:.1f}%" if tot_cell else "-"])
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["格子", "地块数", "耕地面积_m2", "面积_亩",
                        "格子面积_m2", "覆盖率"])
            w.writerows(rows)
        self.log(f"[报表] 已导出 {len(rows) - 1} 格 → {path}")

    # ---------- 统计 / 保存 / 撤销 ----------

    def report_stats(self):
        if self.layer is None or self.current_cell is None:
            return
        n = 0
        area = 0.0
        for f in self.layer.getFeatures(self._cell_filter()):
            n += 1
            area += f.geometry().area()
        mu = area / 666.6667
        self.log(f"[统计] {self.current_cell}：{n} 个要素，"
                 f"{area:,.0f} m²（约 {mu:,.1f} 亩）")
        self.refresh_layer_views()  # 分层视图开着时跟随每笔操作刷新
        dlg = getattr(self, "dialog", None)
        if dlg is not None and hasattr(dlg, "refresh_progress"):
            dlg.refresh_progress()

    def save(self):
        if self.layer is None:
            return
        if self.layer.isEditCommandActive():
            self.layer.endEditCommand()
        if not self.layer.isEditable():
            self.layer.startEditing()
            return
        if not self.layer.commitChanges():
            self.log("[错误] 保存失败，请检查输出文件是否被占用")
            return
        self.layer.startEditing()
        self._ledger_sess += 1
        self._undo_prev = 0
        self.log("[i] 已保存")

    def undo(self):
        if self.layer is None:
            return
        stack = self.layer.undoStack()
        if stack.canUndo():
            stack.undo()
            self.layer.triggerRepaint()
            self.log("[i] 已撤销上一笔")
            self.report_stats()

    # ---------- 操作台账：记录 / 重放 / 回退（Ctrl+Z 自动对齐） ----------

    LEDGER_KIND_LABELS = {
        "base": "🟧 建底板",
        "baseline": "📋 基线快照（存量格）",
        "osm": "🌐 OSM 道路",
        "road": "🛣 道路",
        "rect": "⬛ 框挖",
        "erase": "🟥 多边形挖",
        "add": "🟩 补画",
        "split": "✂ 切分",
    }

    def _on_undo_index(self, idx):
        """撤销栈变化：真撤销时把台账里更晚的步一并丢弃（防脱节）。

        commit/切格/保存后栈清空（count=0）不算撤销，只更新基线。"""
        if self.ledger is None or self.layer is None:
            return
        try:
            stack = self.layer.undoStack()
            if idx < self._undo_prev and stack.count() > 0:
                dropped = self.ledger.drop_after_stack(
                    self._ledger_sess, idx)
                if dropped:
                    self.log(f"[台账] 检测到撤销，已同步丢弃 {dropped} 条台账记录")
            self._undo_prev = idx
        except Exception:
            pass

    def _ledger_stack_pos(self):
        try:
            return self.layer.undoStack().index()
        except Exception:
            return None

    def _ledger_prepare(self):
        """每个落层操作开始前调用：该格第一笔时先拍头部快照（此刻状态
        还没被本操作改动，重放才不会把第一笔算两遍）。"""
        if self.ledger is None or self.current_cell is None:
            return
        if not self.ledger.cell_rows(self.current_cell):
            self._ledger_snapshot_head()

    def _ledger_record(self, kind, wkt, params=None, seq=None):
        """落一笔进台账（操作成功后、endEditCommand 之后调用才能取到栈位；
        头部快照由 _ledger_prepare 在操作前补）。返回步号 seq。"""
        if self.ledger is None or self.current_cell is None:
            return seq
        if seq is None:
            seq = self.ledger.next_seq(self.current_cell)
        self.ledger.add({
            "cell": self.current_cell, "seq": seq, "kind": kind,
            "params": dict(params or {}), "geom": wkt or "",
            "sess": self._ledger_sess, "sidx": self._ledger_stack_pos(),
        })
        return seq

    def _ledger_snapshot_head(self):
        """台账头部：整格底板（base）或存量格的逐要素快照（baseline）。"""
        feats = list(self.layer.getFeatures(self._cell_filter()))
        pristine = (
            len(feats) == 1 and feats[0]["source"] == "base"
            and self.cell_geom is not None
            and abs(feats[0].geometry().area()
                    - self.cell_geom.area()) < 1.0)
        seq = self.ledger.next_seq(self.current_cell)
        sidx = self._ledger_stack_pos()
        if pristine:
            self.ledger.add({"cell": self.current_cell, "seq": seq,
                             "kind": "base", "params": {"source": "base"},
                             "geom": feats[0].geometry().asWkt(),
                             "sess": self._ledger_sess, "sidx": sidx})
        else:
            for f in feats:
                if not f.hasGeometry() or f.geometry().isEmpty():
                    continue
                self.ledger.add({
                    "cell": self.current_cell, "seq": seq,
                    "kind": "baseline",
                    "params": {"source": f["source"] or "base"},
                    "geom": f.geometry().asWkt(),
                    "sess": self._ledger_sess, "sidx": sidx})
            self.log("[台账] 存量格：已拍基线快照（快照之前的历史不可回退）")

    def ledger_steps(self, cell):
        """[(seq, 标签, 行数)] 供台账列表 UI。"""
        steps = self.ledger.steps(cell) if self.ledger is not None else {}
        out = []
        for seq in sorted(steps):
            rows = steps[seq]
            kind = rows[0]["kind"]
            n = len(rows)
            if kind == "baseline":
                label = f"📋 基线快照（{n} 块，存量格起点）"
            elif kind == "osm":
                label = f"🌐 OSM 道路（{n} 条）"
            elif kind == "road":
                label = (f"🛣 道路 "
                         f"{rows[0]['params'].get('width', 0):.0f} m")
            else:
                label = self.LEDGER_KIND_LABELS.get(kind, kind)
            ts = rows[0].get("ts", "")
            stamp = f" · {ts[5:16]}" if ts else ""
            out.append((seq, f"{label}{stamp}", n))
        return out

    def ledger_rollback(self, cell, seq, log_msg=None):
        """回到该格第 seq 步：头部快照重置当前格，依序重放后续各步。

        一次编辑命令完成重放，随后立即落盘并作废撤销栈——回退本身就是
        新历史，Ctrl+Z 不应把台账与几何再次撕开。"""
        if self.layer is None or self.ledger is None:
            return False
        if cell != self.current_cell or self.cell_geom is None:
            self.log("[台账] 只能回退当前格，请先切到该格")
            return False
        steps = self.ledger.steps(cell)
        if not steps or seq not in steps:
            self.log("[台账] 该步不存在")
            return False
        head = steps[min(steps)]
        if not head or head[0]["kind"] not in ("base", "baseline"):
            self.log("[台账] 缺基线头部，无法重放")
            return False
        self.layer.beginEditCommand("台账回退")
        for f in list(self.layer.getFeatures(self._cell_filter())):
            self.layer.deleteFeature(f.id())
        for r in head:
            g = QgsGeometry.fromWkt(r["geom"])
            if not g.isEmpty():
                self.layer.addFeature(self._new_feature(
                    g, r["params"].get("source") or "base"))
        for s in sorted(k for k in steps if k <= seq)[1:]:
            for r in steps[s]:
                self._replay_row(r)
        self.layer.endEditCommand()
        if self.layer.isEditable():
            self.layer.commitChanges()
            self.layer.startEditing()
        self._ledger_sess += 1
        self._undo_prev = 0
        self.ledger.truncate_after(cell, seq)
        self.layer.triggerRepaint()
        self.log(log_msg or f"[台账] 已回到 {cell} 第 {seq} 步，之后的操作已作废")
        self.report_stats()
        return True

    def ledger_delete_step(self, cell, seq):
        """单独删除某一步（其余操作保留并重放）；底板步不可删。"""
        if self.layer is None or self.ledger is None:
            return False
        if cell != self.current_cell or self.cell_geom is None:
            self.log("[台账] 只能操作当前格，请先切到该格")
            return False
        steps = self.ledger.steps(cell)
        if seq not in steps:
            self.log("[台账] 该步不存在")
            return False
        if seq == min(steps):
            self.log("[台账] 底板步不能删——重开请用「选中格子建底板」")
            return False
        n = self.ledger.delete_step(cell, seq)
        last = self.ledger.max_seq(cell)
        ok = self.ledger_rollback(
            cell, last,
            log_msg=f"[台账] 已删除第 {seq} 步（{n} 行），其余操作已重放")
        if ok:
            self.refresh_layer_views()
        return ok

    def _replay_row(self, r):
        kind = r["kind"]
        if kind in ("base", "baseline"):
            return
        g = QgsGeometry.fromWkt(r["geom"]) if r["geom"] else None
        if g is None or g.isEmpty():
            return
        if kind in ("road", "osm"):
            width = float(r["params"].get("width") or self.road_width())
            self._run_difference(self._buffer(g, width / 2.0))
        elif kind in ("rect", "erase"):
            self._run_difference(g)
        elif kind == "add":
            self._run_add(g)
        elif kind == "split":
            self._run_split(g)

    def ledger_set_width(self, cell, seq, width):
        """改某步道路宽度并重放到最后（后续操作保留，个别可能失效需复核）。"""
        if self.ledger is None:
            return False
        n = self.ledger.update_params(cell, seq, {"width": float(width)})
        if not n:
            self.log("[台账] 该步不存在")
            return False
        last = self.ledger.max_seq(cell)
        ok = self.ledger_rollback(cell, last)
        if ok:
            self.log(f"[台账] 第 {seq} 步宽度已改为 {width:g} m 并重放完成")
        return ok

    def ledger_summary(self):
        """{cell: {kind: 条数, "steps": 步数, "last": 最近时间}} 进度总览用。"""
        out = {}
        if self.ledger is None:
            return out
        for r in self.ledger.rows:
            c = out.setdefault(r["cell"], {})
            c[r["kind"]] = c.get(r["kind"], 0) + 1
            c["steps"] = max(c.get("steps", 0), r["seq"])
            if r.get("ts"):
                c["last"] = max(c.get("last", ""), r["ts"])
        return out

    # ---------- 分层视图：底板/补块/扣除 三类独立区域（派生自台账） ----------

    # (键, 图层名, 包含 kind, 填充色 "r,g,b", 是否按道路宽缓冲)
    VIEW_SPECS = (
        ("base", "耕地·底板视图", ("base", "baseline"), "255,170,0", False),
        ("add", "耕地·补块视图", ("add",), "0,200,80", False),
        ("cut", "耕地·扣除视图", ("road", "osm", "rect", "erase"),
         "255,60,60", True),
    )
    VIEW_KIND_NAMES = {"base": "整格底板", "baseline": "存量基线", "road": "道路",
                       "osm": "OSM 道路", "rect": "框挖(城镇等)", "erase": "多边形挖",
                       "add": "补块", "split": "切分"}

    def set_layer_views(self, on):
        """开关分层视图：当前格的操作分解为三个独立图层（从台账派生）。"""
        self._views_on = bool(on)
        if not on:
            for vl in self._view_layers.values():
                try:
                    QgsProject.instance().removeMapLayer(vl.id())
                except Exception:
                    pass
            self._view_layers = {}
            self.log("[分层] 视图已关闭，图层已移除")
            return
        self.log("[分层] 已开启：底板（橙）/ 补块（绿）/ 扣除（红，道路按宽度"
                 "缓冲展示）。在视图里选中区域后可单步删除")
        self.refresh_layer_views()

    def _make_view_layer(self, name, color):
        vl = QgsVectorLayer(
            f"Polygon?crs={self.layer.crs().authid()}", name, "memory")
        vl.dataProvider().addAttributes([
            QgsField("类型", QMetaType.Type.QString),
            QgsField("步号", QMetaType.Type.Int),
            QgsField("宽度m", QMetaType.Type.Double),
        ])
        vl.updateFields()
        try:
            sym = QgsFillSymbol.createSimple({
                "color": f"{color},70", "outline_color": f"{color},255",
                "outline_width": "0.66"})
            vl.renderer().setSymbol(sym)
        except Exception:
            pass
        QgsProject.instance().addMapLayer(vl)
        return vl

    def refresh_layer_views(self):
        """按台账重建三个视图层的要素（当前格）。任何落地/回退后自动调。"""
        if not self._views_on or self.ledger is None or self.layer is None:
            return
        rows = self.ledger.cell_rows(self.current_cell) \
            if self.current_cell else []
        for key, title, kinds, color, buffered in self.VIEW_SPECS:
            vl = self._view_layers.get(key)
            if vl is None or not vl.isValid():
                vl = self._make_view_layer(title, color)
                self._view_layers[key] = vl
            feats = []
            for r in rows:
                if r["kind"] not in kinds:
                    continue
                g = QgsGeometry.fromWkt(r["geom"]) if r["geom"] else None
                if g is None or g.isEmpty():
                    continue
                if buffered and r["kind"] in ("road", "osm"):
                    w = float(r["params"].get("width") or self.road_width())
                    g = self._buffer(g, w / 2.0)
                    if g is None or g.isEmpty():
                        continue
                f = QgsFeature(vl.fields())
                f.setAttributes([
                    self.VIEW_KIND_NAMES.get(r["kind"], r["kind"]),
                    int(r["seq"]),
                    float(r["params"].get("width") or 0.0) or None,
                ])
                f.setGeometry(g)
                feats.append(f)
            dp = vl.dataProvider()
            dp.truncate()
            dp.addFeatures(feats)
            vl.triggerRepaint()

    def delete_selected_ops(self):
        """删除在分层视图里选中的操作步（其余保留并重放）。"""
        if self.ledger is None or self.current_cell is None:
            self.log("[提示] 先打开标注层并选中格子")
            return False
        seqs = set()
        for key, *_ in self.VIEW_SPECS:
            vl = self._view_layers.get(key)
            if vl is None:
                continue
            for f in vl.selectedFeatures():
                try:
                    seqs.add(int(f["步号"]))
                except (TypeError, ValueError):
                    pass
        if not seqs:
            self.log("[提示] 先在分层视图里选中要删的区域（可框选多个）")
            return False
        ok_any = False
        for s in sorted(seqs, reverse=True):
            ok_any = self.ledger_delete_step(self.current_cell, s) or ok_any
        if ok_any:
            self.refresh_layer_views()
        return ok_any

    def close_session(self):
        """对话框关闭：提交未保存编辑，结束编辑会话。"""
        self._shutdown_sam()  # GPU 进程与临时目录随会话释放（无论层是否开过）
        if self._views_on:
            self.set_layer_views(False)
        if self.layer is None:
            return
        if self.layer.isEditCommandActive():
            self.layer.endEditCommand()
        if self.layer.isEditable():
            if self.layer.commitChanges():
                self.log("[i] 关闭前已自动保存")
            else:
                self.layer.rollBack()
                self.log("[警告] 自动保存失败，未提交的修改已回滚")
        self.layer.triggerRepaint()


def _plugin_version():
    """读 metadata.txt 版本号（现场排查"跑的是哪版"用）。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "metadata.txt")
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                if ln.startswith("version="):
                    return ln.strip().split("=", 1)[1]
    except Exception:
        pass
    return "?"


class FoldGroup(QGroupBox):
    """点击标题栏折叠/展开的分组（▾ 展开 / ▸ 折叠）。

    自包含实现：不用 QgsCollapsibleGroupBox——其折叠的高度控制在
    dock+滚动区环境下不生效（QGIS 4 实测 maximumHeight 未被设置）。"""

    def __init__(self, title, collapsed=False):
        self._base_title = title
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击标题栏折叠 / 展开")
        outer = QVBoxLayout(self)
        self._content = QWidget()
        outer.addWidget(self._content)
        self._folded = not collapsed  # 先置反，保证 set_folded 一定执行
        self.set_folded(collapsed)

    def content(self):
        """折叠区容器：子控件往它身上布。"""
        return self._content

    def is_folded(self):
        return self._folded

    def set_folded(self, fold):
        fold = bool(fold)
        if fold == self._folded:
            return
        self._folded = fold
        self._content.setVisible(not fold)
        self.setTitle(("▸ " if fold else "▾ ") + self._base_title)
        # 折叠改变 sizeHint：主动让外层（含滚动区）重排，
        # 否则部分平台的事件循环惰性下高度不更新
        w = self.parentWidget()
        while w is not None:
            w.updateGeometry()
            if isinstance(w, QScrollArea):
                if w.widget() is not None:
                    w.widget().adjustSize()
                break
            w = w.parentWidget()

    def _title_bar_h(self):
        return self.fontMetrics().height() + 8

    def mouseReleaseEvent(self, e):
        try:
            y = e.position().toPoint().y()
        except AttributeError:  # Qt5 兼容
            y = e.y()
        if y <= self._title_bar_h():
            self.set_folded(not self._folded)
            e.accept()
            return
        super().mouseReleaseEvent(e)


class AnnotateDock(QDockWidget):
    """标注工作台侧边栏（可停靠/浮动，与地图并排常驻不抢焦点）。"""

    def __init__(self, iface):
        super().__init__("耕地标注工作台 — 减法勾绘", iface.mainWindow())
        # objectName 唯一：QGIS 才能记住停靠位置，且出现在 视图→面板 菜单
        self.setObjectName("cropland_annotate_dock")
        self.iface = iface
        self.wb = AnnotationController(iface, self.append_log)
        self.wb.dialog = self
        self.tools = {}
        self.setMinimumWidth(520)
        self.setup_ui()
        self._shortcuts = self._make_shortcuts()
        # 键盘兜底：Esc/回车/退格/Ctrl+Z 不再依赖画布持焦点（侧栏常驻后
        # 焦点常落在面板上），也避开 QShortcut 挂画布在 reload 后残留歧义
        QApplication.instance().installEventFilter(self)
        # 打开即自动接续：识别工程里已有的标注层（重启 QGIS/重开工作台
        # 不必再手动选渔网+打开文件，也不产生重复图层）
        QTimer.singleShot(0, self._auto_reopen)

    def _auto_reopen(self):
        if self.wb.layer is not None:
            return
        qs = QSettings()
        saved = qs.value("cropland_delineator/last_cell", "", type=str) or ""
        path = saved.rpartition("|")[0]
        if not (path and os.path.exists(path)):
            # 没有上次记录：退而求其次，认工程里现成的「耕地标注」层
            for L in QgsProject.instance().mapLayers().values():
                if (isinstance(L, QgsVectorLayer) and L.isValid()
                        and L.name() == "耕地标注"):
                    p = L.source().split("|")[0]
                    if os.path.exists(p):
                        path = p
                        break
        if not path:
            return
        grid_src = qs.value("cropland_delineator/last_grid", "", type=str) or ""
        if grid_src:
            for L in QgsProject.instance().mapLayers().values():
                if (isinstance(L, QgsVectorLayer) and L.isValid()
                        and L.source() == grid_src):
                    self.cbo_grid.setLayer(L)
                    break
        self.edit_path.setText(path)
        try:
            if self.wb.open_layer(path):
                self._build_tools()
                self.wb.try_select_aitracer_target()
                self.append_log("[i] 已自动接续标注层与上次工作格")
        except Exception as exc:
            self.append_log(f"[提示] 自动接续失败：{exc!r}——请手动打开")
        self._sync_ui()

    def eventFilter(self, obj, ev):
        """按键兜底：只要当前工具是我们的笔刷且笔迹进行中，Esc/回车/
        退格/Ctrl+Z 无论焦点在主窗口哪里都作用于笔迹。

        严格限定范围避免劫持：工作台隐藏、非本插件工具、其它窗口
        （对话框/弹层）、文本输入框内的一律放行。

        Ctrl+Z 特殊：主窗口原生撤销是 QShortcut，按键先以
        ShortcutOverride 事件征询焦点控件，无人认领则快捷键直接触发、
        KeyPress 根本不会进到这里（v0.8.10 实机事故：点选落点后按
        Ctrl+Z 撤掉的是底板）。因此笔迹进行中的 Ctrl+Z 要在
        ShortcutOverride 阶段就 accept 截胡，按键随即以普通 KeyPress
        回到下面同一套梯级；无笔迹时放行，让原生撤销照常工作。"""
        ty = ev.type()
        if (ty not in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride)
                or not self.isVisible()):
            return super().eventFilter(obj, ev)
        if not isinstance(obj, QWidget):
            # 应用级过滤器会看到非控件目标（QWindow 等，Qt6 下部分按键
            # 直接发给窗口对象）：它们没有 window()/isAncestorOf，直接放行
            return super().eventFilter(obj, ev)
        if isinstance(obj, (QLineEdit, QPlainTextEdit)):
            return super().eventFilter(obj, ev)  # 打字场景：按键属于输入框
        mw = self.iface.mainWindow()
        if obj.window() is not mw and obj is not self and not self.isAncestorOf(obj):
            return super().eventFilter(obj, ev)  # 主窗口/本侧栏之外不碰
        tool = self.iface.mapCanvas().mapTool()
        if not isinstance(tool, map_tools.WorkbenchTool):
            return super().eventFilter(obj, ev)
        key, mods = ev.key(), ev.modifiers()
        ctrl_z = (key == Qt.Key.Key_Z
                  and mods == Qt.KeyboardModifier.ControlModifier)
        if ty == QEvent.Type.ShortcutOverride:
            if ctrl_z and tool.has_stroke():
                ev.accept()   # 认领：原生撤销快捷键不再触发
                return True
            return super().eventFilter(obj, ev)
        if tool.has_stroke():
            if key == Qt.Key.Key_Escape:
                tool.cancel()
                return True
            if key == Qt.Key.Key_Backspace or ctrl_z:
                tool.remove_last()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                tool._finish()
                return True
        elif ctrl_z:
            # 笔迹已收：Ctrl+Z=撤销上一笔落地（显式走原生撤销，
            # 不经过快捷键系统，reload 残留干扰不到）
            self._native_undo()
            return True
        return super().eventFilter(obj, ev)

    def _teardown(self):
        """插件卸载时摘除应用级事件过滤器（快捷键随 dock 一并销毁）。"""
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass

    def _make_shortcuts(self):
        """E=选中即挖除、N=下一格、M=磁力切分、R=磁力道路（应用级，输入框打字不受影响）。

        注意不在这里建 Ctrl+Z：QShortcut 挂画布的话，reloadPlugin 后旧快捷键
        残留在画布上，与新会话同名快捷键被 Qt 判为歧义而双双哑火（实测）。
        Ctrl+Z/Esc/回车/退格统一走 eventFilter 兜底（见 eventFilter）。"""
        try:
            from qgis.PyQt.QtGui import QShortcut
        except ImportError:  # Qt5 的 QShortcut 在 QtWidgets
            from qgis.PyQt.QtWidgets import QShortcut
        shortcuts = []

        def when_visible(fn):
            # 侧边栏隐藏（会话已收）时 E/N/M/R 不响应，防误触
            def run():
                if self.isVisible():
                    fn()
            return run

        for key, fn in (("E", self.wb.erase_selected),
                        ("N", self.wb.next_cell),
                        ("M", self._shortcut_msplit),
                        ("R", self._shortcut_mroad)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(when_visible(fn))
            shortcuts.append(sc)
        return shortcuts

    def _native_undo(self):
        from qgis.PyQt.QtWidgets import QAction
        act = self.iface.mainWindow().findChild(QAction, "mActionUndo")
        if act is not None:
            act.trigger()
        else:
            self.wb.undo()

    # ---------- 操作台账 / 进度总览 ----------

    def _open_ledger(self):
        cell = self.wb.current_cell
        if cell is None or self.wb.ledger is None:
            self.append_log("[提示] 先打开标注层并选中格子")
            return
        steps = self.wb.ledger_steps(cell)
        if not steps:
            self.append_log("[台账] 该格还没有台账记录（画一笔即开始记录）")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"操作台账 — {cell}")
        dlg.setMinimumSize(430, 380)
        v = QVBoxLayout(dlg)
        lst = QListWidget()
        for seq, label, _n in steps:
            lst.addItem(f"{seq:>3}  {label}")
        lst.setCurrentRow(len(steps) - 1)
        v.addWidget(lst)
        row = QHBoxLayout()
        btn_back = QPushButton("↩ 回到此步")
        btn_width = QPushButton("🛣 改宽并重放")
        btn_del = QPushButton("✕ 删除此步")
        btn_close = QPushButton("关闭")
        row.addWidget(btn_back)
        row.addWidget(btn_width)
        row.addWidget(btn_del)
        row.addWidget(btn_close)
        v.addLayout(row)
        tip = QLabel("「回到此步」= 重放该步及之前的历史，之后的操作作废；"
                     "「删除此步」= 只删这一步，其余操作保留并重放（也可在"
                     "🥞分层视图里选中区域后删除）。道路步可改宽度后重放到最后。"
                     "「选中挖除」「QA 清理」等修正操作不入台账，回退时会被一并抹掉。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        def selected():
            i = lst.currentRow()
            return steps[i][0] if 0 <= i < len(steps) else None

        def do_back():
            seq = selected()
            if seq is None:
                return
            if QMessageBox.question(
                    dlg, "台账回退",
                    f"确定回到第 {seq} 步？\n该步之后的操作将全部作废。"
                    ) != QMessageBox.StandardButton.Yes:
                return
            if self.wb.ledger_rollback(cell, seq):
                dlg.accept()

        def do_width():
            seq = selected()
            if seq is None:
                return
            rows = self.wb.ledger.steps(cell).get(seq, [])
            if not rows or rows[0]["kind"] not in ("road", "osm"):
                QMessageBox.information(dlg, "改宽", "请先选中道路类的一步")
                return
            cur = float(rows[0]["params"].get("width", 25.0))
            width, ok = QInputDialog.getDouble(
                dlg, "改宽重放", "道路全宽（米）", cur, 2.0, 80.0, 1)
            if not ok:
                return
            if self.wb.ledger_set_width(cell, seq, width):
                dlg.accept()

        def do_del():
            seq = selected()
            if seq is None:
                return
            if QMessageBox.question(
                    dlg, "删除单步",
                    f"确定删除第 {seq} 步？\n其余操作保留并自动重放。"
                    ) != QMessageBox.StandardButton.Yes:
                return
            if self.wb.ledger_delete_step(cell, seq):
                dlg.accept()

        btn_back.clicked.connect(do_back)
        btn_width.clicked.connect(do_width)
        btn_del.clicked.connect(do_del)
        btn_close.clicked.connect(dlg.reject)
        dlg.show()

    def _open_progress(self):
        """渔网全部格子的 块数/面积/阶段/台账 汇总（只读）。"""
        stats = self.wb.cell_stats()
        summary = self.wb.ledger_summary()
        cells = self.wb._grid_cells()
        ids = [c[0] for c in cells] if cells else sorted(stats)
        lines = [f"{'格子':<16}{'块数':>4} {'面积(亩)':>10}  "
                 f"底板 OSM  手挖  补画  切分  台账步  最近操作"]
        for cid in ids:
            n, area, has_base = stats.get(cid, (0, 0.0, False))
            s = summary.get(cid, {})

            def mark(k, s=s):
                v = s.get(k, 0)
                return f"{v:>3}" if v else "  ·"

            head = "✓" if (has_base or s.get("base")
                           or s.get("baseline")) else "·"
            digs = int(s.get("rect", 0)) + int(s.get("erase", 0))
            lines.append(
                f"{cid:<16}{n:>4} {area / 666.6667:>10,.1f}  "
                f"{head}   {mark('osm')}  {digs:>3}  {mark('add')}"
                f"  {mark('split')}  {s.get('steps', 0):>5}"
                f"  {s.get('last', '')[:16]}")
        done = sum(1 for c in ids if c in stats)
        lines.append("")
        lines.append(f"已开工 {done}/{len(ids)} 格"
                     + ("" if cells else "（渔网未选，仅列台账中出现的格子）"))
        dlg = QDialog(self)
        dlg.setWindowTitle("处理进度总览")
        dlg.setMinimumSize(640, 320)
        v = QVBoxLayout(dlg)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        try:
            txt.setFontFamily("monospace")
        except Exception:
            pass
        txt.setPlainText("\n".join(lines))
        v.addWidget(txt)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.reject)
        v.addWidget(btn)
        dlg.show()

    def _shortcut_msplit(self):
        if self.wb.layer is None:
            return
        btn = self.tool_btns.get("msplit")
        if btn is not None:
            btn.setChecked(True)
        self.activate_tool("msplit")

    def _shortcut_mroad(self):
        if self.wb.layer is None:
            return
        btn = self.tool_btns.get("mroad")
        if btn is not None:
            btn.setChecked(True)
        self.activate_tool("mroad")

    # ---------- UI ----------

    def setup_ui(self):
        """三层布局：可折叠的一次性设置与收尾 QA 包住常驻的每格循环操作，
        日志弹性撑满剩余高度——高频操作始终在视野内，低频功能一步可达。"""
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ① 标注数据：一次性设置，默认折叠（点标题栏展开/收起）
        data_box = FoldGroup("① 标注数据（渔网 / 影像 / 成果文件）", collapsed=True)
        form = QFormLayout(data_box.content())
        self.cbo_grid = QgsMapLayerComboBox()
        self.cbo_grid.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self.cbo_grid.layerChanged.connect(self._suggest_path)
        form.addRow("渔网图层：", self.cbo_grid)

        self.cbo_raster = QgsMapLayerComboBox()
        self.cbo_raster.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.cbo_raster.setToolTip(
            "磁力切分沿线走的影像；留空则自动探测覆盖当前格的栅格")
        try:
            self.cbo_raster.setAllowEmptyLayer(True)
            self.cbo_raster.setLayer(None)
        except AttributeError:  # 旧版无空选项
            pass
        self.cbo_raster.layerChanged.connect(self._raster_changed)
        form.addRow("影像图层：", self.cbo_raster)

        path_row = QHBoxLayout()
        self.edit_path = QLineEdit()
        self.edit_path.setToolTip("标注成果 GPKG；每笔自动进编辑缓冲，保存/切格/关闭时落盘")
        if self.cbo_grid.currentLayer() is not None:
            self._suggest_path()
        path_row.addWidget(self.edit_path)
        btn_browse = QToolButton()
        btn_browse.setText("…")
        btn_browse.clicked.connect(self.browse)
        path_row.addWidget(btn_browse)
        form.addRow("标注文件：", path_row)

        self.btn_open = QPushButton("打开 / 新建标注层")
        self.btn_open.clicked.connect(self.open_layer)
        form.addRow(self.btn_open)
        layout.addWidget(data_box)

        # ② 当前格子：每格打底动作（建底板 / 翻格 / OSM 挖除）
        cell_box = QGroupBox("② 当前格子")
        cell_box.setFlat(True)
        cell_box.setToolTip(
            "建底板前先在渔网图层选中格子。OSM：先按渔网范围从 Overpass 拉道路"
            "中心线（缓存在渔网目录），再对当前格一键按大/小路档宽度挖除；"
            "漏网小路用 🧲🛣 磁力道路或 🛣 道路笔刷补")
        cl = QVBoxLayout(cell_box)
        crow = QHBoxLayout()
        self.btn_base = QPushButton("选中格子 → 建底板")
        self.btn_base.setToolTip("在渔网图层选中一个格子后点此键；底板已存在则切换到该格")
        self.btn_base.clicked.connect(self.wb.set_cell_from_selection)
        self.btn_zoom = QPushButton("缩放到格子")
        self.btn_zoom.clicked.connect(self.wb.zoom_to_cell)
        self.btn_next = QPushButton("➡ 下一格\n(N)")
        self.btn_next.setToolTip("保存当前格 → 渔网行列顺序的下一格 → 建底板 → 缩放")
        self.btn_next.clicked.connect(self.wb.next_cell)
        crow.addWidget(self.btn_base, 2)
        crow.addWidget(self.btn_zoom, 1)
        crow.addWidget(self.btn_next, 1)
        cl.addLayout(crow)
        orow = QHBoxLayout()
        self.btn_fetch = QPushButton("🌐 拉取 OSM 道路")
        self.btn_fetch.clicked.connect(self.fetch_osm)
        self.btn_osm_erase = QPushButton("🛣 按 OSM 道路挖除当前格")
        self.btn_osm_erase.clicked.connect(self.osm_erase)
        orow.addWidget(self.btn_fetch, 1)
        orow.addWidget(self.btn_osm_erase, 2)
        cl.addLayout(orow)
        layout.addWidget(cell_box)

        # ③ 笔刷：2×4 网格（线类一行、面类一行，高频在前）+ 宽度档
        tool_box = QGroupBox("③ 笔刷（左键加点 · 右键/Enter 收笔 · Esc 取消）")
        tool_box.setFlat(True)
        tool_box.setToolTip(
            "宽度档：笔刷激活时按 1/2 切换大/小路，[ ] 微调；"
            "🛣 道路与 🧲🛣 磁力道路按当前档位宽度缓冲扣除")
        tl = QGridLayout(tool_box)
        tl.setHorizontalSpacing(4)
        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)
        self.tool_btns = {}
        tools = (
            ("msplit", "🧲 磁力切分", "点起点/终点锚点，自动沿影像上的田埂/小路走线再切分；"
             "虚线=建议线（点击即采纳），青色框=计算范围；右键/Enter 收笔，"
             "Backspace / Ctrl+Z 退点（快捷键 M）"),
            ("mroad", "🧲🛣 磁力道路", "点起点/终点锚点，自动沿影像上道路的中心线走线"
             "（红带=将按档位宽度扣除的范围），收笔即从底板扣除——适合 OSM "
             "路网没覆盖、需手动选除的道路；1/2 换大/小路档，[ ] 调宽，"
             "Backspace / Ctrl+Z 退点（快捷键 R）"),
            ("road", "🛣 道路", "沿路画中心线，自动按当前档位宽度缓冲并从底板扣除"),
            ("split", "✂ 切分", "画一条线把大地块从缝隙处分成两块（田间小路）"),
            ("rect", "⬛ 框挖", "拖框扣除城镇等连片建设区"),
            ("erase", "🟥 多边形挖", "画多边形扣除非耕地（林地、水域等不规则区域）"),
            ("add", "🟩 补画", "画多边形直接补一块耕地（建筑密集格用加法），自动裁回格子"),
            ("clickseg", "🖱 点选", "SAM 点选分割：左键加正点（目标内部），"
             "右键加负点（排除误粘部分），AI 沿影像边界实时出掩码预览，"
             "Enter 落地、Esc 取消、Backspace/Ctrl+Z 退点（没有落点时 "
             "Ctrl+Z=撤销上一笔已落地）；按 1=补画（绿）2=挖除（红）。"
             "首次使用每格需几秒载入影像"),
        )
        for i, (key, label, tip) in enumerate(tools):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setAutoDefault(False)
            self.btn_group.addButton(btn)
            tl.addWidget(btn, i // 4, i % 4)
            self.tool_btns[key] = btn
            btn.clicked.connect(lambda _c, k=key: self.activate_tool(k))
        wrow = QHBoxLayout()
        wrow.addWidget(QLabel("宽度档：大路"))
        self.spin_major = QDoubleSpinBox()
        self.spin_major.setRange(1, 500)
        self.spin_major.setDecimals(0)
        self.spin_major.setSuffix(" m")
        self.spin_major.setValue(self.wb.road_widths[0])
        wrow.addWidget(self.spin_major)
        wrow.addSpacing(10)
        wrow.addWidget(QLabel("小路"))
        self.spin_minor = QDoubleSpinBox()
        self.spin_minor.setRange(1, 500)
        self.spin_minor.setDecimals(0)
        self.spin_minor.setSuffix(" m")
        self.spin_minor.setValue(self.wb.road_widths[1])
        wrow.addWidget(self.spin_minor)
        wrow.addStretch(1)
        self.spin_major.valueChanged.connect(
            lambda v: self._set_width(0, v))
        self.spin_minor.valueChanged.connect(
            lambda v: self._set_width(1, v))
        tl.addLayout(wrow, 2, 0, 1, 4)
        layout.addWidget(tool_box)

        # ④ 常用动作条：每格反复按的四个键，与笔刷同屏常驻
        action_row = QHBoxLayout()
        action_row.setSpacing(4)
        self.btn_erase_sel = QPushButton("⭕ 选中即挖除 (E)")
        self.btn_erase_sel.setToolTip(
            "选中一个/多个多边形（AITracer 接纳的紫色面、手画面、任意多边形图层），"
            "按 E 把它们当精确橡皮从当前格挖除，被选面随后删除。一步撤销。")
        self.btn_erase_sel.clicked.connect(self.wb.erase_selected)
        self.btn_undo = QPushButton("↩ 撤销一笔")
        self.btn_undo.clicked.connect(self.wb.undo)
        self.btn_ledger = QPushButton("⏱ 台账…")
        self.btn_ledger.setToolTip(
            "当前格的每笔操作列表：可回到任意一步（确定性重放），"
            "道路步可改宽度后重放")
        self.btn_ledger.clicked.connect(self._open_ledger)
        self.btn_save = QPushButton("💾 保存")
        self.btn_save.clicked.connect(self.wb.save)
        action_row.addWidget(self.btn_erase_sel)
        action_row.addWidget(self.btn_undo)
        action_row.addWidget(self.btn_ledger)
        action_row.addWidget(self.btn_save)
        layout.addLayout(action_row)

        # ⑤ 收尾 QA：低频检视，默认折叠（收尾阶段展开）
        qa_box = FoldGroup("⑤ 收尾 QA（碎块 / 重叠 / 漏画 / 报表 / 总览）", collapsed=True)
        pl = QVBoxLayout(qa_box.content())
        self.lst_cells = QListWidget()
        self.lst_cells.setMaximumHeight(110)
        self.lst_cells.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pl.addWidget(self.lst_cells)
        qrow = QHBoxLayout()
        self.spin_min_area = QDoubleSpinBox()
        self.spin_min_area.setRange(1, 100000)
        self.spin_min_area.setDecimals(1)
        self.spin_min_area.setSuffix(" m²")
        self.spin_min_area.setValue(666.7)
        self.btn_qa_clean = QPushButton("🧹 清理碎块")
        self.btn_qa_clean.setToolTip("删除当前格面积小于阈值的碎块（默认 1 亩），可撤销")
        self.btn_qa_clean.clicked.connect(
            lambda: self.wb.qa_cleanup(self.spin_min_area.value()))
        self.btn_qa_check = QPushButton("重叠检查")
        self.btn_qa_check.clicked.connect(self.wb.qa_overlaps)
        qrow.addWidget(self.spin_min_area)
        qrow.addWidget(self.btn_qa_clean)
        qrow.addWidget(self.btn_qa_check)
        qrow.addStretch(1)
        pl.addLayout(qrow)
        qrow2 = QHBoxLayout()
        self.btn_qa_holes = QPushButton("🕳 漏画检查")
        self.btn_qa_holes.setToolTip(
            "当前格未覆盖的大空洞做成「漏画候选」图层（含道路/城镇等有意挖除，目视判别）")
        self.btn_qa_holes.clicked.connect(
            lambda: self.wb.qa_holes(self.spin_min_area.value()))
        self.btn_qa_dark = QPushButton("💧 暗斑提示")
        self.btn_qa_dark.setToolTip("列出内部亮度异常低的地块（疑似把水体/阴影画进来了）")
        self.btn_qa_dark.clicked.connect(self.wb.qa_dark)
        self.btn_report = QPushButton("导出报表 CSV")
        self.btn_report.clicked.connect(self.export_report)
        self.btn_progress = QPushButton("📊 进度总览")
        self.btn_progress.setToolTip("渔网全部格子的块数/面积/阶段/台账步数总览")
        self.btn_progress.clicked.connect(self._open_progress)
        qrow2.addWidget(self.btn_qa_holes)
        qrow2.addWidget(self.btn_qa_dark)
        qrow2.addWidget(self.btn_report)
        qrow2.addWidget(self.btn_progress)
        pl.addLayout(qrow2)
        qrow3 = QHBoxLayout()
        self.btn_views = QPushButton("🥞 分层视图")
        self.btn_views.setCheckable(True)
        self.btn_views.setToolTip(
            "把当前格的操作分解成三个独立图层：底板（橙）/ 补块（绿）/ "
            "扣除（红，道路按宽度缓冲展示）——最终耕地=底板+补块−扣除。\n"
            "在视图里选中一个/多个区域后点「删除选中操作」即可单步删除并自动重放")
        self.btn_views.toggled.connect(self.wb.set_layer_views)
        self.btn_del_ops = QPushButton("🗑 删除选中操作")
        self.btn_del_ops.setToolTip(
            "删除在分层视图里选中的操作步（可框选多个；其余操作保留并自动重放）")
        self.btn_del_ops.clicked.connect(self.wb.delete_selected_ops)
        qrow3.addWidget(self.btn_views, 1)
        qrow3.addWidget(self.btn_del_ops, 2)
        pl.addLayout(qrow3)
        layout.addWidget(qa_box)

        # ⑥ 日志：弹性撑满剩余高度（dock 越高日志越长，矮了出滚动条）
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(72)
        layout.addWidget(self.log_box, 1)

        self._sync_ui()
        self.append_log(f"[i] 工作台就绪 v{_plugin_version()}：展开"
                        "「① 标注数据」→ 选渔网/文件 → 打开"
                        "→ 选中格建底板 → 开画")
        self.append_log("[i] 快捷键：E 选中即挖除 · N 下一格 · M 磁力切分 · "
                        "画路按住 Shift 正交锁")
        # 内容整体进滚动区：侧边栏高度不够时滚动，不挤压布局
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self.setWidget(scroll)

    def _raster_changed(self, layer):
        if layer is not None and layer.isValid():
            self.wb.raster_layer = layer
            self.append_log(f"[磁力] 影像绑定：{layer.name()}")
        else:
            self.wb.raster_layer = None
        self.wb._lasso_key = None  # 强制下次重建引擎

    def _set_width(self, idx, value):
        self.wb.road_widths[idx] = float(value)

    def _suggest_path(self, *_):
        grid = self.grid_layer()
        if grid is None:
            return
        src = grid.source().split("|")[0]
        if src.startswith("/") and os.path.isfile(src):
            default = os.path.join(os.path.dirname(src), "耕地标注.gpkg")
            current = self.edit_path.text().strip()
            # 只在用户没改过路径时跟随渔网目录刷新默认值
            if not current or current.endswith("耕地标注.gpkg"):
                self.edit_path.setText(default)

    def browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "标注文件", self.edit_path.text().strip() or "", "GeoPackage (*.gpkg)")
        if path:
            self.edit_path.setText(path)

    # ---------- 行为 ----------

    def grid_layer(self):
        return self.cbo_grid.currentLayer()

    def append_log(self, msg):
        self.log_box.appendPlainText(msg)

    def open_layer(self):
        path = self.edit_path.text().strip()
        if not path:
            QMessageBox.warning(self, "缺少路径", "请先填写或选择标注文件路径")
            return
        grid = self.grid_layer()
        if grid is None:
            QMessageBox.warning(self, "缺少渔网", "请先选择渔网图层")
            return
        try:
            ok = self.wb.open_layer(path)
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", f"{exc}")
            return
        if ok:
            self._build_tools()
            self.wb.try_select_aitracer_target()
        self._sync_ui()

    def _build_tools(self):
        canvas = self.iface.mapCanvas()
        self.tools = {
            "road": map_tools.PolylineTool(canvas, self.wb, "line", "yellow", "road"),
            "split": map_tools.PolylineTool(canvas, self.wb, "line", "cyan", "split"),
            "msplit": map_tools.MagneticSplitTool(canvas, self.wb),
            "mroad": map_tools.MagneticSplitTool(canvas, self.wb, "road"),
            "rect": map_tools.RectEraseTool(canvas, self.wb),
            "erase": map_tools.PolygonTool(canvas, self.wb, "erase"),
            "add": map_tools.PolygonTool(canvas, self.wb, "add"),
            "clickseg": map_tools.ClickSegTool(canvas, self.wb),
        }

    # ---------- OSM 道路先验 ----------

    def fetch_osm(self):
        grid = self.grid_layer()
        if grid is None:
            QMessageBox.warning(self, "缺少渔网", "请先选择渔网图层")
            return
        self.btn_fetch.setEnabled(False)
        self.btn_fetch.setText("拉取中…")
        self.append_log("[OSM] 正在从 Overpass 拉取道路（可能需要几十秒）…")
        self._fetch = osm_roads.FetchThread(grid)
        self._fetch.done.connect(self._osm_fetched)
        self._fetch.start()

    def _osm_fetched(self, result):
        self.btn_fetch.setEnabled(True)
        self.btn_fetch.setText("🌐 拉取道路")
        grid = self.grid_layer()
        if result.get("error") or not result.get("geojson"):
            msg = result.get("error") or "未取到道路数据"
            self.append_log(f"[警告] {msg}")
            cache = osm_roads.cache_path(grid) if grid is not None else None
            if cache and os.path.exists(cache):
                self.append_log(f"[OSM] 改用缓存：{cache}")
                self._load_roads(cache)
            else:
                QMessageBox.warning(self, "OSM 拉取失败", msg)
            return
        self.append_log(f"[OSM] {result['endpoint']} 取到 "
                        f"{len(result['geojson'])} 条道路")
        dest = self.wb.layer.crs() if self.wb.layer is not None else None
        path = osm_roads.save_roads(result["geojson"], grid, dest)
        if path is None:
            QMessageBox.warning(self, "OSM 保存失败", "道路缓存写入失败")
            return
        self._load_roads(path)

    def _load_roads(self, path):
        vl = QgsVectorLayer(path + "|layername=osm_roads", "OSM 道路", "ogr")
        if not vl.isValid():
            self.append_log(f"[错误] 道路缓存无效：{path}")
            return
        QgsProject.instance().addMapLayer(vl)
        self.wb.roads_layer = vl
        counts = {}
        for f in vl.getFeatures():
            hw = str(f["highway"] or "其他")
            counts[hw] = counts.get(hw, 0) + 1
        top = "、".join(f"{k}×{v}" for k, v in
                        sorted(counts.items(), key=lambda kv: -kv[1])[:5])
        self.append_log(f"[OSM] 已加载 {vl.featureCount()} 条道路（{top}…）")

    def osm_erase(self):
        self.wb.apply_osm_roads()

    # ---------- 进度 / 报表 ----------

    def refresh_progress(self):
        if not hasattr(self, "lst_cells"):
            return
        self.lst_cells.clear()
        cells = self.wb._grid_cells()
        if cells is None:
            return
        agg = self.wb.cell_stats()
        for cid, _r, _c, _fid in cells:
            n, area, has_base = agg.get(cid, (0, 0.0, False))
            mark = "➤" if cid == self.wb.current_cell else ("✅" if has_base else "⬜")
            self.lst_cells.addItem(
                f"{mark} {cid} — {n} 块 · {area / 666.6667:,.0f} 亩")

    def export_report(self):
        default = os.path.join(
            os.path.dirname(self.wb.path) if self.wb.path else os.path.expanduser("~"),
            "耕地标注报表.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报表", default, "CSV (*.csv)")
        if path:
            self.wb.export_report(path)

    def activate_tool(self, key):
        tool = self.tools.get(key)
        if tool is None:
            self.append_log("[提示] 请先打开标注层")
            for b in self.btn_group.buttons():
                b.setChecked(False)
            return
        self.iface.mapCanvas().setMapTool(tool)

    def _sync_ui(self):
        ready = self.wb.layer is not None
        self.btn_base.setEnabled(ready)
        self.btn_zoom.setEnabled(ready and self.wb.cell_geom is not None)
        self.btn_next.setEnabled(ready)
        self.btn_osm_erase.setEnabled(ready)
        if not ready:
            for b in self.btn_group.buttons():
                b.setEnabled(False)
        else:
            for b in self.btn_group.buttons():
                b.setEnabled(True)
        self.refresh_progress()

    def closeEvent(self, event):
        # 先切回漫游工具，避免画布还挂着已销毁的工具
        try:
            self.iface.actionPan().trigger()
        except Exception:
            pass
        self.wb.close_session()
        super().closeEvent(event)
