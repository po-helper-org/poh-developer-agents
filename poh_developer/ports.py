"""Порты: чем стадия разговаривает с харнессом.

Порт, а не прямой вызов клиента харнесса, — потому что стадия живёт в своём
репозитории и обязана собираться и тестироваться без него. Харнесс подставляет
реализации на старте воркера (`configure`), тесты — свои заглушки.

Отбор в порты шёл **по природе зависимости, а не по теме**. Ресурс харнесса —
порт: каталог промптов, клон с токеном, тело Issue, слой памяти, вызов модели.
Чистая механика портом не становится: «запусти в потоке и бей heartbeat» — не
ресурс, и порт ради порта только удлинил бы путь. Такие помощники переехали в
пакет копией и живут рядом с кодом, который их зовёт.

Второй эффект тот же, что в разборе адаптации под GitLab: провайдер трекера
меняется заменой реализации порта, а не правкой воркфлоу.
"""

from typing import Any, Protocol


class GitHubPort(Protocol):
    """Всё, что стадия делает с трекером и репозиторием кода.

    Токен живёт по эту сторону порта и в контейнер агента не попадает: агент
    исполняет чужой код, и радиус поражения токена рядом с ним — весь смысл
    его изоляции.
    """

    def get_issue(self, repo: str, number: int) -> dict: ...
    def get_issue_body(self, repo: str, number: int) -> str: ...
    def update_issue_body(self, repo: str, number: int, body: str) -> None: ...
    def post_comment(self, repo: str, number: int, body: str) -> None: ...
    def list_comments(self, repo: str, number: int) -> list[dict]: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...

    def get_pull(self, repo: str, number: int) -> dict: ...
    def review_text(self, repo: str, number: int) -> str: ...
    def changes_requested(self, repo: str, number: int) -> bool | None: ...
    def push_fixes(self, repo: str, clone_dir: str, branch: str, message: str) -> bool: ...

    def clone_repo(self, repo: str, dest: str, branch: str | None = None) -> None: ...
    def branch_exists(self, repo: str, branch: str) -> bool: ...
    def get_file(self, repo: str, path: str, ref: str) -> str | None: ...
    def blob_base(self, repo: str, ref: str) -> str: ...
    def push_artifacts_to_branch(self, repo: str, clone_dir: str, branch: str,
                                 paths: list[str], message: str) -> None: ...
    def publish_worktree(self, repo: str, clone_dir: str, branch: str, *,
                         title: str, body: str, message: str,
                         draft: bool = False,
                         ignore_for_empty_check: tuple = (),
                         force_include: tuple = ()) -> int | None: ...
    def dispatch_workflow(self, repo: str, workflow_file: str, ref: str,
                          inputs: dict) -> None: ...


class IssueBlocksPort(Protocol):
    """Именованные блоки в теле Issue.

    Стадия пишет сюда находки агента (секция GROW) и читает постановку. Запись
    отказывает `ValueError`, если содержимое похоже на маркер блока: находка
    приходит от модели и может дословно процитировать разметку.
    """

    GROW: str

    def read(self, body: str, block: str) -> str: ...
    def write(self, body: str, block: str, content: str) -> str: ...

    # Именованные участки, которые читает стадия. Отдельными методами, а не
    # `read(body, "HowToDemo")`: как называется раздел и чем он ограничен —
    # словарь харнесса, и знать его стадии незачем.
    def howtodemo_block(self, body: str) -> str: ...
    def open_questions(self, body: str) -> list[str]: ...


class RepowisePort(Protocol):
    """Постоянный индекс кода.

    Недоступность **деградирует стадию, а не конвейер**: агент, не достучавшийся
    до индекса, работает без него. Поэтому у порта есть `available()`, и его
    отрицательный ответ — штатный исход, а не отказ.
    """

    DEVELOP: str
    PROBE_TIMEOUT_SEC: int

    def enabled(self) -> bool: ...
    def available(self) -> bool: ...
    def session_id(self, repo: str, issue_number: int, stage: str) -> str: ...
    def openhands_mcp_config(self, session: str) -> dict: ...
    def transcript(self, session: str) -> str: ...


class MemoryPort(Protocol):
    """Слой саморефлексии: правила организации на вход, запись об итерации на выход.

    Как и Repowise, необязателен: не подключён — агенты работают без правил.
    """

    DEVELOP: str

    def rules(self, role: str) -> Any: ...
    def control_arm(self, issue_number: int) -> bool: ...


class LlmPort(Protocol):
    """Вызов модели там, где стадии нужен разбор текста, а не код."""

    MODEL_CLASSIFY: str

    def extract(self, prompt: str, schema: Any, *, model: str = "") -> Any: ...


class PromptsPort(Protocol):
    """Промпты харнесса.

    Отдельным портом, а не файлом в пакете: промпты правит тот, кто правит
    поведение контура, и они лежат в его репозитории рядом с остальными.
    """

    def load(self, name: str) -> str: ...


_ports: dict[str, Any] = {}

_NAMES = ("github", "issue_blocks", "repowise", "memory", "llm", "prompts")


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
def llm() -> LlmPort: return _get("llm")
def prompts() -> PromptsPort: return _get("prompts")


def reset() -> None:
    """Снять все реализации. Для тестов: подставленный в одном тесте порт,
    доживший до другого, даёт зелёный прогон на незаданной зависимости."""
    _ports.clear()
