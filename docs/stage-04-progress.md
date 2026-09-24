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
