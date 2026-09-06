#!/usr/bin/env python3
"""一键打包发版：生成插件 ZIP + plugins.xml（自建 QGIS 插件源索引）。

用法（在仓库根目录）：
    python3 tools/release_plugin.py [--repo luohaoran616/cropland]

产出：
    dist/cropland_delineator-<版本>.zip   # 顶层为 cropland_delineator/，
                                          # 已排除 tests / __pycache__ / *.pyc
    dist/plugins.xml                      # 供 QGIS「插件源」使用的索引

随后用 gh 创建 Release（publish.sh 已自动包含）：
    gh release create v<版本> dist/cropland_delineator-<版本>.zip dist/plugins.xml ...

朋友侧只需在 QGIS 里添加一次插件源：
    https://github.com/<repo>/releases/latest/download/plugins.xml
之后每次发新版本号，他们的 QGIS 会自动提示升级。
"""

import argparse
import hashlib
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "qgis-plugin", "cropland_delineator")
DIST = os.path.join(ROOT, "dist")

EXCLUDE_DIRS = {"tests", "__pycache__", ".git"}
EXCLUDE_SUFFIX = {".pyc", ".pyo"}


def read_version():
    with open(os.path.join(SRC, "metadata.txt"), encoding="utf-8") as fh:
        m = re.search(r"^version\s*=\s*(\S+)", fh.read(), re.M)
    if not m:
        sys.exit("[错误] metadata.txt 里读不到 version")
    return m.group(1)


def build_zip(ver):
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"cropland_delineator-{ver}.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(SRC):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1] in EXCLUDE_SUFFIX:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, SRC)
                z.write(full, os.path.join("cropland_delineator", rel))
                n += 1
    return out, n


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_plugins_xml(repo, ver, fname, size, digest):
    dl = f"https://github.com/{repo}/releases/download/v{ver}/{fname}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="Cropland Delineator" version="{ver}">
    <name>Cropland Delineator</name>
    <description>耕地标注工作台：渔网格减法勾绘 + 磁力走线 + SAM 点选分割 + 操作台账</description>
    <version>{ver}</version>
    <qgis_minimum_version>4.0</qgis_minimum_version>
    <qgis_maximum_version>4.99</qgis_maximum_version>
    <homepage>https://github.com/{repo}</homepage>
    <tracker>https://github.com/{repo}/issues</tracker>
    <author_name>luo</author_name>
    <file_name>{fname}</file_name>
    <download_url>{dl}</download_url>
    <experimental>False</experimental>
    <deprecated>False</deprecated>
    <tags>cropland, annotation, agriculture, remote sensing</tags>
    <!-- sha256={digest} size={size} -->
  </pyqgis_plugin>
</plugins>
"""
    path = os.path.join(DIST, "plugins.xml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="luohaoran616/cropland")
    args = ap.parse_args()

    ver = read_version()
    zip_path, n = build_zip(ver)
    size = os.path.getsize(zip_path)
    digest = sha256(zip_path)
    xml_path = write_plugins_xml(args.repo, ver,
                                 os.path.basename(zip_path), size, digest)
    print(f"[打包] {n} 个文件 → {zip_path}（{size/1024:.0f} KiB）")
    print(f"[校验] sha256 = {digest}")
    print(f"[索引] {xml_path}")
    print("\n下一步（需 gh 已登录）：")
    print(f"  gh release create v{ver} {zip_path} {xml_path} "
          f"-t v{ver} -n '耕地标注工作台 v{ver}'")
    print(f"\n朋友侧插件源 URL（添加一次，永久有效，自动跟随最新版）：")
    print(f"  https://github.com/{args.repo}/releases/latest/download/plugins.xml")


if __name__ == "__main__":
    main()
