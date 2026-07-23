#!/bin/sh
# Runs once at container startup (nginx:alpine's entrypoint sources every
# executable script in /docker-entrypoint.d/ before nginx starts). Captures
# a one-time snapshot of real resource limits/usage into a static JSON file
# so the landing page can show actual numbers without a live backend —
# there is no ongoing process, so this is "at time of creation," not live.
set -eu

OUT=/usr/share/nginx/html/container-info.json

read_first() {
  for f in "$@"; do
    if [ -r "$f" ]; then cat "$f"; return; fi
  done
}

# Converts a cgroup byte value to MB; treats "max"/empty/the v1 "no limit"
# sentinel (a value near 2^63) as unset rather than a real number.
to_mb() {
  v="${1:-}"
  if [ -z "$v" ] || [ "$v" = "max" ]; then echo "null"; return; fi
  case "$v" in
    9223372036854*) echo "null"; return ;;
  esac
  echo $((v / 1024 / 1024))
}

MEM_LIMIT_RAW=$(read_first /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes || true)
MEM_USAGE_RAW=$(read_first /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory/memory.usage_in_bytes || true)
MEM_LIMIT_MB=$(to_mb "$MEM_LIMIT_RAW")
MEM_USAGE_MB=$(to_mb "$MEM_USAGE_RAW")

CPU_COUNT=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || echo null)

CPU_QUOTA_JSON=null
CPU_MAX_LINE=$(read_first /sys/fs/cgroup/cpu.max || true)
if [ -n "${CPU_MAX_LINE:-}" ]; then
  Q=$(echo "$CPU_MAX_LINE" | awk '{print $1}')
  P=$(echo "$CPU_MAX_LINE" | awk '{print $2}')
  if [ "$Q" != "max" ] && [ -n "$P" ] && [ "$P" != "0" ]; then
    CPU_QUOTA_JSON=$(awk "BEGIN { printf \"%.2f\", $Q/$P }")
  fi
else
  Q=$(read_first /sys/fs/cgroup/cpu/cpu.cfs_quota_us || true)
  P=$(read_first /sys/fs/cgroup/cpu/cpu.cfs_period_us || true)
  if [ -n "${Q:-}" ] && [ "$Q" != "-1" ] && [ -n "${P:-}" ]; then
    CPU_QUOTA_JSON=$(awk "BEGIN { printf \"%.2f\", $Q/$P }")
  fi
fi

NGINX_VERSION=$(nginx -v 2>&1 | sed 's#^nginx version: nginx/##')
OS_NAME=$(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d'"' -f2)
CONTENT_SIZE=$(du -sh /usr/share/nginx/html 2>/dev/null | cut -f1)
CONTAINER_ID=$(hostname)
CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

cat > "$OUT" <<JSON
{
  "createdAt": "$CREATED_AT",
  "containerId": "$CONTAINER_ID",
  "os": "$OS_NAME",
  "nginxVersion": "$NGINX_VERSION",
  "cpuCount": $CPU_COUNT,
  "cpuQuotaCores": $CPU_QUOTA_JSON,
  "memoryLimitMb": $MEM_LIMIT_MB,
  "memoryUsageMb": $MEM_USAGE_MB,
  "contentSize": "$CONTENT_SIZE"
}
JSON
