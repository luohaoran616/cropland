# Cropland Delineator — 耕地标注工作台

QGIS 4 插件：面向 5m 卫星影像的耕地逐格标注（减法勾绘）。
渔网格（样方）驱动，整格建底板后用各类笔刷做减法/补画，最终耕地 =
底板 + 补块 − 扣除（道路 / 城镇 / 坑塘…），全程操作台账可重放、可单步删除。

## 功能速览

- **标注工作台侧边栏**：格导航（建底板 / 缩放 / 下一格）、笔刷 2×4 网格、
  收尾 QA（碎块清理 / 重叠 / 漏画 / 暗斑 / 报表 / 进度）、操作日志；
- **笔刷**：道路（缓冲差集）、切分、框挖、多边形挖/补画、磁力切分/磁力道路
  （沿影像梯度的 A\* 走线，可选 NN 边界证据增强）、**SAM 点选分割**
  （点一下目标内部自动出掩码，正/负点迭代，Enter 落地）；
- **操作台账**：每笔几何+参数进 JSONL，回退到任意步、删除任意单步、
  改道路宽度重放；🥞 分层视图把当前格分解为底板 / 补块 / 扣除三个
  独立图层，图上选中即可删除对应操作；
- **OSM 道路先验**：Overpass 拉取路网按档宽批量挖除；
- 键盘：E 选中即挖除 / N 下一格 / M 磁力切分 / R 磁力道路，
  笔迹中 Esc 取消、Backspace·Ctrl+Z 退点、Enter 收笔。

## 安装（朋友侧，一次性）

1. QGIS → 插件 → 管理和安装插件 → **设置** → 插件源 → **添加**：
   - 名称：`cropland`（随意）
   - URL：`https://github.com/luohaoran616/cropland/releases/latest/download/plugins.xml`
2. 回到「全部」搜索 **Cropland Delineator** 安装（首次会提示信任该源）；
3. 菜单 矢量 → Cropland Delineator → 勾选「耕地标注工作台」打开侧栏；
4. 之后我发新版，你的 QGIS 会在插件管理器里自动提示「升级」，一键更新。

核心标注功能零依赖、开箱即用。

## 可选：启用 SAM 点选分割（🖱 点选）

需要 NVIDIA GPU（约 1GB 显存）：

```bash
git clone https://github.com/luohaoran616/cropland.git
cd cropland/nn

# 1) Python 环境（uv 或 venv 均可，需要 torch(CUDA) + segment-anything + numpy）
uv venv && uv pip install torch --index-url https://download.pytorch.org/whl/cu124
uv pip install segment-anything numpy

# 2) 下载 SAM ViT-B 权重（约 358MB，Apache-2.0）放 ckpt/
wget -O ckpt/sam_vit_b_01ec64.pth \
  https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth
```

插件会按 `仓库布局 nn/` 自动发现（把插件与 nn/ 放成同样的相对位置），
或设置环境变量 `CROPLAND_NN_DIR` 指向 nn/ 目录后重启 QGIS。
没配也不影响其他功能，日志会提示一句。

`nn/` 目录另有离线边界证据生成管线（磁力走线增强，可选），见 [nn/README.md](nn/README.md)。

## 开发

```bash
# 无头回归（offscreen）
cd qgis-plugin/cropland_delineator
QT_QPA_PLATFORM=offscreen \
PYTHONPATH=/usr/share/qgis/python:$(git rev-parse --show-toplevel)/qgis-plugin \
python3 tests/test_ledger.py        # 台账 12 组
python3 tests/test_lasso.py         # 磁力引擎 14 组

# 发版：改 metadata.txt 版本号后
bash tools/publish.sh
```
