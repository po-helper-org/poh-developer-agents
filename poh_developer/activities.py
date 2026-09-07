"""Шаги стадии «Разработка»: от постановки задачи агенту до опубликованного PR.

Порядок шагов и решения между ними держит воркфлоу `IssueDevelopment`; здесь
только сами шаги. Каждый `@activity.defn` — точка, за которой Temporal
запоминает результат: повтор воркфлоу возьмёт его из истории, а не выполнит
заново.

Всё, что стадии нужно от контура, приходит через порты (`ports.py`): трекер и
репозиторий кода, блоки в теле Issue, индекс кода, слой памяти, телеметрия.
Обратной зависимости нет — пакет не импортирует из `poh-issue-agents` ничего.

Две вещи живут здесь копией, а не портом, и это решение, а не недосмотр:

* `_run_with_heartbeat` — чистая механика «запусти в потоке и бей heartbeat».
  Ресурсом контура она не является, и порт ради неё только удлинил бы путь.
  Вторая копия обслуживает стадии анализа в `poh-issue-agents/worker/activities.py`.
* `FNR_DIR` — куда стадия анализа кладёт свои артефакты. Это общий словарь двух
  стадий, и вторая его половина объявлена там же.

Расхождение копий безопасно: обе читаются людьми, а поведение стадии проверяют
её собственные тесты. Проводной контракт с воркфлоу — другое дело, он объявлен
ровно один раз (`workflow_types.py`).
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from temporalio import activity

from . import develop, ports, pr_closing, task_context, test_report
from .workflow_types import DevelopPlan, Diagnosis, IssueInput

logger = logging.getLogger(__name__)


# Куда стадия анализа кладёт артефакты, из которых разработка собирает
# постановку. Вторая половина этого словаря — в `poh-issue-agents`
# (`worker/activities.py`, там же `_collect_fnr_artifacts`).
FNR_DIR = "sa_documentation/FNR/FNR_1"


HEARTBEAT_INTERVAL_SEC = 30.0


def _swallow_after_cancel(task: "asyncio.Task") -> None:
    """Забрать исключение задачи, брошенной отменой, и сказать это в лог.

    Молча глотать нельзя: поток мог упасть по настоящей причине, и она —
    единственный след того, чем закончилась оборванная стадия.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.info("стадия оборвана отменой, поток завершился ошибкой: %s", exc)


async def _run_with_heartbeat(fn, *args, label: str):
    """Гоняет блокирующий fn в потоке и шлёт heartbeat каждые
    HEARTBEAT_INTERVAL_SEC, пока он не завершится.

    Heartbeat только между шагами недостаточен: прогон агента идёт до
    `develop.run_timeout()` (сорок пять минут), а heartbeat_timeout воркфлоу —
    минуты; без периодического сигнала изнутри шага сервер счёл бы activity
    мёртвой и (при maximum_attempts=1) уронил бы весь прогон. `to_thread`
    освобождает event loop, но сам по себе не бьёт — поэтому бьём здесь, пока
    поток занят.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_SEC)
            if task in done:
                return task.result()  # переброс исключения из потока, если было
            activity.heartbeat(label)
    except asyncio.CancelledError:
        # Отмена активности (terminate воркфлоу, таймаут) обрывает ожидание, но
        # НЕ поток: `to_thread` не прерывается, docker-прогон доигрывает и
        # кладёт исключение в задачу, которую уже никто не ждёт. asyncio на
        # сборке такой задачи пишет «Task exception was never retrieved»
        # уровнем ERROR — и это уезжает в Sentry как сбой контура
        # (ISSUE-AGENT-C: код 137 у контейнера агента, снятого намеренно).
        # Колбэк забирает исключение, поэтому предупреждения не будет.
        task.add_done_callback(_swallow_after_cancel)
        raise


def _refresh_issue_body(issue: IssueInput) -> str:
    """Перечитывает тело Issue из GitHub вместо устаревшего снимка.
    
    Снимок в `IssueInput` создается вебхуком один раз и устаревает для
    долгоживущих задач в `ready-for-dev`.
    """
    try:
        fresh = ports.github().get_issue(issue.repo, issue.issue_number)
        return fresh.get("body") or ""
    except Exception as exc:  # noqa: BLE001 — деградация к старому снимку
        logger.warning("не удалось обновить тело #%s, используется снимок: %s",
                       issue.issue_number, exc)
        return issue.body


def _split_sections(parts: list[str]) -> list[tuple[str, str]]:
    """Разложить куски постановки на секции `(заголовок, содержимое)`.

    Заголовком считается ПЕРВАЯ СТРОКА куска, начинающегося с решётки, а не
    весь кусок. Раньше весь кусок целиком становился именем секции, и его
    содержимое исчезало: так терялся весь блок правил репозитория
    (`_DEV_FALLBACK_RULES`) вместе с инструкцией писать находки в
    `.followups.md`, которую `collect_dev_followups` затем честно читал.
    Уцелевал ровно один блок — тот, что начинается с перевода строки.
    """
    sections: list[tuple[str, str]] = []
    name = ""
    body: list[str] = []

    def flush() -> None:
        if name or body:
            sections.append((name, "\n".join(body).strip("\n")))

    for part in parts:
        if part.startswith("#"):
            flush()
            head, _, rest = part.partition("\n")
            name, body = head.strip(), ([rest] if rest.strip() else [])
        else:
            body.append(part)
    flush()
    return sections


ORG_RULES_HEADING = "## Накопленный опыт этой организации"


def _join_sections(sections: list[tuple[str, str]]) -> str:
    """Собрать постановку обратно, СОХРАНЯЯ заголовки секций.

    Заголовки нужны агенту: без них постановка превращается в поток текста, где
    неотличимы тело задачи, артефакты аналитики и правила работы.
    """
    out: list[str] = []
    for name, content in sections:
        block = "\n".join(x for x in (name, content) if x)
        if block.strip():
            out.append(block)
    return "\n\n".join(out)


DEV_TESTS_TIMEOUT_SEC = 900


def _runner_home(slug: str) -> str:
    """Домашний каталог раннера — каталог задачи, но только при живой интеграции.

    Агент ищет конфигурацию MCP в `$HOME/.openhands/mcp.json` (спайк FR-16), а
    общий том смонтирован в другом месте. Переставить HOME дешевле, чем
    оборачивать ENTRYPOINT образа.

    Интеграция выключена — возвращаем пусто, и HOME остаётся тем, что задан
    образом: поведение прогонов без Repowise не меняется вовсе.
    """
    if not ports.repowise().enabled():
        return ""
    return f"{develop.workspace_mount()}/{slug}"


def _write_runner_mcp_config(issue: IssueInput, root: Path) -> None:
    """Конфигурация MCP в каталог задачи, откуда её прочитает раннер.

    Каталог лежит на общем томе и виден обоим контейнерам; права выставляются
    вместе с остальным содержимым каталога задачи — раннер работает от
    непривилегированного пользователя и в чужой каталог писать не сможет.
    """
    if not ports.repowise().enabled():
        return
    if not ports.repowise().available(timeout=ports.repowise().PROBE_TIMEOUT_SEC):
        # НАСТРОЕННЫЙ, но недоступный прокси — не то же самое, что выключенная
        # интеграция, и раньше эти случаи не различались: конфиг писался по
        # флагу, агент шёл поднимать по нему MCP и умирал на инициализации, не
        # сделав ни одного хода. Контур при этом докладывал «агент не изменил
        # ни одного файла» — то есть обвинял агента в отказе инфраструктуры.
        # Живой случай: poh-demo-checkout#151, 2026-08-25.
        #
        # Работа без индекса — штатный режим, он же обещан документацией.
        logger.warning(
            "Develop %s#%s: Repowise настроен, но прокси не отвечает — "
            "конфигурацию MCP не пишу, агент пойдёт без индекса",
            issue.repo, issue.issue_number)
        return
    config_dir = root / develop.MCP_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / develop.MCP_CONFIG_NAME).write_text(
        json.dumps(ports.repowise().openhands_mcp_config(
            issue.repo, issue.issue_number, ports.repowise().DEVELOP),
            ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _dev_paths(issue: IssueInput) -> tuple[Path, Path]:
    """Каталог задачи в общем томе и клон внутри него.

    Том общий с раннером и смонтирован в обоих контейнерах по одному пути:
    воркер готовит каталог и читает результат, раннер пишет. Через bind-mount
    так не сделать — путь внутри воркера на хосте не существует.
    """
    root = Path(develop.workspace_mount()) / develop.task_slug(issue.repo, issue.issue_number)
    return root, root / "repo"


# M4 (ревью задачи 7): артефакты цепочки FNR сверх требований. Прежняя
# сборка (до задачи 7) тянула все пять в постановку; задача 7 оставила
# только требования — concept.md, task.md, repowise-dialog.md и
# validation.md выпали без упоминания. Возвращаются файлами в каталог: он
# для того и заведён, чтобы объём перестал быть поводом выбрасывать
# контекст. НЕ обязательны — `task_context.required()` их не требует.
_OPTIONAL_ANALYSIS_ARTIFACTS = (
    (task_context.TASK, "постановка, с которой начинался анализ FNR"),
    (task_context.REPOWISE_DIALOG, "диалог с индексом кода на старте анализа"),
    (task_context.VALIDATION, "проверка требований на этапе анализа"),
)


def _fetch_optional_artifact(repo: str, name: str, branch: str) -> str:
    """Один необязательный артефакт цепочки FNR с ветки аналитики (M4).

    Деградация, а не отказ, при ЛЮБОМ сбое (не только 404 — `get_file`
    бросает исключение на прочих кодах ответа): эти артефакты никогда не
    входят в `task_context.required()`, и сетевой сбой на одном из них не
    должен ронять всю подготовку контекста, которая уже прошла обязательные
    требования.
    """
    try:
        return ports.github().get_file(repo, f"{FNR_DIR}/{name}", branch) or ""
    except Exception as exc:  # noqa: BLE001 — деградация, а не отказ
        logger.warning("не удалось прочитать %s с ветки %s: %s", name, branch, exc)
        return ""


def _dev_prepare(issue: IssueInput, branch: str) -> tuple[str, list[str]]:
    """Свежий клон + контекст каталогом `.harness/` + короткая постановка.

    Возвращает текст постановки и перечень идентификаторов правил, подсыпанных
    слоем саморефлексии. Перечень нужен записи об итерации: без него нельзя
    отличить «правило сработало» от «правило не читали», и счётчики
    подтверждения на стороне слоя теряют смысл.

    Постановка собирается ЗДЕСЬ, а не в промпте агента: то, что уехало в
    работу, должно быть видно дословно. Иначе на разборе «почему агент сделал
    не то» предъявить нечего. Но сама постановка (`.task.md`, снимается перед
    коммитом) больше НЕ несёт содержательный контекст — до задачи 7 она
    паковала требования, артефакты аналитики, обсуждение и план декомпозиции
    в один файл с потолком 50000 знаков на всё и 10000 на артефакт; докстринг
    того кода признавал «переполнение достижимо штатно». Контекст теперь
    лежит файлами каталога `.harness/` (`task_context.py`) — он
    коммитится вместе с кодом, виден в PR и читается через полгода, а
    исполнитель открывает файлы из git по мере надобности, без потолка на
    объём и без усечения.

    Обязательный набор файлов контекста объявлен в `task_context.required()` и
    проверяется здесь ЖЁСТКО: отсутствие обязательного файла — отказ стадии
    (`RuntimeError`), а не предупреждение в лог. Молчаливая неполная сборка
    хуже упавшей стадии — исполнитель работал бы вслепую, не зная, что
    контекста не хватает.
    """
    root, clone_dir = _dev_paths(issue)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    ports.github().clone_repo(issue.repo, str(clone_dir))
    _write_runner_mcp_config(issue, root)

    # Свежее тело Issue вместо устаревшего снимка вебхука.
    fresh_body = _refresh_issue_body(issue)

    parts = [f"# Задача: реализовать Issue #{issue.issue_number}",
             "", f"## {issue.title}", "", fresh_body or "(тело пустое)", ""]

    # --- Контекст каталогом: исполнитель читает файлы из git, не пересказ ---
    harness = clone_dir / task_context.DIR
    # H1 (ревью задачи 7): `.harness/` коммитится в main, поэтому клон того же
    # репозитория приезжает с каталогом ПРЕДЫДУЩЕЙ задачи, если она мержилась
    # раньше. `mkdir(exist_ok=True)` без очистки оставлял унаследованные файлы
    # на месте: молчаливый провал сборки ЭТОЙ задачи (токен истёк, артефакт
    # переименован, sysreq не дописан) не создавал отказа — проверка
    # обязательного набора видела чужой файл прошлой задачи и засчитывала его,
    # а исполнитель получал требования и сценарий чужого Issue. Каталог
    # обязан собираться с нуля на каждом прогоне, а не дополняться.
    shutil.rmtree(harness, ignore_errors=True)
    harness.mkdir(parents=True, exist_ok=True)
    entries: dict[str, str] = {}

    requirements = ""
    concept = ""
    if branch:
        requirements = ports.github().get_file(
            issue.repo, f"{FNR_DIR}/system_requirements.md", branch) or ""
        if requirements:
            (harness / task_context.REQUIREMENTS).write_text(
                requirements, encoding="utf-8")
            entries[task_context.REQUIREMENTS] = "системные требования (ветка аналитики)"
        # N3 (повторное ревью): concept.md читается один раз и используется
        # ОДИН раз — только как источник DECISIONS (M2, ниже). До этой правки
        # то же содержимое дополнительно писалось СВОИМ именем (M4) — байт в
        # байт та же строка дважды под разными подписями в карте контекста,
        # будто это разные срезы; исполнитель тратил бюджет чтения дважды на
        # один и тот же текст.
        concept = _fetch_optional_artifact(issue.repo, task_context.CONCEPT, branch)

    scenario = ports.issue_blocks().howtodemo_block(fresh_body or "")
    if scenario:
        (harness / task_context.HOWTODEMO).write_text(scenario, encoding="utf-8")
        entries[task_context.HOWTODEMO] = "сценарий приёмки: им проверяется результат"

    # M2 (ревью задачи 7): DECISIONS больше НЕ копия `.reflect.md`. Постановка
    # обещает агенту дословно «файл в коммит не попадёт, его снимает контур»
    # — под это обещание агент пишет намерение, допущения и СОМНЕНИЯ, а копия
    # в decisions.md коммитилась и уезжала в PR: обещание нарушалось кодом
    # же, который его давал. Источник — concept.md ветки аналитики: там
    # вердикт дебатов, то есть принятые решения и их причины, а не намерения
    # агента. Заодно (L1) это делает DECISIONS ретрай-устойчивым: файл
    # приходит через git на каждом вызове независимо от локального состояния
    # каталога задачи, а не из файла, который снесла же предыдущая попытка.
    if concept:
        (harness / task_context.DECISIONS).write_text(concept, encoding="utf-8")
        entries[task_context.DECISIONS] = (
            "источник — вердикт дебатов цепочки аналитики: принятые решения "
            "и их причины"
        )

    # M4: остальные артефакты цепочки FNR — опциональные, читаются, только
    # если ветка аналитики есть (иначе им взяться неоткуда).
    #
    # N3 (повторное ревью): concept.md сюда НЕ входит, хотя формально он тоже
    # артефакт цепочки FNR. `DECISIONS` выше — тот же файл, объявленный в
    # `task_context.py` и несущий роль «принятые решения и их причины»
    # (M2); отдельная копия под именем `task_context.CONCEPT` дублировала бы
    # его содержимое байт в байт под другой подписью в карте контекста, а не
    # добавляла новый срез. `task_context.CONCEPT` остаётся именем артефакта
    # для чтения с ветки аналитики (см. `_fetch_optional_artifact` выше) — он
    # просто не становится ВТОРЫМ файлом каталога.
    if branch:
        for name, description in _OPTIONAL_ANALYSIS_ARTIFACTS:
            content = _fetch_optional_artifact(issue.repo, name, branch)
            if content:
                (harness / name).write_text(content, encoding="utf-8")
                entries[name] = description

    (harness / task_context.CONTEXT_MAP).write_text(
        task_context.render_map(entries), encoding="utf-8")

    # Обязательный набор — ОТДЕЛЬНО от `entries`: перечень выше называет
    # только то, что реально записалось, и на молчаливом провале записи
    # (сеть недоступна, токен истёк) был бы просто короче — а `missing()` на
    # НЁМ ЖЕ ответила бы «всё доставлено». Проверяем против объявленного вовне
    # обязательного набора, который от факта записи не зависит.
    #
    # `branch` есть, а требования дописать не удалось — сообщение называет
    # ИМЕННО ЭТО (L6, ревью задачи 7): ветка аналитики нередко существует
    # раньше, чем в неё попадает `system_requirements.md` — цепочка FNR ещё не
    # дошла до стадии `sysreq`, либо оборвалась раньше (`publish_analysis_partial`
    # пушит частичный результат). Ручной `/develop` в этом окне — не редкость,
    # и сообщение обязано сказать человеку, что делать, а не только что сломано.
    absent = task_context.missing(harness, task_context.required(has_analysis=bool(branch)))
    if absent:
        raise RuntimeError(
            f"Develop {issue.repo}#{issue.issue_number}: контекст не собран — "
            f"в {task_context.DIR}/ нет обязательных файлов: {', '.join(absent)}. "
            "Отказ стадии вместо слепого прогона исполнителя. Если ветка "
            f"аналитики `{branch}` создана недавно — вероятно, цепочка FNR ещё "
            "не дошла до стадии `sysreq` (или оборвалась раньше неё): дождись "
            "её завершения или перезапусти `/analyze` и повтори `/develop`."
        )

    # H1 (ревью задачи 7), вторая половина чинки: пустая карта контекста при
    # ЖИВОЙ ветке аналитики — самостоятельный повод для отказа, а не только
    # следствие проверки выше. Собранный контекст не может не содержать
    # НИЧЕГО: если ветка аналитики есть, а `entries` пуст (ни требований, ни
    # сценария, ни решений), это неотличимо от молчаливого провала сборки.
    # Сегодня проверка `required()`/`missing()` выше уже ловит этот случай
    # (REQUIREMENTS обязателен при `has_analysis`) и потому оказывается первой
    # — её сообщение конкретнее (называет отсутствующий файл). Эта проверка —
    # СТРАХОВКА: полагаться ИСКЛЮЧИТЕЛЬНО на `required()` значит терять
    # защиту, если его определение изменится независимо от этой строки;
    # `test_empty_context_fails_even_if_required_set_is_patched_to_demand_nothing`
    # подтверждает мутацией, что она действительно ловит сама, а не только
    # вслед за `required()`.
    #
    # Без ветки (`branch` пуст) `entries` пустым бывает штатно: «аналитики нет
    # — работай от тела Issue» — не отказ (см. `task_context.required()`,
    # `test_no_analysis_branch_does_not_require_a_requirements_file`), и здесь
    # эта строка это уважает — проверка условна на `branch`.
    if branch and not entries:
        raise RuntimeError(
            f"Develop {issue.repo}#{issue.issue_number}: контекст не собран — "
            f"{task_context.DIR}/ пуст, хотя ветка аналитики {branch} есть. "
            "Отказ стадии вместо слепого прогона исполнителя."
        )

    # L2 (ревью задачи 7): сообщение обязано подсказывать выход, а не только
    # называть находку. В штатной работе этот путь недостижим — единственный
    # код, что дописывал маркер (`_apply_size_limit`), задача 7 удалила
    # целиком, — проверка защищает от УНАСЛЕДОВАННОГО маркера, если он всё же
    # попал в исходный текст (например, дословно процитирован в требованиях).
    # Без подсказки такой отказ повторялся бы на каждой разработке по этой
    # задаче: файл на ветке аналитики не правит сам себя.
    corrupted = task_context.truncation_markers(harness)
    if corrupted:
        source = f"ветке аналитики `{branch}`" if branch else "теле Issue"
        raise RuntimeError(
            f"Develop {issue.repo}#{issue.issue_number}: в {task_context.DIR}/ "
            f"найден след усечения (маркер «{task_context.TRUNCATION_MARKER}») "
            f"в файлах: {', '.join(corrupted)}. Если это не сбойное усечение, а "
            "часть настоящего текста (маркер процитирован дословно) — исправь "
            f"исходный текст в {source} (перепиши или убери маркер) и повтори "
            "`/develop`."
        )

    parts.append(
        f"## Контекст\n\n"
        f"Требования и сценарий приёмки, если они собрались, — в `{task_context.DIR}/`"
        f" рядом с кодом; что именно там лежит и в каком порядке читать — в "
        f"`{task_context.DIR}/{task_context.CONTEXT_MAP}`. Это файлы git, а не "
        "пересказ: открывай по мере надобности, потолка на объём нет.\n\n"
        f"Результат проверяется сценарием приёмки, если он есть "
        f"(`{task_context.DIR}/{task_context.HOWTODEMO}`), и тестами репозитория."
        + ("" if branch else "\n\nАналитики по задаче нет — работай от тела Issue.")
    )

    # Правила репозитория и Repowise (как было)
    rules = (clone_dir / ".openhands" / "task-rules.md")
    parts.append(rules.read_text(encoding="utf-8") if rules.exists() else _DEV_FALLBACK_RULES)
    # Дописывается ПОСЛЕ правил репозитория, а не вместо: правила проекта
    # главнее, а обращение к индексу — общий приём контура, который к ним
    # добавляется в обеих ветках (свои правила есть и когда их нет).
    if ports.repowise().enabled():
        parts.append(_DEV_REPOWISE_RULES)

    # Всегда, независимо от того, чьи правила выше: свои у репозитория или
    # запасные. Требование контура к прогону не может зависеть от того, завёл
    # ли репозиторий свой файл правил.
    parts.append(_DEV_REFLECT_NOTE_RULE)

    # Правило фокуса — той же логикой и в том же месте, что и след решения
    # чуть выше: отдельным блоком постановки, а не довеском к запасным
    # правилам, иначе граница приёмки пропадала бы ровно там, где нужнее
    # всего — в репозиториях со своими правилами.
    parts.append(_FOCUS_RULE)

    # Правила и накопленный опыт организации — слой саморефлексии.
    #
    # Отбор идёт ЗДЕСЬ, а не отдельной активностью перед этой. Отдельная
    # активность повезла бы текст правил через полезную нагрузку Temporal, а
    # это против решения, закреплённого `tests/test_develop_child.py`: между
    # шагами едут числа и пути, не содержимое, потолок 4 КБ.
    #
    # Блок дописывается ПОСЛЕДНИМ и НЕ начинается с заголовка: разбор на
    # секции ниже сделал бы его именем секции без содержимого. При выключенном
    # слое текст пуст, и постановка не меняется ни на символ.
    # Контрольная выборка: каждая N-я задача идёт БЕЗ правил. Не порча
    # прогона, а единственный способ ответить на вопрос «слой не мешает?» —
    # сравнивать доли исходов не с чем, если правила подсыпаются всегда.
    if ports.memory().control_arm(issue.issue_number):
        logger.info("Develop %s#%s: контрольная итерация — правила организации "
                    "не подсыпаются", issue.repo, issue.issue_number)
        memory_rules = ports.memory().Rules(text="", ids=[])
    else:
        memory_rules = ports.memory().rules(ports.memory().DEVELOP, repo=issue.repo,
                                    query=f"{issue.title}\n{fresh_body or ''}")
    if memory_rules.text:
        # Заголовок ставит КОНТУР, а не слой: слой не знает, во что его блок
        # вставят, и заголовков не выдаёт. Отдельной секцией, а не довеском к
        # предыдущей — см. докстринг ORG_RULES_HEADING.
        parts.append(f"{ORG_RULES_HEADING}\n{memory_rules.text.strip()}")

    # Постановка короткая по построению: тяжёлый контекст ушёл в `.harness/`
    # выше, здесь остаются заголовок/тело задачи, указатель на контекст и
    # рабочие инструкции контура — потолка на размер больше нет и не нужно.
    task = _join_sections(_split_sections(parts))
    (clone_dir / ".task.md").write_text(task, encoding="utf-8")

    # Перечень подсыпанных правил — файлом в КОРНЕ задачи, а не в клоне.
    # Корень лежит вне рабочего дерева git, поэтому файл физически не может
    # уехать в коммит: `git add -A` его не видит. Через полезную нагрузку
    # Temporal перечень не везём — между шагами едут числа и пути.
    _write_injected_rules(root, memory_rules.ids)

    _handover_to_runner(root)
    return task, memory_rules.ids


INJECTED_RULES_FILE = ".reflect-rules.json"


def _write_injected_rules(root: Path, ids: list[str]) -> None:
    """Сохранить перечень. Отказ записи не срывает подготовку постановки."""
    try:
        (root / INJECTED_RULES_FILE).write_text(
            json.dumps(ids, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("перечень подсыпанных правил не сохранён: %s", e)


SIGNALS_FILE = ".reflect-signals.json"


def _write_signal(root: Path, name: str, value) -> None:
    """Добавить измеренный сигнал. Отказ записи не срывает шаг."""
    try:
        path = root / SIGNALS_FILE
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except ValueError:
                data = {}
        data[name] = value
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("сигнал %s не сохранён: %s", name, e)


def _read_signals(root: Path) -> dict:
    """Прочитать измеренные сигналы. Нет файла — пусто, это обычный ход дел."""
    try:
        data = json.loads((root / SIGNALS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_injected_rules(root: Path) -> list[str]:
    """Прочитать перечень. Нет файла — пустой список, это обычный ход событий."""
    try:
        raw = json.loads((root / INJECTED_RULES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in raw] if isinstance(raw, list) else []


_DEV_REPOWISE_RULES = """## Индекс кода (MCP-сервер `repowise`) — обращение обязательно

Постоянный индекс репозиториев организации: граф символов, история git, blame,
поиск, оценка риска правки, мёртвый код.

1. **До начала работы** — ПЕРВЫМ ДЕЙСТВИЕМ, до чтения файлов и до первой
   правки — задай индексу не меньше одного вопроса о компонентах, которые
   собираешься менять, и об их связях: `search_codebase`, `get_context`,
   `get_symbol`, `get_answer`. Это дешевле, чем читать репозиторий целиком, и
   точнее, чем догадываться по именам файлов.

   «Задача выглядит простой», «требования и так подробные», «репозиторий
   маленький» — НЕ основания пропустить шаг. Индекс знает то, чего нет ни в
   требованиях, ни в файлах: кто ещё вызывает этот код, чем он был раньше и
   почему устроен так. Пропуск шага — ошибка прогона, даже если правка вышла
   верной.
2. **При затруднении** спроси снова — вместо того чтобы продолжать вслепую.
   Не понял, почему код устроен так — спроси про историю и решение
   (`get_why`, `get_risk`), а не переписывай.
3. **Индекс недоступен** — работай без него. Это штатный режим, а не повод
   останавливаться. Недоступен — значит вызов вернул ошибку; отсутствие
   желания спрашивать недоступностью не считается.

Весь диалог сохраняется автоматически и публикуется артефактом: пересказывать
его в отчёте не нужно.
"""


REFLECT_NOTE_FILE = ".reflect.md"


TASK_BODY_LIMIT = 4000


_DEV_FALLBACK_RULES = f"""## Как работать

Правила репозитория — в `AGENTS.md` и `CLAUDE.md`, они обязательны.

1. **MVP первым.** Кратчайший путь к тому, что просят. Не углубляйся в
   надёжность и редкие ветки, пока основное не работает.
2. **Edge-кейс — не в эту ветку.** Найденное по дороге не чини здесь, а запиши
   в `{develop.FOLLOWUPS_FILE}` в корне рабочего каталога — по одному разделу
   `## <кратко что не учтено>` на находку, в теле: где (файл:строка), чем
   грозит, при каких условиях всплывёт. Issue по ним заведёт контур: `gh` и
   токена у тебя нет намеренно. Ничего не нашёл — файл не создавай.
3. **Тесты.** Прогоняй проверки проекта; красный прогон в PR не отдаём.
4. **Коммитить самому не надо** — коммит, пуш и PR делает контур после тебя.
"""


_DEV_REFLECT_NOTE_RULE = f"""## След решения

В конце запиши `{REFLECT_NOTE_FILE}` в корне рабочего
каталога тремя разделами: `## Намерение` — что ты решил сделать и почему
именно так; `## Допущения` — что принял на веру, не проверив; `## Сомнения` —
где не уверен и что стоит перепроверить человеку. По строке на пункт.

Это не отчёт о работе: дифф и так виден. Это то, чего по диффу НЕ
восстановить — почему сделано так, а не иначе. Восстанавливать это потом по
артефактам бессмысленно: на такой задаче даже сильные модели угадывают редко.

Файл в коммит не попадёт, его снимает контур.
"""


_FOCUS_RULE = f"""## Фокус

Нашёл по дороге то, без чего сценарий приёмки всё равно пройдёт, — запиши в
`{develop.FOLLOWUPS_FILE}` и иди дальше. Нужно для прохождения сценария — сделай здесь же,
отдельной задачи не заводи.

Граница не на вкус: критерий один — пройдёт ли сценарий без этого.

Ветка, где сделано лишнее, ревьюится дольше и откатывается целиком. Работа,
которой не хватило для сценария, возвращается кругом правок и стоит второго
прогона.
"""


def _handover_to_runner(path: Path) -> None:
    """Передать каталог задачи раннеру целиком: он работает не от root.

    Передаётся ВЕСЬ каталог задачи, а не только клон. Каталог задачи — это
    ещё и `$HOME` раннера (см. `_runner_home`), а OpenHands держит там своё
    состояние: `$HOME/.openhands/conversations`. Оставленный за root'ом, он
    даёт `PermissionError` на первом же шаге, но код возврата остаётся нулевым
    — снаружи прогон выглядит как отработавший, а правок нет ни одной.

    Падаем громко. Молча оставленный каталог root'а — рабочее место, в которое
    агент не может писать: он не сообщает об отказе, а уходит писать в /tmp и
    докладывает об успехе. Прогон отрабатывает целиком и не оставляет ни одной
    правки — отказ, который снаружи выглядит как исправная работа.
    """
    try:
        for current, dirs, files in os.walk(path):
            os.chown(current, develop.RUNNER_UID, develop.RUNNER_GID)
            for name in (*dirs, *files):
                os.chown(os.path.join(current, name),
                         develop.RUNNER_UID, develop.RUNNER_GID)
    except OSError as exc:
        raise RuntimeError(
            f"не передал рабочий каталог раннеру (uid {develop.RUNNER_UID}): {exc}. "
            "Без этого агент не сможет писать в него и молча уйдёт в /tmp."
        ) from exc


def _reap_runner(slug: str) -> None:
    """Снять контейнер прошлой попытки, если он пережил своего запускателя.

    Temporal повторяет активность до трёх раз. Умерший вместе с воркером прогон
    контейнер за собой не убирает (`--rm` срабатывает только на нормальном
    выходе), и вторая попытка либо упирается в занятое имя, либо запускает
    второго агента в тот же рабочий каталог. На стенде остаток жил полчаса и
    доедал память, из-за которой следующая задача еле ползла.
    """
    subprocess.run(develop.reap_command(slug), capture_output=True, text=True,
                   timeout=60, check=False)


def _dev_run_agent(issue: IssueInput) -> str:
    """Прогон одноразового контейнера. Возвращает хвост вывода."""
    shortage = develop.resource_shortage()
    if shortage:
        # Отказ ДО старта, а не сожжённый прогон: контейнер агента поднимается
        # на общем хосте, и нехватка памяти бьёт не по нам, а по соседям через
        # OOM-killer, который выбирает жертву сам.
        raise RuntimeError(f"прогон агента не начат: {shortage}")
    slug = develop.task_slug(issue.repo, issue.issue_number)
    _reap_runner(slug)
    command = develop.runner_command(
        slug,
        image=develop.runner_image(),
        volume=develop.workspace_volume(),
        mount=develop.workspace_mount(),
        network=develop.proxy_network(),
        home=_runner_home(slug),
    )
    env = {**os.environ, **develop.runner_env(
        os.environ.get("ZAI_API_KEY", ""),
        os.environ.get("ZAI_BASE_URL", ""),
        os.environ.get("DEVELOP_MODEL", "").strip() or "openai/glm-4.6",
    )}
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=develop.run_timeout())
    tail = (result.stdout or "")[-4000:] + (result.stderr or "")[-2000:]
    if result.returncode != 0:
        raise RuntimeError(
            f"прогон агента разработки завершился с кодом {result.returncode}: "
            f"{tail[-1500:]}")
    # Логируем и на успехе. Раньше вывод жил только в тексте исключения, то есть
    # при ненулевом коде, — и прогон, который отработал двадцать минут и не
    # тронул ни одного файла, не оставлял ни строки. Разбираться было не с чем.
    logger.info("Develop %s#%s: вывод агента\n%s",
                issue.repo, issue.issue_number, tail or "(пусто)")
    return tail


DEV_DIALOG_PATH = "docs/research/issue-{n}-repowise-dialog.md"


def _collect_dev_dialog(repo: str, issue_number: int, run_failed: bool) -> str:
    """Транскрипт сессии разработки. Забирает ВОРКЕР, а не раннер.

    Раннер к этому моменту уже мёртв — в этом и смысл: артефакт переживает
    прогон, включая аварийный, а диалог полезен ровно тогда, когда разбирают
    неудачу.

    Пустая сессия даёт артефакт с отметкой, а не отсутствие артефакта:
    «агент не обращался к индексу» — это факт, который надо видеть, а не
    пробел, который надо угадывать.
    """
    session = ports.repowise().session_id(repo, issue_number, ports.repowise().DEVELOP)
    text = ports.repowise().transcript(session)
    if text:
        return text
    failed = " (прогон завершился аварийно)" if run_failed else ""
    return (
        f"---\nissue: {repo}#{issue_number}\nsession: {session}\n"
        f"agent: {ports.repowise().DEVELOP}\noutcome: no-turns\nturns: 0\n---\n\n"
        f"# Итог\n\nЗа время прогона обращений к индексу не было{failed}.\n\n"
        f"Причины бывают три: индекс был недоступен, задача не потребовала "
        f"дополнительного контекста, либо агент не воспользовался им, хотя "
        f"стоило. Первую отличают по артефакту аналитики того же Issue.\n"
    )


def _publish_dev_dialog_sync(issue: IssueInput, branch: str) -> None:
    """Опубликовать диалог разработки. Best-effort: исход прогона не подменяет.

    Сбой публикации артефакта не должен выглядеть как сбой разработки — иначе
    разбор начнут не с того места.
    """
    if not ports.repowise().enabled():
        return
    text = _collect_dev_dialog(issue.repo, issue.issue_number, run_failed=False)
    path = DEV_DIALOG_PATH.format(n=issue.issue_number)
    try:
        if branch:
            ports.github().push_artifacts_to_branch(
                issue.repo, branch, {path: text},
                f"docs(repowise): диалог разработки по issue #{issue.issue_number}")
        ports.github().post_comment(
            issue.repo, issue.issue_number,
            f"## 🧭 Контекст из Repowise (разработка)\n\n"
            f"Диалог агента разработки с индексом кода — `{path}`"
            f"{f' в ветке `{branch}`' if branch else ''}.\n\n"
            f"<details><summary>Показать</summary>\n\n{text[:20000]}\n\n</details>")
    except Exception as exc:
        logger.warning("диалог разработки не опубликован (%s#%s): %s",
                       issue.repo, issue.issue_number, exc)


def _dev_tests(issue: IssueInput) -> str:
    """Прогон проверок проекта. Пусто в конфиге — шаг пропускается.

    Гоняется ЗДЕСЬ, до пуша: красный код не должен доезжать до PR, а на PR от
    агента CI может и не запуститься (события от токена Actions не порождают
    прогонов).
    """
    root, clone_dir = _dev_paths(issue)
    command = os.environ.get("DEVELOP_TEST_COMMAND", "").strip()
    if not command:
        # Пусто — шаг пропускается, и это НЕ «тесты прошли». Записываем
        # неизвестность явно: иначе слой саморефлексии засчитает пропуск как
        # успех, а свёртка сигналов начнёт хвалить прогоны, которых не было.
        _write_signal(root, "tests_passed", None)
        return "(проверки не заданы — DEVELOP_TEST_COMMAND пуст)"

    result = subprocess.run(command, shell=True, cwd=str(clone_dir),
                            capture_output=True, text=True, timeout=DEV_TESTS_TIMEOUT_SEC)
    out = ((result.stdout or "") + (result.stderr or ""))[-3000:]

    # Исход пишется ДО возможного исключения. Красный прогон — самый интересный
    # для разбора, и терять о нём запись значит собирать статистику только по
    # удачам.
    _write_signal(root, "tests_passed", result.returncode == 0)

    if result.returncode != 0:
        raise RuntimeError(f"проверки не прошли (код {result.returncode}):\n{out[-1500:]}")
    return out


def _dev_git(clone_dir: Path):
    """git по рабочему дереву задачи, от лица воркера.

    Каталог задачи передан раннеру (uid 10001), а воркер работает от root.
    Голый `git` отвечает на это `fatal: detected dubious ownership` и
    отказывается работать — так на живом прогоне #169 упала диагностика
    красного прогона, и круг правок не состоялся вовсе. `safe.directory`
    объявляет каталог доверенным для конкретной команды, не трогая глобальный
    конфиг; тот же приём используется при публикации (`github_client`).
    """
    env = {**os.environ,
           "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "safe.directory",
           "GIT_CONFIG_VALUE_0": "*"}

    def git(*args: str, check: bool = True):
        proc = subprocess.run(["git", "-C", str(clone_dir), *args], env=env,
                              capture_output=True, text=True, timeout=300)
        if check and proc.returncode:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise RuntimeError(f"git {' '.join(args)} → код {proc.returncode}: {detail}")
        return proc

    return git


def _test_report_patterns() -> tuple[str, ...]:
    """Где искать отчёт. Пусто в конфиге — обычные места (B5)."""
    raw = os.environ.get("DEVELOP_TEST_REPORT", "").strip()
    if not raw:
        return test_report.DEFAULT_PATTERNS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _run_test_command(cwd: Path) -> int:
    """Прогон проверок в указанном дереве. Возвращает код, не бросает.

    Используется базовой линией и перепроверкой на мигание: там красный код —
    это ИСХОД, а не отказ шага.
    """
    command = os.environ.get("DEVELOP_TEST_COMMAND", "").strip()
    result = subprocess.run(command, shell=True, cwd=str(cwd),
                            capture_output=True, text=True,
                            timeout=DEV_TESTS_TIMEOUT_SEC)
    return result.returncode


def _dev_last_failures(issue: IssueInput) -> set[str] | None:
    """Что упало в ИТОГОВОМ прогоне — отчёт уже написан `dev_tests`."""
    _, clone_dir = _dev_paths(issue)
    return test_report.failed_tests(clone_dir, _test_report_patterns())


def _dev_baseline_failures(issue: IssueInput) -> set[str] | None:
    """Что падало БЕЗ правки агента — на отдельном чистом дереве.

    Отдельное дерево, а НЕ `git stash` (B2): сорванный `stash pop` уничтожает
    работу агента — ровно то, что контур научился спасать черновиком. Механизм
    проверки не имеет права уничтожать то, что проверяет.

    Дерево не несёт установленных зависимостей, и там, где тесты без них не
    идут, прогон закономерно упадёт. Это штатный откат к прежнему поведению
    (B16), а не дефект: исход просто окажется неразобранным.
    """
    root, clone_dir = _dev_paths(issue)
    base_tree = root / "baseline"
    shutil.rmtree(base_tree, ignore_errors=True)
    git = _dev_git(clone_dir)
    head = git("rev-parse", "HEAD").stdout.strip()
    git("worktree", "add", "--detach", str(base_tree), head)
    try:
        _run_test_command(base_tree)
        return test_report.failed_tests(base_tree, _test_report_patterns())
    finally:
        # Дерево снимается всегда: оно живёт в общем томе с раннером, а тот
        # ограничен по месту. Осиротевшая регистрация worktree к тому же
        # ломает следующий `worktree add` в тот же путь.
        git("worktree", "remove", "--force", str(base_tree), check=False)


def _dev_rerun_failures(issue: IssueInput) -> set[str] | None:
    """Повтор набора на дереве агента — проверка на мигание (B6).

    Перегоняется ВЕСЬ набор, а не подозрительные тесты поимённо (B7): выбор
    отдельных требует синтаксиса конкретного раннера — той самой привязки, от
    которой уходит разбор отчёта.
    """
    _, clone_dir = _dev_paths(issue)
    _run_test_command(clone_dir)
    return test_report.failed_tests(clone_dir, _test_report_patterns())


def _diagnose(issue: IssueInput, baseline: list[str] | None) -> Diagnosis:
    root, _ = _dev_paths(issue)
    unparsed = Diagnosis(parsed=False, baseline=[], own=[], foreign=[])

    after = _dev_last_failures(issue)
    if after is None:
        return unparsed

    if baseline is None:
        base = _dev_baseline_failures(issue)
        if base is None:
            return unparsed
        # Мигающий тест падает в итоговом прогоне и не падает в повторном.
        # Своим считаем только устойчивое падение.
        again = _dev_rerun_failures(issue)
        if again is None:
            return unparsed
        after = after & again
    else:
        base = set(baseline)

    own = sorted(after - base)
    foreign = sorted(after & base)

    # `tests_passed` — про СВОИ поломки (B22): иначе слой саморефлексии считает
    # неудачей чистую работу в красном репозитории и учится на шуме.
    _write_signal(root, "tests_passed", not own)
    _write_signal(root, "tests_red_before", bool(base))
    # Смысл сигнала сменился — ряд разорван (B23). Без признака версии свёртка
    # усреднит несравнимое: до выкладки писали «набор зелёный», после —
    # «агент не сломал своего».
    _write_signal(root, "tests_signal_version", 2)

    return Diagnosis(parsed=True, baseline=sorted(base), own=own, foreign=foreign)


@activity.defn
async def dev_diagnose(issue: IssueInput,
                       baseline: list[str] | None) -> Diagnosis:
    """Чьи это поломки — агента или репозитория.

    `baseline=None` — снять базовую линию и перепроверить на мигание.
    Непустой список — база уже известна (повтор после починки, B13): её не
    снимают заново и на мигание не перепроверяют (B8).

    Диагностика НЕ имеет права ронять прогон: она объясняет отказ тестов, а
    не заменяет его. Любой свой сбой — неразобранный исход, то есть прежнее
    поведение контура.
    """
    try:
        return await _run_with_heartbeat(_diagnose, issue, baseline,
                                         label="dev:diagnose")
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        activity.logger.warning("Develop %s#%s: диагностика не удалась: %s",
                                issue.repo, issue.issue_number, exc)
        return Diagnosis(parsed=False, baseline=[], own=[], foreign=[])


def _clear_test_reports(clone_dir: Path) -> list[str]:
    """Снять отчёты о тестах из рабочего дерева перед коммитом.

    Отчёт пишется в дерево самим прогоном тестов, а `publish_worktree`
    забирает дерево целиком через `git add -A`. Без снятия отчёт уехал бы в PR
    мусором и — хуже — обманул бы гвард «изменений нет, открывать нечего»:
    прогон, где агент не тронул ни строки, всё равно открыл бы пул-реквест.
    Ровно это уже случалось с постановкой `.task.md`.

    Снимаются только НЕОТСЛЕЖИВАЕМЫЕ файлы: отчёт, лежащий в репозитории,
    принадлежит ему, а не нашему прогону, и его удаление показалось бы в PR
    правкой, которой никто не просил.
    """
    removed: list[str] = []
    for path in test_report.find_reports(clone_dir, _test_report_patterns()):
        rel = path.relative_to(clone_dir)
        tracked = _dev_git(clone_dir)(
            "ls-files", "--error-unmatch", str(rel), check=False).returncode == 0
        if tracked:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("отчёт %s не снят: %s", rel, exc)
            continue
        removed.append(str(rel))
    return removed


def _dev_publish(issue: IssueInput, branch: str, foreign: list[str]) -> int | None:
    """Коммит, пуш и PR — руками воркера, его токеном.

    Агенту токен не давали намеренно; здесь он уже не нужен агенту, а нужен
    контуру. Возвращает номер PR либо None, если агент ничего не изменил.
    """
    # Корень задачи нужен не только клону: туда перекладывается файл намерений,
    # чтобы пережить снятие из рабочего дерева.
    root, clone_dir = _dev_paths(issue)
    # Постановка — вход контура, а не часть правки. Она лежит в рабочем дереве, и
    # `git add -A` забирает её вместе с кодом: на живом прогоне это дало PR из
    # одного файла на 1721 строку — нашей же постановки. Хуже того, дифф из неё
    # обманывал гвард «изменений нет — открывать нечего», и PR открывался по
    # прогону, в котором агент не тронул ни одного файла.
    # Одна точка снятия на весь контур: перечень служебных файлов живёт в
    # `develop.py`, а не переписывается в каждой функции заново.
    removed = develop.clear_service_files(clone_dir, keep_dir=root)
    if removed:
        logger.info("Develop %s#%s: сняты служебные файлы: %s",
                    issue.repo, issue.issue_number, ", ".join(removed))
    reports = _clear_test_reports(clone_dir)
    if reports:
        logger.info("Develop %s#%s: сняты отчёты о тестах: %s",
                    issue.repo, issue.issue_number, ", ".join(reports))
    work = develop.work_branch(issue.issue_number)
    return ports.github().publish_worktree(
        issue.repo, str(clone_dir), work,
        title=f"feat(#{issue.issue_number}): {issue.title}",
        body=develop.pr_body(issue.issue_number, branch=branch, foreign=foreign),
        message=f"feat(#{issue.issue_number}): реализация по системным требованиям",
        # `.harness/` — единственный служебный каталог, что НЕ снимается
        # (задача 7: контекст обязан дойти до PR). Он пишется в `_dev_prepare`
        # ДО прогона агента и потому существует независимо от того, тронул ли
        # агент код, — «пустой прогон» больше не значит «дифф пуст», если
        # эту проверку не поправить. Исключаем каталог из решения «есть ли
        # диф», а не из самого коммита: `git add -A` продолжает забирать его.
        ignore_for_empty_check=(f"{task_context.DIR}/**",),
        # M3 (ревью задачи 7): если `.gitignore` ЦЕЛЕВОГО репозитория содержит
        # `.harness/`, голый `git add -A` молча пропускает каталог — PR уйдёт
        # без контекста и без единого предупреждения. `force_include`
        # заставляет каталог попасть в коммит независимо от `.gitignore` и
        # подтверждает это фактом (деревом HEAD), а не только вызовом `add -f`.
        force_include=(task_context.DIR,),
    )


async def _dev_resolve_branch(issue: IssueInput, root_issue: int | None = None,
                              branch: str | None = None) -> str:
    """Выключатель разработки + ветка аналитики — общий вход в стадию.

    Раньше — две дословные копии, в `trigger_openhands_resolver` и в
    `dev_begin`: правка формата ветки или условия выключателя попала бы в
    одну и была бы забыта в другой, и линейный путь молча разошёлся бы с
    путём через дочерний воркфлоу.
    """
    if not develop.enabled():
        raise RuntimeError(
            "DEVELOP_ENABLED выключен — задача остаётся в очереди к разработчику")

    if branch is None:
        source = root_issue if root_issue else issue.issue_number
        branch = f"research/issue-{source}"
    if not await asyncio.to_thread(ports.github().branch_exists, issue.repo, branch):
        # Путь бага: аналитики не было, и ветки с артефактами тоже. Штатно —
        # агент работает от тела Issue, но знать об этом должен явно.
        branch = ""
    return branch


async def _dev_dispatch_and_announce(issue: IssueInput, branch: str) -> None:
    """Режим `dispatch`: запуск в GitHub Actions + объявление человеку.

    Раньше — две дословные копии, в `trigger_openhands_resolver` и в
    `dev_dispatch`: правка аргументов `dispatch_inputs` или текста объявления
    попала бы в одну и была бы забыта в другой.
    """
    await asyncio.to_thread(
        ports.github().dispatch_workflow,
        issue.repo, develop.workflow_file(), develop.workflow_ref(),
        develop.dispatch_inputs(issue.issue_number, branch=branch),
    )
    await _dev_announce(issue, branch,
                        where="запустил OpenHands Resolver в GitHub Actions")


@activity.defn
async def trigger_openhands_resolver(issue: IssueInput, root_issue: int | None = None, 
                                     branch: str | None = None) -> int | None:
    """Активность Develop: разработка по подготовленному Issue.

    Два режима (`develop.py`). `local` — прогон одноразовым контейнером
    на своём сервере, контур замкнут внутри стенда. `dispatch` — прогон уезжает
    в GitHub Actions, для репозиториев без стенда.

    Возвращает номер PR (режим `local`) либо None (`dispatch`: результат
    придёт событием `pr-open`, прогон идёт на чужой стороне).
    
    ISSUE-113: для подзадачи плана использует ветку родителя, а не свою.
    `root_issue` — номер родительской задачи (если это подзадача плана),
    `branch` — готовая ветка (если вычислена в workflow).
    """
    branch = await _dev_resolve_branch(issue, root_issue=root_issue, branch=branch)

    if develop.mode() == develop.DISPATCH:
        await _dev_dispatch_and_announce(issue, branch)
        return None

    # Порядок не косметический: сначала клон и постановка — они единственные
    # могут не состояться до того, как что-либо сказано человеку.
    task, rule_ids = await _run_with_heartbeat(_dev_prepare, issue, branch,
                                               label="dev:prepare")
    logger.info("Develop %s#%s: постановка (%d симв.), правил подсыпано %d\n%s",
                issue.repo, issue.issue_number, len(task), len(rule_ids), task[:2000])
    await _dev_announce(issue, branch, where="запустил OpenHands на своём сервере")

    try:
        await _run_with_heartbeat(_dev_run_agent, issue, label="dev:agent")
    finally:
        # В finally, а не после: диалог полезнее всего при разборе упавшего
        # прогона, и терять его ровно в этом случае было бы худшим из исходов.
        await asyncio.to_thread(_publish_dev_dialog_sync, issue, branch)
    # Находки собираются ДО тестов и публикации: файл находок обязан исчезнуть
    # из рабочего дерева раньше коммита, иначе он уедет в PR — в ревью как мусор,
    # а на следующем круге правок агент прочитает свои прошлые находки как новые.
    await collect_dev_followups(issue)
    await _run_with_heartbeat(_dev_tests, issue, label="dev:tests")
    # Монолитный путь диагноза красного прогона не делает — тесты здесь
    # либо зелёные, либо шаг уже упал. Чужой красноты, о которой стоило бы
    # оговориться в теле PR, взяться неоткуда.
    number = await _run_with_heartbeat(_dev_publish, issue, branch, [],
                                       label="dev:publish")

    if number is None:
        task, _clone = _dev_paths(issue)
        raise RuntimeError(develop.empty_run_reason(task))
    return number


@activity.defn
async def dev_begin(issue: IssueInput) -> DevelopPlan:
    """Решения входа в стадию: работаем ли вообще, в каком режиме и от чего.

    Собрано в один шаг намеренно. Выключатель и наличие ветки читаются из
    окружения и из GitHub — в воркфлоу так нельзя, там решение обязано быть
    детерминированным при реплее. Один вызов вместо трёх ещё и делает вход в
    стадию одной строкой в истории.
    """
    branch = await _dev_resolve_branch(issue)
    return DevelopPlan(
        mode=develop.mode(), branch=branch,
        repair_rounds=max(0, int(os.environ.get("DEVELOP_REPAIR_ROUNDS", "1") or 1)),
    )


@activity.defn
async def dev_dispatch(issue: IssueInput, branch: str) -> None:
    """Режим `dispatch`: прогон уезжает в GitHub Actions.

    Своих шагов на этой стороне нет — отсюда и один вызов вместо цепочки.
    Результат придёт событием `pr-open` от внешнего агента.
    """
    await _dev_dispatch_and_announce(issue, branch)


@activity.defn
async def dev_prepare(issue: IssueInput, branch: str) -> int:
    """Шаг 1: свежий клон и постановка файлом. Возвращает длину постановки.

    Длину, а не текст: постановка уже лежит в `.task.md` в общем томе, и
    дублировать её в payload Temporal незачем. В лог она уходит целиком — там
    её и смотрят, когда разбираются «почему агент сделал не то».
    """
    task, rule_ids = await _run_with_heartbeat(_dev_prepare, issue, branch,
                                               label="dev:prepare")
    logger.info("Develop %s#%s: постановка (%d симв.), правил подсыпано %d\n%s",
                issue.repo, issue.issue_number, len(task), len(rule_ids), task[:2000])
    return len(task)


@activity.defn
async def dev_announce(issue: IssueInput, branch: str) -> None:
    """Шаг 2: метка и комментарий о начале работы — best-effort.

    Отдельным шагом, а не частью прогона: объявление обязано случиться ПОСЛЕ
    успешного клона (иначе контур скажет о работе, которая не началась) и ДО
    прогона агента (иначе человек двадцать минут не знает, что задача в работе).
    """
    await _dev_announce(issue, branch, where="запустил OpenHands на своём сервере")


@activity.defn
async def dev_run_agent(issue: IssueInput) -> None:
    """Шаг 3: прогон одноразового контейнера агента.

    Возврата нет: хвост вывода уходит в лог воркера на любом исходе
    (`_dev_run_agent`), а в историю воркфлоу ему не место — это килобайты
    текста на прогон.
    """
    await _run_with_heartbeat(_dev_run_agent, issue, label="dev:agent")


def _repair_brief(issue: IssueInput, own: list[str]) -> str:
    """Постановка круга правок. ТОЛЬКО свои падения (B11)."""
    listed = "\n".join(f"- `{name}`" for name in own)
    return (
        f"# Круг правок по Issue #{issue.issue_number}\n\n"
        f"Твоя правка уже лежит в этом рабочем дереве — начинай с неё, не с нуля.\n\n"
        f"После неё упали тесты, которых до правки не было:\n\n{listed}\n\n"
        f"## Что нужно\n\n"
        f"Почини **только эти** падения, не меняя решения задачи.\n\n"
        f"Остальные красные тесты в наборе, если они есть, падали и без твоей "
        f"правки — они не твои и трогать их не нужно.\n"
    )


def _dev_repair(issue: IssueInput, own: list[str]) -> str:
    """Переписать постановку на починку и прогнать того же агента.

    Постановка подменяется прямо в рабочем дереве: агент читает `.task.md`
    (см. `_dev_prepare`), и другого входа у него нет. Файл служебный и
    снимается перед коммитом (`develop.SERVICE_FILES`) — в PR он не уедет.
    """
    root, clone_dir = _dev_paths(issue)
    (clone_dir / ".task.md").write_text(_repair_brief(issue, own), encoding="utf-8")
    _write_signal(root, "repair_attempts", 1)
    return _dev_run_agent(issue)


@activity.defn
async def dev_repair(issue: IssueInput, own: list[str]) -> None:
    """Повторный заход агента: починить своё.

    Отдельная активность, а не флаг у `dev_run_agent` (B12): свой шаг в
    истории Temporal, свой таймаут и видимый факт, что контур пробовал
    починить, а не сдался сразу.

    Возврата нет по той же причине, что и у `dev_run_agent`: хвост вывода —
    килобайты текста, им не место в истории воркфлоу.
    """
    await _run_with_heartbeat(_dev_repair, issue, own, label="dev:repair")


@activity.defn
async def dev_announce_repair(issue: IssueInput, own: list[str]) -> None:
    """Сказать в ленте, что контур чинит своё и что именно.

    Молчащий контур, который внутри себя делает второй дорогой заход
    (агент идёт до 45 минут), неотличим от зависшего.

    Сообщение не имеет права сорвать починку: отказ гасится здесь.
    """
    listed = "\n".join(f"- `{name}`" for name in own)
    try:
        await asyncio.to_thread(
            ports.github().post_comment, issue.repo, issue.issue_number,
            f"## 🔁 Чиню своё\n\n"
            f"После правки упали тесты, которых до неё не было:\n\n{listed}\n\n"
            f"Отправляю агента на повторный заход — он правит только эти "
            f"падения. Остальные красные тесты в наборе, если они есть, "
            f"падали и без правки.\n\n"
            f"Заход один: не починит — отдам задачу человеку.")
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        activity.logger.warning("Develop %s#%s: о починке не сообщено: %s",
                                issue.repo, issue.issue_number, exc)


@activity.defn
async def dev_empty_run_reason(issue: IssueInput) -> str:
    """Почему прогон агента не дал изменений — по следам самого раннера.

    Отдельной активностью, потому что воркфлоу файловой системы не видит, а
    признак лежит именно там: каталог событий разговора OpenHands. Пуст —
    агент не сделал ни одного хода, то есть отказало окружение, а не работа
    агента. Это разные новости и зовут человека в разные места.
    """
    task, _clone = _dev_paths(issue)
    return develop.empty_run_reason(task)


@activity.defn
async def dev_followups(issue: IssueInput) -> list[str]:
    """Шаг 4: находки агента — строками в секцию GROW тела родителя.

    Идёт ДО тестов и публикации: файл находок обязан исчезнуть из рабочего
    дерева раньше коммита, иначе он уедет в PR — в ревью как мусор, а на
    следующем круге правок агент прочитает свои прошлые находки как новые.
    """
    return await collect_dev_followups(issue)


@activity.defn
async def dev_tests(issue: IssueInput) -> None:
    """Шаг 5: проверки проекта — до пуша.

    Красный код не должен доезжать до PR, а на PR от агента CI может и не
    запуститься: события от токена Actions не порождают прогонов.
    """
    await _run_with_heartbeat(_dev_tests, issue, label="dev:tests")


def _dev_publish_partial(issue: IssueInput, branch: str, reason: str) -> int | None:
    """Выложить черновиком то, что агент успел написать до срыва.

    Повторяет подготовку дерева из `_dev_publish` — снятие служебных файлов и
    сохранение `.harness/`: иначе в черновик уедет наша же постановка, а гвард
    «есть ли дифф» обманется ею и открыл бы черновик по прогону, в котором
    агент не тронул ни одного файла.
    """
    root, clone_dir = _dev_paths(issue)
    removed = develop.clear_service_files(clone_dir, keep_dir=root)
    if removed:
        logger.info("Develop %s#%s: сняты служебные файлы: %s",
                    issue.repo, issue.issue_number, ", ".join(removed))
    reports = _clear_test_reports(clone_dir)
    if reports:
        logger.info("Develop %s#%s: сняты отчёты о тестах: %s",
                    issue.repo, issue.issue_number, ", ".join(reports))
    work = develop.work_branch(issue.issue_number)
    return ports.github().publish_worktree(
        issue.repo, str(clone_dir), work,
        title=f"СОРВАЛОСЬ feat(#{issue.issue_number}): {issue.title}",
        body=("Прогон разработки **сорвался**. Это не готовая работа, а то, "
              "что агент успел написать до срыва, — материал для разбора.\n\n"
              f"Задача: #{issue.issue_number}\n\n"
              f"Причина:\n\n```\n{reason}\n```\n"),
        message=f"wip(#{issue.issue_number}): прогон сорвался, сохранено как есть",
        ignore_for_empty_check=(f"{task_context.DIR}/**",),
        force_include=(task_context.DIR,),
        # Черновик, а не обычный PR: работа заведомо негодная. Обычный выглядел
        # бы кандидатом на слияние, а ревью подобрало бы его и потратило бюджет
        # на то, что контур сам признал негодным.
        draft=True,
    )


@activity.defn
async def dev_publish_partial(issue: IssueInput, branch: str,
                              reason: str) -> int | None:
    """Спасти работу сорвавшегося прогона черновым PR.

    `None` — сохранять нечего (агент не тронул ни одного файла) либо выложить
    не удалось. И то, и другое — не повод падать: прогон УЖЕ сорвался, и
    спасательный шаг не имеет права подменить собой его причину.

    Отказ, ради которого написано: на `poh-demo-checkout#166` тринадцать минут
    работы агента исчезли из-за трёх красных тестов — `dev_publish` идёт после
    `dev_tests` и просто не выполнился.
    """
    try:
        number = await _run_with_heartbeat(_dev_publish_partial, issue, branch, reason,
                                           label="dev:publish-partial")
    except Exception as exc:  # noqa: BLE001 — см. докстринг: причина уже есть
        activity.logger.warning("Develop %s#%s: частичная выкладка не удалась: %s",
                                issue.repo, issue.issue_number, exc)
        return None
    if number is None:
        # Агент не изменил ни одного файла. Комментария нет намеренно: сообщать
        # человеку не о чем, а лишняя строка в ленте — шум.
        return None
    try:
        await asyncio.to_thread(
            ports.github().post_comment, issue.repo, issue.issue_number,
            f"## ⏸ Прогон разработки сорвался\n\n"
            f"Сохранил то, что успел написать агент, — черновым PR #{number}. "
            f"Это **не готовая работа**, а материал для разбора.\n\n"
            f"Причина:\n\n```\n{reason}\n```\n\n"
            f"Ревью на черновик не тратится: снимите статус черновика, когда "
            f"работа станет годной.")
    except Exception as exc:  # noqa: BLE001
        # Черновик уже открыт. Промолчать про номер значит потерять его из
        # виду вовсе — воркфлоу решит, что спасать было нечего.
        activity.logger.warning("Develop %s#%s: не удалось сообщить о черновике "
                                "#%s: %s", issue.repo, issue.issue_number, number, exc)
    return number


@activity.defn
async def dev_publish(issue: IssueInput, branch: str,
                      foreign: list[str]) -> int | None:
    """Шаг 6: коммит, пуш и PR — руками воркера, его токеном.

    `None` — агент не изменил ни одного файла. Это не сбой шага, а его
    результат; решение, что делать с пустым прогоном, принимает воркфлоу.

    `foreign` — тесты, красные и без правки агента. Уходят оговоркой в тело
    PR: красный набор без объяснения смотрящий примет за поломку агента и
    пойдёт разбирать его правку.
    """
    return await _run_with_heartbeat(_dev_publish, issue, branch, foreign,
                                     label="dev:publish")


@activity.defn
async def capture_episode(issue: IssueInput, branch: str,
                          pr_number: int | None) -> bool:
    """Шаг 7: запись об итерации — слою саморефлексии.

    Горячий такт: фиксируется то, что уже известно коду, БЕЗ обращения к
    модели. Оценивать в этот момент нечего — фактов об исходе ещё нет, их
    соберёт отложенный проход рефлексии, когда пул-реквест доедет до слияния
    или будет закрыт.

    Намерение агента берётся из файла `.reflect.md`, если тот его написал.
    Отсутствие файла НЕ срывает шаг: запись уходит без намерения, а сигналы и
    дифф остаются. Требовать от агента файл под угрозой падения стадии значило
    бы обменять работающую разработку на полноту записи.

    Возврат — успех отправки. Неуспех не роняет прогон: слой опционален.
    """
    if not ports.memory().enabled():
        return False

    root, clone_dir = _dev_paths(issue)
    # Из КОРНЯ: к этому моменту публикация уже сняла файл из рабочего дерева и
    # переложила сюда. Чтение из клона давало пустое намерение при исправном
    # агенте — файл удалялся за секунды до чтения.
    reflect = _read_reflect_note(root) or _read_reflect_note(clone_dir)

    episode = {
        "run_id": activity.info().workflow_id,
        "repo": issue.repo,
        "issue": issue.issue_number,
        # Текст задачи. Без него судья слоя видит только числа — и на живом
        # прогоне #94 поставил 0.93 правке в две строки на задачу «описать
        # поведение функций»: слито, тесты зелёные, круг правок один. Числа
        # хорошие, работа не сделана. Соразмерность правки замыслу проверяется
        # только против постановки.
        #
        # Режется на стороне отправителя: гнать по сети и хранить в памяти
        # десятки килобайт постановки незачем, слой всё равно возьмёт начало.
        "task_title": issue.title,
        "task_body": (issue.body or "")[:TASK_BODY_LIMIT] or None,
        "phase": "develop",
        "agent": ports.memory().DEVELOP,
        "branch": develop.work_branch(issue.issue_number),
        "pr_number": pr_number,
        "model": os.environ.get("DEVELOP_MODEL", "").strip() or "openai/glm-4.6",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "rules_injected": _read_injected_rules(root),
        # Число попыток активности: правило контура «Attempt > 1 у долгой
        # активности — уже беда» до сих пор нигде не читалось кодом.
        #
        # Плюс всё, что контур измерил по ходу прогона: результат тестов и
        # прочее. Измерение из первых рук точнее восстановленного постфактум.
        # `control` ставится ЯВНО, а не выводится из пустого перечня правил:
        # пустым он бывает и когда слой был недоступен в момент подготовки.
        # «Правил не дали нарочно» и «правил не досталось» — разные вещи, и
        # смешивать их значит подмешивать в контрольную выборку брак.
        "artifacts": {"activity_attempt": activity.info().attempt,
                      "control": ports.memory().control_arm(issue.issue_number),
                      **_read_signals(root)},
        **reflect,
    }
    ok = await asyncio.to_thread(ports.memory().put_episode, episode)
    if ok:
        logger.info("Develop %s#%s: запись об итерации отдана слою памяти",
                    issue.repo, issue.issue_number)
    return ok


def _read_reflect_note(clone_dir: Path) -> dict:
    """Разобрать `.reflect.md`, если агент его написал.

    Формат нарочно простой — заголовки второго уровня «Намерение»,
    «Допущения», «Сомнения». Требовать от агента строгий JSON значило бы
    получать пустой файл там, где сейчас получается частичный.
    """
    path = clone_dir / REFLECT_NOTE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    buckets: dict[str, list[str]] = {}
    current = None
    for line in text.split("\n"):
        head = line.strip().lstrip("#").strip().lower()
        if line.startswith("#") and head:
            current = head
            buckets[current] = []
        elif current:
            item = line.strip().lstrip("-").strip()
            if item:
                buckets[current].append(item)

    def pick(*names: str) -> list[str]:
        for n in names:
            for key, items in buckets.items():
                if key.startswith(n):
                    return items
        return []

    intent = pick("намерение", "intent")
    return {
        "intent": " ".join(intent) or None,
        "assumptions": pick("допущения", "assumptions"),
        "uncertainty": pick("сомнения", "uncertainty"),
        "alternatives_rejected": pick("отброшен", "alternatives"),
    }


async def collect_dev_followups(issue: IssueInput) -> list[str]:
    """Находки шага — строками в секцию GROW тела родителя, а не новыми Issue.

    Прежде каждая находка заводила отдельный Issue: он проходил триаж,
    поднимал свой вечный цикл и вставал в общую очередь контура — 221 из 267
    открытых задач организации заведены контуром, и находки составляют
    большую их часть. Теперь находка ждёт гейта приёмки строкой в секции GROW
    тела родителя; Issue из неё заведёт человек, если решит — и только после
    того, как MVP подтверждён (Task 11).

    Агент по-прежнему оставляет находки файлом: `gh` и GITHUB_TOKEN ему не
    дают намеренно, это весь смысл его изоляции — он исполняет чужой код. Файл
    снимается ПОСЛЕ того, как находки долетели до GROW (или сразу, если
    парсить оказалось нечего) — а не заранее. Снятый до сетевой записи файл
    убивал бы повтор активности так же, как сама неудавшаяся запись, только
    тише: следующая попытка не находила бы файл и молча докладывала бы «находок
    нет» вместо честного повтора (ревью, находка 3). Уехать в PR он всё равно
    не может, даже если так и останется лежать до конца прогона: `_dev_publish`
    снимает любой служебный файл через `develop.clear_service_files` перед
    коммитом, независимо от исхода этой функции.

    Накопление прежнего содержимого блока — присоединением целой строки
    записи к целому прежнему содержимому, без построчного разбора. Раньше
    накопление читало прежнее содержимое и оставляло только строки,
    начинающиеся с `- [` — многострочная находка занимает несколько
    физических строк, и её продолжение под это правило не подходило: со
    второго прогона от неё оставался только заголовок (ревью, находка 1). По
    той же причине терялся и любой текст, дописанный в блок человеком. Здесь
    прежнее содержимое — уже готовый текст блока, и трогать в нём нечего:
    новые записи дописываются к нему целиком, каким бы оно ни было.

    Запись в GROW — best-effort, как раньше было создание Issue: прогон
    разработки уже состоялся, и находка не должна ронять шаг целиком.
    Запись блока (`ports.issue_blocks().write`) намеренно отказывает
    `ValueError`, если содержимое
    похоже на маркер блока — а находка приходит от модели и может дословно
    процитировать разметку (например, разбирая баг в самой разметке блоков)
    — или если тело Issue уже повреждено. Такой отказ, как и любой сетевой
    сбой чтения/записи тела, ловится здесь: находка не засчитывается
    записанной на ЭТОМ прогоне, файл остаётся на диске для следующей попытки,
    а сам отказ уходит и в лог, и в Sentry (`capture_followups_failure`) —
    иначе о молчаливой потере узнают только по stdout контейнера, который
    никого не будит (ревью, находка 4).
    """
    _, clone_dir = _dev_paths(issue)
    path = clone_dir / develop.FOLLOWUPS_FILE
    if not path.exists():
        return []  # «не нашёл» — законный исход, комментировать нечего

    try:
        items = develop.parse_followups(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — разбор находок не ломает разработку
        logger.warning("не разобрал %s по %s#%s: %s",
                       develop.FOLLOWUPS_FILE, issue.repo, issue.issue_number, exc)
        items = []
    if not items:
        path.unlink(missing_ok=True)  # нечего сохранять, нечего и повторять
        return []

    try:
        body = await asyncio.to_thread(ports.github().get_issue_body,
                                       issue.repo, issue.issue_number)
        previous = ports.issue_blocks().read(body, ports.issue_blocks().GROW) or ""

        # Извлечём уже существующие заголовки из прежней секции GROW
        existing_titles = set()
        if previous.strip():
            for line in previous.split('\n'):
                # Формат: `- [ ] Заголовок — тело` или `- [ ] Заголовок`
                if line.strip().startswith('- [ ] '):
                    # Вытаскиваем заголовок до первого ` — ` (если есть) или до конца строки
                    rest = line.strip()[6:]  # убираем '- [ ] '
                    title = rest.split(' — ', 1)[0].strip()
                    if title:
                        existing_titles.add(title)

        # Фильтруем items: оставляем только новые (не в existing_titles)
        new_items = [item for item in items if item['title'] not in existing_titles]

        # Формируем строки только для новых записей
        lines = [f"- [ ] {item['title']} — {item.get('body', '').strip()}" for item in new_items]

        if lines:  # Только если есть новые записи
            new_block = "\n".join(lines)
            content = (f"{previous}\n{new_block}" if previous.strip()
                      else f"## GROW — после прохождения HowToDemo\n\n{new_block}")
            await asyncio.to_thread(
                ports.github().update_issue_body, issue.repo, issue.issue_number,
                ports.issue_blocks().write(body, ports.issue_blocks().GROW, content))
    except Exception as exc:  # noqa: BLE001 — запись находок не ломает разработку
        logger.warning("не записал находки по %s#%s в секцию %s: %s",
                       issue.repo, issue.issue_number, ports.issue_blocks().GROW, exc)
        ports.telemetry().capture_followups_failure(issue, type(exc).__name__, str(exc))
        return []
    path.unlink(missing_ok=True)  # находки доехали — теперь их можно снять
    return [item["title"] for item in new_items]


async def _dev_announce(issue: IssueInput, branch: str, *, where: str) -> None:
    """Метка и комментарий о начале работы — best-effort и ОДИН раз на задачу.

    Прогон к этому моменту начался; падать из-за непоставленной метки значило
    бы отправить в `failed` задачу, которая на самом деле в работе.

    Повторный вход в передачу (перезапуск активности, второе решение человека)
    не должен давать второго объявления: на живом прогоне #39 их набралось три
    штуки подряд, и по треду нельзя было понять, идёт одна работа или три.
    Признак — метка `in-development`: её ставит эта же функция строкой ниже, и
    снимает смена фазы (`set_phase`), то есть она держится ровно столько,
    сколько длится передача.
    """
    try:
        already = await asyncio.to_thread(
            ports.github().get_issue, issue.repo, issue.issue_number)
        names = [label["name"] for label in already.get("labels", [])]
        if develop.IN_DEVELOPMENT_LABEL in names:
            logger.info("Develop %s#%s: объявление уже сделано — повторно не пишу",
                        issue.repo, issue.issue_number)
            return
    except Exception as exc:
        # Не прочитали состояние — объявляем. Лишний комментарий хуже молчания
        # ровно настолько, насколько молчание хуже дубля: человек должен знать,
        # что работа идёт.
        logger.warning("Develop %s#%s: не прочитал метки (%s) — объявляю",
                       issue.repo, issue.issue_number, exc)
    for step, call in (
        ("метка", lambda: ports.github().add_label(
            issue.repo, issue.issue_number, develop.IN_DEVELOPMENT_LABEL)),
        ("комментарий", lambda: ports.github().post_comment(
            issue.repo, issue.issue_number,
            develop.handoff_comment(issue.issue_number, repo=issue.repo,
                                    branch=branch, where=where))),
    ):
        try:
            await asyncio.to_thread(call)
        except Exception as exc:
            logger.warning("Develop %s#%s: %s не проставлен (%s) — прогон уже идёт",
                           issue.repo, issue.issue_number, step, exc)


def _prfix_paths(repo: str, pr_number: int) -> tuple[Path, Path]:
    root = Path(develop.workspace_mount()) / pr_closing.task_slug(repo, pr_number)
    return root, root / "repo"


def _prfix_prepare(repo: str, pr_number: int, branch: str, task: str) -> None:
    """Свежий клон ВЕТКИ PR + постановка круга файлом.

    Клонируется именно ветка PR, а не основная: правки ложатся поверх того, что
    ревьюер видел, иначе круг переписывал бы чужую работу.
    """
    root, clone_dir = _prfix_paths(repo, pr_number)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    ports.github().clone_repo(repo, str(clone_dir), branch=branch)
    (clone_dir / ".task.md").write_text(task, encoding="utf-8")
    _handover_to_runner(root)


@activity.defn
async def run_pr_fix_round(repo: str, pr_number: int, round_number: int):
    """Один круг правок.

    `True` — правки внесены и запрошена перепроверка. Строка (возможно пустая) —
    правок не потребовалось, и это её разбор: агент не нашёл в ревью того, что
    требует изменений в коде. Законный исход, а не сбой; на нём круг и
    останавливается.

    Разные типы возврата намеренно: «сделали» и «не потребовалось» — разные
    исходы, и сводить их к булеву значению значило бы потерять объяснение,
    ради которого разбор и заводился.
    """
    pr = await asyncio.to_thread(ports.github().get_pull, repo, pr_number)
    branch = pr["head"]["ref"]
    review = await asyncio.to_thread(ports.github().review_text, repo, pr_number)
    task = pr_closing.build_task(pr_number, review=review, round_number=round_number,
                                 max_rounds_=pr_closing.max_rounds())

    # Правила организации для роли РАЗРАБОТКИ, а не ревью. Круг правок — это
    # агент, который пишет код: он читает замечания и меняет файлы. Правила
    # роли `review` описывают, как формулировать замечание, — исполнителю они
    # бесполезны, а нужные ему (как писать код в этой организации) не доезжали
    # вовсе. Блок дописывается к постановке здесь, а не в
    # `pr_closing.build_task`: тот модуль намеренно чистый и в сеть не ходит.
    org_rules = ports.memory().rules(ports.memory().DEVELOP, repo=repo, query=review[:500])
    if org_rules.text:
        task += "\n" + org_rules.text

    await _run_with_heartbeat(_prfix_prepare, repo, pr_number, branch, task,
                              label="prfix:prepare")

    slug = pr_closing.task_slug(repo, pr_number)
    await asyncio.to_thread(_reap_runner, slug)
    command = develop.runner_command(
        slug, image=develop.runner_image(),
        volume=develop.workspace_volume(), mount=develop.workspace_mount(),
        network=develop.proxy_network(), home=_runner_home(slug))
    env = {**os.environ, **develop.runner_env(
        os.environ.get("ZAI_API_KEY", ""), os.environ.get("ZAI_BASE_URL", ""),
        os.environ.get("DEVELOP_MODEL", "").strip() or "openai/glm-4.6")}

    def _run() -> None:
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=develop.run_timeout())
        if result.returncode != 0:
            tail = ((result.stdout or "") + (result.stderr or ""))[-1500:]
            raise RuntimeError(f"круг правок сорвался (код {result.returncode}): {tail}")

    await _run_with_heartbeat(_run, label="prfix:agent")

    _, clone_dir = _prfix_paths(repo, pr_number)
    verdict_path = clone_dir / pr_closing.VERDICT_FILE
    verdict = verdict_path.read_text(encoding="utf-8") if verdict_path.exists() else ""
    # Ни разбор, ни постановка круга не уезжают в коммит: они живут в
    # комментарии PR, а не в коде.
    #
    # Постановка опаснее разбора. Она меняется на КАЖДОМ круге — номер круга,
    # накопленный текст ревью, — поэтому пуш всегда видел дифф и всегда
    # докладывал «правки внесены». Исход «замечаний нет, PR готов к merge»
    # становился недостижим: цикл сжигал все три круга и отдавал PR человеку, а
    # настоящий вердикт агента терялся.
    develop.clear_service_files(clone_dir)

    pushed = await _run_with_heartbeat(
        ports.github().push_fixes, repo, str(clone_dir), branch,
        f"fix(#{pr_number}): правки по замечаниям ревью (круг {round_number})",
        label="prfix:push")
    if not pushed:
        return verdict or ""

    await asyncio.to_thread(
        ports.github().post_comment, repo, pr_number,
        pr_closing.round_comment(pr_number, round_number=round_number,
                                 max_rounds_=pr_closing.max_rounds(), verdict=verdict))
    return True


@activity.defn
async def finish_pr_fixing(repo: str, pr_number: int, rounds: int, settled: bool,
                           verdict: str = "") -> None:
    """Итог доведения: либо PR готов, либо он уходит человеку."""
    if settled:
        # Спрашиваем ФАКТОМ, а не выводим из того, что агент не нашёл предмета
        # для правок: замечание человека агент мог отклонить, а GitHub всё
        # равно не даст слить (R7).
        try:
            blocked = await asyncio.to_thread(
                ports.github().changes_requested, repo, pr_number)
        except Exception as exc:  # noqa: BLE001
            # Проверка не имеет права ронять итог круга (R9).
            activity.logger.warning("состояние ревью не прочитано: %s", exc)
            blocked = None
        await asyncio.to_thread(ports.github().post_comment, repo, pr_number,
                                pr_closing.settled_comment(rounds, verdict, blocked))
        if blocked:
            # Замечания остались — задача в очереди к людям, как и при
            # исчерпании кругов (R8).
            await asyncio.to_thread(ports.github().add_label, repo, pr_number,
                                    pr_closing.NEEDS_HUMAN_PR)
        return
    await asyncio.to_thread(
        ports.github().post_comment, repo, pr_number,
        pr_closing.exhausted_comment(pr_closing.max_rounds(), rounds_done=rounds))
    await asyncio.to_thread(ports.github().add_label, repo, pr_number,
                            pr_closing.NEEDS_HUMAN_PR)
