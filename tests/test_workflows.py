"""Воркфлоу стадии: то, что обязано пережить переезд.

Поведение шагов проверяют их собственные тесты. Здесь сторожится другое — то,
что ломается молча и обнаруживается на живом прогоне: имена, по которым
Temporal узнаёт воркфлоу и активности, и маркеры, без которых правка решения
роняет идущие прогоны недетерминизмом.
"""

import ast
import pathlib

from poh_developer import integration, workflows

SOURCE = pathlib.Path(workflows.__file__).read_text(encoding="utf-8")

# Маркеры, под которыми уже уехали правки решений. Список ЗАКРЫТЫЙ: новый
# маркер добавляется сюда тем же коммитом, что и ветка под ним, и это
# единственный способ заметить обратное — что ветку добавили без маркера.
MARKERS = {
    "issue-development-partial-publish",
    "issue-development-repair-loop",
    "issue-lifecycle-capture-episode-always",
    "issue-lifecycle-develop-plan-stage",
    "issue-lifecycle-empty-run-diagnosis",
}


def _patched_ids() -> set[str]:
    """Идентификаторы всех `workflow.patched(...)` модуля."""
    found = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in ("patched", "deprecate_patch"):
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
    return found


def test_the_markers_are_exactly_the_known_ones():
    """Маркер, потерянный при переезде, не роняет ни один обычный тест: они
    гоняют код с нуля, а расхождение возникает только на реплее записанной
    истории. Прогон при этом не падает заметно — он перестаёт выполнять задачи
    воркфлоу, а снаружи задача выглядит живой (2026-08-25: 29 прогонов из 149).
    """
    assert _patched_ids() == MARKERS


def test_both_workflows_are_registered():
    """Воркфлоу, не попавший в список, контур не зарегистрирует, и родитель
    получит отказ старта дочернего прогона на живой задаче."""
    assert {w.__name__ for w in integration.WORKFLOWS} == {"IssueDevelopment", "IssuePrFix"}


def test_workflow_names_survived_the_move():
    """Temporal сверяет воркфлоу по ИМЕНИ, и в историях идущих прогонов лежат
    прежние. Переименование здесь оборвало бы прогон, начатый до переезда."""
    names = {w.__temporal_workflow_definition.name for w in integration.WORKFLOWS}

    assert names == {"IssueDevelopment", "IssuePrFix"}


def test_the_plan_is_called_by_name_not_by_reference():
    """План работ живёт у контура. Ссылку на его функцию пакет взять не может —
    значит зовётся он строкой, и строка обязана совпадать с той, под которой
    контур его регистрирует."""
    assert integration.PLAN_ACTIVITY == "build_mvp_plan"
    assert f'"{integration.PLAN_ACTIVITY}"' in SOURCE


def test_the_stage_does_not_import_the_contour():
    """Обратной зависимости нет (R5): импорт из `poh-issue-agents` сделал бы
    пакет несобираемым в одиночку — и это выяснилось бы у того, кто его ставит."""
    contour = {"activities", "workflows", "github_client", "forge", "shared", "worker"}
    imported = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert imported & contour == set()
