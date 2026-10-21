#!/bin/sh
# 设备端启动脚本（plan §A5 手动验证用）。
# 用法（hdc shell 拿到 root 后）：
#   chmod +x /data/local/tmp/jws-payload/bin/python3.12
#   sh /data/local/tmp/jws-payload/run.sh
#
# 前置：载荷已 hdc file send 到 /data/local/tmp/jws-payload/，ELF 已自签。
set -e

ROOT=/data/local/tmp/jws-payload
PY=$ROOT/bin/python3.12

[ -x "$PY" ] || chmod +x "$PY" 2>/dev/null || true

export HOME=$ROOT/home
mkdir -p "$HOME"
export PYTHONUNBUFFERED=1
export PATH=$ROOT/bin:$PATH
export LD_LIBRARY_PATH=$ROOT/lib:$LD_LIBRARY_PATH
# 同源前端：getWsBase() 为空时 WS 走 window.location.host，5173 一站式访问
export JIUWENSWARM_RUNTIME_PLATFORM=harmony

cd "$ROOT"
exec "$PY" "$ROOT/harmony_entry.py" "$@"
