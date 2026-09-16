# ---------------------------------------------------------------------------
# ads-research-agent — developer entrypoints.
# `make up` is the only command needed to get a working stack.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps migrate revision psql redis test test-api \
        test-integration guards verify verify-p2 verify-p3 verify-p4 verify-p5a verify-p6 \
        browser browser-p6 typecheck lint fmt contracts health clean

API := apps/api
WEB := apps/web

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
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

test: test-api test-integration guards typecheck ## Run every check

test-api: ## Run the api unit suite (no database needed)
	cd $(API) && uv run pytest tests -q --ignore=tests/integration

test-integration: ## Run the DB+Redis suite inside the compose network
	@docker compose ps --status running --format '{{.Service}}' | grep -qx postgres \
		|| { echo "postgres is not running — run 'make up' first"; exit 1; }
	# Not `uv run pytest` on the host: postgres, redis and api publish no host
	# port (PRD §5.2), so those hostnames only resolve inside the network. The
	# `test` service's own command is `pytest tests/integration -q`.
	docker compose run --rm test

guards: ## Fail if any route is missing its require(Permission)
	cd $(API) && uv run python scripts/check_route_guards.py

verify: ## Run PRD §19.1's acceptance list against the running stack
	./scripts/verify-p0b.sh

verify-p2: ## Run P2's exit criteria against the running stack
	./scripts/verify-p2.sh

verify-p5a: ## Run P5a's exit criteria against the running stack
	./scripts/verify-p5a.sh
verify-p3: ## Run P3's exit criteria against the running stack (needs a live OpenRouter key)
	./scripts/verify-p3.sh

verify-p4: ## Run P4's exit criteria against the running stack (needs a live OpenRouter key)
	./scripts/verify-p4.sh

verify-p6: ## Run P6's exit criteria against the running stack
	./scripts/verify-p6.sh

browser: ## Render the auth screens in Chromium (desktop + mobile) and assert on them
	@docker compose cp scripts/browser-check-p0b.py worker:/tmp/browser-check.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		uv run --no-project --with playwright==1.49.0 python /tmp/browser-check.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-p6: ## Drive the P6 screens as an admin and as an operator, at 1440 and 390
	@docker compose cp scripts/browser-check-p6.py worker:/tmp/browser-check-p6.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		uv run --no-project --with playwright==1.49.0 python /tmp/browser-check-p6.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

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
