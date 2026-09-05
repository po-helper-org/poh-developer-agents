"""Контракт активности Develop — разработка по подготовленному Issue.

Research доводит Issue до системных требований и метки `ready-for-dev` (точка
передачи H1 протокола). Develop берёт его оттуда и доводит до открытого PR.

Код пишет OpenHands. Где именно он это делает — вопрос не удобства, а радиуса
поражения, и отсюда два режима:

`local` (умолчание) — прогон идёт **одноразовым контейнером на своём же
сервере**. Контур получается замкнутым: репозиторий обслуживается целиком
внутри стенда, без чужих раннеров и без минут GitHub.

`dispatch` — прогон уезжает в GitHub Actions (`workflow_dispatch`). Остаётся
для репозиториев, где стенда нет.

Почему отдельный контейнер, а не воркер. Аналитика (`claude -p`) пишет
документы, и её соседство с токенами безобидно. Агент разработки делает другое:
он ИСПОЛНЯЕТ код репозитория — ставит зависимости, гоняет тесты. Внутри воркера
это означало бы выполнение произвольного кода чужого проекта рядом с
GitHub-токеном и ключом модели. Поэтому прогон живёт минуты в отдельном
контейнере, видит только каталог своей задачи и умирает вместе с ней; коммит,
пуш и PR делает воркер уже после — своими руками и своим токеном.

Модуль намеренно чистый: ни сети, ни Temporal, ни GitHub — как
`shared/lifecycle.py` и `shared/agent_events.py`.
"""

import os
import pathlib
import shlex

# --- Режимы ---

LOCAL = "local"
DISPATCH = "dispatch"

# Файл workflow в репозитории-цели для режима `dispatch`. Диспатчится по имени
# файла, а не по имени workflow: имя внутри файла человек меняет свободно,
# путь — контракт.
DEFAULT_WORKFLOW_FILE = "openhands-resolver.yml"
DEFAULT_REF = "main"

# Метка-индикатор для человека: видно в списке Issue, что задача уехала в
# разработку, не открывая ни Actions, ни Temporal.
IN_DEVELOPMENT_LABEL = "in-development"

AGENT_NAME = "openhands"

# Образ агента разработки и том, который воркер делит с ним. Том именно общий:
# воркер готовит каталог задачи и читает результат, раннер пишет в него. Через
# bind-mount это не сделать — путь внутри воркера не существует на хосте.
DEFAULT_RUNNER_IMAGE = "poh-openhands-runner:local"
DEFAULT_WORKSPACE_VOLUME = "poh-dev-workspace"
DEFAULT_WORKSPACE_MOUNT = "/workspaces"

# Пользователь, от которого работает раннер (см. `openhands/Dockerfile`).
#
# Объявлен здесь, потому что каталог задачи готовит ВОРКЕР — от root, — а писать
# в него будет раннер. Разъехались числа — каталог остаётся недоступным на
# запись, и это самый дорогой из возможных отказов: агент не падает, а молча
# уходит писать в /tmp и докладывает об успехе. На живом прогоне так потерялись
# двадцать минут работы, а PR открылся пустым.
RUNNER_UID = 10001
RUNNER_GID = 10001

# Конфигурация MCP агента разработки. Пути сняты со спайка FR-16
# (`docs/spikes/2026-08-19-openhands-mcp-config.md`): агент читает её из
# `$HOME/.openhands/mcp.json`. Воркер кладёт файл в каталог задачи на общем
# томе и переставляет раннеру HOME туда же — образ при этом не трогается.
MCP_CONFIG_DIR = ".openhands"
MCP_CONFIG_NAME = "mcp.json"

# Потолок прогона. Больше часа — это не «агент думает», это агент застрял, и
# держать задачу в неопределённости дороже, чем оборвать и сказать об этом.
DEFAULT_RUN_TIMEOUT_SEC = 2700

# Сколько свободной памяти хоста нужно, чтобы вообще начинать прогон.
#
# Раннер с моделью берёт около трёхсот мегабайт, но опасен не он: хост общий, и
# нехватка бьёт по соседям через OOM-killer, который выбирает жертву сам. Живой
# случай 2026-08-25 — ядро пять раз за семнадцать минут убило чужую боевую базу
# на 2.4 ГБ, пока на хосте шли сборки образов.
#
# Отказаться до старта дешевле во всех смыслах: прогон не сожжён, задача не
# помечена проваленной, а причина названа словами вместо «агент ничего не
# сделал».
DEFAULT_MIN_FREE_MB = 600


def min_free_mb() -> int:
    raw = os.environ.get("DEVELOP_MIN_FREE_MB", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MIN_FREE_MB
    except ValueError:
        return DEFAULT_MIN_FREE_MB
    # Ноль или отрицательное значение — выключение проверки, а не «нужно ноль».
    return max(value, 0)


def free_memory_mb() -> int:
    """Свободная память ХОСТА в мегабайтах. -1 — выяснить не удалось.

    `MemAvailable`, а не `MemFree`: страницы кэша отдаются под нагрузку, и
    считать их занятыми значит отказываться от прогона на исправной машине.

    Воркер живёт в контейнере, но `/proc/meminfo` показывает память хоста — а
    именно она здесь и важна: OOM-killer выбирает жертву по хосту, а не по
    нашему cgroup.
    """
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


def resource_shortage() -> str:
    """Чего не хватает для прогона. Пустая строка — можно запускать."""
    floor = min_free_mb()
    if floor <= 0:
        return ""
    free = free_memory_mb()
    if free < 0:
        # Не смогли измерить — не повод отказывать: на машине без
        # /proc/meminfo прогон и раньше шёл.
        return ""
    if free < floor:
        return (f"свободной памяти хоста {free} МБ при пороге {floor} МБ — "
                f"прогон не начинаю, чтобы не спровоцировать OOM у соседей "
                f"(порог правится DEVELOP_MIN_FREE_MB)")
    return ""


def agent_moved(task_dir) -> bool:
    """Сделал ли агент хоть один ход.

    OpenHands пишет каждое своё действие событием в
    `$HOME/.openhands/conversations/<id>/events/`. Пустой каталог событий при
    существующем разговоре означает ровно одно: агент поднялся и умер на
    инициализации, не приняв ни одного решения.

    Отличать это обязательно. «Отработал и ничего не изменил» — вопрос к
    постановке, «не сделал ни одного хода» — к инфраструктуре, и человека они
    зовут в разные места.
    """
    root = pathlib.Path(task_dir) / MCP_CONFIG_DIR / "conversations"
    if not root.is_dir():
        return False
    for conversation in root.iterdir():
        events = conversation / "events"
        if events.is_dir() and any(events.iterdir()):
            return True
    return False


NO_CHANGES = "агент не изменил ни одного файла — открывать нечего"
NEVER_STARTED = (
    "агент не сделал ни одного хода — прогон оборвался на инициализации, "
    "файлов он не касался. Это отказ окружения, а не работы агента: "
    "смотреть надо доступность MCP, память хоста и ключ модели, "
    "а не постановку задачи"
)


def empty_run_reason(task_dir) -> str:
    """Почему прогон не дал изменений — по следам самого раннера."""
    return NO_CHANGES if agent_moved(task_dir) else NEVER_STARTED

# Находки агента разработки — ФАЙЛОМ в рабочем каталоге, а не через `gh`.
#
# Правило работы «edge-кейс не в эту ветку, а отдельным SubIssue» в режиме
# `dispatch` агент исполнял сам: в Actions есть и `gh`, и GITHUB_TOKEN. У
# одноразового контейнера нет ни того, ни другого — и это не упущение, а весь
# смысл его изоляции: он исполняет чужой код, токена ему не дают. Поэтому агент
# оставляет находки файлом, а строками в секцию GROW тела родителя их
# дописывает воркер своими руками (`collect_dev_followups`) — не заводя по
# ним отдельных Issue. Файл снимается до коммита: он постановка для контура,
# а не часть правки.
FOLLOWUPS_FILE = ".followups.md"

# Потолок находок за прогон. Тридцать «надо бы потом учесть» — это не бэклог, а
# способ не делать основную работу; и каждый такой Issue контур обработает
# рекурсивно, то есть за них ещё и заплатит.
MAX_FOLLOWUPS = 8

# --- Служебные файлы рабочего дерева ---
#
# Всё, что контур и агент создают в клоне ради самого прогона и что НЕ является
# частью правки. Перечень один на весь контур, а снятие — одной функцией.
#
# Раньше это были разрозненные `unlink` в разных функциях без общего списка, и
# механизм отказывал дважды, оба раза молча:
#
#   прогон #19 — постановка уехала в PR одним файлом на 1721 строку и заодно
#                скрыла главное: агент не изменил ни одного файла кода;
#   прогон #35 — оба круга правок закоммитили свою же постановку: `.verdict.md`
#                снимался, `.task.md` забыли.
#
# Добавляешь служебный файл — добавляй СЮДА. Тест на это есть.
#
# `.harness/` (каталог контекста задачи, `shared/task_context.py`) в этот
# перечень НЕ входит, и это НАМЕРЕННО, а не пропуск. Остальные файлы здесь —
# вход контура для ОДНОГО прогона: постановка пересказана дословно в лог,
# находки перенесены в секцию GROW, разбор ревью — в комментарий PR, и после
# этого их смысл исчерпан, а в коммите они шум. `.harness/` — наоборот: это
# ЗАДАЧА в файлах, а не отчёт о прогоне. Он коммитится вместе с кодом, чтобы
# было видно в PR и читалось через полгода — до задачи 7 постановка снималась
# перед коммитом точно так же, как остальные файлы этого перечня, и
# восстановить, что именно видел исполнитель, было нечем.
SERVICE_FILES = (
    ".task.md",        # постановка, собранная контуром
    ".followups.md",   # находки агента для бэклога
    ".verdict.md",     # разбор замечаний ревью на круге правок
    ".reflect.md",     # намерения агента для слоя саморефлексии
)


# Служебные файлы, содержимое которых нужно ПОСЛЕ снятия.
#
# «Снять из рабочего дерева» и «уничтожить» — разные вещи, и путать их дорого.
# `.reflect.md` обязан исчезнуть из клона до коммита, но его читает запись об
# итерации, а она идёт после публикации. Первый живой прогон это и показал:
#
#   17:50:28  сняты служебные файлы: .task.md, .reflect.md
#   17:50:32  запись об итерации отдана слою памяти
#
# Агент инструкцию выполнил, файл написал — и он был удалён за четыре секунды
# до чтения. Намерение в записи оказалось пустым при исправном агенте.
PRESERVED_FILES = (".reflect.md",)


def clear_service_files(clone_dir, keep_dir=None) -> list[str]:
    """Снять служебные файлы из рабочего дерева. Возвращает имена снятых.

    Вызывается ПЕРЕД коммитом на обоих путях — и в разработке, и в круге
    правок. Одна точка снятия вместо разрозненных: пропущенный файл здесь
    означает не ошибку сборки, а тихо испорченный пул-реквест.

    `keep_dir` — куда переложить файлы из `PRESERVED_FILES` вместо удаления.
    Обычно это корень задачи: он лежит ВНЕ рабочего дерева git, поэтому в
    коммит содержимое не попадёт, а прочитать его позже можно.
    """
    from pathlib import Path
    removed = []
    for name in SERVICE_FILES:
        path = Path(clone_dir) / name
        if not path.exists():
            continue
        if keep_dir is not None and name in PRESERVED_FILES:
            target = Path(keep_dir) / name
            try:
                target.write_bytes(path.read_bytes())
            except OSError:
                # Не сохранили — не повод оставить файл в дереве: в коммите он
                # опаснее, чем потеря его содержимого.
                pass
        path.unlink(missing_ok=True)
        removed.append(name)
    return removed


def mode() -> str:
    """`local` по умолчанию: стенд самодостаточен, пока явно не сказано иначе."""
    raw = os.environ.get("DEVELOP_MODE", "").strip().lower()
    return DISPATCH if raw == DISPATCH else LOCAL


def enabled() -> bool:
    """Выключатель контура разработки.

    Пусто — включено: демо-контур замкнут по умолчанию, иначе забытая
    переменная тихо обрывает путь на самом дорогом шаге. Явное `0` оставляет
    Issue в `ready-for-dev`, то есть в очереди к живому разработчику.
    """
    raw = os.environ.get("DEVELOP_ENABLED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def workflow_file() -> str:
    return os.environ.get("DEVELOP_WORKFLOW_FILE", "").strip() or DEFAULT_WORKFLOW_FILE


def workflow_ref() -> str:
    return os.environ.get("DEVELOP_REF", "").strip() or DEFAULT_REF


def runner_image() -> str:
    return os.environ.get("DEVELOP_RUNNER_IMAGE", "").strip() or DEFAULT_RUNNER_IMAGE


def workspace_volume() -> str:
    return os.environ.get("DEVELOP_WORKSPACE_VOLUME", "").strip() or DEFAULT_WORKSPACE_VOLUME


def workspace_mount() -> str:
    return os.environ.get("DEVELOP_WORKSPACE_MOUNT", "").strip() or DEFAULT_WORKSPACE_MOUNT


def run_timeout() -> int:
    raw = os.environ.get("DEVELOP_TIMEOUT_SEC", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_RUN_TIMEOUT_SEC
    except ValueError:
        return DEFAULT_RUN_TIMEOUT_SEC
    return value if value > 0 else DEFAULT_RUN_TIMEOUT_SEC


def task_slug(repo: str, issue_number: int) -> str:
    """Имя каталога задачи в общем томе. Слэш репозитория заменён: это имя
    каталога, а не путь."""
    return f"dev-{repo.replace('/', '__')}-{issue_number}"


def work_branch(issue_number: int) -> str:
    return f"feature/{issue_number}-openhands"


def runner_command(slug: str, *, image: str, volume: str, mount: str,
                   network: str = "", home: str = "") -> list[str]:
    """Команда запуска одноразового контейнера.

    `--rm` обязателен: контейнер существует ради одного прогона, и оставлять
    его — значит копить мусор с ключом модели в переменных окружения.

    Сеть агенту нужна: он ставит зависимости проекта. Секреты внутрь идут
    ровно два — ключ и адрес модели; GitHub-токена там нет, потому что пушит
    и открывает PR воркер, а не агент.

    `network` — сеть, в которой раннеру виден MCP-прокси Repowise. Пусто —
    раннер работает без индекса; это штатный режим, а не отказ.

    `home` — куда переставить домашний каталог. Нужен ровно затем, чтобы агент
    нашёл конфигурацию MCP: он читает её из `$HOME/.openhands/mcp.json`
    (спайк FR-16), а общий том смонтирован в другом месте. Переставить одну
    переменную дешевле, чем оборачивать ENTRYPOINT образа или монтировать
    отдельный файл, путь которого на хосте воркеру неизвестен.

    Пусто — HOME остаётся тем, что задан образом, и поведение раннера не
    меняется вовсе.
    """
    command = [
        # Имя, а не случайное: воркер запускает контейнер и ждёт, но если воркер
        # умер (выкладка, рестарт, terminate воркфлоу), клиент исчезает, а
        # контейнер остаётся работать — с ключом модели, минутами CPU и памятью.
        # `--rm` тут не спасает: он убирает контейнер только после нормального
        # выхода. По имени прогон находится и снимается перед новой попыткой.
        "docker", "run", "--rm", "--name", slug,
        "-v", f"{volume}:{mount}",
        "-w", f"{mount}/{slug}/repo",
        "-e", "LLM_API_KEY",
        "-e", "LLM_BASE_URL",
        "-e", "LLM_MODEL",
    ]
    if network:
        command += ["--network", network]
    if home:
        command += ["-e", f"HOME={home}"]
    command.append(image)
    return command


def proxy_network() -> str:
    """Сеть docker, в которой раннеру виден MCP-прокси Repowise.

    Пусто — раннер работает без индекса. Это штатный режим: интеграция может
    быть не поднята вовсе, и разработка от этого останавливаться не должна.
    """
    return os.environ.get("REPOWISE_NETWORK", "").strip()


def reap_command(slug: str) -> list[str]:
    """Снять остаток прошлой попытки. Нет такого контейнера — команда просто
    вернёт ненулевой код, и это штатно."""
    return ["docker", "rm", "-f", slug]


def runner_env(api_key: str, base_url: str, model: str) -> dict[str, str]:
    return {"LLM_API_KEY": api_key, "LLM_BASE_URL": base_url, "LLM_MODEL": model}


def dispatch_inputs(issue_number: int, *, branch: str, priority: str = "") -> dict[str, str]:
    """Входы прогона для режима `dispatch`. Только строки: `workflow_dispatch`
    других не принимает."""
    return {
        "issue_number": str(issue_number),
        "research_branch": branch or "",
        "priority": priority or "",
    }


def handoff_comment(issue_number: int, *, repo: str, branch: str, where: str) -> str:
    """Комментарий в Issue о старте разработки."""
    artifacts = (f"- контекст для агента: ветка `{branch}`\n"
                 if branch else
                 "- аналитики по задаче не было — агент работает от тела Issue\n")
    return (
        f"🛠 Передал задачу в разработку — {where}.\n\n"
        f"{artifacts}"
        "- найденные по дороге edge-кейсы агент в этой же ветке не чинит — они "
        "лягут строками в секцию GROW тела этого Issue и будут ждать решения "
        "после гейта приёмки\n\n"
        f"Ветка работы — `{work_branch(issue_number)}`, "
        f"по завершении откроется PR с `Closes #{issue_number}`."
    )


def pr_body(issue_number: int, *, branch: str, foreign: list[str]) -> str:
    note = ""
    if foreign:
        # Красный набор без объяснения уходит на ревью загадкой: смотрящий
        # решит, что сломал агент, и пойдёт разбирать его правку.
        listed = "\n".join(f"- `{name}`" for name in foreign)
        note = (
            "\n## Эти тесты падали и до правки\n\n"
            f"{listed}\n\n"
            "Агент их не трогал: они красные на том же коммите и без его "
            "работы — проверено прогоном на чистом дереве.\n"
        )
    return (
        f"Closes #{issue_number}\n\n"
        f"Разработку вёл OpenHands по системным требованиям из `{branch or '—'}`.\n"
        "Найденные по дороге edge-кейсы в этой ветке не чинились — они собраны "
        f"строками в секции GROW тела #{issue_number} и ждут гейта приёмки.\n"
        f"{note}\n"
        f"<sub>origin: agent · root-issue: #{issue_number}</sub>\n"
    )


def parse_followups(text: str) -> list[dict]:
    """Находки из файла агента: заголовок `## …` — название, остальное — тело.

    Проза без заголовков находкой не считается. Без заголовка не понять, где
    кончается одна находка и начинается следующая, а Issue с телом вместо
    названия хуже, чем ненайденный edge-кейс: он живёт в бэклоге и его читают.
    """
    items: list[dict] = []
    title: str | None = None
    body: list[str] = []

    def flush() -> None:
        if title:
            items.append({"title": title, "body": "\n".join(body).strip()})

    for raw in (text or "").splitlines():
        if raw.startswith("## "):
            flush()
            if len(items) >= MAX_FOLLOWUPS:
                return items[:MAX_FOLLOWUPS]
            title = raw[3:].strip()
            body = []
            continue
        if title is not None:
            body.append(raw)
    flush()
    return items[:MAX_FOLLOWUPS]


def followup_body(item: dict, *, parent: int) -> str:
    """Тело SubIssue по находке. Ключ цепочки первой строкой — по нему контур
    отличает свой выход от входа и считает глубину (правила R6, R7)."""
    return "\n".join([
        f"root-issue: #{parent}",
        "",
        f"Найдено агентом разработки по задаче #{parent} и намеренно не "
        "исправлено в её ветке: MVP это не блокирует.",
        "",
        item.get("body") or "(без описания)",
    ])


def quote(command: list[str]) -> str:
    """Команда одной строкой — для лога и сообщений об ошибке."""
    return " ".join(shlex.quote(part) for part in command)
