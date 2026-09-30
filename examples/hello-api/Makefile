.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install fmt lint type imports test cov check run docker clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install everything
	$(UV) sync --all-extras --group dev

fmt: ## Format the codebase
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Lint (no fixes)
	$(UV) run ruff format --check .
	$(UV) run ruff check .

# mypy, pytest and import-linter run through the interpreter, as the hub's Makefile does, not
# through their generated .venv shims: a Windows Application Control policy can refuse those
# shims (`Failed to spawn`) and take `make check` down on a developer box while CI on Linux
# stays green. import-linter has no `__main__`, so its click command is called directly.
# pytest runs with `-P` so the working directory is not put on `sys.path`, which is how CI's
# own test run imports.
type: ## Strict type check
	$(UV) run python -m mypy

imports: ## Enforce the architectural layering contracts
	$(UV) run python -c "from importlinter.cli import lint_imports_command; lint_imports_command()"

test: ## Run the test suite with 100% branch coverage enforced
	$(UV) run python -P -m pytest --cov --cov-report=term-missing

cov: ## Write an HTML coverage report to htmlcov/
	$(UV) run python -P -m pytest --cov --cov-report=html

check: lint type imports test ## Everything CI runs, on one interpreter

run: ## Serve the API on :8090 with reload
	$(UV) run uvicorn hello_api.api.app:create_app --factory --reload --port 8090

# Signed-in gh fetches private client packages; with no session git fetches anonymously.
docker: ## Build the container image
	@GITHUB_TOKEN="$$(gh auth token 2>/dev/null)" docker build --secret id=github_token,env=GITHUB_TOKEN -t hello-api:local .

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
