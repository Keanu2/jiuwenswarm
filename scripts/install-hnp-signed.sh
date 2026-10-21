#!/usr/bin/env bash
# install-hnp-signed.sh — Phase 2：载荷打包为独立签名 .hnp，经设备端 hnp install 上机。
# 背景：内核 XPM 拒绝 exec /data 上"无 dmverity 保护"的 ELF（xpm_mode 0-5 全拦）；
# hnp install 走 BMS/hnp 服务的正规安装通道，会为装进去的 ELF 落内核认可的代码签名。
# 签名材料（已验证打通）：SDK 官方调试材料——设备 trusted_apps_sources.json 的
# "OpenHarmony apps" 信任源收录了整套链（app-signing-cert / issuer-ca / profile-debug
# 全在内），直接用 OpenHarmony.p12 的官方私钥签名，无需自建证书。
# 用法：bash scripts/install-hnp-signed.sh [appIdentifier]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"   # hdc file send 的本地相对路径据此解析
DEVECO="${DEVECO:-/d/Program Files/Huawei/DevEco Studio}"
JAVA="$DEVECO/jbr/bin/java.exe"
LIB="$DEVECO/sdk/default/openharmony/toolchains/lib"
HNPCLI="$DEVECO/sdk/default/openharmony/toolchains/hnpcli.exe"
HDC="$DEVECO/sdk/default/openharmony/toolchains/hdc.exe"
PY="$REPO/.venv/Scripts/python.exe"

PAYLOAD="$REPO/build/hnp-workdir/payload"
HNP_DIR="$REPO/build/hnp-workdir"
SIGN_DIR="$REPO/build/harmony-sign"
BUNDLE_NAME="com.openjiuwen.harmony_facade"
APP_IDENTIFIER="${1:-6901234567890123456}"
DEVICE_TMP=/data/local/tmp

hap_sign() { "$JAVA" -jar "$LIB/hap-sign-tool.jar" "$@"; }

# hdc file send 怪癖封装（同 harmony-deploy-driver.sh 的 dsend）
dsend() {
  local l dir rf out found
  dir="$(dirname "$1")"; l="$(basename "$1")"
  rf="$2$l"
  out="$(cd "$dir" && MSYS_NO_PATHCONV=1 "$HDC" file send "$l" "$2" 2>&1)"
  case "$out" in *"[Fail]"*) echo "$out" >&2; return 1;; esac
  found="$("$HDC" shell "find '$2' -name '$l' -type f 2>/dev/null | head -1" | tr -d '\r')"
  [ -n "$found" ] || { echo "设备上找不到 $l" >&2; return 1; }
  if [ "$found" != "$rf" ]; then "$HDC" shell "mv '$found' '$rf'" >/dev/null 2>&1; fi
  echo "$out" | tail -1
}

echo "==> [1/5] staging：载荷拷入 <名>/ 顶层目录 + 写 hnp.json（links 供 PATH 命令）"
# 结构对照已知可安装的 python312.hnp（DevPython-OH）：
#   - zip 内所有条目位于唯一顶层目录下（basename(pack -i 的目录)）——故 staging 目录名=jiuwenswarm
#   - hnp.json 的 links.source 必须带前导斜杠（/bin/...，包内绝对路径）——
#     实测无斜杠时设备端报 hnp_installer.c:1030 "hnp path invalid"
#   - 根部不放 MANIFEST.json 等杂物
STAGE="$HNP_DIR/stage/jiuwenswarm"
if [ -f "$STAGE/hnp.json" ] && [ -d "$STAGE/bin" ]; then
  echo "跳过 staging（$STAGE 已就绪）"
else
  rm -rf "$HNP_DIR/stage"; mkdir -p "$STAGE"
  tar -C "$PAYLOAD" --exclude=MANIFEST.json -cf - . | tar -C "$STAGE" -xf -
fi
cat > "$STAGE/hnp.json" <<'EOF'
{
  "type": "hnp-config",
  "name": "jiuwenswarm",
  "version": "1.0.0",
  "install": {
    "links": [
      { "source": "/bin/python3.12", "target": "python3.12" }
    ]
  }
}
EOF

echo "==> [2/5] hnpcli pack（583MB 源 → zip，需几分钟）"
HNP_UNSIGNED="$HNP_DIR/jiuwenswarm.hnp"
if [ -f "$HNP_UNSIGNED" ]; then
  echo "跳过 pack（$HNP_UNSIGNED 已存在，$(du -h "$HNP_UNSIGNED" | cut -f1)）"
else
  "$HNPCLI" pack -i "$STAGE" -o "$HNP_DIR" || { echo "hnpcli pack 失败" >&2; exit 1; }
fi
HNP_UNSIGNED="$(ls -t "$HNP_DIR"/jiuwenswarm*.hnp 2>/dev/null | grep -v signed | head -1 || true)"
[ -n "$HNP_UNSIGNED" ] && [ -f "$HNP_UNSIGNED" ] || { echo "打包产物缺失" >&2; exit 1; }
echo "未签名 hnp：$HNP_UNSIGNED ($(du -h "$HNP_UNSIGNED" | cut -f1))"

echo "==> [3/5] 调试签名材料（SDK 官方材料，与设备信任源 "OpenHarmony apps" 全对齐）"
mkdir -p "$SIGN_DIR"
# 设备 /system/etc/security/trusted_apps_sources.json 实测收录 "OpenHarmony apps" 信任源：
#   app-signing-cert=CN=OpenHarmony Application Release（即模板内嵌 development-certificate，
#   公钥与 keystore "openharmony application release" 私钥一致，实测核对）
#   issuer-ca=OpenHarmony Application CA；profile-debug-signing-cert=OpenHarmony Application Profile Debug
# 故不自建证书——直接用 SDK 私钥签名，链路完全落在设备信任源内。
# API 26 别名实测：OpenHarmony.p12 密码/条目密码均 123456，hap-sign-tool 需显式 -keyPwd。
# 拆 SDK 链文件（顺序 [Root CA, App CA, Profile Debug]）→ root-ca.pem / app-ca.pem
if [ ! -f "$SIGN_DIR/root-ca.pem" ] || [ ! -f "$SIGN_DIR/app-ca.pem" ]; then
  awk -v d="$SIGN_DIR" '/BEGIN CERTIFICATE/{n++}
    { if (n==1) print > (d "/root-ca.pem"); else if (n==2) print > (d "/app-ca.pem") }' "$LIB/OpenHarmonyProfileDebug.pem"
fi
# profile：模板 + bundle-info.bundle-name + bundle-info.app-identifier
# （顶层 bundle-name 是旧脚本的 bug——模板/设备侧结构都是 bundle-info 内）
"$PY" - "$LIB/UnsgnedDebugProfileTemplate.json" "$SIGN_DIR/debug-profile-hnp.json" \
  "$BUNDLE_NAME" "$APP_IDENTIFIER" <<'PYEOF'
import json, sys
tpl_path, out_path, bundle, appid = sys.argv[1:5]
tpl = json.load(open(tpl_path, encoding="utf-8"))
bi = tpl.setdefault("bundle-info", {})
bi["bundle-name"] = bundle
bi["app-identifier"] = appid   # hnp install -I 必须与之一致
json.dump(tpl, open(out_path, "w", encoding="utf-8"), indent=2)
PYEOF
[ -f "$SIGN_DIR/debug-profile-hnp.p7b" ] || hap_sign sign-profile \
  -mode localSign -keyAlias "openharmony application profile debug" -keyPwd "123456" \
  -profileCertFile "$LIB/OpenHarmonyProfileDebug.pem" \
  -inFile "$SIGN_DIR/debug-profile-hnp.json" -signAlg SHA256withECDSA \
  -keystoreFile "$LIB/OpenHarmony.p12" -keystorePwd "123456" \
  -outFile "$SIGN_DIR/debug-profile-hnp.p7b"
# app 证书链（sign-app 要求链文件）：叶子=模板内嵌 development-certificate（OpenHarmony
# Application Release，App CA 签发），后接 App CA、Root CA——叶子在前
"$PY" - "$SIGN_DIR/debug-profile-hnp.json" "$SIGN_DIR" <<'PYEOF'
import json, os, sys
jpath, sdir = sys.argv[1], sys.argv[2]
devcert = json.load(open(jpath, encoding="utf-8"))["bundle-info"]["development-certificate"]
parts = [devcert] + [open(os.path.join(sdir, p)).read() for p in ("app-ca.pem", "root-ca.pem")]
with open(os.path.join(sdir, "app-cert-chain.pem"), "w") as f:
    f.write("".join(p.strip() + "\n" for p in parts))
PYEOF

echo "==> [4/5] hap-sign-tool sign-app 签 .hnp（SDK release 私钥）"
HNP_SIGNED="$HNP_DIR/jiuwenswarm-signed.hnp"
if [ ! -f "$HNP_SIGNED" ] || [ "$HNP_UNSIGNED" -nt "$HNP_SIGNED" ]; then
  hap_sign sign-app \
    -mode localSign -keyAlias "openharmony application release" -keyPwd "123456" \
    -keystoreFile "$LIB/OpenHarmony.p12" -keystorePwd "123456" \
    -appCertFile "$SIGN_DIR/app-cert-chain.pem" -profileFile "$SIGN_DIR/debug-profile-hnp.p7b" \
    -inFile "$HNP_UNSIGNED" -signAlg SHA256withECDSA \
    -outFile "$HNP_SIGNED" -compatibleVersion 12
fi
echo "已签名 hnp：$HNP_SIGNED ($(du -h "$HNP_SIGNED" | cut -f1))"

echo "==> [5/5] 推设备(rootpath 下) + hnp install（-S 传相对路径）"
# RE 破案（hnp-binary.bin 反汇编，-S case @0x100e8）：-S 的值经
#   snprintf_s("%s/%s", rootpath(-i), optarg) → realpath() 校验
#   → 失败即 hnp_installer.c:1030 "hnp path invalid" ret 34
# 故 -S 只能是【相对 -i 的相对路径且不含 ..】；绝对路径会被拼成
# <root>//data/... 废路径。且 rootpath(-i) 必须非空（0x10778 校验）。
# hnp 内部常量 /data/app/el1/bundle/%d/hnp/%s —— -u 100 → bundle/100
ROOTPATH=/data/app/el1/bundle/100
dsend "$HNP_SIGNED" "$ROOTPATH/"
"$HDC" shell "hnp install -u 100 -p jiuwenswarm -i $ROOTPATH -a arm64 -I $APP_IDENTIFIER -S jiuwenswarm-signed.hnp" 2>&1 | tee "$SIGN_DIR/hnp-install.log"
echo
echo "安装后设备侧状态："
"$HDC" shell "ls -la $ROOTPATH/hnp/ $ROOTPATH/hnppublic/ 2>&1; find $ROOTPATH/hnp -maxdepth 4 -name 'python3.12' 2>/dev/null | head -3" | cat
