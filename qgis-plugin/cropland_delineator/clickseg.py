"""SAM 点选分割的 QGIS 侧支撑：环境发现、影像窗口、掩码→多边形、常驻服务。

自研实现（决策 0013）：不依赖 AITracer 代码/服务；复用项目 nn/ 里已有的
SAM ViT-B + GPU 推理环境（决策 0011 建），worker 见 nn/sam_click.py。
"""

import base64
import json
import math
import os
import subprocess

import numpy as np

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal
from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
)

try:
    from osgeo import gdal as _gdal
    from osgeo import ogr as _ogr
except ImportError:  # pragma: no cover - QGIS 发行版都带 gdal
    _gdal = None
    _ogr = None

from .lasso import _DTYPES  # QGIS 块读取的数据类型映射（WMS 兜底用）


# ---------- 环境发现 ----------

def _venv_python(nn_dir):
    """venv 的 Python 路径：Linux .venv/bin/python，Windows .venv\\Scripts\\python.exe。"""
    for p in (os.path.join(nn_dir, ".venv", "bin", "python"),
              os.path.join(nn_dir, ".venv", "Scripts", "python.exe")):
        if os.path.isfile(p):
            return p
    return None


def find_nn_dir():
    """找到 nn/ 推理环境（.venv + sam_click.py + 权重）；找不到返回 None。

    查找顺序：环境变量 CROPLAND_NN_DIR → QSettings（朋友在 Python 控制台
    一行代码指定，免环境变量免重启）→ 插件目录旁的开发布局 alpha/nn。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    env = os.environ.get("CROPLAND_NN_DIR")
    if env:
        cands.append(env)
    try:
        from qgis.PyQt.QtCore import QSettings
        qs_dir = QSettings().value(
            "cropland_delineator/nn_dir", "", type=str)
        if qs_dir:
            cands.append(qs_dir)
    except Exception:
        pass
    cands += [
        os.path.join(here, "..", "..", "nn"),   # alpha/qgis-plugin/cropland_delineator → alpha/nn
        os.path.join(here, "..", "nn"),
        os.path.join(here, "nn"),
    ]
    for c in cands:
        if not c or not os.path.isdir(c):
            continue
        py = _venv_python(c)
        worker = os.path.join(c, "sam_click.py")
        if not (py and os.path.isfile(worker)):
            continue
        import glob
        if glob.glob(os.path.join(c, "ckpt", "sam_vit_b*.pth")):
            # realpath 解符号链接：QGIS 从 profile 的软链插件目录进来时，
            # abspath 只做词法规范化会把 ../.. 折回 profile 下的不存在路径
            # （isdir/glob 走内核解析能通过，返回的字符串却是错的）
            return os.path.realpath(c)
    return None


def service_argv(nn_dir):
    """(python, [worker, ckpt, ...])。"""
    import glob
    py = _venv_python(nn_dir)
    if py is None:
        raise RuntimeError(f"{nn_dir}/.venv 里找不到 python（bin/python 或 Scripts/python.exe）")
    hits = sorted(glob.glob(os.path.join(nn_dir, "ckpt", "sam_vit_b*.pth")))
    if not hits:
        raise RuntimeError(f"{nn_dir}/ckpt 里没有 sam_vit_b*.pth 权重")
    return (py, [os.path.join(nn_dir, "sam_click.py"), hits[0]])


# ---------- 影像窗口：格 → uint8 RGB npy ----------

def _to_rgb_u8(arr):
    """任意波段数组 → HxWx3 uint8（2%/98% 拉伸，SAM 吃 RGB）。"""
    if arr.ndim == 3:
        if arr.shape[0] >= 3:
            arr = np.transpose(arr[:3], (1, 2, 0))
        else:
            arr = np.repeat(arr[0:1].transpose(1, 2, 0), 3, axis=2)
    else:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    a = arr.astype(np.float32)
    lo, hi = np.percentile(a, (2.0, 98.0))
    if hi - lo < 1e-6:
        hi = lo + 1.0
    return np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)


def build_window_npy(raster_layer, rect, rect_crs, out_npy, margin_px=96):
    """把 rect（外扩 margin_px 像元）读成 uint8 HxWx3 存 out_npy。

    返回窗口元数据 {x0, y0, pw, ph, h, w}（像元坐标 → 影像 CRS 的仿射）。
    窗口数学与 lasso.LassoEngine._read_window 完全一致（像素级对齐）。
    """
    rc = raster_layer.crs()
    if rect_crs is not None and rc != rect_crs:
        rect = QgsCoordinateTransform(
            rect_crs, rc, QgsProject.instance()
        ).transformBoundingBox(QgsRectangle(rect))
    dp = raster_layer.dataProvider()
    arr = None
    x0 = y0 = pw = ph = None
    uri = dp.dataSourceUri().split("|")[0]
    if _gdal is not None and uri and os.path.exists(uri):
        ds = _gdal.Open(uri, _gdal.GA_ReadOnly)
        if ds is not None and ds.RasterCount > 0:
            gt = ds.GetGeoTransform()
            pw, ph = abs(gt[1]), abs(gt[5])
            m = margin_px * pw
            wx0, wx1 = rect.xMinimum() - m, rect.xMaximum() + m
            wy0 = rect.yMinimum() - margin_px * ph
            wy1 = rect.yMaximum() + margin_px * ph
            cx0 = max(int(math.floor((wx0 - gt[0]) / gt[1])), 0)
            cx1 = min(int(math.ceil((wx1 - gt[0]) / gt[1])), ds.RasterXSize)
            cy0 = max(int(math.floor((gt[3] - wy1) / -gt[5])), 0)
            cy1 = min(int(math.ceil((gt[3] - wy0) / -gt[5])), ds.RasterYSize)
            if cx1 - cx0 >= 2 and cy1 - cy0 >= 2:
                a = ds.ReadAsArray(cx0, cy0, cx1 - cx0, cy1 - cy0)
                if a is not None:
                    arr, x0, y0 = a, gt[0] + cx0 * gt[1], gt[3] + cy0 * gt[5]
    if arr is None:
        # 兜底：QGIS 数据源按窗口读块（WMS / 非文件）
        ext = dp.extent()
        pw = ext.width() / max(dp.xSize(), 1)
        ph = ext.height() / max(dp.ySize(), 1)
        win = QgsRectangle(
            rect.xMinimum() - margin_px * pw,
            rect.yMinimum() - margin_px * ph,
            rect.xMaximum() + margin_px * pw,
            rect.yMaximum() + margin_px * ph,
        ).intersect(ext)
        cols = max(2, int(round(win.width() / pw)))
        rows = max(2, int(round(win.height() / ph)))
        bands = []
        for b in range(1, dp.bandCount() + 1):
            blk = dp.block(b, win, cols, rows)
            if blk is None or blk.isEmpty():
                continue
            try:
                a = np.frombuffer(bytes(blk.data()), _DTYPES[int(blk.dataType())])
                if a.size == rows * cols:
                    bands.append(a.reshape(rows, cols))
            except Exception:
                continue
        if not bands:
            raise ValueError("读不到影像窗口")
        arr = np.stack(bands)
        x0, y0 = win.xMinimum(), win.yMaximum()
    rgb = _to_rgb_u8(arr)
    np.save(out_npy, rgb)
    h, w = rgb.shape[:2]
    return {"x0": float(x0), "y0": float(y0), "pw": float(pw), "ph": float(ph),
            "h": int(h), "w": int(w)}


# ---------- 掩码 → 多边形（影像 CRS） ----------

def unpack_mask(mask_b64, h, w):
    bits = np.unpackbits(
        np.frombuffer(base64.b64decode(mask_b64), np.uint8), count=h * w)
    return bits.reshape(h, w).astype(np.uint8)


def mask_to_geometry(mask_b64, h, w, x0, y0, pw, ph):
    """packbits 掩码 → gdal 多边形化 → 影像 CRS 坐标的 QgsGeometry 列表。"""
    if _gdal is None or _ogr is None:
        raise RuntimeError("QGIS python 缺 osgeo.gdal/ogr")
    mask = unpack_mask(mask_b64, h, w)
    ds = _gdal.GetDriverByName("Memory").Create("", w, h, 1, _gdal.GDT_Byte)
    ds.SetGeoTransform((x0, pw, 0.0, y0, 0.0, -ph))
    band = ds.GetRasterBand(1)
    band.WriteArray(mask)
    band.SetNoDataValue(0)
    lyr = ds.CreateLayer("m", None, _ogr.wkbPolygon)
    lyr.CreateField(_ogr.FieldDefn("dn", _ogr.OFTInteger))
    # maskband=band.GetMaskBand()：0 像元算洞（内部湖岛成内环）
    _gdal.Polygonize(band, band.GetMaskBand(), lyr, 0, [], callback=None)
    geoms = []
    for f in lyr:
        ref = f.GetGeometryRef()
        if ref is None:
            continue
        g = QgsGeometry.fromWkt(ref.ExportToWkt())
        if g is not None and not g.isEmpty():
            geoms.append(g)
    return geoms


def pick_blob(geoms, positive_pts, pixel_size):
    """取包含任一正点的连通块（无命中取最大块），并集后按 ~1.2 像元化简。

    positive_pts 元素可为 QgsPointXY 或 (x, y)（影像 CRS）。
    """
    if not geoms:
        return None

    def as_pt(p):
        return p if isinstance(p, QgsPointXY) else QgsPointXY(p[0], p[1])

    pts = [QgsGeometry.fromPointXY(as_pt(p)) for p in positive_pts] or [None]
    sel = [g for g in geoms
           if any(p is not None and g.intersects(p) for p in pts)]
    if not sel:
        sel = [max(geoms, key=lambda g: g.area())]
    out = sel[0]
    for g in sel[1:]:
        out = out.combine(g)
    tol = max(pixel_size, 0.01) * 1.2
    out = out.simplify(tol)
    return out if not out.isEmpty() else None


# ---------- 常驻 worker 服务 ----------

class _Pump(QThread):
    """把子进程的一行行输出泵成信号（readline 不预读，实时）。"""
    line = pyqtSignal(str)

    def __init__(self, stream):
        super().__init__()
        self._s = stream

    def run(self):
        while True:
            ln = self._s.readline()
            if not ln:
                break
            self.line.emit(ln.rstrip("\n"))


class SamClickService(QObject):
    """SAM worker 的异步客户端：单请求在途，回调总在主线程。"""

    def __init__(self, python, argv, log):
        super().__init__()
        self._log = log
        # QGIS（尤其 Windows）给自身内嵌 Python 设了 PYTHONHOME/PYTHONPATH，
        # 子进程一旦继承，venv 的 python 会被指向 QGIS 的库目录——torch
        # 装在 venv 里自然 import 不到。启动 worker 前必须剥掉这些变量。
        env = dict(os.environ)
        for k in list(env):
            if k.upper() in ("PYTHONHOME", "PYTHONPATH"):
                del env[k]
        self._proc = subprocess.Popen(
            [python] + argv, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, cwd=os.path.dirname(argv[0]))
        self._pending = None          # (cb,) 单在途请求
        self._last_embed = None
        self.device = None
        self._ready_pyhome = None     # worker 实际看到的 PYTHONHOME（诊断用）
        self._out = _Pump(self._proc.stdout)
        self._out.line.connect(self._on_line)
        self._out.start()
        self._err = _Pump(self._proc.stderr)
        self._err.line.connect(lambda s: log(f"[SAM] {s}"))
        self._err.start()
        # worker 没到 ready 就断管（如 torch 没装好即崩）：给一句人话提示
        self._out.finished.connect(self._died_early)

    def _died_early(self):
        if self.device is None:
            self._log("[点选] SAM worker 启动即退出——多半是 nn/ 环境没装完整，"
                      "请重跑 nn/setup.sh（或 setup.bat）续装后再试")

    def alive(self):
        return self._proc.poll() is None

    def busy(self):
        return self._pending is not None

    def _on_line(self, ln):
        if not ln:
            return
        try:
            obj = json.loads(ln)
        except ValueError:
            self._log(f"[SAM] 无法解析：{ln[:120]}")
            return
        if obj.get("event") == "ready":
            self.device = obj.get("device")
            self._ready_pyhome = obj.get("pyhome")
            self._log(f"[点选] SAM 就绪（{self.device}）")
            return
        if self._pending is None:
            return  # 陈旧响应，丢弃
        cb = self._pending
        self._pending = None
        try:
            cb(obj)
        except Exception as exc:
            self._log(f"[点选] 回调异常：{exc!r}")

    def _send(self, req, cb):
        if not self.alive():
            cb({"ok": False, "error": "SAM 进程已退出"})
            return
        if self._pending is not None:
            return  # 忙：调用方稍后重试
        self._pending = cb
        try:
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._pending = None
            cb({"ok": False, "error": repr(exc)})

    def embed(self, npy_path, cb, key=None):
        """同 key 不重复编码（换格才重新 set_image，约 1s GPU）。"""
        if key is not None and self._last_embed == key:
            cb({"ok": True, "cached": True})
            return

        def done(r):
            if r.get("ok"):
                self._last_embed = key or npy_path
            cb(r)
        self._send({"cmd": "embed", "npy": npy_path}, done)

    def predict(self, points, labels, fresh, cb):
        self._send({"cmd": "predict", "points": points,
                    "labels": labels, "fresh": bool(fresh)}, cb)

    def shutdown(self):
        for attr in ("stdin",):
            try:
                getattr(self._proc, attr).close()
            except Exception:
                pass
        try:
            self._proc.terminate()
            self._proc.wait(3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        for t in (getattr(self, "_out", None), getattr(self, "_err", None)):
            if t is not None:
                t.wait(1500)
