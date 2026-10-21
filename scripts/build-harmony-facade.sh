#!/usr/bin/env bash
# build-harmony-facade.sh — Phase 3：facade HAP 内嵌 hnp，经 BMS 正规安装上机。
#
# 战况（Phase 1/2 判死后的最终路线）：
#   - XPM 拒 /data 无 dmverity ELF，sysctl 不可关（xpm_mode 0-5 全拦）。
#   - hnp CLI 直装全灭：EnforceCodeSignForApp 返回 -256 并全回滚（需 BMS profile
#     上下文；DevPython-OH 成品 python312.hnp 同样失败，判决性实验已做）。
#   - 活路 = HAP 内嵌 hnp。地面真值：设备上 gitnext 的 entry.hap（已拉回
#     build/gitnext-entry.hap 解剖）：
#       module.json 声明 hnpPackages:[{"package":"jiuwenswarm.hnp","type":"public"}]
#       zip 内 hnp/arm64-v8a/jiuwenswarm.hnp（Stored；hnp 本体不签名——git/openssh
#       均无签名条目，BMS 安装时才对 ELF 施加代码签名）
#       hnp.json links.source 无前导斜杠（git.hnp 地面真值 "bin/git"）
#     BMS 装 HAP → hnp 落 /data/app/el1/bundle/100/hnppublic/jiuwenswarm.org/
#     jiuwenswarm_1.0.0/，ELF 带内核认可签名，无需任何 root hack 即可 exec。
#
# 前置：build/hnp-workdir/jiuwenswarm.hnp（staging+pack 见 install-hnp-signed.sh [1-2/5]）
# 签名材料与 install-hnp-signed.sh [3/5] 同配方（build/harmony-sign/），本脚本可自动补齐。
# 用法：./scripts/build-harmony-facade.sh [--install]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEVECO="${DEVECO:-/d/Program Files/Huawei/DevEco Studio}"
TOOLCHAINS="$DEVECO/sdk/default/openharmony/toolchains"
HDC="$TOOLCHAINS/hdc.exe"
LIB="$TOOLCHAINS/lib"
JAVA="$DEVECO/jbr/bin/java.exe"
FACADE="$REPO/../harmony_facade"
PY="$REPO/.venv/Scripts/python.exe"

HNP_DIR="$REPO/build/hnp-workdir"
HNP_UNSIGNED="$HNP_DIR/jiuwenswarm.hnp"        # hnpcli pack 产物（未签名）
HNP_HAP="$HNP_DIR/jiuwenswarm-hap.hnp"         # links 修正版（BMS 流地面真值格式）
SIGN_DIR="$REPO/build/harmony-sign"
BUNDLE_NAME="com.jiuwenswarm.launcher"
APP_IDENTIFIER="6917581042768913572"
HNP_ROOT=/data/app/el1/bundle/100/hnppublic/jiuwenswarm.org/jiuwenswarm_1.0.0
INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

hap_sign() { "$JAVA" -jar "$LIB/hap-sign-tool.jar" "$@"; }

[ -f "$HNP_UNSIGNED" ] || { echo "缺 $HNP_UNSIGNED（先跑 install-hnp-signed.sh 的 staging+pack）" >&2; exit 2; }

echo "==> [1/5] hnp.json links 修正（gitnext 地面真值：无前导斜杠）→ $HNP_HAP"
if [ -f "$HNP_HAP" ] && [ "$HNP_UNSIGNED" -nt "$HNP_HAP" ]; then rm -f "$HNP_HAP"; fi
if [ ! -f "$HNP_HAP" ]; then
  "$PY" - "$HNP_UNSIGNED" "$HNP_HAP" <<'PYEOF'
import json, sys, zipfile
sys.stdout.reconfigure(newline="\n")
src, dst = sys.argv[1:3]
cfg = {"type": "hnp-config", "name": "jiuwenswarm", "version": "1.0.0",
       "install": {"links": [{"source": "bin/python3.12", "target": "python3.12"}]}}
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
    names = zin.namelist()
    cfgnames = [n for n in names if n.endswith("/hnp.json") and n.count("/") == 1]
    assert cfgnames, "hnp 内找不到 <顶层>/hnp.json"
    for n in names:
        info = zin.getinfo(n)
        if n == cfgnames[0]:
            zout.writestr(info, json.dumps(cfg, indent=2))
        else:
            zout.writestr(info, zin.read(n))
print("hnp.json 已按 BMS 流格式重写:", cfgnames[0])
PYEOF
else
  echo "跳过（$HNP_HAP 已是修正版）"
fi

echo "==> [2/5] 壳工程构建（ohpm + hvigorw assembleHap + cpp native）"
mkdir -p "$FACADE/entry/src/main/resources/rawfile"
cp "$REPO/scripts/harmony_entry.py" "$FACADE/entry/src/main/resources/rawfile/harmony_entry.py"
cd "$FACADE"
export PATH="$DEVECO/tools/node:$DEVECO/tools/ohpm/bin:$PATH"
# hvigor 6.x 要求 DEVECO_SDK_HOME 指向 SDK 根（内含 default/{hms,openharmony,sdk-pkg.json}，
# sdk-pkg.json 声明 26.0.0 与 hvigor-config modelVersion 对齐）；未设时报
# 00303217 "Invalid value of 'DEVECO_SDK_HOME'"（本机系统环境未设该变量）
export DEVECO_SDK_HOME="$DEVECO/sdk"
[ -d oh_modules ] && [ -d entry/oh_modules ] || ohpm install --all
./hvigorw assembleHap --mode module -p product=default -p buildMode=debug --no-daemon
# 只认 hvigor 原生产物名（entry-default-unsigned.hap）——通配 *unsigned*.hap 会捞到
# 本脚本自己注入过的 entry-default-unsigned-hnp.hap，hnp 条目逐轮叠加致体积翻倍+签名失败
UNSIGNED_HAP="$(find entry/build -name 'entry-default-unsigned.hap' | head -1)"
[ -n "$UNSIGNED_HAP" ] || { echo "未找到 unsigned HAP 产物" >&2; exit 1; }
UNSIGNED_HAP="$FACADE/$UNSIGNED_HAP"
OUT_DIR="$(dirname "$UNSIGNED_HAP")"
echo "unsigned HAP: $UNSIGNED_HAP ($(du -h "$UNSIGNED_HAP" | cut -f1))"

echo "==> [3/5] 注入 hnp/arm64-v8a/jiuwenswarm.hnp（Stored）+ 核正 module.json hnpPackages"
INJECTED_HAP="$OUT_DIR/entry-default-unsigned-hnp.hap"
"$PY" - "$UNSIGNED_HAP" "$INJECTED_HAP" "$HNP_HAP" <<'PYEOF'
import json, sys, zipfile
sys.stdout.reconfigure(newline="\n")
hap_in, hap_out, hnp_file = sys.argv[1:4]
with zipfile.ZipFile(hap_in) as z:
    names = z.namelist()
    assert "module.json" in names, "unsigned HAP 缺 module.json"
    # 防回归：绝不允许输入已含 hnp 条目（find 捞错文件/上轮产物污染时在此拦下）
    assert not any(n.startswith("hnp/") for n in names), \
        f"输入 HAP 已含 hnp 条目（{[n for n in names if n.startswith('hnp/')]})——疑似注入产物被再次当作输入"
    infos = {n: z.getinfo(n) for n in names}
    data = {n: z.read(n) for n in names}
mj = json.loads(data["module.json"])
mod = mj.setdefault("module", {})
want = [{"package": "jiuwenswarm.hnp", "type": "public"}]
if mod.get("hnpPackages") == want:
    print("module.json: hnpPackages 已由 hvigor 保留")
else:
    mod["hnpPackages"] = want
    data["module.json"] = json.dumps(mj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    print("module.json: hnpPackages 已补写（hvigor 丢弃或缺失）")
with open(hnp_file, "rb") as f:
    hnp_bytes = f.read()
info = zipfile.ZipInfo("hnp/arm64-v8a/jiuwenswarm.hnp", date_time=(2009, 1, 1, 0, 0, 0))
info.compress_type = zipfile.ZIP_STORED          # 地面真值：gitnext 的 hnp 条目为 Stored
info.external_attr = 0o100644 << 16
with zipfile.ZipFile(hap_out, "w") as z:
    for n in names:
        z.writestr(infos[n], data[n])
    z.writestr(info, hnp_bytes)
print("注入完成 →", hap_out, "(hnp %d bytes)" % len(hnp_bytes))
PYEOF

echo "==> [4/5] 签名材料核对 + sign-app（SDK 原生配方）"
mkdir -p "$SIGN_DIR"
# profile JSON（bundle-info 内 bundle-name + app-identifier；顶层 bundle-name 是旧 bug）
# 守卫：文件缺失，或 bundleName 漂移（改过 BUNDLE_NAME 变量）都必须重生成
if [ ! -f "$SIGN_DIR/debug-profile-hnp.json" ] || ! grep -q "\"$BUNDLE_NAME\"" "$SIGN_DIR/debug-profile-hnp.json"; then
  "$PY" - "$LIB/UnsgnedDebugProfileTemplate.json" "$SIGN_DIR/debug-profile-hnp.json" \
    "$BUNDLE_NAME" "$APP_IDENTIFIER" <<'PYEOF'
import json, sys, time
sys.stdout.reconfigure(newline="\n")
tpl_path, out_path, bundle, appid = sys.argv[1:5]
tpl = json.load(open(tpl_path, encoding="utf-8"))
bi = tpl.setdefault("bundle-info", {})
bi["bundle-name"] = bundle
bi["app-identifier"] = appid
# 模板 validity 固定 2021-01-13→2024-01-13（早已过期）：BMS 安装不查，AAFWK 启动查
# → 云控 10106105 "under control" + 桌面点开"应用已过期"。覆写为 now-1d → now+10y。
now = int(time.time())
tpl["validity"] = {"not-before": str(now - 86400), "not-after": str(now + 315360000)}
json.dump(tpl, open(out_path, "w", encoding="utf-8"), indent=2)
PYEOF
fi
# 拆 SDK 链（[Root CA, App CA, Profile Debug] → root-ca.pem / app-ca.pem）
if [ ! -f "$SIGN_DIR/root-ca.pem" ] || [ ! -f "$SIGN_DIR/app-ca.pem" ]; then
  awk -v d="$SIGN_DIR" '/BEGIN CERTIFICATE/{n++}
    { if (n==1) print > (d "/root-ca.pem"); else if (n==2) print > (d "/app-ca.pem") }' "$LIB/OpenHarmonyProfileDebug.pem"
fi
# p7b：json 更新过必须重签（旧 p7b 若签自 bug 版 json，bundle-info 不匹配会被 BMS 拒装）
if [ ! -f "$SIGN_DIR/debug-profile-hnp.p7b" ] || [ "$SIGN_DIR/debug-profile-hnp.json" -nt "$SIGN_DIR/debug-profile-hnp.p7b" ]; then
  rm -f "$SIGN_DIR/debug-profile-hnp.p7b"
  hap_sign sign-profile -mode localSign \
    -keyAlias "openharmony application profile debug" -keyPwd "123456" \
    -profileCertFile "$LIB/OpenHarmonyProfileDebug.pem" \
    -inFile "$SIGN_DIR/debug-profile-hnp.json" -signAlg SHA256withECDSA \
    -keystoreFile "$LIB/OpenHarmony.p12" -keystorePwd "123456" \
    -outFile "$SIGN_DIR/debug-profile-hnp.p7b"
fi
# app 证书链：叶子=模板 development-certificate + App CA + Root CA（叶子在前）
if [ ! -f "$SIGN_DIR/app-cert-chain.pem" ] || [ "$SIGN_DIR/debug-profile-hnp.json" -nt "$SIGN_DIR/app-cert-chain.pem" ]; then
  "$PY" - "$SIGN_DIR/debug-profile-hnp.json" "$SIGN_DIR" <<'PYEOF'
import json, os, sys
sys.stdout.reconfigure(newline="\n")
jpath, sdir = sys.argv[1], sys.argv[2]
devcert = json.load(open(jpath, encoding="utf-8"))["bundle-info"]["development-certificate"]
parts = [devcert] + [open(os.path.join(sdir, p)).read() for p in ("app-ca.pem", "root-ca.pem")]
with open(os.path.join(sdir, "app-cert-chain.pem"), "w") as f:
    f.write("".join(p.strip() + "\n" for p in parts))
PYEOF
fi
SIGNED_HAP="$OUT_DIR/entry-default-signed-jws.hap"
hap_sign sign-app -mode localSign \
  -keyAlias "openharmony application release" -keyPwd "123456" \
  -keystoreFile "$LIB/OpenHarmony.p12" -keystorePwd "123456" \
  -appCertFile "$SIGN_DIR/app-cert-chain.pem" -profileFile "$SIGN_DIR/debug-profile-hnp.p7b" \
  -inFile "$INJECTED_HAP" -signAlg SHA256withECDSA \
  -outFile "$SIGNED_HAP" -compatibleVersion 12
echo "✅ signed HAP: $SIGNED_HAP ($(du -h "$SIGNED_HAP" | cut -f1))"

if [ "$INSTALL" -eq 1 ]; then
  echo "==> [5/5] hdc install + BMS hnp 落地验证"
  # 预清 hnppublic 残留：卸载 app 不清 hnp（实测），同名 hnp 目录存在时
  # BMS 装 hnp 报 9568407 "installing the native package failed"
  "$HDC" shell "rm -rf /data/app/el1/bundle/100/hnppublic/jiuwenswarm.org; rm -f /data/app/el1/bundle/100/hnppublic/bin/python3.12; true"
  # hdc 本地路径怪癖（memory #12）：本地绝对路径一律被前置 hdc cwd 拼坏
  # （实测 D:\cwd\ + D:/abs → no such file）——必须 cd 产物目录传相对文件名
  ( cd "$OUT_DIR" && "$HDC" install -r "$(basename "$SIGNED_HAP")" )
  echo "等待 BMS 完成 hnp 提取/代码签名（轮询 $HNP_ROOT）…"
  found=0
  for i in $(seq 1 60); do
    if "$HDC" shell "ls $HNP_ROOT/bin/python3.12 2>/dev/null" | tr -d '\r' | grep -q python; then
      echo "第 $i 次轮询命中（$((i*2))s）"; found=1; break
    fi
    sleep 2
  done
  [ "$found" -eq 1 ] || echo "⚠ 120s 内未见 $HNP_ROOT——查 hilog: hdc shell 'hilog | grep -i hnp'"
  echo "── hnppublic 布局"
  "$HDC" shell "ls /data/app/el1/bundle/100/hnppublic/ $HNP_ROOT/ 2>&1 | head -14"
  echo "── exec 验证（BMS 签名生效则无需任何 root hack）"
  # 串首必须是命令词（exec），否则 /data 开头整串触发 MSYS 路径转换（memory #12e）
  "$HDC" shell "exec $HNP_ROOT/bin/python3.12 --version 2>&1"
else
  echo "跳过安装（--install 启用）。手动：cd $(basename "$OUT_DIR") 后 hdc install -r $(basename "$SIGNED_HAP")"
fi
