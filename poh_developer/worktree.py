"""Коммит и пуш рабочего дерева агента разработки — общая часть для провайдеров.

Механика одна на GitHub и GitLab: агент отработал в каталоге задачи, воркер
берёт то, что он написал, коммитит в рабочую ветку и толкает её. Различаются
только две вещи — имя пользователя для credential-хелпера и то, чем открывается
запрос на изменения (pull request либо merge request). Обе живут у клиентов,
git-часть — здесь.

Почему общая, а не по копии на клиента. В этом коде записаны разборы трёх
инцидентов: пустой индекс на ретрае существующей ветки, `.gitignore` целевого
репозитория, съедающий `force_include`, и проверка присутствия путей ФАКТОМ, а
не замыслом. Вторая копия означала бы, что следующая правка дойдёт до одного
провайдера и не дойдёт до второго — а обнаружится это на живом прогоне.

Делает это ВОРКЕР, а не агент разработки: агенту токен не давали намеренно, он
исполняет код чужого репозитория. Здесь токен нужен уже не агенту, а контуру —
и живёт он ровно в этом процессе.
"""
from __future__ import annotations

import logging
import os
import subprocess

_log = logging.getLogger("worktree")


class GitCommandError(RuntimeError):
    """git отказал, и причина видна в тексте.

    Живёт здесь, а не у клиента: git-механику зовут оба провайдера, и тип
    отказа у них обязан быть один — иначе `except` в вызывающем коде ловил бы
    GitHub и пропускал GitLab.
    """


def runner(clone_dir: str, env: dict):
    """git по рабочему дереву задачи, с видимой причиной отказа.

    Токен живёт в окружении (`GIT_PUSH_TOKEN`), а не в argv, и в stderr git его
    не печатает — но текст ошибки уезжает в историю Temporal и в комментарий
    Issue, поэтому подстраховка стоит дешевле разбирательства.
    """
    token = env.get("GIT_PUSH_TOKEN") or env.get("GH_PUSH_TOKEN") or ""

    def git(*args: str, check: bool = True):
        proc = subprocess.run(["git", "-C", clone_dir, *args], env=env,
                              capture_output=True, text=True, timeout=300)
        if check and proc.returncode:
            detail = (proc.stderr or proc.stdout or "").strip()[:800]
            if token:
                detail = detail.replace(token, "[Filtered]")
            raise GitCommandError(
                f"git {' '.join(args)} → код {proc.returncode}: {detail}")
        return proc

    return git


def _missing_from_tree(git, paths: tuple[str, ...]) -> list[str]:
    """Каких путей из `paths` нет в дереве HEAD — проверка ФАКТОМ (M3), а не
    предположением, что `git add -f` где-то выше сработал. `git add -A`
    молча пропускает пути, которые `.gitignore` целевого репозитория
    игнорирует — единственный способ узнать, доехал ли путь до коммита,
    который вот-вот уйдёт в push, это посмотреть в сам коммит.

    Пустой `paths` — нулевая стоимость: ни одного вызова git не делается.
    """
    if not paths:
        return []
    tree = git("ls-tree", "-r", "--name-only", "HEAD", check=False).stdout
    names = tree.splitlines()
    missing = []
    for path in paths:
        if any(name == path or name.startswith(f"{path}/") for name in names):
            continue
        missing.append(path)
    return missing


def commit_and_push(repo: str, clone_dir: str, branch: str, *,
                    message: str,
                    credentials: tuple[str, str],
                    committer_email: str,
                    ignore_for_empty_check: tuple[str, ...] = (),
                    force_include: tuple[str, ...] = ()) -> bool:
    """Закоммитить рабочее дерево в `branch` и протолкнуть. False — коммитить нечего.

    `credentials` — пара (имя пользователя, токен) от клиента провайдера:
    `x-access-token` у GitHub, `oauth2` у GitLab. Токен идёт через
    credential.helper в env, а не в URL: argv команды целиком попадает в текст
    CalledProcessError, и вклеенный токен уехал бы в историю Temporal при
    первом же сбое пуша.

    `ignore_for_empty_check` — пути (git pathspec, например `".harness/**"`),
    которые не учитываются при решении «есть ли изменения». Они по-прежнему
    попадают в сам коммит через `git add -A` ниже — исключаются только из
    ПРОВЕРКИ пустоты. Нужно вызывающему, который пишет в рабочее дерево
    каталог, обязанный дойти до PR независимо от того, менял ли агент код: без
    исключения такой каталог сам по себе выглядел бы диффом, и «агент ничего
    не изменил» перестало бы обнаруживаться.

    `force_include` — пути (без pathspec-магии, например `".harness"`),
    которые обязаны попасть в коммит НЕЗАВИСИМО от `.gitignore` целевого
    репозитория (M3). Голый `git add -A` молча пропускает путь, который
    репозиторий игнорирует: проверка пустоты (уже не учитывающая этот путь
    через `ignore_for_empty_check`) видит только код агента, коммит и PR
    проходят — а каталог контекста теряется без единого предупреждения.
    `git add -f` ниже обходит `.gitignore`; после решения о коммите факт
    присутствия в дереве HEAD подтверждается ЗАНОВО (`_missing_from_tree`) —
    замыслом («мы же вызвали add -f») здесь не обойтись, потому что путь может
    остаться только в индексе, если решение «коммитить или нет» приняло его не
    в расчёт (см. ветку `forced_pending` ниже).
    """
    username, token = credentials
    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0":
            f"!f() {{ echo username={username}; echo password=$GIT_PUSH_TOKEN; }}; f",
        "GIT_CONFIG_KEY_1": "user.name",
        "GIT_CONFIG_VALUE_1": "openhands-agent",
        "GIT_CONFIG_KEY_2": "user.email",
        "GIT_CONFIG_VALUE_2": committer_email,
        # Каталог задачи передан раннеру (uid 10001), а коммит и пуш делает воркер
        # от root. Git на такое отвечает `fatal: detected dubious ownership` и
        # отказывается работать — готовая работа агента не доехала бы до PR.
        # Объявляем каталог доверенным для этой команды, не трогая общий конфиг.
        "GIT_CONFIG_KEY_3": "safe.directory",
        "GIT_CONFIG_VALUE_3": clone_dir,
        "GIT_PUSH_TOKEN": token,
    }

    git = runner(clone_dir, env)

    # ДО checkout: ветка уже существует ЛОКАЛЬНО только если по этому же
    # clone_dir сюда уже заходил предыдущий вызов этой функции. `dev_prepare`
    # клонирует репозиторий заново на каждый прогон СТАДИИ (а не на каждую
    # попытку публикации), поэтому в пределах ретраев `dev_publish` рабочее
    # дерево — то же самое: если ветка уже есть, коммит на ней, скорее всего,
    # уже сделан прошлой попыткой, и упал только пуш (или сам PR).
    branch_existed = git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                         check=False).returncode == 0

    git("checkout", "-B", branch)
    git("add", "-A")
    for path in force_include:
        # -f обходит .gitignore целевого репозитория: без него `add -A` выше
        # молча пропускает путь, который репозиторий игнорирует (M3).
        git("add", "-f", "--", path)

    # Пустой ИНДЕКС — не то же самое, что «агент ничего не менял»: если ветка
    # уже существовала до этого вызова, значит, коммит уехал в прошлой попытке,
    # а сорвался только пуш (или создание PR) — публикацию нужно довести, а не
    # объявлять «нет диффа». Пустой коммит по-прежнему не делаем: PR без диффа
    # ревьюить нечего, а фаза задачи от него сдвинулась бы как от настоящей
    # работы — это по-прежнему верно для ПЕРВОЙ попытки на новой ветке.
    diff_args = ["diff", "--cached", "--quiet", "--", "."]
    diff_args += [f":(exclude){pattern}" for pattern in ignore_for_empty_check]
    visible_diff = git(*diff_args, check=False).returncode != 0

    # M3: `force_include` может внести изменение, которого «видимый» дифф не
    # видит (он же его специально исключает через `ignore_for_empty_check`).
    # Гейт на `branch_existed` обязателен: на СОВЕРШЕННО НОВОЙ ветке
    # force_include-путь «новый» ВСЕГДА — без гейта пустой прогон считался бы
    # правкой при КАЖДОМ вызове, и это ровно тот регресс M1/H1, ради которого
    # `ignore_for_empty_check` вообще существует.
    forced_pending = branch_existed and bool(force_include) and git(
        "diff", "--cached", "--quiet", "--", *force_include, check=False
    ).returncode != 0

    if visible_diff or forced_pending:
        git("commit", "-m", message, "-m",
            "Автор изменений — OpenHands, запущен активностью Develop.")
    elif not branch_existed:
        _log.warning("%s: агент не изменил ни одного файла", repo)
        return False

    # Подтверждаем ФАКТОМ, а не замыслом вызова `add -f` выше (M3): проверяем
    # дерево HEAD — то, что вот-вот уйдёт в push.
    missing = _missing_from_tree(git, force_include)
    if missing:
        raise RuntimeError(
            f"{repo}: {', '.join(missing)} не попал(и) в коммит ветки {branch}, "
            "хотя force_include этого требует — вероятно, .gitignore целевого "
            "репозитория. Публикация остановлена до пуша."
        )

    git("push", "--force-with-lease", "-u", "origin", branch)
    return True
