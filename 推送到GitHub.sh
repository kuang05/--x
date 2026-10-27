#!/usr/bin/env bash
# 一键推送到 GitHub。前提：目标仓库已在 GitHub 上创建（空仓库即可）。
#
# 用法：
#   ./推送到GitHub.sh kuang05 aepcb
#   或：GITHUB_REPO=git@github.com:kuang05/aepcb.git ./推送到GitHub.sh
#
# 说明：本脚本只做「配置 remote → 推 main → 推标签」，不会创建仓库。
set -euo pipefail
cd "$(dirname "$0")"

KEY="${HOME}/.ssh/deploy_key"
if [ ! -f "$KEY" ]; then
  echo "缺少私钥：$KEY" >&2
  exit 1
fi
chmod 600 "$KEY"

if [ -n "${GITHUB_REPO:-}" ]; then
  REMOTE="$GITHUB_REPO"
else
  USER_NAME="${1:?用法: $0 <GitHub用户名> <仓库名>}"
  REPO="${2:?用法: $0 <GitHub用户名> <仓库名>}"
  REMOTE="git@github.com:${USER_NAME}/${REPO}.git"
fi

export GIT_SSH_COMMAND="ssh -i ${KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"

echo "== 1/4 认证测试 =="
ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -T git@github.com 2>&1 | head -1 || true

echo "== 2/4 检查远端仓库 =="
if ! git ls-remote "$REMOTE" HEAD >/dev/null 2>&1; then
  echo "远端不可用：$REMOTE" >&2
  echo "请先在 GitHub 网页创建空仓库（不要勾选 README/.gitignore），再重跑本脚本。" >&2
  exit 2
fi

echo "== 3/4 配置 remote 与分支 =="
git remote remove origin >/dev/null 2>&1 || true
git remote add origin "$REMOTE"
git branch -M main

echo "== 4/4 推送 main 与标签 =="
git push -u origin main
git push origin --tags

echo "完成：$REMOTE"
git log --oneline -1
