# Замечания человека в ревью доходят до контура — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Формальное ревью человека и построчное замечание будят круг правок так же, как доклад PR-Agent, а PR с заблокированным слиянием перестаёт называться готовым.

**Architecture:** Разбор события ревью живёт чистой функцией в `shared/` и отдаёт готовый `AgentEvent` — тот самый тип, которым уже пользуется `/agent-event`. Вебхук на событии ревью строит его и отдаёт в общий путь корреляции и старта цикла, выделенный из обработчика `/agent-event`. Отдельно: итог круга правок спрашивает GitHub о состоянии ревью и не обещает готовности, когда слияние заблокировано.

**Tech Stack:** Python 3.12, FastAPI, Temporal Python SDK, pytest, GitHub REST.

## Global Constraints

- Тесты гонять командой репозитория: `python -m pytest -q`. Порог покрытия — 83%.
- **Раскладка:** `worker/` и `webhook/` НЕ пакеты — Dockerfile расплющивает их в `/app`. Импорт `from worker.X import ...` падает в контейнере. Общий код — только в `shared/`, его копируют оба образа.
- **Фикстура pytest из чужого тестового модуля не видна** — `conftest.py` либо своя копия.
- **Правка решения воркфлоу требует `workflow.patched(...)`.** Гвард `tests/test_workflow_replay.py` обязан оставаться зелёным.
- **Каждый вызов активности передаёт ВСЕ её аргументы.** Гвард — `tests/test_activity_arg_types.py`.
- **Вебхук — чистый транспорт:** GitHub-клиента у него нет, комментариев он не пишет, отказ только логирует.
- Спецификация: `docs/superpowers/specs/2026-09-03-human-review-reaches-contour-design.md`, требования R1–R11.

## Что уже есть

- `github_client.review_text(repo, number)` (`worker/github_client.py:892`) — читает `/pulls/{n}/reviews` (тело + состояние) и построчные `/pulls/{n}/comments`. **Содержимое ревью человека уже доступно**, не хватает побудки.
- `/agent-event` (`webhook/main.py:329`) — принимает `AgentEvent`, корреллирует PR→Issue (`shared/agent_events.correlate`), поднимает `IssueLifecycle` сигналом `agent_event`, а несопоставленное отправляет в `OrphanAgentEvent`.
- `AgentEvent` (`shared/agent_events.py:43`) — поля `repo, agent, phase, status, ref, root_issue, detail, revision`. Ключ идемпотентности включает `revision`.
- `_phase_pr_review` (`worker/workflows.py:3050`) — круг правок, до `deadlines.pr_fix_max_rounds` заходов.
- `pr_closing.settled_comment(round_number, verdict)` (`shared/pr_closing.py:124`) — итог «PR готов к слиянию».
- `finish_pr_fixing(repo, pr_number, rounds, settled, verdict)` (`worker/activities.py:4384`).

## Раскладка файлов

| Файл | За что отвечает |
|---|---|
| `shared/review_events.py` | Разбор события ревью GitHub в `AgentEvent`. Чистая функция: ни сети, ни Temporal |
| `webhook/main.py` | Ветка события и общий путь старта цикла, выделенный из `/agent-event` |
| `worker/github_client.py` | Чтение состояния ревью PR |
| `shared/pr_closing.py` | Текст итога, не обещающий готовности при заблокированном слиянии |
| `worker/activities.py` | `finish_pr_fixing` спрашивает состояние ревью |

---

### Task 1: Разбор события ревью

Закрывает R2, R3, R4 в части разбора.

**Files:**
- Create: `shared/review_events.py`
- Test: `tests/test_review_events.py`

**Interfaces:**
- Consumes: `shared.agent_events.AgentEvent`
- Produces: `from_review(payload: dict) -> AgentEvent | None`, `from_review_comment(payload: dict) -> AgentEvent | None` — `None` означает «будить нечего»

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_review_events.py`:

```python
"""Событие ревью GitHub → факт для цикла Issue.

Отказ, ради которого написано: `poh-demo-checkout#172`. Человек оставил
ревью со статусом `CHANGES_REQUESTED` и замечанием про регрессию, а контур
дважды объявил «PR готов к слиянию» — событий ревью он не получает вовсе.
"""

from shared import review_events


def _review(state="changes_requested", login="kibarik", kind="User", body="есть замечание"):
    return {
        "action": "submitted",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 172, "body": "Closes #171"},
        "review": {"id": 9, "state": state, "body": body,
                   "commit_id": "abc123", "user": {"login": login, "type": kind}},
    }


def test_changes_requested_wakes_the_repair_loop():
    event = review_events.from_review(_review())

    assert event is not None
    assert event.repo == "o/r"
    assert event.phase == "pr-review"
    assert event.status == "started"
    assert event.ref == "172"
    assert event.revision == "abc123", "ревизия входит в ключ идемпотентности"
    assert "есть замечание" in event.detail
    assert "Closes #171" in event.detail, "по нему корреляция найдёт задачу"


def test_a_plain_comment_review_also_wakes_it():
    """`COMMENTED` — тоже замечания, просто без блокировки слияния."""
    assert review_events.from_review(_review(state="commented")) is not None


def test_approval_wakes_nothing():
    """Править по одобрению нечего — круг правок стоил бы прогона агента зря."""
    assert review_events.from_review(_review(state="approved")) is None


def test_a_dismissed_review_wakes_nothing():
    assert review_events.from_review(_review(state="dismissed")) is None


def test_a_bot_review_wakes_nothing():
    """PR-Agent докладывает через `/agent-event` (R4).

    Вторая побудка от него дала бы два круга правок на один доклад — то есть
    двойную цену прогона агента за один и тот же текст.
    """
    assert review_events.from_review(_review(login="poh-harness-demo[bot]",
                                             kind="Bot")) is None


def test_an_empty_review_body_still_wakes_when_changes_are_requested():
    """Пустое тело при `CHANGES_REQUESTED` — замечания в построчных.

    Молчаливое «нечего будить» здесь означало бы ровно тот отказ, ради
    которого всё пишется.
    """
    assert review_events.from_review(_review(body="")) is not None


def test_an_empty_commented_review_wakes_nothing():
    """`COMMENTED` без текста и без блокировки — не замечание."""
    assert review_events.from_review(_review(state="commented", body="")) is None


def test_wrong_action_wakes_nothing():
    payload = _review()
    payload["action"] = "edited"
    assert review_events.from_review(payload) is None


def _review_comment(login="kibarik", kind="User"):
    return {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 172, "body": "Closes #171"},
        "comment": {"id": 5, "body": "здесь опечатка", "path": "src/a.py",
                    "line": 12, "commit_id": "abc123",
                    "user": {"login": login, "type": kind}},
    }


def test_an_inline_comment_wakes_the_repair_loop():
    """`review_text` построчные замечания читает — оставить их без побудки
    значило бы починить половину (R2)."""
    event = review_events.from_review_comment(_review_comment())

    assert event is not None
    assert event.ref == "172"
    assert "src/a.py:12" in event.detail
    assert "здесь опечатка" in event.detail


def test_an_inline_comment_from_a_bot_wakes_nothing():
    assert review_events.from_review_comment(
        _review_comment(login="pr-agent[bot]", kind="Bot")) is None
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_review_events.py -q -p no:randomly --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.review_events'`

- [ ] **Step 3: Написать модуль**

Создать `shared/review_events.py`:

```python
"""Событие ревью GitHub → факт для цикла Issue.

Тип факта — тот же `AgentEvent`, которым докладывают внешние агенты. Заводить
для человека отдельный вид события значило бы два пути к одному действию: круг
правок уже читает замечания любого происхождения (`github_client.review_text`
берёт и тело ревью, и построчные), и различать их ему незачем.

Модуль чистый: ни сети, ни Temporal. Вебхук — транспорт, и разбор конверта
обязан проверяться без него.
"""
from __future__ import annotations

from shared import lifecycle
from shared.agent_events import STARTED, AgentEvent

# Кто оставил ревью — под этим именем факт видно в трассировке.
HUMAN_REVIEW_AGENT = "human-review"

# Состояния, по которым есть что править. `approved` и `dismissed` не будят:
# по одобрению править нечего, а снятое ревью — уже не замечание.
_WAKING_STATES = ("changes_requested", "commented")


def _is_bot(user: dict) -> bool:
    """Ревью бота по этому пути не будит ничего.

    PR-Agent докладывает через `/agent-event`, и вторая побудка дала бы два
    круга правок на один доклад — двойную цену прогона агента за тот же текст.
    """
    return (user.get("type") or "") == "Bot" or (user.get("login") or "").endswith("[bot]")


def from_review(payload: dict) -> AgentEvent | None:
    """Формальное ревью. `None` — будить нечего."""
    if payload.get("action") != "submitted":
        return None
    review = payload.get("review") or {}
    state = (review.get("state") or "").lower()
    if state not in _WAKING_STATES:
        return None
    if _is_bot(review.get("user") or {}):
        return None

    body = (review.get("body") or "").strip()
    # Пустое тело при `changes_requested` — замечания в построчных, и это
    # ровно тот случай, ради которого всё пишется. Пустой `commented` —
    # действительно ничего.
    if not body and state != "changes_requested":
        return None

    pull = payload.get("pull_request") or {}
    return AgentEvent(
        repo=(payload.get("repository") or {}).get("full_name") or "",
        agent=HUMAN_REVIEW_AGENT,
        phase=lifecycle.PR_REVIEW,
        status=STARTED,
        ref=str(pull.get("number") or ""),
        # Тело PR едет в detail: по нему `correlate` находит задачу через
        # `Closes #N`, если номер не пришёл явно.
        detail=f"### Ревью ({state})\n{body}\n\n{pull.get('body') or ''}".strip(),
        # Ревизия входит в ключ идемпотентности: два ревью по одному коммиту —
        # один повод, два по разным — два.
        revision=str(review.get("commit_id") or ""),
    )


def from_review_comment(payload: dict) -> AgentEvent | None:
    """Построчное замечание без формального ревью. `None` — будить нечего."""
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if _is_bot(comment.get("user") or {}):
        return None
    body = (comment.get("body") or "").strip()
    if not body:
        return None

    pull = payload.get("pull_request") or {}
    where = f"{comment.get('path')}:{comment.get('line') or '?'}"
    return AgentEvent(
        repo=(payload.get("repository") or {}).get("full_name") or "",
        agent=HUMAN_REVIEW_AGENT,
        phase=lifecycle.PR_REVIEW,
        status=STARTED,
        ref=str(pull.get("number") or ""),
        detail=f"### {where}\n{body}\n\n{pull.get('body') or ''}".strip(),
        revision=str(comment.get("commit_id") or ""),
    )
```

Проверь фактом, что `lifecycle.PR_REVIEW` называется именно так, а `STARTED` экспортируется из `shared/agent_events.py` — не на веру.

- [ ] **Step 4: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_review_events.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add shared/review_events.py tests/test_review_events.py
git commit -m "feat(review): разбор события ревью GitHub в факт для цикла"
```

---

### Task 2: Вебхук будит круг правок

Закрывает R1, R2, R5, R6.

**Files:**
- Modify: `webhook/main.py` (обработчик `/agent-event` — выделить общий путь; `_handle_delivery` — ветка события)
- Test: `tests/test_webhook_review_events.py`

**Interfaces:**
- Consumes: `review_events.from_review`, `review_events.from_review_comment`
- Produces: ничего для следующих задач

- [ ] **Step 1: Написать падающие тесты**

Заглушки клиента Temporal копируй по образцу существующих тестов вебхука
(найди файл, где уже подменяется `get_temporal_client`, и повтори приём).

```python
"""Ревью человека будит круг правок.

Отказ: `poh-demo-checkout#172` — вебхук не подписан на события ревью и
обработчика не имеет, поэтому замечание человека не вызывает ничего.
"""

import pytest


def _payload(state="changes_requested", login="kibarik"):
    return {
        "action": "submitted",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 172, "body": "Closes #171"},
        "review": {"id": 9, "state": state, "body": "замечание",
                   "commit_id": "abc", "user": {"login": login, "type": "User"}},
    }


async def test_a_human_review_starts_the_lifecycle(webhook_client, started):
    resp = await webhook_client("pull_request_review", _payload())

    assert resp["ok"] is True
    assert started, "цикл не был поднят — замечание снова потеряно"
    assert started[0]["signal"] == "agent_event"
    assert started[0]["event"].phase == "pr-review"


async def test_an_approval_starts_nothing(webhook_client, started):
    await webhook_client("pull_request_review", _payload(state="approved"))
    assert started == []


async def test_an_author_outside_the_allowlist_starts_nothing(
        webhook_client, started, monkeypatch):
    """Круг правок стоит прогона агента — запускать его по ревью произвольного
    участника нельзя (R5)."""
    monkeypatch.setenv("AGENT_TRIGGER_ALLOWLIST", "kibarik")

    await webhook_client("pull_request_review", _payload(login="прохожий"))

    assert started == []


async def test_a_review_on_an_unrelated_pr_goes_to_audit(webhook_client, started):
    """PR не заводился контуром — событие роняется в аудит, а не поднимает цикл (R6)."""
    payload = _payload()
    payload["pull_request"]["body"] = "без ссылки на задачу"

    resp = await webhook_client("pull_request_review", payload)

    assert resp["ok"] is True
    assert started == []
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_webhook_review_events.py -q -p no:randomly --no-cov`
Expected: FAIL — цикл не поднимается: обработчика нет.

- [ ] **Step 3: Выделить общий путь старта**

В `webhook/main.py` из тела `/agent-event` выделить функцию — она нужна двум
вызывающим, и дублировать корреляцию с аудитом нельзя:

```python
async def _dispatch_agent_event(client, event) -> dict:
    """Корреляция факта с задачей и подъём цикла. Общий путь для двух входов.

    Зовут `/agent-event` (внешние агенты) и вебхук событий ревью (человек).
    Один путь намеренно: корреляция, аудит несопоставленного и подъём цикла
    обязаны вести себя одинаково независимо от того, кто принёс факт.
    """
    issue_number, how = correlate(event)
    if issue_number is None:
        await _report_orphan(client, event, how)
        return {"ok": True, "correlated": False, "reason": how}

    event.correlation = how
    await client.start_workflow(
        "IssueLifecycle",
        args=_lifecycle_args_for(event, issue_number),
        id=issue_workflow_id(event.repo, issue_number),
        task_queue="issue-lifecycle",
        start_signal="agent_event",
        start_signal_args=[event],
    )
    return {"ok": True, "correlated": True, "issue": issue_number, "by": how}
```

Тело `/agent-event` после проверок подписи и allowlist репозитория сводится к
`return await _dispatch_agent_event(client, event)`. Импорты `correlate` и
`parse_event` внутри обработчика — оставь как есть, но `correlate` понадобится
и новой функции: подними импорт на уровень модуля, если он там ещё не стоит.

- [ ] **Step 4: Добавить ветку события**

В `_handle_delivery`, рядом с ветками `issues` и `issue_comment`:

```python
    if x_github_event in ("pull_request_review", "pull_request_review_comment"):
        # Замечание человека — такой же повод для круга правок, как доклад
        # PR-Agent. До 3 сентября этих событий не приходило вовсе, и контур
        # объявлял «PR готов к слиянию», не увидев возражения (#302).
        from shared import review_events

        build = (review_events.from_review
                 if x_github_event == "pull_request_review"
                 else review_events.from_review_comment)
        event = build(payload)
        if event is None:
            return {"ok": True}
        number = int(event.ref) if event.ref.isdigit() else 0
        if not _may_start_expensive(payload, "круг правок", event.repo, number):
            return {"ok": True}
        return await _dispatch_agent_event(client, event)
```

Проверка allowlist — существующая `_may_start_expensive(payload, what, repo,
issue_number)` (`webhook/main.py:195`); новой не заводи.

- [ ] **Step 5: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_webhook_review_events.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 6: Прогнать весь набор**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 7: Коммит**

```bash
git add webhook/main.py tests/test_webhook_review_events.py
git commit -m "feat(webhook): ревью человека будит круг правок"
```

---

### Task 3: «Готов к слиянию» говорится только когда это правда

Закрывает R7, R8, R9.

**Files:**
- Modify: `worker/github_client.py` (чтение состояния ревью)
- Modify: `shared/pr_closing.py` (`settled_comment`)
- Modify: `worker/activities.py` (`finish_pr_fixing`)
- Test: `tests/test_pr_ready_to_merge.py`

**Interfaces:**
- Consumes: ничего из предыдущих задач
- Produces: `github_client.changes_requested(repo, number) -> bool | None` — `None` означает «спросить не удалось»; `pr_closing.settled_comment(round_number, verdict, blocked)`

- [ ] **Step 1: Написать падающие тесты**

```python
"""PR не называется готовым к слиянию, когда GitHub его слить не даёт.

Отказ: `poh-demo-checkout#172` — контур дважды написал «PR готов к слиянию»,
хотя формальное ревью человека стояло в `CHANGES_REQUESTED` и слияние было
заблокировано.
"""

from shared import pr_closing


def test_a_blocked_pr_is_not_called_ready():
    body = pr_closing.settled_comment(1, verdict="", blocked=True)

    assert "готов к слиянию" not in body
    assert "замечания" in body.lower()


def test_an_unblocked_pr_is_called_ready_as_before():
    body = pr_closing.settled_comment(1, verdict="", blocked=False)

    assert "готов к слиянию" in body


def test_an_unknown_state_says_neither():
    """Спросить не удалось — не повод ни обещать, ни обвинять (R9)."""
    body = pr_closing.settled_comment(1, verdict="", blocked=None)

    assert "готов к слиянию" not in body


def test_the_verdict_still_travels_with_the_result():
    body = pr_closing.settled_comment(2, verdict="замечание отклонено: ...",
                                      blocked=False)
    assert "замечание отклонено" in body


def test_changes_requested_reads_the_latest_review_per_author(monkeypatch):
    """Считается ПОСЛЕДНЕЕ ревью каждого автора.

    Человек мог запросить изменения, а затем одобрить: старое `CHANGES_REQUESTED`
    в списке остаётся, но блокировки больше нет.
    """
    import github_client as gc

    class _Resp:
        ok = True
        def json(self):
            return [
                {"user": {"login": "a"}, "state": "CHANGES_REQUESTED",
                 "submitted_at": "2026-09-03T05:00:00Z"},
                {"user": {"login": "a"}, "state": "APPROVED",
                 "submitted_at": "2026-09-03T06:00:00Z"},
            ]

    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(gc, "_auth_headers", lambda repo: {})

    assert gc.changes_requested("o/r", 172) is False


def test_changes_requested_when_the_last_one_blocks(monkeypatch):
    import github_client as gc

    class _Resp:
        ok = True
        def json(self):
            return [
                {"user": {"login": "a"}, "state": "APPROVED",
                 "submitted_at": "2026-09-03T05:00:00Z"},
                {"user": {"login": "a"}, "state": "CHANGES_REQUESTED",
                 "submitted_at": "2026-09-03T06:00:00Z"},
            ]

    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(gc, "_auth_headers", lambda repo: {})

    assert gc.changes_requested("o/r", 172) is True


def test_a_failed_question_is_unknown_not_false(monkeypatch):
    """`None`, а не `False`: молчаливое «не заблокировано» вернуло бы ложное
    обещание готовности ровно в тот момент, когда мы ничего не знаем."""
    import github_client as gc

    class _Resp:
        ok = False
        def json(self):
            return []

    monkeypatch.setattr(gc.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(gc, "_auth_headers", lambda repo: {})

    assert gc.changes_requested("o/r", 172) is None
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_pr_ready_to_merge.py -q -p no:randomly --no-cov`
Expected: FAIL — `settled_comment() got an unexpected keyword argument 'blocked'`

- [ ] **Step 3: Прочитать состояние ревью**

В `worker/github_client.py` рядом с `review_text`:

```python
def changes_requested(repo: str, number: int) -> bool | None:
    """Блокирует ли ревью слияние. `None` — спросить не удалось.

    Считается ПОСЛЕДНЕЕ ревью каждого автора: человек мог запросить изменения,
    а потом одобрить, и старая запись в списке остаётся навсегда.

    `None`, а не `False`, намеренно: молчаливое «не заблокировано» вернуло бы
    обещание готовности ровно тогда, когда мы ничего не знаем.
    """
    resp = requests.get(f"https://api.github.com/repos/{repo}/pulls/{number}/reviews",
                        headers=_auth_headers(repo), params={"per_page": 100},
                        timeout=30)
    if not resp.ok:
        _log.warning("состояние ревью %s#%s не прочитано: %s", repo, number,
                     resp.status_code)
        return None
    last: dict[str, str] = {}
    for item in resp.json():
        state = (item.get("state") or "").upper()
        if state not in ("APPROVED", "CHANGES_REQUESTED"):
            # `COMMENTED` и `DISMISSED` состояния автора не меняют.
            continue
        login = (item.get("user") or {}).get("login") or ""
        last[login] = state
    return any(state == "CHANGES_REQUESTED" for state in last.values())
```

- [ ] **Step 4: Поправить текст итога**

В `shared/pr_closing.py`:

```python
def settled_comment(round_number: int, verdict: str = "",
                    blocked: bool | None = False) -> str:
    """Итог: правок не потребовалось. Разбор публикуется вместе с итогом.

    Без разбора этот комментарий означал бы «мы ничего не сделали, поверьте на
    слово». С ним видно, какие замечания отклонены и почему, — и следующий круг
    не начнёт спорить о том же заново.

    `blocked` — стоит ли на PR ревью, запрещающее слияние. `True` — про
    готовность не говорим вовсе: на #172 контур дважды объявил PR готовым, а
    слияние было заблокировано замечанием человека. `None` — состояние
    неизвестно, и обещать тоже нечего.
    """
    body = (
        "✅ Круг правок завершён: агент не нашёл в ревью того, что требует "
        f"изменений в коде (кругов пройдено: {round_number}).\n\n"
    )
    if blocked is True:
        body += ("На PR стоят замечания, запрещающие слияние. Снять их может "
                 "только их автор — решение за человеком.")
    elif blocked is False:
        body += "PR готов к слиянию и ждёт решения разработчика."
    else:
        body += "Состояние ревью узнать не удалось — проверьте PR перед слиянием."
    if verdict.strip():
        body += ("\n\n<details><summary>Разбор замечаний</summary>\n\n"
                 f"{verdict.strip()}\n\n</details>")
    return body
```

- [ ] **Step 5: Спросить состояние при публикации итога**

В `worker/activities.py`, в `finish_pr_fixing`:

```python
    if settled:
        # Спрашиваем ФАКТОМ, а не выводим из того, что агент не нашёл предмета
        # для правок: замечание человека агент мог отклонить, а GitHub всё
        # равно не даст слить (R7).
        try:
            blocked = await asyncio.to_thread(
                github_client.changes_requested, repo, pr_number)
        except Exception as exc:  # noqa: BLE001
            # Проверка не имеет права ронять итог круга (R9).
            activity.logger.warning("состояние ревью не прочитано: %s", exc)
            blocked = None
        await asyncio.to_thread(github_client.post_comment, repo, pr_number,
                                pr_closing.settled_comment(rounds, verdict, blocked))
        if blocked:
            # Замечания остались — задача в очереди к людям, как и при
            # исчерпании кругов (R8).
            await asyncio.to_thread(github_client.add_label, repo, pr_number,
                                    pr_closing.NEEDS_HUMAN_PR)
        return
```

- [ ] **Step 6: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_pr_ready_to_merge.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 7: Прогнать весь набор и гвард реплея**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 8: Коммит**

```bash
git add worker/github_client.py shared/pr_closing.py worker/activities.py \
        tests/test_pr_ready_to_merge.py
git commit -m "fix(pr): не называть готовым к слиянию PR, который слить нельзя"
```

---

### Task 4: Подписка и документация

Закрывает R10, R11.

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: ничего

- [ ] **Step 1: Дописать README**

В раздел «Круг правок — доведение PR по замечаниям» добавить:

```markdown
**Замечания человека будят круг так же, как доклад PR-Agent** — событиями
`pull_request_review` и `pull_request_review_comment`. Будят состояния
`changes_requested` и `commented`; `approved` и ревью ботов — нет (бот
докладывает через `/agent-event`, и вторая побудка дала бы два круга на один
доклад). Автор ревью проходит тот же allowlist, что и прочие дорогие триггеры.

**Подписку нужно проставить руками — в двух местах:** хук репозитория и
настройки GitHub App. Кодом это не делается.

Симптом отсутствующей подписки коварен: контур продолжает получать
`pull_request` и выглядит исправным, а замечания человека молча не приводят
ни к чему — ровно так и обнаружился #302, когда контур дважды объявил «PR
готов к слиянию», не увидев `CHANGES_REQUESTED`.
```

- [ ] **Step 2: Коммит**

```bash
git add README.md
git commit -m "docs: ревью человека будит круг правок; подписка проставляется руками"
```

---

## Что остаётся человеку после плана

- **Проставить подписку** на `pull_request_review` и
  `pull_request_review_comment` в хуке каждого репозитория контура и в
  настройках GitHub App. Без этого код не сработает, и выглядеть это будет как
  исправная работа.
- **Живой прогон:** оставить `CHANGES_REQUESTED` на PR, открытом контуром, и
  убедиться, что круг правок запустился, а итог не обещает готовности.
- **GitLab** тем же путём не закрывается: у него своя модель ревью — часть
  работ по GitLab (#141, #275, #276).
