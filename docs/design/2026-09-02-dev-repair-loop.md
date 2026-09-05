# Круг правок разработки — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Прогон разработки отличает свою поломку от чужой и от мигающего теста, чинит свою одним повторным заходом агента и зовёт человека только тогда, когда починить не вышло.

**Architecture:** Разбор JUnit XML живёт отдельным модулем в `shared/`. Красный путь прогона добавляет две активности: `dev_diagnose` (базовая линия на отдельном чистом дереве, перепроверка на мигание, разница множеств) и `dev_repair` (переписывает `.task.md` и гоняет тот же контейнер агента). Воркфлоу под маркером ловит отказ `dev_tests`, спрашивает диагноз и решает: чужая краснота — публиковать, своя — чинить и проверять снова. Зелёный прогон не платит ничего.

**Tech Stack:** Python 3.12, Temporal Python SDK, pytest, `xml.etree.ElementTree`, git worktree, docker.

## Global Constraints

- Тесты гонять командой репозитория: `python -m pytest -q`. Порог покрытия — 83%, красный прогон в PR не отдаём.
- **Раскладка:** `worker/` и `webhook/` НЕ пакеты — Dockerfile расплющивает их в `/app`. Импорт `from worker.X import ...` падает в контейнере. Внутри воркера — `import activities`, `import github_client`. Модули из `shared/` импортируются как `from shared import X`.
- **Фикстура pytest из чужого тестового модуля не видна** — `conftest.py` либо своя копия.
- **Правка решения воркфлоу требует `workflow.patched(...)`.** Гвард `tests/test_workflow_replay.py` обязан оставаться зелёным; прогонять отдельным шагом.
- **Каждый вызов активности передаёт ВСЕ её аргументы**, включая те, у которых есть умолчание: при несовпадении числа Temporal выбрасывает типы и отдаёт словари вместо объектов. Гвард — `tests/test_activity_arg_types.py`.
- **Телеметрия и диагностика не имеют права ронять то, о чём отчитываются**: отказ диагностики не подменяет причину отказа тестов.
- Спецификация: `docs/superpowers/specs/2026-09-02-dev-repair-loop-design.md`, требования B1–B26.

## Что уже есть

- `_dev_tests(issue) -> str` (`worker/activities.py:3252`) — гоняет `DEVELOP_TEST_COMMAND` в клоне, пишет сигнал `tests_passed`, бросает `RuntimeError` на ненулевом коде. `DEV_TESTS_TIMEOUT_SEC = 900` (`:2608`).
- `_dev_run_agent(issue) -> str` (`:3159`) — `docker run --rm` по `develop.runner_command(...)`, таймаут `develop.run_timeout()`.
- Постановка агента — `.task.md` в клоне (`:2938`), снимается перед коммитом (`develop.SERVICE_FILES`).
- `_dev_paths(issue) -> (root, clone_dir)` (`:2659`), `_write_signal(root, name, value)` (`:2975`), `_run_with_heartbeat(fn, *args, label=...)`.
- `IssueDevelopment` (`worker/workflows.py`): `dev_prepare → dev_announce → build_mvp_plan → dev_run_agent → dev_followups → dev_tests → dev_publish`; отказ после агента перехватывается под маркером `issue-development-partial-publish` и даёт черновой PR.

## Раскладка файлов

| Файл | За что отвечает |
|---|---|
| `shared/test_report.py` | Разбор JUnit XML: поиск отчётов, множество имён упавших тестов. Чистая функция, без сети и докера |
| `shared/workflow_types.py` | Тип `Diagnosis` — исход диагностики, ездит между активностью и воркфлоу |
| `worker/activities.py` | `dev_diagnose`, `dev_repair`, `dev_announce_repair`; правка `dev_publish` под оговорку |
| `worker/workflows.py` | Красный путь: диагноз, починка, повторные тесты |
| `worker/worker.py` | Регистрация новых активностей |

---

### Task 1: Разбор JUnit XML

Закрывает B4, B5 и нормализацию путей.

**Files:**
- Create: `shared/test_report.py`
- Test: `tests/test_test_report.py`

**Interfaces:**
- Consumes: ничего
- Produces:
  - `DEFAULT_PATTERNS: tuple[str, ...]`
  - `find_reports(tree: Path, patterns: tuple[str, ...]) -> list[Path]`
  - `failed_tests(tree: Path, patterns: tuple[str, ...]) -> set[str] | None` — `None` означает «исход не разобран» (отчётов нет, они битые или тестов в них ноль)

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_test_report.py`:

```python
"""Разбор JUnit XML: имена упавших тестов, а не код возврата.

Кода возврата мало: он не отличает размен (агент починил один тест и сломал
другой) от чистой работы.
"""

from pathlib import Path

from shared import test_report

_PYTEST = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_pricing" name="test_green" time="0.1"/>
<testcase classname="tests.test_pricing" name="test_red" time="0.1">
<failure message="assert 0 == 600">boom</failure></testcase>
<testcase classname="tests.test_cart" name="test_broken" time="0.1">
<error message="ImportError">boom</error></testcase>
</testsuite></testsuites>
"""

# node --test пишет `file` АБСОЛЮТНЫМ путём и `classname="test"` для всех
# тестов сразу — по classname их не различить, а абсолютный путь в базовой
# линии другой (она гоняется в отдельном дереве).
_NODE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testcase name="промо даёт скидку" classname="test"
  file="{tree}/tests/pricing.test.mjs">
<failure type="testCodeFailure" message="0 !== 600">boom</failure></testcase>
<testcase name="здоровье отвечает" classname="test"
  file="{tree}/tests/healthz.test.mjs"/>
</testsuites>
"""


def test_reads_failures_and_errors_from_pytest(tmp_path):
    """`<error>` — тоже падение: тест не прошёл, причина иная."""
    (tmp_path / "junit.xml").write_text(_PYTEST, encoding="utf-8")

    failed = test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS)

    assert failed == {"tests.test_pricing::test_red", "tests.test_cart::test_broken"}


def test_node_paths_are_relative_to_the_tree(tmp_path):
    """Ключ не должен зависеть от того, в каком каталоге гнали тесты.

    Базовая линия снимается в ОТДЕЛЬНОМ дереве. Оставь путь абсолютным — и ни
    один ключ базовой линии не совпадёт с итоговым, а каждое падение будет
    выглядеть своим. Механизм различения перестал бы работать целиком.
    """
    (tmp_path / "junit.xml").write_text(_NODE.format(tree=tmp_path), encoding="utf-8")

    failed = test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS)

    assert failed == {"tests/pricing.test.mjs::промо даёт скидку"}


def test_a_file_attribute_wins_over_a_useless_classname(tmp_path):
    """`classname="test"` у node одинаков для всех — различать нечем."""
    (tmp_path / "junit.xml").write_text(_NODE.format(tree=tmp_path), encoding="utf-8")

    failed = test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS)

    assert not any(key.startswith("test::") for key in failed)


def test_no_report_is_unparsed_not_green(tmp_path):
    """Отсутствие отчёта — НЕ «упавших нет».

    Пустое множество значило бы «своих поломок нет» и открыло бы PR по
    прогону, о котором ничего не известно.
    """
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) is None


def test_broken_xml_is_unparsed(tmp_path):
    (tmp_path / "junit.xml").write_text("<testsuites><oops", encoding="utf-8")
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) is None


def test_zero_tests_is_unparsed(tmp_path):
    """Отчёт без единого теста — раннер не добрался до тестов."""
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0"?><testsuites></testsuites>', encoding="utf-8")
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) is None


def test_green_run_is_an_empty_set_not_none(tmp_path):
    """Зелёный прогон — разобранный исход с пустым множеством."""
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0"?><testsuites><testcase classname="a" name="b"/>'
        '</testsuites>', encoding="utf-8")
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) == set()


def test_finds_maven_and_gradle_reports_without_configuration(tmp_path):
    """Maven и Gradle пишут отчёт сами — настраивать нечего."""
    for rel in ("target/surefire-reports/TEST-a.xml",
                "build/test-results/test/TEST-b.xml"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<?xml version="1.0"?><testsuites>'
                        '<testcase classname="c" name="d"><failure/></testcase>'
                        '</testsuites>', encoding="utf-8")

    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) == {
        "c::d"}
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_test_report.py -q -p no:randomly --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.test_report'`

- [ ] **Step 3: Написать модуль**

Создать `shared/test_report.py`:

```python
"""Имена упавших тестов из отчёта JUnit XML.

Разбирается ОТЧЁТ, а не текст вывода. Вывод в консоль у каждого раннера свой,
и разбор stdout привязал бы контур к двум-трём знакомым; JUnit XML пишут Maven
и Gradle сами без флагов, pytest по `--junit-xml`, `node --test` по
`--test-reporter=junit`, PHPUnit по `--log-junit`.

Оговорка о сегодняшней пользе (спека B4): репозитории организации — Python и
Node, то есть ровно те два раннера, чей вывод пришлось бы разбирать иначе.
Формат выбран не ради мнимой немедленной общности, а чтобы новый стек не
требовал правки этого модуля.
"""
from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree

_log = logging.getLogger("test_report")

# Обычные места отчёта. Maven и Gradle кладут его сюда сами.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "junit*.xml",
    "**/junit*.xml",
    "target/surefire-reports/*.xml",
    "build/test-results/**/*.xml",
)


def find_reports(tree: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Файлы отчётов в дереве. Порядок не важен — читаются все."""
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in tree.glob(pattern):
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                seen.add(resolved)
                found.append(path)
    return found


def _key(case: ElementTree.Element, tree: Path) -> str:
    """Имя теста, одинаковое в любом каталоге прогона.

    `file` предпочтительнее `classname`: node --test ставит всем тестам
    `classname="test"`, и различить их по нему нельзя. Но `file` он пишет
    АБСОЛЮТНЫМ, а базовая линия гоняется в другом дереве — без приведения к
    относительному пути ни один ключ не совпал бы, и каждое падение выглядело
    бы своим.
    """
    scope = case.get("file") or case.get("classname") or ""
    if scope:
        try:
            scope = str(Path(scope).resolve().relative_to(tree.resolve()))
        except ValueError:
            # Не внутри дерева — оставляем как есть: хуже, чем относительный
            # путь, но лучше, чем потерянный тест.
            pass
    return f"{scope}::{case.get('name') or ''}"


def failed_tests(tree: Path, patterns: tuple[str, ...]) -> set[str] | None:
    """Множество имён упавших тестов. `None` — исход НЕ разобран.

    `None` и пустое множество — разные вещи, и путать их нельзя: пустое
    означает «упавших нет», то есть основание открыть PR, а `None` — что о
    прогоне не известно ничего и решать по нему нельзя.
    """
    reports = find_reports(tree, patterns)
    if not reports:
        return None
    failed: set[str] = set()
    total = 0
    parsed_any = False
    for path in reports:
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError) as exc:
            _log.warning("отчёт %s не разобран: %s", path, exc)
            continue
        parsed_any = True
        for case in root.iter("testcase"):
            total += 1
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(_key(case, tree))
    if not parsed_any or total == 0:
        # Ноль тестов — раннер не добрался до них (не поставились зависимости,
        # не нашёлся каталог). Считать это зелёным прогоном нельзя.
        return None
    return failed
```

- [ ] **Step 4: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_test_report.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 5: Прогнать весь набор**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 6: Коммит**

```bash
git add shared/test_report.py tests/test_test_report.py
git commit -m "feat(tests): разбор JUnit XML — имена упавших тестов"
```

---

### Task 2: Диагноз — своя поломка против чужой и мигающей

Закрывает B1, B2, B3, B6, B7, B8, B13, B15, B16, B17, B22, B23, B24.

**Files:**
- Modify: `shared/workflow_types.py` (тип `Diagnosis`)
- Modify: `worker/activities.py` (рядом с `_dev_tests`, `worker/activities.py:3252`)
- Modify: `worker/worker.py` (регистрация)
- Modify: `tests/test_develop_child.py` (гвард перечня активностей)
- Test: `tests/test_dev_diagnose.py`

**Interfaces:**
- Consumes: `shared.test_report.failed_tests(tree, patterns) -> set[str] | None`
- Produces:
  - `Diagnosis(parsed: bool, baseline: list[str], own: list[str], foreign: list[str])`
  - активность `dev_diagnose(issue: IssueInput, baseline: list[str] | None) -> Diagnosis`

- [ ] **Step 1: Завести тип**

В `shared/workflow_types.py` рядом с прочими дата-классами:

```python
@dataclass
class Diagnosis:
    """Разбор красного прогона тестов.

    `parsed=False` — исход не разобран (отчёта нет, он битый, тестов ноль,
    базовый прогон не состоялся). Тогда `own` и `foreign` пусты и смысла не
    несут: решать по ним нельзя, контур обязан вести себя как прежде.
    """
    parsed: bool
    baseline: list[str]
    own: list[str]
    foreign: list[str]
```

- [ ] **Step 2: Написать падающие тесты**

Создать `tests/test_dev_diagnose.py`:

```python
"""Своя поломка против чужой и мигающей.

Отказ, ради которого написано: poh-demo-checkout#166 и #167 — оба прогона
кончились человеком, и ни в одном агент ничего не ломал. `main` красный с
1 сентября из-за истёкшего промокода, а `dev_tests` знает только код возврата.
"""

import activities as a
from shared.workflow_types import IssueInput


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=167, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


def _stub(monkeypatch, tmp_path, *, base, after1, after2):
    """Подменить прогоны: базовый, итоговый и перепроверочный.

    `after1` итоговый прогон уже сделал `dev_tests` — активность его только
    читает; `base` и `after2` она гоняет сама.
    """
    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, tmp_path / "repo"))
    monkeypatch.setattr(a, "_dev_baseline_failures", lambda issue: base)
    monkeypatch.setattr(a, "_dev_rerun_failures", lambda issue: after2)
    monkeypatch.setattr(a, "_dev_last_failures", lambda issue: after1)


async def test_foreign_redness_is_not_ours(monkeypatch, tmp_path):
    """Те же падения, что и без агента, — не его поломки (B3).

    Ровно случай #167: три промо-теста красны и на чистом main.
    """
    three = {"tests/pricing.test.mjs::промо", "tests/pricing.test.mjs::порог",
             "tests/pricing.test.mjs::скидка"}
    _stub(monkeypatch, tmp_path, base=three, after1=three, after2=three)

    d = await a.dev_diagnose(_issue(), None)

    assert d.parsed is True
    assert d.own == []
    assert sorted(d.foreign) == sorted(three)


async def test_a_new_failure_is_ours(monkeypatch, tmp_path):
    three = {"p::a", "p::b", "p::c"}
    _stub(monkeypatch, tmp_path, base=three, after1=three | {"s::мой"},
          after2=three | {"s::мой"})

    d = await a.dev_diagnose(_issue(), None)

    assert d.own == ["s::мой"]
    assert sorted(d.foreign) == ["p::a", "p::b", "p::c"]


async def test_a_swap_is_caught(monkeypatch, tmp_path):
    """Починил один, сломал другой — счёт тот же, множества разные (B3)."""
    _stub(monkeypatch, tmp_path, base={"p::a"}, after1={"s::мой"}, after2={"s::мой"})

    d = await a.dev_diagnose(_issue(), None)

    assert d.own == ["s::мой"]
    assert d.foreign == []


async def test_a_flaky_failure_is_not_charged_to_the_agent(monkeypatch, tmp_path):
    """Упало один раз из двух — мигающий тест, не поломка (B6).

    Без этой проверки мигающий тест вменяется агенту — то же неверное
    вменение, ради устранения которого пишется вся работа.
    """
    _stub(monkeypatch, tmp_path, base=set(), after1={"s::мигает"}, after2=set())

    d = await a.dev_diagnose(_issue(), None)

    assert d.own == []


async def test_an_unparsed_baseline_gives_up_quietly(monkeypatch, tmp_path):
    """База не разобралась — решать нельзя (B15).

    Молчаливое послабление опаснее лишнего отказа: контур, переставший
    замечать поломки из-за сбоя своего разбора, хуже беспокоящего зря.
    """
    _stub(monkeypatch, tmp_path, base=None, after1={"s::x"}, after2={"s::x"})

    d = await a.dev_diagnose(_issue(), None)

    assert d.parsed is False
    assert d.own == []


async def test_an_unparsed_final_run_gives_up_quietly(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, base=set(), after1=None, after2=set())

    d = await a.dev_diagnose(_issue(), None)

    assert d.parsed is False


async def test_a_broken_baseline_run_does_not_raise(monkeypatch, tmp_path):
    """Базовый прогон упал по таймауту или без зависимостей — не отказ (B16).

    Отдельное дерево не несёт `node_modules` и виртуального окружения; там,
    где тесты без них не идут, базовый прогон закономерно падает.
    """
    def boom(issue):
        raise RuntimeError("прогон базовой линии не состоялся")

    _stub(monkeypatch, tmp_path, base=set(), after1={"s::x"}, after2={"s::x"})
    monkeypatch.setattr(a, "_dev_baseline_failures", boom)

    d = await a.dev_diagnose(_issue(), None)

    assert d.parsed is False


async def test_a_given_baseline_is_reused_without_rerunning(monkeypatch, tmp_path):
    """После починки база НЕ снимается заново (B13), мигание не перепроверяется (B8).

    Базовый коммит не менялся, а лишний прогон набора стоит времени;
    подозрительные тесты уже подтверждены дважды.
    """
    calls: list[str] = []
    _stub(monkeypatch, tmp_path, base=set(), after1={"s::мой"}, after2=set())
    monkeypatch.setattr(a, "_dev_baseline_failures",
                        lambda issue: calls.append("base") or set())
    monkeypatch.setattr(a, "_dev_rerun_failures",
                        lambda issue: calls.append("rerun") or set())

    d = await a.dev_diagnose(_issue(), ["p::чужое"])

    assert calls == [], "ни базы, ни перепроверки быть не должно"
    assert d.own == ["s::мой"]
    assert d.baseline == ["p::чужое"]


async def test_a_renamed_test_counts_as_ours(monkeypatch, tmp_path):
    """Переименованный агентом тест — свой (B17).

    Старого имени в базовой линии нет, новое красное. Обратное правило дало бы
    способ спрятать поломку переименованием.
    """
    _stub(monkeypatch, tmp_path, base={"s::старое имя"},
          after1={"s::новое имя"}, after2={"s::новое имя"})

    d = await a.dev_diagnose(_issue(), None)

    assert d.own == ["s::новое имя"]


async def test_signals_separate_our_breakage_from_foreign(monkeypatch, tmp_path):
    """Сигналы слою: `tests_passed` про СВОЁ (B22), чужое отдельно (B24).

    Иначе слой считает неудачей чистую работу в красном репозитории и учится
    на шуме.
    """
    signals: dict = {}
    _stub(monkeypatch, tmp_path, base={"p::чужое"}, after1={"p::чужое"},
          after2={"p::чужое"})
    monkeypatch.setattr(a, "_write_signal",
                        lambda root, name, value: signals.__setitem__(name, value))

    await a.dev_diagnose(_issue(), None)

    assert signals["tests_passed"] is True, "своих поломок нет — для слоя это успех"
    assert signals["tests_red_before"] is True
    assert signals["tests_signal_version"] == 2, "разрыв ряда обязан быть виден (B23)"
```

- [ ] **Step 3: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_dev_diagnose.py -q -p no:randomly --no-cov`
Expected: FAIL — `AttributeError: module 'activities' has no attribute 'dev_diagnose'`

- [ ] **Step 4: Написать прогоны тестов и активность**

В `worker/activities.py` рядом с `_dev_tests`:

```python
def _test_report_patterns() -> tuple[str, ...]:
    """Где искать отчёт. Пусто в конфиге — обычные места (B5)."""
    raw = os.environ.get("DEVELOP_TEST_REPORT", "").strip()
    if not raw:
        return test_report.DEFAULT_PATTERNS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _run_test_command(cwd: Path) -> int:
    """Прогон проверок в указанном дереве. Возвращает код, не бросает.

    Используется базовой линией и перепроверкой на мигание: там красный код —
    это ИСХОД, а не отказ шага.
    """
    command = os.environ.get("DEVELOP_TEST_COMMAND", "").strip()
    result = subprocess.run(command, shell=True, cwd=str(cwd),
                            capture_output=True, text=True,
                            timeout=DEV_TESTS_TIMEOUT_SEC)
    return result.returncode


def _dev_last_failures(issue: IssueInput) -> set[str] | None:
    """Что упало в ИТОГОВОМ прогоне — отчёт уже написан `dev_tests`."""
    _, clone_dir = _dev_paths(issue)
    return test_report.failed_tests(clone_dir, _test_report_patterns())


def _dev_baseline_failures(issue: IssueInput) -> set[str] | None:
    """Что падало БЕЗ правки агента — на отдельном чистом дереве.

    Отдельное дерево, а НЕ `git stash` (B2): сорванный `stash pop` уничтожает
    работу агента — ровно то, что контур научился спасать черновиком. Механизм
    проверки не имеет права уничтожать то, что проверяет.

    Дерево не несёт установленных зависимостей, и там, где тесты без них не
    идут, прогон закономерно упадёт. Это штатный откат к прежнему поведению
    (B16), а не дефект: исход просто окажется неразобранным.
    """
    root, clone_dir = _dev_paths(issue)
    base_tree = root / "baseline"
    shutil.rmtree(base_tree, ignore_errors=True)
    head = subprocess.run(["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(clone_dir), "worktree", "add", "--detach",
                    str(base_tree), head],
                   check=True, capture_output=True, text=True)
    try:
        _run_test_command(base_tree)
        return test_report.failed_tests(base_tree, _test_report_patterns())
    finally:
        # Дерево снимается всегда: оно живёт в общем томе с раннером, а тот
        # ограничен по месту. Осиротевшая регистрация worktree к тому же
        # ломает следующий `worktree add` в тот же путь.
        subprocess.run(["git", "-C", str(clone_dir), "worktree", "remove",
                        "--force", str(base_tree)],
                       capture_output=True, text=True)


def _dev_rerun_failures(issue: IssueInput) -> set[str] | None:
    """Повтор набора на дереве агента — проверка на мигание (B6).

    Перегоняется ВЕСЬ набор, а не подозрительные тесты поимённо (B7): выбор
    отдельных требует синтаксиса конкретного раннера — той самой привязки, от
    которой уходит разбор отчёта.
    """
    _, clone_dir = _dev_paths(issue)
    _run_test_command(clone_dir)
    return test_report.failed_tests(clone_dir, _test_report_patterns())


def _diagnose(issue: IssueInput, baseline: list[str] | None) -> Diagnosis:
    root, _ = _dev_paths(issue)
    unparsed = Diagnosis(parsed=False, baseline=[], own=[], foreign=[])

    after = _dev_last_failures(issue)
    if after is None:
        return unparsed

    if baseline is None:
        base = _dev_baseline_failures(issue)
        if base is None:
            return unparsed
        # Мигающий тест падает в итоговом прогоне и не падает в повторном.
        # Своим считаем только устойчивое падение.
        again = _dev_rerun_failures(issue)
        if again is None:
            return unparsed
        after = after & again
    else:
        base = set(baseline)

    own = sorted(after - base)
    foreign = sorted(after & base)

    # `tests_passed` — про СВОИ поломки (B22): иначе слой саморефлексии считает
    # неудачей чистую работу в красном репозитории и учится на шуме.
    _write_signal(root, "tests_passed", not own)
    _write_signal(root, "tests_red_before", bool(base))
    # Смысл сигнала сменился — ряд разорван (B23). Без признака версии свёртка
    # усреднит несравнимое: до выкладки писали «набор зелёный», после —
    # «агент не сломал своего».
    _write_signal(root, "tests_signal_version", 2)

    return Diagnosis(parsed=True, baseline=sorted(base), own=own, foreign=foreign)


@activity.defn
async def dev_diagnose(issue: IssueInput,
                       baseline: list[str] | None) -> Diagnosis:
    """Чьи это поломки — агента или репозитория.

    `baseline=None` — снять базовую линию и перепроверить на мигание.
    Непустой список — база уже известна (повтор после починки, B13): её не
    снимают заново и на мигание не перепроверяют (B8).

    Диагностика НЕ имеет права ронять прогон: она объясняет отказ тестов, а
    не заменяет его. Любой свой сбой — неразобранный исход, то есть прежнее
    поведение контура.
    """
    try:
        return await _run_with_heartbeat(_diagnose, issue, baseline,
                                         label="dev:diagnose")
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        activity.logger.warning("Develop %s#%s: диагностика не удалась: %s",
                                issue.repo, issue.issue_number, exc)
        return Diagnosis(parsed=False, baseline=[], own=[], foreign=[])
```

Дописать импорты в начало `worker/activities.py`: `from shared import test_report` и `Diagnosis` в существующий импорт из `shared.workflow_types`. `shutil`, `subprocess`, `os`, `Path` там уже есть — проверь фактом, а не на веру.

- [ ] **Step 5: Зарегистрировать активность**

В `worker/worker.py` дописать `activities.dev_diagnose` в оба перечня — рядом с `activities.dev_tests` (список `DEVELOP_ACTIVITIES` и перечень в теле запуска воркера).

В `tests/test_develop_child.py`, в ожидаемый перечень гварда
`test_all_dev_steps_are_registered_activities`, после `activities_module.dev_tests`:

```python
                # Диагностика красного прогона: зовётся не по порядку, а из
                # обработчика отказа тестов — регистрация нужна та же.
                activities_module.dev_diagnose,
```

- [ ] **Step 6: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_dev_diagnose.py tests/test_develop_child.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 7: Прогнать весь набор**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 8: Коммит**

```bash
git add shared/workflow_types.py worker/activities.py worker/worker.py \
        tests/test_dev_diagnose.py tests/test_develop_child.py
git commit -m "feat(develop): диагноз красного прогона — своя поломка против чужой и мигающей"
```

---

### Task 3: Повторный заход агента

Закрывает B9, B11, B12, B18, B21, B24 в части числа заходов.

**Files:**
- Modify: `worker/activities.py`
- Modify: `worker/worker.py`
- Modify: `tests/test_develop_child.py`
- Test: `tests/test_dev_repair.py`

**Interfaces:**
- Consumes: `Diagnosis.own` из Task 2
- Produces: активность `dev_repair(issue: IssueInput, own: list[str]) -> None`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_dev_repair.py`:

```python
"""Повторный заход агента: чинит СВОЁ, в своём же дереве.

Отказ, ради которого написано: прогон разработки кончался человеком даже
тогда, когда поломка была своя и мелкая, — попытки починить не было вовсе.
"""

import activities as a
from shared.workflow_types import IssueInput


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=167, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


def _clone(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir(parents=True)
    (clone / ".task.md").write_text("# исходная постановка\n", encoding="utf-8")
    return clone


async def test_the_brief_names_only_our_failures(monkeypatch, tmp_path):
    """Чужие падения в текст не идут ни в каком виде (B11).

    Агент, увидевший четыре поломки вместо одной, пойдёт чинить чужой код — и
    правка чужого приедет в PR под видом решения задачи. Это хуже отказа.
    """
    clone = _clone(tmp_path)
    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, clone))
    monkeypatch.setattr(a, "_dev_run_agent", lambda issue: "ok")

    await a.dev_repair(_issue(), ["tests/server.test.mjs::мой тест"])

    brief = (clone / ".task.md").read_text(encoding="utf-8")
    assert "мой тест" in brief
    assert "промо" not in brief


async def test_the_agent_keeps_its_own_work(monkeypatch, tmp_path):
    """Починка идёт в ТОМ ЖЕ дереве (B9).

    Чистое дерево означало бы не починку, а второй прогон задачи с нуля.
    """
    clone = _clone(tmp_path)
    (clone / "src.py").write_text("работа агента", encoding="utf-8")
    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, clone))
    monkeypatch.setattr(a, "_dev_run_agent", lambda issue: "ok")

    await a.dev_repair(_issue(), ["s::x"])

    assert (clone / "src.py").read_text(encoding="utf-8") == "работа агента"


async def test_the_run_is_counted_for_the_reflection_layer(monkeypatch, tmp_path):
    """Число заходов — отдельный сигнал (B24)."""
    clone = _clone(tmp_path)
    signals: dict = {}
    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, clone))
    monkeypatch.setattr(a, "_dev_run_agent", lambda issue: "ok")
    monkeypatch.setattr(a, "_write_signal",
                        lambda root, name, value: signals.__setitem__(name, value))

    await a.dev_repair(_issue(), ["s::x"])

    assert signals["repair_attempts"] == 1
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_dev_repair.py -q -p no:randomly --no-cov`
Expected: FAIL — `AttributeError: module 'activities' has no attribute 'dev_repair'`

- [ ] **Step 3: Написать активность**

В `worker/activities.py` рядом с `dev_run_agent`:

```python
def _repair_brief(issue: IssueInput, own: list[str]) -> str:
    """Постановка круга правок. ТОЛЬКО свои падения (B11)."""
    listed = "\n".join(f"- `{name}`" for name in own)
    return (
        f"# Круг правок по Issue #{issue.issue_number}\n\n"
        f"Твоя правка уже лежит в этом рабочем дереве — начинай с неё, не с нуля.\n\n"
        f"После неё упали тесты, которых до правки не было:\n\n{listed}\n\n"
        f"## Что нужно\n\n"
        f"Почини **только эти** падения, не меняя решения задачи.\n\n"
        f"Остальные красные тесты в наборе, если они есть, падали и без твоей "
        f"правки — они не твои и трогать их не нужно.\n"
    )


def _dev_repair(issue: IssueInput, own: list[str]) -> str:
    """Переписать постановку на починку и прогнать того же агента.

    Постановка подменяется прямо в рабочем дереве: агент читает `.task.md`
    (см. `_dev_prepare`), и другого входа у него нет. Файл служебный и
    снимается перед коммитом (`develop.SERVICE_FILES`) — в PR он не уедет.
    """
    root, clone_dir = _dev_paths(issue)
    (clone_dir / ".task.md").write_text(_repair_brief(issue, own), encoding="utf-8")
    _write_signal(root, "repair_attempts", 1)
    return _dev_run_agent(issue)


@activity.defn
async def dev_repair(issue: IssueInput, own: list[str]) -> None:
    """Повторный заход агента: починить своё.

    Отдельная активность, а не флаг у `dev_run_agent` (B12): свой шаг в
    истории Temporal, свой таймаут и видимый факт, что контур пробовал
    починить, а не сдался сразу.

    Возврата нет по той же причине, что и у `dev_run_agent`: хвост вывода —
    килобайты текста, им не место в истории воркфлоу.
    """
    await _run_with_heartbeat(_dev_repair, issue, own, label="dev:repair")
```

- [ ] **Step 4: Зарегистрировать активность**

В `worker/worker.py` дописать `activities.dev_repair` в оба перечня.

В ожидаемый перечень гварда `tests/test_develop_child.py` после `dev_diagnose`:

```python
                # Круг правок: агент чинит своё. Тоже зовётся из обработчика
                # отказа, а не по порядку шагов.
                activities_module.dev_repair,
```

- [ ] **Step 5: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_dev_repair.py tests/test_develop_child.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add worker/activities.py worker/worker.py tests/test_dev_repair.py \
        tests/test_develop_child.py
git commit -m "feat(develop): круг правок — агент чинит свою поломку"
```

---

### Task 4: Красный путь в воркфлоу

> Правка по итогам реализации: `dev_announce_repair` (Task 5) нужна уже
> здесь — воркфлоу её зовёт. Заводи её вместе с этой задачей.

Закрывает B1, B10, B14, B25, B26.

**Files:**
- Modify: `shared/workflow_types.py` (поле `repair_rounds` у `DevelopPlan`)
- Modify: `worker/activities.py` (`_dev_begin` заполняет поле)
- Modify: `worker/workflows.py` (тело `try` в `IssueDevelopment.run`, вызов `activities.dev_tests`)
- Test: `tests/test_dev_repair_workflow.py`
- Test: `tests/test_workflow_replay.py` (прогнать без правок)

**Interfaces:**
- Consumes: `dev_diagnose(issue, baseline) -> Diagnosis`, `dev_repair(issue, own) -> None`
- Produces: ничего для следующих задач

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_dev_repair_workflow.py`. Заглушки штатных шагов копируй по
образцу `tests/test_dev_partial_publish_workflow.py` — фикстуры и заглушки
чужого тестового модуля не видны, и подмена активности идёт ЗАМЕНОЙ по имени,
а не добавлением: Temporal отвергает два определения с одним именем.

```python
"""Красный прогон: диагноз, починка, повторная проверка.

Отказ, ради которого написано: #166 и #167 — оба прогона кончились человеком,
хотя агент ничего не ломал.
"""

import inspect
import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shared.workflow_types import DevelopPlan, Diagnosis, IssueInput
from workflows import IssueDevelopment

_calls: list[str] = []
_repair_args: list[list[str]] = []


@activity.defn(name="dev_begin")
async def begin_local(issue: IssueInput) -> DevelopPlan:
    _calls.append("begin")
    return DevelopPlan(mode="local", branch="research/issue-167", repair_rounds=1)


@activity.defn(name="dev_dispatch")
async def dispatch_stub(issue: IssueInput, branch: str) -> None:
    _calls.append("dispatch")


@activity.defn(name="dev_prepare")
async def prepare_ok(issue: IssueInput, branch: str) -> int:
    _calls.append("prepare")
    return 1


@activity.defn(name="dev_announce")
async def announce_ok(issue: IssueInput, branch: str) -> None:
    _calls.append("announce")


@activity.defn(name="build_mvp_plan")
async def plan_ok(issue: IssueInput, branch: str) -> bool:
    _calls.append("plan")
    return True


@activity.defn(name="dev_run_agent")
async def agent_ok(issue: IssueInput) -> None:
    _calls.append("agent")


@activity.defn(name="dev_followups")
async def followups_ok(issue: IssueInput) -> list[int]:
    _calls.append("followups")
    return []


@activity.defn(name="dev_tests")
async def checks_ok(issue: IssueInput) -> None:
    _calls.append("tests")


@activity.defn(name="dev_publish")
async def publish_ok(issue: IssueInput, branch: str, foreign: list[str]) -> int | None:
    _calls.append("publish")
    return 101


@activity.defn(name="capture_episode")
async def episode_ok(issue: IssueInput, branch: str, pr_number: int | None) -> bool:
    _calls.append("capture_episode")
    return True


@activity.defn(name="dev_publish_partial")
async def partial_stub(issue: IssueInput, branch: str, reason: str) -> int | None:
    _calls.append("partial")
    return 42


@activity.defn(name="dev_announce_repair")
async def announce_repair_stub(issue: IssueInput, own: list[str]) -> None:
    _calls.append("announce_repair")


BASE = [begin_local, dispatch_stub, prepare_ok, announce_ok, plan_ok, agent_ok,
        followups_ok, checks_ok, publish_ok, episode_ok, partial_stub,
        announce_repair_stub]


@activity.defn(name="dev_tests")
async def checks_fail(issue: IssueInput) -> None:
    _calls.append("tests")
    raise RuntimeError("проверки не прошли (код 1):\n# fail 3")


def _acts(*overrides):
    def _name(fn):
        return activity._Definition.must_from_callable(fn).name

    replaced = {_name(fn): fn for fn in overrides}
    base = [replaced.pop(_name(fn), fn) for fn in BASE]
    return [*base, *replaced.values()]


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=167, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


async def _run(env, acts, expect_failure: bool):
    tq = f"tq-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=tq, workflows=[IssueDevelopment],
                      activities=acts):
        if expect_failure:
            with pytest.raises(Exception) as excinfo:
                await env.client.execute_workflow(
                    IssueDevelopment.run, _issue(), id=f"wf-{uuid.uuid4()}",
                    task_queue=tq)
            return excinfo.value
        return await env.client.execute_workflow(
            IssueDevelopment.run, _issue(), id=f"wf-{uuid.uuid4()}", task_queue=tq)


@pytest.mark.timeout(60)
async def test_a_green_run_costs_nothing_extra():
    """Зелёный прогон не гоняет ни диагноза, ни починки (B1)."""
    _calls.clear()

    @activity.defn(name="dev_diagnose")
    async def diagnose_stub(issue: IssueInput,
                            baseline: list[str] | None) -> Diagnosis:
        _calls.append("diagnose")
        return Diagnosis(parsed=True, baseline=[], own=[], foreign=[])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        number = await _run(env, _acts(diagnose_stub), expect_failure=False)

    assert number == 101
    assert "diagnose" not in _calls
    assert "repair" not in _calls


@pytest.mark.timeout(60)
async def test_foreign_redness_publishes_instead_of_failing():
    """Чужая краснота — прогон УДАЛСЯ (B14). Ровно случай #167."""
    _calls.clear()

    @activity.defn(name="dev_diagnose")
    async def diagnose_foreign(issue: IssueInput,
                               baseline: list[str] | None) -> Diagnosis:
        _calls.append("diagnose")
        return Diagnosis(parsed=True, baseline=["p::a"], own=[], foreign=["p::a"])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        number = await _run(env, _acts(checks_fail, diagnose_foreign),
                            expect_failure=False)

    assert number == 101, "PR должен открыться, человека звать не за чем"
    assert "repair" not in _calls
    assert "partial" not in _calls, "черновик тут не при чём — прогон удался"


@pytest.mark.timeout(60)
async def test_our_breakage_triggers_exactly_one_repair_round():
    """Своя поломка чинится, и ровно один раз (B9, B10)."""
    _calls.clear()
    _repair_args.clear()
    seen = {"n": 0}

    @activity.defn(name="dev_diagnose")
    async def diagnose_own(issue: IssueInput,
                           baseline: list[str] | None) -> Diagnosis:
        _calls.append("diagnose")
        seen["n"] += 1
        if seen["n"] == 1:
            return Diagnosis(parsed=True, baseline=["p::a"], own=["s::мой"],
                             foreign=["p::a"])
        return Diagnosis(parsed=True, baseline=["p::a"], own=[], foreign=["p::a"])

    @activity.defn(name="dev_repair")
    async def repair_stub(issue: IssueInput, own: list[str]) -> None:
        _calls.append("repair")
        _repair_args.append(own)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        number = await _run(env, _acts(checks_fail, diagnose_own, repair_stub),
                            expect_failure=False)

    assert number == 101, "починка удалась — PR обязан открыться"
    assert _calls.count("repair") == 1
    assert _repair_args == [["s::мой"]], "агенту уходят только СВОИ падения"
    assert _calls.count("tests") == 2, "после починки набор гоняется заново"


@pytest.mark.timeout(60)
async def test_a_failed_repair_ends_with_a_human_and_a_draft():
    """Не починил — отказ, черновик, человек (B10, B26)."""
    _calls.clear()

    @activity.defn(name="dev_diagnose")
    async def diagnose_own(issue: IssueInput,
                           baseline: list[str] | None) -> Diagnosis:
        _calls.append("diagnose")
        return Diagnosis(parsed=True, baseline=[], own=["s::мой"], foreign=[])

    @activity.defn(name="dev_repair")
    async def repair_stub(issue: IssueInput, own: list[str]) -> None:
        _calls.append("repair")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, _acts(checks_fail, diagnose_own, repair_stub),
                   expect_failure=True)

    assert _calls.count("repair") == 1, "заход обязан быть один"
    assert "partial" in _calls, "работа агента должна уехать черновиком"


@pytest.mark.timeout(60)
async def test_an_unparsed_diagnosis_keeps_the_old_behaviour():
    """Исход не разобран — ведём себя как прежде (B15).

    Ни починки, ни публикации: отказ тестов остаётся отказом.
    """
    _calls.clear()

    @activity.defn(name="dev_diagnose")
    async def diagnose_blind(issue: IssueInput,
                             baseline: list[str] | None) -> Diagnosis:
        _calls.append("diagnose")
        return Diagnosis(parsed=False, baseline=[], own=[], foreign=[])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        err = await _run(env, _acts(checks_fail, diagnose_blind),
                         expect_failure=True)

    assert "repair" not in _calls
    assert "partial" in _calls
    parts = []
    cur: BaseException | None = err
    while cur is not None:
        parts.append(str(cur))
        cur = cur.__cause__
    assert "fail 3" in "\n".join(parts), "наружу должна уйти причина отказа тестов"


@pytest.mark.timeout(60)
async def test_repair_can_be_turned_off_entirely():
    """`repair_rounds=0` — починки нет, поведение прежнее (B10)."""
    _calls.clear()

    @activity.defn(name="dev_begin")
    async def begin_no_repair(issue: IssueInput) -> DevelopPlan:
        _calls.append("begin")
        return DevelopPlan(mode="local", branch="b", repair_rounds=0)

    @activity.defn(name="dev_diagnose")
    async def diagnose_own(issue: IssueInput,
                           baseline: list[str] | None) -> Diagnosis:
        _calls.append("diagnose")
        return Diagnosis(parsed=True, baseline=[], own=["s::мой"], foreign=[])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, _acts(begin_no_repair, checks_fail, diagnose_own),
                   expect_failure=True)

    assert "repair" not in _calls


def test_repair_loop_patch_marker_is_frozen():
    """Идентификатор патча — часть истории идущих прогонов разработки."""
    src = inspect.getsource(IssueDevelopment.run)
    assert 'workflow.patched("issue-development-repair-loop")' in src
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_dev_repair_workflow.py -q -p no:randomly --no-cov`
Expected: FAIL — диагноз не вызывается, `dev_publish` не принимает `foreign`.

- [ ] **Step 3: Завести число заходов настройкой**

В `shared/workflow_types.py` дописать поле в `DevelopPlan`:

```python
    # Сколько раз агент пробует починить своё. Умолчание 1: второй заход
    # удваивает худший случай прогона (агент идёт до 45 минут). Поднимать
    # стоит, когда наберётся статистика, сколько починок удаётся.
    repair_rounds: int = 1
```

Умолчание обязательно: поле читается из истории прогонов, начатых до выкладки,
где его не было вовсе.

В `worker/activities.py`, где собирается `DevelopPlan` (функция за активностью
`dev_begin`), заполнить поле из окружения:

```python
        repair_rounds=max(0, int(os.environ.get("DEVELOP_REPAIR_ROUNDS", "1") or 1)),
```

Читает окружение АКТИВНОСТЬ, а не воркфлоу: решение воркфлоу обязано быть
детерминированным при реплее, а результат активности лежит в истории — повтор
возьмёт то же значение, что и первый прогон (см. докстринг `DevelopPlan`).
`0` выключает починку целиком, не трогая остального.

- [ ] **Step 4: Поставить красный путь**

В `worker/workflows.py` заменить одиночный вызов `activities.dev_tests` на
разбор красного исхода. Точное место — внутри существующего `try`, между
`dev_followups` и `dev_publish`:

```python
            foreign: list[str] = []
            try:
                await workflow.execute_activity(
                    activities.dev_tests, issue,
                    start_to_close_timeout=timedelta(seconds=1800),
                    heartbeat_timeout=timedelta(seconds=300),
                    retry_policy=once,
                )
            except Exception as tests_exc:                 # noqa: BLE001
                # Красный прогон — ещё не приговор: тесты могли падать и без
                # правки агента. Ровно это случилось на #166 и #167, где `main`
                # был красный из-за истёкшего промокода, а прогон списали в
                # отказ вместе с работой агента.
                #
                # ПОД МАРКЕРОМ: новые команды в теле воркфлоу роняют
                # недетерминизмом прогоны, начатые до выкладки.
                if not workflow.patched("issue-development-repair-loop"):
                    raise
                last_exc: BaseException = tests_exc
                diagnosis = await workflow.execute_activity(
                    activities.dev_diagnose, args=[issue, None],
                    start_to_close_timeout=timedelta(seconds=3900),
                    heartbeat_timeout=timedelta(seconds=300),
                    retry_policy=once,
                )
                if not diagnosis.parsed:
                    # Об исходе не известно ничего — решать по нему нельзя.
                    raise
                # Заходов ровно `plan.repair_rounds` (умолчание 1). Число
                # приходит из активности, а не из окружения: решение воркфлоу
                # обязано быть детерминированным при реплее, и прочитанное
                # прямо здесь `os.environ` дало бы разное значение до и после
                # правки переменной — см. докстринг `DevelopPlan`.
                rounds = 0
                while diagnosis.own and rounds < plan.repair_rounds:
                    rounds += 1
                    await workflow.execute_activity(
                        activities.dev_announce_repair,
                        args=[issue, diagnosis.own],
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=cheap,
                    )
                    await workflow.execute_activity(
                        activities.dev_repair, args=[issue, diagnosis.own],
                        start_to_close_timeout=timedelta(seconds=3600),
                        heartbeat_timeout=timedelta(seconds=300),
                        retry_policy=once,
                    )
                    # Повторный прогон НЕ роняет ветку своим отказом: при
                    # чужой красноте он красный всегда, и падение наружу
                    # означало бы, что починку невозможно признать удавшейся
                    # ни в одном репозитории с чужой краснотой — то есть ровно
                    # там, ради чего всё это писалось. Решает диагноз ниже.
                    try:
                        await workflow.execute_activity(
                            activities.dev_tests, issue,
                            start_to_close_timeout=timedelta(seconds=1800),
                            heartbeat_timeout=timedelta(seconds=300),
                            retry_policy=once,
                        )
                    except Exception as retry_exc:         # noqa: BLE001
                        last_exc = retry_exc
                    # База та же: базовый коммит не менялся, а лишний прогон
                    # набора стоит времени. Мигание не перепроверяем — эти
                    # тесты уже подтверждены дважды.
                    diagnosis = await workflow.execute_activity(
                        activities.dev_diagnose,
                        args=[issue, diagnosis.baseline],
                        start_to_close_timeout=timedelta(seconds=1900),
                        heartbeat_timeout=timedelta(seconds=300),
                        retry_policy=once,
                    )
                    if not diagnosis.parsed:
                        # Об исходе повторного прогона не известно ничего.
                        raise last_exc
                if diagnosis.own:
                    # Заходы кончились, свои падения остались — человек.
                    raise last_exc
                foreign = diagnosis.foreign
            number = await workflow.execute_activity(
                activities.dev_publish, args=[issue, plan.branch, foreign],
                start_to_close_timeout=timedelta(seconds=600),
                heartbeat_timeout=timedelta(seconds=300),
                retry_policy=cheap,
            )
```

Обрати внимание: `raise` без аргумента — наружу уходит **исходная** ошибка
тестов, а не что-то собранное заново. Причина отказа не подменяется.

Второй прогон `dev_tests` в ветке починки бросит своё исключение, если тесты
снова красные; его подхватит тот же `except` уровнем выше — перехват частичной
выкладки, — и работа агента уедет черновиком.

- [ ] **Step 5: Прогнать тесты красного пути**

Run: `python -m pytest tests/test_dev_repair_workflow.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 6: Прогнать гвард реплея — главная проверка задачи**

Run: `python -m pytest tests/test_workflow_replay.py -q -p no:randomly --no-cov`
Expected: PASS. Красный гвард означает, что маркер поставлен не там или не
поставлен вовсе, и выкладка убьёт идущие прогоны разработки. Чинить, а не
обходить.

- [ ] **Step 7: Коммит**

```bash
git add shared/workflow_types.py worker/activities.py worker/workflows.py \
        tests/test_dev_repair_workflow.py
git commit -m "feat(develop): красный прогон диагностируется и чинится

Маркер issue-development-repair-loop обязателен: у идущих прогонов в истории
на этом месте нет ни диагноза, ни починки."
```

---

### Task 5: Что видит человек

Закрывает B19, B20, B21.

**Files:**
- Modify: `worker/activities.py` (`_dev_publish`, `dev_publish`, новая `dev_announce_repair`)
- Modify: `shared/develop.py` (`pr_body`)
- Modify: `worker/worker.py`, `tests/test_develop_child.py` (регистрация)
- Test: `tests/test_dev_repair_messages.py`

**Interfaces:**
- Consumes: `Diagnosis.foreign`, `Diagnosis.own`
- Produces:
  - `dev_publish(issue: IssueInput, branch: str, foreign: list[str]) -> int | None`
  - `dev_announce_repair(issue: IssueInput, own: list[str]) -> None`
  - `develop.pr_body(issue_number: int, *, branch: str, foreign: list[str]) -> str`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_dev_repair_messages.py`:

```python
"""Что человек читает в ленте и в PR.

Молчащий контур, который внутри себя делает второй дорогой заход,
неотличим от зависшего.
"""

import activities as a
from shared import develop
from shared.workflow_types import IssueInput


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=167, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


def test_pr_body_names_foreign_redness(monkeypatch):
    """Красный набор без объяснения ушёл бы на ревью как загадка (B19)."""
    body = develop.pr_body(167, branch="research/issue-167",
                           foreign=["tests/pricing.test.mjs::промо"])

    assert "промо" in body
    assert "до правки" in body


def test_pr_body_says_nothing_when_the_suite_is_green(monkeypatch):
    """Нет чужой красноты — нет и оговорки: лишний абзац в каждом PR."""
    body = develop.pr_body(167, branch="research/issue-167", foreign=[])

    assert "до правки" not in body


async def test_the_repair_round_is_announced(monkeypatch):
    """Контур говорит, что чинит и что именно (B21)."""
    posted: list = []
    monkeypatch.setattr(a.github_client, "post_comment",
                        lambda repo, number, body: posted.append(body))

    await a.dev_announce_repair(_issue(), ["tests/server.test.mjs::мой"])

    assert "мой" in posted[0]
    assert "чиню" in posted[0].lower() or "починк" in posted[0].lower()


async def test_announcing_the_repair_never_stops_it(monkeypatch):
    """Отказ сообщения не имеет права сорвать починку."""
    def boom(*args, **kwargs):
        raise RuntimeError("GitHub отказал")

    monkeypatch.setattr(a.github_client, "post_comment", boom)

    await a.dev_announce_repair(_issue(), ["s::x"])  # не бросает
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python -m pytest tests/test_dev_repair_messages.py -q -p no:randomly --no-cov`
Expected: FAIL — `pr_body() got an unexpected keyword argument 'foreign'`

- [ ] **Step 3: Дописать оговорку в тело PR**

Функция сегодня возвращает одну склеенную строку. Заменить её тело целиком в
`shared/develop.py`:

```python
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
```

`foreign` именованный и БЕЗ умолчания: молчаливое умолчание позволило бы
вызвать функцию, забыв протащить список, и оговорка тихо исчезла бы из PR.
Найди все вызовы `pr_body` и передай список в каждом.

- [ ] **Step 4: Протащить `foreign` до выкладки**

В `worker/activities.py` — три правки. Сигнатура исполнителя:

```python
def _dev_publish(issue: IssueInput, branch: str, foreign: list[str]) -> int | None:
```

Внутри него — вызов сборки тела:

```python
        body=develop.pr_body(issue.issue_number, branch=branch, foreign=foreign),
```

И сама активность:

```python
@activity.defn
async def dev_publish(issue: IssueInput, branch: str,
                      foreign: list[str]) -> int | None:
    """Шаг 6: коммит, пуш и PR — руками воркера, его токеном.

    `None` — агент не изменил ни одного файла. Это не сбой шага, а его
    результат; решение, что делать с пустым прогоном, принимает воркфлоу.

    `foreign` — тесты, красные и без правки агента. Уходят оговоркой в тело
    PR: красный набор без объяснения смотрящий примет за поломку агента и
    пойдёт разбирать его правку.
    """
    return await _run_with_heartbeat(_dev_publish, issue, branch, foreign,
                                     label="dev:publish")
```

**Все вызовы `dev_publish` обязаны передать все три аргумента** — Temporal при
несовпадении числа выбрасывает типы и отдаёт словари вместо объектов. Гвард
`tests/test_activity_arg_types.py` это ловит; найди каждый вызов и передай
список — там, где чужая краснота не разбиралась, пустой:

```bash
grep -rn "dev_publish" worker/ tests/
```

`_dev_publish_partial` НЕ трогаем: у сорванного прогона чужая краснота не
разбиралась, и обещать её разбор в черновике было бы неправдой.

- [ ] **Step 5: Написать сообщение о починке**

В `worker/activities.py` рядом с `dev_repair`:

```python
@activity.defn
async def dev_announce_repair(issue: IssueInput, own: list[str]) -> None:
    """Сказать в ленте, что контур чинит своё и что именно.

    Молчащий контур, который внутри себя делает второй дорогой заход
    (агент идёт до 45 минут), неотличим от зависшего.

    Сообщение не имеет права сорвать починку: отказ гасится здесь.
    """
    listed = "\n".join(f"- `{name}`" for name in own)
    try:
        await asyncio.to_thread(
            github_client.post_comment, issue.repo, issue.issue_number,
            f"## 🔁 Чиню своё\n\n"
            f"После правки упали тесты, которых до неё не было:\n\n{listed}\n\n"
            f"Отправляю агента на повторный заход — он правит только эти "
            f"падения. Остальные красные тесты в наборе, если они есть, "
            f"падали и без правки.\n\n"
            f"Заход один: не починит — отдам задачу человеку.")
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        activity.logger.warning("Develop %s#%s: о починке не сообщено: %s",
                                issue.repo, issue.issue_number, exc)
```

Зарегистрировать `activities.dev_announce_repair` в `worker/worker.py` и в
ожидаемом перечне гварда `tests/test_develop_child.py`:

```python
                # Сообщение о починке: без него второй дорогой заход выглядит
                # зависанием.
                activities_module.dev_announce_repair,
```

- [ ] **Step 6: Дописать причину отказа после неудавшейся починки**

Отказ после починки должен говорить, что попытка была (B20). Перехват
частичной выкладки берёт текст из `_failure_reason(exc)` — это исключение
второго прогона `dev_tests`, и слова про попытку в нём нет.

В `worker/workflows.py`, в ветке `if not diagnosis.parsed or diagnosis.own:`
перед `raise` дописать строку в лог воркфлоу и пометить прогон:

```python
                    if not diagnosis.parsed or diagnosis.own:
                        # Человеку важно отличить «агент не смог» от «контур
                        # не пробовал»: попытка была, и она видна в ленте
                        # сообщением `dev_announce_repair` выше.
                        workflow.logger.warning(
                            "починка не удалась, осталось своих падений: %s",
                            len(diagnosis.own))
                        raise
```

- [ ] **Step 7: Прогнать и убедиться, что прошло**

Run: `python -m pytest tests/test_dev_repair_messages.py tests/test_activity_arg_types.py tests/test_develop_child.py -q -p no:randomly --no-cov`
Expected: PASS

- [ ] **Step 8: Прогнать весь набор**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 9: Коммит**

```bash
git add worker/activities.py worker/worker.py shared/develop.py \
        tests/test_dev_repair_messages.py tests/test_develop_child.py \
        worker/workflows.py
git commit -m "feat(develop): человек видит чужую красноту и факт починки"
```

---

### Task 6: Сквозной путь на настоящем репозитории

**Files:**
- Test: `tests/test_dev_repair_e2e.py`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: ничего

- [ ] **Step 1: Написать сквозной тест**

Идёт через настоящий git и настоящий прогон тестов: проверка «есть ли своя
поломка» опирается на поведение git worktree и на разбор отчёта, и подменять
их значит проверять собственную выдумку.

Создать `tests/test_dev_repair_e2e.py`:

```python
"""Сквозной путь: красный main, чистая работа агента, никакого человека.

Отказ, ради которого написано: poh-demo-checkout#166 и #167 — `main` красный
с 1 сентября из-за истёкшего промокода, оба прогона списаны в отказ вместе с
работой агента, которая ничего не ломала.
"""

import subprocess

import activities as a
from shared.workflow_types import IssueInput

_TEST_FILE = """import test from 'node:test';
import assert from 'node:assert';
test('чужой красный', () => assert.strictEqual(0, 1));
test('зелёный', () => assert.strictEqual(1, 1));
"""


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=167, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


def _repo(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    tests = clone / "tests"
    tests.mkdir()
    (tests / "a.test.mjs").write_text(_TEST_FILE, encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "seed"], check=True)
    return clone


async def test_foreign_redness_is_not_charged_to_the_agent(monkeypatch, tmp_path):
    """Агент правит файл, не трогая красный тест, — своих поломок нет."""
    clone = _repo(tmp_path)
    (clone / "src.mjs").write_text("// правка агента\n", encoding="utf-8")

    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, clone))
    monkeypatch.setenv(
        "DEVELOP_TEST_COMMAND",
        'node --test --test-reporter=junit --test-reporter-destination=junit.xml '
        '"tests/*.test.mjs" || true')
    monkeypatch.delenv("DEVELOP_TEST_REPORT", raising=False)

    # Итоговый прогон делает `dev_tests`; здесь его роль играет прямой запуск.
    a._run_test_command(clone)

    d = await a.dev_diagnose(_issue(), None)

    assert d.parsed is True, "отчёт обязан разобраться на настоящем прогоне"
    assert d.own == [], "агент не трогал красный тест — поломка не его"
    assert d.foreign == ["tests/a.test.mjs::чужой красный"]


async def test_the_agents_work_survives_the_diagnosis(monkeypatch, tmp_path):
    """Базовая линия НЕ трогает дерево агента (B2).

    Если снимать её прятанием правок (`git stash`), сорванный `stash pop`
    уничтожит работу агента — ровно то, что контур научился спасать черновиком.
    Механизм проверки не имеет права уничтожать то, что проверяет.
    """
    clone = _repo(tmp_path)
    (clone / "src.mjs").write_text("// правка агента\n", encoding="utf-8")

    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, clone))
    monkeypatch.setenv(
        "DEVELOP_TEST_COMMAND",
        'node --test --test-reporter=junit --test-reporter-destination=junit.xml '
        '"tests/*.test.mjs" || true')
    monkeypatch.delenv("DEVELOP_TEST_REPORT", raising=False)
    a._run_test_command(clone)

    await a.dev_diagnose(_issue(), None)

    assert (clone / "src.mjs").read_text(encoding="utf-8") == "// правка агента\n"


async def test_a_breakage_the_agent_introduced_is_ours(monkeypatch, tmp_path):
    """Агент ломает зелёный тест — падение засчитывается ему."""
    clone = _repo(tmp_path)
    broken = _TEST_FILE.replace("assert.strictEqual(1, 1)", "assert.strictEqual(1, 2)")
    (clone / "tests" / "a.test.mjs").write_text(broken, encoding="utf-8")

    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, clone))
    monkeypatch.setenv(
        "DEVELOP_TEST_COMMAND",
        'node --test --test-reporter=junit --test-reporter-destination=junit.xml '
        '"tests/*.test.mjs" || true')
    monkeypatch.delenv("DEVELOP_TEST_REPORT", raising=False)

    a._run_test_command(clone)

    d = await a.dev_diagnose(_issue(), None)

    assert d.own == ["tests/a.test.mjs::зелёный"]
    assert d.foreign == ["tests/a.test.mjs::чужой красный"]
```

Если `node` в окружении прогона отсутствует, пометь модуль
`pytest.importorskip`-эквивалентом: `pytest.mark.skipif(shutil.which("node") is None,
reason="нужен node")`. Молча зелёный тест без node хуже пропущенного.

- [ ] **Step 2: Прогнать**

Run: `python -m pytest tests/test_dev_repair_e2e.py -q -p no:randomly --no-cov`
Expected: PASS (или SKIP, если в окружении нет `node`).

- [ ] **Step 3: Прогнать весь набор и гвард реплея**

Run: `python -m pytest -q -p no:randomly`
Expected: PASS, покрытие не ниже 83%.

- [ ] **Step 4: Коммит**

```bash
git add tests/test_dev_repair_e2e.py
git commit -m "test(develop): сквозной путь — чужая краснота и своя поломка на настоящем git"
```

---

## Что остаётся человеку после плана

- **Флаг отчёта в `DEVELOP_TEST_COMMAND` на стенде.** Без него pytest и node
  отчёта не пишут, и контур останется на прежнем поведении (B15) — правка
  строки в `.env`, не кода:
  `pytest -q --junit-xml=junit.xml` и
  `node --test --test-reporter=junit --test-reporter-destination=junit.xml`.
  Оба раннера должны продолжать возвращать ненулевой код на красноте —
  `dev_tests` опирается на него.
- **Живой прогон.** План закрывается модульными проверками, сквозным тестом и
  гвардом реплея; работает ли механизм на стенде, покажет живая задача в
  `poh-demo-checkout` — там `main` красный, и это ровно нужное условие.
- **Пустой `DEVELOP_TEST_COMMAND`** оставляет всё как есть: `dev_tests`
  пропускает шаг и не бросает, красный путь не запускается вовсе (B18).
  Отдельной правки не требует.
- **Число заходов починки** — `DEVELOP_REPAIR_ROUNDS`, умолчание 1 (B10). Поднимать её
  стоит, когда наберётся статистика, сколько починок удаётся.
- **CI по расписанию**, ловящий бомбы с часовым механизмом, — отдельный
  блокер, эта работа его не закрывает.
