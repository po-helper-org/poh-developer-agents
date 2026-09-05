# FNR-6: дорогие стадии Issue как дочерние воркфлоу — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести разработку (OpenHands) и круг правок по ревью из активностей внутри `IssueLifecycle` в дочерние воркфлоу с детерминированными идентификаторами, чтобы операционная история работ по Issue восстанавливалась средствами Temporal, а ретраи повторяли отдельный шаг, а не всю стадию.

**Architecture:** Правило: стадия с тремя свойствами (минуты работы · недетерминизм · денежная стоимость) исполняется дочерним воркфлоу с id из `shared/workflow_ids.py`, её внутренние шаги — самостоятельные активности со своими таймаутами и политиками ретраев. Существующие приватные функции `_dev_*` остаются нетронутыми; поверх них добавляются тонкие обёртки `@activity.defn`. Прежний путь через `trigger_openhands_resolver` сохраняется целиком — по нему идёт реплей прогонов, начатых до выкладки.

**Tech Stack:** Python 3.12, temporalio (Python SDK), pytest + pytest-asyncio, `temporalio.testing.WorkflowEnvironment`.

## Global Constraints

- **Спека:** `sa_documentation/FNR/FNR_6/system_requirements.md`. Вердикт дебатов — `sa_documentation/FNR/FNR_6/concept.md`.
- **Маркеры патча обязательны.** Любое изменение решений воркфлоу `IssueLifecycle` — только под `workflow.patched(...)`. На стенде живут прогоны, начатые на прежнем коде; изменение без маркера роняет их недетерминизмом.
- **`worker/workflows.py:_run_linear` НЕ ИЗМЕНЯТЬ.** Метод объявлен неизменяемым (`worker/workflows.py:1493-1500`): по нему идёт реплей прогонов прежнего поколения, чья история не знает маркеров патча. Вызов `trigger_openhands_resolver` внутри него (`worker/workflows.py:1755-1762`) остаётся как есть.
- **`trigger_openhands_resolver` МОЖНО рефакторить, но НЕ менять его наблюдаемое поведение.**
  По нему идёт ветка «без патча» и `_run_linear`. Реплей-детерминизм Temporal касается
  кода ВОРКФЛОУ, а не активностей: активности не переигрываются, в историю пишется их
  результат. Поэтому вынести общую логику в приватный хелпер и звать его из старой и
  новой активности — безопасно и предпочтительно: две копии разъедутся на первой же правке.
- **Между шагами передаются пути, слаги и номера, НЕ содержимое.** Тексты (постановка, вывод агента, вывод тестов) остаются в общем томе и в логе воркера. Потолок полезной нагрузки возврата активности — 4 КБ (НФТ-01).
- **Все форматы id — только в `shared/workflow_ids.py`.** Второго места сборки быть не должно.
- **`ParentClosePolicy.ABANDON`** обязателен для обеих новых стадий: дорогой прогон не должен умирать от continue-as-new или завершения родителя.
- **Ручные гварды НЕ снимать** в этой работе: `_dev_announce` (проверка метки `in-development`, `worker/activities.py:1318-1340`) и `_analysis_running` (`worker/workflows.py:771`).
- **`max_concurrent_activities=3` НЕ менять** (`worker/worker.py:106`) — НФТ-02.
- **`shared/develop.py` НЕ менять** — модуль намеренно чистый (ни сети, ни Temporal, ни GitHub).
- **Порядок шагов разработки:** находки (`collect_dev_followups`) собираются ПОСЛЕ агента и ДО тестов и публикации. Нарушение даёт файл находок в PR и повторное чтение прошлых находок агентом на следующем круге.
- **Тесты:** `.venv/bin/pytest -q` (цель `make test`). Рабочий каталог — корень репозитория; `tests/conftest.py` добавляет `worker/` и корень в `sys.path`.
- **Язык:** комментарии и докстринги — русский, как во всём репозитории. Комментарий объясняет ПРИЧИНУ решения, а не пересказывает код.

---

### Task 1: Идентификаторы дорогих стадий

**Files:**
- Modify: `shared/workflow_ids.py:36-39` (добавить после `analysis_workflow_id`)
- Test: `tests/test_workflow_ids.py` (создать)

**Interfaces:**
- Consumes: ничего
- Produces:
  - `development_workflow_id(repo_full_name: str, issue_number: int) -> str` → `"develop-<repo>-<issue>"`
  - `pr_fix_workflow_id(repo_full_name: str, pr_number: int, round_number: int) -> str` → `"prfix-<repo>-<pr>-<round>"`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_workflow_ids.py`:

```python
"""Идентификаторы дорогих стадий — ключ к восстановлению истории Issue.

Дерево дочерних прогонов в Temporal UI привязано к RunId родителя, а родитель
делает continue-as-new при 800 событиях истории (`worker/workflows.py:74`).
Поэтому полнота восстановления держится на ИДЕНТИФИКАТОРЕ, а не на дереве —
и формат id проверяется тестом, а не соглашением.
"""

from shared.workflow_ids import (
    analysis_workflow_id,
    development_workflow_id,
    estimate_workflow_id,
    issue_workflow_id,
    pr_fix_workflow_id,
)


def test_development_id_is_derived_from_the_issue():
    """Идентификатор выводится из номера Issue и не содержит номера попытки:
    повторный запуск при идущем прогоне обязан упереться в занятый id, а не
    поднять второй контейнер раннера."""
    assert development_workflow_id("o/r", 39) == "develop-o/r-39"


def test_development_id_is_stable_across_calls():
    """Ключ идемпотентности: два вызова дают одну строку, иначе
    WorkflowAlreadyStartedError никогда не сработает."""
    assert development_workflow_id("o/r", 39) == development_workflow_id("o/r", 39)


def test_pr_fix_id_includes_the_round():
    """Круги правок — честно разные прогоны со своей историей: второй круг не
    должен упираться в занятый идентификатор первого."""
    assert pr_fix_workflow_id("o/r", 41, 2) == "prfix-o/r-41-2"


def test_pr_fix_rounds_do_not_collide():
    assert pr_fix_workflow_id("o/r", 41, 1) != pr_fix_workflow_id("o/r", 41, 2)


def test_stage_prefixes_are_distinct():
    """Восстановление истории идёт фильтром по префиксу: `issue-` даёт только
    родителей, `develop-` — только разработки. Совпади префиксы — фильтр
    перестал бы разделять стадии."""
    ids = [
        issue_workflow_id("o/r", 7),
        analysis_workflow_id("o/r", 7),
        estimate_workflow_id("o/r", 7, 555),
        development_workflow_id("o/r", 7),
        pr_fix_workflow_id("o/r", 7, 1),
    ]
    prefixes = [i.split("-", 1)[0] for i in ids]
    assert len(set(prefixes)) == len(prefixes), f"префиксы пересеклись: {prefixes}"
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/test_workflow_ids.py -q`
Expected: FAIL — `ImportError: cannot import name 'development_workflow_id' from 'shared.workflow_ids'`

- [ ] **Step 3: Реализовать**

Добавить в конец `shared/workflow_ids.py`:

```python
def development_workflow_id(repo_full_name: str, issue_number: int) -> str:
    # Номера попытки в ключе НЕТ намеренно: разработка по одному Issue идёт в
    # один момент времени в одном экземпляре, и повторный запуск при идущем
    # прогоне обязан упереться в WorkflowAlreadyStarted. Иначе на репозитории
    # окажется два агента в одном рабочем каталоге — имя контейнера раннера
    # тоже выводится из номера Issue (`shared/develop.py:131`).
    return f"develop-{repo_full_name}-{issue_number}"


def pr_fix_workflow_id(repo_full_name: str, pr_number: int, round_number: int) -> str:
    # Номер круга в ключе, в отличие от разработки: круги разделены ожиданием
    # доклада ревью, и второй круг — честно новый прогон со своей историей.
    # Без номера он упирался бы в id первого, и доводка PR вставала бы после
    # первого же круга.
    return f"prfix-{repo_full_name}-{pr_number}-{round_number}"
```

- [ ] **Step 4: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/test_workflow_ids.py -q`
Expected: PASS — 5 passed

- [ ] **Step 5: Проверить единственность источника формата**

Run: `grep -rn '"develop-\|"prfix-\|f"develop-\|f"prfix-' --include="*.py" . | grep -v '/.venv/' | grep -v '/tests/' | grep -v '/.claude/'`
Expected: ровно две строки, обе в `shared/workflow_ids.py`

- [ ] **Step 6: Коммит**

```bash
git add shared/workflow_ids.py tests/test_workflow_ids.py
git commit -m "feat(workflow-ids): идентификаторы дорогих стадий Issue

История работ по Issue восстанавливается фильтром по префиксу id, а не
деревом в UI: родитель делает continue-as-new и меняет RunId, а дочерние
прогоны остаются у прежнего. Формат живёт в одном модуле — разъехавшись,
копии молча сделали бы выборку неполной."
```

---

### Task 2: Тип `DevelopPlan` и активности входа в разработку

**Files:**
- Modify: `shared/workflow_types.py` (добавить `DevelopPlan` после `AnalyzeInput`, строка ~196)
- Modify: `worker/activities.py` (добавить активности после `trigger_openhands_resolver`, строка ~1261)
- Test: `tests/test_develop_child.py` (создать)

**Interfaces:**
- Consumes: `shared.develop` (модуль без изменений), `IssueInput`
- Produces:
  - `DevelopPlan(mode: str, branch: str)` — датакласс в `shared/workflow_types.py`
  - `activities.dev_begin(issue: IssueInput) -> DevelopPlan` — проверяет выключатель, определяет режим и ветку аналитики
  - `activities.dev_dispatch(issue: IssueInput, branch: str) -> None` — режим `dispatch`: диспатч в GitHub Actions + объявление

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_develop_child.py`:

```python
"""Разработка как дочерний воркфлоу: границы шагов.

Прежде разработка была ОДНОЙ активностью из четырёх внутренних шагов, и её
ретрай повторял всё целиком. На живом прогоне #39 падал только `git push` —
а заново шёл двадцатиминутный прогон агента, и контур трижды объявил о
передаче задачи. Здесь проверяются границы, по которым стадия разрезана.
"""

import pytest

import activities as activities_module
from shared.workflow_types import DevelopPlan, IssueInput


def _issue(number: int = 39) -> IssueInput:
    return IssueInput(repo="o/r", issue_number=number, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


@pytest.fixture
def gh(monkeypatch):
    """GitHub подменён целиком: активности не ходят в сеть в тестах."""
    calls: dict = {"dispatch": [], "labels": [], "comments": [], "branches": True}
    monkeypatch.setattr(activities_module.github_client, "dispatch_workflow",
                        lambda repo, wf, ref, inputs:
                            calls["dispatch"].append((repo, wf, ref, inputs)))
    monkeypatch.setattr(activities_module.github_client, "add_label",
                        lambda repo, n, label: calls["labels"].append(label))
    monkeypatch.setattr(activities_module.github_client, "post_comment",
                        lambda repo, n, body: calls["comments"].append(body))
    monkeypatch.setattr(activities_module.github_client, "branch_exists",
                        lambda repo, branch: calls["branches"])
    monkeypatch.setattr(activities_module.github_client, "get_issue",
                        lambda repo, n: {"labels": []})
    return calls


async def test_begin_reports_local_mode_and_the_analysis_branch(gh, monkeypatch):
    """Ветка аналитики — вход агента. Она определяется ОДИН раз на входе в
    стадию, а не заново на каждом шаге: иначе удалённая по дороге ветка дала
    бы шагам разный контекст."""
    monkeypatch.setenv("DEVELOP_MODE", "local")
    monkeypatch.delenv("DEVELOP_ENABLED", raising=False)

    plan = await activities_module.dev_begin(_issue(39))

    assert plan == DevelopPlan(mode="local", branch="research/issue-39")


async def test_begin_reports_an_empty_branch_when_there_was_no_analysis(gh, monkeypatch):
    """Аналитики не было — агент работает от тела Issue. Пустая строка, а не
    отсутствие поля: «работаем без требований» это решение, и оно обязано быть
    видно в истории воркфлоу."""
    monkeypatch.setenv("DEVELOP_MODE", "local")
    gh["branches"] = False

    plan = await activities_module.dev_begin(_issue(39))

    assert plan.branch == ""


async def test_begin_refuses_when_the_switch_is_off(gh, monkeypatch):
    """Явное `0` оставляет Issue в очереди к живому разработчику. Отказ громкий:
    молча пропущенная стадия неотличима от исправной работы."""
    monkeypatch.setenv("DEVELOP_ENABLED", "0")

    with pytest.raises(RuntimeError, match="DEVELOP_ENABLED"):
        await activities_module.dev_begin(_issue(39))


async def test_begin_reports_dispatch_mode(gh, monkeypatch):
    monkeypatch.setenv("DEVELOP_MODE", "dispatch")
    monkeypatch.delenv("DEVELOP_ENABLED", raising=False)

    plan = await activities_module.dev_begin(_issue(39))

    assert plan.mode == "dispatch"


async def test_dispatch_sends_strings_only_and_announces(gh, monkeypatch):
    """`workflow_dispatch` принимает только строки — число молча уронит прогон
    на стороне GitHub, где мы его уже не увидим."""
    monkeypatch.setenv("DEVELOP_MODE", "dispatch")

    await activities_module.dev_dispatch(_issue(39), "research/issue-39")

    assert len(gh["dispatch"]) == 1
    _, _, _, inputs = gh["dispatch"][0]
    assert all(isinstance(v, str) for v in inputs.values())
    assert gh["labels"] == ["in-development"]
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/test_develop_child.py -q`
Expected: FAIL — `ImportError: cannot import name 'DevelopPlan' from 'shared.workflow_types'`

- [ ] **Step 3: Добавить тип**

В `shared/workflow_types.py`, сразу после датакласса `AnalyzeInput`:

```python
@dataclass
class DevelopPlan:
    """Решения, принятые на входе в разработку, — один раз на стадию.

    Режим и ветка аналитики определяются активностью, а не воркфлоу: и то и
    другое читается из окружения и из GitHub, а решение воркфлоу обязано быть
    детерминированным при реплее. Результат активности лежит в истории, поэтому
    повтор возьмёт то же значение, что и первый прогон.
    """
    mode: str    # "local" | "dispatch" (`shared/develop.py`)
    branch: str  # ветка артефактов аналитики; "" — аналитики не было
```

- [ ] **Step 4: Добавить активности**

В `worker/activities.py`, сразу после `trigger_openhands_resolver` (после строки 1260, до `collect_dev_followups`):

```python
# --- Разработка дочерним воркфлоу: шаги как отдельные активности ---
#
# Прежде вся разработка была ОДНОЙ активностью, и её ретрай повторял четыре
# внутренних шага целиком. На прогоне #39 падал только `git push` — уже после
# работы агента, — а заново шёл весь прогон, и контур трижды объявил о передаче
# задачи. Лечение снижением до одной попытки отменяло ретраи и там, где они
# уместны. Разрезав стадию на активности, ретраи возвращаются туда, где дёшевы.
#
# Приватные `_dev_*` НЕ трогаем: по ним идёт `trigger_openhands_resolver`, а по
# нему — реплей прогонов, начатых до выкладки, и прежний линейный сценарий.


@activity.defn
async def dev_begin(issue: IssueInput) -> DevelopPlan:
    """Решения входа в стадию: работаем ли вообще, в каком режиме и от чего.

    Собрано в один шаг намеренно. Выключатель и наличие ветки читаются из
    окружения и из GitHub — в воркфлоу так нельзя, там решение обязано быть
    детерминированным при реплее. Один вызов вместо трёх ещё и делает вход в
    стадию одной строкой в истории.
    """
    if not develop.enabled():
        raise RuntimeError(
            "DEVELOP_ENABLED выключен — задача остаётся в очереди к разработчику")

    branch = f"research/issue-{issue.issue_number}"
    if not await asyncio.to_thread(github_client.branch_exists, issue.repo, branch):
        # Путь бага: аналитики не было, и ветки с артефактами тоже. Штатно —
        # агент работает от тела Issue, но знать об этом должен явно.
        branch = ""

    return DevelopPlan(mode=develop.mode(), branch=branch)


@activity.defn
async def dev_dispatch(issue: IssueInput, branch: str) -> None:
    """Режим `dispatch`: прогон уезжает в GitHub Actions.

    Своих шагов на этой стороне нет — отсюда и один вызов вместо цепочки.
    Результат придёт событием `pr-open` от внешнего агента.
    """
    await asyncio.to_thread(
        github_client.dispatch_workflow,
        issue.repo, develop.workflow_file(), develop.workflow_ref(),
        develop.dispatch_inputs(issue.issue_number, branch=branch),
    )
    await _dev_announce(issue, branch,
                        where="запустил OpenHands Resolver в GitHub Actions")
```

- [ ] **Step 5: Добавить импорт типа**

В `worker/activities.py` найти импорт из `shared.workflow_types` и добавить `DevelopPlan` в список (алфавитный порядок сохранить).

Run: `grep -n "from shared.workflow_types import" -A 20 worker/activities.py | head -25`

- [ ] **Step 6: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/test_develop_child.py -q`
Expected: PASS — 5 passed

- [ ] **Step 7: Прогнать все тесты — прежний путь не сломан**

Run: `.venv/bin/pytest -q`
Expected: PASS, число упавших 0

- [ ] **Step 8: Коммит**

```bash
git add shared/workflow_types.py worker/activities.py tests/test_develop_child.py
git commit -m "feat(develop): вход в стадию разработки отдельной активностью

Режим и ветка аналитики читаются из окружения и из GitHub — в воркфлоу так
нельзя, решение там обязано быть детерминированным при реплее. Результат
активности лежит в истории, поэтому повтор возьмёт то же значение."
```

---

### Task 3: Шаги разработки как активности

**Files:**
- Modify: `worker/activities.py` (добавить обёртки после `dev_dispatch`)
- Modify: `worker/worker.py:40-88` (регистрация активностей)
- Test: `tests/test_develop_child.py` (дополнить)

**Interfaces:**
- Consumes: `DevelopPlan` из Task 2; приватные `_dev_prepare`, `_dev_run_agent`, `_dev_tests`, `_dev_publish`, `_dev_announce`, `collect_dev_followups` — без изменений
- Produces:
  - `activities.dev_prepare(issue: IssueInput, branch: str) -> int` — длина постановки в символах
  - `activities.dev_announce(issue: IssueInput, branch: str) -> None`
  - `activities.dev_run_agent(issue: IssueInput) -> None` — бросает исключение при ненулевом коде
  - `activities.dev_followups(issue: IssueInput) -> list[int]` — номера заведённых SubIssue
  - `activities.dev_tests(issue: IssueInput) -> None`
  - `activities.dev_publish(issue: IssueInput, branch: str) -> int | None` — номер PR либо None

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_develop_child.py`:

```python
# --- границы шагов: что уезжает в историю воркфлоу ---

async def test_prepare_returns_a_size_not_the_task_text(gh, monkeypatch):
    """Между шагами едут числа и пути, не содержимое.

    Постановка уже лежит файлом в общем томе (`.task.md`), и дублировать её в
    payload Temporal незачем: на большой задаче требования дают сотни килобайт,
    а история воркфлоу — не хранилище документов. НФТ-01: потолок 4 КБ.
    """
    monkeypatch.setattr(activities_module, "_dev_prepare",
                        lambda issue, branch: "x" * 5000)

    size = await activities_module.dev_prepare(_issue(39), "research/issue-39")

    assert size == 5000
    assert isinstance(size, int)


async def test_publish_returns_the_pr_number(gh, monkeypatch):
    monkeypatch.setattr(activities_module, "_dev_publish",
                        lambda issue, branch: 101)

    assert await activities_module.dev_publish(_issue(39), "b") == 101


async def test_publish_returns_none_when_the_agent_changed_nothing(gh, monkeypatch):
    """`None` — не сбой шага, а его честный результат. Решение «открывать
    нечего» принимает воркфлоу: у него есть контекст стадии, у активности нет."""
    monkeypatch.setattr(activities_module, "_dev_publish",
                        lambda issue, branch: None)

    assert await activities_module.dev_publish(_issue(39), "b") is None


async def test_run_agent_raises_on_a_failed_run(gh, monkeypatch):
    """Ненулевой код раннера — сбой шага. Он обязан долететь до воркфлоу
    исключением: проглоченный, он дал бы пустой PR при доложенном успехе."""
    def boom(issue):
        raise RuntimeError("прогон агента разработки завершился с кодом 137")

    monkeypatch.setattr(activities_module, "_dev_run_agent", boom)

    with pytest.raises(RuntimeError, match="137"):
        await activities_module.dev_run_agent(_issue(39))


def test_all_dev_steps_are_registered_activities():
    """Шаг, не зарегистрированный в воркере, не вызовется из воркфлоу — и
    обнаружится это на живом прогоне, а не здесь."""
    import worker as worker_module

    names = worker_module.DEVELOP_ACTIVITIES
    expected = [activities_module.dev_begin, activities_module.dev_dispatch,
                activities_module.dev_prepare, activities_module.dev_announce,
                activities_module.dev_run_agent, activities_module.dev_followups,
                activities_module.dev_tests, activities_module.dev_publish]
    assert names == expected
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/test_develop_child.py -q`
Expected: FAIL — `AttributeError: module 'activities' has no attribute 'dev_prepare'`

- [ ] **Step 3: Добавить обёртки шагов**

В `worker/activities.py`, сразу после `dev_dispatch`:

```python
@activity.defn
async def dev_prepare(issue: IssueInput, branch: str) -> int:
    """Шаг 1: свежий клон и постановка файлом. Возвращает длину постановки.

    Длину, а не текст: постановка уже лежит в `.task.md` в общем томе, и
    дублировать её в payload Temporal незачем. В лог она уходит целиком — там
    её и смотрят, когда разбираются «почему агент сделал не то».
    """
    task = await _run_with_heartbeat(_dev_prepare, issue, branch, label="dev:prepare")
    logger.info("Develop %s#%s: постановка (%d симв.)\n%s",
                issue.repo, issue.issue_number, len(task), task[:2000])
    return len(task)


@activity.defn
async def dev_announce(issue: IssueInput, branch: str) -> None:
    """Шаг 2: метка и комментарий о начале работы — best-effort.

    Отдельным шагом, а не частью прогона: объявление обязано случиться ПОСЛЕ
    успешного клона (иначе контур скажет о работе, которая не началась) и ДО
    прогона агента (иначе человек двадцать минут не знает, что задача в работе).
    """
    await _dev_announce(issue, branch, where="запустил OpenHands на своём сервере")


@activity.defn
async def dev_run_agent(issue: IssueInput) -> None:
    """Шаг 3: прогон одноразового контейнера агента.

    Возврата нет: хвост вывода уходит в лог воркера на любом исходе
    (`_dev_run_agent`), а в историю воркфлоу ему не место — это килобайты
    текста на прогон.
    """
    await _run_with_heartbeat(_dev_run_agent, issue, label="dev:agent")


@activity.defn
async def dev_followups(issue: IssueInput) -> list[int]:
    """Шаг 4: находки агента — отдельными SubIssue.

    Идёт ДО тестов и публикации: файл находок обязан исчезнуть из рабочего
    дерева раньше коммита, иначе он уедет в PR — в ревью как мусор, а на
    следующем круге правок агент прочитает свои прошлые находки как новые.
    """
    return await collect_dev_followups(issue)


@activity.defn
async def dev_tests(issue: IssueInput) -> None:
    """Шаг 5: проверки проекта — до пуша.

    Красный код не должен доезжать до PR, а на PR от агента CI может и не
    запуститься: события от токена Actions не порождают прогонов.
    """
    await _run_with_heartbeat(_dev_tests, issue, label="dev:tests")


@activity.defn
async def dev_publish(issue: IssueInput, branch: str) -> int | None:
    """Шаг 6: коммит, пуш и PR — руками воркера, его токеном.

    `None` — агент не изменил ни одного файла. Это не сбой шага, а его
    результат; решение, что делать с пустым прогоном, принимает воркфлоу.
    """
    return await _run_with_heartbeat(_dev_publish, issue, branch, label="dev:publish")
```

- [ ] **Step 4: Зарегистрировать активности в воркере**

В `worker/worker.py` добавить перед `async def main()` (после строки 30):

```python
# Шаги разработки — списком, а не россыпью в конструкторе Worker: этот же
# список проверяет тест регистрации. Незарегистрированный шаг не вызовется из
# воркфлоу, и обнаружилось бы это на живом прогоне.
DEVELOP_ACTIVITIES = [
    activities.dev_begin,
    activities.dev_dispatch,
    activities.dev_prepare,
    activities.dev_announce,
    activities.dev_run_agent,
    activities.dev_followups,
    activities.dev_tests,
    activities.dev_publish,
]
```

В том же файле, в списке `activities=[...]`, заменить строку
`            activities.trigger_openhands_resolver,`
на:
```python
            activities.trigger_openhands_resolver,
            *DEVELOP_ACTIVITIES,
```

- [ ] **Step 5: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/test_develop_child.py -q`
Expected: PASS — 10 passed

- [ ] **Step 6: Прогнать все тесты**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 упавших

- [ ] **Step 7: Проверить, что воркер импортируется**

Run: `.venv/bin/python -c "import sys; sys.path[:0]=['worker','.']; import worker; print(len(worker.DEVELOP_ACTIVITIES))"`
Expected: `8`

- [ ] **Step 8: Коммит**

```bash
git add worker/activities.py worker/worker.py tests/test_develop_child.py
git commit -m "feat(develop): шаги разработки как отдельные активности

Прежде вся стадия была одной активностью, и её ретрай повторял четыре шага
целиком. На прогоне #39 падал только git push — уже после работы агента, — а
заново шёл весь прогон. Разрезав стадию, ретраи возвращаются туда, где дёшевы.

Приватные _dev_* не тронуты: по ним идёт trigger_openhands_resolver, а по нему
— реплей прогонов, начатых до выкладки."
```

---

### Task 4: Воркфлоу `IssueDevelopment`

**Files:**
- Modify: `worker/workflows.py` (добавить класс перед `IssueAnalysis`, строка ~1766)
- Modify: `worker/worker.py:38` (регистрация воркфлоу)
- Test: `tests/test_develop_workflow.py` (создать)

**Interfaces:**
- Consumes: активности из Task 2 и Task 3
- Produces: `IssueDevelopment.run(issue: IssueInput) -> int | None` — номер PR (режим `local`) либо `None` (режим `dispatch`)

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_develop_workflow.py`:

```python
"""IssueDevelopment: порядок шагов и раздельные ретраи.

Стадия разрезана на активности ради двух вещей сразу — видимости в Temporal и
осмысленных ретраев. Здесь проверяется вторая: срыв публикации повторяет
публикацию, а не двадцатиминутный прогон агента.
"""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shared.workflow_types import DevelopPlan, IssueInput
from workflows import IssueDevelopment

REPO = "o/r"
ISSUE = 39

_calls: list[str] = []
_fail_publish_times = 0


@activity.defn(name="dev_begin")
async def begin_local(issue: IssueInput) -> DevelopPlan:
    _calls.append("begin")
    return DevelopPlan(mode="local", branch="research/issue-39")


@activity.defn(name="dev_dispatch")
async def dispatch_stub(issue: IssueInput, branch: str) -> None:
    _calls.append("dispatch")


@activity.defn(name="dev_prepare")
async def prepare_ok(issue: IssueInput, branch: str) -> int:
    _calls.append("prepare")
    return 1780


@activity.defn(name="dev_announce")
async def announce_ok(issue: IssueInput, branch: str) -> None:
    _calls.append("announce")


@activity.defn(name="dev_run_agent")
async def agent_ok(issue: IssueInput) -> None:
    _calls.append("agent")


@activity.defn(name="dev_followups")
async def followups_ok(issue: IssueInput) -> list[int]:
    _calls.append("followups")
    return []


@activity.defn(name="dev_tests")
async def tests_ok(issue: IssueInput) -> None:
    _calls.append("tests")


@activity.defn(name="dev_publish")
async def publish_ok(issue: IssueInput, branch: str) -> int | None:
    _calls.append("publish")
    return 101


@activity.defn(name="dev_publish")
async def publish_flaky(issue: IssueInput, branch: str) -> int | None:
    """Публикация срывается дважды и удаётся с третьей — случай прогона #39."""
    global _fail_publish_times
    _calls.append("publish")
    _fail_publish_times += 1
    if _fail_publish_times < 3:
        raise RuntimeError("git push → код 1: protected branch")
    return 101


@activity.defn(name="dev_publish")
async def publish_empty(issue: IssueInput, branch: str) -> int | None:
    _calls.append("publish")
    return None


def _issue() -> IssueInput:
    return IssueInput(repo=REPO, issue_number=ISSUE, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


async def _run(env, tq, acts) -> int | None:
    async with Worker(env.client, task_queue=tq,
                      workflows=[IssueDevelopment], activities=acts):
        return await env.client.execute_workflow(
            IssueDevelopment.run, _issue(), id=f"wf-{uuid.uuid4()}", task_queue=tq)


@pytest.mark.timeout(60)
async def test_steps_run_in_the_order_that_protects_the_pr():
    """Находки собираются ПОСЛЕ агента и ДО тестов и публикации: файл находок
    обязан исчезнуть из рабочего дерева раньше коммита, иначе уедет в PR."""
    _calls.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        number = await _run(env, tq, [begin_local, dispatch_stub, prepare_ok,
                                      announce_ok, agent_ok, followups_ok,
                                      tests_ok, publish_ok])

    assert number == 101
    assert _calls == ["begin", "prepare", "announce", "agent",
                      "followups", "tests", "publish"]


@pytest.mark.timeout(60)
async def test_a_failed_push_retries_only_the_push():
    """Прогон #39: пуш падал на последнем шаге, а повторялась вся стадия вместе
    с агентом, и контур трижды объявил о передаче задачи."""
    global _fail_publish_times
    _calls.clear()
    _fail_publish_times = 0
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        number = await _run(env, tq, [begin_local, dispatch_stub, prepare_ok,
                                      announce_ok, agent_ok, followups_ok,
                                      tests_ok, publish_flaky])

    assert number == 101
    assert _calls.count("publish") == 3, "публикация не повторилась"
    assert _calls.count("agent") == 1, "агент прогнан заново — ровно дефект #39"
    assert _calls.count("announce") == 1, "контур объявил о передаче дважды"


@pytest.mark.timeout(60)
async def test_an_empty_run_is_a_loud_failure():
    """Прогон без единой правки — не успех: открывать нечего, и человек должен
    об этом узнать. Молчание здесь неотличимо от исправной работы."""
    _calls.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        with pytest.raises(Exception, match="ни одного файла"):
            await _run(env, tq, [begin_local, dispatch_stub, prepare_ok,
                                 announce_ok, agent_ok, followups_ok,
                                 tests_ok, publish_empty])


@pytest.mark.timeout(60)
async def test_dispatch_mode_skips_the_local_steps():
    """Режим `dispatch`: работа идёт на чужой стороне, шагов здесь нет.
    `None` — не сбой, а «жди события pr-open»."""
    _calls.clear()

    @activity.defn(name="dev_begin")
    async def begin_dispatch(issue: IssueInput) -> DevelopPlan:
        _calls.append("begin")
        return DevelopPlan(mode="dispatch", branch="research/issue-39")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        number = await _run(env, tq, [begin_dispatch, dispatch_stub, prepare_ok,
                                      announce_ok, agent_ok, followups_ok,
                                      tests_ok, publish_ok])

    assert number is None
    assert _calls == ["begin", "dispatch"]
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/test_develop_workflow.py -q`
Expected: FAIL — `ImportError: cannot import name 'IssueDevelopment' from 'workflows'`

- [ ] **Step 3: Добавить импорт типа в воркфлоу**

В `worker/workflows.py` в блоке `with workflow.unsafe.imports_passed_through():` добавить `DevelopPlan` в импорт из `shared.workflow_types` (алфавитный порядок: после `CommentAckInput`).

- [ ] **Step 4: Реализовать воркфлоу**

В `worker/workflows.py`, перед `@workflow.defn(name="IssueAnalysis")`:

```python
@workflow.defn(name="IssueDevelopment")
class IssueDevelopment:
    """Разработка по подготовленному Issue — дочерний прогон цикла.

    Отдельным воркфлоу, а не активностью, по двум причинам сразу.

    Первая — видимость. Активность внутри родителя не имеет своего
    WorkflowId: в `workflow list` строки нет, а после завершения не остаётся
    и следа — операционная история собиралась логами контейнера и `docker ps`.

    Вторая — ретраи. Одна активность на четыре шага повторялась целиком: на
    прогоне #39 падал только `git push`, уже после работы агента, а заново шёл
    весь прогон, и контур трижды объявил о передаче задачи. Здесь у каждого
    шага своя политика: дорогие и недетерминированные (агент, тесты) идут в
    одну попытку, дешёвые и повторяемые (клон, публикация) — в три.

    Идентификатор фиксирован (`develop-<repo>-<n>`), поэтому повторный запуск
    при идущем прогоне упирается в WorkflowAlreadyStarted, а не поднимает
    второго агента в тот же рабочий каталог.
    """

    @workflow.run
    async def run(self, issue: IssueInput) -> int | None:
        """Возвращает номер PR (`local`) либо None (`dispatch`).

        `None` родитель читает как «работа идёт на чужой стороне, жди события
        `pr-open`», а не как отказ.
        """
        cheap = RetryPolicy(maximum_attempts=3)
        # Одна попытка там, где шаг недетерминирован, идёт десятками минут и
        # стоит денег. Повтор такого инициирует человек, а не политика ретраев.
        once = RetryPolicy(maximum_attempts=1)

        plan = await workflow.execute_activity(
            activities.dev_begin, issue,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=cheap,
        )

        if plan.mode == "dispatch":
            await workflow.execute_activity(
                activities.dev_dispatch, args=[issue, plan.branch],
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=cheap,
            )
            return None

        # Порядок не косметический: сначала клон и постановка — они
        # единственные могут не состояться до того, как что-либо сказано
        # человеку.
        await workflow.execute_activity(
            activities.dev_prepare, args=[issue, plan.branch],
            start_to_close_timeout=timedelta(seconds=600),
            heartbeat_timeout=timedelta(seconds=300),
            retry_policy=cheap,
        )
        await workflow.execute_activity(
            activities.dev_announce, args=[issue, plan.branch],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=cheap,
        )
        await workflow.execute_activity(
            activities.dev_run_agent, issue,
            start_to_close_timeout=timedelta(seconds=3600),
            heartbeat_timeout=timedelta(seconds=300),
            retry_policy=once,
        )
        # Находки — ДО тестов и публикации: файл находок обязан исчезнуть из
        # рабочего дерева раньше коммита, иначе уедет в PR как мусор, а на
        # следующем круге правок агент прочитает свои прошлые находки как новые.
        await workflow.execute_activity(
            activities.dev_followups, issue,
            start_to_close_timeout=timedelta(seconds=300),
            retry_policy=cheap,
        )
        await workflow.execute_activity(
            activities.dev_tests, issue,
            start_to_close_timeout=timedelta(seconds=1800),
            heartbeat_timeout=timedelta(seconds=300),
            retry_policy=once,
        )
        number = await workflow.execute_activity(
            activities.dev_publish, args=[issue, plan.branch],
            start_to_close_timeout=timedelta(seconds=600),
            heartbeat_timeout=timedelta(seconds=300),
            retry_policy=cheap,
        )

        if number is None:
            raise ApplicationError("агент не изменил ни одного файла — открывать нечего")
        return number
```

- [ ] **Step 5: Добавить импорт `ApplicationError`**

В `worker/workflows.py` рядом с `from temporalio.exceptions import WorkflowAlreadyStartedError` заменить на:

```python
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
```

- [ ] **Step 6: Зарегистрировать воркфлоу**

В `worker/worker.py`: в импорте из `workflows` добавить `IssueDevelopment` (алфавитно — после `IssueAnalysis`), и в списке `workflows=[...]` заменить строку 38-39 на:

```python
        workflows=[IssueLifecycle, IssueAnalysis, IssueDevelopment, IssueEstimation,
                   ConsolidationWorkflow, WebhookAudit, OrphanAgentEvent, CommentAck],
```

- [ ] **Step 7: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/test_develop_workflow.py -q`
Expected: PASS — 4 passed

- [ ] **Step 8: Прогнать все тесты**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 упавших

- [ ] **Step 9: Коммит**

```bash
git add worker/workflows.py worker/worker.py tests/test_develop_workflow.py
git commit -m "feat(develop): IssueDevelopment как дочерний воркфлоу

Активность внутри родителя не имеет своего WorkflowId: в workflow list строки
нет, после завершения следа не остаётся — историю собирали логами контейнера.
Плюс раздельные ретраи: срыв публикации повторяет публикацию, а не прогон
агента (дефект прогона #39)."
```

---

### Task 5: Развилка `_start_development` под маркером патча

**Files:**
- Modify: `worker/workflows.py:1347-1391` (метод `_start_development`)
- Test: `tests/test_develop_is_a_child.py` (создать)

**Interfaces:**
- Consumes: `IssueDevelopment` (Task 4), `development_workflow_id` (Task 1)
- Produces: ничего нового наружу — меняется путь исполнения `IssueLifecycle`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_develop_is_a_child.py`:

```python
"""Разработка действительно становится дочерним прогоном цикла.

Тест воркфлоу (`test_develop_workflow.py`) проверяет порядок шагов, но не то,
что получилось в Temporal. Здесь поднимается настоящий `IssueLifecycle` и
проверяется результат: разработка видна как child владельца состояния Issue,
несёт канонический id и переживает завершение родителя.
"""

import uuid

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import ParentClosePolicy as ProtoParentClosePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shared import lifecycle
from shared.workflow_ids import development_workflow_id
from shared.workflow_types import (
    ClassificationResult,
    Deadlines,
    DevelopPlan,
    DuplicateResult,
    GateResult,
    IssueInput,
    PriorityResult,
    ProtocolState,
)
from workflows import IssueAnalysis, IssueDevelopment, IssueEstimation, IssueLifecycle

REPO = "o/r"
ISSUE = 39

_calls: list[str] = []


# --- границы триажа (та же раскладка, что в test_agents_as_children.py) ---

@activity.defn(name="mark_awaiting")
async def awaiting_stub(repo: str, issue_number: int, waiting=None) -> None: ...


@activity.defn(name="prefilter_bot_and_security")
async def prefilter_ok(issue: IssueInput, origin_agent: bool = False): return None


@activity.defn(name="read_protocol_state")
async def protocol_default(repo: str, issue_number: int) -> ProtocolState:
    return ProtocolState()


@activity.defn(name="read_open_questions")
async def no_open_questions(repo: str, branch: str) -> list[str]: return []


@activity.defn(name="read_deadlines")
async def deadlines_autostart() -> Deadlines:
    """Автостарт включён: тест о том, что разработка стартует дочерним прогоном,
    а не о том, кто принимает решение о сборке."""
    return Deadlines(decompose_enabled=False, develop_autostart=True)


@activity.defn(name="set_phase")
async def set_phase_stub(repo: str, issue_number: int, phase: str) -> None: ...


@activity.defn(name="intake_gate")
async def gate_ok(issue: IssueInput, thread: list[str]) -> GateResult:
    return GateResult(status="SUFFICIENT", content="")


@activity.defn(name="classify_issue")
async def classify_feature(issue: IssueInput) -> ClassificationResult:
    return ClassificationResult(label="advisor:feature-request", answer="ok")


@activity.defn(name="duplicate_check")
async def duplicate_none(issue: IssueInput) -> DuplicateResult:
    return DuplicateResult(decision="none", best_match_number=None,
                           probability=0.0, reason="", context_branch=None)


@activity.defn(name="score_priority")
async def score_p1(issue: IssueInput, c, d) -> PriorityResult:
    return PriorityResult(tier="P1", breakdown_markdown="разбор")


@activity.defn(name="post_priority_comment")
async def post_priority(issue: IssueInput, p, d) -> None: ...


@activity.defn(name="escalate_to_human")
async def escalate(issue: IssueInput, reason: str = "") -> None: ...


@activity.defn(name="post_error_label")
async def post_error(issue: IssueInput, reason: str) -> None:
    _calls.append(f"error:{reason[:40]}")


@activity.defn(name="mark_ready_for_dev")
async def ready(issue: IssueInput, priority_tier: str, branch: str) -> None: ...


# --- границы разработки ---

@activity.defn(name="dev_begin")
async def begin_local(issue: IssueInput) -> DevelopPlan:
    return DevelopPlan(mode="local", branch="research/issue-39")


@activity.defn(name="dev_dispatch")
async def dispatch_stub(issue: IssueInput, branch: str) -> None: ...


@activity.defn(name="dev_prepare")
async def prepare_ok(issue: IssueInput, branch: str) -> int: return 1780


@activity.defn(name="dev_announce")
async def announce_ok(issue: IssueInput, branch: str) -> None:
    _calls.append("announce")


@activity.defn(name="dev_run_agent")
async def agent_ok(issue: IssueInput) -> None:
    _calls.append("agent")


@activity.defn(name="dev_followups")
async def followups_ok(issue: IssueInput) -> list[int]: return []


@activity.defn(name="dev_tests")
async def tests_ok(issue: IssueInput) -> None: ...


@activity.defn(name="dev_publish")
async def publish_ok(issue: IssueInput, branch: str) -> int | None:
    _calls.append("publish")
    return 101


ALL_ACTIVITIES = [prefilter_ok, protocol_default, deadlines_autostart, set_phase_stub,
                  gate_ok, classify_feature, duplicate_none, score_p1, post_priority,
                  escalate, post_error, ready, no_open_questions, awaiting_stub,
                  begin_local, dispatch_stub, prepare_ok, announce_ok, agent_ok,
                  followups_ok, tests_ok, publish_ok]


def _issue() -> IssueInput:
    return IssueInput(repo=REPO, issue_number=ISSUE, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


def _worker(env, tq):
    return Worker(env.client, task_queue=tq,
                  workflows=[IssueLifecycle, IssueAnalysis, IssueEstimation,
                             IssueDevelopment],
                  activities=ALL_ACTIVITIES)


async def _await_phase(env, handle, expected: str) -> str:
    for _ in range(300):
        if await handle.query(IssueLifecycle.phase) == expected:
            break
        await env.sleep(1)
    return await handle.query(IssueLifecycle.phase)


async def _child_starts(handle) -> list:
    out = []
    async for ev in handle.fetch_history_events():
        if ev.HasField("start_child_workflow_execution_initiated_event_attributes"):
            out.append(ev.start_child_workflow_execution_initiated_event_attributes)
    return out


@pytest.mark.timeout(120)
async def test_development_runs_as_a_child_with_the_canonical_id():
    """Определение готовности FNR-6: разработка видна отдельным прогоном с
    предсказуемым id — по нему и восстанавливается операционная история."""
    _calls.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        async with _worker(env, tq):
            handle = await env.client.start_workflow(
                IssueLifecycle.run, _issue(), id=f"wf-{uuid.uuid4()}", task_queue=tq)
            await _await_phase(env, handle, lifecycle.PR_OPEN)
            children = await _child_starts(handle)

    ids = [c.workflow_id for c in children]
    assert development_workflow_id(REPO, ISSUE) in ids, (
        f"разработка не стала дочерним прогоном: {ids}"
    )
    assert "publish" in _calls, "цикл не дождался результата дочернего прогона"


@pytest.mark.timeout(120)
async def test_the_development_child_survives_the_parent():
    """Прогон агента идёт до 45 минут. Ни continue-as-new родителя, ни его
    завершение не должны его убивать — иначе дорогой прогон обрывается по
    причине, к нему не относящейся."""
    _calls.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        async with _worker(env, tq):
            handle = await env.client.start_workflow(
                IssueLifecycle.run, _issue(), id=f"wf-{uuid.uuid4()}", task_queue=tq)
            await _await_phase(env, handle, lifecycle.PR_OPEN)
            children = await _child_starts(handle)

    develop_children = [c for c in children
                        if c.workflow_id == development_workflow_id(REPO, ISSUE)]
    assert [c.parent_close_policy for c in develop_children] == [
        ProtoParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
    ], "дочерний прогон разработки погибнет вместе с родителем"
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/test_develop_is_a_child.py -q`
Expected: FAIL — разработка исполняется активностью, событий `start_child_workflow_execution_initiated` с id `develop-o/r-39` в истории нет

- [ ] **Step 3: Добавить импорт id**

В `worker/workflows.py` в блоке `imports_passed_through` заменить строку импорта на:

```python
    from shared.workflow_ids import (
        analysis_workflow_id,
        development_workflow_id,
        estimate_workflow_id,
    )
```

- [ ] **Step 4: Ввести развилку в `_start_development`**

В `worker/workflows.py` заменить тело `try:` в `_start_development` (строки 1348-1368) на:

```python
        try:
            if workflow.patched("issue-lifecycle-develop-child"):
                # Дочерний прогон: у стадии появляется свой WorkflowId, а
                # значит строка в `workflow list` и след, переживающий её
                # завершение. Одна попытка на уровне стадии — ретраи живут
                # внутри, на отдельных шагах.
                pr_number = await workflow.execute_child_workflow(
                    IssueDevelopment.run, issue,
                    id=development_workflow_id(issue.repo, issue.issue_number),
                    # Прогон агента идёт до 45 минут. Ни continue-as-new
                    # родителя, ни его завершение не должны его убивать.
                    parent_close_policy=ParentClosePolicy.ABANDON,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            else:
                pr_number = await workflow.execute_activity(
                    activities.trigger_openhands_resolver,
                    issue,
                    # Прогон агента идёт десятками минут, поэтому потолок общий
                    # на весь шаг, а живость сообщается heartbeat'ом.
                    start_to_close_timeout=timedelta(seconds=3600),
                    heartbeat_timeout=timedelta(seconds=300),
                    # Ретрай повторяет активность ЦЕЛИКОМ, включая прогон
                    # агента: на прогоне #39 контур трижды объявил о передаче
                    # задачи и трижды прогнал агента заново.
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
        except WorkflowAlreadyStartedError:
            # Разработка по этому Issue уже идёт — второй дорогой прогон не
            # нужен. Результата отсюда не видно (это чужой прогон), поэтому
            # фазу не двигаем: её сдвинет тот, кто прогон и запускал.
            workflow.logger.info("development already running for %s#%s",
                                 issue.repo, issue.issue_number)
            return (self._phase, self._stage, False)
        except Exception as e:
```

Остальное тело метода (обработка `Exception`, `pr_number is None`, возврат фазы) — **без изменений**.

- [ ] **Step 5: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/test_develop_is_a_child.py -q`
Expected: PASS — 2 passed

- [ ] **Step 6: Прогнать все тесты — прежний путь и реплей целы**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 упавших. Особое внимание: `tests/test_develop.py`, `tests/test_develop_autostart.py`, `tests/test_agents_as_children.py`

- [ ] **Step 7: Убедиться, что `_run_linear` не тронут**

Run: `git diff worker/workflows.py | grep -c "^-.*trigger_openhands_resolver"`
Expected: `0` — ни одна строка с вызовом старой активности не удалена

- [ ] **Step 8: Коммит**

```bash
git add worker/workflows.py tests/test_develop_is_a_child.py
git commit -m "feat(lifecycle): развилка на IssueDevelopment под маркером патча

Маркер обязателен: на стенде живут прогоны, начатые на прежнем коде, и
изменение решений воркфлоу без маркера роняет их недетерминизмом. Ветка без
патча ведёт прежним путём через trigger_openhands_resolver — по ней идёт
реплей этих прогонов и прежний линейный сценарий."
```

---

### Task 6: Круг правок как дочерний воркфлоу `IssuePrFix`

**Files:**
- Modify: `worker/workflows.py` (добавить класс после `IssueDevelopment`)
- Modify: `worker/workflows.py:1409-1425` (цикл в `_phase_pr_review`)
- Modify: `worker/worker.py:38` (регистрация)
- Test: `tests/test_pr_fix_child.py` (создать)

**Interfaces:**
- Consumes: `pr_fix_workflow_id` (Task 1), существующая активность `activities.run_pr_fix_round` — без изменений
- Produces: `IssuePrFix.run(repo: str, pr_number: int, round_number: int) -> bool | str`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_pr_fix_child.py`:

```python
"""Круг правок как дочерний прогон.

Круг обладает всеми признаками дорогой стадии: поднимает тот же образ раннера
тем же `develop.runner_command`, идёт до 2700 с и недетерминирован. Оставить его
активностью значило бы сохранить ровно ту неоднородность, которая породила
исходную проблему.
"""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shared.workflow_ids import pr_fix_workflow_id
from workflows import IssuePrFix

REPO = "o/r"
PR = 41

_rounds: list[int] = []


@activity.defn(name="run_pr_fix_round")
async def round_fixed(repo: str, pr_number: int, round_number: int):
    _rounds.append(round_number)
    return True


@activity.defn(name="run_pr_fix_round")
async def round_nothing_to_do(repo: str, pr_number: int, round_number: int):
    _rounds.append(round_number)
    return "замечаний, требующих правок в коде, нет"


async def _run(env, tq, act, round_number: int):
    async with Worker(env.client, task_queue=tq,
                      workflows=[IssuePrFix], activities=[act]):
        return await env.client.execute_workflow(
            IssuePrFix.run, args=[REPO, PR, round_number],
            id=pr_fix_workflow_id(REPO, PR, round_number), task_queue=tq)


@pytest.mark.timeout(60)
async def test_a_round_that_made_fixes_returns_true():
    """`True` — правки внесены и запрошена перепроверка."""
    _rounds.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        assert await _run(env, f"tq-{uuid.uuid4()}", round_fixed, 1) is True
        assert _rounds == [1]


@pytest.mark.timeout(60)
async def test_a_round_with_nothing_to_fix_returns_its_verdict():
    """Строка — правок не потребовалось, и это её разбор. Законный исход, а не
    сбой: сводить его к булеву значению значило бы потерять объяснение."""
    _rounds.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        out = await _run(env, f"tq-{uuid.uuid4()}", round_nothing_to_do, 1)

    assert isinstance(out, str) and "нет" in out


@pytest.mark.timeout(60)
async def test_each_round_gets_its_own_id():
    """Круги разделены ожиданием доклада ревью: без номера в ключе второй круг
    упирался бы в id первого, и доводка PR вставала бы после первого же круга."""
    _rounds.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        assert await _run(env, tq, round_fixed, 1) is True
        assert await _run(env, tq, round_fixed, 2) is True

    assert _rounds == [1, 2]
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/test_pr_fix_child.py -q`
Expected: FAIL — `ImportError: cannot import name 'IssuePrFix' from 'workflows'`

- [ ] **Step 3: Реализовать воркфлоу**

В `worker/workflows.py`, сразу после класса `IssueDevelopment`:

```python
@workflow.defn(name="IssuePrFix")
class IssuePrFix:
    """Один круг правок по замечаниям ревью — дочерний прогон цикла.

    Отдельный воркфлоу на КАЖДЫЙ круг, а не на цикл целиком: круги разделены
    ожиданием внешнего доклада ревью, и объединение их в один прогон дало бы
    воркфлоу, большую часть жизни простаивающий в ожидании чужого сигнала.
    Ожиданием по-прежнему управляет родитель — он владеет состоянием задачи.
    """

    @workflow.run
    async def run(self, repo: str, pr_number: int, round_number: int):
        """`True` — правки внесены и запрошена перепроверка. Строка — правок не
        потребовалось, и это её разбор.

        Разные типы возврата намеренно: «сделали» и «не потребовалось» — разные
        исходы, и сводить их к булеву значению значило бы потерять объяснение.
        """
        return await workflow.execute_activity(
            activities.run_pr_fix_round,
            args=[repo, pr_number, round_number],
            start_to_close_timeout=timedelta(seconds=3600),
            heartbeat_timeout=timedelta(seconds=300),
            # Круг недетерминирован и стоит денег: повтор инициирует следующая
            # итерация родителя, а не политика ретраев.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
```

- [ ] **Step 4: Добавить импорт id**

В `worker/workflows.py` в блоке `imports_passed_through` дополнить импорт:

```python
    from shared.workflow_ids import (
        analysis_workflow_id,
        development_workflow_id,
        estimate_workflow_id,
        pr_fix_workflow_id,
    )
```

- [ ] **Step 5: Ввести развилку в `_phase_pr_review`**

В `worker/workflows.py` заменить блок `try:` внутри `while` (строки 1413-1425) на:

```python
            try:
                if workflow.patched("issue-lifecycle-prfix-child"):
                    outcome = await workflow.execute_child_workflow(
                        IssuePrFix.run,
                        args=[issue.repo, self._pr_number, rounds],
                        id=pr_fix_workflow_id(issue.repo, self._pr_number, rounds),
                        # Круг идёт до 45 минут: завершение родителя не должно
                        # обрывать начатую доводку PR.
                        parent_close_policy=ParentClosePolicy.ABANDON,
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                else:
                    outcome = await workflow.execute_activity(
                        activities.run_pr_fix_round,
                        args=[issue.repo, self._pr_number, rounds],
                        start_to_close_timeout=timedelta(seconds=3600),
                        heartbeat_timeout=timedelta(seconds=300),
                        # Круг недетерминирован и стоит денег: повтор инициирует
                        # следующая итерация, а не политика ретраев.
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
```

Остальное тело цикла (`except Exception`, проверка `outcome is not True`, ожидание сигнала) — **без изменений**.

- [ ] **Step 6: Зарегистрировать воркфлоу**

В `worker/worker.py`: добавить `IssuePrFix` в импорт из `workflows` и в список:

```python
        workflows=[IssueLifecycle, IssueAnalysis, IssueDevelopment, IssueEstimation,
                   IssuePrFix, ConsolidationWorkflow, WebhookAudit, OrphanAgentEvent,
                   CommentAck],
```

- [ ] **Step 7: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/test_pr_fix_child.py -q`
Expected: PASS — 3 passed

- [ ] **Step 8: Прогнать все тесты**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 упавших. Особое внимание: `tests/test_pr_closing.py`

- [ ] **Step 9: Коммит**

```bash
git add worker/workflows.py worker/worker.py tests/test_pr_fix_child.py
git commit -m "feat(pr-closer): круг правок как дочерний воркфлоу

Круг поднимает тот же образ раннера и идёт до 45 минут — по свойствам он
идентичен разработке. Отдельный прогон на КАЖДЫЙ круг: круги разделены
ожиданием доклада ревью, и один прогон на цикл простаивал бы в ожидании
чужого сигнала."
```

---

### Task 7: Документация — как восстанавливать историю

**Files:**
- Modify: `README.md` (подраздел «Отслеживание прогона OpenHands (Develop)»)
- Modify: `sa_documentation/naming_conventions.md`

**Interfaces:**
- Consumes: идентификаторы из Task 1, воркфлоу из Task 4 и Task 6
- Produces: ничего исполняемого

- [ ] **Step 1: Переписать подраздел README**

В `README.md` найти подраздел `### Отслеживание прогона OpenHands (Develop)` и заменить первый абзац и первый пункт списка (про Temporal) на:

```markdown
### Отслеживание работ по Issue

Операционная история Issue восстанавливается **по идентификаторам прогонов**,
а не по дереву в Temporal UI. Причина: родитель делает continue-as-new при 800
событиях истории (`HISTORY_EVENT_THRESHOLD`), после чего меняется RunId, а
дочерние прогоны остаются привязаны к прежнему. Дерево — приятное следствие,
пока родитель не перезапустился; полноту даёт id.

| Прогон | Идентификатор | Что показывает |
|---|---|---|
| Родитель Issue | `issue-<repo>-<n>` | фаза, решения человека, сигналы, парковки |
| Аналитика (`/analyze`) | `analysis-<repo>-<n>` | цепочка FNR по стадиям |
| Оценка (`/estimate`) | `estimate-<repo>-<n>-<marker>` | расчёт трудоёмкости |
| Разработка (OpenHands) | `develop-<repo>-<n>` | клон, прогон агента, находки, тесты, публикация |
| Круг правок по ревью | `prfix-<repo>-<pr>-<round>` | одна итерация доводки PR |

Все форматы собираются в `shared/workflow_ids.py` — второго места нет
намеренно, иначе выборка по префиксу молча стала бы неполной.

```bash
docker exec <temporal-контейнер> temporal workflow list --query "WorkflowId between 'develop-' and 'develop-~'"
docker exec <temporal-контейнер> temporal workflow show --workflow-id develop-<repo>-<n>
```

`show` по стадии разработки даёт шаги событиями `ActivityTaskScheduled`:
`dev_begin` → `dev_prepare` → `dev_announce` → `dev_run_agent` →
`dev_followups` → `dev_tests` → `dev_publish`. У каждого шага своя политика
ретраев: агент и тесты идут в одну попытку, клон и публикация — в три.
```

Остальные пункты (логи воркера, контейнер-раннер, Sentry, метки в Issue) —
оставить как есть.

- [ ] **Step 2: Проверить, что раздел читается**

Run: `grep -n -A 30 "### Отслеживание работ по Issue" README.md | head -40`
Expected: таблица из пяти строк и блок с командами

- [ ] **Step 3: Обновить словарь терминов**

В `sa_documentation/naming_conventions.md` заменить строку про резолвер разработки на:

```markdown
| Резолвер разработки | `IssueDevelopment` (`worker/workflows.py`) | Дочерний воркфлоу, id `develop-<repo>-<n>`; шаги — активности `dev_*`. Прежний путь `trigger_openhands_resolver` сохранён для реплея прогонов старого поколения |
| Круг правок | `IssuePrFix` (`worker/workflows.py`) | Дочерний воркфлоу, id `prfix-<repo>-<pr>-<round>`; один прогон на круг |
```

- [ ] **Step 4: Коммит**

```bash
git add README.md sa_documentation/naming_conventions.md
git commit -m "docs: восстановление истории Issue по идентификаторам прогонов

Дерево дочерних прогонов в UI привязано к RunId, а родитель делает
continue-as-new — полноту даёт идентификатор, и это записано явно, иначе
процедура воспроизводится только автором изменений."
```

---

## Выкладка на стенд

Двумя этапами — так требует вердикт дебатов: сначала разработка и проверка живым Issue, затем круг правок.

**Этап A — после Task 5:**

```bash
SHA=$(git rev-parse HEAD)
ssh poh-stand "cd /etc/dokploy/compose/compose-connect-redundant-system-mzso3q/code/harness && \
  sed -i 's|^ISSUE_AGENT_CONTEXT=.*|ISSUE_AGENT_CONTEXT=https://github.com/po-helper-org/poh-issue-agents.git#$SHA|' .env && \
  docker compose build issue-webhook issue-worker && docker compose up -d issue-webhook issue-worker"
```

Пин **обязательно на полный SHA**: BuildKit кэширует git-клон по URL и после нового коммита в ту же ветку молча собирает прежний код.

Проверка:
1. `docker logs <worker> | grep -ci nondetermin` → `0`
2. Завести живой Issue в `po-helper-org/poh-demo-checkout`, дождаться разработки
3. `temporal workflow list` содержит строку `develop-po-helper-org/poh-demo-checkout-<n>`
4. Прогоны, начатые до выкладки, продолжают реагировать на метки

**Этап B — после Task 6:** та же процедура, проверка кругом правок на живом PR.

---

## Self-Review

**Покрытие спеки:**

| Требование спеки | Задача плана |
|---|---|
| 4.1.1 Идентификаторы стадий | Task 1 |
| 4.2.1 Шаги разработки как активности | Task 2 (вход), Task 3 (шаги) |
| 4.2.2 Воркфлоу `IssueDevelopment` | Task 4 |
| 4.2.3 Развилка `_start_development` под маркером | Task 5 |
| 4.3.1 `IssuePrFix` и развилка `_phase_pr_review` | Task 6 |
| 4.4.1 Документация восстановления истории | Task 7 |
| НФТ-01 (payload ≤ 4 КБ) | Task 3, Step 1 — тест на возврат длины вместо текста |
| НФТ-02 (`max_concurrent_activities=3`) | Global Constraints — запрет на изменение |
| НФТ-04 (нет недетерминизма) | Task 5 Step 6-7, выкладка Этап A п.1 |
| НФТ-06 (`ABANDON`) | Task 5, Step 1 — тест `test_the_development_child_survives_the_parent` |
| НФТ-07 (таймаут раннера) | Global Constraints — `shared/develop.py` не менять |
| Этапы миграции 1–6 | Task 1–7 + раздел «Выкладка на стенд» |

Пробелов нет.

**Согласованность типов:** `DevelopPlan(mode, branch)` объявлен в Task 2 и используется в Task 4 и Task 5 с теми же полями. `dev_publish` возвращает `int | None` в Task 3 и так же читается в Task 4. `IssuePrFix.run` возвращает `bool | str` в Task 6 — совпадает с контрактом `run_pr_fix_round` (`worker/activities.py:1464-1476`).

**Плейсхолдеры:** отсутствуют — каждый шаг содержит исполнимый код или команду с ожидаемым выводом.
