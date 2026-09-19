#!/bin/bash
# 把 weclaw 补丁、测试与构建脚本固化到生产服务器 /opt/nian/vendor/weclaw，
# 并备份当前线上二进制，确保 /tmp 构建目录丢失后仍可重建。
#
# 用法：bash sync_to_server.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${NIAN_HOST:-nian}"
DEST="/opt/nian/vendor/weclaw"

# 沙箱出口会临时封锁 22 端口，用重试等待窗口恢复。
for i in $(seq 1 20); do
  echo "=== attempt $i $(date +%H:%M:%S) ==="
  if ssh -o ConnectTimeout=30 "$HOST" "set -e; mkdir -p $DEST; if [ -x /opt/nian/bin/weclaw ]; then cp /opt/nian/bin/weclaw $DEST/weclaw-\$(date +%Y%m%d).bin; fi" \
     && scp -o ConnectTimeout=30 -q "$HERE/nian.patch" "$HERE/zz_patch_test.go" "$HERE/zz_voice_test.go" "$HERE/build.sh" "$HOST:$DEST/" \
     && ssh -o ConnectTimeout=30 "$HOST" "ls -la $DEST && echo WECLAW_ARCHIVED"; then
    exit 0
  fi
  sleep 30
done
echo SYNC_FAILED
exit 1
