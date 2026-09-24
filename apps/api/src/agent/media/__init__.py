"""Stage 04's media gateway: pixels over OpenRouter (PRD §9.1).

Stage 01 has `llm/` for text; this is the same discipline for images and
video. Model ids are runtime data the user chose (Law 36), every request is
checked against the capability record it was chosen from before any spend,
and a video is submitted once (Law 37).

Deliberately empty: `calc/` imports `agent.media.types` and
`agent.media.capability`, which are pure, and importing the package must not
drag HTTP or Redis in behind them.
"""
