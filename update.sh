#!/usr/bin/env bash
# Авто-обновление сервера: тянет ТОЛЬКО код из GitHub, не трогая состояние/конфиг
# (paper_book.json, copy_watchlist.json, wallet_sources.json, perf_history* — живут на сервере).
cd "$(dirname "$0")" || exit 1
LOG="update.log"
CODE=('*.py' Dockerfile requirements.txt docs/index.html docker-compose.yml update.sh)

git fetch -q origin main 2>>"$LOG" || exit 0

# ВАЖНО (фикс 2026-08-03): раньше сравнивался только указатель HEAD. Если git checkout падал
# (ошибки глушились), `git reset --soft` всё равно сдвигал HEAD -> скрипт навсегда решал, что
# «изменений нет», и сервер молча оставался на старом коде (реальный случай: 3+ часа простоя).
# Теперь сравниваем РЕАЛЬНЫЕ ФАЙЛЫ на диске с origin/main — самолечится из любого состояния.
CHANGED="$(git diff --name-only origin/main -- "${CODE[@]}" 2>/dev/null)"
if [ -z "$CHANGED" ] && [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
  exit 0                                                    # реально нечего обновлять
fi

if ! git checkout -q origin/main -- "${CODE[@]}" 2>>"$LOG"; then
  echo "[update] $(date -u +%FT%TZ) ОШИБКА checkout — HEAD НЕ двигаем, повторим" >>"$LOG"
  exit 1                                                    # не двигаем HEAD -> не блокируем себя
fi
git reset -q --soft origin/main                             # сдвинуть HEAD, worktree не трогаем

if docker compose up -d --build --force-recreate >>"$LOG" 2>&1; then
  echo "[update] $(date -u +%FT%TZ) применён $(git rev-parse --short HEAD)" >>"$LOG"
else
  echo "[update] $(date -u +%FT%TZ) ОШИБКА сборки/запуска — старый контейнер работает" >>"$LOG"
  exit 1
fi
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"     # лог не растёт бесконечно
