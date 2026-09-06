"""磁力套索：两点定一线，自动沿影像上可见的线状地物（田埂/田间路/未入库道路）走。

原理：影像梯度 → 代价面（梯度越强越便宜），A* 在两点之间找最短代价路径。
速度策略：先在粗网格（2^k 最大池化，取 min 保住细线）上定走廊，
再回全分辨率在走廊内精确细化——典型 300~600px 的切分线 0.2~0.5s。
纯 numpy + 标准库，无新依赖；QGIS Python 里只有 numpy 也能跑。
"""

import heapq
import json
import math
import os

import numpy as np

try:  # 读影像窗口首选 gdal（文件型栅格），失败再退 QGIS 数据源接口
    from osgeo import gdal
except ImportError:  # pragma: no cover
    gdal = None

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsRasterLayer,
    QgsRectangle,
    QgsCoordinateTransform,
    QgsProject,
    Qgis,
)


# ---------------------------------------------------------------- #
# 纯 numpy 寻路核心（不依赖 QGIS 也能 import 测试）
# ---------------------------------------------------------------- #

class GridPathFinder:
    """8 邻域 A*。cost 取值 [0,1]，越小的像素越容易被穿过（边缘=低代价）。"""

    BASE = 0.35  # 单位长度底价：路径不会为省一点点梯度而无谓绕远
    _SQRT2 = math.sqrt(2.0)
    _NEIGH = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
              (1, 1, _SQRT2), (1, -1, _SQRT2),
              (-1, 1, _SQRT2), (-1, -1, _SQRT2))

    def __init__(self, cost):
        self.cost = np.ascontiguousarray(cost, dtype=np.float32)
        self.h, self.w = self.cost.shape
        self._pooled = {}  # pool → (GridPathFinder, pool)

    # ---------- A* ----------

    def _astar(self, start, goal, mask=None, weight=1.0, step_scale=1.0):
        """返回 [(col, row), ...]（start→goal），不可达返回 None。"""
        cost = self.cost
        w, h = self.w, self.h
        base = self.BASE
        gx, gy = goal
        dist = {start: 0.0}
        prev = {}
        closed = set()
        heap = [(0.0, 0, start)]
        seq = 0
        while heap:
            f, _, node = heapq.heappop(heap)
            if node in closed:
                continue
            if node == goal:
                path = [node]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                path.reverse()
                return path
            closed.add(node)
            x, y = node
            d = dist[node]
            row_cost = cost[y]
            for dc, dr, s in self._NEIGH:
                nx, ny = x + dc, y + dr
                if nx < 0 or ny < 0 or nx >= w or ny >= h:
                    continue
                if mask is not None and not mask[ny, nx]:
                    continue
                nb = (nx, ny)
                if nb in closed:
                    continue
                ng = d + s * step_scale * (
                    base + 0.5 * (row_cost[x] + cost[ny, nx]))
                old = dist.get(nb)
                if old is None or ng < old:
                    dist[nb] = ng
                    prev[nb] = node
                    seq += 1
                    dx = gx - nx
                    dy = gy - ny
                    heapq.heappush(heap, (
                        ng + weight * step_scale * base * math.hypot(dx, dy),
                        seq, nb))
        return None

    # ---------- 粗网格与走廊 ----------

    def pooled(self, pool):
        """2^k 倍降采样代价面（窗口取 min：细线只要有一格压住就保留）。"""
        if pool not in self._pooled:
            if pool == 1:
                self._pooled[1] = (self, 1)
            else:
                h2, w2 = self.h // pool, self.w // pool
                trimmed = self.cost[:h2 * pool, :w2 * pool]
                low = trimmed.reshape(
                    h2, pool, w2, pool).min(axis=(1, 3))
                self._pooled[pool] = (GridPathFinder(low), pool)
        return self._pooled[pool]

    def corridor(self, pooled_path, pool, radius):
        """把粗网格路径膨胀成全分辨率走廊掩膜。"""
        mask = np.zeros((self.h, self.w), dtype=bool)
        for c, r in pooled_path:
            cx, cy = c * pool + pool // 2, r * pool + pool // 2
            mask[max(0, cy - radius):cy + radius + 1,
                 max(0, cx - radius):cx + radius + 1] = True
        return mask

    # ---------- 对外 ----------

    def find(self, p0, p1, refine=True, max_nodes=22000):
        """p0/p1 = (col,row)。返回全分辨率坐标路径；不可达返回 None。

        refine=False 只走粗网格（预览用，快）；True 时粗网格定走廊后回
        全分辨率细化（提交用，像素级）。
        """
        x0 = min(max(int(round(p0[0])), 0), self.w - 1)
        y0 = min(max(int(round(p0[1])), 0), self.h - 1)
        x1 = min(max(int(round(p1[0])), 0), self.w - 1)
        y1 = min(max(int(round(p1[1])), 0), self.h - 1)
        if (x0, y0) == (x1, y1):
            return [(x0, y0)]
        bw = abs(x1 - x0) + 1
        bh = abs(y1 - y0) + 1
        pool = 1
        while (bw // pool + 1) * (bh // pool + 1) > max_nodes and pool < 32:
            pool *= 2
        if pool == 1:
            return self._astar((x0, y0), (x1, y1), weight=1.15)
        pf, _ = self.pooled(pool)
        gp0 = (min(x0 // pool, pf.w - 1), min(y0 // pool, pf.h - 1))
        gp1 = (min(x1 // pool, pf.w - 1), min(y1 // pool, pf.h - 1))
        ppath = pf._astar(gp0, gp1, weight=1.3, step_scale=pool)
        if ppath is None:
            return self._astar((x0, y0), (x1, y1), weight=1.15)
        if not refine:
            return [(c * pool + pool // 2, r * pool + pool // 2)
                    for c, r in ppath]
        mask = self.corridor(ppath, pool, radius=pool * 2 + 3)
        # 端点必须在走廊里（粗路径两端就是端点所在块）
        fine = self._astar((x0, y0), (x1, y1), mask=mask)
        return fine if fine is not None else [
            (c * pool + pool // 2, r * pool + pool // 2) for c, r in ppath]

    def path_cost(self, pts):
        """一条路径的总代价（测试用：曲线应显著低于直线）。"""
        total = 0.0
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            s = math.hypot(bx - ax, by - ay)
            total += s * (self.BASE + 0.5 * (
                self.cost[ay, ax] + self.cost[by, bx]))
        return total


# ---------------------------------------------------------------- #
# 影像窗口引擎（QGIS 侧）
# ---------------------------------------------------------------- #

_DTYPES = {
    1: np.uint8, 2: np.uint16, 3: np.int16,
    4: np.uint32, 5: np.int32, 6: np.float32, 7: np.float64,
}


def load_evidence(npz_path):
    """读神经网络边界证据：.npz（ev 数组，P(边界)∈[0,1]）+ 同名 .json 元数据。

    由 QGIS 外环境离线生成（SAM 系模型），与引擎窗口同网格同 CRS。
    任何缺失/损坏都静默返回 (None, None)，调用方退回纯梯度。"""
    try:
        meta_path = os.path.splitext(npz_path)[0] + ".json"
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        with np.load(npz_path) as z:
            ev = np.asarray(z["ev"], dtype=np.float32)
        if ev.ndim != 2:
            return None, None
        return ev, meta
    except Exception:
        return None, None


def evidence_path(raster_layer, cell_id):
    """约定路径：<影像所在目录>/边缘证据/<格子ID>.npz。非文件型栅格返回 None。"""
    try:
        uri = raster_layer.source().split("|")[0]
    except Exception:
        return None
    if not uri.startswith("/"):
        uri = os.path.abspath(uri)  # 相对源按进程 CWD 归一（异常源算出的路径不存在=自动跳过）
    return os.path.join(os.path.dirname(uri), "边缘证据", f"{cell_id}.npz")


class LassoEngine:
    """绑定栅格 + 一个窗口（当前格外扩 margin_px），提供点到点自动路径。

    对外坐标一律是栅格层自己的 CRS；调用方负责与画布 CRS 的互转。
    """

    def __init__(self, raster_layer, extent, extent_crs, margin_px=256,
                 evidence=None):
        if not isinstance(raster_layer, QgsRasterLayer) \
                or not raster_layer.isValid():
            raise ValueError("影像图层无效")
        rc = raster_layer.crs()
        rect = QgsRectangle(extent)
        if extent_crs is not None and rc != extent_crs:
            xform = QgsCoordinateTransform(
                extent_crs, rc, QgsProject.instance())
            rect = xform.transformBoundingBox(rect)
        lum, x0, y0, pw, ph = self._read_window(
            raster_layer, rect, margin_px)
        if lum is None or lum.size < 4:
            raise ValueError("读不到影像窗口")
        gy, gx = np.gradient(lum.astype(np.float32))
        g = np.hypot(gx, gy)
        gmax = float(np.percentile(g, 98.5))
        if gmax <= 1e-6:
            gnorm = np.zeros_like(g)
        else:
            gnorm = np.clip(g / gmax, 0.0, 1.0)
        cost = 1.0 - gnorm
        self.used_evidence = False
        if evidence is not None:
            ev = np.asarray(evidence, dtype=np.float32)
            if ev.shape == cost.shape:
                # 神经网络边界证据（P 越大越像边界）与梯度证据取更便宜的一路：
                # 证据只能新增可信线（无亮度反差的边界），不会削弱梯度已认出的线
                cost = np.minimum(cost, 1.0 - np.clip(ev, 0.0, 1.0))
                self.used_evidence = True
        self.finder = GridPathFinder(cost)
        self.lum = lum.astype(np.float32)   # 亮度矩阵（QA 暗斑检查用）
        self.x0, self.y0, self.pw, self.ph = x0, y0, pw, ph
        self.crs = rc
        self.shape = (int(lum.shape[0]), int(lum.shape[1]))

    # ---------- 读窗口 ----------

    @staticmethod
    def _read_window(layer, rect, margin_px):
        """返回 (亮度矩阵, 窗口左上 x, 窗口左上 y, 像宽, 像高)。"""
        dp = layer.dataProvider()
        uri = dp.dataSourceUri().split("|")[0]
        if gdal is not None and uri and os.path.exists(uri):
            ds = gdal.Open(uri, gdal.GA_ReadOnly)
            if ds is not None and ds.RasterCount > 0:
                gt = ds.GetGeoTransform()
                pw, ph = abs(gt[1]), abs(gt[5])
                m = margin_px * pw
                wx0 = rect.xMinimum() - m
                wx1 = rect.xMaximum() + m
                wy0 = rect.yMinimum() - margin_px * ph
                wy1 = rect.yMaximum() + margin_px * ph
                cx0 = int(math.floor((wx0 - gt[0]) / gt[1]))
                cx1 = int(math.ceil((wx1 - gt[0]) / gt[1]))
                cy0 = int(math.floor((gt[3] - wy1) / -gt[5]))
                cy1 = int(math.ceil((gt[3] - wy0) / -gt[5]))
                cx0, cy0 = max(cx0, 0), max(cy0, 0)
                cx1 = min(cx1, ds.RasterXSize)
                cy1 = min(cy1, ds.RasterYSize)
                if cx1 - cx0 >= 2 and cy1 - cy0 >= 2:
                    arr = ds.ReadAsArray(cx0, cy0, cx1 - cx0, cy1 - cy0)
                    if arr is not None:
                        if arr.ndim == 3:
                            arr = arr.astype(np.float32).mean(axis=0)
                        else:
                            arr = arr.astype(np.float32)
                        top_x = gt[0] + cx0 * gt[1]
                        top_y = gt[3] + cy0 * gt[5]
                        return arr, top_x, top_y, pw, ph
        # 退路：QGIS 数据源按窗口读块（WMS/非文件等）
        ext = dp.extent()
        pw = ext.width() / max(dp.xSize(), 1)
        ph = ext.height() / max(dp.ySize(), 1)
        m = margin_px * pw
        win = QgsRectangle(rect.xMinimum() - m, rect.yMinimum() - margin_px * ph,
                           rect.xMaximum() + m, rect.yMaximum() + margin_px * ph)
        win = win.intersect(ext)
        cols = max(2, int(round(win.width() / pw)))
        rows = max(2, int(round(win.height() / ph)))
        bands = []
        for b in range(1, dp.bandCount() + 1):
            blk = dp.block(b, win, cols, rows)
            if blk is None or blk.isEmpty():
                continue
            try:
                npdt = _DTYPES[int(blk.dataType())]
                a = np.frombuffer(bytes(blk.data()), dtype=npdt)
                if a.size == rows * cols:
                    bands.append(a.reshape(rows, cols).astype(np.float32))
            except Exception:
                continue
        if not bands:
            return None, 0, 0, pw, ph
        lum = np.mean(bands, axis=0)
        return lum, win.xMinimum(), win.yMaximum(), pw, ph

    # ---------- 坐标与路径 ----------

    def pixel_size(self):
        return self.pw

    def to_px(self, p):
        return (int((p.x() - self.x0) / self.pw),
                int((self.y0 - p.y()) / self.ph))

    def to_map(self, c, r):
        return QgsPointXY(self.x0 + (c + 0.5) * self.pw,
                          self.y0 - (r + 0.5) * self.ph)

    def path(self, p0, p1, fast=False):
        """p0/p1 为 QgsPointXY（栅格 CRS）。越出窗口返回 None（调用方走直线）。"""
        c0, r0 = self.to_px(p0)
        c1, r1 = self.to_px(p1)
        h, w = self.shape
        if not (0 <= c0 < w and 0 <= r0 < h and 0 <= c1 < w and 0 <= r1 < h):
            return None
        pts = self.finder.find((c0, r0), (c1, r1), refine=not fast)
        if not pts:
            return None
        return [self.to_map(c, r) for c, r in pts]

    # ---------- 有界计算窗口（预览=提交，窗口可画出来） ----------

    TILE = 256    # 窗口对齐到固定瓦片网格：光标在同一瓦片内移动时窗口不闪
    MARGIN = 96   # 端点外扩余量（px），给弯路留空间
    CAP = 1280    # 窗口任一边上限：超过说明段太长，退回粗网格走廊法

    def window_px(self, p0, p1):
        """两端点的瓦片对齐计算窗口 (x0, y0, x1, y1) 像素坐标。

        两端点所在范围外扩 MARGIN 后向外对齐到 TILE 网格——光标在瓦片内
        移动时窗口稳定不变，跨进相邻瓦片时才扩一档，一目了然。超 CAP
        返回 None。
        """
        c0, r0 = self.to_px(p0)
        c1, r1 = self.to_px(p1)
        h, w = self.shape
        if not (0 <= c0 < w and 0 <= r0 < h and 0 <= c1 < w and 0 <= r1 < h):
            return None
        t, m = self.TILE, self.MARGIN
        x0 = max(0, (min(c0, c1) - m) // t * t)
        y0 = max(0, (min(r0, r1) - m) // t * t)
        x1 = min(w, ((max(c0, c1) + m) // t + 1) * t)
        y1 = min(h, ((max(r0, r1) + m) // t + 1) * t)
        if x1 - x0 > self.CAP or y1 - y0 > self.CAP:
            return None
        return (x0, y0, x1, y1)

    def window_bbox(self, win):
        """像素窗口 → 栅格 CRS 地图坐标 (xmin, ymin, xmax, ymax)，画格子框用。"""
        x0, y0, x1, y1 = win
        return (self.x0 + x0 * self.pw, self.y0 - y1 * self.ph,
                self.x0 + x1 * self.pw, self.y0 - y0 * self.ph)

    def path_window(self, p0, p1):
        """只在两端点的瓦片并集窗口内做全分辨率 A*。

        返回 (路径 QgsPointXY 列表, 像素窗口)；端点越窗或窗口超 CAP 时
        返回 (None, None)（调用方可退回走廊法或直线）。预览与提交共用
        本入口、同一 weight，所以"预览看到的线=点击采纳的线"。
        """
        win = self.window_px(p0, p1)
        if win is None:
            return None, None
        x0, y0, x1, y1 = win
        c0, r0 = self.to_px(p0)
        c1, r1 = self.to_px(p1)
        sub = GridPathFinder(self.finder.cost[y0:y1, x0:x1])
        pts = sub._astar((c0 - x0, r0 - y0), (c1 - x0, r1 - y0), weight=1.15)
        if not pts:
            return None, win
        return [self.to_map(c + x0, r + y0) for c, r in pts], win


# ---------------------------------------------------------------- #
# 结果组装（纯函数，供工具与测试共用）
# ---------------------------------------------------------------- #

def _same(a, b):
    return abs(a.x() - b.x()) < 1e-9 and abs(a.y() - b.y()) < 1e-9


def assemble(anchors, segs):
    """锚点 + 段路径 → 一条完整折线（缺段的走直线兜底）。"""
    out = []
    for i in range(len(anchors) - 1):
        seg = segs.get(i)
        if seg:
            for p in seg:
                if not out or not _same(out[-1], p):
                    out.append(p)
        elif not out or not _same(out[-1], anchors[i]):
            out.append(anchors[i])
    if anchors and (not out or not _same(out[-1], anchors[-1])):
        out.append(anchors[-1])
    return out


def simplify_pts(pts, tol):
    """Douglas-Peucker 轻简化去像素抖动（容差≈1 像素，别过度）。"""
    if len(pts) < 3 or tol <= 0:
        return pts
    g = QgsGeometry.fromPolylineXY(pts)
    s = g.simplify(tol)
    if s is None or s.isEmpty():
        return pts
    line = s.asPolyline()
    return line if len(line) >= 2 else pts


def grow_ends(pts, inside, step, max_dist=16000.0, step_cap=64.0):
    """两端沿首末方向逐步外推，直到落出地块（inside(p) 为 False）。

    磁力线的锚点常在地块内部；固定长度延长在斜交边界时会滑进内部出不来，
    逐步外推到刚出界即停（splitGeometry 要求线两端越过地块边界）。
    整格底板可达数公里宽：基础步长走满 step*80（默认 800m）还没出去就
    步长翻倍加速（单步封顶 step_cap），总程封顶 max_dist；最后"多走一步"
    仍用基础 step，避免大步一跳跨进相邻地块。
    """
    if len(pts) < 2 or step <= 0:
        return pts
    out = list(pts)
    for end in ("start", "end"):
        a, b = (out[0], out[1]) if end == "start" else (out[-1], out[-2])
        dx, dy = a.x() - b.x(), a.y() - b.y()
        n = math.hypot(dx, dy) or 1.0
        ux, uy = dx / n, dy / n
        s, walked, p = step, 0.0, a
        while walked < max_dist and inside(p):
            p = QgsPointXY(p.x() + ux * s, p.y() + uy * s)
            walked += s
            if walked >= step * 80:      # 还没出去 → 大地块，加速
                s = min(s * 2, step_cap)
        q = QgsPointXY(p.x() + ux * step, p.y() + uy * step)  # 再多走一步
        if end == "start":
            out.insert(0, q)
        else:
            out.append(q)
    return out
