#!/usr/bin/env bash
# fetch-ohos-wheels.sh — 预取 HNP 里没有的 Rust C 扩展 OHOS wheel。
#
# 事实定稿（2026-10 实测）：
#   - ohpcd 镜像（volces.com）是 OHOS 专编索引，平台 tag 是 ohos_aarch64
#     （非 manylinux），且不含纯 py 包；纯 py wheel 走清华镜像
#   - jiter 只有 cp39-abi3 wheel——pip download 的 tag 匹配接受不了它，
#     所以全部改用 curl 直连索引页提取 URL 下载，绕开 pip
#   - pydantic-core 镜像只有 2.33.2（venv 钉的是 2.46.3）：改用
#     pydantic 2.11.9（恰好要求 core==2.33.2）配套降级，纯 py 从清华拉
#   - watchfiles 1.1.1 → 1.0.0 漂移：uvicorn 仅 reload 模式 import，低风险
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/build/ohos-wheels"
OHOSD="https://artifacts-cn-beijing.volces.com/repository/pypi.ohpcd/simple"
TUNA="https://pypi.tuna.tsinghua.edu.cn/simple"
mkdir -p "$OUT"

# dist 名 → 拉取规格：index:镜像  ver:版本  match:文件名匹配模式
# （dist 名用 PyPI 规范形式，文件名用 wheel 实际下划线形式）
fetch_spec() {
  case "$1" in
    pydantic-core) echo "ohosd|2.33.2|pydantic_core-2.33.2-cp312-cp312-ohos_aarch64.whl";;
    pydantic)      echo "tuna|2.11.9|pydantic-2.11.9-py3-none-any.whl";;
    orjson)        echo "ohosd|3.11.8|orjson-3.11.8-cp312-cp312-ohos_aarch64.whl";;
    rpds-py)       echo "ohosd|0.30.0|rpds_py-0.30.0-cp312-cp312-ohos_aarch64.whl";;
    tiktoken)      echo "ohosd|0.12.0|tiktoken-0.12.0-cp312-cp312-ohos_aarch64.whl";;
    jiter)         echo "ohosd|0.14.0|jiter-0.14.0-cp39-abi3-ohos_aarch64.whl";;
    watchfiles)    echo "ohosd|1.0.0|watchfiles-1.0.0-cp312-cp312-ohos_aarch64.whl";;
    *) return 1;;
  esac
}

# 从 simple 索引页提取指定 wheel 的相对 URL 并下载
fetch_one() {
  local dist="$1" idx="$2" ver="$3" fname="$4" base out_f url
  base="$OHOSD"; [ "$idx" = tuna ] && base="$TUNA"
  out_f="$OUT/$fname"
  [ -s "$out_f" ] && { echo "  已存在：$fname"; return 0; }
  # href 形如 "../../packages/<pkg>/<file>.whl#sha256=..."——文件名后跟 #sha256
  url="$(curl -sL --max-time 30 "$base/$dist/" | grep -oE "href=\"[^\"]*$fname" | head -1 | sed 's/href="//')"
  [ -n "$url" ] || { echo "  ✗ $dist：索引页找不到 $fname" >&2; return 1; }
  url="${url%%#*}"
  case "$url" in
    ../*) url="$base/../${url#../../}";;  # 索引页在 <root>/simple/<pkg>/ 下
    http*) ;;
    /*) url="$base$url";;
  esac
  curl -sL --max-time 300 "$url" -o "$out_f"
  [ -s "$out_f" ] || { echo "  ✗ $dist：下载为空" >&2; return 1; }
  echo "  ✓ $fname ($(du -h "$out_f" | cut -f1))"
}

echo "==> 预取 OHOS wheel（7 个：6 个 Rust 扩展 + pydantic 降级配套）"
fail=0
for dist in pydantic-core pydantic orjson rpds-py tiktoken jiter watchfiles; do
  IFS='|' read -r idx ver fname <<< "$(fetch_spec "$dist")"
  fetch_one "$dist" "$idx" "$ver" "$fname" || fail=1
done
ls -lh "$OUT"
[ "$fail" = 0 ] || { echo "存在拉取失败项（见上）" >&2; exit 1; }
echo "全部就位。"
