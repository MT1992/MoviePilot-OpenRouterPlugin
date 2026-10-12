#!/usr/bin/env bash
# ============================================================
#  OpenRouter 免费模型助手 —— MoviePilot 插件一键安装脚本
#  适用：Docker / NAS（群晖、威联通、绿联、Unraid 等）/ Linux 源码部署
#
#  用法：
#     ./install.sh                  # 默认容器名 moviepilot
#     ./install.sh my-mp            # 指定容器名
#     ./install.sh --local /path/to/MoviePilot   # 源码/CLI 部署，不用 Docker
#
#  可重复执行（幂等）：重复运行＝覆盖安装 / 升级
# ============================================================
set -euo pipefail

PLUGIN_ID="OpenRouterHelper"
PLUGIN_DIR_NAME="openrouterhelper"     # 必须是插件类名的小写形式
ICON_NAME="openrouterhelper.png"

# 容器内的真实加载路径（源码依据：PluginHelper.install 写入
#   Path(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()）
CONTAINER_PLUGINS="/app/app/plugins"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- 参数解析 ----------
MODE="docker"
CONTAINER="moviepilot"
LOCAL_ROOT=""

if [[ $# -gt 0 ]]; then
  case "$1" in
    --local)
      MODE="local"
      LOCAL_ROOT="${2:-}"
      if [[ -z "$LOCAL_ROOT" ]]; then
        echo "错误：--local 需要指定 MoviePilot 源码根目录" >&2
        exit 1
      fi
      ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      CONTAINER="$1"
      ;;
  esac
fi

# ---------- 定位插件源（自动适配 v2 / v3 目录结构） ----------
SRC=""
for cand in "plugins.v2/$PLUGIN_DIR_NAME" "plugins.v3/$PLUGIN_DIR_NAME" "plugins/$PLUGIN_DIR_NAME"; do
  if [[ -f "$SCRIPT_DIR/$cand/__init__.py" ]]; then
    SRC="$SCRIPT_DIR/$cand"
    break
  fi
done
# 也支持直接被拷成单目录后运行
if [[ -z "$SRC" && -f "$SCRIPT_DIR/$PLUGIN_DIR_NAME/__init__.py" ]]; then
  SRC="$SCRIPT_DIR/$PLUGIN_DIR_NAME"
fi
if [[ -z "$SRC" ]]; then
  echo "错误：在 $SCRIPT_DIR 下找不到 $PLUGIN_DIR_NAME/__init__.py" >&2
  echo "请把本脚本与插件目录放在一起，或直接在插件目录内运行。" >&2
  exit 1
fi

ICON="$SCRIPT_DIR/icons/$ICON_NAME"
echo "==> 插件源目录：$SRC"

verify_in_container() {
  local c="$1"
  if docker exec "$c" test -f "$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME/__init__.py" 2>/dev/null; then
    echo "==> 校验通过：$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME/__init__.py 已存在"
  else
    echo "警告：写入后未在容器内找到 __init__.py，请手动确认路径。" >&2
  fi
}

install_local() {
  local root="$1"
  local target="$root/app/plugins/$PLUGIN_DIR_NAME"
  if [[ ! -d "$root/app" ]]; then
    echo "错误：$root 看起来不是 MoviePilot 源码根目录（缺少 app/ 目录）" >&2
    exit 1
  fi
  mkdir -p "$root/app/plugins"
  rm -rf "$target"
  cp -r "$SRC" "$target"
  rm -rf "$target/__pycache__"
  # 图标两处都放，兼容不同版本取图逻辑
  mkdir -p "$root/app/plugins/icons"
  [[ -f "$ICON" ]] && cp -f "$ICON" "$root/app/plugins/icons/$ICON_NAME"
  [[ -f "$ICON" ]] && cp -f "$ICON" "$target/$ICON_NAME"
  echo "==> 已安装到：$target"
  echo "==> 请重启 MoviePilot 服务后到 [设置 → 插件] 中启用「OpenRouter 免费模型助手」"
}

install_docker() {
  local c="$1"

  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未找到 docker 命令。若为源码部署请用：./install.sh --local /path/to/MoviePilot" >&2
    exit 1
  fi
  if ! docker inspect "$c" >/dev/null 2>&1; then
    echo "错误：容器 [$c] 不存在。用 docker ps 查看实际名称。" >&2
    exit 1
  fi
  local running
  running="$(docker inspect -f '{{.State.Running}}' "$c")"
  echo "==> 目标容器：$c（running=$running）"

  # 备份已有版本（可回滚）
  if docker exec "$c" test -d "$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME" 2>/dev/null; then
    local bak="$CONTAINER_PLUGINS/${PLUGIN_DIR_NAME}.bak.$(date +%Y%m%d%H%M%S)"
    echo "==> 检测到已安装，先备份到：$bak"
    docker exec "$c" sh -c "rm -rf '$bak'; cp -r '$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME' '$bak'" || true
    docker exec "$c" rm -rf "$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME"
  fi

  # 确保目标目录存在
  docker exec "$c" mkdir -p "$CONTAINER_PLUGINS" "$CONTAINER_PLUGINS/icons"

  # 拷贝插件代码
  docker cp "$SRC" "$c:$CONTAINER_PLUGINS/"
  docker exec "$c" rm -rf "$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME/__pycache__" || true

  # 图标：公共 icons 目录 + 插件目录内，两处都放
  if [[ -f "$ICON" ]]; then
    docker cp "$ICON" "$c:$CONTAINER_PLUGINS/icons/$ICON_NAME"
    docker cp "$ICON" "$c:$CONTAINER_PLUGINS/$PLUGIN_DIR_NAME/$ICON_NAME"
  fi

  verify_in_container "$c"

  echo "==> 重启容器 $c ..."
  docker restart "$c" >/dev/null
  echo "==> 完成。请稍等 20~60 秒待服务启动，然后："
  echo "    1) 打开 MoviePilot → 设置 → 插件，搜索「OpenRouter 免费模型助手」"
  echo "    2) 填入 OpenRouter API Key，打开「启用插件」并保存"
  echo "    3) 打开「仅显示支持工具调用」（默认已开），选择模型后保存"
  echo "    4) 如需重启后仍生效，再打开「写入 app.env 持久化」并保存一次"
  echo
  echo "    注意：升级 MoviePilot 镜像后 /app 会被重建，插件会消失，重跑本脚本即可。"
}

if [[ "$MODE" == "local" ]]; then
  install_local "$LOCAL_ROOT"
else
  install_docker "$CONTAINER"
fi
