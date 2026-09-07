"""Сквозной путь: красный main, чистая работа агента, никакого человека.

Отказ, ради которого написано: poh-demo-checkout#166 и #167 — `main` красный
с 1 сентября из-за истёкшего промокода, оба прогона списаны в отказ вместе с
работой агента, которая ничего не ломала.
"""

import shutil
import subprocess

import pytest

from poh_developer import activities as a
from poh_developer.workflow_types import IssueInput

# Без node тесты не проверяют ничего: разбор отчёта опирается на настоящий
# прогон. Молча зелёный тест хуже пропущенного — он врёт, что механизм цел.
pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="нужен node для настоящего прогона тестов")

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
    # Ключ кончается именем теста; префикс зависит от того, пишет ли раннер
    # атрибут `file` — старые Node его не пишут, и тогда остаётся `classname`.
    # Проверяем инвариант «опознан тот самый тест», а не форму ключа.
    assert [k.split("::")[-1] for k in d.foreign] == ["чужой красный"]


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

    assert [k.split("::")[-1] for k in d.own] == ["зелёный"]
    assert [k.split("::")[-1] for k in d.foreign] == ["чужой красный"]
