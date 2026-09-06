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

echo [2/4] 安装 torch / torchvision（默认走阿里云国内镜像，失败自动回退官方源）
nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo        未检测到 NVIDIA GPU → 安装 CPU 版（点选可用，每格约 10-30 秒较慢）
    set CU=cpu
) else (
    echo        检测到 NVIDIA GPU → 安装 CUDA 版（约 2-3 GB，耐心等）
    set CU=cu124
)

rem 可用环境变量覆盖镜像：set PYTORCH_INDEX=... / set PIP_INDEX=... 后再跑本脚本
if "%PYTORCH_INDEX%"=="" set PYTORCH_INDEX=https://mirrors.aliyun.com/pytorch-wheels/%CU%
if "%PIP_INDEX%"=="" set PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/

"%PIP%" install torch==2.6.0 torchvision==0.21.0 --index-url %PYTORCH_INDEX%
if errorlevel 1 (
    echo [提示] 镜像安装失败，改用官方源重试（较慢，可挂代理）…
    "%PIP%" install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/%CU%
    if errorlevel 1 (echo [错误] torch 安装失败：检查网络后重跑本脚本续装 & pause & exit /b 1)
)

echo [3/4] 安装 segment-anything / numpy（阿里云 PyPI 镜像）
"%PIP%" install segment-anything numpy -i %PIP_INDEX%
if errorlevel 1 (
    echo [提示] 镜像失败，改用官方 PyPI…
    "%PIP%" install segment-anything numpy
    if errorlevel 1 (echo [错误] 依赖安装失败 & pause & exit /b 1)
)

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
if errorlevel 1 (
    echo.
    echo [错误] 自检失败：torch / segment-anything 没有装进 .venv。
    echo        上面紧挨着的报错就是原因（常见：下载中断、杀软拦截）。
    echo        直接重跑本脚本即可续装；反复失败就把报错截图发回来。
    pause & exit /b 1
)

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
