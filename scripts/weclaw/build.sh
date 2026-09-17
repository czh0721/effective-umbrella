#!/bin/bash
# 从上游 v0.7.1 重新构建打过念念补丁的 weclaw。
#
# 背景：v0.7.1 原生 HTTP agent 不转发消息发送者，且缺少媒体端点等能力，
# 念念依赖 scripts/weclaw/nian.patch 中的改动。若 /tmp 构建目录丢失，
# 用本脚本 + 同目录补丁可在任意机器上重建。
#
# 用法：bash build.sh [输出路径]
set -euo pipefail

VERSION="v0.7.1"
REPO="https://github.com/fastclaw-ai/weclaw.git"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/weclaw}"
SRC="${WECLAW_SRC:-/tmp/opencode/weclaw-src}"

if [ ! -d "$SRC/.git" ]; then
  git clone --branch "$VERSION" --depth 1 "$REPO" "$SRC"
fi

cd "$SRC"
git checkout -- .
git apply "$HERE/nian.patch"
cp "$HERE/zz_patch_test.go" agent/zz_patch_test.go

go build -o "$OUT" .
echo "built $OUT"
"$OUT" --version || true
