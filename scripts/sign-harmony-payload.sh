#!/usr/bin/env bash
# 对载荷里的 ELF（python 解释器 + 所有 .so）做鸿蒙代码签名（plan §A4）。
# binary-sign-tool 的 selfSign 模式免证书；签完全设备可执行（首次有允许弹窗）。
set -euo pipefail

DEVECO="${DEVECO:-/d/Program Files/Huawei/DevEco Studio}"
JAVA="$DEVECO/jbr/bin/java.exe"
SIGN_JAR="$DEVECO/sdk/default/openharmony/toolchains/lib/binary-sign-tool.jar"
PAYLOAD="${1:-build/hnp-workdir/payload}"

[ -x "$JAVA" ] || { echo "java 不存在：$JAVA" >&2; exit 2; }
[ -f "$SIGN_JAR" ] || { echo "binary-sign-tool.jar 不存在：$SIGN_JAR" >&2; exit 2; }
[ -d "$PAYLOAD" ] || { echo "载荷目录不存在：$PAYLOAD" >&2; exit 2; }

# 收集待签 ELF：bin/ 下仅 ELF 魔数文件（bin 里多为 python shebang 脚本存根，
# sign 工具拒签非 ELF）+ 全部 .so（含 .so.N 版本化命名）
PY="$(cd "$(dirname "$0")/.." && pwd)/.venv/Scripts/python.exe"
# python 在 Windows 向管道 print 会输出 \r\n，mapfile -t 只剥 \n——
# 残留的 \r 会粘在路径尾部让 java 找不到文件（日志里不可见），必须 tr 掉
mapfile -t targets < <("$PY" - "$PAYLOAD" <<'PYEOF' | tr -d '\r'
import os, sys

root = sys.argv[1]
bindir = os.path.join(root, "bin")
if os.path.isdir(bindir):
    for n in sorted(os.listdir(bindir)):
        p = os.path.join(bindir, n)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        # 必须输出正斜杠：Windows 反斜杠路径经 xargs/bash 传给
                        # native java.exe 时会被 MSYS 路径转换当转义符吃掉
                        print(p.replace("\\", "/"))
            except OSError:
                pass
for dirpath, _, files in os.walk(root):
    for n in sorted(files):
        if n.endswith(".so") or ".so." in n:
            print(os.path.join(dirpath, n).replace("\\", "/"))
PYEOF
)
[ ${#targets[@]} -gt 0 ] || { echo "没找到可签的 ELF" >&2; exit 2; }

echo "共 ${#targets[@]} 个 ELF，开始自签（8 并发）…"
sign_one() {
  local f="$1"
  if "$JAVA" -jar "$SIGN_JAR" sign -inFile "$f" -outFile "$f" -selfSign "1" >/dev/null 2>&1; then
    echo "  signed: $f"
  else
    echo "  FAILED: $f" >&2
    return 1
  fi
}
export -f sign_one
export JAVA SIGN_JAR

printf '%s\n' "${targets[@]}" | xargs -P 8 -I{} bash -c 'sign_one "$@"' _ {} \
  || { echo "存在签名失败项（见上）" >&2; exit 1; }
echo "全部完成。"
