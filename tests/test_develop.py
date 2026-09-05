"""Контракт прогона разработки — та его часть, что не зависит от воркера.

Проверяется граница, на которой контур передаёт работу исполнителю и забирает
её обратно: что уезжает агенту, что ему НЕ уезжает, как называется его
контейнер и чем он отличает «не сделал ни одного хода» от «сделал и ничего не
изменил».

Тесты активностей (`trigger_openhands_resolver`, `_dev_run_agent`,
`_handover_to_runner`) живут в `poh-issue-agents`: им нужен воркер.
"""

import pathlib
import re

import pytest

from poh_developer import develop, runner

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_DOCKERFILE = REPO_ROOT / "agent" / "Dockerfile"


# --- Постановка, уезжающая на чужую сторону ---

def test_inputs_are_all_strings():
    """`workflow_dispatch` принимает только строки — число молча уронит прогон
    на стороне GitHub, где мы его уже не увидим."""
    inputs = develop.dispatch_inputs(12, branch="research/issue-12", priority="P1")

    assert all(isinstance(v, str) for v in inputs.values())
    assert inputs["issue_number"] == "12"


def test_missing_branch_is_an_explicit_empty_string():
    """Пустая строка — это «аналитики не было», и агент обязан отличать её от
    несуществующей ветки. Отсутствие ключа означало бы «не передали»."""
    inputs = develop.dispatch_inputs(12, branch="")

    assert inputs["research_branch"] == ""


def test_comment_says_where_the_run_happens():
    body = develop.handoff_comment(12, repo="o/r", branch="research/issue-12",
                                   where="запустил OpenHands на своём сервере")

    assert "research/issue-12" in body
    assert "на своём сервере" in body    # где именно идёт работа
    assert "Closes #12" in body          # чем прогон должен закончиться
    assert "GROW" in body                # и что он делает с edge-кейсами


def test_comment_without_analysis_says_so_instead_of_naming_a_branch():
    body = develop.handoff_comment(12, repo="o/r", branch="", where="запустил агента")

    assert "аналитики по задаче не было" in body
    assert "research/issue-12" not in body


# --- Рубильники ---

def test_develop_is_on_by_default(monkeypatch):
    """Забытая переменная не должна тихо обрывать контур на самом дорогом шаге."""
    monkeypatch.delenv("DEVELOP_ENABLED", raising=False)

    assert develop.enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "NO"])
def test_develop_switches_off_only_explicitly(monkeypatch, raw):
    monkeypatch.setenv("DEVELOP_ENABLED", raw)

    assert develop.enabled() is False


def test_local_is_the_default(monkeypatch):
    """Стенд самодостаточен, пока явно не сказано иначе: репозиторий
    обслуживается целиком внутри контура, без чужих раннеров."""
    monkeypatch.delenv("DEVELOP_MODE", raising=False)

    assert develop.mode() == develop.LOCAL


# --- Одноразовый контейнер исполнителя ---

def test_runner_gets_no_github_token():
    """Агент исполняет код чужого репозитория. Токен ему не нужен: пушит и
    открывает PR воркер — своим токеном и после прогона."""
    command = develop.runner_command("dev-o__r-7", image="img",
                                     volume="vol", mount="/workspaces")

    passed = {command[i + 1] for i, part in enumerate(command) if part == "-e"}
    assert passed == {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"}
    assert not any("GH" in part or "TOKEN" in part for part in command)


def test_runner_is_disposable_and_sees_only_its_task():
    command = develop.runner_command("dev-o__r-7", image="img",
                                     volume="vol", mount="/workspaces")

    assert "--rm" in command                                  # не копим мусор с ключом
    assert "vol:/workspaces" in command                       # общий том с воркером
    assert command[command.index("-w") + 1] == "/workspaces/dev-o__r-7/repo"


def test_runner_container_has_a_deterministic_name():
    """У прогона должно быть имя, по которому его можно найти и снять.

    Контейнер живёт своей жизнью: воркер запускает его и ждёт, но если воркер
    умер (выкладка, рестарт, terminate воркфлоу), клиент исчезает, а контейнер
    остаётся работать — с ключом модели, минутами CPU и памятью. На стенде так
    и вышло: прогон сняли, а раннер жил ещё полчаса и доедал память.
    """
    slug = develop.task_slug("o/r", 7)
    command = develop.runner_command(slug, image="i", volume="v", mount="/m")

    assert "--name" in command
    assert command[command.index("--name") + 1] == slug


def test_leftover_run_is_reaped_by_name():
    """Новая попытка начинается с чистого места.

    Temporal повторяет активность разработки до трёх раз. Если предыдущая
    попытка умерла вместе с воркером, её контейнер остался работать — и вторая
    попытка либо упирается в занятое имя, либо запускает второго агента в тот
    же рабочий каталог. Оба исхода хуже, чем снять остаток.
    """
    slug = develop.task_slug("o/r", 7)

    assert develop.reap_command(slug) == ["docker", "rm", "-f", slug]


def test_task_slug_is_a_directory_name_not_a_path():
    """Слэш репозитория в имени каталога создал бы вложенность вместо задачи."""
    assert "/" not in develop.task_slug("po-helper-org/poh-demo-checkout", 7)


def test_run_timeout_falls_back_on_garbage(monkeypatch):
    """Битое значение не должно означать «без потолка»: зависший агент держал
    бы задачу в неопределённости молча."""
    monkeypatch.setenv("DEVELOP_TIMEOUT_SEC", "скоро")

    assert develop.run_timeout() == develop.DEFAULT_RUN_TIMEOUT_SEC


# --- Согласие модуля с образом ---

def test_runner_uid_matches_the_image():
    """Uid раннера объявлен в одном месте и совпадает с образом.

    Воркер готовит каталог задачи от root, а раннер работает непривилегированным
    пользователем. Разъехались числа — каталог остаётся недоступным на запись, и
    это САМЫЙ дорогой из возможных отказов: агент не падает, а молча уходит
    писать в /tmp и докладывает об успехе. На живом прогоне #19 так потерялись
    21 минута работы, а PR открылся пустым.
    """
    found = re.search(r"useradd\s+-m\s+-u\s+(\d+)",
                      RUNNER_DOCKERFILE.read_text(encoding="utf-8"))

    assert found, "не нашёл создание пользователя в образе раннера"
    assert int(found.group(1)) == develop.RUNNER_UID


def test_runner_node_major_is_pinned_to_the_declared_one():
    """Мажор Node раннера объявлен здесь и сверяется с образом.

    Код проекта исполняет раннер, а проверки проекта гоняет ВОРКЕР — той же
    командой, что и CI. На живом прогоне #13 это дало
    `Could not find 'tests/*.test.mjs'`: команда репозитория —
    `node --test "tests/*.test.mjs"`, glob раскрывает сам Node (с 22), а в
    образе воркера стоял 20. Красный шаг разработки на зелёном коде.

    Раньше инвариант держался сверкой двух Dockerfile в одном репозитории.
    После разъезда по репозиториям сверять там нечего, поэтому мажор объявлен
    константой ЗДЕСЬ: образ воркера в `poh-issue-agents` обязан сверяться с
    ней, а не с собственной строкой.
    """
    found = re.search(r"deb\.nodesource\.com/setup_(\d+)\.x",
                      RUNNER_DOCKERFILE.read_text(encoding="utf-8"))

    assert found, "не нашёл установку Node в образе раннера"
    assert int(found.group(1)) == runner.RUNNER_NODE_MAJOR


def test_the_uid_the_other_repo_reads_is_the_one_the_stage_declares():
    """`poh_developer.runner` — единая точка входа для чужого образа, а не
    второй источник правды: значение обязано совпадать с объявленным в
    контракте прогона."""
    assert runner.RUNNER_UID == develop.RUNNER_UID


# --- Служебные файлы ---

def test_service_files_are_listed_in_one_place():
    """Механизм отказывал дважды и оба раза молча: прогон #19 — постановка на
    1721 строку уехала в PR и заодно скрыла, что кода агент не тронул; прогон
    #35 — оба круга правок закоммитили собственную постановку (`.verdict.md`
    снимался, `.task.md` забыли).

    Добавляешь служебный файл — добавляй в перечень.
    """
    assert set(develop.SERVICE_FILES) == {
        ".task.md", ".followups.md", ".verdict.md", ".reflect.md"}


def test_reflect_note_survives_the_sweep():
    """`.reflect.md` обязан исчезнуть из клона до коммита, но его читает запись
    об итерации — а она идёт ПОСЛЕ публикации.

    Первый живой прогон слоя саморефлексии: файл был удалён за четыре секунды
    до чтения, и намерение в записи оказалось пустым при исправном агенте.
    «Снять из рабочего дерева» и «уничтожить» — разные вещи.
    """
    assert ".reflect.md" in develop.PRESERVED_FILES
    assert ".reflect.md" in develop.SERVICE_FILES


def test_sweep_moves_the_preserved_file_out_of_the_worktree(tmp_path):
    clone, keep = tmp_path / "repo", tmp_path / "task"
    clone.mkdir(), keep.mkdir()
    (clone / ".task.md").write_text("постановка", encoding="utf-8")
    (clone / ".reflect.md").write_text("намерение", encoding="utf-8")

    removed = develop.clear_service_files(clone, keep_dir=keep)

    assert set(removed) == {".task.md", ".reflect.md"}
    assert not (clone / ".task.md").exists()
    assert not (clone / ".reflect.md").exists()      # из дерева ушёл
    assert (keep / ".reflect.md").read_text(encoding="utf-8") == "намерение"


def test_harness_dir_is_not_a_service_file():
    """`.harness/` коммитится НАМЕРЕННО: это задача в файлах, а не отчёт о
    прогоне. До задачи 7 постановка снималась перед коммитом так же, как
    остальные служебные файлы, и восстановить, что именно видел исполнитель,
    было нечем."""
    assert not any(name.startswith(".harness") for name in develop.SERVICE_FILES)


# --- Разбор находок агента ---

def test_findings_need_a_heading():
    """Проза без заголовков находкой не считается: без заголовка не понять, где
    кончается одна и начинается следующая, а запись с телом вместо названия
    хуже, чем ненайденный edge-кейс — она живёт в бэклоге и её читают."""
    items = develop.parse_followups("просто текст без заголовка\nещё строка")

    assert items == []


def test_findings_are_capped():
    """Тридцать «надо бы потом учесть» — не бэклог, а способ не делать основную
    работу; и каждый такой пункт контур обработает рекурсивно."""
    text = "\n".join(f"## находка {i}\nтело" for i in range(30))

    assert len(develop.parse_followups(text)) == develop.MAX_FOLLOWUPS


def test_finding_keeps_its_body():
    items = develop.parse_followups(
        "## Отрицательная цена проходит в расчёт\n"
        "`subtotal` проверяет `price < 0` уже после умножения на `qty`.\n")

    assert items[0]["title"] == "Отрицательная цена проходит в расчёт"
    assert "subtotal" in items[0]["body"]


# --- Отказ, который надо уметь назвать ---

def test_a_run_that_never_started_is_told_apart_from_one_that_changed_nothing(tmp_path):
    """«Отработал и ничего не изменил» — вопрос к постановке. «Не сделал ни
    одного хода» — к инфраструктуре. Человека они зовут в разные места, и
    сводить их к одному сообщению значит отправлять его не туда."""
    assert develop.empty_run_reason(tmp_path) == develop.NEVER_STARTED

    events = tmp_path / develop.MCP_CONFIG_DIR / "conversations" / "c1" / "events"
    events.mkdir(parents=True)
    (events / "0.json").write_text("{}", encoding="utf-8")

    assert develop.empty_run_reason(tmp_path) == develop.NO_CHANGES


# --- Резерв памяти хоста ---

def test_shortage_is_named_with_the_knob_that_fixes_it(monkeypatch):
    """Отказ до старта дешевле сожжённого прогона, но только если назвал причину.

    Живой случай 2026-08-25: ядро пять раз за семнадцать минут убило чужую
    боевую базу на 2.4 ГБ, пока на хосте шли сборки образов.
    """
    monkeypatch.setenv("DEVELOP_MIN_FREE_MB", "100000")
    monkeypatch.setattr(develop, "free_memory_mb", lambda: 512)

    reason = develop.resource_shortage()

    assert "DEVELOP_MIN_FREE_MB" in reason
    assert "512" in reason


def test_unmeasurable_memory_does_not_block_the_run(monkeypatch):
    """На машине без /proc/meminfo прогон и раньше шёл. Не смогли измерить —
    не повод отказывать."""
    monkeypatch.setattr(develop, "free_memory_mb", lambda: -1)

    assert develop.resource_shortage() == ""


def test_the_guard_switches_off_by_zero(monkeypatch):
    monkeypatch.setenv("DEVELOP_MIN_FREE_MB", "0")
    monkeypatch.setattr(develop, "free_memory_mb", lambda: 1)

    assert develop.resource_shortage() == ""
