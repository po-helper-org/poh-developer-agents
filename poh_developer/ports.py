"""Порты: чем стадия разговаривает с контуром.

Порт, а не прямой вызов клиента контура, — потому что стадия живёт в своём
репозитории и обязана собираться и тестироваться без него. Контур подставляет
реализации на старте воркера (`configure`), тесты — свои заглушки.

Отбор в порты шёл **по природе зависимости, а не по теме**. Ресурс контура —
порт: клон с токеном, тело Issue, индекс кода, слой памяти, телеметрия. Чистая
механика портом не становится: «запусти в потоке и бей heartbeat» — не ресурс,
и порт ради него только удлинил бы путь. Такие помощники живут в пакете копией
рядом с кодом, который их зовёт (`activities.py`, шапка модуля).

Состав портов задан вызовами в `activities.py`, а не замыслом: каждый метод
здесь кто-то зовёт. Порт без вызывающего — это обещание контуру реализовать то,
чем никто не пользуется, и первым его сломает тот, кто поверит объявлению.

Второй эффект тот же, что в разборе адаптации под GitLab: провайдер трекера
меняется заменой реализации порта, а не правкой воркфлоу.
"""

from typing import Any, Protocol


class GitHubPort(Protocol):
    """Всё, что стадия делает с трекером и репозиторием кода.

    Токен живёт по ту сторону порта и в контейнер агента не попадает: агент
    исполняет чужой код, и радиус поражения токена рядом с ним — весь смысл
    его изоляции. Поэтому клон — один метод порта, а не тройка «дай адрес, дай
    имя пользователя, дай токен»: собирать из них команду `git` на этой стороне
    значило бы держать токен здесь.
    """

    def get_issue(self, repo: str, issue_number: int) -> dict: ...
    def get_issue_body(self, repo: str, issue_number: int) -> str: ...
    def update_issue_body(self, repo: str, issue_number: int, body: str) -> None: ...
    def post_comment(self, repo: str, issue_number: int, body: str) -> None: ...
    def add_label(self, repo: str, issue_number: int, label: str) -> None: ...

    def get_pull(self, repo: str, number: int) -> dict: ...
    def review_text(self, repo: str, number: int, limit: int = 12000) -> str: ...
    def changes_requested(self, repo: str, number: int) -> bool | None: ...
    def push_fixes(self, repo: str, clone_dir: str, branch: str, message: str) -> bool: ...

    def clone_repo(self, repo: str, dest: str, branch: str | None = None) -> None: ...
    def branch_exists(self, repo: str, branch: str) -> bool: ...
    def get_file(self, repo: str, path: str, ref: str) -> str | None: ...
    def push_artifacts_to_branch(self, repo: str, branch: str,
                                 files: dict[str, str], message: str) -> None: ...
    def publish_worktree(self, repo: str, clone_dir: str, branch: str, *,
                         title: str, body: str, message: str,
                         ignore_for_empty_check: tuple[str, ...] = (),
                         force_include: tuple[str, ...] = (),
                         draft: bool = False) -> int | None: ...
    def dispatch_workflow(self, repo: str, workflow_file: str, ref: str,
                          inputs: dict) -> None: ...


class IssueBlocksPort(Protocol):
    """Именованные участки тела Issue.

    Стадия дописывает сюда находки агента (секция GROW) и читает сценарий
    приёмки. Запись отказывает `ValueError`, если содержимое похоже на маркер
    блока: находка приходит от модели и может дословно процитировать разметку.
    """

    GROW: str

    def read(self, body: str, block: str) -> str: ...
    def write(self, body: str, block: str, content: str) -> str: ...

    # Отдельным методом, а не `read(body, "HowToDemo")`: сценарий приёмки живёт
    # либо в размеченном блоке, либо в разделе тела, и правило выбора между ними
    # — словарь контура. Знать его стадии незачем.
    def howtodemo_block(self, body: str) -> str: ...


class RepowisePort(Protocol):
    """Постоянный индекс кода.

    Недоступность **деградирует стадию, а не конвейер**: агент, не достучавшийся
    до индекса, работает без него. Поэтому у порта есть `available()`, и его
    отрицательный ответ — штатный исход, а не отказ.
    """

    DEVELOP: str
    PROBE_TIMEOUT_SEC: float

    def enabled(self) -> bool: ...
    def available(self, timeout: float = 5.0) -> bool: ...
    def session_id(self, repo: str, issue_number: int, agent: str) -> str: ...
    def openhands_mcp_config(self, repo: str, issue_number: int, agent: str) -> dict: ...
    def transcript(self, session: str) -> str | None: ...


class MemoryPort(Protocol):
    """Слой саморефлексии: правила организации на вход, запись об итерации на выход.

    Как и индекс кода, необязателен: не подключён — агент работает без правил.
    `Rules` объявлен здесь типом-значением, потому что стадия строит пустой
    экземпляр сама, когда задача попала в контрольную группу.
    """

    DEVELOP: str
    Rules: type

    def enabled(self) -> bool: ...
    def rules(self, agent: str, repo: str = "", query: str = "") -> Any: ...
    def control_arm(self, issue_number: int) -> bool: ...
    def put_episode(self, episode: dict) -> bool: ...


class TelemetryPort(Protocol):
    """Наблюдаемость контура.

    Существует ради одного отказа: находки агента не доехали до тела задачи.
    Запись находок best-effort и прогон не роняет, поэтому в логе она остаётся
    строкой `warning`, а `warning` в событие Sentry не превращается (порог
    `event_level=ERROR`). «Шаг доложил успех, а результата нет» — ровно тот
    класс отказа, который обязан быть виден, и видимым его делает этот вызов.
    """

    def capture_followups_failure(self, issue: Any, exc_type: str,
                                  message: str) -> str | None: ...


_ports: dict[str, Any] = {}

_NAMES = ("github", "issue_blocks", "repowise", "memory", "telemetry")


def configure(**impls: Any) -> None:
    """Подставить реализации портов. Зовётся один раз на старте воркера.

    Имя вне перечня — опечатка, и молча принятая она означала бы порт, который
    никогда не сработает: отказ проявится далеко от причины, на первом живом
    прогоне.
    """
    unknown = set(impls) - set(_NAMES)
    if unknown:
        raise ValueError(f"неизвестные порты: {', '.join(sorted(unknown))}; "
                         f"известные: {', '.join(_NAMES)}")
    _ports.update({k: v for k, v in impls.items() if v is not None})


def _get(name: str) -> Any:
    impl = _ports.get(name)
    if impl is None:
        raise RuntimeError(
            f"порт '{name}' не сконфигурирован: вызови "
            f"poh_developer.ports.configure({name}=...) на старте воркера")
    return impl


def github() -> GitHubPort: return _get("github")
def issue_blocks() -> IssueBlocksPort: return _get("issue_blocks")
def repowise() -> RepowisePort: return _get("repowise")
def memory() -> MemoryPort: return _get("memory")
def telemetry() -> TelemetryPort: return _get("telemetry")


def reset() -> None:
    """Снять все реализации. Для тестов: подставленный в одном тесте порт,
    доживший до другого, даёт зелёный прогон на незаданной зависимости."""
    _ports.clear()
