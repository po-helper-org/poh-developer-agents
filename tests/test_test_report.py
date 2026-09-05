"""Разбор JUnit XML: имена упавших тестов, а не код возврата.

Кода возврата мало: он не отличает размен (агент починил один тест и сломал
другой) от чистой работы.
"""

from pathlib import Path

from poh_developer import test_report

_PYTEST = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_pricing" name="test_green" time="0.1"/>
<testcase classname="tests.test_pricing" name="test_red" time="0.1">
<failure message="assert 0 == 600">boom</failure></testcase>
<testcase classname="tests.test_cart" name="test_broken" time="0.1">
<error message="ImportError">boom</error></testcase>
</testsuite></testsuites>
"""

# node --test пишет `file` АБСОЛЮТНЫМ путём и `classname="test"` для всех
# тестов сразу — по classname их не различить, а абсолютный путь в базовой
# линии другой (она гоняется в отдельном дереве).
_NODE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testcase name="промо даёт скидку" classname="test"
  file="{tree}/tests/pricing.test.mjs">
<failure type="testCodeFailure" message="0 !== 600">boom</failure></testcase>
<testcase name="здоровье отвечает" classname="test"
  file="{tree}/tests/healthz.test.mjs"/>
</testsuites>
"""


def test_reads_failures_and_errors_from_pytest(tmp_path):
    """`<error>` — тоже падение: тест не прошёл, причина иная."""
    (tmp_path / "junit.xml").write_text(_PYTEST, encoding="utf-8")

    failed = test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS)

    assert failed == {"tests.test_pricing::test_red", "tests.test_cart::test_broken"}


def test_node_paths_are_relative_to_the_tree(tmp_path):
    """Ключ не должен зависеть от того, в каком каталоге гнали тесты.

    Базовая линия снимается в ОТДЕЛЬНОМ дереве. Оставь путь абсолютным — и ни
    один ключ базовой линии не совпадёт с итоговым, а каждое падение будет
    выглядеть своим. Механизм различения перестал бы работать целиком.
    """
    (tmp_path / "junit.xml").write_text(_NODE.format(tree=tmp_path), encoding="utf-8")

    failed = test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS)

    assert failed == {"tests/pricing.test.mjs::промо даёт скидку"}


def test_a_file_attribute_wins_over_a_useless_classname(tmp_path):
    """`classname="test"` у node одинаков для всех — различать нечем."""
    (tmp_path / "junit.xml").write_text(_NODE.format(tree=tmp_path), encoding="utf-8")

    failed = test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS)

    assert not any(key.startswith("test::") for key in failed)


def test_no_report_is_unparsed_not_green(tmp_path):
    """Отсутствие отчёта — НЕ «упавших нет».

    Пустое множество значило бы «своих поломок нет» и открыло бы PR по
    прогону, о котором ничего не известно.
    """
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) is None


def test_broken_xml_is_unparsed(tmp_path):
    (tmp_path / "junit.xml").write_text("<testsuites><oops", encoding="utf-8")
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) is None


def test_zero_tests_is_unparsed(tmp_path):
    """Отчёт без единого теста — раннер не добрался до тестов."""
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0"?><testsuites></testsuites>', encoding="utf-8")
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) is None


def test_green_run_is_an_empty_set_not_none(tmp_path):
    """Зелёный прогон — разобранный исход с пустым множеством."""
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0"?><testsuites><testcase classname="a" name="b"/>'
        '</testsuites>', encoding="utf-8")
    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) == set()


def test_finds_maven_and_gradle_reports_without_configuration(tmp_path):
    """Maven и Gradle пишут отчёт сами — настраивать нечего."""
    for rel in ("target/surefire-reports/TEST-a.xml",
                "build/test-results/test/TEST-b.xml"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<?xml version="1.0"?><testsuites>'
                        '<testcase classname="c" name="d"><failure/></testcase>'
                        '</testsuites>', encoding="utf-8")

    assert test_report.failed_tests(tmp_path, test_report.DEFAULT_PATTERNS) == {
        "c::d"}
