.PHONY: help test parity github-ci images up down
.DEFAULT_GOAL := help

help: ## Show family commands
	@echo "test       Run the family tooling and configuration tests"
	@echo "parity     Check all service repositories against the family standard"
	@echo "images     Build all images with your GitHub browser login"
	@echo "github-ci  Copy that login into Actions (no PAT)"
	@echo "up         Build and start all eight services (requires .env.family)"
	@echo "down       Stop the family, keeping its data volumes"

test:
	uv run --with pytest --with pyyaml pytest tests -q

parity:
	python scripts/parity.py

github-ci:
	python scripts/share_github.py

images:
	@GITHUB_TOKEN="$$(gh auth token)" docker compose build

up: images
	docker compose up -d --no-build

down:
	docker compose down
