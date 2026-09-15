# Example experiment output — run `20260914-161711`

A complete, unedited output set from one real run of the full pipeline against the
live X API, committed as a reference for what the system produces. Every file here
is exactly as written by the run — nothing trimmed, reformatted, or synthesized.

The directory layout below mirrors the real `data/` directory exactly, so paths here
are the paths a live run writes to. `data/` itself is gitignored; this snapshot is
frozen so later runs can't overwrite it.

## The run

| | |
|---|---|
| Experiment | `bradshaw_action_count_test` |
| Date | 2026-09-14 |
| Duration | 45m 01s — three 15-minute phases |
| Accounts | `primary` (treatment), `secondary` (control) |
| Outcome | completed cleanly; both agents stopped on their own, process exited 0 |

Phases were purely time-based for this run (`pre_treatment: 15m` etc., no
`max_action_count`), so all three ended on `max_duration`. Treatment arms were
assigned at the end of pre-treatment from seed 42, and the muting intervention
fired one second later against the treatment arm only.

**This is a demonstration run, not a result.** Phases are minutes rather than the
paper's days, both accounts share one authored persona rather than personas
synthesized from real participant data, and there is one account per arm.

## Files

```
data/
  2026-09-14/
    _orchestrator/_experiment/
      run_20260914-161711_reproducibility_log.jsonl   experiment-wide event log
    twitter/
      primary/
        run_20260914-161711.jsonl                     observations
        run_20260914-161711_runtime_log.jsonl         one row per LLM decision
        run_20260914-161711_engagement_log.jsonl      one row per action attempted
        run_20260914-161711_intervention_history.jsonl what the intervention did
      secondary/
        run_20260914-161711.jsonl
        run_20260914-161711_runtime_log.jsonl
        run_20260914-161711_engagement_log.jsonl
  state/
    _experiments/bradshaw_action_count_test/
      orchestrator_state.json                         global experiment state
    primary/twitter_agent_state.json                  per-agent state
    secondary/twitter_agent_state.json
```

Note `secondary` has **no** `intervention_history` — it was the control arm, so no
intervention was ever applied to it. That absence is the evidence of arm isolation.

### `reproducibility_log.jsonl` — start here

The whole experiment in 26 events. Phase transitions (with the reason each one
fired), treatment assignments, the intervention and its per-target results, and
every scheduling decision the orchestrator made. Reading this top to bottom is the
fastest way to understand what happened.

### Observations (`run_<id>.jsonl`)

One row per activation, each carrying `observation_id`, `timestamp_utc`,
`experiment_id`, `agent_id`, `phase`, `treatment_arm`, and the observed
`platform_state` keyed by feed (`home`, `following`).

Because `treatment_arm` is stamped per row, the condition-blind baseline is visible
directly in the data: rows collected during pre-treatment carry `null`.

Each account has **one** observation row here. Both agents were still working
through their first activation — 65 of 108 observed posts each — when the 45 minutes
elapsed. One LLM call per post, two agents sharing a single local model, is roughly
40 seconds per decision. Throughput, not phase length, is what bounded this run.

### `runtime_log.jsonl`

One row per decision, 65 per account. Under `logging: full` each row carries the
complete `constructed_prompt` the model saw, its raw `model_output`, the
`selected_action`, the resolved `target_object`, and the execution result — so any
decision can be re-examined after the fact, and a model error told apart from a bug
in what it was shown.

`no_action` rows carry a `no_action_reason` distinguishing the model genuinely
choosing not to act (`chose`) from a decision rejected because that exact action
already succeeded on that target (`repeat`) or refused by a pacing budget
(`rate_capped`).

### `engagement_log.jsonl`

One row per action *attempted*, regardless of origin — cold start, `follow_all`, an
agent decision, or an orchestrator intervention. Broader than the runtime log on
purpose: it is a complete record of what the account did, not only what the model
chose.

That difference explains a discrepancy worth knowing about: `primary` has 21
engagement rows but only 8 agent actions. The other 13 are the 10 startup follows
and the 3 intervention mutes, neither of which involved a model call.

### `intervention_history.jsonl`

One row, written for the treated account: the action, the source list, the sampling
fraction, the number of targets, and each target's outcome. It records the sampling
*decision*, not just the result — which 70% this particular sockpuppet drew.

### State files

`orchestrator_state.json` holds the experiment's global state: current phase, when
it was entered, the persisted randomization seed, treatment assignments, and
intervention status per account. The seed is read back from here rather than from
config, so changing config later cannot alter an experiment already underway.

The per-agent `twitter_agent_state.json` files hold each account's durable platform
state — who it follows, who it has muted, what it has liked and reposted — plus its
interaction history, the persona it actually ran under, and internal memory.
Persisting the persona means a later edit to the persona file is detectable rather
than silent.

## Reading the files

Every file is newline-delimited JSON, one object per line, appended and never
rewritten. Each row is wrapped in a `metadata` / `timestamp` / `data` envelope.

```bash
head -1 data/2026-09-14/twitter/secondary/run_20260914-161711_runtime_log.jsonl | python -m json.tool
```

```python
import json
rows = [json.loads(l)["data"] for l in open(path, encoding="utf-8") if l.strip()]
```

Every row in every stream carries `phase` and `treatment_arm`, so any of it can be
sliced by experimental condition without joining against another file.
