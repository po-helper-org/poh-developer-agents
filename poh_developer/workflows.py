"""Воркфлоу стадии «Разработка»: порядок шагов и решения между ними.

`IssueDevelopment` — дочерний прогон цикла задачи, `IssuePrFix` — круг правок
по замечаниям ревью. Сами шаги живут в `activities.py`; здесь только то, в
каком порядке они идут, что делать с отказом каждого и когда останавливаться.

**Правка решения требует маркера.** Код воркфлоу исполняется не только на новых
задачах: Temporal проигрывает историю идущих прогонов против ТЕКУЩЕГО кода, и
изменённая ветка без `workflow.patched(...)` роняет их недетерминизмом. Роняет
тихо — прогон не падает, он перестаёт выполнять задачи воркфлоу, а снаружи
задача выглядит живой. Так 2026-08-25 встали 29 прогонов из 149.

Гвард на это живёт в `poh-issue-agents` (`tests/test_workflow_replay.py`) и
после переезда продолжает сторожить эти классы: контур импортирует их обратно,
и гвард собирает типы воркфлоу обходом своего модуля. Здесь его копии нет
намеренно — весь корпус фикстур это истории `IssueLifecycle`, снятые со стенда
контура, и проигрывать в этом репозитории нечего. Здесь сторожится то, что от
историй не зависит: перечень маркеров (`tests/test_workflows.py`).

Обратная зависимость ровно одна — план работ по требованиям. Он остался у
контура, потому что строится вызовом `claude -p`, машинерией стадий анализа,
и зовётся ПО ИМЕНИ (`PLAN_ACTIVITY`): ссылки на функцию из чужого репозитория
у пакета нет и быть не должно.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from . import activities as _activities
from .workflow_types import IssueInput

# Активность КОНТУРА, которую зовёт стадия: план работ по требованиям.
#
# Имя, а не ссылка: функция живёт в `poh-issue-agents`, и импортировать её
# отсюда пакет не имеет права (R5). Контур обязан зарегистрировать её на
# очереди стадии — воркфлоу ищет активность там же, где исполняется само.
PLAN_ACTIVITY = "build_mvp_plan"


# Потолок разворота `.cause` в `_failure_reason`. Сегодняшняя цепочка стадии
# разработки — три уровня (ChildWorkflowError → ActivityError →
# ApplicationError), но число — не память самой длинной ожидаемой цепочки, а
# защита от зацикливания: `.cause` — обычный settable-атрибут, и ничто не
# мешает ему сослаться само на себя или на предка. Без потолка такая цепочка
# крутила бы разворот вечно вместо того, чтобы вернуть хоть какую-то причину.
_MAX_CAUSE_UNWRAP = 10


def _failure_reason(e: BaseException) -> str:
    """"ExcType: message" из ПЕРВОПРИЧИНЫ для тегов/группировки Sentry.

    catch-ветки ловят обёртку Temporal, а не исходное исключение activity —
    но глубина обёртки зависит от того, ЧТО сорвалось. Активность внутри
    воркфлоу даёт `ActivityError`; с тех пор как дорогие стадии стали
    дочерними воркфлоу, тот же сбой активности ВНУТРИ ребёнка приходит уже
    как `ChildWorkflowError` поверх `ActivityError`. Разворачиваем `.cause` В
    ЦИКЛЕ, а не один раз: единственный разворот `ChildWorkflowError` даёт
    `ActivityError`, у которого своего `.type` нет — наружу уходило бы одно
    и то же «ActivityError: Activity task failed» на любую причину сбоя
    разработки (выключенный DEVELOP_ENABLED, код 137, отказ пуша, красные
    тесты — всё в одном ведре без текста причины). Останавливаемся на первом
    `ApplicationError` (у него есть `.type` = имя исходного класса) либо на
    исключении без дальнейшей причины. Чистые операции над атрибутами —
    детерминированы, безопасны в workflow-коде.
    """
    cause = getattr(e, "cause", None) or e
    for _ in range(_MAX_CAUSE_UNWRAP):
        exc_type = getattr(cause, "type", None)
        # `.type` — имя класса ТОЛЬКО у ApplicationError. Оно и есть искомая
        # первопричина: дальше разворачивать нечего.
        if isinstance(exc_type, str):
            return f"{exc_type}: {cause}"
        # `.type` есть, но не строка — это TimeoutError: там тем же именем
        # занято перечисление TimeoutType, и его значение число. В Sentry
        # уезжал тег `exc_type: 1` и fingerprint по этой единице
        # (ISSUE-AGENT-B), поэтому число как тип не годится.
        #
        # Но и разворачивать глубже НЕЛЬЗЯ. Temporal кладёт причиной таймаута
        # сбой ПОСЛЕДНЕЙ попытки, и на шаге с тремя попытками «упал один раз,
        # потом встал» доложилось бы тем первым падением: человек в Issue и
        # отпечаток в Sentry указывали бы на ошибку, которая на самом деле
        # была пережита, а настоящая причина — таймаут — исчезала бы.
        if exc_type is not None:
            break
        deeper = getattr(cause, "cause", None)
        if deeper is None:
            break
        cause = deeper
    return f"{type(cause).__name__}: {cause}"


@workflow.defn(name="IssueDevelopment")
class IssueDevelopment:
    """Разработка по подготовленному Issue — дочерний прогон цикла.

    Отдельным воркфлоу, а не активностью, по двум причинам сразу.

    Первая — видимость. Активность внутри родителя не имеет своего
    WorkflowId: в `workflow list` строки нет, а после завершения не остаётся
    и следа — операционная история собиралась логами контейнера и `docker ps`.

    Вторая — ретраи. Одна активность на четыре шага повторялась целиком: на
    прогоне #39 падал только `git push`, уже после работы агента, а заново шёл
    весь прогон, и контур трижды объявил о передаче задачи. Здесь у каждого
    шага своя политика: дорогие и недетерминированные (агент, тесты) идут в
    одну попытку, дешёвые и повторяемые (клон, публикация) — в три.

    Идентификатор фиксирован (`develop-<repo>-<n>`), поэтому повторный запуск
    при идущем прогоне упирается в WorkflowAlreadyStarted, а не поднимает
    второго агента в тот же рабочий каталог.
    """

    @workflow.run
    async def run(self, issue: IssueInput) -> int | None:
        """Возвращает номер PR (`local`) либо None (`dispatch`).

        `None` родитель читает как «работа идёт на чужой стороне, жди события
        `pr-open`», а не как отказ.
        """
        cheap = RetryPolicy(maximum_attempts=3)
        # Одна попытка там, где шаг недетерминирован, идёт десятками минут и
        # стоит денег. Повтор такого инициирует человек, а не политика ретраев.
        once = RetryPolicy(maximum_attempts=1)

        plan = await workflow.execute_activity(
            _activities.dev_begin, issue,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=cheap,
        )

        if plan.mode == "dispatch":
            await workflow.execute_activity(
                _activities.dev_dispatch, args=[issue, plan.branch],
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=cheap,
            )
            return None

        number: int | None = None
        agent_ran = False
        try:
            # Порядок не косметический: сначала клон и постановка — они
            # единственные могут не состояться до того, как что-либо сказано
            # человеку.
            await workflow.execute_activity(
                _activities.dev_prepare, args=[issue, plan.branch],
                start_to_close_timeout=timedelta(seconds=600),
                heartbeat_timeout=timedelta(seconds=300),
                retry_policy=cheap,
            )
            await workflow.execute_activity(
                _activities.dev_announce, args=[issue, plan.branch],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=cheap,
            )
            # MVP: план работ — СТРОГО здесь, между готовым рабочим местом
            # (`dev_prepare` выше уже наполнил `.harness/`) и стартом агента.
            #
            # Не раньше: каталог, который читает и куда пишет `/plan-mvp`,
            # создаёт только `dev_prepare`. Прежняя попытка (Task 9, откачена
            # ревью, revert 80b3291) звала планирование до подготовки —
            # находка K2, «холодный старт»: стадия падала в каталоге,
            # которого никто ещё не создал.
            #
            # Не позже: план — вход агента, а не отчёт по итогам его работы.
            #
            # ПОД МАРКЕРОМ: новая активность — новая команда в истории, и
            # прогоны, начатые до выкладки, обязаны реплеиться прежней
            # последовательностью, без неё.
            #
            # Отказ НЕ роняет прогон: план — необязательный вход агента, а не
            # результат стадии (`PLAN` не входит в `task_context.required()`
            # намеренно) — агент штатно работает без него уже сегодня. Топить
            # дорогой прогон разработки из-за упавшего необязательного шага
            # значило бы разменивать штатный путь на необязательное ускорение.
            if workflow.patched("issue-lifecycle-develop-plan-stage"):
                try:
                    has_plan = await workflow.execute_activity(
                        PLAN_ACTIVITY, args=[issue, plan.branch],
                        start_to_close_timeout=timedelta(seconds=1200),  # claude до 900 + буфер
                        heartbeat_timeout=timedelta(seconds=300),
                        retry_policy=once,
                    )
                except Exception as e:                    # noqa: BLE001
                    workflow.logger.warning(
                        "план работ не построен: %s", _failure_reason(e))
                else:
                    if not has_plan:
                        workflow.logger.warning(
                            "план работ пуст или не создан — агент продолжит без него")
            # Флаг ставится ДО запуска, а не после: агент пишет в рабочее
            # дерево по ходу работы, и упавший на середине оставляет ровно то,
            # ради чего всё это и делается. Ставить после успеха значило бы
            # терять самый интересный для разбора случай.
            #
            # Признак ведём ЯВНЫМ флагом, а не выводим из вида исключения: вид
            # отказа и наличие изменений — разные вещи, и связывать их значит
            # вернуться к тому же дефекту с другой стороны. Пустое дерево
            # отсекает сама выкладка (`publish_worktree` вернёт None).
            agent_ran = True
            await workflow.execute_activity(
                _activities.dev_run_agent, issue,
                start_to_close_timeout=timedelta(seconds=3600),
                heartbeat_timeout=timedelta(seconds=300),
                retry_policy=once,
            )
            # Находки — ДО тестов и публикации: файл находок обязан исчезнуть из
            # рабочего дерева раньше коммита, иначе уедет в PR как мусор, а на
            # следующем круге правок агент прочитает свои прошлые находки как новые.
            await workflow.execute_activity(
                _activities.dev_followups, issue,
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=cheap,
            )
            foreign: list[str] = []
            try:
                await workflow.execute_activity(
                    _activities.dev_tests, issue,
                    start_to_close_timeout=timedelta(seconds=1800),
                    heartbeat_timeout=timedelta(seconds=300),
                    retry_policy=once,
                )
            except Exception as tests_exc:                 # noqa: BLE001
                # Красный прогон — ещё не приговор: тесты могли падать и без
                # правки агента. Ровно это случилось на #166 и #167, где `main`
                # был красный из-за истёкшего промокода, а прогон списали в
                # отказ вместе с работой агента.
                #
                # ПОД МАРКЕРОМ: новые команды в теле воркфлоу роняют
                # недетерминизмом прогоны, начатые до выкладки.
                if not workflow.patched("issue-development-repair-loop"):
                    raise
                # Причина, которая уйдёт наружу, если разобрать не выйдет.
                # Обновляется отказом повторного прогона: человеку нужен
                # свежий список падений, а не доремонтный.
                last_exc: BaseException = tests_exc
                try:
                    diagnosis = await workflow.execute_activity(
                        _activities.dev_diagnose, args=[issue, None],
                        start_to_close_timeout=timedelta(seconds=3900),
                        heartbeat_timeout=timedelta(seconds=300),
                        retry_policy=once,
                    )
                except Exception as diag_exc:              # noqa: BLE001
                    # Диагностика объясняет отказ тестов, а не заменяет его.
                    # Сама активность свои сбои гасит, но отказ ВЫЗОВА (нет
                    # активности на воркере, таймаут, срыв воркера) приходит
                    # уровнем выше — и без этой ветки наружу уходил бы он, а
                    # исходная причина исчезала. Этот класс подмены в контуре
                    # уже случался.
                    workflow.logger.warning(
                        "диагностика красного прогона не состоялась: %s",
                        _failure_reason(diag_exc))
                    raise tests_exc
                if not diagnosis.parsed:
                    # Об исходе не известно ничего — решать по нему нельзя.
                    raise
                # Заходов ровно `plan.repair_rounds` (умолчание 1). Число
                # приходит из активности, а не из окружения: решение воркфлоу
                # обязано быть детерминированным при реплее, и прочитанное
                # прямо здесь `os.environ` дало бы разное значение до и после
                # правки переменной — см. докстринг `DevelopPlan`.
                rounds = 0
                while diagnosis.own and rounds < plan.repair_rounds:
                    rounds += 1
                    await workflow.execute_activity(
                        _activities.dev_announce_repair,
                        args=[issue, diagnosis.own],
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=cheap,
                    )
                    await workflow.execute_activity(
                        _activities.dev_repair, args=[issue, diagnosis.own],
                        start_to_close_timeout=timedelta(seconds=3600),
                        heartbeat_timeout=timedelta(seconds=300),
                        retry_policy=once,
                    )
                    # Повторный прогон НЕ роняет ветку своим отказом: при
                    # чужой красноте он красный всегда, и падение наружу
                    # означало бы, что починку невозможно признать удавшейся
                    # ни в одном репозитории, где набор красен не по вине
                    # агента, — то есть ровно там, ради чего всё это писалось.
                    # Решает диагноз ниже, а не код возврата.
                    try:
                        await workflow.execute_activity(
                            _activities.dev_tests, issue,
                            start_to_close_timeout=timedelta(seconds=1800),
                            heartbeat_timeout=timedelta(seconds=300),
                            retry_policy=once,
                        )
                    except Exception as retry_exc:         # noqa: BLE001
                        # Наружу пойдёт СВЕЖИЙ отказ, а не доремонтный:
                        # прежний перечисляет падения, часть которых уже
                        # починена, и человек читал бы неправду.
                        last_exc = retry_exc
                    # База та же: базовый коммит не менялся, а лишний прогон
                    # набора стоит времени. Мигание не перепроверяем — эти
                    # тесты уже подтверждены дважды.
                    try:
                        diagnosis = await workflow.execute_activity(
                            _activities.dev_diagnose,
                            args=[issue, diagnosis.baseline],
                            start_to_close_timeout=timedelta(seconds=1900),
                            heartbeat_timeout=timedelta(seconds=300),
                            retry_policy=once,
                        )
                    except Exception as diag_exc:          # noqa: BLE001
                        workflow.logger.warning(
                            "диагностика после починки не состоялась: %s",
                            _failure_reason(diag_exc))
                        raise last_exc
                    if not diagnosis.parsed:
                        # Об исходе повторного прогона не известно ничего.
                        raise last_exc
                if diagnosis.own:
                    # Заходы кончились, свои падения остались — человек.
                    workflow.logger.warning(
                        "починка не удалась, осталось своих падений: %s",
                        len(diagnosis.own))
                    raise last_exc
                foreign = diagnosis.foreign
            number = await workflow.execute_activity(
                _activities.dev_publish, args=[issue, plan.branch, foreign],
                start_to_close_timeout=timedelta(seconds=600),
                heartbeat_timeout=timedelta(seconds=300),
                retry_policy=cheap,
            )
        except Exception as exc:                          # noqa: BLE001
            # Сорванный прогон обязан оставить материал для разбора.
            #
            # Отказ, ради которого написано: на #166 упали три теста из
            # семидесяти трёх, и тринадцать минут работы агента исчезли без
            # следа — `dev_publish` идёт после `dev_tests` и не выполнился.
            #
            # ПОД МАРКЕРОМ: новая команда в теле воркфлоу роняет
            # недетерминизмом прогоны, начатые до выкладки, а прогон агента
            # идёт до 45 минут — реплей убил бы ровно ту работу, которую этот
            # код спасает.
            if agent_ran and workflow.patched("issue-development-partial-publish"):
                try:
                    await workflow.execute_activity(
                        _activities.dev_publish_partial,
                        args=[issue, plan.branch, _failure_reason(exc)[:1500]],
                        start_to_close_timeout=timedelta(seconds=600),
                        heartbeat_timeout=timedelta(seconds=300),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                except Exception as save_exc:              # noqa: BLE001
                    # Спасение НЕ подменяет причину: наружу уходит исходное
                    # исключение, а неудача самой выкладки только пишется в
                    # лог. Иначе первопричина исчезает — этот класс подмены в
                    # контуре уже случался.
                    workflow.logger.warning(
                        "частичная выкладка не удалась: %s",
                        _failure_reason(save_exc))
            raise
        finally:
            # Запись об итерации — В FINALLY, а не после успешных шагов.
            #
            # Красные тесты и сорвавшийся прогон агента — самые интересные для
            # разбора исходы, и именно они пропускали запись: исключение из
            # шага уносило управление мимо неё. Слой собирал статистику только
            # по удачам и на ней же учился.
            #
            # ПОД МАРКЕРОМ: новая команда в теле воркфлоу роняет
            # недетерминизмом прогоны, начатые до выкладки, а прогон агента
            # идёт до 45 минут. Прецедент в этом же файле — реплей без маркера
            # падает `Timer machine does not handle ActivityTaskScheduled`.
            if workflow.patched("issue-lifecycle-capture-episode-always"):
                try:
                    await workflow.execute_activity(
                        _activities.capture_episode,
                        args=[issue, plan.branch, number],
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=cheap,
                    )
                except Exception as e:                   # noqa: BLE001
                    # Слой опционален и не имеет права стоить прогона — тем
                    # более уже упавшего, где запись лишь пояснение к отказу.
                    workflow.logger.warning(
                        "запись об итерации не отдана слою памяти: %s",
                        _failure_reason(e))

        if number is None:
            reason = "агент не изменил ни одного файла — открывать нечего"
            if workflow.patched("issue-lifecycle-empty-run-diagnosis"):
                # Прежнее сообщение обвиняло агента в бездействии даже тогда,
                # когда он не сделал ни одного хода — то есть когда отказало
                # окружение. Человек шёл разбирать постановку вместо
                # инфраструктуры. Признак лежит на диске, поэтому спрашиваем
                # активность: воркфлоу файловой системы не видит.
                #
                # Уточнение НЕ ИМЕЕТ ПРАВА подменить собой исходный отказ:
                # диагностика, способная сломать то, что диагностирует, хуже
                # её отсутствия. Не вышло — докладываем прежним текстом.
                try:
                    reason = await workflow.execute_activity(
                        _activities.dev_empty_run_reason,
                        args=[issue],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=cheap,
                    )
                except Exception as e:                   # noqa: BLE001
                    workflow.logger.warning(
                        "причину пустого прогона выяснить не удалось: %s",
                        _failure_reason(e))
            raise ApplicationError(reason)
        return number


@workflow.defn(name="IssuePrFix")
class IssuePrFix:
    """Один круг правок по замечаниям ревью — дочерний прогон цикла.

    Отдельный воркфлоу на КАЖДЫЙ круг, а не на цикл целиком: круги разделены
    ожиданием внешнего доклада ревью, и объединение их в один прогон дало бы
    воркфлоу, большую часть жизни простаивающий в ожидании чужого сигнала.
    Ожиданием по-прежнему управляет родитель — он владеет состоянием задачи.
    """

    @workflow.run
    async def run(self, repo: str, pr_number: int, round_number: int) -> bool | str:
        """`True` — правки внесены и запрошена перепроверка. Строка — правок не
        потребовалось, и это её разбор.

        Разные типы возврата намеренно: «сделали» и «не потребовалось» — разные
        исходы, и сводить их к булеву значению значило бы потерять объяснение.
        """
        return await workflow.execute_activity(
            _activities.run_pr_fix_round,
            args=[repo, pr_number, round_number],
            start_to_close_timeout=timedelta(seconds=3600),
            heartbeat_timeout=timedelta(seconds=300),
            # Круг недетерминирован и стоит денег: повтор инициирует следующая
            # итерация родителя, а не политика ретраев.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
