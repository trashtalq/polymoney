#!/usr/bin/env bash
# Авто-обновление сервера: тянет ТОЛЬКО код из GitHub, не трогая состояние/конфиг
# (paper_book.json, copy_watchlist.json, wallet_sources.json, perf_history* — живут на сервере).
cd "$(dirname "$0")" || exit 1
git fetch -q origin main 2>/dev/null || exit 0
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0   # нет изменений
# обновляем только файлы кода:
git checkout -q origin/main -- '*.py' Dockerfile requirements.txt docs/index.html 2>/dev/null
git reset -q --soft origin/main                                          # сдвинуть HEAD, worktree не трогаем
docker compose up -d --build --force-recreate 2>&1 | tail -2             # --force-recreate: иначе процесс не перезапускается
echo "[update] applied $(date -u +%Y-%m-%dT%H:%M) -> $(git rev-parse --short HEAD)"
