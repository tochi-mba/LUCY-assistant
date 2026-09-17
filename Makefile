.PHONY: help install fmt lint type imports test cov check run docker clean \
        parity github-ci images up down matrix
.DEFAULT_GOAL := help

UV ?= uv

help: ## Show available targets
	@echo "The hub"
	@echo "  install    Create the virtualenv and install everything"
	@echo "  fmt        Format the codebase"
	@echo "  lint       Lint (no fixes)"
	@echo "  type       Strict type check"
	@echo "  imports    Enforce the architectural layering contracts"
	@echo "  test       Run the suite with 100% branch coverage enforced"
	@echo "  cov        Write an HTML coverage report to htmlcov/"
	@echo "  check      Everything CI runs: lint type imports test"
	@echo "  run        Serve the hub on :8000 with reload"
	@echo "  docker     Build the container image"
	@echo "  clean      Remove caches and build output"
	@echo "The family desk"
	@echo "  parity     Check every service repository against the family standard"
	@echo "  github-ci  Install lucy-assistant family CI on the family repositories"
	@echo "  images     Build every image with your gh sign-in (browser or token)"
	@echo "  up         Build and start the default compose services"
	@echo "  down       Stop the family, keeping its data volumes"
	@echo "             Set COMPOSE_PROFILES=local to include local-only compose services"

install: ## Create the virtualenv and install everything
	$(UV) sync --all-extras --group dev

fmt: ## Format the codebase
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Lint (no fixes)
	$(UV) run ruff format --check .
	$(UV) run ruff check .

type: ## Strict type check
	$(UV) run mypy

imports: ## Enforce the architectural layering contracts
	$(UV) run lint-imports

test: ## Run the suite with 100% branch coverage enforced
	$(UV) run pytest --cov --cov-report=term-missing

cov: ## Write an HTML coverage report to htmlcov/
	$(UV) run pytest --cov --cov-report=html

check: lint type imports test ## Everything CI runs, on one interpreter

matrix: ## The full check on both supported interpreters
	$(UV) run --python 3.12 pytest -q
	$(UV) run --python 3.13 pytest -q

run: ## Serve the hub on :8000 with reload
	$(UV) run uvicorn lucy_api.api.app:create_app --factory --reload --port 8000

# Signed-in gh fetches private client packages; with no session git fetches anonymously.
docker: ## Build the container image
	@GITHUB_TOKEN="$$(gh auth token 2>/dev/null)" docker build --secret id=github_token,env=GITHUB_TOKEN -t lucy-api:local .

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage build dist
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} +

parity: ## Check every service repository against the family standard
	$(UV) run python scripts/parity.py

github-ci: ## Install lucy-assistant family CI on the family repositories
	$(UV) run python scripts/connect_github.py

images: ## Build every image with your gh sign-in
	@GITHUB_TOKEN="$$(gh auth token)" docker compose build

up: images ## Build and start the default compose services
	docker compose up -d --no-build

down: ## Stop the family, keeping its data volumes
	docker compose down
