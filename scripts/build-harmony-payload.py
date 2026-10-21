#!/usr/bin/env python
"""组装鸿蒙 PC 上机载荷（plan §A2）。

输入三源，合成一份可直接 push 到设备的载荷::

    --hnp         DevPython-OH 的 python312.hnp（OHOS python 3.12 运行时
                  + 预编译 C 扩展库 numpy/pandas/cryptography/... ）
    --venv        本机已验证可跑的 Windows venv（纯 py 依赖按 RECORD 拷贝）
    --ohos-wheels pypi.ohpcd 预取的 OHOS wheel（补 HNP 缺的 Rust 扩展）

产物布局（对齐 build-harmony.sh 的 HNP 预期）::

    <out>/
      bin/python3.12 ...             # HNP 运行时（bin/lib/share，去头文件）
      lib/python3.12/site-packages/  # HNP 保留件 + venv 纯 py 闭包 + ohpcd 覆盖
      harmony_entry.py               # 仓库 scripts/harmony_entry.py 原样拷入
      MANIFEST.json

依赖合成规则
------------
1. HNP site-packages 只保留 HNP_KEEP 清单（10 个 C 扩展 + Crypto/grpc，
   含 .libs 伴随目录与 dist-info）；科学计算大件（cv2/scipy/matplotlib…）
   与散落 .pth 一律不带。
2. 纯 py 闭包：种子来自 venv 里 import 三服务入口的 sys.modules 实测，
   再经 dist-info 元数据传递展开（marker 按 OHOS 环境求值），按 RECORD
   精确拷贝（跳 *.pyd/*.so/__pycache__，错误平台 tag 不会被 import 机制
   选中，带纯 py 回退的包自动回退）。venv 里的 HNP 已提供件跳过。
3. --ohos-wheels 里的 wheel（pydantic-core/orjson/rpds-py/watchfiles/
   tiktoken/jiter…）解包覆盖，作为 HNP 缺失 C 扩展的补齐。

OHOS wheel 预取（一次性，网络可用时）::

    python scripts/build-harmony-payload.py --print-requirements > build/req.txt
    ./.venv/Scripts/python.exe -m pip download --only-binary=:all: \\
        --python-version 3.12 --implementation cp --abi cp312 \\
        --platform manylinux_2_28_aarch64 -d build/ohos-wheels \\
        -i https://artifacts-cn-beijing.volces.com/repository/pypi.ohpcd/simple \\
        -r build/req.txt

用法::

    python scripts/build-harmony-payload.py \\
        --hnp <python312.hnp> --venv .venv \\
        --ohos-wheels build/ohos-wheels --out build/hnp-workdir/payload
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import zipfile
from pathlib import Path

try:
    from packaging.markers import Marker
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    print("需要 packaging 库（用 .venv 的 python 运行本脚本）", file=sys.stderr)
    raise

# HNP zip 内的根目录名
HNP_ROOT = "python-wheel-3.12"

# ── 种子：实测启动闭包 + 懒加载但有价值的通道/A2A/MCP/解析器（dist 名）────
SEED = [
    # 实测 import 闭包（jiuwenswarm 三服务入口）
    "aiofiles", "aiohappyeyeballs", "aiohttp", "aiosignal", "alembic",
    "annotated-types", "anyio", "attrs", "cachetools", "certifi",
    "charset-normalizer", "click", "dashscope", "dingtalk-stream", "distro",
    "filelock", "frozenlist", "greenlet", "httpcore", "httpx", "httpx-sse",
    "idna", "importlib-metadata", "json-repair", "jsonschema",
    "jsonschema-specifications", "loguru", "mako", "markdown-it-py",
    "markupsafe", "mdurl", "mcp", "multidict", "oauthlib", "openai",
    "openjiuwen", "packaging", "pathable", "pdfplumber", "portalocker",
    "propcache", "pycparser", "pydantic", "pydantic-settings", "pygments",
    "pymilvus", "pysbd", "python-dateutil", "python-multipart",
    "python-socks", "pytz", "redis", "referencing", "requests",
    "requests-oauthlib", "rich", "six", "slack-bolt", "slack-sdk", "sniffio",
    "socksio", "soupsieve", "sqlalchemy", "sqlmodel", "sse-starlette",
    "starlette", "tenacity", "tqdm", "typing-extensions",
    "typing-inspection", "urllib3", "uvicorn", "h11", "websockets",
    "websocket-client", "wecom-aibot-sdk", "yarl", "zipp", "pyyaml",
    "ruamel-yaml", "python-dotenv", "jsonschema-path", "importlib-resources",
    "orjson",
    # 懒加载但需要（通道 / A2A / MCP / 文档解析 / LLM provider）
    "a2a-sdk", "json-rpc", "a2ui-agent-sdk", "fastmcp", "openapi-pydantic",
    "fastapi", "lark-oapi", "python-telegram-bot", "discord-py",
    "skillnet-ai", "google-genai", "asyncssh", "authlib", "joserfc",
    "pyjwt", "croniter", "openpyxl", "python-docx", "docx2txt",
    "trafilatura", "pypdf2", "tzdata", "anthropic", "aiologic",
    "opentelemetry-api", "opentelemetry-sdk", "opentelemetry-proto",
    # openjiuwen/extensions/observability/file_exporter.py 顶层 import
    # opentelemetry.exporter.otlp.proto.common._internal.trace_encoder（encode_spans）
    "opentelemetry-exporter-otlp-proto-common",
]

# ── HNP site-packages 保留件（前缀；目录 / .libs / dist-info 都匹配）────────
HNP_KEEP = [
    "numpy", "pandas",                              # 启动闭包必需
    "PIL", "pillow",                                # ImageParser
    "lxml",                                         # trafilatura/通道解析
    "cryptography", "bcrypt", "cffi", "_cffi_backend",  # 传输加密（cffi 的 .so 在根下）
    "greenlet", "psutil",                           # sqlalchemy 异步 / 运行时
    "Crypto", "pycryptodome",                       # pycryptodome（lark/skillnet）
    "grpc", "grpcio",                               # pymilvus 懒依赖
]

# ── HNP 已提供、venv 拷贝时跳过的 C 扩展 dist 名 ────────────────────────────
HNP_PROVIDES = [
    "numpy", "pandas", "pillow", "lxml", "cryptography", "bcrypt", "cffi",
    "greenlet", "psutil", "pycryptodome", "grpcio",
]

# ── 需 wheel 覆盖的 dist（HNP 没有的 Rust C 扩展 + pydantic 降级配套）────────
# pydantic 一并覆盖：ohpcd 只有 pydantic-core 2.33.2，配 pydantic 2.11.9
# （Requires-Dist: pydantic-core==2.33.2），venv 的 2.13.3/core 2.46.3 不能用。
# watchfiles 1.1.1 → 1.0.0 漂移（uvicorn 仅 reload 模式 import，低风险）。
WHEEL_OVERLAY = [
    "pydantic", "pydantic-core", "orjson", "rpds-py", "watchfiles",
    "tiktoken", "jiter",
]

# ── 显式排除（dev 工具 / Windows 专属 / 重型可选功能，设备端降级）───────────
EXCLUDE = {
    "pytest", "pytest-asyncio", "pytest-cov", "pytest-html", "pytest-metadata",
    "pytest-mock", "pylint", "astroid", "isort", "mypy", "mypy-extensions",
    "ruff", "uv", "build", "coverage", "codespell", "virtualenv", "fakeredis",
    "pip", "wheel",
    "pywin32", "pywin32-ctypes", "win32-setctime",
    "chromadb", "faiss-cpu", "onnxruntime", "playwright", "transformers",
    "tokenizers", "safetensors", "huggingface-hub", "hf-xet", "tree-sitter",
    "tree-sitter-bash", "mmh3", "sqlite-vec", "pypdfium2", "pyoxigraph",
    "mermaid-py", "lupa", "kubernetes",
    "librt", "graphviz", "python-pptx", "python-json-logger",
    "prometheus-client", "xlsxwriter",
    "opentelemetry-exporter-otlp-proto-grpc",
    # HNP 已提供（防止从 venv 重复拷）
    *HNP_PROVIDES,
    # ohpcd wheel 覆盖
    *WHEEL_OVERLAY,
}

# OHOS 目标环境（PEP 508 marker 求值用）
OHOS_ENV = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "aarch64",
    "os_name": "posix",
    "implementation_name": "cpython",
    "python_version": "3.12",
    "python_full_version": "3.12.0",
}

SKIP_SUFFIXES = (".pyd", ".so", ".pyc", ".pyi", ".exe", ".dll", ".dylib", ".h")
# HNP 里的 .so 是 OHOS 本体二进制，绝不能跳；只剪头文件/缓存
HNP_SKIP_SUFFIXES = (".h", ".pyc", ".exe", ".dll")
SKIP_DIR_PARTS = {"__pycache__", "tests", "test"}


def log(msg: str) -> None:
    print(f"[payload] {msg}", flush=True)


def norm(name: str) -> str:
    return str(canonicalize_name(name))


class VenvIndex:
    """venv site-packages 的 dist-info 索引：按 dist 名取元数据与文件清单。"""

    def __init__(self, site: Path):
        self.site = site
        self.by_dist: dict[str, Path] = {}
        for di in site.glob("*.dist-info"):
            name_ver = di.name[: -len(".dist-info")]
            dist = norm(name_ver.rpartition("-")[0])
            if dist not in self.by_dist:
                self.by_dist[dist] = di
        self.editable: set[str] = set()
        for pth in site.glob("__editable__*.pth"):
            self.editable.add(norm(pth.name.split(".")[1].rsplit("-", 1)[0]))

    def dist_info(self, dist: str) -> Path | None:
        return self.by_dist.get(norm(dist))

    def requires(self, dist: str) -> list[str]:
        di = self.dist_info(dist)
        if di is None:
            return []
        out = []
        for line in (di / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Requires-Dist:"):
                out.append(line.split(":", 1)[1].strip())
        return out

    def record_files(self, dist: str) -> list[str]:
        di = self.dist_info(dist)
        if di is None or not (di / "RECORD").exists():
            return []
        with (di / "RECORD").open(newline="", encoding="utf-8") as fh:
            return [row[0] for row in csv.reader(fh) if row]

    def version(self, dist: str) -> str:
        di = self.dist_info(dist)
        if di is None:
            return "?"
        return di.name[: -len(".dist-info")].rpartition("-")[2]


def expand_closure(index: VenvIndex, seeds: list[str]) -> tuple[set[str], list[str]]:
    """从种子出发，经 venv 元数据传递展开纯 py 闭包（返回闭包与降级警告）。"""
    closure: set[str] = set()
    warnings: list[tuple[str, str, str]] = []
    queue = [norm(s) for s in seeds]
    provided = {norm(d) for d in HNP_PROVIDES + WHEEL_OVERLAY}
    while queue:
        dist = queue.pop()
        if dist in closure:
            continue
        if index.dist_info(dist) is None:
            if dist in index.editable:
                continue  # workswarm：jiuwenswarm 本体从仓库拷
            closure.add(dist)
            continue
        closure.add(dist)
        for req_str in index.requires(dist):
            try:
                req = Requirement(req_str)
            except Exception:
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate(OHOS_ENV):
                        continue
                except Exception:
                    pass
            dep = norm(req.name)
            if dep in closure:
                continue
            if dep in EXCLUDE:
                if dep not in provided:
                    warnings.append((dist, dep, "excluded"))
                continue
            if index.dist_info(dep) is None and dep not in index.editable:
                warnings.append((dist, dep, "not-in-venv"))
                continue
            queue.append(dep)
    return closure, warnings


def copy_recorded(index: VenvIndex, dist: str, site_out: Path) -> int:
    """按 RECORD 拷贝一个 dist 的文件（剪掉二进制/缓存/测试）。"""
    count = 0
    for rel in index.record_files(dist):
        parts = Path(rel).parts
        if any(p in SKIP_DIR_PARTS for p in parts):
            continue
        if Path(rel).suffix in SKIP_SUFFIXES:
            continue
        src = index.site / rel
        if not src.is_file():
            continue
        dst = site_out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    return count


def extract_hnp(hnp_zip: zipfile.ZipFile, out: Path) -> tuple[str, int]:
    """从 python312.hnp 解出运行时（bin/lib/share）+ HNP_KEEP 保留件。

    返回 (python 版本目录名, 保留文件数)。lib/python3.12/site-packages 只
    解 HNP_KEEP 前缀命中的条目；include/ 与头文件不解。
    """
    names = hnp_zip.namelist()
    lib_dirs = sorted({n.split("/")[2] for n in names
                       if n.startswith(f"{HNP_ROOT}/lib/python3.") and n.count("/") > 2
                       and n.split("/")[2].startswith("python3.") and "/" in n[len(f"{HNP_ROOT}/lib/"):]})
    pyver = lib_dirs[0] if lib_dirs else "python3.12"
    site_prefix = f"{HNP_ROOT}/lib/{pyver}/site-packages/"
    site_out = out / "lib" / pyver / "site-packages"

    def keep_site(name: str) -> bool:
        rest = name[len(site_prefix):]
        top = rest.split("/")[0]
        return any(top == k or top.startswith(k + ".") or top.startswith(k + "-")
                   or top.startswith(k + "_") for k in HNP_KEEP)

    count = 0
    for info in hnp_zip.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        if name.startswith(f"{HNP_ROOT}/include/"):
            continue
        if name.startswith(site_prefix):
            if not keep_site(name):
                continue
            rel = Path(name[len(site_prefix):])
            if any(p in SKIP_DIR_PARTS for p in rel.parts):
                continue
            if rel.suffix in HNP_SKIP_SUFFIXES:
                continue
            dst = site_out / rel
        elif name.startswith(f"{HNP_ROOT}/"):
            # bin/ lib/(非 site-packages) share/
            rel = Path(name[len(f"{HNP_ROOT}/"):])
            if rel.parts[0] not in ("bin", "lib", "share"):
                continue
            if any(p in SKIP_DIR_PARTS for p in rel.parts):
                continue
            if rel.suffix in HNP_SKIP_SUFFIXES:
                continue
            dst = out / rel
        else:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        with hnp_zip.open(info) as src, open(dst, "wb") as fh:
            shutil.copyfileobj(src, fh)
        count += 1
    return pyver, count


def main() -> int:
    ap = argparse.ArgumentParser(description="组装鸿蒙上机 Python 载荷")
    ap.add_argument("--hnp", help="python312.hnp 路径（DevPython-OH 产物）")
    ap.add_argument("--venv", default=".venv", help="Windows venv 根目录")
    ap.add_argument("--ohos-wheels", help="ohpcd OHOS wheel 目录")
    ap.add_argument("--allow-missing-wheels", action="store_true",
                    help="允许 wheel 目录为空（仅测试组装管线，产物不可上机跑）")
    ap.add_argument("--out", default="build/hnp-workdir/payload", help="载荷输出目录")
    ap.add_argument("--repo", default=None, help="jiuwenswarm 仓库根（默认脚本上两级）")
    ap.add_argument("--print-requirements", action="store_true",
                    help="打印需从 ohpcd 取的 wheel 清单（版本随 venv 钉死）后退出")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]
    venv_site = Path(args.venv).resolve() / "Lib" / "site-packages"
    if not venv_site.is_dir():
        log(f"venv site-packages 不存在：{venv_site}")
        return 2
    index = VenvIndex(venv_site)
    closure, warns = expand_closure(index, SEED)

    if args.print_requirements:
        # 解析后的 pin（venv 版本 + 两处有意覆盖），与 fetch-ohos-wheels.sh 一致
        pins = {
            "pydantic": "2.11.9",        # venv 2.13.3 → 降级配 ohpcd 的 core 2.33.2
            "pydantic-core": "2.33.2",   # venv 2.46.3 → 镜像仅有 2.33.2
            "orjson": index.version("orjson"),
            "rpds-py": index.version("rpds-py"),
            "tiktoken": index.version("tiktoken"),
            "jiter": index.version("jiter"),
            "watchfiles": "1.0.0",       # venv 1.1.1 → 镜像仅有 1.0.0
        }
        for dist in sorted(pins):
            print(f"{dist}=={pins[dist]}")
        return 0

    unresolved = sorted({f"{r} -> {d} ({why})" for r, d, why in warns})
    for line in unresolved:
        log(f"依赖降级/缺失：{line}")
    missing_venv = sorted(d for d in closure if index.dist_info(d) is None)
    if missing_venv:
        log(f"闭包中 venv 缺失（将不拷贝）：{missing_venv}")

    if not args.hnp or not args.ohos_wheels:
        log("未提供 --hnp/--ohos-wheels：仅完成依赖分析（--print-requirements 可导出清单）")
        return 0

    hnp_path = Path(args.hnp).resolve()
    wheels_dir = Path(args.ohos_wheels).resolve()
    out = Path(args.out).resolve()
    wheels = list(wheels_dir.glob("*.whl")) if wheels_dir.is_dir() else []
    if not hnp_path.is_file():
        log(f"HNP 不存在：{hnp_path}")
        return 2
    if not wheels and not args.allow_missing_wheels:
        log(f"OHOS wheel 目录为空：{wheels_dir}")
        return 2

    if out.exists():
        # Windows 下杀毒/索引器/残留句柄可能瞬时占用（WinError 145/5/183）。
        # onerror 只做修复（清只读位）后重抛，绝不能吞错；删后再验存在性。
        import os as _os
        import stat as _stat
        import time as _time

        def _rm_onerror(func, path, exc):
            _os.chmod(path, _stat.S_IWRITE)
            func(path)  # 仍失败则抛出，交给外层重试

        for attempt in range(4):
            try:
                shutil.rmtree(out, onerror=_rm_onerror)
            except OSError as e:
                log(f"rmtree 异常重试 {attempt + 1}/4（{e}）")
                _time.sleep(1.5)
                continue
            if not out.exists():
                break
            log(f"rmtree 后目录仍存在，重试 {attempt + 1}/4")
            _time.sleep(1.5)
        else:
            raise RuntimeError(f"无法清空 {out}（被占用？杀毒扫描？稍后重试）")
    out.mkdir(parents=True)

    # 1. HNP 运行时 + 保留件
    log("解包 python312.hnp（运行时 + C 扩展保留件）…")
    with zipfile.ZipFile(hnp_path) as zf:
        pyver, n_hnp = extract_hnp(zf, out)
    site_packages = out / "lib" / pyver / "site-packages"
    log(f"HNP：python {pyver}，{n_hnp} 个文件")

    # 2. 纯 py 闭包（RECORD 拷贝；HNP 已提供/wheel 覆盖的跳过）
    skip_dist = {norm(d) for d in HNP_PROVIDES + WHEEL_OVERLAY}
    copied = 0
    for dist in sorted(closure):
        if dist in skip_dist or index.dist_info(dist) is None:
            continue
        copied += copy_recorded(index, dist, site_packages)
    log(f"纯 py 依赖：{len(closure)} 个 dist，{copied} 个文件")

    # 3. jiuwenswarm 本体 + 前端 dist + 入口脚本
    log("拷贝 jiuwenswarm 包 + 前端 dist …")
    for item in (repo / "jiuwenswarm").rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(repo)
        if any(p in SKIP_DIR_PARTS for p in rel.parts):
            continue
        if item.suffix in SKIP_SUFFIXES:
            continue
        if "frontend" in rel.parts and "dist" not in rel.parts:
            continue  # 前端只带 dist 产物
        dst = site_packages / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dst)
    shutil.copy2(repo / "scripts" / "harmony_entry.py", out / "harmony_entry.py")

    # 4. ohpcd wheel 覆盖（缺的 Rust C 扩展）
    overlay_names = {norm(d) for d in WHEEL_OVERLAY}
    covered: set[str] = set()
    for whl in sorted(wheels):
        dist = norm(whl.name.split("-")[0])
        if dist not in overlay_names:
            log(f"跳过非 overlay wheel：{whl.name}")
            continue
        for old in site_packages.glob(f"{dist}-*.dist-info"):
            shutil.rmtree(old, ignore_errors=True)
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(site_packages)
        covered.add(dist)
        log(f"覆盖 {dist} ← {whl.name}")
    uncovered = sorted(overlay_names - covered)
    if uncovered:
        log(f"警告：缺 OHOS wheel（设备端 import 会失败，按需补）：{uncovered}")

    # 4.5 OHOS wheel 落地修正（2026-10-20 上机踩坑，详见 memory harmony-deploy-pipeline-facts）
    #   (a) tiktoken_ext：ohpcd 的 tiktoken wheel 不含此命名空间包，而 tiktoken
    #       顶层 import tiktoken_ext —— 从 venv 补纯 py 副本
    tke_src, tke_dst = venv_site / "tiktoken_ext", site_packages / "tiktoken_ext"
    if tke_src.is_dir() and not tke_dst.is_dir():
        shutil.copytree(tke_src, tke_dst, ignore=shutil.ignore_patterns("__pycache__"))
        log("补 tiktoken_ext（ohpcd wheel 不含，拷 venv 纯 py 副本）")
    #   (b) EXT_SUFFIX：OHOS 运行时只认 .cpython-312.so（lib-dynload 命名为准），
    #       wheel 里的 .cpython-312-aarch64-linux-ohos.so finder 不认 → 改名
    for so in site_packages.rglob("*.cpython-312-aarch64-linux-ohos.so"):
        fixed = so.with_name(so.name.replace("-aarch64-linux-ohos", ""))
        so.rename(fixed)
        log(f"EXT_SUFFIX 改名：{fixed.name}")
    #   (c) binary-sign-tool selfSign 的 *-tmp-signed 残留
    for res in site_packages.rglob("*-tmp-signed"):
        res.unlink()
        log(f"清 selfSign 残留：{res.name}")

    # 5. MANIFEST
    files = [p for p in out.rglob("*") if p.is_file()]
    size_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
    manifest = {
        "python": pyver,
        "closure": sorted(closure),
        "dep_warnings": unresolved,
        "hnp_keep": HNP_KEEP,
        "wheel_overlaid": sorted(covered),
        "wheel_missing": uncovered,
        "entry": "harmony_entry.py",
        "services": {"agentserver": 18092, "gateway": 19000, "frontend": 5173},
        "files": len(files),
        "size_mb": round(size_mb, 1),
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"完成：{out}（{len(files)} 文件，{size_mb:.0f} MB）")
    log("下一步：scripts/sign-harmony-payload.sh 自签 → hdc file send → A5 验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
