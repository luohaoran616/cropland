"""Cropland Delineator — QGIS 4 plugin entry point."""


def classFactory(iface):
    from .plugin import CroplandDelineatorPlugin

    return CroplandDelineatorPlugin(iface)
