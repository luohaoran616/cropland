#!/usr/bin/env bash
# 发布 / 更新插件：提交代码 → push → 打包 → 建 GitHub Release。
# 前置：gh auth login（一次即可）。
# 用法：bash tools/publish.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="luohaoran616/cropland"
VER="$(grep '^version' qgis-plugin/cropland_delineator/metadata.txt | cut -d= -f2)"
echo "== 发布 v$VER → $REPO =="

if ! gh auth status >/dev/null 2>&1; then
    echo "[错误] gh 未登录：先运行  gh auth login  再来" >&2
    exit 1
fi

# 1) 提交并推送（白名单 .gitignore 保证只含插件代码与 nn 脚本）
git add -A
git commit -m "release v$VER" || echo "[跳过] 无待提交变更"
git push -u origin main || {
    echo "[提示] 远端已有提交（如建仓时自动生成的 README），rebase 后重推…"
    git pull --rebase --allow-unrelated-histories origin main
    git push -u origin main
}

# 2) 打包 + 建 Release（带 ZIP 与 plugins.xml 两个资产）
python3 tools/release_plugin.py --repo "$REPO"
gh release create "v$VER" \
    "dist/cropland_delineator-$VER.zip" dist/plugins.xml \
    -t "v$VER" -N "耕地标注工作台 v$VER。升级直接在 QGIS 插件管理器里点「升级」。"

echo
echo "✅ 发布完成。朋友侧插件源 URL（在 QGIS 插件管理器 → 设置 → 添加，一次即可）："
echo "   https://github.com/$REPO/releases/latest/download/plugins.xml"
