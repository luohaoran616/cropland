"""标注工作台的地图工具：每个笔触即时生效（缓冲/差集/并集/切分）。

交互约定（与 QGIS 数字化习惯一致）：
  左键 = 加点；右键或 Enter = 结束一笔；Esc = 取消当前笔；Backspace / Ctrl+Z = 退一个点
  1/2 = 切换大路/小路宽度档；[ / ] = 宽度 -1 / +1 米（道路笔刷 / 磁力道路）
  画线时按住 Shift = 正交锁（当前点吸附到与上一点水平/垂直，直路两点即成）
"""

from qgis.PyQt.QtCore import Qt, QTimer, QThread, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import (
    Qgis, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker

from .lasso import assemble, simplify_pts, grow_ends, _same


def _geometry_type_enum(kind):
    """QGIS 4 用 Qgis.GeometryType，旧接口是 QgsWkbTypes.GeometryType。"""
    try:
        return Qgis.GeometryType.Line if kind == "line" else Qgis.GeometryType.Polygon
    except AttributeError:
        gt = QgsWkbTypes.GeometryType
        return gt.LineGeometry if kind == "line" else gt.PolygonGeometry


def make_rubber_band(canvas, kind, color, alpha_fill=60):
    rb = QgsRubberBand(canvas, _geometry_type_enum(kind))
    c = QColor(color)
    if kind == "line":
        rb.setColor(c)
        rb.setWidth(2)
    else:
        fill = QColor(c)
        fill.setAlpha(alpha_fill)
        try:
            rb.setFillColor(fill)
            rb.setStrokeColor(c)
            rb.setWidth(1)
        except AttributeError:  # 旧接口只有 setColor
            rb.setColor(fill)
    return rb


class WorkbenchTool(QgsMapTool):
    """共同底座：点采集、橡皮带预览、快捷键、清理。"""

    def __init__(self, canvas, wb, kind, color, alpha_fill=60):
        super().__init__(canvas)
        self.wb = wb            # annotate.AnnotationController
        self.pts = []           # 当前笔的顶点（地图 CRS）
        self.rb = None
        self.rb_buf = None      # 道路缓冲预览带（道路类工具用）
        self.rb_kind = kind
        self.rb_color = color
        self.rb_alpha = alpha_fill
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ---------- 基础设施 ----------

    def _ensure_rb(self):
        if self.rb is None:
            self.rb = make_rubber_band(
                self.canvas(), self.rb_kind, self.rb_color, self.rb_alpha)
            self.rb.show()
        return self.rb

    def _clear_rb(self):
        if self.rb is not None:
            self.canvas().scene().removeItem(self.rb)
            self.rb = None
        if self.rb_buf is not None:
            try:
                self.canvas().scene().removeItem(self.rb_buf)
            except Exception:
                pass
            self.rb_buf = None

    def _map_point(self, e):
        """优先吸附点，退回鼠标点。"""
        try:
            return e.snapPoint()  # QgsMapMouseEvent 自动吸附
        except Exception:
            pass
        su = self.canvas().snappingUtils()
        try:
            m = su.snapToMap(e.pos())
            if m.isValid():
                return QgsPointXY(m.point())
        except Exception:
            pass
        return self.toMapCoordinates(e.pos())

    def cancel(self):
        self.pts = []
        if self.rb is not None:
            self.rb.reset(_geometry_type_enum(self.rb_kind))
        if self.rb_buf is not None:
            self.rb_buf.reset(_geometry_type_enum("polygon"))
        self.wb.log("[提示] 已取消当前笔")

    # 道路类工具共用的缓冲预览带（红半透明=将扣除的范围）
    def _draw_buffer_preview(self, buf):
        if buf is None:
            if self.rb_buf is not None:
                self.rb_buf.reset(_geometry_type_enum("polygon"))
            return
        if self.rb_buf is None:
            self.rb_buf = make_rubber_band(self.canvas(), "polygon", "red", 45)
            self.rb_buf.show()
        self.rb_buf.reset(_geometry_type_enum("polygon"))
        try:
            for poly in (buf.asMultiPolygon() if buf.isMultipart() else [buf.asPolygon()]):
                if not poly:
                    continue
                for p in poly[0]:
                    self.rb_buf.addPoint(p)
        except Exception:
            pass

    def remove_last(self):
        if self.pts:
            self.pts.pop()
            self._refresh_preview(None)

    def has_stroke(self):
        """当前是否有落点中的笔（Ctrl+Z 撤回判据）。"""
        return bool(self.pts)

    # ---------- 事件 ----------

    def canvasMoveEvent(self, e):
        self._refresh_preview(self._map_point(e))

    def keyPressEvent(self, e):
        key = e.key()
        if key in (Qt.Key.Key_Escape,):
            self.cancel()
        elif key == Qt.Key.Key_Backspace:
            self.remove_last()
        elif key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            self._finish()
        elif (key == Qt.Key.Key_Z
                and e.modifiers() == Qt.KeyboardModifier.ControlModifier):
            # 画布持焦点时的 Ctrl+Z 梯级（与侧栏 eventFilter 兜底同一语义）：
            # 落点中=退一颗点；没有落点=撤销上一笔已落地的操作
            if self.has_stroke():
                self.remove_last()
            else:
                self.wb.undo()
        else:
            text = e.text()
            if text in ("1", "2"):
                self.wb.set_road_preset(int(text) - 1)
            elif text == "[":
                self.wb.nudge_road_width(-1)
            elif text == "]":
                self.wb.nudge_road_width(+1)

    def deactivate(self):
        self.pts = []
        self._clear_rb()
        super().deactivate()

    # ---------- 子类实现 ----------

    def _refresh_preview(self, cur):
        raise NotImplementedError

    def _finish(self):
        raise NotImplementedError


class PolylineTool(WorkbenchTool):
    """折线类工具：道路笔刷（缓冲后差集）与切分刀。"""

    MIN_POINTS = 2

    def __init__(self, canvas, wb, kind, color, mode):
        super().__init__(canvas, wb, kind, color)
        self.mode = mode  # 'road' | 'split'

    @staticmethod
    def _ortho_snap(prev, p):
        """Shift 按住时把当前点吸附到与上一点水平/垂直的方向。"""
        if prev is None or p is None:
            return p
        if not (QApplication.keyboardModifiers()
                & Qt.KeyboardModifier.ShiftModifier):
            return p
        if abs(p.x() - prev.x()) >= abs(p.y() - prev.y()):
            return QgsPointXY(p.x(), prev.y())
        return QgsPointXY(prev.x(), p.y())

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            p = self._ortho_snap(self.pts[-1] if self.pts else None,
                                 self._map_point(e))
            self.pts.append(p)
            self._refresh_preview(None)
        elif e.button() == Qt.MouseButton.RightButton:
            self._finish()

    def _line_geom(self, cur=None):
        pts = list(self.pts) + ([cur] if cur else [])
        if len(pts) < 2:
            return None
        return QgsGeometry.fromPolylineXY(pts)

    def _refresh_preview(self, cur):
        cur = self._ortho_snap(self.pts[-1] if self.pts else None, cur)
        line = self._line_geom(cur)
        rb = self._ensure_rb()
        rb.reset(_geometry_type_enum("line"))
        if line is None:
            return
        if self.mode == "road":
            buf = self.wb.buffer_preview(line)
            self._draw_buffer_preview(buf)
        for p in (self.pts + ([cur] if cur else [])):
            rb.addPoint(p)

    def _finish(self):
        line = self._line_geom()
        self.pts = []
        if self.rb is not None:
            self.rb.reset(_geometry_type_enum("line"))
        if getattr(self, "rb_buf", None) is not None:
            self.rb_buf.reset(_geometry_type_enum("polygon"))
        if line is None:
            self.wb.log("[提示] 点数不足（至少 2 个点），已忽略")
            return
        line_layer = self.wb.to_layer(line)
        if self.mode == "road":
            self.wb.apply_road(line_layer)
        else:
            self.wb.apply_split(line_layer)


class PathThread(QThread):
    """后台算一段磁力路径；结果用普通 (x,y) 元组跨线程，避免碰 C++ 对象。

    统一走 path_window（有界瓦片对齐窗口内全分辨率 A*）——预览与提交
    共用同一入口同一参数，所以"预览看到的线=点击采纳的线"。段太长超窗
    时自动退回走廊法（此时无窗口框）。
    """

    done = pyqtSignal(dict)

    def __init__(self, engine, xf_fwd, xf_back, p0, p1, gen, idx):
        super().__init__()
        self.engine = engine
        self.xf_fwd, self.xf_back = xf_fwd, xf_back
        self.p0, self.p1, self.gen, self.idx = p0, p1, gen, idx

    def _fwd(self, p):
        if self.xf_fwd is None:
            return p
        return QgsPointXY(self.xf_fwd.transform(p))

    def _back(self, p):
        if self.xf_back is None:
            return p
        return QgsPointXY(self.xf_back.transform(p))

    def run(self):
        pts, wbbox = [], None
        try:
            a, b = self._fwd(self.p0), self._fwd(self.p1)
            path, win = self.engine.path_window(a, b)
            if path is None and win is None:
                path = self.engine.path(a, b)  # 超窗长段：走廊法兜底
            if win is not None:
                wbbox = self.engine.window_bbox(win)
            pts = [(p.x(), p.y()) for p in (path or [])]
        except Exception:
            pts, wbbox = [], None
        if wbbox is not None:
            x0, y0, x1, y1 = wbbox
            c0, c1 = self._back(QgsPointXY(x0, y0)), self._back(QgsPointXY(x1, y1))
            wbbox = (min(c0.x(), c1.x()), min(c0.y(), c1.y()),
                     max(c0.x(), c1.x()), max(c0.y(), c1.y()))
        self.done.emit({"gen": self.gen, "idx": self.idx, "pts": pts,
                        "win": wbbox,
                        "target": (self.p1.x(), self.p1.y())})


class MagneticSplitTool(WorkbenchTool):
    """磁力切分 / 磁力道路 / 磁力补画：点锚点，中间自动沿影像上的线状地物走。

    长线中途多点几颗锚点防跑偏；路径只在两端点所在的瓦片对齐窗口内
    计算（青色虚线框所示），预览与提交同一结果——看到的虚线就是点击
    采纳的线。没有可用影像时退化为普通折线（照样能用）。

    mode='split'：右键/Enter 收笔 → 与 ✂ 切分相同的落层逻辑（端点外推
    到出地块后 splitGeometry）。mode='road'：收笔 → 与 🛣 道路笔刷相同
    的缓冲差集（按当前档位宽度扣除，红带预览=将扣的范围，端点不外推），
    用于 OSM 路网没覆盖、需要手选扣除的道路。mode='poly'：磁力补画——
    沿目标边界点一圈顶点，每段吸边，收笔时补算闭合段成环，整块落地为
    补画（1）或挖除（2）。
    """

    def __init__(self, canvas, wb, mode="split"):
        if mode == "poly":
            super().__init__(canvas, wb, "polygon", "green", 45)
            self.poly_mode = "add"   # 1=补画（绿） 2=挖除（红）
        else:
            super().__init__(canvas, wb, "line", "magenta")
        self.mode = mode          # 'split' | 'road' | 'poly'
        self.anchors = []          # 锚点（地图 CRS）
        self.segs = {}             # 段号 → 路径顶点（地图 CRS，含段起点）
        self.preview = []          # 末锚点→光标的实时预览（虚线）
        self._cursor = None
        self._gen = 0              # 笔代际：取消/收笔/换工具后丢弃过期结果
        self._threads = set()
        self._pv_busy = False
        self._pending_finish = False
        self._straight_mode = False
        self._engine = None
        self._xf = None            # 地图 CRS → 栅格 CRS
        self._xf_back = None       # 栅格 CRS → 地图 CRS（画窗口框用）
        self._marks = []
        self.rb_pv = None          # 虚线预览带（建议路径）
        self._win_band = None      # 青色虚线窗口框（本次计算范围）
        self._pv_target = None     # 预览终点；点击落在其上时直接采纳预览
        self._timer = QTimer(canvas)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._preview_now)

    # ---------- 引擎 ----------

    def activate(self):
        super().activate()
        if self.mode == "poly":
            self.wb.log("[磁力描] 沿目标边界左键点一圈顶点（每段自动吸边，"
                        "虚线=建议线）→ 右键或 Enter 闭合落地 · 1=补画（绿）"
                        "2=挖除（红） · Backspace / Ctrl+Z 退点 · Esc 取消")
        elif self.mode == "road":
            self.wb.log("[磁力道] 左键点起点 → 移动（虚线=建议线，红带=按当前"
                        "档宽将扣除的范围）→ 点终点，长路中途多点锚点 → 右键"
                        "或 Enter 扣除 · 1/2 换大/小路档 · [ ] 调宽度 · "
                        "Backspace / Ctrl+Z 退点 · Esc 取消")
        else:
            self.wb.log("[磁力] 左键点起点 → 移动鼠标（虚线=建议线，青色框=计算"
                        "范围）→ 点终点，长线可中途多点锚点 → 右键或 Enter 收笔"
                        "切分 · Backspace / Ctrl+Z 退点 · Esc 取消")

    def _ensure_engine(self):
        if self._straight_mode:
            return False
        if self._engine is None:
            eng, xf = self.wb.lasso_context()
            if eng is None:
                self._straight_mode = True
                self.wb.log("[磁力] 没有可用影像，退化直线模式"
                            "（选中影像图层后重开本工具可恢复）")
                return False
            self._engine, self._xf = eng, xf
            if xf is not None:
                from qgis.core import (QgsCoordinateTransform,
                                       QgsProject)
                self._xf_back = QgsCoordinateTransform(
                    xf.destinationCrs(), xf.sourceCrs(),
                    QgsProject.instance())
        return True

    def _launch(self, p0, p1, idx):
        t = PathThread(self._engine, self._xf, self._xf_back,
                       p0, p1, self._gen, idx)
        t.done.connect(self._on_done)
        self._threads.add(t)
        t.finished.connect(lambda t=t: self._threads.discard(t))
        t.finished.connect(t.deleteLater)
        t.start()
        return t

    # ---------- 事件 ----------

    def keyPressEvent(self, e):
        if self.mode == "poly" and e.text() in ("1", "2"):
            self._set_poly_mode("add" if e.text() == "1" else "erase")
            e.accept()
            return
        super().keyPressEvent(e)
        # 道路模式换档/调宽后立即刷新红色缓冲带（不用等下一次移动鼠标）
        if self.mode == "road" and e.text() in ("1", "2", "[", "]"):
            self._refresh_preview(None)

    def _set_poly_mode(self, m):
        if m == self.poly_mode:
            return
        self.poly_mode = m
        self.wb.log(f"[磁力描] 落地模式：{'补画' if m == 'add' else '挖除'}")
        if self.rb is not None:
            try:
                c = QColor("green" if m == "add" else "red")
                f = QColor(c)
                f.setAlpha(45)
                self.rb.setStrokeColor(c)
                self.rb.setFillColor(f)
            except Exception:
                pass
        self._refresh_preview(None)

    def canvasMoveEvent(self, e):
        self._cursor = self._map_point(e)
        if self.anchors:
            self._timer.start()
        self._refresh_preview(None)

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_left_click(self._map_point(e))
        elif e.button() == Qt.MouseButton.RightButton:
            self._finish()

    def _on_left_click(self, p):
        self._ensure_engine()
        # 预览还新鲜（终点≈本次点击处）→ 直接采纳建议线，零等待。
        # 必须在清空 preview 之前取样，否则采纳到的恒是空表（→直线兜底）。
        fresh = None
        if (self._engine is not None and self._pv_target is not None
                and self._pv_target.distance(p)
                <= max(self._engine.pixel_size(), 1.0)):
            fresh = list(self.preview)
        self.anchors.append(p)
        self._add_mark(p)
        self.preview = []
        if len(self.anchors) >= 2 and self._engine is not None:
            if fresh:
                self.segs[len(self.anchors) - 2] = fresh
            else:
                self._launch(self.anchors[-2], self.anchors[-1],
                             len(self.anchors) - 2)
        self._pv_target = None
        self._refresh_preview(None)

    def _preview_now(self):
        if not self.anchors or self._cursor is None:
            return
        if self._pv_busy or self._engine is None:
            return
        self._pv_busy = True
        self._launch(self.anchors[-1], self._cursor, idx=-1)

    def _on_done(self, res):
        if res["idx"] < 0:
            self._pv_busy = False
        if res["gen"] != self._gen:
            return
        if res["idx"] < 0:
            self.preview = [QgsPointXY(x, y) for x, y in res["pts"]]
            if self.preview:
                self._pv_target = QgsPointXY(*res["target"])
            self._show_window(res["win"])
        elif 0 <= res["idx"] <= len(self.anchors) - 1:
            # idx==len-1 是补画模式的闭合段（末顶点→首顶点）
            self.segs[res["idx"]] = [QgsPointXY(x, y) for x, y in res["pts"]]
        self._refresh_preview(None)
        if self._pending_finish and self._commit_busy() == 0:
            self._pending_finish = False
            self._finish()   # 重走收笔判定（poly 此时才补算/采用闭合段）

    def _commit_busy(self):
        return sum(1 for t in list(self._threads)
                   if getattr(t, "idx", -1) >= 0 and not t.isFinished())

    # ---------- 预览 ----------

    def _refresh_preview(self, cur):
        if self.mode == "poly":
            # 面环预览：已定段连成环 + 到光标的建议线（虚线）
            rb = self._ensure_rb()
            rb.reset(_geometry_type_enum("polygon"))
            ring = assemble(self.anchors, self.segs) if self.anchors else []
            pv = list(self.preview) + ([cur] if cur else [])
            for p in ring + pv + ([ring[0]] if ring else []):
                rb.addPoint(p)
            band = self._ensure_pv_band()
            band.reset(_geometry_type_enum("line"))
            for p in pv:
                band.addPoint(p)
            return
        rb = self._ensure_rb()
        rb.reset(_geometry_type_enum("line"))
        for p in assemble(self.anchors, self.segs):
            rb.addPoint(p)
        pv = self._ensure_pv_band()
        pv.reset(_geometry_type_enum("line"))
        for p in list(self.preview) + ([cur] if cur else []):
            pv.addPoint(p)
        if self.mode == "road":
            # 红带预览：已定段+建议线整条按当前档宽缓冲=将扣除的范围
            all_pts = assemble(self.anchors, self.segs) \
                + list(self.preview) + ([cur] if cur else [])
            buf = None
            if len(all_pts) >= 2:
                buf = self.wb.buffer_preview(
                    QgsGeometry.fromPolylineXY(all_pts))
            self._draw_buffer_preview(buf)

    def _ensure_pv_band(self):
        """虚线洋红带：还没采纳的"建议路径"（与已定段区分开）。"""
        if self.rb_pv is None:
            self.rb_pv = QgsRubberBand(self.canvas(),
                                       _geometry_type_enum("line"))
            self.rb_pv.setColor(QColor("magenta"))
            self.rb_pv.setWidth(2)
            try:
                self.rb_pv.setLineStyle(Qt.PenStyle.DashLine)
            except Exception:
                pass
            self.rb_pv.show()
        return self.rb_pv

    def _show_window(self, wbbox):
        """画/更新青色虚线框：当前段的计算范围（瓦片对齐窗口）。"""
        if wbbox is None:
            self._hide_window()
            return
        x0, y0, x1, y1 = wbbox
        corners = [QgsPointXY(x0, y0), QgsPointXY(x1, y0), QgsPointXY(x1, y1),
                   QgsPointXY(x0, y1), QgsPointXY(x0, y0)]
        band = self._win_band
        if band is None:
            band = QgsRubberBand(self.canvas(), _geometry_type_enum("line"))
            band.setColor(QColor(0, 200, 255, 180))
            band.setWidth(1)
            try:
                band.setLineStyle(Qt.PenStyle.DashLine)
            except Exception:
                pass
            band.show()
            self._win_band = band
        band.reset(_geometry_type_enum("line"))
        for p in corners:
            band.addPoint(p)

    def _hide_window(self):
        if self._win_band is not None:
            self._win_band.reset(_geometry_type_enum("line"))

    def _clear_aux_bands(self):
        for band in (self._win_band, self.rb_pv, self.rb_buf):
            if band is not None:
                try:
                    self.canvas().scene().removeItem(band)
                except Exception:
                    pass
        self._win_band = None
        self.rb_pv = None
        self.rb_buf = None

    def _add_mark(self, p):
        m = QgsVertexMarker(self.canvas())
        m.setCenter(p)
        m.setColor(QColor("yellow"))
        m.setIconSize(8)
        m.setPenWidth(3)
        try:
            m.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        except AttributeError:
            pass
        self._marks.append(m)

    def _clear_marks(self):
        for m in self._marks:
            try:
                self.canvas().scene().removeItem(m)
            except Exception:
                pass
        self._marks = []

    def has_stroke(self):
        return bool(self.anchors)

    def remove_last(self):
        if self.anchors:
            self._gen += 1          # 作废在途段（含闭合段），防迟到结果落进旧槽
            self._pending_finish = False
            self.anchors.pop()
            self.segs.pop(len(self.anchors), None)
            self.preview = []
            self._pv_target = None
            self._hide_window()   # 撤掉刚去掉那段的计算范围框
            self._clear_marks()
            for p in self.anchors:
                self._add_mark(p)
            self._refresh_preview(None)

    def cancel(self):
        self._gen += 1
        self._reset_stroke()
        super().cancel()

    def _reset_stroke(self):
        self.anchors = []
        self.segs = {}
        self.preview = []
        self._pv_target = None
        self._pending_finish = False
        self._hide_window()
        self._clear_marks()
        if self.rb is not None:
            self.rb.reset(_geometry_type_enum(
                "polygon" if self.mode == "poly" else "line"))
        if self.rb_pv is not None:
            self.rb_pv.reset(_geometry_type_enum("line"))
        if self.rb_buf is not None:
            self.rb_buf.reset(_geometry_type_enum("polygon"))

    def deactivate(self):
        self._gen += 1
        self._reset_stroke()
        self._clear_aux_bands()
        for t in list(self._threads):
            t.wait(50)
        super().deactivate()

    # ---------- 收笔 ----------

    def _finish(self):
        if self.mode == "poly":
            if len(self.anchors) < 3:
                self.wb.log("[提示] 至少点 3 个顶点圈出区域再收笔")
                return
            if self._commit_busy():
                self._pending_finish = True
                self.wb.log("[磁力] 最后一段还在算，稍候…")
                return
            n = len(self.anchors)
            if (self._engine is not None
                    and self.segs.get(n - 1) is None):
                # 闭合段（末顶点→首顶点）还没算：后台补算，回来再落地
                self._pending_finish = True
                self.wb.log("[磁力] 正在算闭合段，稍候…")
                self._launch(self.anchors[-1], self.anchors[0], n - 1)
                return
            self._do_finish()
            return
        if len(self.anchors) < 2:
            self.wb.log("[提示] 至少点两个锚点（起点、终点）再收笔")
            return
        if self._commit_busy():
            self._pending_finish = True
            self.wb.log("[磁力] 最后一段还在算，稍候…")
            return
        self._do_finish()

    def _do_finish(self):
        self._gen += 1
        if self.mode == "poly":
            n = len(self.anchors)
            ring = assemble(self.anchors, self.segs)
            for p in (self.segs.get(n - 1) or []):   # 闭合段：末顶点→首顶点
                if ring and not _same(ring[-1], p):
                    ring.append(p)
            if len(ring) >= 3:
                tol = (1.2 * self._engine.pixel_size()
                       if self._engine is not None else 0.0)
                ring = simplify_pts(ring, tol)
                if not _same(ring[0], ring[-1]):
                    ring = list(ring) + [ring[0]]
                geom = QgsGeometry.fromPolygonXY([ring])
                add = self.poly_mode == "add"
                self._reset_stroke()
                g = self.wb.to_layer(geom)
                if add:
                    self.wb.apply_add(g)
                else:
                    self.wb.apply_difference(g, "erase")
            else:
                self._reset_stroke()
                self.wb.log("[提示] 顶点不足（至少 3 个），已忽略")
            return
        pts = assemble(self.anchors, self.segs)
        tol = 1.2 * self._engine.pixel_size() if self._engine is not None else 0.0
        if len(pts) < 2:
            self._reset_stroke()
            self.wb.log("[提示] 路径无效，已忽略")
            return
        pts = simplify_pts(pts, tol)
        if self.mode == "road":
            # 端点不外推：锚点钉在哪就扣到哪（Flat 端帽），所见即所得
            self._reset_stroke()
            self.wb.apply_road(self.wb.to_layer(
                QgsGeometry.fromPolylineXY(pts)))
            return
        pts = self._grow_out(pts)
        self._reset_stroke()
        line = QgsGeometry.fromPolylineXY(pts)
        self.wb.apply_split(self.wb.to_layer(line))

    def _grow_out(self, pts):
        """两端外推到刚出地块（splitGeometry 要求线越过地块边界）。"""
        try:
            geoms = [f.geometry() for f in
                     self.wb.layer.getFeatures(self.wb._cell_filter())]
        except Exception:
            geoms = []

        def inside(p):
            gp = QgsGeometry.fromPointXY(p)
            return any(g.intersects(gp) for g in geoms)

        step = self._engine.pixel_size() * 2 if self._engine is not None else 10.0
        return grow_ends(pts, inside, max(step, 10.0))


class RectEraseTool(WorkbenchTool):
    """框选挖除：左键按下拖动画矩形，松开即差集。"""

    def __init__(self, canvas, wb):
        super().__init__(canvas, wb, "polygon", "red", 45)
        self.anchor = None

    def canvasPressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.anchor = self._map_point(e)

    def canvasMoveEvent(self, e):
        if self.anchor is None:
            return
        cur = self._map_point(e)
        rb = self._ensure_rb()
        rb.reset(_geometry_type_enum("polygon"))
        for p in (self.anchor,
                  QgsPointXY(cur.x(), self.anchor.y()),
                  cur,
                  QgsPointXY(self.anchor.x(), cur.y()),
                  self.anchor):
            rb.addPoint(p)

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.anchor is None:
            return
        cur = self._map_point(e)
        x0, x1 = sorted((self.anchor.x(), cur.x()))
        y0, y1 = sorted((self.anchor.y(), cur.y()))
        self.anchor = None
        if self.rb is not None:
            self.rb.reset(_geometry_type_enum("polygon"))
        if x1 - x0 < 1e-6 or y1 - y0 < 1e-6:
            return
        rect = QgsGeometry.fromPolygonXY([[
            QgsPointXY(x0, y0), QgsPointXY(x1, y0),
            QgsPointXY(x1, y1), QgsPointXY(x0, y1), QgsPointXY(x0, y0),
        ]])
        self.wb.apply_difference(self.wb.to_layer(rect), "erase")

    def _refresh_preview(self, cur):
        pass  # 预览在 canvasMoveEvent 里按锚点画

    def _finish(self):
        pass

    def cancel(self):
        self.anchor = None
        super().cancel()


class PolygonTool(WorkbenchTool):
    """多边形类工具：多边形挖除（差集）与补画（裁回格子后新增）。"""

    MIN_POINTS = 3

    def __init__(self, canvas, wb, mode):
        color = "green" if mode == "add" else "red"
        super().__init__(canvas, wb, "polygon", color, 45)
        self.mode = mode  # 'erase' | 'add'

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.pts.append(self._map_point(e))
            self._refresh_preview(None)
        elif e.button() == Qt.MouseButton.RightButton:
            self._finish()

    def _poly_geom(self, cur=None):
        pts = list(self.pts) + ([cur] if cur else [])
        if len(pts) < self.MIN_POINTS:
            return None
        return QgsGeometry.fromPolygonXY([pts + [pts[0]]])

    def _refresh_preview(self, cur):
        geom = self._poly_geom(cur)
        rb = self._ensure_rb()
        rb.reset(_geometry_type_enum("polygon"))
        if geom is None:
            return
        for p in geom.asPolygon()[0]:
            rb.addPoint(p)

    def _finish(self):
        geom = self._poly_geom()
        self.pts = []
        if self.rb is not None:
            self.rb.reset(_geometry_type_enum("polygon"))
        if geom is None:
            self.wb.log("[提示] 点数不足（至少 3 个点），已忽略")
            return
        geom_layer = self.wb.to_layer(geom)
        if self.mode == "add":
            self.wb.apply_add(geom_layer)
        else:
            self.wb.apply_difference(geom_layer, "erase")


class ClickSegTool(WorkbenchTool):
    """SAM 点选分割：左键正点 / 右键负点 → 掩码实时预览 → Enter 落地。

    1=补画（绿）2=挖除（红）；Backspace/Ctrl+Z 退点，Esc 取消。
    落地走 apply_add / apply_difference，与手画笔刷同一条台账路径。"""

    MODES = {"add": ("补画", "green"), "erase": ("挖除", "red")}

    def __init__(self, canvas, wb):
        super().__init__(canvas, wb, "polygon", "green", 70)
        self.mode = "add"
        self.points = []       # [(QgsPointXY 画布CRS, 1正|0负)]
        self.mask_geom = None  # 预览几何（画布CRS）
        self._ctx = None       # (npy, meta, xf画布→影像)
        self._svc = None
        self._gen = 0          # 笔迹代数：任何点位变动 +1，在途结果对不上即作废
        self._marks = []

    # ---------- 状态 ----------

    def has_stroke(self):
        return bool(self.points) or self.mask_geom is not None

    def _set_mode(self, m):
        if m == self.mode:
            return
        self.mode = m
        name, color = self.MODES[m]
        self.wb.log(f"[点选] 落地模式：{name}")
        if self.rb is not None:
            try:
                c = QColor(color)
                f = QColor(c)
                f.setAlpha(70)
                self.rb.setFillColor(f)
                self.rb.setStrokeColor(c)
            except Exception:
                pass

    def _reset(self):
        self._gen += 1
        self.points = []
        self.mask_geom = None
        if self.rb is not None:
            self.rb.reset(_geometry_type_enum("polygon"))
        self._clear_marks()

    def cancel(self):
        self._reset()
        self.wb.log("[提示] 已取消当前笔")

    def remove_last(self):
        if self.points:
            self._gen += 1
            self.points.pop()
            self._clear_marks()
            for p, lab in self.points:
                self._add_mark(p, lab)
            if self.points:
                self._predict()
            else:
                self.mask_geom = None
                if self.rb is not None:
                    self.rb.reset(_geometry_type_enum("polygon"))
            return
        if self.mask_geom is not None:
            self._reset()

    def deactivate(self):
        self._reset()
        super().deactivate()   # 服务进程留在控制器上，切回来不用重新加载模型

    # ---------- 标记点 ----------

    def _add_mark(self, p, label):
        m = QgsVertexMarker(self.canvas())
        m.setIconType(QgsVertexMarker.ICON_CIRCLE if label
                      else QgsVertexMarker.ICON_X)
        m.setColor(QColor("cyan") if label else QColor("red"))
        m.setIconSize(7)
        m.setPenWidth(2)
        m.setCenter(p)
        self._marks.append(m)

    def _clear_marks(self):
        for m in self._marks:
            try:
                self.canvas().scene().removeItem(m)
            except Exception:
                pass
        self._marks = []

    # ---------- 事件 ----------

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.add_point(self._map_point(e), 1)
        elif e.button() == Qt.MouseButton.RightButton:
            # 右键=负点（排除误粘区域）；Enter 才落地
            if not self.points:
                self.wb.log("[提示] 先用左键点一个目标内部的正点，再右键加负点")
                return
            self.add_point(self._map_point(e), 0)

    def keyPressEvent(self, e):
        t = e.text()
        if t == "1":
            self._set_mode("add")
            e.accept()
            return
        if t == "2":
            self._set_mode("erase")
            e.accept()
            return
        super().keyPressEvent(e)

    def add_point(self, p, label):
        self._gen += 1
        self.points.append((p, label))
        self._add_mark(p, label)
        self._predict()

    # ---------- 预测 ----------

    def _predict(self):
        try:
            self._predict_inner()
        except Exception:
            import traceback
            tail = "\n".join(traceback.format_exc().splitlines()[-4:])
            self.wb.log(f"[点选] 内部错误（详见 Python 控制台）：\n{tail}")

    def _predict_inner(self):
        ctx = self.wb.clickseg_context()
        if ctx is None or ctx[0] is None:
            return
        self._ctx = ctx
        svc = self.wb.sam_click()
        if svc is None:
            return
        self._svc = svc
        if svc.busy():
            # 在途：结果回来发现点位变了会自动补算
            if not getattr(self, "_busy_noted", False):
                self.wb.log("[点选] SAM 还在算上一发，稍候自动补算最新点位")
                self._busy_noted = True
            return

        def after_embed(r):
            if not r.get("ok"):
                self.wb.log(f"[点选] 影像编码失败：{r.get('error')}")
                return
            self._send_predict(svc, ctx)

        svc.embed(ctx[0], after_embed, key=self.wb._clickseg_key)

    def _send_predict(self, svc, ctx):
        """只发 predict（embed 已就位）。缓存命中时 embed 是同步回调，
        回调里不能再回头走 embed，否则 embed→cb→embed 无限递归。"""
        self._busy_noted = False
        _npy, meta, xf = ctx
        pts_px, labels = [], []
        for p, lab in self.points:
            q = xf.transform(p) if xf is not None else QgsPointXY(p)
            pts_px.append([(q.x() - meta["x0"]) / meta["pw"],
                           (meta["y0"] - q.y()) / meta["ph"]])
            labels.append(lab)
        gen = self._gen  # 发出时的笔迹代数；回来时对不上=期间退点/取消/换笔

        def done(r):
            if not r.get("ok"):
                self.wb.log(f"[点选] 分割失败：{r.get('error')}")
                return
            if gen != self._gen:
                if self.points:  # 算的时候又点了：按最新点位补算
                    self._send_predict(svc, self._ctx)
                return           # 已退光/取消：结果作废，不落幽灵预览
            self._on_mask(r, meta, xf)

        svc.predict(pts_px, labels, len(self.points) <= 1, done)

    def _on_mask(self, r, meta, xf):
        from . import clickseg as _cs
        geoms = _cs.mask_to_geometry(
            r["mask"], r["h"], r["w"],
            meta["x0"], meta["y0"], meta["pw"], meta["ph"])
        pos = [p for p, lab in self.points if lab == 1] \
            or [p for p, _ in self.points]
        pos_r = [xf.transform(p) if xf is not None else p for p in pos]
        g = _cs.pick_blob(geoms, pos_r, meta["pw"])
        if g is None:
            self.mask_geom = None
            if self.rb is not None:
                self.rb.reset(_geometry_type_enum("polygon"))
            return
        if xf is not None:  # 影像CRS → 画布CRS
            inv = QgsCoordinateTransform(
                xf.destinationCrs(), xf.sourceCrs(), QgsProject.instance())
            g.transform(inv)
        self.mask_geom = g
        self._draw_preview(g)

    def _draw_preview(self, g):
        rb = self._ensure_rb()
        rb.reset(_geometry_type_enum("polygon"))
        try:
            for poly in (g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]):
                if not poly:
                    continue
                for p in poly[0]:
                    rb.addPoint(p)
        except Exception:
            pass

    # ---------- 落地 ----------

    def _finish(self):
        if self.mask_geom is None:
            self.wb.log("[提示] 还没有掩码：先在目标内部点一个正点")
            return
        g = self.wb.to_layer(QgsGeometry(self.mask_geom))
        self._reset()
        if self.mode == "add":
            self.wb.apply_add(g)
        else:
            self.wb.apply_difference(g, "erase")
