# Stage 04 — progress

## S4-P1 — media gateway (branch `feat/s4-p1`)

All eleven §23.1 deliverables are built and committed, one commit each
(`S4-P1: …`); fixtures → capability → catalogue → images → videos →
budget → jobs → calc → routes + eligibility.

| §23.1 acceptance | Where it is proven |
|---|---|
| pytest media/ calc/ green; coverage on media/ ≥ 85 % | `tests/media/`, `tests/test_calc_media.py`, `tests/integration/test_media_*.py` |
| `test_video_submit_once_under_crash` at all five points | `tests/integration/test_media_jobs.py` |
| unsupported `aspect_ratio` ⇒ 422 `capability_unsupported`, zero requests reach the mock | `test_media_routes.py::test_an_unsupported_aspect_ratio_is_our_422_and_no_request_reaches_the_mock` |
| default video model not allowlisted ⇒ `media_model_not_allowlisted` naming video | `test_media_routes.py::test_a_default_video_model_off_the_allowlist_blocks_naming_video` |
| estimate returns the breakdown and writes no `GenerationJob` | `test_media_routes.py::test_the_estimate_returns_its_breakdown_and_writes_no_generation_job` |
| admin PUTs an allowlist; operator 403 | `test_media_routes.py::test_an_admin_can_put_an_allowlist_and_an_operator_gets_403` |
| `check_route_guards.py` passes; `mypy src/agent` exits 0 | run locally, by exit code |

Open rulings: `docs/stage-04-questions.md` § S4-P1. Next phase: S4-P2
(frontend foundations) or S4-P3 (start dialog, needs P1 + P2).

## S4-P24 — hardening (branch `feat/s4-p24`) — checkpoint

Worktree `~/ads-s4-p24` on main@c4cab8e7. The stack is `docker compose -p s4p24`
(postgres + redis, subnet 172.31.124.0/24, no host ports). Tests run in the
worker test image (`s4p24-wtest`, a re-tag of `s4p16-wtest`), each agent on its
own database and Redis index.

| Deliverable | State | Commit |
|---|---|---|
| 1. 3 golden CreativeInput fixtures + request-keyed cassettes (CC16) | committed; each run replays green twice | `S4-P24: golden CreativeInput fixtures + cassettes (CC16)` |
| CC9, two-process determinism | committed; A = B byte-identical, C relabelled-equal, all 3 fixtures | `S4-P24: determinism across two processes (CC9)` |
| 2. 4-role × mutating-route authz matrix (CC10) | committed; 104 cells, and the matrix that was red on main is now green | `S4-P24: 4-role x every-mutating-route authz matrix (CC10)` |
| 3. Canaries (CC11) | committed; mutation-checked | `S4-P24: canaries through the full-slate golden run (CC11)` |
| 4. Real `kill -9` suite (CC5, CC12) + 4 product fixes | built, 17/17 ×2; verifying | — |
| 5. Cost caps (CC2), estimate accuracy (CC3), wall-clock bound (CC1) | in progress | — |
| 6. Coverage gates (CC15), CC6 bytes, CC7 real files | in progress | — |
| 7. Visual + axe over 12 screens × 2 themes × 2 widths, LCP, lint p95, keyboard-only G8 guard (CC14) | in progress | — |
| 8. `docs/stage-04.md` runbook (§18) + the §17 index | in progress | — |

Product defects found and fixed so far:
- Determinism (CC9): 4.3.1 and 4.3.3 evidence order; package asset, media,
  file, job, audit and exception order; AssetDecision's clock.
- Crash recovery (CC5, CC12): a stuck budget reservation; 4.4.2 could not
  resume past a chosen master; a reaped run kept its lock; runs that never
  started were never reaped.
