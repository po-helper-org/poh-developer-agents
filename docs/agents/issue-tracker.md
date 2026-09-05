# Трекер задач: GitHub

Issue и спеки этого репозитория живут как GitHub Issues в
`po-helper-org/poh-developer-agents`. Тот же трекер у всего контура — цепочка
`Closes #N` сквозная, и отдельный трекер на один репозиторий её бы разорвал.

## Операции

Штатный инструмент — `gh` CLI; репозиторий он определяет сам, если запущен
внутри клона.

- **Завести**: `gh issue create --title "..." --body "..."` (многострочное тело — heredoc)
- **Прочитать**: `gh issue view <n> --comments`
- **Список**: `gh issue list --state open --json number,title,body,labels,comments`
- **Комментарий**: `gh issue comment <n> --body "..."`
- **Метки**: `gh issue edit <n> --add-label "..."` / `--remove-label "..."`
- **Закрыть**: `gh issue close <n> --comment "..."`

## Среды без `gh`

Часть агентских сред работает без `gh` CLI и без прямого доступа к GitHub API —
там те же операции идут через MCP-сервер GitHub. Соответствие:

| Операция | `gh` | MCP |
|---|---|---|
| завести issue | `gh issue create` | `mcp__github__issue_write` |
| прочитать issue | `gh issue view` | `mcp__github__issue_read` |
| список | `gh issue list` | `mcp__github__list_issues` |
| комментарий | `gh issue comment` | `mcp__github__add_issue_comment` |
| поиск | `gh search issues` | `mcp__github__search_issues` |
| PR: прочитать | `gh pr view` | `mcp__github__pull_request_read` |
| PR: завести | `gh pr create` | `mcp__github__create_pull_request` |

Проверьте, что доступно, и берите то, что есть. Отсутствие `gh` — не повод
сказать, что операция невозможна.

## PR как поверхность заявок

**PR как поверхность заявок: нет.** _(Поставьте «да», если внешние PR этого
репозитория считаются заявками на доработку; флаг читает `/triage`.)_

GitHub делит одно пространство номеров между issue и PR, поэтому голый `#42`
может быть и тем и другим: разрешайте через `gh pr view 42` с откатом на
`gh issue view 42`.

## Когда скилл говорит «опубликовать в трекер»

Завести GitHub Issue.

## Когда скилл говорит «взять релевантный тикет»

`gh issue view <n> --comments`.

## Операции wayfinding

Используются `/wayfinder`. **Карта** — одна issue с **дочерними** issue-тикетами.

- **Карта**: issue с меткой `wayfinder:map`, в теле — Notes / Decisions-so-far / Fog.
- **Дочерний тикет**: sub-issue карты (`gh api` по эндпоинту sub-issues). Где
  sub-issues недоступны — задача в task-list карты плюс `Part of #<map>` первой
  строкой тела. Метки: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`).
- **Блокировки**: нативные issue dependencies GitHub.
  `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<db-id>`,
  где `<db-id>` — числовой **database id** блокирующей issue
  (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, **не** `#number` и не `node_id`).
  Где недоступны — строка `Blocked by: #<n>, #<n>` в начале тела.
- **Фронтир**: открытые дети карты, у которых нет открытых блокировщиков и нет
  исполнителя; первый в порядке карты выигрывает.
- **Взять**: `gh issue edit <n> --add-assignee @me` — первая запись сессии.
- **Закрыть**: комментарий с ответом, `gh issue close`, затем указатель на
  контекст в Decisions-so-far карты.

## ⚠️ Метки этого трекера исполняемые

Контур читает метки как команды, а не как пометки: `ready-for-dev` запускает
агента разработки, `agents:off` снимает задачу с обработки целиком. Перед
любой простановкой меток — [`triage-labels.md`](triage-labels.md).
