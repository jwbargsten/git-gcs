.PHONY: help sync test lint fmt clean dist

# unset venv: prevent uv from complaining about already being in a virtual env
# (uv has its own .venv folder)
VIRTUAL_ENV:=

help: ## this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9._-]+:.*?## / {printf "\033[1m\033[36m%-38s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## sync project with uv
	uv sync

test: ## run tests
	uv run pytest tests/

lint: ## lint the source code
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

fmt: ## format the source code with ruff
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

clean: ## delete all pycache, .pyc, .pyo and .egg-info files
	-rm -rf ./src/git_remote_gcs.egg-info dist/ build/
	find . -type f -name '*.py[co]' -delete -o -type d -name __pycache__ -delete

dist: lint test clean ## build sdist and wheel into dist/
	uv build
