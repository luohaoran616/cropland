@echo off
rem SAM 点选分割（🖱 点选）环境一键搭建 · Windows
rem 用法：双击本文件，或在 cmd 里执行 nn\setup.bat
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python 启动器 py。
    echo        请安装 Python 3.12 并勾选 "Add python.exe to PATH"：
    echo        https://www.python.org/downloads/release/python-3120/
    pause & exit /b 1
)

echo [1/4] 建 Python 3.12 虚拟环境
py -3.12 -m venv .venv
if errorlevel 1 (
    echo [错误] py -3.12 失败：请安装 Python 3.12（上面的链接）。
    pause & exit /b 1
)
set PIP=.venv\Scripts\pip.exe

nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo [2/4] 未检测到 NVIDIA GPU → 安装 CPU 版 torch（点选可用，每格约 10-30 秒较慢）
    set IDX=https://download.pytorch.org/whl/cpu
) else (
    echo [2/4] 检测到 NVIDIA GPU → 安装 CUDA 版 torch/torchvision（约 2-3 GB，耐心等）
    set IDX=https://download.pytorch.org/whl/cu124
)
"%PIP%" install torch==2.6.0 torchvision==0.21.0 --index-url %IDX%
if errorlevel 1 (echo [错误] torch 安装失败：检查网络（国内可挂代理）后重跑本脚本续装 & pause & exit /b 1)

echo [3/4] 安装 segment-anything / numpy
"%PIP%" install segment-anything numpy
if errorlevel 1 (echo [错误] 依赖安装失败 & pause & exit /b 1)

echo [4/4] 下载 SAM ViT-B 权重（358MB，失败就重跑本脚本续传）
if not exist ckpt mkdir ckpt
curl.exe -L -C - -o ckpt\sam_vit_b_01ec64.pth https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth

certutil -hashfile ckpt\sam_vit_b_01ec64.pth SHA256 | findstr /i "ec2df62732614e57411cdcf32a23ffdf28910380d03139ee0f4fcbe91eb8c912" >nul
if errorlevel 1 (
    echo [错误] 权重校验不通过：删掉 ckpt 里的文件后重跑本脚本。
    pause & exit /b 1
)

echo [自检]
.venv\Scripts\python.exe -c "import torch, segment_anything; print('  torch', torch.__version__, '| CUDA', torch.cuda.is_available()); print('  segment-anything ok')"

echo.
echo ==============================================
echo  环境就绪：%cd%
echo  最后一步：QGIS - 插件 - Python 控制台，粘贴下面这行
echo  （路径已按本机填好，粘贴完立即可用、无需重启）：
echo.
echo from qgis.PyQt.QtCore import QSettings; QSettings().setValue("cropland_delineator/nn_dir", r"%cd%")
echo.
echo ==============================================
pause
