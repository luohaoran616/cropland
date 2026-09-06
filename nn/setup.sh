#!/usr/bin/env bash
# SAM 点选分割（🖱 点选）环境一键搭建 · Linux
# 用法：bash setup.sh   （任意位置执行均可，自动定位到脚本所在目录）
# 做四件事：建 venv → 装 torch/torchvision（自动判 GPU/CPU）→ 装
# segment-anything/numpy → 下载 SAM ViT-B 权重（断点续传 + sha256 校验）。
set -euo pipefail
NN="$(cd "$(dirname "$0")" && pwd)"
cd "$NN"

CKPT=ckpt/sam_vit_b_01ec64.pth
SHA=ec2df62732614e57411cdcf32a23ffdf28910380d03139ee0f4fcbe91eb8c912
URL=https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth
# 国内网络可选：PYTORCH_INDEX=https://mirrors.aliyun.com/pytorch-wheels/cu124 bash setup.sh
PYTORCH_INDEX="${PYTORCH_INDEX:-https://download.pytorch.org/whl/cu124}"

# ---- 1/4 建 venv（torch 2.6 轮子最高支持 Python 3.12，必须钉住） ----
echo "[1/4] 建 Python 3.12 虚拟环境"
if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    PIP=(uv pip install --python .venv/bin/python)
else
    PY=python3.12
    command -v "$PY" >/dev/null 2>&1 || PY=python3
    "$PY" -m venv .venv
    PIP=(.venv/bin/pip install)
    [ "$PY" = python3 ] && echo "  [提示] 未找到 python3.12，用 $("$PY" -V 2>&1)。"
    echo "         若下面 torch 装不上，装个 uv 最省事：curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# ---- 2/4 GPU 判定 + torch ----
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    echo "[2/4] 检测到 NVIDIA GPU → 安装 CUDA 版 torch/torchvision（约 2-3 GB，耐心等）"
else
    echo "[2/4] 未检测到 NVIDIA GPU → 安装 CPU 版（点选可用，每格编码约 10-30 秒较慢）"
    PYTORCH_INDEX=https://download.pytorch.org/whl/cpu
fi
"${PIP[@]}" torch==2.6.0 torchvision==0.21.0 --index-url "$PYTORCH_INDEX"

# ---- 3/4 轻依赖 ----
echo "[3/4] 安装 segment-anything / numpy"
"${PIP[@]}" segment-anything numpy

# ---- 4/4 权重（358MB，断点续传 + 校验） ----
echo "[4/4] 下载 SAM ViT-B 权重"
mkdir -p ckpt
if [ -f "$CKPT" ] && echo "$SHA  $CKPT" | sha256sum -c --quiet >/dev/null 2>&1; then
    echo "      已存在且校验通过，跳过"
else
    wget -c --progress=dot:giga -O "$CKPT" "$URL" \
        || curl -L -C - -o "$CKPT" "$URL"
    echo "$SHA  $CKPT" | sha256sum -c
fi

# ---- 自检 ----
echo "[自检]"
.venv/bin/python - <<'PY'
import torch
print(f"  torch {torch.__version__} · CUDA {'可用 ✓' if torch.cuda.is_available() else '不可用（CPU 模式）'}")
import segment_anything
print("  segment-anything 导入 ✓")
PY

cat <<TIP

✅ 环境就绪：$NN
最后一步，让 QGIS 插件找到它（任选其一，A 最简单）：
  A. 打开 QGIS → 插件 → Python 控制台，粘贴这一行（把路径换成上面显示的）：
     from qgis.PyQt.QtCore import QSettings; QSettings().setValue("cropland_delineator/nn_dir", "$NN")
     粘贴完立即可用，无需重启 QGIS。
  B. 或设环境变量 CROPLAND_NN_DIR=$NN 后重启 QGIS。
然后在耕地标注工作台点「🖱 点选」，首次每格编码约 1 秒（CPU 约 10-30 秒）。
TIP
