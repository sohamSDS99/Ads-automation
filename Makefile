# ---------------------------------------------------------------------------
# ads-research-agent — developer entrypoints.
# `make up` is the only command needed to get a working stack.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps migrate revision psql redis test test-api \
        test-integration guards verify verify-p2 verify-p3 verify-p4 verify-p5a verify-p5b \
        verify-p6 verify-p7 verify-p8 verify-s2p1 verify-s3p3 verify-s3p5 verify-s3p6 eval coverage coverage-calc \
        browser browser-p6 browser-p7 browser-p8 browser-nav browser-s2p0 browser-s2p6a browser-s2p6b browser-s2p6c browser-s3p8 browser-s4p2 browser-s4p3 browser-s4p18 browser-s4p19 browser-s4p20 browser-s4p21 browser-s4p22 browser-s4p23 browser-documents browser-connections browser-workspaces \
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

guards: ## Fail if a route lost its require(Permission), arithmetic escaped calc/, a verdict escaped guardrails/, or Stage 04's selection stopped being pure
	cd $(API) && uv run python scripts/check_route_guards.py
	cd $(API) && uv run python scripts/check_calc_isolation.py
	cd $(API) && uv run python scripts/check_guardrails_purity.py
	cd $(API) && uv run python scripts/check_creative_purity.py

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

verify-s3p1: ## Run S3-P1's exit criteria (no stack needed; guardrails/ reads nothing)
	./scripts/verify-s3p1.sh

verify-s3p2: ## Run S3-P2's exit criteria (§21: gates, resume, canary, compiling lexicon)
	./scripts/verify-s3p2.sh

verify-s3p3: ## Run S3-P3's exit criteria (§21: H1, the two 403s, the 409, the 401, the licence loop)
	./scripts/verify-s3p3.sh

verify-s3p5: ## Run S3-P5's exit criteria (§21: scoped specs, the ratio, determinism, law 31)
	./scripts/verify-s3p5.sh

verify-s3p6: ## Run S3-P6's exit criteria (§21: the DAG, publish, the six exports, the pin)
	./scripts/verify-s3p6.sh

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

browser-s3p0: ## Drive Stage 03's cold-start entry on a bare project, at 1440 and 390
	@docker compose cp scripts/browser-check-s3p0.py worker:/tmp/browser-check-s3p0.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s3p0.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-s3p8: ## Drive the claims register, the signature ceremony and the S3-P8 screens, at 1440 and 390
	@docker compose cp scripts/browser-check-s3p8.py worker:/tmp/browser-check-s3p8.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-s3p8.py
	@echo "screenshots: docker compose cp worker:/tmp/shots ./shots"

browser-s4p2: ## Drive the 04 rail entry, the Stage 04 landing and Media generation against fixtures, at 1280 and 390, light and dark, with axe (no backend)
	cd $(WEB) && scripts/s4p2/run.sh

browser-s4p3: ## Drive the Start creative dialog against the REAL api on the isolated s4p3 stack (recorded catalogue), at 1280 and 390, light and dark, with axe
	cd $(WEB) && scripts/s4p3/run.sh

browser-s4p18: ## Drive the Creative Console, the brief and G7 against the REAL api + worker on the isolated s4p18 stack, four roles, axe, visual baselines at 1280/390 light/dark
	cd $(WEB) && scripts/s4p18/run.sh

browser-s4p19: ## Drive the Ad Studio against the REAL api on the isolated s4p19 stack: the 400 ms chip, heatmap → preview, keyboard swap, parity test, axe, visual baselines at 1280/390 light/dark
	cd $(WEB) && scripts/s4p19/run.sh

browser-s4p20: ## Drive Extras and the Landing audit against the REAL api + worker on the isolated s4p20 stack: read-only offers + window, the chosen trade-off point, fold overlay vs the capture's pixels on both devices, patch → clipboard, recharts on Extras only, axe, visual baselines at 1280/390 light/dark
	cd $(WEB) && scripts/s4p20/run.sh

browser-s4p21: ## Drive the Media Library against the REAL api + file server on the isolated s4p21 stack: 500 tiles in a perf trace, aspect-true, no master in the grid, regeneration cost before submit, muted proxy with the brand band and a 206, axe, visual baselines at 1280/390 light/dark
	cd $(WEB) && scripts/s4p21/run.sh

browser-s4p22: ## Drive the G8/G8b review workspace and H3 against the REAL api + file server on the isolated s4p22 stack: a keyboard-only review of 20 assets, Approve gated on four ticks, the legal owner's clear with step-up and receipt, absent controls for everyone else, withdraw counts before confirm, axe, visual baselines at 1280/390 light/dark
	cd $(WEB) && scripts/s4p22/run.sh

browser-s4p23: ## Drive QA, Package + release, the canonical released page and the package diff against the REAL api + file server + arq worker on the isolated s4p23 stack: previews at true scale with the server's truncations marked, virtualised server-filtered conformance, an approver releasing v1 by typing it, absent release controls for everyone else, a read-only canonical URL, text + side-by-side media diff, axe, visual baselines at 1280/390 light/dark
	cd $(WEB) && scripts/s4p23/run.sh

# S4-P24 — every Stage 04 harness in sequence (Copy & Creative PRD §17 CC14:
# axe and baselines over the 12 §15.4 screens x light/dark x 390/1280, console
# LCP, lint-preview p95, 500 tiles <= 16 ms/frame, the keyboard-only G8). Each
# stack runs under its own name (s4p24-NN) and subnet (172.31.181-187.0/24),
# never the s4pNN names a live session may hold, and is taken down (`down -v`)
# when its harness ends. The rewrite targets are baked into the build, so ONE
# build serves all eight and every stack publishes the same host ports
# (api 8781, file server 8782, catalogue/openrouter 8783, web 3781) — one at a
# time. Every harness runs; the target fails if any one failed.
S4_STAGE04_ENV := SKIP_BUILD=1 S4_TEARDOWN=1 WEB_PORT=3781 S4_API_PORT=8781 S4_FILES_PORT=8782 \
	S4_AUX_PORT=8783 WORKER_INTERNAL_URL=http://127.0.0.1:8782
.PHONY: browser-stage-04
browser-stage-04: ## Run every Stage 04 browser harness (s4p2, s4p3, s4p18-s4p23) in sequence on isolated s4p24-NN stacks; fails if any fails
	cd $(WEB) && API_INTERNAL_URL=http://127.0.0.1:8781 WORKER_INTERNAL_URL=http://127.0.0.1:8782 NEXT_TELEMETRY_DISABLED=1 pnpm build
	@cd $(WEB) && failed=""; \
	run() { name=$$1; shift; echo "=== browser-stage-04: $$name"; \
	  env $(S4_STAGE04_ENV) SHOTS=/tmp/s4p24-$$name-shots "$$@" || failed="$$failed $$name"; }; \
	run s4p2 env STUB_PORT=8781 scripts/s4p2/run.sh; \
	run s4p3 env S4_PROJECT=s4p24-03 S4_SUBNET=172.31.181.0/24 S4_SUBNET6=fd00:ada:181::/64 scripts/s4p3/run.sh; \
	run s4p18 env S4_PROJECT=s4p24-18 S4_SUBNET=172.31.182.0/24 S4_SUBNET6=fd00:ada:182::/64 scripts/s4p18/run.sh; \
	run s4p19 env S4_PROJECT=s4p24-19 S4_SUBNET=172.31.183.0/24 S4_SUBNET6=fd00:ada:183::/64 scripts/s4p19/run.sh; \
	run s4p20 env S4_PROJECT=s4p24-20 S4_SUBNET=172.31.184.0/24 S4_SUBNET6=fd00:ada:184::/64 scripts/s4p20/run.sh; \
	run s4p21 env S4_PROJECT=s4p24-21 S4_SUBNET=172.31.185.0/24 S4_SUBNET6=fd00:ada:185::/64 scripts/s4p21/run.sh; \
	run s4p22 env S4_PROJECT=s4p24-22 S4_SUBNET=172.31.186.0/24 S4_SUBNET6=fd00:ada:186::/64 scripts/s4p22/run.sh; \
	run s4p23 env S4_PROJECT=s4p24-23 S4_SUBNET=172.31.187.0/24 S4_SUBNET6=fd00:ada:187::/64 scripts/s4p23/run.sh; \
	if [ -n "$$failed" ]; then echo "browser-stage-04: FAILED:$$failed"; exit 1; fi; \
	echo "browser-stage-04: all 8 harnesses passed"

browser-google-connect: ## Drive Connect with Google as an operator, at 1440 and 390
	@docker compose cp scripts/browser-check-google-connect.py worker:/tmp/browser-check-google-connect.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-google-connect.py
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

.PHONY: coverage-creative
coverage-creative: ## Stage 04 CC15: creative/ pure modules and media/ >= 85%; nodes/creative, preview/, export/ >= 80% — each on its own, over unit + integration
	# §17 CC15 names five floors, not one, and one blended number lets a
	# well-covered package carry a bare one — so ONE pytest run records the
	# lines, then one `coverage report` per package holds its own floor. Stage
	# 04's nodes are exercised mostly by the integration suite (a database,
	# Redis, ffmpeg, exiftool, tesseract), so a unit-only figure would misstate
	# them: the run is unit + integration, inside the `test` image, the only
	# place those hostnames and binaries exist. The pure modules are read from
	# check_creative_purity.PURE_MODULES, never copied here. A failing test does
	# not void the figure (the lines it ran still ran; `make test` is the gate
	# on passing), but pytest stopping for any other reason (exit > 1) does.
	# `--continue-on-collection-errors`: two host-only files read the repo root.
	@docker compose ps --status running --format '{{.Service}}' | grep -qx postgres \
		|| { echo "postgres is not running — run 'make up' first"; exit 1; }
	docker compose run --rm test sh -c ' \
		pure=$$(PYTHONPATH=scripts python -c "from check_creative_purity import PURE_MODULES as m; print(*m)"); \
		[ -n "$$pure" ] || { echo "check_creative_purity.PURE_MODULES is empty"; exit 1; }; \
		covs=""; inc=""; \
		for m in $$pure; do covs="$$covs --cov=agent.creative.$${m%.py}"; inc="$$inc,*/agent/creative/$$m"; done; \
		python -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors $$covs \
			--cov=agent.media --cov=agent.nodes.creative --cov=agent.preview --cov=agent.export \
			--cov-report=; \
		rc=$$?; [ $$rc -le 1 ] || { echo "pytest stopped (exit $$rc): nothing measured"; exit $$rc; }; \
		failed=""; \
		measure() { echo "== $$1 (floor $$3%)"; \
			python -m coverage report --include="$$2" --fail-under=$$3 --sort=-miss \
				--skip-covered --show-missing || failed="$$failed $$1"; }; \
		measure creative-pure "$${inc#,}" 85; \
		measure agent.media "*/agent/media/*" 85; \
		measure agent.nodes.creative "*/agent/nodes/creative/*" 80; \
		measure agent.preview "*/agent/preview/*" 80; \
		measure agent.export "*/agent/export/*" 80; \
		[ $$rc -eq 0 ] || echo "note: some tests failed (pytest exit 1); coverage is still what they ran"; \
		[ -z "$$failed" ] || { echo "coverage-creative: under the floor:$$failed"; exit 1; }; \
		echo "coverage-creative: all five floors met"'

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

browser-nav: ## Drive the side panel (stages in it, pages at its foot) at 1440 and 390
	@docker compose cp scripts/browser-check-nav.py worker:/tmp/browser-check-nav.py
	@docker compose exec -T worker mkdir -p /tmp/shots
	docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
		/app/.venv/bin/python /tmp/browser-check-nav.py
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
	cd $(WEB) && pnpm lint

fmt: ## Format the api
	cd $(API) && uv run ruff format .
	cd $(API) && uv run ruff check --fix .

contracts: ## Regenerate packages/contracts from the Pydantic models
	cd $(API) && uv run python scripts/export_schemas.py

health: ## Hit the health endpoint through the web rewrite, exactly as a browser would
	@curl -fsS http://localhost:3000/api/v1/health | python3 -m json.tool
