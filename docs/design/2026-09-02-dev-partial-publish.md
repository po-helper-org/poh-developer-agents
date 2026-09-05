# Частичная выкладка разработки — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сорванный прогон разработки оставляет материал для разбора — ветку и черновой пул-реквест с тем, что успел написать агент, — вместо пустоты.

**Architecture:** Перехват вокруг шагов после агента в `IssueDevelopment` зовёт активность частичной выкладки; та переиспользует `publish_worktree`, научив её признаку черновика. Исходная причина отказа остаётся источником правды и не подменяется неудачей выкладки. Отдельным пул-реквестом в `po-helper-org/poh-pr-agents` черновики исключаются из обхода свипера, чтобы ревью на них не тратилось.

**Tech Stack:** Python 3.12, Temporal Python SDK, pytest, GitHub REST через `worker/github_client.py`.

## Global Constraints

- Тесты гонять командой репозитория: `python -m pytest -q`. Порог покрытия — 83%, красный прогон в PR не отдаём.
- **Раскладка:** `worker/` и `webhook/` НЕ пакеты — Dockerfile расплющивает их в `/app`. Импорт `from worker.X import ...` падает в контейнере. Внутри воркера — `import activities`, `import github_client`.
- **Фикстура pytest из чужого тестового модуля не видна** — `conftest.py` либо своя копия.
- **Правка решения воркфлоу требует `workflow.patched(...)`.** Гвард `tests/test_workflow_replay.py` обязан оставаться зелёным; прогонять отдельным шагом.
- **Телеметрия и спасение работы не имеют права ронять то, о чём отчитываются**: отказ частичной выкладки не заменяет и не скрывает исходную причину.
- **Каждый вызов активности передаёт ВСЕ её аргументы**, включая те, у которых есть умолчание: при несовпадении числа Temporal выбрасывает типы и отдаёт словари. Гвард — `tests/test_activity_arg_types.py`.
- Спецификация: `docs/superpowers/specs/2026-09-02-dev-partial-publish-design.md`, требования D1–D14.

## Что уже есть

- `publish_worktree(repo, clone_dir, branch, *, title, body, message, ignore_for_empty_check=(), force_include=())` (`worker/github_client.py:674`) — коммит, пуш и пул-реквест одной функцией. Пустое дерево даёт `None`. Ответ `422 already exists` уже обрабатывается: находит открытый пул-реквест и возвращает его номер (`:812`).
- `publish_analysis_partial(analyze, reason) -> list[str]` (`worker/activities.py:2529`) — образец «спасти сделанное при срыве».
- Шаги разработки в `IssueDevelopment` (`worker/workflows.py:3587-3661`): `dev_prepare`, `dev_announce`, `build_mvp_plan`, `dev_run_agent`, `dev_followups`, `dev_tests`, `dev_publish`; `finally` делает только `capture_episode`.
- `publish_worktree` у GitLab-клиента **не существует** — выкладка разработки только для GitHub (D3).

---

### Task 1: Признак черновика у выкладки

Закрывает D2, D3 в части клиента; сторожит D5.

**Files:**
- Modify: `worker/github_client.py:674` (`publish_worktree`), `:807` (создание пул-реквеста)
- Test: тесты клиента (найди существующий файл, новый рядом не заводи)

**Interfaces:**
- Consumes: ничего
- Produces: `publish_worktree(..., draft: bool = False)` — при `draft=True` пул-реквест создаётся черновым

- [ ] **Step 1: Написать падающие тесты**

```python
def test_publish_worktree_opens_normal_pr_by_default(monkeypatch, tmp_path):
    """Штатная выкладка не должна стать черновой — это регрессия видимости."""
    sent = {}

    class _Resp:
        status_code = 201
        text = ""
        def raise_for_status(self): pass
        def json(self): return {"number": 7}

    monkeypatch.setattr(github_client.requests, "post",
                        lambda url, **kw: sent.update(body=kw.get("json")) or _Resp())
    monkeypatch.setattr(github_client, "_auth_headers", lambda repo: {})
    monkeypatch.setattr(github_client, "_default_branch", lambda repo: "main")
    _stub_git_with_changes(monkeypatch, tmp_path)

    github_client.publish_worktree("o/r", str(tmp_path), "feature/1-openhands",
                                   title="t", body="b", message="m")

    assert sent["body"].get("draft") in (False, None)


def test_publish_worktree_opens_draft_when_asked(monkeypatch, tmp_path):
    """Сорванный прогон выкладывается черновиком: работа заведомо негодная,
    и обычный пул-реквест выглядел бы кандидатом на слияние."""
    sent = {}

    class _Resp:
        status_code = 201
        text = ""
        def raise_for_status(self): pass
        def json(self): return {"number": 7}

    monkeypatch.setattr(github_client.requests, "post",
                        lambda url, **kw: sent.update(body=kw.get("json")) or _Resp())
    monkeypatch.setattr(github_client, "_auth_headers", lambda repo: {})
    monkeypatch.setattr(github_client, "_default_branch", lambda repo: "main")
    _stub_git_with_changes(monkeypatch, tmp_path)

    github_client.publish_worktree("o/r", str(tmp_path), "feature/1-openhands",
                                   title="t", body="b", message="m", draft=True)

    assert sent["body"]["draft"] is True


def test_publish_worktree_empty_worktree_returns_none_with_draft(monkeypatch, tmp_path):
    """Пустое дерево остаётся пустым исходом и с черновиком (D4)."""
    _stub_git_without_changes(monkeypatch, tmp_path)
    assert github_client.publish_worktree("o/r", str(tmp_path), "b",
                                          title="t", body="b", message="m",
                                          draft=True) is None


def test_publish_worktree_reuses_existing_pr_with_draft(monkeypatch, tmp_path):
    """Повторный прогон обновляет прежний черновик, а не плодит второй (D5).

    Обработка `422 already exists` есть уже сегодня (`worker/github_client.py:812`).
    Тест здесь ради того, чтобы признак черновика её не сломал: сорванные
    прогоны повторяются часто, и второй пул-реквест на ту же ветку — мусор.
    """
    class _Conflict:
        status_code = 422
        text = '{"errors":[{"message":"A pull request already exists"}]}'
        def raise_for_status(self): raise AssertionError("не должно дойти сюда")

    class _Existing:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"number": 13}]

    monkeypatch.setattr(github_client.requests, "post", lambda url, **kw: _Conflict())
    monkeypatch.setattr(github_client.requests, "get", lambda url, **kw: _Existing())
    monkeypatch.setattr(github_client, "_auth_headers", lambda repo: {})
    monkeypatch.setattr(github_client, "_default_branch", lambda repo: "main")
    _stub_git_with_changes(monkeypatch, tmp_path)

    assert github_client.publish_worktree("o/r", str(tmp_path), "feature/166-openhands",
                                          title="t", body="b", message="m",
                                          draft=True) == 13
```

В последнем тесте `_default_branch` подменена намеренно: `requests.get` там занят ответом про существующий пул-реквест и настоящий `_default_branch` получил бы не тот ответ.

Вспомогательные `_stub_git_with_changes` / `_stub_git_without_changes` напиши по образцу уже существующих тестов `publish_worktree` в этом файле: посмотри, как они подменяют вызовы git, и повтори тем же приёмом.

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest -q -p no:randomly --no-cov -k "publish_worktree and draft"`
Expected: FAIL — `publish_worktree() got an unexpected keyword argument 'draft'`

- [ ] **Step 3: Добавить признак**

В сигнатуру `publish_worktree` добавить `draft: bool = False` (в блок именованных, рядом с `force_include`), а в тело запроса создания пул-реквеста — поле `draft`:

```python
        json={"title": title, "head": branch, "base": _default_branch(repo),
              "body": body, "draft": draft},
```

В докстринге объяснить причину:

```
    `draft=True` — пул-реквест открывается черновым. Так выкладывается работа
    СОРВАВШЕГОСЯ прогона: она заведомо негодная, а обычный пул-реквест выглядел
    бы кандидатом на слияние. Черновик к тому же не подбирается ревью — ни
    вебхуком PR-Agent, ни его свипером (см. правку в poh-pr-agents).
```

- [ ] **Step 4: Прогнать и убедиться, что прошло**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 5: Коммит**

```bash
git add worker/github_client.py tests/
git commit -m "feat(publish): признак черновика у выкладки рабочего дерева"
```

---

### Task 2: Активность частичной выкладки

Закрывает D1, D4, D6, D7, D8 в части исполнения.

**Files:**
- Modify: `worker/activities.py`
- Modify: `worker/worker.py`
- Test: `tests/test_dev_partial_publish.py`

**Interfaces:**
- Consumes: `publish_worktree(..., draft=True)` из Task 1
- Produces: активность `dev_publish_partial(issue, branch: str, reason: str) -> int | None` — номер чернового пул-реквеста; `None`, если сохранять нечего или выложить не удалось

- [ ] **Step 1: Написать падающие тесты**

```python
import pytest

import activities as a
from shared.workflow_types import IssueInput


def _issue():
    return IssueInput(repo="o/r", issue_number=166, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


async def test_partial_publish_opens_draft_and_reports(monkeypatch):
    """Сорванный прогон оставляет материал для разбора (D1).

    Отказ, ради которого написано: poh-demo-checkout#166 — тринадцать минут
    работы агента и три красных теста, после которых не осталось ни ветки,
    ни диффа.
    """
    published = {}
    posted = []
    monkeypatch.setattr(a.github_client, "publish_worktree",
                        lambda repo, clone, branch, **kw:
                            published.update(branch=branch, draft=kw.get("draft")) or 42)
    monkeypatch.setattr(a.github_client, "post_comment",
                        lambda repo, number, body: posted.append(body))

    number = await a.dev_publish_partial(_issue(), "feature/166-openhands",
                                         "проверки не прошли (код 1): # fail 3")

    assert number == 42
    assert published["draft"] is True
    assert published["branch"] == "feature/166-openhands"
    assert "42" in posted[0]
    assert "fail 3" in posted[0]


async def test_partial_publish_names_failure_not_readiness(monkeypatch):
    """Комментарий не обещает готовности (D6): работа заведомо негодная."""
    posted = []
    monkeypatch.setattr(a.github_client, "publish_worktree",
                        lambda repo, clone, branch, **kw: 42)
    monkeypatch.setattr(a.github_client, "post_comment",
                        lambda repo, number, body: posted.append(body))

    await a.dev_publish_partial(_issue(), "feature/166-openhands", "причина")

    text = posted[0].lower()
    assert "сорва" in text
    assert "готово к ревью" not in text


async def test_partial_publish_says_nothing_when_nothing_to_save(monkeypatch):
    """Агент не изменил ни одного файла — сохранять нечего (D4)."""
    posted = []
    monkeypatch.setattr(a.github_client, "publish_worktree",
                        lambda repo, clone, branch, **kw: None)
    monkeypatch.setattr(a.github_client, "post_comment",
                        lambda repo, number, body: posted.append(body))

    assert await a.dev_publish_partial(_issue(), "feature/166-openhands", "причина") is None
    assert posted == []


async def test_partial_publish_failure_does_not_raise(monkeypatch):
    """Отказ выкладки не роняет то, о чём она отчитывается (D8)."""
    def boom(*args, **kwargs):
        raise RuntimeError("git push отказал")

    monkeypatch.setattr(a.github_client, "publish_worktree", boom)
    monkeypatch.setattr(a.github_client, "post_comment", lambda *args: None)

    assert await a.dev_publish_partial(_issue(), "feature/166-openhands", "причина") is None
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_dev_partial_publish.py -q -p no:randomly --no-cov`
Expected: FAIL с `AttributeError: module 'activities' has no attribute 'dev_publish_partial'`

- [ ] **Step 3: Написать активность**

Штатная выкладка `_dev_publish` (`worker/activities.py:3282`) снимает служебные
файлы, оставляет `.harness/` и зовёт `publish_worktree` с готовыми заголовком и
телом. Частичная делает то же самое: иначе в черновик уедет мусор, который
штатная вычищает.

В `worker/activities.py` рядом с `_dev_publish`:

```python
def _dev_publish_partial(issue: IssueInput, branch: str, reason: str) -> int | None:
    """Выложить черновиком то, что агент успел написать до срыва.

    Повторяет подготовку дерева из `_dev_publish` — снятие служебных файлов и
    сохранение `.harness/`: иначе в черновик уедет постановка, а гвард «есть ли
    дифф» обманется ею же.

    Черновик, а не обычный пул-реквест: работа заведомо негодная, прогон
    сорвался. Обычный выглядел бы кандидатом на слияние, а ревью подобрало бы
    его и потратило бюджет на то, что контур сам признал негодным.
    """
    root, clone_dir = _dev_paths(issue)
    removed = develop.clear_service_files(clone_dir, keep_dir=root)
    if removed:
        logger.info("Develop %s#%s: сняты служебные файлы: %s",
                    issue.repo, issue.issue_number, ", ".join(removed))
    work = develop.work_branch(issue.issue_number)
    return github_client.publish_worktree(
        issue.repo, str(clone_dir), work,
        title=f"СОРВАЛОСЬ feat(#{issue.issue_number}): {issue.title}",
        body=("Прогон разработки **сорвался**. Это не готовая работа, а то, что "
              "агент успел написать до срыва — материал для разбора.\n\n"
              f"Причина:\n\n```\n{reason}\n```\n"),
        message=f"wip(#{issue.issue_number}): прогон сорвался, сохранено как есть",
        ignore_for_empty_check=(f"{task_context.DIR}/**",),
        force_include=(task_context.DIR,),
        draft=True,
    )


@activity.defn
async def dev_publish_partial(issue: IssueInput, branch: str,
                              reason: str) -> int | None:
    """Спасти работу сорвавшегося прогона черновым пул-реквестом.

    `None` — сохранять нечего (агент не тронул ни одного файла) либо выложить
    не удалось. И то, и другое — не повод падать: прогон УЖЕ сорвался, и
    спасательный шаг не имеет права подменить собой его причину.

    Отказ, ради которого написано: на poh-demo-checkout#166 тринадцать минут
    работы агента исчезли из-за трёх красных тестов — `dev_publish` идёт после
    `dev_tests` и просто не выполнился.
    """
    try:
        number = await asyncio.to_thread(_dev_publish_partial, issue, branch, reason)
    except Exception as exc:                              # noqa: BLE001
        activity.logger.warning("частичная выкладка не удалась: %s", exc)
        return None
    if number is None:
        # Агент не изменил ни одного файла. Комментария нет намеренно: сообщать
        # человеку не о чем, а лишняя строка в ленте — шум.
        return None
    await asyncio.to_thread(
        github_client.post_comment, issue.repo, issue.issue_number,
        f"## ⏸ Прогон разработки сорвался\n\n"
        f"Сохранил то, что успел написать агент, — черновым пул-реквестом "
        f"#{number}. Это **не готовая работа**, а материал для разбора.\n\n"
        f"Причина:\n\n```\n{reason}\n```\n\n"
        f"Ревью на черновик не тратится: снимите статус черновика, когда "
        f"работа станет годной.")
    return number
```

Зарегистрировать `activities.dev_publish_partial` в `worker/worker.py` рядом с прочими активностями разработки.

Проверь фактом, что `asyncio`, `develop` и `task_context` в модуле уже импортированы — `_dev_publish` их использует, значит должны быть.

- [ ] **Step 4: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_dev_partial_publish.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 5: Прогнать весь набор**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 6: Коммит**

```bash
git add worker/activities.py worker/worker.py tests/test_dev_partial_publish.py
git commit -m "feat(develop): активность частичной выкладки сорванного прогона"
```

---

### Task 3: Прогон разработки спасает работу при срыве

Закрывает D1, D8, D12, D13, D14. **Правка решений воркфлоу — обязателен `workflow.patched`.**

**Files:**
- Modify: `worker/workflows.py:3587-3665` (тело `try` в `IssueDevelopment`)
- Test: `tests/test_dev_partial_publish_workflow.py`
- Test: `tests/test_workflow_replay.py` (прогнать без правок)

**Interfaces:**
- Consumes: `dev_publish_partial(issue, branch, reason) -> int | None` из Task 2
- Produces: ничего для следующих задач

- [ ] **Step 1: Написать падающие тесты**

Заглушки активностей разработки копируй ВЕРБАТИМ из `tests/test_develop_workflow.py` — фикстуры чужого тестового модуля не видны. Ниже — только то, что относится к спасению работы.

```python
"""Сорванный прогон разработки выкладывает то, что успел написать агент.

Отказ, ради которого написано: poh-demo-checkout#166 — три красных теста из
семидесяти трёх, и тринадцать минут работы агента исчезли без следа.
"""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shared.workflow_types import DevelopPlan, IssueInput
from workflows import IssueDevelopment

# --- сюда скопировать блок заглушек из tests/test_develop_workflow.py ---

_calls: list[str] = []
_partial: list[tuple[str, str]] = []


@activity.defn(name="dev_tests")
async def tests_fail(issue: IssueInput) -> None:
    _calls.append("tests")
    raise RuntimeError("проверки не прошли (код 1):\n# fail 3")


@activity.defn(name="dev_followups")
async def followups_fail(issue: IssueInput) -> list[int]:
    _calls.append("followups")
    raise RuntimeError("сбор находок сорвался")


@activity.defn(name="dev_run_agent")
async def agent_fails(issue: IssueInput) -> None:
    _calls.append("agent")
    raise RuntimeError("агент упал")


@activity.defn(name="dev_prepare")
async def prepare_fails(issue: IssueInput, branch: str) -> int:
    _calls.append("prepare")
    raise RuntimeError("подготовка сорвалась")


@activity.defn(name="dev_publish_partial")
async def partial_stub(issue: IssueInput, branch: str, reason: str) -> int | None:
    _calls.append("partial")
    _partial.append((branch, reason))
    return 42


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=166, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


async def _run(env, acts):
    tq = f"tq-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=tq, workflows=[IssueDevelopment],
                      activities=acts):
        with pytest.raises(Exception):
            await env.client.execute_workflow(
                IssueDevelopment.run, _issue(), id=f"wf-{uuid.uuid4()}", task_queue=tq)


@pytest.mark.asyncio
async def test_red_tests_save_the_work():
    """Красные тесты — ветка и черновик всё равно появляются (D1)."""
    _calls.clear(); _partial.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, [*BASE_ACTIVITIES, tests_fail, partial_stub])

    assert "partial" in _calls
    assert "fail 3" in _partial[0][1], "причина должна доехать до выкладки"


@pytest.mark.asyncio
async def test_followups_failure_saves_the_work():
    """Сбой сбора находок — тот же случай: агент уже написал (D1)."""
    _calls.clear(); _partial.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, [*BASE_ACTIVITIES, followups_fail, partial_stub])

    assert "partial" in _calls


@pytest.mark.asyncio
async def test_failure_before_the_agent_saves_nothing():
    """До агента изменений нет — публиковать нечего (D1).

    Иначе контур открывал бы пустые черновики на каждый отказ подготовки.
    """
    _calls.clear(); _partial.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, [*BASE_ACTIVITIES, prepare_fails, partial_stub])

    assert "partial" not in _calls


@pytest.mark.asyncio
async def test_agent_failure_saves_the_work():
    """Агент упал посреди правки — написанное всё равно сохраняем.

    Это материал для разбора, а не готовая работа; комментарий обязан
    говорить именно так.
    """
    _calls.clear(); _partial.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, [*BASE_ACTIVITIES, agent_fails, partial_stub])

    assert "partial" in _calls


@pytest.mark.asyncio
async def test_partial_publish_failure_keeps_the_original_reason():
    """Отказ выкладки не подменяет исходную причину (D8).

    Прогон обязан упасть с ТОЙ ЖЕ ошибкой, что была на самом деле, а не с
    ошибкой спасательного шага — иначе первопричина исчезает.
    """
    _calls.clear()

    @activity.defn(name="dev_publish_partial")
    async def partial_fails(issue: IssueInput, branch: str, reason: str) -> int | None:
        _calls.append("partial")
        raise RuntimeError("выкладка тоже отказала")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"tq-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=tq, workflows=[IssueDevelopment],
                          activities=[*BASE_ACTIVITIES, tests_fail, partial_fails]):
            with pytest.raises(Exception) as excinfo:
                await env.client.execute_workflow(
                    IssueDevelopment.run, _issue(), id=f"wf-{uuid.uuid4()}",
                    task_queue=tq)

    assert "fail 3" in str(excinfo.value), \
        "наружу должна уйти исходная причина, а не отказ спасательного шага"


@pytest.mark.asyncio
async def test_draft_does_not_mean_progress():
    """Черновик не двигает задачу вперёд (D13, D14).

    Задача остаётся в разборе у человека: фаза `failed`, запись эпизода —
    как и сегодня. Иначе спасение работы превратилось бы в тихое
    «всё хорошо», а именно этот класс подмены в контуре уже случался.
    """
    _calls.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, [*BASE_ACTIVITIES, tests_fail, partial_stub])

    # Фаза: прогон обязан упасть — `_run` этого и ждёт (`pytest.raises`),
    # значит `IssueLifecycle` увидит отказ и поставит `failed` как прежде.
    assert _calls.count("capture_episode") == 1, \
        "запись эпизода делается ровно один раз, спасение её не дублирует"
    assert _calls.index("partial") < _calls.index("capture_episode"), \
        "выкладка идёт до записи эпизода — эпизод пишется в finally"
```

Заглушку `capture_episode` возьми из блока, скопированного из
`tests/test_develop_workflow.py`, и допиши в неё `_calls.append("capture_episode")`
— перечень `_calls` служит здесь единственным следом порядка шагов.

`BASE_ACTIVITIES` — перечень успешных заглушек из `tests/test_develop_workflow.py`, собери его там же.

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_dev_partial_publish_workflow.py -q -p no:randomly --no-cov`
Expected: FAIL — активность частичной выкладки не вызывается.

- [ ] **Step 3: Поставить перехват**

В `worker/workflows.py` обернуть шаги ПОСЛЕ `dev_run_agent` так, чтобы отказ любого из них вызывал частичную выкладку, а затем поднимал ИСХОДНУЮ ошибку дальше.

Признак «агент уже отработал» веди явным флагом, а не выводи из вида исключения: вид отказа и наличие изменений — разные вещи, и связывать их значит вернуться к тому же дефекту с другой стороны.

```python
        # Сорванный прогон обязан оставить материал для разбора. Маркер
        # обязателен: у идущих прогонов разработки в истории на этом месте
        # нет ни перехвата, ни активности выкладки, и реплей упал бы
        # недетерминизмом.
        agent_ran = False
        try:
            ...
            await workflow.execute_activity(activities.dev_run_agent, ...)
            agent_ran = True
            ...
        except Exception as exc:
            # Спасение работы НЕ подменяет причину: наружу уходит исходное
            # исключение, а неудача самой выкладки только пишется в лог.
            if agent_ran and workflow.patched("issue-development-partial-publish"):
                try:
                    await workflow.execute_activity(
                        activities.dev_publish_partial,
                        args=[issue, plan.branch, _failure_reason(exc)[:1500]],
                        start_to_close_timeout=timedelta(seconds=600),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                except Exception as save_exc:
                    workflow.logger.warning(
                        "частичная выкладка не удалась: %s", save_exc)
            raise
        finally:
            ...
```

Точное место и форму подгони под то, что реально в файле: `finally` с записью эпизода уже есть, и новый `except` обязан встать перед ним, не сломав существующее поведение. `_failure_reason` в модуле уже есть — используй её, а не собирай текст заново.

- [ ] **Step 4: Прогнать тесты спасения**

Run: `python -m pytest tests/test_dev_partial_publish_workflow.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 5: Прогнать гвард реплея — главная проверка задачи**

Run: `python -m pytest tests/test_workflow_replay.py -q -p no:randomly --no-cov`
Expected: PASS. Красный гвард означает, что маркер поставлен не там или не поставлен вовсе, и выкладка убьёт идущие прогоны разработки. Чинить, а не обходить.

- [ ] **Step 6: Прогнать весь набор**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 7: Коммит**

```bash
git add worker/workflows.py tests/test_dev_partial_publish_workflow.py
git commit -m "feat(develop): сорванный прогон выкладывает работу агента черновиком

Маркер issue-development-partial-publish обязателен: у идущих прогонов
в истории на этом месте нет ни перехвата, ни активности выкладки."
```

---

### Task 4: Свипер PR-Agent не подбирает черновики

Закрывает D9, D10. **Другой репозиторий.**

**Files:**
- Modify: `self-hosted/reliability/sweeper_adapter.py` в `po-helper-org/poh-pr-agents` (`parse_open_prs`)
- Test: `self-hosted/reliability/tests/test_sweeper_adapter.py` там же

**Interfaces:**
- Consumes: ничего из этого репозитория
- Produces: ничего для следующих задач

- [ ] **Step 1: Завести рабочую копию соседнего репозитория**

```bash
git clone https://github.com/po-helper-org/poh-pr-agents.git /tmp/poh-pr-agents
cd /tmp/poh-pr-agents && git checkout -b fix/sweeper-skips-drafts
```

Прочитай `self-hosted/reliability/tests/test_sweeper_adapter.py` целиком: способ прогона тестов и стиль там свои, и повторять надо их, а не привычки соседнего репозитория.

- [ ] **Step 2: Написать падающий тест**

```python
def test_parse_open_prs_skips_drafts():
    """Черновик по определению не готов к ревью.

    Отказ, ради которого написано: контур научился выкладывать сорванную
    работу разработки черновым пул-реквестом. Вебхук PR-Agent черновики уже
    пропускает и ждёт снятия статуса, а свипер брал ВСЕ открытые — и ревью
    тратилось бы на работу, которую контур сам признал негодной.
    """
    pulls = [
        {"number": 1, "head": {"sha": "aaa"}, "draft": False},
        {"number": 2, "head": {"sha": "bbb"}, "draft": True},
        {"number": 3, "head": {"sha": "ccc"}},
    ]
    out = parse_open_prs(pulls, "o/r")
    assert [pr.number for pr in out] == [1, 3], \
        "черновик пропускаем, отсутствие поля считаем обычным пул-реквестом"
```

- [ ] **Step 3: Прогнать и убедиться, что падает**

Прогони набор тестов свипера тем способом, который принят в этом репозитории (посмотри его README или конфигурацию).
Expected: FAIL — в выборке оказался пул-реквест 2.

- [ ] **Step 4: Добавить пропуск**

```python
def parse_open_prs(pulls_json: list, repo: str) -> list:
    out = []
    for pr in pulls_json:
        # Черновик не готов к ревью по определению: автор ещё не предъявил
        # работу. Вебхук это уже учитывает и ждёт снятия статуса, а свипер
        # брал все открытые — и ревью уходило на то, что смотреть рано.
        if pr.get("draft"):
            continue
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha")
        if number is not None and head_sha:
            out.append(OpenPR(repo=repo, number=int(number), head_sha=head_sha))
    return out
```

- [ ] **Step 5: Прогнать и убедиться, что прошло**

Прогони набор тестов свипера. Expected: PASS, прежние тесты не сломаны.

- [ ] **Step 6: Коммит и пул-реквест**

```bash
git add self-hosted/reliability/sweeper_adapter.py self-hosted/reliability/tests/test_sweeper_adapter.py
git commit -m "fix(sweeper): черновики не попадают в обход ревью"
git push origin fix/sweeper-skips-drafts
```

Открыть пул-реквест в `po-helper-org/poh-pr-agents`, в описании назвать причину: контур начал выкладывать сорванную работу разработки черновиком, и без этой правки ревью тратилось бы на заведомо негодное.

---

### Task 5: Сквозной путь

**Files:**
- Test: `tests/test_dev_partial_publish_e2e.py`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: ничего

- [ ] **Step 1: Написать сквозной тест**

Идёт по активностям: воркфлоу проверено в Task 3, здесь важно, что человек получает связный результат.

```python
"""Сквозной путь: срыв — черновик — честный комментарий.

Отказ, ради которого написано: на poh-demo-checkout#166 после красных тестов
не осталось ни ветки, ни диффа, ни причины — только метка `failed`.
"""

import pytest

import activities as a
from shared.workflow_types import IssueInput


@pytest.fixture
def issue():
    return IssueInput(repo="o/r", issue_number=166, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


async def test_failed_run_leaves_draft_and_honest_comment(monkeypatch, issue):
    """Три факта одним прогоном:

    1. открыт ЧЕРНОВОЙ пул-реквест, а не обычный;
    2. комментарий называет прогон сорванным и несёт причину;
    3. комментарий даёт ссылку на черновик — человеку есть куда смотреть.
    """
    state = {"draft": None, "comments": []}
    monkeypatch.setattr(a.github_client, "publish_worktree",
                        lambda repo, clone, branch, **kw:
                            state.update(draft=kw.get("draft")) or 42)
    monkeypatch.setattr(a.github_client, "post_comment",
                        lambda repo, number, body: state["comments"].append(body))

    number = await a.dev_publish_partial(
        issue, "feature/166-openhands",
        "проверки не прошли (код 1):\n# fail 3")

    assert number == 42
    assert state["draft"] is True

    comment = state["comments"][0]
    assert "сорва" in comment.lower()
    assert "fail 3" in comment
    assert "42" in comment
    assert "готово к ревью" not in comment.lower()
```

- [ ] **Step 2: Прогнать**

Run: `python -m pytest tests/test_dev_partial_publish_e2e.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 3: Прогнать весь набор и гвард реплея**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 4: Коммит**

```bash
git add tests/test_dev_partial_publish_e2e.py
git commit -m "test(develop): сквозной путь — срыв, черновик, честный комментарий"
```

---

## Что остаётся человеку после плана

- **Живой прогон.** План закрывается модульными проверками и гвардом реплея; работает ли механизм на стенде, покажет только живая задача — та же `#166`, на которой дефект и нашёлся.
- **Два пул-реквеста, не один.** Основной в `poh-issue-agents`, правка свипера в `poh-pr-agents`. Выкладывать стенд имеет смысл после обоих: без правки свипера первый же черновик получит бесполезное ревью.
- **Передача контекста от анализа к разработке** через `entire` — отдельная работа, обсуждена и отложена по договорённости.
- **Выкладка разработки для GitLab.** Её нет сегодня, и этот план её не заводит (D3).
