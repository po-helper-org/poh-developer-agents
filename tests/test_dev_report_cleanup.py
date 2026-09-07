"""Отчёт о тестах не уезжает в пул-реквест.

Отказ, ради которого написано: `junit.xml` пишется в рабочее дерево, а
`publish_worktree` забирает дерево целиком через `git add -A`. Без снятия
отчёт уехал бы в PR мусором и — хуже — обманул бы гвард «изменений нет,
открывать нечего»: прогон, где агент не тронул ни строки, всё равно открыл бы
пул-реквест. Ровно это уже случалось с `.task.md`.
"""

import subprocess

from poh_developer import activities as a


def _repo(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    (clone / "README.md").write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "seed"], check=True)
    return clone


def test_a_generated_report_is_removed(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVELOP_TEST_REPORT", raising=False)
    clone = _repo(tmp_path)
    (clone / "junit.xml").write_text("<testsuites/>", encoding="utf-8")

    removed = a._clear_test_reports(clone)

    assert removed == ["junit.xml"]
    assert not (clone / "junit.xml").exists()


def test_a_report_committed_by_the_repository_is_left_alone(tmp_path, monkeypatch):
    """Отслеживаемый файл принадлежит репозиторию, а не нашему прогону.

    Снести его значило бы показать в PR удаление, которого никто не просил.
    """
    monkeypatch.delenv("DEVELOP_TEST_REPORT", raising=False)
    clone = _repo(tmp_path)
    (clone / "junit.xml").write_text("<testsuites/>", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "junit.xml"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "report"], check=True)

    removed = a._clear_test_reports(clone)

    assert removed == []
    assert (clone / "junit.xml").exists()


def test_no_report_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVELOP_TEST_REPORT", raising=False)
    assert a._clear_test_reports(_repo(tmp_path)) == []
