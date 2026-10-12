#!/usr/bin/env bash
# ============================================================
#  把本项目推到 GitHub
#
#  前置（一次性，需在浏览器完成，共两步）：
#    1) 添加 SSH 公钥： https://github.com/settings/ssh/new
#       把下面这条完整粘进去（Title 随便填，Key type 选 Authentication Key）：
#
#       ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMXQsYRK3VTRu0JqRqNJ63Q2P64WiitGm9LeCJjc7bBW wj-wyt@users.noreply.github.com
#
#    2) 新建空仓库： https://github.com/new
#       Repository name 填 MoviePilot-OpenRouterPlugin
#       **不要**勾选 Add README / .gitignore / license（保持空仓库，否则会冲突）
#
#  用法：
#     ./push-to-github.sh <你的GitHub用户名> [仓库名]
#     例：./push-to-github.sh wj-wyt
#         ./push-to-github.sh wj-wyt MoviePilot-OpenRouterPlugin
#
#  为什么走 SSH：本机到 github.com 的 HTTPS 被重置（TLS 层阻断），
#  但 ssh.github.com:443 可用。~/.ssh/config 已把 github.com 重定向到该端口。
# ============================================================
set -euo pipefail

GH_USER="${1:-}"
REPO_NAME="${2:-MoviePilot-OpenRouterPlugin}"

if [[ -z "$GH_USER" ]]; then
  echo "用法：./push-to-github.sh <你的GitHub用户名> [仓库名]" >&2
  exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> 1/4 检查 SSH 认证"
AUTH_OUT="$(ssh -o BatchMode=yes -T git@github.com 2>&1 || true)"
if echo "$AUTH_OUT" | grep -qi "successfully authenticated"; then
  echo "    OK：$AUTH_OUT"
elif echo "$AUTH_OUT" | grep -qi "permission denied"; then
  echo "    失败：公钥尚未添加到 GitHub 账号。" >&2
  echo "    请打开 https://github.com/settings/ssh/new 添加下面这把公钥后重跑：" >&2
  echo >&2
  cat ~/.ssh/id_ed25519.pub >&2
  exit 1
else
  echo "    失败：$AUTH_OUT" >&2
  echo "    若为连接问题，请确认网络可访问 ssh.github.com:443。" >&2
  exit 1
fi

echo "==> 2/4 配置 remote"
REMOTE_URL="git@github.com:${GH_USER}/${REPO_NAME}.git"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi
echo "    origin -> $REMOTE_URL"

echo "==> 3/4 探测远程仓库"
if git ls-remote --exit-code origin >/dev/null 2>&1; then
  echo "    远程仓库已存在"
  read -r -p "    远程可能有提交，是否强制覆盖？(y/N) " ans
  if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
    git push -u origin main --force
  else
    git pull --rebase origin main 2>/dev/null || true
    git push -u origin main
  fi
else
  echo "    远程仓库为空或不可访问，尝试直接推送…"
  git push -u origin main
fi

echo "==> 4/4 完成"
echo "    仓库地址：https://github.com/${GH_USER}/${REPO_NAME}"
echo
echo "接下来（可选）：在 MoviePilot 里把它加成插件市场"
echo "    设置 → 插件 → 插件市场 → 右上角齿轮 → 添加仓库"
echo "    https://github.com/${GH_USER}/${REPO_NAME}"
