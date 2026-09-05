.PHONY: test drift install

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q

# Сверка копий с poh-issue-agents@main. Пока копий две, расхождение
# обязано находиться прогоном — см. docs/extraction-plan.md.
drift:
	./scripts/drift.sh
