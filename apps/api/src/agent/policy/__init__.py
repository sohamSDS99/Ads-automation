"""Stage 03's policy surface — the watcher, the classifier and the lifecycle.

Three modules, and the split between them is law 29 made structural:

- `sources.py` reads `policy_sources.yaml`. Configuration, never prompt text.
- `watcher.py` fetches and hashes. It decides *that* something changed and
  never what the change means.
- `classifier.py` proposes what it means. One LLM call, and it proposes only.
- `lifecycle.py` holds §8.6's consequence table. Deterministic code, no model.

The last two are separate files rather than one because the difference between
them is the point: a policy change never silently rewrites a signed rule, and a
consequence the model could reach into is a consequence the model could get
wrong.
"""
