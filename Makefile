# ---------------------------------------------------------------------------
# ads-research-agent — developer entrypoints.
# `make up` is the only command needed to get a working stack.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps migrate revision psql redis test test-api \
        test-integration guards verify verify-p2 verify-p3 verify-p4 verify-p5a verify-p5b \
        verify-p6 verify-p7 verify-p8 verify-s2p1 eval coverage coverage-calc \
        browser browser-p6 browser-p7 browser-p8 browser-s2p0 browser-s2p6a browser-s2p6b browser-s2p6c browser-documents browser-connections browser-workspaces \
        typecheck lint fmt contracts health clean

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

guards: ## Fail if any route is missing its require(Permission), or arithmetic escaped calc/
	cd $(API) && uv run python scripts/check_route_guards.py
	cd $(API) && uv run python scripts/check_calc_isolation.py

verify: ## Run PRD §19.1's acceptance list against the running stack
	./scripts/verify-p0b.sh

verify-p2: ## Run P2's exit criteria against the running stack
	./scripts/verify-p2.sh

verify-p5a: ## Run P5a's exit criteria against the running stack
	./scripts/verify-p5a.sh
verify-p5b: ## Run P5b's exit criteria against the running stack (needs a live OpenRouter key)
	./scripts/verify-p5b.sh

verify-p3: ## Run P3's exit criteria against the running stack (needs a live OpenRouter key)
	./scripts/verify-p3.sh

verify-p4: ## Run P4's exit criteria against the running stack (needs a live OpenRouter key)
	./scripts/verify-p4.sh

verify-p6: ## Run P6's exit criteria against the running stack
	./scripts/verify-p6.sh

verify-p7: ## Run P7's exit criteria against the running stack
	./scripts/verify-p7.sh

verify-p8: ## Run P8's exit criteria against the running stack
	./scripts/verify-p8.sh

verify-s2p1: ## Run S2-P1's exit criteria (no stack needed; uses it for the dedupe test if up)
	./scripts/verify-s2p1.sh

verify-s2p7: ## Run S2-P7's exit criteria (§21: eval, coverage, the §17 index, staleness)
	./scripts/verify-s2p7.sh

verify-google-ads: ## Prove our own account history against the live Google Ads API
	./scripts/verify-google-ads.sh

verify-webshare: ## Prove the crawl proxy against the live Webshare account (no stack needed)
	uv run --project $(API) python scripts/verify-webshare.py

google-ads-oauth: ## Mint a Google Ads refresh token and list the accounts it reaches
	python3 scripts/google-ads-oauth.py --write-env

browser-autofill: ## Drive "let the agent work it out" on step 1, at 1440 and 390
	@docker compose cp scripts/browser-check-autofill.py worker:/tmp/browser-check-autofill.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
		-e PROJECT_ID="$(PROJECT_ID)" worker \
		/app/.venv/bin/python /tmp/browser-check-autofill.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-s2p7: ## Drive the staleness banner and its re-plan offer, at 1440 and 390
	@docker compose cp apps/api/scripts/plan_payload.py worker:/tmp/plan_payload.py
	@docker compose cp scripts/browser-check-s2p7.py worker:/tmp/browser-check-s2p7.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s2p7.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-s2p6c: ## Drive the Plan Viewer, structure tree, freeze dialog and diff, at 1440 and 390
	@docker compose cp apps/api/scripts/plan_payload.py worker:/tmp/plan_payload.py
	@docker compose cp scripts/browser-check-s2p6c.py worker:/tmp/browser-check-s2p6c.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	# The venv's own Python. Every browser target uses this form; see the
	# note on `browser-s2p0` for what the pinned form did.
	# `Dockerfile` installs browsers for the Playwright in `uv.lock`, which
	# resolves the `>=1.49.0` floor to whatever is current — build 1243 at the
	# time of writing. A pinned 1.49.0 addresses build 1148 and dies with
	# "Executable doesn't exist at /ms-playwright/...".
	#
	# S2-P7 note: this trap had been diagnosed four separate times in this
	# file, each fix local to one target, leaving four spellings of the same
	# invocation and seven targets still on the pinned form that cannot survive
	# an image rebuild. All twelve now use `/app/.venv/bin/python`. A repo-wide
	# Makefile change did not belong in a feature PR; it belongs in hardening,
	# which is this one.
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s2p6c.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-connections: ## Assert the Connections tab asks for nothing, at 1440 and 390
	@docker compose cp scripts/browser-check-connections.py worker:/tmp/browser-check-connections.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-connections.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

eval: ## Run both eval harnesses (10 node fixtures + 5 golden PlanInputs)
	cd $(API) && uv run pytest tests/eval -q

coverage: ## Measure coverage on the packages PRD §15 NF9 names
	cd $(API) && uv run pytest tests -q --ignore=tests/integration \
		--cov=agent.orchestrator --cov=agent.nodes --cov=agent.export --cov=agent.auth \
		--cov=agent.scheduling \
		--cov-report=term-missing:skip-covered --cov-fail-under=80

coverage-calc: ## Stage 02 PQ2: >= 85% on calc/ and planning/, measured on their own
	cd $(API) && uv run pytest tests -q --ignore=tests/integration \
		--cov=agent.calc --cov=agent.planning \
		--cov-report=term-missing:skip-covered --cov-fail-under=85

coverage-plan: ## Stage 02 PQ2: >= 80% on nodes/plan, planning/ and export/, each on its own
	# §17 PQ2 names three floors, not one — and one blended number lets a
	# well-covered package carry a bare one, which is the measurement this
	# gate exists to refuse. Three runs, three floors.
	@for pkg in agent.nodes.plan agent.planning agent.export; do \
		echo "== $$pkg"; \
		(cd $(API) && uv run pytest tests -q --ignore=tests/integration \
			--cov=$$pkg --cov-report=term-missing:skip-covered \
			--cov-fail-under=80) || exit 1; \
	done

browser: ## Render the auth screens in Chromium (desktop + mobile) and assert on them
	@docker compose cp scripts/browser-check-p0b.py worker:/tmp/browser-check.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-p6: ## Drive the P6 screens as an admin and as an operator, at 1440 and 390
	@docker compose cp scripts/browser-check-p6.py worker:/tmp/browser-check-p6.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-p6.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-p8: ## Drive the P8 screens (schedules, storage, compare, banners) at 1440 and 390
	@docker compose cp scripts/browser-check-p8.py worker:/tmp/browser-check-p8.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-p8.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-s2p0: ## Drive the Stage 02 handshake screens (tabs, lock, accept, start) at 1440 and 390
	@docker compose cp scripts/browser-check-s2p0.py worker:/tmp/browser-check-s2p0.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	# The worker's own interpreter. `uv run --with playwright==1.49.0` — what
	# every browser target here used to do — installs a second Playwright that
	# addresses chromium build 1148, while the image installs whatever
	# `uv.lock` resolves (1243). It worked until the image was next rebuilt.
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s2p0.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-s2p6b: ## Drive the budget gate's allocation editor and the forecast figures at 1440 and 390
	@docker compose cp scripts/browser-check-s2p6b.py worker:/tmp/browser-check-s2p6b.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s2p6b.py

browser-s2p6a: ## Drive the Plan Console (rail, Calc tab, stage guard) at 1440 and 390
	@docker compose cp scripts/browser-check-s2p6a.py worker:/tmp/browser-check-s2p6a.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	# The worker's own venv: the image ships the Playwright its browsers were
	# installed for.
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s2p6a.py

browser-documents: ## Upload a real PDF into step 1 and assert on what the screen says
	@docker compose cp scripts/browser-check-documents.py worker:/tmp/browser-check-documents.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-documents.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-workspaces: ## Drive the workspace switcher, the two admin tabs and the refusals, at 1440 and 390
	@docker compose cp scripts/browser-check-workspaces.py worker:/tmp/browser-check-workspaces.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	# The worker's own venv: the image ships the browsers *its* Playwright
	# asks for (chromium-1243).
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-workspaces.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-p7: ## Drive the P7 screens as four roles, at 1440 and 390
	@docker compose cp scripts/browser-check-p7.py worker:/tmp/browser-check-p7.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-p7.py
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
