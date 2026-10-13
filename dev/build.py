"""打包脚本（开发用，不属于插件本体）

产出两个 zip：
    1. dist/openrouterhelper-<版本>.zip
       单插件安装包，解压后得到 openrouterhelper/ 目录，可直接拷进 MoviePilot 插件目录。
    2. dist/MoviePilot-OpenRouterPlugin-repo.zip
       插件市场仓库包（package.v2.json / package.v3.json + plugins.v2 + plugins.v3 + 安装脚本 + README）。

版本号只从 package.v2.json 的 version 字段读取，避免多处手改不一致。

运行：
    "C:/Users/wangh/.workbuddy/binaries/python/envs/default/Scripts/python.exe" dev/build.py
"""

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 插件包内要带的文件（相对插件目录）；图标单独从 icons/ 取
PLUGIN_FILES = ("__init__.py", "requirements.txt")
ICON_SRC = ROOT / "icons" / "openrouterhelper.png"

# 市场仓库包要带的文件/目录（相对仓库根）
REPO_FILES = (
    "package.v2.json",
    "package.v3.json",
    "install.sh",
    "install.ps1",
    "push-to-github.sh",
    "README.md",
    ".gitattributes",
    ".gitignore",
)
REPO_DIRS = ("plugins.v2/openrouterhelper", "plugins.v3/openrouterhelper")
REPO_ICON = "icons/openrouterhelper.png"


def _add_file(zf: zipfile.ZipFile, src: Path, arcname: str):
    if not src.exists():
        raise SystemExit(f"缺少文件：{src}")
    zf.write(src, arcname)


def build_plugin(version: str) -> Path:
    out = DIST / f"openrouterhelper-{version}.zip"
    icon_arc = "openrouterhelper/openrouterhelper.png"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in PLUGIN_FILES:
            _add_file(zf, ROOT / "plugins.v2" / "openrouterhelper" / name, f"openrouterhelper/{name}")
        _add_file(zf, ICON_SRC, icon_arc)
    return out


def build_repo(version: str) -> Path:
    out = DIST / "MoviePilot-OpenRouterPlugin-repo.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in REPO_FILES:
            _add_file(zf, ROOT / name, name)
        _add_file(zf, ROOT / REPO_ICON, REPO_ICON)
        for d in REPO_DIRS:
            for src in sorted((ROOT / d).iterdir()):
                if src.is_dir() or src.name.startswith("__pycache__"):
                    continue
                _add_file(zf, src, f"{d}/{src.name}")
    return out


def main() -> int:
    manifest = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))
    version = manifest["OpenRouterHelper"]["version"]

    v3 = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))["OpenRouterHelper"]["version"]
    if v3 != version:
        print(f"警告：package.v3.json 版本 {v3} 与 v2 的 {version} 不一致")

    # 两套插件代码必须逐字节一致
    a = (ROOT / "plugins.v2/openrouterhelper/__init__.py").read_bytes()
    b = (ROOT / "plugins.v3/openrouterhelper/__init__.py").read_bytes()
    if a != b:
        print("警告：plugins.v2 与 plugins.v3 的 __init__.py 内容不一致")

    DIST.mkdir(exist_ok=True)
    # 清掉旧版本安装包，避免 dist 里堆一堆历史 zip
    for old in DIST.glob("openrouterhelper-*.zip"):
        if old.name != f"openrouterhelper-{version}.zip":
            old.unlink()

    p1 = build_plugin(version)
    p2 = build_repo(version)
    print(f"版本 {version}")
    print(f"  插件包   {p1.relative_to(ROOT)}  {p1.stat().st_size} 字节")
    print(f"  仓库包   {p2.relative_to(ROOT)}  {p2.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
