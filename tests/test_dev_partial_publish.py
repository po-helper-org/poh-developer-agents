"""Сорванный прогон разработки оставляет материал для разбора.

Отказ, ради которого написано: `poh-demo-checkout#166`, 2 сентября 2026.
OpenHands отработал тринадцать минут и внёс правки, затем упали три теста из
семидесяти трёх — и не осталось ни ветки, ни PR, ни диффа. `dev_publish` в
`IssueDevelopment` идёт ПОСЛЕ `dev_tests` и просто не выполнился.
"""

from poh_developer import activities as a
from poh_developer.workflow_types import IssueInput


def _issue() -> IssueInput:
    return IssueInput(repo="o/r", issue_number=166, title="t", body="b",
                      author_login="u", author_type="User", interactive=True)


def _no_worktree_prep(monkeypatch, tmp_path):
    """Снять подготовку дерева: здесь проверяется решение, а не работа с git."""
    monkeypatch.setattr(a, "_dev_paths", lambda issue: (tmp_path, tmp_path / "clone"))
    monkeypatch.setattr(a.develop, "clear_service_files",
                        lambda clone_dir, keep_dir: [])


async def test_a_broken_run_opens_a_draft_and_says_so(monkeypatch, tmp_path, github):
    """Ветка, черновик и честный комментарий — то, чего не было на #166."""
    _no_worktree_prep(monkeypatch, tmp_path)
    published: dict = {}
    posted: list = []
    monkeypatch.setattr(github, "publish_worktree",
                        lambda repo, clone, branch, **kw:
                            published.update(branch=branch, draft=kw.get("draft"),
                                             title=kw.get("title"),
                                             body=kw.get("body")) or 42)
    monkeypatch.setattr(github, "post_comment",
                        lambda repo, number, body: posted.append(body))

    number = await a.dev_publish_partial(_issue(), "feature/166",
                                         "проверки не прошли (код 1):\n# fail 3")

    assert number == 42
    assert published["draft"] is True
    assert published["branch"] == a.develop.work_branch(166)
    assert "fail 3" in published["body"], "причина обязана быть в самом PR"
    assert len(posted) == 1
    assert "42" in posted[0], "человеку нужна ссылка на черновик"
    assert "fail 3" in posted[0]


async def test_the_comment_names_the_failure_not_readiness(monkeypatch, tmp_path, github):
    """Работа заведомо негодная — обещать готовность нельзя.

    «Готово к ревью» на сорванном прогоне дороже молчания: человек пойдёт
    смотреть код как кандидата на слияние.
    """
    _no_worktree_prep(monkeypatch, tmp_path)
    posted: list = []
    monkeypatch.setattr(github, "publish_worktree",
                        lambda repo, clone, branch, **kw: 42)
    monkeypatch.setattr(github, "post_comment",
                        lambda repo, number, body: posted.append(body))

    await a.dev_publish_partial(_issue(), "feature/166", "причина")

    text = posted[0].lower()
    assert "сорва" in text
    assert "готово к ревью" not in text


async def test_nothing_written_means_nothing_said(monkeypatch, tmp_path, github):
    """Агент не тронул ни одного файла — сохранять нечего.

    Комментария тоже нет намеренно: сообщать человеку не о чем, а лишняя
    строка в ленте — шум.
    """
    _no_worktree_prep(monkeypatch, tmp_path)
    posted: list = []
    monkeypatch.setattr(github, "publish_worktree",
                        lambda repo, clone, branch, **kw: None)
    monkeypatch.setattr(github, "post_comment",
                        lambda repo, number, body: posted.append(body))

    assert await a.dev_publish_partial(_issue(), "feature/166", "причина") is None
    assert posted == []


async def test_a_failed_rescue_does_not_raise(monkeypatch, tmp_path, github):
    """Отказ выкладки не роняет то, о чём она отчитывается.

    Прогон УЖЕ сорвался. Если спасательный шаг упадёт, наружу уйдёт ЕГО
    ошибка — и первопричина исчезнет. Этот класс подмены в контуре уже
    случался, поэтому активность гасит свой отказ сама.
    """
    _no_worktree_prep(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("git push отказал")

    monkeypatch.setattr(github, "publish_worktree", boom)
    monkeypatch.setattr(github, "post_comment", lambda *args: None)

    assert await a.dev_publish_partial(_issue(), "feature/166", "причина") is None


async def test_a_failed_comment_does_not_lose_the_draft(monkeypatch, tmp_path, github):
    """Черновик открыт, комментарий не ушёл — номер всё равно возвращаем.

    Иначе воркфлоу решит, что спасать было нечего, и открытый черновик
    останется без упоминания вообще.
    """
    _no_worktree_prep(monkeypatch, tmp_path)
    monkeypatch.setattr(github, "publish_worktree",
                        lambda repo, clone, branch, **kw: 42)

    def boom(*args, **kwargs):
        raise RuntimeError("GitHub отказал")

    monkeypatch.setattr(github, "post_comment", boom)

    assert await a.dev_publish_partial(_issue(), "feature/166", "причина") == 42
