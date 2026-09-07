"""Повторный заход агента: чинит СВОЁ, в своём же дереве.

Отказ, ради которого написано: прогон разработки кончался человеком даже
тогда, когда поломка была своя и мелкая, — попытки починить не было вовсе.
"""

from poh_developer import activities as a
from poh_developer.workflow_types import IssueInput


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
