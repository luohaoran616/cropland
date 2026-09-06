#!/usr/bin/env python3
"""SAM 边界证据生成：crop 出的 <cell>.npy → 边界概率 npz（插件磁力引擎融合用）。

在独立 torch 环境运行（见 nn/README.md）：
  .venv/bin/python make_evidence.py --crops crops --out evidence \
      --ckpt ckpt/sam_vit_b_01ec64.pth [--cells 海城市_2_6 海城市_7_3]

流程：768px 分块（64px 重叠）→ SAM ViT-B 自动掩码 → 每个掩码的边界
（本体减 3×3 腐蚀）累积成边界强度 → 三次盒式模糊近似高斯 → 95 百分位
归一化到 P(边界)∈[0,1] → evidence/<cell>.npz(float16) + 同名 .json
（复制 crop 元数据，插件按此校验网格）+ <cell>_预览.png（红=证据强）。

设计注记：决策 0004 证明 SAM2 整格 AMG 切不出可用田块；这里只用它的
掩码"边界在哪里"，不要求掩码=田块——证据与梯度取 min 融合（决策路线 1）。
"""
import argparse
import json
import os
import shutil

import numpy as np
import torch
from PIL import Image
from segment_anything import sam_model_registry, SamAutomaticMaskGenerator

TILE = 768
OVERLAP = 64


def erode3(m):
    """3×3 二值腐蚀（numpy 切片实现，无 scipy 依赖）。"""
    p = np.pad(m, 1, mode="edge")
    return (p[:-2, :-2] & p[:-2, 1:-1] & p[:-2, 2:]
            & p[1:-1, :-2] & p[1:-1, 1:-1] & p[1:-1, 2:]
            & p[2:, :-2] & p[2:, 1:-1] & p[2:, 2:])


def _box1(a, r, axis):
    a = np.moveaxis(a.astype(np.float32), axis, 0)
    pad = np.pad(a, ((r, r),) + ((0, 0),) * (a.ndim - 1), mode="edge")
    c = np.cumsum(pad, axis=0)
    out = (c[2 * r:] - c[:-2 * r]) / (2 * r + 1)
    return np.moveaxis(out, 0, axis)


def blur(a, r=2):
    """三次可分离盒式模糊 ≈ 高斯 σ≈1.5。"""
    for _ in range(3):
        a = _box1(_box1(a, r, 0), r, 1)
    return a


def tile_edges(amg, rgb):
    h, w = rgb.shape[:2]
    acc = np.zeros((h, w), np.float32)
    hits = np.zeros((h, w), np.float32)
    step = max(TILE - OVERLAP, 1)
    for y in range(0, h, step):
        for x in range(0, w, step):
            sub = rgb[y:y + TILE, x:x + TILE]
            if sub.shape[0] < 96 or sub.shape[1] < 96:
                continue
            edge = np.zeros(sub.shape[:2], np.float32)
            for m in amg.generate(sub):
                seg = m["segmentation"]
                edge += (seg & ~erode3(seg))
            hh, ww = edge.shape
            acc[y:y + hh, x:x + ww] += edge
            hits[y:y + hh, x:x + ww] += 1.0
    return acc / np.maximum(hits, 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crops", default="crops")
    ap.add_argument("--out", default="evidence")
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--cells", nargs="+", required=True)
    ap.add_argument("--model", default="vit_b")
    ap.add_argument("--points-per-batch", type=int, default=48)
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    sam = sam_model_registry[a.model](checkpoint=a.ckpt).to(dev)
    sam.eval()
    amg = SamAutomaticMaskGenerator(
        sam, points_per_batch=a.points_per_batch)
    print(f"[ev] 模型 {a.model} on {dev}，显存 "
          f"{torch.cuda.get_device_properties(0).total_memory/1e9:.1f}G"
          if dev == "cuda" else f"[ev] 模型 {a.model} on CPU（慢，预计每格数分钟）")

    os.makedirs(a.out, exist_ok=True)
    for cell in a.cells:
        rgb = np.load(os.path.join(a.crops, cell + ".npy"))
        with open(os.path.join(a.crops, cell + ".json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        raw = tile_edges(amg, rgb)
        ev = blur(raw, r=2)
        pos = ev[ev > 0]
        if pos.size:
            ev = np.clip(ev / max(np.percentile(pos, 95), 1e-6), 0.0, 1.0)
        np.savez_compressed(
            os.path.join(a.out, cell + ".npz"),
            ev=ev.astype(np.float16))
        shutil.copy(os.path.join(a.crops, cell + ".json"),
                    os.path.join(a.out, cell + ".json"))
        prev = rgb.copy()
        red = (ev[None] > 0.5)
        prev[..., 0] = np.where(red[0], 255, prev[..., 0])
        Image.fromarray(prev).save(
            os.path.join(a.out, cell + "_预览.png"))
        hot = float((ev > 0.5).mean())
        print(f"[ev] {cell}: 边界像素占比 {hot:.1%} → {a.out}/{cell}.npz")


if __name__ == "__main__":
    main()
