#!/usr/bin/env python3
"""裁格：把渔网一格的 RGB 影像按引擎同款窗口几何裁出来，供 NN 生成边界证据。

运行（用 QGIS 自带 python，纯 gdal/ogr，无需 initQgis）：
  PYTHONPATH=/usr/share/qgis/python python3 crop_cell.py \
      --raster ../qgis/海城市/海城市_影像_5m.tif \
      --fishnet ../qgis/海城市/海城市_样方渔网_5m_1536.shp \
      --cell 海城市_2_6 --out crops

输出 crops/<cell>.npy（uint8 H×W×3）+ crops/<cell>.json。
窗口几何 100% 复刻 lasso.LassoEngine._read_window 的 gdal 分支
（margin 256px 与引擎默认一致），保证证据数组与引擎网格逐像素对齐。
"""
import argparse
import json
import math
import os

import numpy as np
from osgeo import gdal, ogr, osr

MARGIN_PX = 256  # 与 lasso.LassoEngine 默认 margin_px 一致


def cell_bbox(fishnet, cell):
    ds = ogr.Open(fishnet)
    if ds is None:
        raise SystemExit(f"打不开渔网：{fishnet}")
    lyr = ds.GetLayer()
    for f in lyr:
        if str(f.GetField("FID_1")) == cell:
            xmin, xmax, ymin, ymax = f.GetGeometryRef().GetEnvelope()
            src = lyr.GetSpatialRef()
            return (xmin, ymin, xmax, ymax), src
    raise SystemExit(f"渔网里没有格子 {cell}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raster", required=True)
    ap.add_argument("--fishnet", required=True)
    ap.add_argument("--cell", required=True)
    ap.add_argument("--out", default="crops")
    a = ap.parse_args()

    (xmin, ymin, xmax, ymax), grid_srs = cell_bbox(a.fishnet, a.cell)
    ds = gdal.Open(a.raster)
    if ds is None:
        raise SystemExit(f"打不开影像：{a.raster}")
    gt = ds.GetGeoTransform()
    ras_srs = osr.SpatialReference(wkt=ds.GetProjection())
    if grid_srs is not None and not grid_srs.IsSame(ras_srs):
        # 渔网与影像 CRS 不同：按传统 x,y 序变换两个对角点
        grid_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ras_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tx = osr.CoordinateTransformation(grid_srs, ras_srs)
        x0y0 = tx.TransformPoint(xmin, ymin)
        x1y1 = tx.TransformPoint(xmax, ymax)
        xmin, ymin, xmax, ymax = x0y0[0], x0y0[1], x1y1[0], x1y1[1]

    pw, ph = abs(gt[1]), abs(gt[5])
    m = MARGIN_PX * pw
    wx0, wx1 = xmin - m, xmax + m
    wy0, wy1 = ymin - MARGIN_PX * ph, ymax + MARGIN_PX * ph
    cx0 = max(int(math.floor((wx0 - gt[0]) / gt[1])), 0)
    cx1 = min(int(math.ceil((wx1 - gt[0]) / gt[1])), ds.RasterXSize)
    cy0 = max(int(math.floor((gt[3] - wy1) / -gt[5])), 0)
    cy1 = min(int(math.ceil((gt[3] - wy0) / -gt[5])), ds.RasterYSize)
    if cx1 - cx0 < 2 or cy1 - cy0 < 2:
        raise SystemExit("格子与影像不相交")

    arr = ds.ReadAsArray(cx0, cy0, cx1 - cx0, cy1 - cy0)  # (bands,H,W)
    if arr.ndim == 2:
        arr = arr[None]
    rgb = np.transpose(arr[:3], (1, 2, 0))
    if rgb.dtype != np.uint8:
        lo, hi = np.percentile(rgb, 2.0), np.percentile(rgb, 98.0)
        rgb = np.clip((rgb - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255)
        rgb = rgb.astype(np.uint8)

    os.makedirs(a.out, exist_ok=True)
    stem = os.path.join(a.out, a.cell)
    np.save(stem + ".npy", rgb)
    meta = {
        "cell": a.cell, "shape": [int(rgb.shape[0]), int(rgb.shape[1])],
        "x0": gt[0] + cx0 * gt[1], "y0": gt[3] + cy0 * gt[5],
        "pw": pw, "ph": ph, "crs_wkt": ds.GetProjection(),
    }
    with open(stem + ".json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    print(f"[crop] {a.cell}: {rgb.shape[1]}×{rgb.shape[0]} px "
          f"@{pw:g}m → {stem}.npy (+.json)")


if __name__ == "__main__":
    main()
