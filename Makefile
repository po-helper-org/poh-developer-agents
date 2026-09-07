.PHONY: test install

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q
