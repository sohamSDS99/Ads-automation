# Eval harness

Ten golden fixtures, one per node that decides something a reader will act on,
each asserted on two axes (PRD §17, P8).

**Schema.** The fixture validates against the node's declared output model, and
the model's own re-serialisation round-trips. This is what catches a node whose
prompt has drifted away from the contract it promises — the failure mode that
cost P5b three whole report sections and raised nothing.

**Groundedness.** PRD §18 law 1: an LLM never sources a fact. Connectors write
`Evidence`; nodes cite `evidence_id`s. Each fixture therefore declares the
evidence its node gathered, and the harness asserts:

* every cited id was gathered — the same rule the executor enforces at runtime
  (`NodeContractError`), pinned here so a prompt change is caught before a run;
* every record that carries findings carries at least one citation (§15 NF6);
* no record cites an id that appears nowhere in the gathered set.

**The negative controls are the point.** `test_the_harness_can_fail` runs
deliberately broken fixtures through the same assertions and requires them to
fail. An eval suite that cannot fail measures nothing, and the version of this
file without those cases passed on the first run while `groundedness()` had a
bug that made it return `[]` for every input.

## Adding a case

Drop a JSON file in `fixtures/` shaped as:

```json
{
  "node_id": "1.1.1",
  "why": "One sentence on what this case is holding.",
  "gathered": ["<uuid>", "..."],
  "output": { ... }
}
```

`node_id` must be registered; the harness reads the output model from the
registry rather than being told, so a renamed model cannot leave a stale fixture
passing against nothing.
