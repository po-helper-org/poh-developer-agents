"""Типы, едущие через границу репозиториев.

Форма этих dataclass'ов — не деталь, а формат сообщения: Temporal кладёт их в
историю прогона и восстанавливает при реплее. `IssueDevelopment` durable, и в
историях идущих прогонов лежит именно та форма, что была на старте.
"""

import dataclasses

from poh_developer.workflow_types import Diagnosis, DevelopPlan, IssueInput


def test_issue_input_keeps_its_wire_shape():
    """Поля и их порядок — контракт с историями идущих прогонов.

    Правка этого списка не «переименование поля», а смена формата сообщения:
    Temporal при несовпадении не падает, а отдаёт словарь вместо объекта, и
    отказ проявится далеко от причины.
    """
    fields = [(f.name, f.type) for f in dataclasses.fields(IssueInput)]

    assert fields == [
        ("repo", str), ("issue_number", int), ("title", str),
        ("body", str), ("author_login", str), ("author_type", str),
        ("interactive", bool),
    ]


def test_only_interactive_has_a_default():
    """Умолчание у остальных полей означало бы, что неполный вход проезжает
    молча — вместо отказа на границе."""
    defaulted = [f.name for f in dataclasses.fields(IssueInput)
                 if f.default is not dataclasses.MISSING]

    assert defaulted == ["interactive"]


def test_develop_plan_repairs_once_by_default():
    """Второй заход починки удваивает худший случай прогона: агент идёт до 45
    минут. Поднимать стоит, когда наберётся статистика, сколько починок
    удаётся, а не заранее."""
    plan = DevelopPlan(mode="local", branch="")

    assert plan.repair_rounds == 1


def test_diagnosis_unparsed_carries_no_verdict():
    """`parsed=False` — исход НЕ разобран, и решать по own/foreign нельзя:
    контур обязан вести себя как прежде, а не как при пустых списках."""
    d = Diagnosis(parsed=False, baseline=[], own=[], foreign=[])

    assert d.parsed is False
    assert (d.own, d.foreign) == ([], [])
