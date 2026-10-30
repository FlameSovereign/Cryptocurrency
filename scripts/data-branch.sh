#!/usr/bin/env bash
# 机器人状态和爬虫数据放在单独的 bot-data 分支（挂在 _data/ 目录），主分支只放代码和配置。
# 这样机器人每小时提交一次也不会和你改配置互相冲突，主分支历史也干净。
#
#   scripts/data-branch.sh checkout          取出 bot-data 分支到 _data/（不存在就新建空分支）
#   scripts/data-branch.sh push "提交说明"    把 _data/ 的变化提交并推送（失败自动重试）
set -euo pipefail

BRANCH="${DATA_BRANCH:-bot-data}"
DIR="${DATA_DIR:-_data}"
cmd="${1:?用法: data-branch.sh checkout | push <说明>}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

case "$cmd" in
  checkout)
    # 分清“分支不存在”（ls-remote 返回 2，第一次运行）和“网络抽风”（其他非 0）：
    # 网络抽风时绝不能当成第一次运行，否则实盘机器人会拿着空账本再买一遍
    status=1
    for i in 1 2 3; do
      git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null && status=0 || status=$?
      if [ "$status" -eq 0 ] || [ "$status" -eq 2 ]; then break; fi
      echo "查询 $BRANCH 分支失败（第 $i 次，exit $status），稍后重试" >&2
      sleep $((i * 5))
    done
    if [ "$status" -eq 0 ]; then
      for i in 1 2 3; do
        if git fetch -q --depth=1 origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"; then
          git worktree add -q -B "$BRANCH" "$DIR" "origin/$BRANCH"
          exit 0
        fi
        echo "拉取 $BRANCH 失败（第 $i 次），稍后重试" >&2
        sleep $((i * 5))
      done
      echo "拉不到 $BRANCH 分支，这次不跑（防止拿空账本交易）" >&2
      exit 1
    elif [ "$status" -eq 2 ]; then
      echo "第一次运行：新建空的 $BRANCH 分支"
      git worktree add -q --detach "$DIR"
      git -C "$DIR" checkout -q --orphan "$BRANCH"
      git -C "$DIR" rm -rfq --cached . >/dev/null || true
      git -C "$DIR" clean -fdxq
    else
      echo "连不上 GitHub 查 $BRANCH 分支，这次不跑（防止拿空账本交易）" >&2
      exit 1
    fi
    ;;
  push)
    msg="${2:-update}"
    cd "$DIR"
    git add -A
    if git diff --cached --quiet; then
      echo "没有变化，不提交"
      exit 0
    fi
    git commit -q -m "$msg $(date -u +'%Y-%m-%d %H:%M') UTC"
    for i in 1 2 3 4 5; do
      if git push -q origin "HEAD:refs/heads/$BRANCH"; then
        echo "已推送到 $BRANCH"
        exit 0
      fi
      echo "推送失败（第 $i 次），拉取最新后重试"
      sleep $((i * 5))
      # 机器人和爬虫写的是不同目录，rebase 不会冲突
      if git fetch -q --depth=50 origin "$BRANCH"; then
        git rebase -q FETCH_HEAD || { git rebase --abort; exit 1; }
      fi
    done
    exit 1
    ;;
  *)
    echo "未知命令: $cmd" >&2
    exit 2
    ;;
esac
