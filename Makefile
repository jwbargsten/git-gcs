.PHONY: help sync test test-integration fake-gcs lint fmt clean dist

# unset venv: prevent uv from complaining about already being in a virtual env
# (uv has its own .venv folder)
VIRTUAL_ENV:=

help: ## this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9._-]+:.*?## / {printf "\033[1m\033[36m%-38s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## sync project with uv
	uv sync

test: ## run tests, integration tests are skipped without a fake GCS server
	uv run pytest tests/

test-integration: ## run all tests against the fake GCS server started with `make fake-gcs`
	STORAGE_EMULATOR_HOST=http://localhost:4443 uv run pytest tests/

fake-gcs: ## start an in-memory fake GCS server on port 4443 (brew install fake-gcs-server)
	# TZ=UTC: fake-gcs-server uses the local offset in timestamps, google-cloud-storage only parses "Z"
	TZ=UTC fake-gcs-server -scheme http -port 4443 -backend memory -public-host localhost:4443

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
