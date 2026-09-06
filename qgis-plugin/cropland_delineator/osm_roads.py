"""OSM 道路先验：Overpass 拉取道路中心线 → 缓存 GPKG（重投影到工程 CRS），
供工作台"按道路挖除"一键做道路差集，替代手画中心线。

数据 © OpenStreetMap contributors（ODbL，学术标注注明来源即可）。
只在用户点击"拉取道路"时访问网络；失败时回退渔网目录下的缓存文件。
"""

import json
import os
import urllib.parse
import urllib.request

from qgis.PyQt.QtCore import QThread, pyqtSignal, QMetaType
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsVectorFileWriter,
)

# 国内可达性时好时坏，逐个降级尝试
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# 走"大路"宽度档的 highway 值，其余全部按"小路"档
MAJOR_CLASSES = {
    "motorway", "trunk", "primary",
    "motorway_link", "trunk_link", "primary_link",
}


def cache_path(grid):
    """道路缓存放渔网文件同目录；渔网不是文件图层时返回 None。"""
    src = grid.source().split("|")[0]
    if src.startswith("/") and os.path.isfile(src):
        return os.path.join(os.path.dirname(src), "osm_道路.gpkg")
    return None


def bbox_4326(grid):
    """渔网范围 → (南, 西, 北, 东) WGS84。"""
    ext = QgsRectangle(grid.extent())
    if grid.crs().authid() != "EPSG:4326":
        xform = QgsCoordinateTransform(
            grid.crs(),
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance())
        ext = xform.transformBoundingBox(ext)
    return (ext.yMinimum(), ext.xMinimum(),
            ext.yMaximum(), ext.xMaximum())


class FetchThread(QThread):
    """后台拉 Overpass（几十秒级），完成后 done 信号回主线程建层。"""

    done = pyqtSignal(dict)

    def __init__(self, grid, parent=None):
        super().__init__(parent)
        self.bbox = bbox_4326(grid)

    def run(self):
        s, w, n, e = self.bbox
        query = (f"[out:json][timeout:90];"
                 f"way['highway']({s:.6f},{w:.6f},{n:.6f},{e:.6f});"
                 f"out tags geom;")
        data = urllib.parse.urlencode({"data": query}).encode()
        last_error = "无可用 Overpass 端点"
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                req = urllib.request.Request(
                    endpoint, data=data,
                    headers={"User-Agent": "cropland-delineator-qgis/0.3"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                features = []
                for el in payload.get("elements", []):
                    geo = el.get("geometry")
                    if not geo or len(geo) < 2:
                        continue
                    features.append({
                        "type": "Feature",
                        "properties": {
                            "highway": el.get("tags", {}).get("highway", ""),
                            "osm_id": el.get("id", 0),
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[p["lon"], p["lat"]] for p in geo],
                        },
                    })
                self.done.emit({"geojson": features, "endpoint": endpoint})
                return
            except Exception as exc:  # 换下一个镜像
                last_error = f"{endpoint}: {exc}"
        self.done.emit({"error": last_error})


def save_roads(features, grid, dest_crs=None):
    """WGS84 GeoJSON 要素列表 → 重投影写入缓存 GPKG，返回路径（失败 None）。"""
    path = cache_path(grid)
    if path is None:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "osm_道路.gpkg")
    if dest_crs is None:
        dest_crs = QgsProject.instance().crs()

    mem = QgsVectorLayer(
        f"linestring?crs={dest_crs.authid()}", "osm_roads", "memory")
    mem.dataProvider().addAttributes([
        QgsField("highway", QMetaType.Type.QString),
        QgsField("osm_id", QMetaType.Type.LongLong),
    ])
    mem.updateFields()
    xform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        dest_crs, QgsProject.instance())
    for f in features:
        coords = f["geometry"]["coordinates"]
        g = QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords])
        g.transform(xform)
        feat = QgsFeature(mem.fields())
        feat.setAttributes([
            f["properties"].get("highway", ""),
            f["properties"].get("osm_id", 0),
        ])
        feat.setGeometry(g)
        mem.dataProvider().addFeature(feat)

    if os.path.exists(path):
        os.remove(path)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "osm_roads"
    opts.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    err, _, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, path, QgsProject.instance().transformContext(), opts)
    if err != QgsVectorFileWriter.WriterError.NoError:
        return None
    return path
