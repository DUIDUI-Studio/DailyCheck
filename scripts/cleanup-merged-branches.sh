#!/usr/bin/env bash
# 清理已合入 main 的 origin 分支（远程 + 本地跟踪分支）
#
# 安全性：
#   - 只删除 "内容已合入 origin/main" 的分支（git merge-base --is-ancestor 判定），
#     工作未进 main 的分支一个都不碰
#   - git branch -d 本身拒绝删除未合并分支，双保险
#   - 远程删除与本地删除完全独立：即使远端删除失败，也不会误删本地分支
#   - 远程删除用 git push --no-verify：pre-push hook 对"删远程分支"恒拦截
#     （需 Eric 拍板），本脚本的运行前提就是已获拍板（见 workflow.md §三）
#
# 用法：
#   scripts/cleanup-merged-branches.sh            # 逐分支确认
#   scripts/cleanup-merged-branches.sh --yes      # 全自动
#   scripts/cleanup-merged-branches.sh --dry-run  # 只打印将删什么
#   scripts/cleanup-merged-branches.sh --remote=origin
#   scripts/cleanup-merged-branches.sh --remote=origin --keep-current --dry-run
#
# 可选参数：
#   --remote=<name>  清理哪个远程（默认 origin）
#   --local-only     只删本地分支，不动远程（适用于 Gitea 无凭证的机器）
#   --keep-current   保留当前 checkout 的分支（即使已合入）
#   --dry-run        只打印，不执行
#   --yes            不逐条询问，直接删

set -euo pipefail

REMOTE="origin"
LOCAL_ONLY=0
KEEP_CURRENT=0
DRY_RUN=0
YES=0

for arg in "$@"; do
  case "$arg" in
    --remote=*)    REMOTE="${arg#*=}" ;;
    --local-only)  LOCAL_ONLY=1 ;;
    --keep-current) KEEP_CURRENT=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    --yes)         YES=1 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

BASE="$REMOTE/main"
git rev-parse --verify --quiet "$BASE" >/dev/null \
  || { echo "找不到 $BASE，请先 git fetch $REMOTE"; exit 1; }

CURRENT=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")

# 收集已合入 main 的远程分支名（去掉 $REMOTE/ 前缀）
# 兼容 macOS Bash 3.2：不用 mapfile，用 while read 累积
MERGED=()
while read -r ref; do
  name="${ref#refs/remotes/$REMOTE/}"
  if git merge-base --is-ancestor "$ref" "$BASE" 2>/dev/null; then
    MERGED+=("$name")
  fi
done < <(git for-each-ref --format='%(refname)' "refs/remotes/$REMOTE" \
           | grep -v '/HEAD$' | grep -v "/main$")

echo "== 扫描结果 =="
echo "远程: $REMOTE | 基分支: $BASE"
echo "已合入待清理: ${#MERGED[@]} 个"
echo

if [ "${#MERGED[@]}" -eq 0 ]; then
  echo "没有可清理的分支，退出。"
  exit 0
fi

del_remote=0; del_local=0; skip=0
for b in "${MERGED[@]}"; do
  # 跳过当前分支
  if [ "$KEEP_CURRENT" -eq 1 ] && [ -n "$CURRENT" ] && [ "$b" = "$CURRENT" ]; then
    echo "[跳过] $b (当前 checkout)"
    skip=$((skip+1)); continue
  fi

  if [ "$DRY_RUN" -eq 0 ] && [ "$YES" -eq 0 ]; then
    read -r -p "删除 $b ? [y/N] " ans
    case "$ans" in y|Y|yes) ;; *) echo "[跳过] $b"; skip=$((skip+1)); continue ;; esac
  fi

  echo "[删除] $b"

  # 1) 远程分支（可选）
  if [ "$LOCAL_ONLY" -eq 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "    (dry-run) git push --no-verify $REMOTE --delete $b"
    elif git push --no-verify "$REMOTE" --delete "$b"; then
      del_remote=$((del_remote+1))
    else
      echo "    远程删除失败（不影响本地清理）: $b"
    fi
  fi

  # 2) 本地跟踪分支（独立于远程结果）
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    (dry-run) git branch -d $b"
  else
    if git branch -d "$b" 2>/dev/null; then
      del_local=$((del_local+1))
    elif git show-ref --verify --quiet "refs/heads/$b"; then
      echo "    本地 $b 存在但 git branch -d 拒绝（可能不在当前 main 后），未动"
      skip=$((skip+1))
    fi
  fi
done

echo
echo "== 完成 =="
if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry-run 模式：未做任何修改。去掉 --dry-run 真正执行。"
else
  echo "远程删除: $del_remote | 本地删除: $del_local | 跳过: $skip"
fi
echo
echo "后续建议:"
echo "  git fetch $REMOTE --prune    # 同步其他机器上已失效的远程引用"
