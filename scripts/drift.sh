#!/usr/bin/env bash
# Сверка копий с источником правды.
#
# Пока модули стадии живут двумя копиями — здесь и в `poh-issue-agents` —
# расхождение обязано находиться прогоном, а не человеком на разборе. Правка,
# сделанная там и не доехавшая сюда, делает этот репозиторий враньём; правка,
# сделанная здесь и не доехавшая туда, не влияет ни на что.
#
# Порядок снятия долга — docs/extraction-plan.md. После шага 3 этот скрипт
# удаляется вместе с копиями.
set -euo pipefail

SOURCE_REPO="${DRIFT_SOURCE_REPO:-https://github.com/po-helper-org/poh-issue-agents}"
SOURCE_REF="${DRIFT_SOURCE_REF:-main}"
WORK="${DRIFT_WORKDIR:-.drift}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Пары «файл здесь : файл там». Разъезжаются они по одному, поэтому и
# сверяются по одному — общий diff каталогов сказал бы «отличается» и замолчал.
PAIRS=(
  "poh_developer/develop.py:shared/develop.py"
  "poh_developer/test_report.py:shared/test_report.py"
  "poh_developer/pr_closing.py:shared/pr_closing.py"
  "poh_developer/worktree.py:worker/worktree.py"
  "agent/Dockerfile:openhands/Dockerfile"
)

if [ ! -d "$WORK/.git" ]; then
  rm -rf "$WORK"
  git clone --depth 1 --branch "$SOURCE_REF" "$SOURCE_REPO" "$WORK" >/dev/null 2>&1
else
  git -C "$WORK" fetch --depth 1 origin "$SOURCE_REF" >/dev/null 2>&1
  git -C "$WORK" reset --hard "origin/$SOURCE_REF" >/dev/null 2>&1
fi

echo "источник: $SOURCE_REPO@$SOURCE_REF ($(git -C "$WORK" rev-parse --short HEAD))"
echo

drifted=0
for pair in "${PAIRS[@]}"; do
  mine="$here/${pair%%:*}"
  theirs="$WORK/${pair#*:}"

  if [ ! -f "$theirs" ]; then
    # Файла там нет — либо шаг 3 плана уже сделан, либо его переименовали.
    # Оба случая требуют человека, но означают разное.
    echo "?? ${pair#*:} — в источнике не найден"
    drifted=1
    continue
  fi

  if diff -q "$mine" "$theirs" >/dev/null; then
    echo "ok ${pair%%:*}"
  else
    echo "!! ${pair%%:*} разошёлся с ${pair#*:}"
    diff -u "$theirs" "$mine" | sed 's/^/     /' || true
    drifted=1
  fi
done

echo
if [ "$drifted" -ne 0 ]; then
  echo "копии разошлись с источником правды — см. docs/extraction-plan.md"
  exit 1
fi
echo "копии совпадают с источником"
