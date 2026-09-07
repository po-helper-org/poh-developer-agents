# Разработчик отдельным сервисом — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Стадия «Разработка» живёт своим репозиторием и подключается к харнессу сборкой из git при `docker compose up` — как Issue-агент. Правка стадии доезжает до контура одним PR с бампом тега, а не релизом Issue-агента целиком.

**Architecture:** Четыре захода, каждый обратим сам по себе. Образ раннера переезжает переменной контекста compose. Чистые модули уезжают pip-пакетом, копии в `poh-issue-agents` удаляются. Активности и воркфлоу стадии переезжают следом, разговаривая с харнессом через пять портов-`Protocol` — образец `poh-delivery-agent`. Последним заводится свой контейнер `dev-worker` с очередью `developer`, томом задач и docker-сокетом.

**Tech Stack:** Python 3.12, Temporal Python SDK, pytest, Docker Compose, setuptools.

## Global Constraints

- Спецификация: [`2026-09-05-developer-as-a-service-design.md`](2026-09-05-developer-as-a-service-design.md), требования R1–R13.
- **Тесты этого репозитория:** `python -m pytest -q`. Красный прогон в PR не отдаём.
- **Тесты `poh-issue-agents`:** `python -m pytest -q`, порог покрытия 83%.
- **Раскладка `poh-issue-agents`:** `worker/` и `webhook/` НЕ пакеты — Dockerfile расплющивает их в `/app`, и `from worker.X import ...` в контейнере падает. Внутри воркера — `import activities`. Установленный пакет этой проблемы не имеет: `poh_developer` резолвится одинаково и в тестах, и в образе.
- **Правка решения воркфлоу требует `workflow.patched(...)`.** Гвард replay обязан оставаться зелёным; прогонять отдельным шагом.
- **Каждый вызов активности передаёт ВСЕ её аргументы**, включая те, у которых есть умолчание: при несовпадении числа Temporal выбрасывает типы и отдаёт словари вместо объектов.
- **Порядок шагов жёсткий.** Шаг 3 — точка невозврата, делается отдельным PR ПОСЛЕ зелёного живого прогона на шаге 2.

## Что уже есть

- `poh_developer/{develop,test_report,pr_closing,worktree}.py` — чистые модули, копия среза `3e9040ce`, 58 тестов зелёные.
- `agent/Dockerfile` — образ раннера, копия `openhands/Dockerfile`.
- `scripts/drift.sh` + `make drift` — сверка копий с источником; сегодня показывает полное совпадение.
- `poh_developer/runner.py` — инварианты образа (`RUNNER_NODE_MAJOR = 22`, ре-экспорт `RUNNER_UID`), с тестами. Отдельным модулем, чтобы побайтовая сверка `develop.py` с источником осталась чистой. Встречного теста в `poh-issue-agents` пока нет.
- `poh-delivery-agent` — работающий образец подключения модулем: `ports.py`, `integration.install()`, `TASK_QUEUE`, `WORKFLOWS`, `ACTIVITIES`.
- Харнесс уже поднимает три очереди в одном процессе (`worker/worker.py:85,118,153`) и печатает их список на старте (`:283`).

## Раскладка файлов

| Файл | За что отвечает | Шаг |
|---|---|---|
| `poh-infra/harness/docker-compose.yml` | контекст сборки образа раннера; позже — сервис `dev-worker` | 1, 4 |
| `poh-infra/harness/.env.example` | `DEVELOPER_AGENT_CONTEXT`, `DEVELOPER_AGENT_VERSION` | 1, 2 |
| `poh-issue-agents/worker/requirements.txt` | пин пакета по тегу | 2 |
| `poh-issue-agents/worker/Dockerfile` | встречный тест мажора Node | 1 |
| `poh-issue-agents/shared/*`, `worker/worktree.py` | удаление копий | 2 |
| `poh_developer/ports.py` | пять `Protocol` + `configure()` | 3 |
| `poh_developer/integration.py` | `TASK_QUEUE`, `WORKFLOWS`, `ACTIVITIES`, `install()` | 3 |
| `poh_developer/activities.py` | 37 функций стадии | 3 |
| `poh_developer/workflows.py` | `IssueDevelopment`, `IssuePrFix` | 3 |
| `poh_developer/task_context.py` | состав `.harness/` (R7) | 3 |
| `poh_developer/worker.py` | точка входа своего контейнера | 4 |
| `Dockerfile` (корень) | образ `dev-worker` | 4 |

---

### Task 1: Образ раннера собирается отсюда

Закрывает R1, R9. Обратимо одной переменной.

**Files:**
- Modify: `poh-infra/harness/docker-compose.yml` (сервис `openhands-runner`)
- Modify: `poh-infra/harness/.env.example`
- Add: `poh-issue-agents/tests/test_runner_node_major.py`

**Interfaces:**

```yaml
# было
openhands-runner:
  build:
    context: "${ISSUE_AGENT_CONTEXT:-https://github.com/po-helper-org/poh-issue-agents.git#main}"
    dockerfile: openhands/Dockerfile

# станет — подкаталог в контексте, как у PR_AGENT_CONTEXT
openhands-runner:
  build:
    context: "${DEVELOPER_AGENT_CONTEXT:-https://github.com/po-helper-org/poh-developer-agents.git#main}"
    dockerfile: agent/Dockerfile
  image: "${DEVELOP_RUNNER_IMAGE:-poh-openhands-runner:local}"
```

Плавающий `#main` здесь уместен и объявлен требованием R1: образ не участвует в
replay воркфлоу.

Встречный тест мажора Node — вместо сверки двух Dockerfile в одном репозитории:

```python
# poh-issue-agents/tests/test_runner_node_major.py
def test_worker_node_major_matches_the_developer_stage():
    """Мажор Node объявлен в poh-developer-agents; образ воркера сверяется с ним.

    Проверки проекта гоняет ВОРКЕР — ту же строку, что CI, — а код проекта
    исполняет раннер. Прогон #13: glob в `node --test` раскрывается сам
    начиная с 22, в образе воркера стоял 20. Красный шаг на зелёном коде.
    """
    from poh_developer.runner import RUNNER_NODE_MAJOR   # после Task 2
    ...
```

**Оговорка порядка:** на шаге 1 пакет ещё не поставлен, поэтому тест либо
читает константу из склонированного репозитория, либо заводится вместе с
Task 2. Второе честнее — ставить его раньше зависимости значит держать в CI
тест, который проверяет копию.

**Step 1: Перевести контекст сборки**
- [ ] `DEVELOPER_AGENT_CONTEXT` в `.env.example` с комментарием, почему здесь плавающий main
- [ ] Сервис `openhands-runner` берёт новый контекст, `dockerfile:` убирается (Dockerfile в корне подкаталога)
- [ ] `docker compose build openhands-runner` собирается локально
- [ ] Живой прогон разработки на `poh-demo-checkout` зелёный

**Step 2: Проверить, что образ тот же**
- [ ] `docker run --rm poh-openhands-runner:local openhands --version` совпадает с прежним
- [ ] uid внутри образа = 10001, Node мажор = 22

---

### Task 2: Чистые модули ставятся пакетом

Закрывает R2, R7 частично, снимает разрыв G4 аудита. Обратимо ревертом.

**Files:**
- Add: тег `v0.1.0` в `poh-developer-agents`
- Modify: `poh-issue-agents/worker/requirements.txt`
- Modify: `poh-issue-agents/shared/questions.py`, `worker/activities.py`, `worker/workflows.py`, `worker/delivery_bridge.py` (импорты)
- Delete: `poh-issue-agents/shared/{develop,test_report,pr_closing}.py`, `worker/worktree.py`
- Move: `poh-issue-agents/shared/task_context.py` → `poh_developer/task_context.py`
- Delete: `poh-developer-agents/scripts/drift.sh`, job `drift` в CI

**Interfaces:**

```python
# poh-issue-agents: было
from shared import develop, pr_closing, test_report, task_context
import worktree                      # внутри воркера

# станет
from poh_developer import develop, pr_closing, test_report, task_context
from poh_developer import worktree
```

```
# worker/requirements.txt — неподвижная ревизия, не ветка (R2).
# Тег был предпочтителен, но его пуш отклоняется шлюзом с 403 — поставлен SHA,
# как у poh-delivery-agent и poh-howtodemo-agent рядом.
poh-developer @ git+https://github.com/po-helper-org/poh-developer-agents@b1e2df20c1a51d441ff19cd1ff1809ee763e1665
```

**Step 1: Довезти `task_context` в пакет**
- [ ] `shared/task_context.py` → `poh_developer/task_context.py` вместе с тестами
- [ ] `poh_developer/__init__.py` пополнен
- [ ] `pytest -q` зелёный здесь

**Step 2: Тег и установка**
- [x] ~~Тег `v0.1.0`~~ → полный SHA `b1e2df2`: пуш тегов отклоняется шлюзом (403)
- [ ] Строка в `worker/requirements.txt` с комментарием про политику пина
- [ ] `docker compose build issue-worker` проходит

**Step 3: Переставить импорты и удалить копии**
- [ ] Импорты переставлены во всех потребителях, включая `delivery_bridge.py`
- [ ] Копии удалены из `poh-issue-agents`
- [ ] `pytest -q` зелёный там, покрытие ≥83%
- [ ] Гвард replay зелёный

**Step 4: Встречный тест Node**
- [ ] `tests/test_runner_node_major.py` заведён (перенесён из Task 1)

**Step 5: Снять механику сверки копий**
- [ ] `scripts/drift.sh`, `make drift` и job `drift` удалены — копий больше нет
- [ ] `docs/extraction-plan.md` отмечен как исполненный до шага 3

**Step 6: Прогон**
- [ ] Стенд с `DRY_RUN=1` проходит все стадии
- [ ] Живой прогон разработки зелёный

---

### Task 3: Порты, активности и воркфлоу

Закрывает R4–R6, R8, R10, R11. **Точка невозврата** — отдельный PR после зелёного Task 2.

**Files:**
- Add: `poh_developer/ports.py`, `poh_developer/integration.py`, `poh_developer/activities.py`, `poh_developer/workflows.py`, `poh_developer/workflow_types.py`
- Add: `poh-issue-agents/worker/developer_bridge.py`
- Modify: `poh-issue-agents/worker/worker.py` (регистрация очереди)
- Delete: 37 функций из `poh-issue-agents/worker/activities.py`, `IssueDevelopment` и `IssuePrFix` из `worker/workflows.py`

**Interfaces:**

```python
# poh_developer/ports.py — пять протоколов (R6)
class GitHubPort(Protocol):
    def auth_token(self, repo: str) -> str: ...
    def publish_worktree(self, repo, clone_dir, branch, *, title, body, message,
                         draft: bool = False, ignore_for_empty_check=(),
                         force_include=()) -> int | None: ...
    def review_text(self, repo: str, number: int) -> str: ...
    def changes_requested(self, repo: str, number: int) -> bool | None: ...
    def post_comment(self, repo: str, number: int, body: str) -> None: ...
    ...

class IssueBlocksPort(Protocol):
    def write(self, repo: str, number: int, block: str, content: str) -> None: ...
    def read(self, repo: str, number: int, block: str) -> str: ...

class RepowisePort(Protocol):    ...
class MemoryPort(Protocol):      ...
class LlmPort(Protocol):         ...

def configure(*, github=None, issue_blocks=None, repowise=None,
              memory=None, llm=None) -> None: ...
```

```python
# poh_developer/integration.py — ровно как у poh-delivery-agent
TASK_QUEUE = "developer"
WORKFLOWS = [IssueDevelopment, IssuePrFix]
ACTIVITIES = _activities.ALL

def install(*, github, issue_blocks, repowise, memory, llm) -> None:
    ports.configure(github=github, issue_blocks=issue_blocks,
                    repowise=repowise, memory=memory, llm=llm)
```

```python
# poh-issue-agents/worker/worker.py
from poh_developer import integration as developer
import developer_bridge

developer_bridge.install()
side.append(Worker(client, task_queue=developer.TASK_QUEUE,
                   workflows=developer.WORKFLOWS,
                   activities=developer.ACTIVITIES, ...))
```

**Оговорка R10.** `IssueInput` переезжает в `poh_developer/workflow_types.py`, а
`poh-issue-agents` импортирует его оттуда. Два совпадающих объявления по разные
стороны границы — это молчаливые словари вместо объектов при первом же
расхождении.

**Оговорка R8.** `shared/review_events.py` НЕ переезжает: его выход — тип цикла
задачи, и он тянет за собой словарь фаз.

**Step 1: Порты и заглушки**
- [ ] `ports.py` с пятью `Protocol` и `configure()`
- [ ] Тестовые заглушки портов в `tests/conftest.py`
- [ ] Отказ незаданного порта — внятный `RuntimeError`, не `AttributeError` на `None`

**Step 2: Типы через границу**
- [ ] `IssueInput` и сопутствующие типы стадии в `poh_developer/workflow_types.py`
- [ ] `poh-issue-agents` импортирует их оттуда, своё объявление удалено
- [ ] Гвард `test_activity_arg_types` заведён здесь

**Step 3: Активности**
- [ ] 37 функций перенесены, обращения к харнессу заменены вызовами портов
- [ ] Общие помощники (`_clone_repo`, `_run_with_heartbeat`, `_load_prompt`, `_truncate`) — либо переносятся копией с пометкой, либо становятся частью порта; решение принимается по каждому явно, а не оптом
- [ ] `ACTIVITIES` собран, ни один не потерян

**Step 4: Воркфлоу**
- [ ] `IssueDevelopment` и `IssuePrFix` перенесены без правки решений
- [ ] Гвард replay переехал и зелёный
- [ ] Родитель зовёт дочерний воркфлоу с `task_queue=developer.TASK_QUEUE`

**Step 5: Мост на стороне харнесса**
- [ ] `developer_bridge.py` — реализации пяти портов поверх `github_client`, `issue_blocks`, `repowise`, `memory`, `llm`
- [ ] `delivery_bridge.py` зовёт агента разработки через новую очередь (R12)
- [ ] Копии удалены из `poh-issue-agents`

**Step 6: Прогон**
- [ ] `pytest -q` зелёный в обоих репозиториях
- [ ] Стенд с `DRY_RUN=1`
- [ ] Живой прогон разработки и круга правок на `poh-demo-checkout`

---

### Task 4: Свой контейнер

Закрывает R13. Обратимо возвратом регистрации в общий воркер.

**Files:**
- Add: `poh-developer-agents/Dockerfile`, `poh_developer/worker.py`
- Modify: `poh-infra/harness/docker-compose.yml`, `.env.example`
- Modify: `poh-issue-agents/worker/worker.py` (регистрация очереди убирается)

**Interfaces:**

```yaml
dev-worker:
  build:
    context: "${DEVELOPER_AGENT_CONTEXT:-https://github.com/po-helper-org/poh-developer-agents.git#main}"
    dockerfile: Dockerfile
  image: poh-developer-agent-worker:harness
  restart: unless-stopped
  environment: *issue-env
  volumes:
    - dev-workspace:/workspaces
    - /var/run/docker.sock:/var/run/docker.sock
  depends_on: [temporal, openhands-runner]
  networks: [default, poh-repowise-net, harness-memory-net]
```

**Оговорка.** Из `issue-worker` уезжает том `dev-workspace`, но **не сокет** —
`poh-delivery-agent` катит на прод через docker и живёт в том же процессе.
Сужение частичное, и говорить о нём надо честно.

**Step 1: Точка входа и образ**
- [ ] `poh_developer/worker.py` поднимает воркер на очереди `developer`
- [ ] `Dockerfile` в корне; мажор Node тот же, что у раннера (R9)

**Step 2: Сервис в compose**
- [ ] `dev-worker` заведён, том и сокет переставлены
- [ ] Из `issue-worker` убран `dev-workspace`
- [ ] Регистрация очереди убрана из `worker/worker.py` харнесса

**Step 3: Отказ при неподнятой очереди**
- [ ] При `DEVELOP_ENABLED=1` и отсутствии воркера на `developer` харнесс падает на старте с внятным текстом, а не оставляет задачи висеть в `in-development`
- [ ] Список поднятых очередей по-прежнему печатается

**Step 4: Прогон**
- [ ] `docker compose up -d --build` поднимает оба воркера
- [ ] Живой прогон разработки зелёный
- [ ] Переключение сделано на пустой очереди либо под `workflow.patched(...)`

---

## Тикеты

Заходы заведены тикетами линейной цепью; рабочие чек-листы живут там, этот
документ остаётся замыслом.

| Тикет | Заход | Блокируется |
|---|---|---|
| [#1](https://github.com/po-helper-org/poh-developer-agents/issues/1) | образ агента собирается отсюда | — |
| [#2](https://github.com/po-helper-org/poh-developer-agents/issues/2) | чистые модули пакетом, копии удаляются | #1 |
| [#3](https://github.com/po-helper-org/poh-developer-agents/issues/3) | активности и воркфлоу через порты | #2 |
| [#4](https://github.com/po-helper-org/poh-developer-agents/issues/4) | свой контейнер `dev-worker` | #3 |

**Что изменил грилинг 2026-09-05.** Два решения замысла отменены, объём Task 1
вырос вчетверо:

- контекст сборки **корневой** с ключом `dockerfile:`, а не подкаталог
  `#main:agent`. Следствие — одна переменная `DEVELOPER_AGENT_CONTEXT` вместо
  двух: та же обслуживает и `dev-worker` на Task 4;
- Task 1 трогает не два файла, а восемь. Помимо compose и `.env.example`:
  `DEVELOP_RUNNER_IMAGE` оказалась не объявлена нигде, хотя compose читал её
  всё это время; контекст сборки упомянут в четырёх документах, включая
  релизную заметку для СБ; `.dockerignore` заводится сейчас, чтобы про него не
  вспоминали на Task 4; старый `openhands/Dockerfile` помечается шапкой, пока
  не удалён на #2.

## Порядок PR

| PR | Задачи | Точка проверки |
|---|---|---|
| 1 | Task 1 + Task 2 | живой прогон разработки зелёный |
| 2 | Task 3 | живой прогон разработки и круга правок |
| 3 | Task 4 | `docker compose up` на чистом стенде |

Task 3 не объединяется с Task 2 намеренно: разбор «почему сломалось» при двух
изменениях сразу упирается в оба.
