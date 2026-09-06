"""Helpers used by the dialog: env checks, cutline writing, result merging."""

import json
import os

from qgis.PyQt.QtCore import QMetaType
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsVectorFileWriter,
    QgsWkbTypes,
)


def check_environment(python, worker, model):
    missing = []
    for label, path in (
        ("推理 venv Python（.venv/bin/python）", python),
        ("推理脚本 inference_worker.py", worker),
        ("模型权重 models/DelineateAnythingv2.pt", model),
    ):
        if not os.path.exists(path):
            missing.append(f"{label}\n  {path}")
    return missing


def local_raster_path(layer):
    """Return a local file path for the raster layer, or None if not local."""
    source = layer.source()
    if source.startswith("/") and os.path.isfile(source):
        return source
    # handle "file:///a/b.tif" style URIs
    if source.startswith("file://"):
        path = source[len("file://"):]
        if os.path.isfile(path):
            return path
    return None


def cell_display_id(feat, idx):
    return f"cell_{idx:04d}"


def write_cutline_gpkg(geom, orig_geom, crs, tmp_root, idx):
    """Write one GPKG with two layers: buffered clip extent + original cell."""
    path = os.path.join(tmp_root, f"cutline_{idx:04d}.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    ctx = QgsProject.instance().transformContext()
    for name, geometry in (("cell", geom), ("cell_orig", orig_geom)):
        vl = QgsVectorLayer(
            f"Polygon?crs={crs.authid()}", name, "memory"
        )
        vl.dataProvider().addAttributes([QgsField("fid", QMetaType.Type.Int)])
        vl.updateFields()
        f = QgsFeature(vl.fields())
        f.setGeometry(geometry)
        f.setAttribute("fid", idx)
        vl.dataProvider().addFeatures([f])
        opts.layerName = name
        opts.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            if os.path.exists(path)
            else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        )
        err, _, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
            vl, path, ctx, opts
        )
        if err != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(f"写 cutline 层 {name} 失败：{err}")
    return path


def write_worker_params(workdir, *, image, cutline, output, model,
                        confidence=None, minimum_area_m2=0, clip_to_cell=False):
    params = {
        "image": image,
        "cutline": cutline,
        "output": output,
        "model": model,
        "workdir": workdir,
    }
    if confidence is not None:
        params["confidence"] = confidence
    if minimum_area_m2 and minimum_area_m2 > 0:
        params["minimum_area_m2"] = minimum_area_m2
    if clip_to_cell:
        params["clip_to_cell"] = True
    path = os.path.join(workdir, "params.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(params, fh, ensure_ascii=False, indent=2)
    return path


def merge_outputs(gpkg_list, out_path, iface):
    """Merge per-cell 'fields' layers into one GPKG, load it, return True on success."""
    valid = [p for p in gpkg_list if os.path.exists(p)]
    if not valid:
        return False

    out_path = os.path.abspath(out_path)
    # If the target file is currently loaded as a layer, remove it first:
    # QGIS holds the GPKG open, and overwriting fails with
    # WriterError.ErrFeatureWriteFailed (7).
    from qgis.core import QgsProject

    for lid, layer in list(QgsProject.instance().mapLayers().items()):
        layer_path = layer.source().split("|")[0]
        if layer_path == out_path:
            QgsProject.instance().removeMapLayer(lid)

    fields = None
    crs = None
    merged = QgsVectorLayer(
        "Polygon?crs=EPSG:4326", "cropland_fields", "memory"
    )
    for path in valid:
        layer = QgsVectorLayer(path + "|layername=fields", "cell_out", "ogr")
        if not layer.isValid():
            continue
        if fields is None:
            fields = layer.fields()
            crs = layer.crs()
            merged = QgsVectorLayer(
                f"Polygon?crs={crs.authid()}", "cropland_fields", "memory"
            )
            merged.dataProvider().addAttributes(fields.toList())
            merged.updateFields()
        for feat in layer.getFeatures():
            out = QgsFeature(merged.fields())
            out.setGeometry(feat.geometry())
            out.setAttributes(feat.attributes())
            merged.dataProvider().addFeature(out)
    if fields is None:
        return False

    merged.updateExtents()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "fields"
    opts.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
    )
    err, _, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
        merged, out_path, QgsProject.instance().transformContext(), opts
    )
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"写合并输出失败：{err}")

    iface.addVectorLayer(out_path + "|layername=fields", "cropland_fields", "ogr")
    return True
