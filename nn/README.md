# nn：神经网络边界证据实验（决策路线 1，最小实验）

目的：验证"离线 NN 边界证据 + 现有 A* 磁力引擎"能否救回梯度法贴不住的
无反差边界（同色土路、阴影、地膜干扰）。插件端已就绪（v0.5.2）：
`LassoEngine(evidence=...)` 与梯度代价取 min 融合，`lasso_context()` 自动发现
`<影像目录>/边缘证据/<格子>.npz`。无证据文件时行为与旧版完全一致。

## 管线（三步）

```bash
cd <仓库路径>/nn

# 1) 裁格（QGIS python，纯 gdal；网格与引擎逐像素对齐已验证）
PYTHONPATH=/usr/share/qgis/python python3 crop_cell.py \
    --raster ../qgis/海城市/海城市_影像_5m.tif \
    --fishnet ../qgis/海城市/海城市_样方渔网_5m_1536.shp \
    --cell 海城市_2_6 --out crops

# 2) 生成证据（torch 环境；ckpt 下载一次放 ckpt/）
.venv/bin/python make_evidence.py --crops crops --out evidence \
    --ckpt ckpt/sam_vit_b_01ec64.pth --cells 海城市_2_6

# 3) 部署到插件约定路径并重载插件（引擎缓存键含证据 mtime，自动重建）
mkdir -p ../qgis/海城市/边缘证据
cp evidence/海城市_2_6.npz evidence/海城市_2_6.json ../qgis/海城市/边缘证据/
```

判定：同一批锚点在"难例位置"对比有无证据的走线；证据预览图
`evidence/<格>_预览.png`（红=证据强）先目视判断边界密度是否合理。
结论记回 library（正/负结果都记，参照决策 0004 的负结果格式）。

## 环境（装前须用户批准）

`.venv`（uv，硬链接复用 ~/.cache/uv 里的 torch 2.6.0+cu124，几乎零额外磁盘）：
segment-anything（pip，纯 Python ~100KB）+ SAM ViT-B 权重（375MB，下载一次）。
RTX 3050 4GB 跑 ViT-B 推理显存足够；每格 2049² 分 3×3 块约十几秒。
卸载 = `rm -rf .venv ckpt crops evidence`。
