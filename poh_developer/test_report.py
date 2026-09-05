"""Имена упавших тестов из отчёта JUnit XML.

Разбирается ОТЧЁТ, а не текст вывода. Вывод в консоль у каждого раннера свой,
и разбор stdout привязал бы контур к двум-трём знакомым; JUnit XML пишут Maven
и Gradle сами без флагов, pytest по `--junit-xml`, `node --test` по
`--test-reporter=junit`, PHPUnit по `--log-junit`.

Оговорка о сегодняшней пользе (спека B4): репозитории организации — Python и
Node, то есть ровно те два раннера, чей вывод пришлось бы разбирать иначе.
Формат выбран не ради мнимой немедленной общности, а чтобы новый стек не
требовал правки этого модуля.
"""
from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree

_log = logging.getLogger("test_report")

# Обычные места отчёта. Maven и Gradle кладут его сюда сами.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "junit*.xml",
    "**/junit*.xml",
    "target/surefire-reports/*.xml",
    "build/test-results/**/*.xml",
)


def find_reports(tree: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Файлы отчётов в дереве. Порядок не важен — читаются все."""
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in tree.glob(pattern):
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                seen.add(resolved)
                found.append(path)
    return found


def _key(case: ElementTree.Element, tree: Path) -> str:
    """Имя теста, одинаковое в любом каталоге прогона.

    `file` предпочтительнее `classname`: node --test ставит всем тестам
    `classname="test"`, и различить их по нему нельзя. Но `file` он пишет
    АБСОЛЮТНЫМ, а базовая линия гоняется в другом дереве — без приведения к
    относительному пути ни один ключ не совпал бы, и каждое падение выглядело
    бы своим.
    """
    scope = case.get("file") or case.get("classname") or ""
    if scope:
        try:
            scope = str(Path(scope).resolve().relative_to(tree.resolve()))
        except ValueError:
            # Не внутри дерева — оставляем как есть: хуже, чем относительный
            # путь, но лучше, чем потерянный тест.
            pass
    return f"{scope}::{case.get('name') or ''}"


def failed_tests(tree: Path, patterns: tuple[str, ...]) -> set[str] | None:
    """Множество имён упавших тестов. `None` — исход НЕ разобран.

    `None` и пустое множество — разные вещи, и путать их нельзя: пустое
    означает «упавших нет», то есть основание открыть PR, а `None` — что о
    прогоне не известно ничего и решать по нему нельзя.
    """
    reports = find_reports(tree, patterns)
    if not reports:
        return None
    failed: set[str] = set()
    total = 0
    parsed_any = False
    for path in reports:
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError) as exc:
            _log.warning("отчёт %s не разобран: %s", path, exc)
            continue
        parsed_any = True
        for case in root.iter("testcase"):
            total += 1
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(_key(case, tree))
    if not parsed_any or total == 0:
        # Ноль тестов — раннер не добрался до них (не поставились зависимости,
        # не нашёлся каталог). Считать это зелёным прогоном нельзя.
        return None
    return failed
