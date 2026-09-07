"""Каталог контекста задачи: исполнитель читает файлы, а не пересказ.

До этого постановка упаковывалась в один файл с потолками 50000 знаков на всё
и 10000 на артефакт, лишние секции вытеснялись целиком. Докстринг того кода
признавал: «Переполнение достижимо штатно». Потеря контекста была не риском, а
заложенным поведением.
"""

from poh_developer import task_context


def test_map_lists_every_entry():
    text = task_context.render_map({
        task_context.PLAN: "план работ",
        task_context.REQUIREMENTS: "системные требования",
    })
    assert task_context.PLAN in text
    assert "план работ" in text
    assert task_context.REQUIREMENTS in text


def test_missing_names_absent_files(tmp_path):
    (tmp_path / task_context.PLAN).write_text("текст", encoding="utf-8")
    absent = task_context.missing(tmp_path, {
        task_context.PLAN: "план",
        task_context.REQUIREMENTS: "требования",
    })
    assert absent == [task_context.REQUIREMENTS]


def test_empty_file_counts_as_missing(tmp_path):
    """Пустой файл хуже отсутствующего: он выглядит доставленным."""
    (tmp_path / task_context.PLAN).write_text("   \n", encoding="utf-8")
    assert task_context.missing(tmp_path, {task_context.PLAN: "план"}) == [task_context.PLAN]


def test_truncation_marker_is_found(tmp_path):
    (tmp_path / task_context.PLAN).write_text("начало …[обрезано]", encoding="utf-8")
    assert task_context.truncation_markers(tmp_path) == [task_context.PLAN]


def test_clean_directory_has_no_markers(tmp_path):
    (tmp_path / task_context.PLAN).write_text("целый текст", encoding="utf-8")
    assert task_context.truncation_markers(tmp_path) == []


# Находка 1: BOM и невидимые символы
def test_bom_only_file_counts_as_missing(tmp_path):
    """Файл из одного BOM считается отсутствующим."""
    (tmp_path / task_context.PLAN).write_bytes(b'\xef\xbb\xbf')
    assert task_context.missing(tmp_path, {task_context.PLAN: "план"}) == [task_context.PLAN]


def test_zero_width_space_counts_as_missing(tmp_path):
    """Файл из одного zero-width space считается отсутствующим."""
    (tmp_path / task_context.PLAN).write_text("​", encoding="utf-8")
    assert task_context.missing(tmp_path, {task_context.PLAN: "план"}) == [task_context.PLAN]


def test_bom_plus_whitespace_counts_as_missing(tmp_path):
    """Файл с BOM и пробелами считается отсутствующим."""
    (tmp_path / task_context.PLAN).write_bytes(b'\xef\xbb\xbf   \n  ')
    assert task_context.missing(tmp_path, {task_context.PLAN: "план"}) == [task_context.PLAN]


# Находка 2: Нечитаемый файл
def test_unreadable_file_counts_as_missing(tmp_path):
    """Файл с неверной кодировкой не должен рушить проверку целиком."""
    # Пишем тестовый файл с невалидной UTF-8
    bad_file = tmp_path / task_context.REQUIREMENTS
    bad_file.write_bytes(b'\x80\x81\x82')

    good_file = tmp_path / task_context.PLAN
    good_file.write_text("целый текст", encoding="utf-8")

    # Должны получить оба файла в списке: плохо кодированный и отсутствующий
    absent = task_context.missing(tmp_path, {
        task_context.PLAN: "план",
        task_context.REQUIREMENTS: "требования",
        task_context.DECISIONS: "решения",
    })
    # REQUIREMENTS должен быть в списке как нечитаемый
    # DECISIONS отсутствует
    assert task_context.REQUIREMENTS in absent
    assert task_context.DECISIONS in absent
    assert task_context.PLAN not in absent
    assert len(absent) == 2


# Находка 3: Рекурсивный поиск маркеров
def test_truncation_marker_in_nested_file(tmp_path):
    """Маркер обрезки во вложенной папке должен быть найден."""
    nested = tmp_path / "subdir"
    nested.mkdir()
    (nested / "nested.md").write_text("текст …[обрезано]", encoding="utf-8")

    found = task_context.truncation_markers(tmp_path)
    # Должна найтись вложенная папка/файл
    assert any("nested.md" in str(p) for p in found) or "nested.md" in found


def test_truncation_marker_in_non_md_file(tmp_path):
    """Маркер обрезки в файле не-.md должен быть найден."""
    (tmp_path / "artifact.txt").write_text("текст …[обрезано]", encoding="utf-8")
    (tmp_path / "howtodemo.puml").write_text("diagram …[обрезано]", encoding="utf-8")

    found = task_context.truncation_markers(tmp_path)
    # Должны найтись оба файла (не только .md)
    assert len(found) >= 2


# ───────────────────── обязательный набор (задача 7) ─────────────────────
#
# missing() проверяет только то, что ей передали. Без канонического
# обязательного набора сборщик контекста мог бы строить `entries` по факту
# записанного — и молчаливый провал записи требований выглядел бы как
# «всё доставлено», потому что отсутствующий файл просто не был бы назван.

def test_requirements_are_mandatory_with_analysis_branch():
    assert task_context.required(has_analysis=True) == {
        task_context.REQUIREMENTS: "системные требования (ветка аналитики)"}


def test_nothing_is_mandatory_without_an_analysis_branch():
    """Аналитики нет — штатный путь «работай от тела Issue», а не провал."""
    assert task_context.required(has_analysis=False) == {}


def test_howtodemo_and_decisions_are_never_mandatory():
    """Сценарий может лежать в письме БФТ; решений прошлых шагов может не
    быть вовсе — оба случая штатные, а не отказ подготовки."""
    required = task_context.required(has_analysis=True)
    assert task_context.HOWTODEMO not in required
    assert task_context.DECISIONS not in required


def test_plan_is_never_mandatory_here():
    """План кладёт стадия декомпозиции — не сборщик контекста разработки."""
    assert task_context.PLAN not in task_context.required(has_analysis=True)


def test_required_set_catches_absence_even_when_nothing_was_recorded(tmp_path):
    """Регрессия сценария из докстринга `required()`: каталог пуст — будто
    вся сборка контекста провалилась молча, — а обязательный набор всё равно
    ловит отсутствие, а не сообщает «всё доставлено»."""
    absent = task_context.missing(tmp_path, task_context.required(has_analysis=True))
    assert absent == [task_context.REQUIREMENTS]


# ───────────────── L2 (ревью задачи 7): маркер усечения — одна константа ─────────────────
#
# Литерал маркера был продублирован в двух модулях (`task_context.py` и
# `_truncate` в `worker/activities.py`): разойдись они — проверка на след
# усечения тихо перестала бы находить то, что дописывает `_truncate`.

def test_truncation_marker_literal_appears_in_exactly_one_place():
    """Единственное разрешённое место буквального текста маркера — сама
    константа здесь; всё остальное в `worker/` обязано на неё ссылаться, а
    не заводить свою копию строки."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "worker").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if task_context.TRUNCATION_MARKER in line and "TRUNCATION_MARKER" not in line:
                offenders.append(f"{path.relative_to(root)}:{lineno}")
    assert offenders == [], f"литерал маркера продублирован вне константы: {offenders}"
