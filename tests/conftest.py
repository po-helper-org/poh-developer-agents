"""Двойники портов: чем стадия разговаривает с контуром на прогоне тестов.

Стадия собирается и проверяется без контура — реализации портов на живом
прогоне подставляет он, а здесь эти двойники. Собираются они ИЗ САМИХ
протоколов (`ports.py`), а не перечислением руками: метод, добавленный в порт и
забытый здесь, иначе дал бы `AttributeError` в тесте вместо внятного отказа.

Метод, к которому тест не готовился, отказывает вслух. Молча вернув `None`, он
дал бы зелёный тест на пути, который на живом прогоне не работает, — ровно тот
класс отказа, который стадия ловит у себя («шаг доложил успех, а результата
нет»).
"""

import pytest

from poh_developer import ports


def _members(proto) -> tuple[set[str], dict]:
    """Методы и объявленные константы протокола."""
    methods = {n for n in dir(proto)
               if not n.startswith("_") and callable(getattr(proto, n, None))}
    consts = {n for n in getattr(proto, "__annotations__", {})}
    return methods, consts


class PortStub:
    """Двойник одного порта.

    Что тест задал — то и зовётся; остальное отказывает с именем метода. Имя в
    отказе обязательно: без него «не задали заглушку» неотличимо от «стадия
    зовёт не то».
    """

    def __init__(self, name: str, proto, **given):
        methods, consts = _members(proto)
        self._name = name
        for m in methods:
            self.__dict__.setdefault(m, self._refuser(m))
        for c in consts:
            self.__dict__.setdefault(c, f"<{name}.{c}>")
        self.__dict__.update(given)

    def _refuser(self, method: str):
        def refuse(*args, **kwargs):
            raise AssertionError(
                f"тест зовёт порт {self._name}.{method}, но заглушку не задал: "
                f"подставь её через monkeypatch.setattr(<порт>, {method!r}, ...)")
        return refuse


@pytest.fixture
def github() -> PortStub:
    return PortStub("github", ports.GitHubPort)


@pytest.fixture
def issue_blocks() -> PortStub:
    return PortStub("issue_blocks", ports.IssueBlocksPort, GROW="GROW")


@pytest.fixture
def repowise() -> PortStub:
    return PortStub("repowise", ports.RepowisePort,
                    DEVELOP="openhands", PROBE_TIMEOUT_SEC=5.0,
                    enabled=lambda: False)


@pytest.fixture
def memory() -> PortStub:
    class Rules:
        def __init__(self, text: str = "", ids=()):
            self.text, self.ids = text, list(ids)

    return PortStub("memory", ports.MemoryPort,
                    DEVELOP="develop", Rules=Rules,
                    enabled=lambda: False,
                    control_arm=lambda issue_number: False,
                    rules=lambda agent, repo="", query="": Rules())


@pytest.fixture
def telemetry() -> PortStub:
    return PortStub("telemetry", ports.TelemetryPort,
                    capture_followups_failure=lambda *a, **k: None)


@pytest.fixture(autouse=True)
def wired_ports(github, issue_blocks, repowise, memory, telemetry):
    """Подставить все порты на каждый тест и снять после.

    Autouse и со снятием: порт, подставленный в одном тесте и доживший до
    другого, даёт зелёный прогон на незаданной зависимости — отказ, который
    проявится на живом прогоне, а не здесь.

    Неотключаемые умолчания — «выключено»: индекс кода и слой памяти
    необязательны, и тест, который о них не знает, должен идти путём без них.
    """
    ports.reset()
    ports.configure(github=github, issue_blocks=issue_blocks, repowise=repowise,
                    memory=memory, telemetry=telemetry)
    yield
    ports.reset()
