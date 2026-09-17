#!/bin/bash
# 本机自愈巡检：连续 3 次 /health 失败就重启 nian 服务。
# 由 nian-healthcheck.timer 每分钟调用，独立于应用进程运行。
set -u

URL="http://127.0.0.1:8000/health"
STATE="/run/nian-health.fail"
THRESHOLD=3

if curl -fsS -m 8 "$URL" >/dev/null 2>&1; then
  echo 0 > "$STATE" 2>/dev/null || true
  exit 0
fi

fails=$(cat "$STATE" 2>/dev/null || echo 0)
case "$fails" in
  ''|*[!0-9]*) fails=0 ;;
esac
fails=$((fails + 1))
echo "$fails" > "$STATE" 2>/dev/null || true
logger -t nian-health "health check failed (${fails}/${THRESHOLD})"

if [ "$fails" -ge "$THRESHOLD" ]; then
  logger -t nian-health "restarting nian after ${fails} consecutive failures"
  systemctl restart nian
  echo 0 > "$STATE" 2>/dev/null || true
fi
