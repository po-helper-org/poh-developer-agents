"""Точка подключения к харнессу — единственное, что он о стадии знает.

Харнесс на старте воркера вызывает `install(...)`, отдавая реализации портов, и
регистрирует `WORKFLOWS` + `ACTIVITIES` на очереди `TASK_QUEUE`. Больше он о
стадии не знает ничего: ни как устроен прогон агента, ни чем разбирается
красный тест, ни как считается круг правок.

Обратной зависимости нет: `poh_developer` не импортирует из харнесса ничего.

Форма снята с `poh-delivery-agent`, который подключён к тому же харнессу так же
и работает. Изобретать вторую форму подключения для второго модуля значило бы
иметь два способа делать одно.
"""

from poh_developer import activities as _activities, ports
from poh_developer.workflows import PLAN_ACTIVITY, IssueDevelopment, IssuePrFix

# Своя очередь Temporal. Харнесс уже держит три в одном процессе
# (`issue-lifecycle`, `delivery`, `howtodemo`) — четвёртая продолжает практику,
# а не заводит исключение.
TASK_QUEUE = "developer"

# Воркфлоу стадии. Родитель зовёт `IssueDevelopment` дочерним прогоном на этой
# очереди, `IssuePrFix` — круг правок по замечаниям ревью.
WORKFLOWS = [IssueDevelopment, IssuePrFix]

# Все точки входа стадии. Перечислены поимённо, а не собраны обходом модуля:
# activity, потерянная при переезде, иначе проявилась бы не отсутствием в
# списке, а зависшим воркфлоу на живом прогоне.
ACTIVITIES = [
    _activities.dev_begin,
    _activities.dev_prepare,
    _activities.dev_announce,
    _activities.dev_dispatch,
    _activities.trigger_openhands_resolver,
    _activities.dev_run_agent,
    _activities.dev_tests,
    _activities.dev_diagnose,
    _activities.dev_repair,
    _activities.dev_announce_repair,
    _activities.dev_empty_run_reason,
    _activities.dev_followups,
    _activities.dev_publish,
    _activities.dev_publish_partial,
    _activities.capture_episode,
    _activities.run_pr_fix_round,
    _activities.finish_pr_fixing,
]

# Имя активности, которую стадия ждёт ОТ харнесса. Объявлено здесь, чтобы та
# сторона регистрировала её под тем же именем, а не по памяти.
#
# Обратная зависимость ровно одна и того же рода, что у Delivery-Agent: он
# отдаёт конфликтующие ветки активности `delivery_fix_conflicts`, которая и
# есть агент разработки. После переезда стадии эта активность живёт здесь, и
# мост харнесса обязан звать её на очереди `developer`, а не на своей.
CONFLICT_FIX_ACTIVITY = "delivery_fix_conflicts"

# План работ по требованиям остаётся у харнесса и зовётся стадией по имени
# (объявлено в `workflows.py`, здесь переэкспорт для той стороны).
#
# Решение принято по зависимостям, а не по теме: шаг строит план вызовом
# `claude -p` в клоне — той же машинерией, что и стадии анализа, вместе с их
# разбором кредов провайдера и лимита частоты. Забрать его сюда значило бы
# завести вторую копию этой машинерии ради одного вызова, и копия расходилась
# бы с оригиналом ровно там, где меняют провайдера.
#
# Харнесс обязан зарегистрировать её НА ЭТОЙ ЖЕ ОЧЕРЕДИ: воркфлоу ищет
# активность там, где исполняется само, и её отсутствие проявится не отказом
# старта, а зависшим прогоном разработки.
__all__ = ["ACTIVITIES", "CONFLICT_FIX_ACTIVITY", "PLAN_ACTIVITY", "TASK_QUEUE",
           "WORKFLOWS", "install"]


def install(*, github=None, issue_blocks=None, repowise=None,
            memory=None, telemetry=None) -> None:
    """Подставить реализации портов.

    Именованные аргументы, а не словарь: опечатка в имени порта должна быть
    видна на вызове, а не проявиться отказом на первом живом прогоне.
    """
    ports.configure(github=github, issue_blocks=issue_blocks,
                    repowise=repowise, memory=memory, telemetry=telemetry)
