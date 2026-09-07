"""Что человек читает в ленте и в PR.

Молчащий контур, который внутри себя делает второй дорогой заход,
неотличим от зависшего.
"""

from poh_developer import activities as a
from poh_developer import develop
from poh_developer.workflow_types import IssueInput


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


async def test_the_repair_round_is_announced(monkeypatch, github):
    """Контур говорит, что чинит и что именно (B21)."""
    posted: list = []
    monkeypatch.setattr(github, "post_comment",
                        lambda repo, number, body: posted.append(body))

    await a.dev_announce_repair(_issue(), ["tests/server.test.mjs::мой"])

    assert "мой" in posted[0]
    assert "чиню" in posted[0].lower() or "починк" in posted[0].lower()


async def test_announcing_the_repair_never_stops_it(monkeypatch, github):
    """Отказ сообщения не имеет права сорвать починку."""
    def boom(*args, **kwargs):
        raise RuntimeError("GitHub отказал")

    monkeypatch.setattr(github, "post_comment", boom)

    await a.dev_announce_repair(_issue(), ["s::x"])  # не бросает
