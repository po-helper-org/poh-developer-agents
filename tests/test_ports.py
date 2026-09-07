"""Порты: то, ради чего они заведены, и то, чем они опасны.

Протокол сам по себе ничего не проверяет — он подсказка типизатору. Здесь
проверяется поведение вокруг него: что незаданный порт называет себя вслух,
что опечатка в имени видна на вызове, и что подставленное в одном тесте не
доживает до другого.
"""

import pytest

from poh_developer import integration, ports


@pytest.fixture(autouse=True)
def _clean_ports():
    """Порт, подставленный в одном тесте и доживший до другого, даёт зелёный
    прогон на незаданной зависимости — отказ, который проявится на живом
    прогоне, а не здесь."""
    ports.reset()
    yield
    ports.reset()


def test_missing_port_names_itself_and_the_fix():
    """Отказ обязан сказать, ЧТО не сконфигурировано и ЧЕМ это чинится.

    Без этого он приходит как AttributeError на None далеко от причины — на
    первом живом прогоне, посреди чужого стека.
    """
    with pytest.raises(RuntimeError) as e:
        ports.github()

    assert "github" in str(e.value)
    assert "configure" in str(e.value)


def test_unknown_port_is_refused_at_the_call():
    """Опечатка в имени порта, принятая молча, означала бы порт, который
    никогда не сработает."""
    with pytest.raises(ValueError) as e:
        ports.configure(gihtub=object())

    assert "gihtub" in str(e.value)
    assert "github" in str(e.value)      # перечень известных — в том же тексте


def test_configured_port_is_returned():
    stub = object()
    ports.configure(github=stub)

    assert ports.github() is stub


def test_configure_is_additive():
    """Второй вызов не сбрасывает то, что подставил первый: харнесс вправе
    конфигурировать порты по частям."""
    a, b = object(), object()
    ports.configure(github=a)
    ports.configure(memory=b)

    assert ports.github() is a
    assert ports.memory() is b


def test_none_does_not_erase_a_configured_port():
    """`install()` передаёт все пять имён, и незаданные приходят как None.
    Затирать ими уже подставленное значило бы, что порядок вызовов решает."""
    stub = object()
    ports.configure(github=stub)
    ports.configure(github=None, memory=object())

    assert ports.github() is stub


def test_install_passes_every_port_by_name():
    """Мост харнесса зовёт install() именованными аргументами — опечатка видна
    на вызове, а не отказом на прогоне."""
    impls = {n: object() for n in
             ("github", "issue_blocks", "repowise", "memory", "telemetry")}
    integration.install(**impls)

    assert ports.github() is impls["github"]
    assert ports.issue_blocks() is impls["issue_blocks"]
    assert ports.repowise() is impls["repowise"]
    assert ports.memory() is impls["memory"]
    assert ports.telemetry() is impls["telemetry"]


def test_queue_is_its_own():
    """Стадия работает на своей очереди: харнесс уже держит три в одном
    процессе, и четвёртая продолжает практику, а не заводит исключение."""
    assert integration.TASK_QUEUE == "developer"
    assert integration.TASK_QUEUE not in ("issue-lifecycle", "delivery", "howtodemo")


def test_every_activity_is_registered():
    """Точка входа, потерянная при переезде, проявилась бы зависшим воркфлоу на
    живом прогоне, а не пустым местом в списке.

    Сверка со ВСЕМИ `@activity.defn` модуля, а не с числом: список собран
    руками, и забытая строка — ровно тот способ его сломать.
    """
    from poh_developer import activities

    defined = {getattr(activities, n) for n in dir(activities)
               if callable(getattr(activities, n, None))
               and getattr(getattr(activities, n), "__temporal_activity_definition", None)}

    assert set(integration.ACTIVITIES) == defined
    assert len(integration.ACTIVITIES) == len(defined)   # без повторов


def test_activity_names_are_unchanged_by_the_move():
    """Temporal сверяет активность по ИМЕНИ, и история идущего прогона знает
    прежние. Переименование здесь оборвало бы прогон, начатый до переезда."""
    from temporalio import activity

    names = {activity._Definition.must_from_callable(f).name
             for f in integration.ACTIVITIES}

    assert "dev_prepare" in names
    assert "run_pr_fix_round" in names
    assert "trigger_openhands_resolver" in names
    assert all(not n.startswith("poh_developer") for n in names)


def test_workflows_are_not_here_yet():
    """Воркфлоу приезжают следующим заходом. Тест сторожит не пустоту, а то,
    что её заметят: заполнив список, придётся вернуться сюда."""
    assert integration.WORKFLOWS == []
