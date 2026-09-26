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

## S4-P24 — hardening (branch `feat/s4-p24`) — complete

Worktree `~/ads-s4-p24` on main@c4cab8e7. The stack is `docker compose -p s4p24`
(postgres + redis, subnet 172.31.124.0/24, no host ports). Tests ran in the
worker test image (`s4p24-wtest`, a re-tag of `s4p16-wtest`).

| Deliverable | Proven by |
|---|---|
| 1. 3 golden CreativeInput fixtures + request-keyed cassettes (CC16) | `tests/integration/test_s4p24_golden.py`; `tests/cassettes/creative/*.json` |
| CC9, two-process determinism | `tests/integration/test_s4p24_determinism.py` (+ the `s4p24_pinned_inputs` plugin) |
| 2. 4-role × mutating-route authz matrix (CC10) | `tests/integration/test_s4p24_authz_matrix.py`, `test_authz_matrix.py` |
| 3. Canaries (CC11) | `tests/integration/test_s4p24_canaries.py` |
| 4. Real `kill -9` suite (CC5, CC12) | `tests/integration/test_s4p24_kill9.py` |
| 5. Cost caps (CC2), estimate accuracy (CC3), wall clock (CC1): **all three NOT MET**, each a strict xfail plus a ratchet | `test_s4p24_cc2_media_caps.py`, `test_s4p24_cost_and_time.py`, `tests/test_s4p24_estimate_accuracy.py`, `tests/creative/test_cc1_copy_track_structure.py` |
| 6. Coverage gates (CC15), CC6 bytes, CC7 real files | `make coverage-creative`; `tests/postprod/test_image_bytes_property.py`, `test_video_verify_gates.py` |
| 7. Visual + axe over 12 screens × 2 themes × 2 widths, LCP, lint p95, keyboard-only G8 (CC14) | `make browser-stage-04` (915 checks) |
| 8. Runbook (§17 + every §18 failure mode) and the §17 index | `docs/stage-04.md`, `tests/test_nfr_stage_04.py` |
| Phase gate | `docs/gates/phase-4.md`: **NO GO**, with the path to GO |

Rulings owed: `docs/stage-04-questions.md` § S4-P24 (9 items).
