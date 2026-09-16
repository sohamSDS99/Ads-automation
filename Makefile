# ---------------------------------------------------------------------------
# ads-research-agent — developer entrypoints.
# `make up` is the only command needed to get a working stack.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps migrate revision psql redis test test-api \
        typecheck lint fmt contracts health clean

API := apps/api
WEB := apps/web

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the whole stack (postgres, redis, api, worker, web) and migrate
	docker compose up -d --build
	@echo "waiting for api…"
	@until docker compose exec -T api python -c "import socket,sys; s=socket.socket(socket.AF_INET6); sys.exit(s.connect_ex(('::1',8000)))" 2>/dev/null; do sleep 1; done
	$(MAKE) migrate
	@echo "→ http://localhost:3000"

down: ## Stop the stack (keeps volumes)
	docker compose down

clean: ## Stop the stack and delete its volumes — destroys local data
	docker compose down -v

restart: ## Recreate the app services
	docker compose up -d --build api worker web

ps: ## Show service status
	docker compose ps

logs: ## Tail all logs
	docker compose logs -f --tail=100

migrate: ## Apply migrations (mirrors Railway's preDeployCommand)
	docker compose exec -T api alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add widgets"
	docker compose exec -T api alembic revision --autogenerate -m "$(m)"

psql: ## Open a psql shell
	docker compose exec postgres psql -U agent -d agent

redis: ## Open a redis shell
	docker compose exec redis redis-cli

test: test-api typecheck ## Run every check

test-api: ## Run the api test suite
	cd $(API) && uv run pytest -q

typecheck: ## mypy (api) + tsc (web)
	cd $(API) && uv run mypy
	cd $(WEB) && pnpm exec tsc --noEmit

lint: ## ruff (api) + eslint (web)
	cd $(API) && uv run ruff check .
	cd $(API) && uv run ruff format --check .
	cd $(WEB) && pnpm exec eslint .

fmt: ## Format the api
	cd $(API) && uv run ruff format .
	cd $(API) && uv run ruff check --fix .

contracts: ## Regenerate packages/contracts from the Pydantic models
	cd $(API) && uv run python scripts/export_schemas.py

health: ## Hit the health endpoint through the web rewrite, exactly as a browser would
	@curl -fsS http://localhost:3000/api/v1/health | python3 -m json.tool
