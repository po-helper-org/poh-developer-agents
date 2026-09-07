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
  "poh_developer/task_context.py:shared/task_context.py"
)

# Dockerfile образа НЕ сверяется побайтово, и это не упущение.
#
# С #1 направление истины у него перевёрнуто: харнесс собирает раннера из
# `agent/Dockerfile` ЗДЕСЬ, а `openhands/Dockerfile` там — устаревшая копия,
# помеченная шапкой и ждущая удаления на #2. Побайтовое равенство означало бы
# требование не ставить эту шапку.
#
# Но инвариант, ради которого сверка и была (R9), пока жив: до удаления копии
# оба образа обязаны сходиться в uid раннера и мажоре Node — их сверяют тесты
# в `poh-issue-agents`. Поэтому сверяются ЗНАЧЕНИЯ, а не байты.
IMAGE_INVARIANTS=(
  "useradd -u:useradd\\s+-m\\s+-u\\s+([0-9]+)"
  "Node major:deb\\.nodesource\\.com/setup_([0-9]+)\\.x"
)

# Рабочий клон источника. Обновление идёт через FETCH_HEAD, а НЕ через
# `origin/<ref>`: клон поверхностный и однобранчевый, remote-tracking ref на
# произвольный DRIFT_SOURCE_REF в нём не заводится — и `reset --hard
# origin/<ref>` падает на первой же сверке против другой ветки. FETCH_HEAD —
# это ровно то, что мы только что забрали, чем бы оно ни было.
#
# Вывод git подавляется только на stdout: заглушенный stderr прятал отказ, а
# `set -e` убивал скрипт без единой строки о причине.
if [ ! -d "$WORK/.git" ]; then
  rm -rf "$WORK"
  git clone --depth 1 --branch "$SOURCE_REF" "$SOURCE_REPO" "$WORK" >/dev/null
else
  git -C "$WORK" fetch --depth 1 origin "$SOURCE_REF" >/dev/null
  git -C "$WORK" reset --hard FETCH_HEAD >/dev/null
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

# Инварианты образа: значения, а не байты (см. IMAGE_INVARIANTS выше).
mine="$here/agent/Dockerfile"
theirs="$WORK/openhands/Dockerfile"
if [ -f "$theirs" ]; then
  for entry in "${IMAGE_INVARIANTS[@]}"; do
    label="${entry%%:*}"
    pattern="${entry#*:}"
    # Разделитель `|`, а не `/`: в регексах есть слэши (URL nodesource).
    a=$(sed -nE "s|.*${pattern}.*|\1|p" "$mine"   | head -1)
    b=$(sed -nE "s|.*${pattern}.*|\1|p" "$theirs" | head -1)
    if [ -z "$a" ] || [ -z "$b" ]; then
      echo "?? образ: '$label' не найден (здесь='${a:-—}', там='${b:-—}')"
      drifted=1
    elif [ "$a" = "$b" ]; then
      echo "ok образ: $label = $a"
    else
      echo "!! образ: $label разошёлся — здесь $a, там $b"
      drifted=1
    fi
  done
else
  # Копия удалена (#2 сделан) — сверять больше нечего и не нужно.
  echo "ok образ: openhands/Dockerfile в источнике отсутствует — копия удалена"
fi

echo
if [ "$drifted" -ne 0 ]; then
  echo "копии разошлись с источником правды — см. docs/extraction-plan.md"
  exit 1
fi
echo "копии совпадают с источником"
