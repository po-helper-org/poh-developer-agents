"""Стадия «Разработка» контура производства — чистые модули.

Ни сети, ни Temporal, ни GitHub: здесь только то, что можно вынуть из сервиса,
не разбирая его. Оркестрация (активности, воркфлоу, клиенты провайдеров)
принадлежит тому, кто держит воркер, — сегодня это `poh-issue-agents`.

Разбор стадии целиком — `AUDIT.md`, контракт — `docs/stage-contract.md`.
"""

__all__ = ["develop", "integration", "ports", "pr_closing", "runner",
           "task_context", "test_report", "worktree", "workflow_types"]
