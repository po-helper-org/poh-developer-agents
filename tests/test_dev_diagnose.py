"""Своя поломка против чужой и мигающей.

Отказ, ради которого написано: poh-demo-checkout#166 и #167 — оба прогона
кончились человеком, и ни в одном агент ничего не ломал. `main` красный с
1 сентября из-за истёкшего промокода, а `dev_tests` знает только код возврата.
"""

from poh_developer import activities as a
from poh_developer.workflow_types import IssueInput


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
