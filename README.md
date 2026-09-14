# ScraperSky
X/Twitter account orchestration and scraping system.

## Requirements
- [Docker Engine](https://docs.docker.com/engine/install)
- [Ollama](https://ollama.com/download) with the [`qwen2.5:14b`](https://ollama.com/library/qwen2.5:14b) model pulled — only needed for accounts running the **Agent Runtime** (`sockpuppet_config.persona_prompt`, see below); not required otherwise.

## Installation
1. Clone repo:
```bash
git clone https://github.com/your-username/ScraperSky.git
cd ScraperSky
```
2. Create env and accounts files:
```bash
cp .env.example .env
cp accounts.yaml.example accounts.yaml
```
3. Open `accounts.yaml` and fill in credentials for each account you want to run (see below).
4. Review `config.yaml` and adjust `data_collection.targets`/`platform`/etc. as needed (defaults work out of the box; per-account overrides live in `accounts.yaml`).
5. **Only if running the Agent Runtime** (any account has `sockpuppet_config.persona_prompt` set): install [Ollama](https://ollama.com/download), pull the model, and start the server:
```bash
ollama pull qwen2.5:14b
ollama serve
```
   Running via `docker compose up` needs `OLLAMA_BASE_URL` set in `.env` so the container can reach Ollama on your host — see `.env.example` and the Agent Runtime section below.

## Credentials

ScraperSky can run multiple accounts at once, each with its own credentials. Every account needs its own **auth_token**, **bearer_token**, and **csrf_token**, entered in `accounts.yaml` (not `.env` — see Configuration below).

To get these for a given account:

1. Log into that Twitter account on desktop and go to the Home page (X's default algorithmic feed).
2. Open browser DevTools and go to the **Network** tab.

<img width="1206" height="470" alt="image" src="https://github.com/user-attachments/assets/e7a4ae50-d7ae-4bfe-85c6-92c66dbf5496" />

3. Find any request titled **user_flow.json** (scroll the Twitter page a little if none appear).
4. In the request, scroll to the **Request Headers** section:
   - Set `bearer_token` to the value of **Authorization**
   - Set `auth_token` to the **auth_token** field inside **Cookie**
   - Set `csrf_token` to the **ct0** field inside **Cookie**

Repeat for each additional account, adding one entry per account under `accounts:` in `accounts.yaml`.

> **Note:** Credentials expire when that account logs out or Twitter rotates them. If you get 403 errors for one account, extract fresh credentials for it and update its entry in `accounts.yaml`.

## Configuration

Settings are split across three files:
- **`.env`** — Docker volume paths (machine/deployment-specific, not experiment behavior). No secrets live here anymore.
- **`accounts.yaml`** — secrets: one entry per account (credentials + optional per-account behavioral overrides). Gitignored, never commit — see `accounts.yaml.example` for the template.
- **`config.yaml`** — default behavior shared by every account. Non-secret and safe to share/commit.

### `.env`

| Variable | Description | Default |
|---|---|---|
| `HOST_OUTPUT_DIR` | Output folder on the host machine | `./data` |
| `CONTAINER_OUTPUT_DIR` | Output folder inside the container | `/app/data` |
| `OUTPUT_DIR` | Output folder as seen by the app itself (matches `CONTAINER_OUTPUT_DIR` when run via Docker) | `/app/data` |
| `CONFIG_PATH` | Path to `config.yaml` (see below) | `config.yaml` |
| `ACCOUNTS_PATH` | Path to `accounts.yaml` (see below) | `accounts.yaml` |

### `accounts.yaml`

```yaml
accounts:
  - name: primary
    auth_token: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    bearer_token: "Bearer xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    csrf_token: xxxxxxxxxxxxxxxxxxxxxxxx
    # Optional -- every one of config.yaml's top-level keys can be
    # overridden per-account. Anything omitted here falls back to the
    # matching top-level key in config.yaml.
    data_collection:
      targets:
        - home
    actions:
      follow_all: true
    account_lists:
      follow_list: follow_user_ids.json

  - name: secondary
    auth_token: ...
    bearer_token: ...
    csrf_token: ...
    scroll_delay: 4   # scalar keys are overridable too, not just dict-valued ones
    timezone: UTC
    data_collection:
      targets:
        - following
```

Each account entry needs `name` (used to namespace its output/state, and shown in its console log lines), plus `auth_token`/`bearer_token`/`csrf_token`. Every other key — `platform`, `scroll_delay`, `fetch_max_retries`, `fetch_retry_backoff`, `timezone`, `data_collection`, `actions`, `account_lists`, `sockpuppet_config`, `experiment_design` — is an optional per-account override of the matching top-level key in `config.yaml`. Dict-valued keys (`data_collection`/`actions`/`account_lists`) merge shallowly — e.g. an account can override just `data_collection.targets` and still inherit `data_collection.search_query` from `config.yaml`. Scalar keys (`scroll_delay`, etc.) simply replace the default outright for that account.

Every account runs concurrently (one thread each), with its own `httpx` client, x-client-transaction-id generator, agent-state file (`data/state/<name>/twitter_agent_state.json`), and output namespace (`data/<date>/twitter/<name>/`) — so accounts never collide with each other's follow/mute bookkeeping or JSONL output.

### `config.yaml`

Every key below is a **default** that applies to any account that doesn't override it in `accounts.yaml` (see above).

| Key | Description | Default |
|---|---|---|
| `platform` | Which scraper implementation to use. Currently only `twitter` | *(required, here or per-account)* |
| `data_collection.targets` | What to collect. **Observation feeds** — `home` (X's algorithmic default feed — the "Home" tab, GraphQL `HomeTimeline`), `following` (X's reverse-chronological feed — the "Following" tab, GraphQL `HomeLatestTimeline`), `search` (keyword search); several may be listed. Plus two **derived data streams** — `engagement_log` (every action this account performs) and `intervention_history` (what the intervention did to it). `home` and `following` are easy to mix up by name alone; see the naming key comment near the top of `src/scraper/twitter.py` if in doubt | *(required, here or per-account)* |
| `data_collection.search_query` | Query text for the `search` target ("All of these words" — plain keywords, no query operators yet). Required (here or per-account) if `search` is a target | *(required if using `search`)* |
| `data_collection.observation_frequency` | How often platform state is observed. Only `every_visit` (one observation per activation) is implemented | `every_visit` |
| `actions.follow_all` | Whether to run `follow_all()` at startup, following every account in the `follow_list` account list | `false` |
| `scroll_delay` | Seconds between timeline requests. Minimum of 2 recommended | `2` |
| `fetch_max_retries` | Retries for a single cursor before giving up and restarting the timeline from the top | `5` |
| `fetch_retry_backoff` | Base seconds for retry backoff (multiplied by attempt number) | `5` |
| `empty_batch_backoff_base` | Base seconds for the all-duplicates backoff on `home`/`search` (roughly doubles each consecutive batch with zero new tweets, reset once a batch has any new tweet) | `5` |
| `empty_batch_backoff_max` | Cap in seconds for that backoff | `300` |
| `empty_batch_backoff_jitter` | Randomizes each backoff wait by ±this fraction (`0.2` = ±20%), so the cadence isn't a mechanically clean power-of-two sequence | `0.2` |
| `timezone` | Timezone for output file timestamps ([zoneinfo](https://docs.python.org/3/library/zoneinfo.html) format) | `America/New_York` |
| `account_lists` | Named account lists (see below) | *(required — at least `follow_list`, here or per-account)* |

```yaml
platform: twitter
data_collection:
  targets:
    - home
  search_query: trump   # only read when "search" is a target
actions:
  follow_all: true
scroll_delay: 2
fetch_max_retries: 5
fetch_retry_backoff: 5
empty_batch_backoff_base: 5
empty_batch_backoff_max: 300
empty_batch_backoff_jitter: 0.2
timezone: America/New_York
account_lists:
  follow_list: follow_user_ids.json
```

Observation and actions are independent: `data_collection.targets` picks what gets scraped, `actions.follow_all` separately controls whether `follow_all()` runs at startup — you can follow without scraping, scrape without following, or both. `follow_all()` reads the `follow_list` account list; add more named lists as needed, nothing else changes until code reads a given name.

Several observation targets can be collected at once in either mode: the Agent Runtime fetches a snapshot of every configured target each activation, while the old scripted path round-robins a few pages per target per rotation. A single target keeps the scripted path's original behavior exactly — one continuous, indefinite scroll.

Each saved observation batch carries a self-describing header (`observation_id`, `timestamp_utc`, `experiment_id`, `agent_id`, `phase`, `treatment_arm`), so collected feed data can be split by experiment phase and treatment arm during analysis without cross-referencing any other log.

Actions beyond `follow_all` (liking, retweeting, muting) are implemented (`favorite_tweet()`, `retweet()`, `mute_user()`, `follow_user()`) and reachable through a single dispatcher, `TwitterScraper.execute_action(action, target)`. Outside of `follow_all()`, the only thing that calls it automatically today is the Agent Runtime's LLM-driven decision loop, below.

> **Known issue:** the `search` target is fully wired (query hash, variables, response parsing) but currently returns `404` against the live API. `x-client-transaction-id` and `content-type: application/json` have been added to GET requests as likely fixes; neither has confirmed it yet. `home` and `following` are unaffected and working.

### Cold-start initialization (`sockpuppet_config`)

A newly created sockpuppet account has no follows or engagement history, so the platform's recommendation system has nothing to personalize against. `sockpuppet_config.initialization_params`, when set (here or per-account), runs a one-time cold-start pass *before* `data_collection` observation begins for that account:

```yaml
sockpuppet_config:
  account_lists:
    - follow_list
  initialization_params:
    min_follows: 15
    min_engagements: 15
    max_initialization_days: 3
```

| Key | Description |
|---|---|
| `sockpuppet_config.account_lists` | Which named `account_lists` to draw cold-start candidates from |
| `initialization_params.min_follows` | Stop following once the account follows at least this many accounts |
| `initialization_params.min_engagements` | Stop liking/reposting once at least this many engagement events have been recorded |
| `initialization_params.max_initialization_days` | Give up (and proceed to observation anyway) after this many days even if the criteria above aren't met |

Follows are sampled (shuffled, not sequential) from the configured account lists and self-throttled to stay under the live follow rate limit (15/15min) instead of reacting to `429`s. Engagement candidates (likes/reposts) come from the account's own Home feed (GraphQL `HomeTimeline`) — not yet scoped to tweets *from* the configured account lists specifically, since that needs a per-account tweet-fetch endpoint this codebase doesn't have captured yet (see `ROADMAP.md` Phase 4a). Progress and completion are persisted in agent-state, so a restart resumes rather than restarting from zero, and a completed cold-start never re-runs.

**Cold-start engagement is persona-driven** when the account has a `persona_prompt` configured: each candidate post goes through the *same* decision cycle the later phases use (see **Agent Runtime** below), with the action set restricted to follow/like/repost — no mute. That's what makes each account's initialization reflect its own persona rather than a generic pattern, which matters because this phase is what shapes the baseline feed the experiment then measures. Without a persona configured, engagement falls back to a random like-or-repost choice.

`main.py` rejects a configured `min_follows` at startup if it isn't reachable within `max_initialization_days` given the 400/day follow limit.

Commented out by default in `config.yaml` — this performs real follow/like/repost actions against the live account, so uncomment and set real values deliberately.

### Agent Runtime (`sockpuppet_config.persona_prompt`)

Setting `sockpuppet_config.persona_prompt` (here or per-account) switches that account from the old scripted single-target observation loop to the LLM-driven **Agent Runtime** (roadmap Phase 4): a repeating five-stage decision cycle — Observation → Prompt Construction → Decision → Execution → Logging — that runs *instead of* (not alongside) the plain `data_collection` loop, after cold-start:

```yaml
sockpuppet_config:
  persona_prompt: personas/example_persona.txt
  phase: pre_treatment
  treatment_arm: control
  activation_schedule:
    distribution: truncated_powerlaw
    min_interval: 15m
    max_interval: 6h

experiment_design:
  experiment_name: mercury_muting
```

| Key | Description |
|---|---|
| `sockpuppet_config.persona_prompt` | Path to a text file with the persona's profile summary + behavioral tendencies (see `personas/example_persona.txt.example`). Enables Agent Runtime mode |
| `sockpuppet_config.phase` | Current experiment phase (`initialization` restricts actions to follow/like/retweet; anything else also allows mute). Static per-account value, used only when `experiment_design.treatment_arms` (below) is NOT set for this account — an opted-in account's real phase comes from the Experiment Orchestrator instead (see **Experiment Orchestrator** below) |
| `sockpuppet_config.treatment_arm` | Static per-account override, used only when `experiment_design.treatment_arms` is NOT set — an opted-in account's real, randomized assignment comes from the orchestrator instead |
| `sockpuppet_config.activation_schedule` | How long the runtime waits between decision cycles. `distribution: uniform` samples evenly between `min_interval`/`max_interval`; `truncated_powerlaw` is approximated as a log-uniform draw over the same range; `poisson` draws exponential inter-arrival times with the window's midpoint as the mean, clamped into the window (the schema gives a window, not a rate) |
| `sockpuppet_config.agent_id` | Stable identifier for this sockpuppet within the experiment, written into every observation, runtime-log, and engagement row | *(defaults to the account name)* |
| `sockpuppet_config.action_rate_limits` | Per-action pacing on top of the platform's own limits, e.g. `retweet: {max_per_hour: 4, min_interval: 8m}`. An action with no entry is uncapped. Both constraints apply independently — an hourly cap alone still permits a burst within a minute. A capped decision becomes a logged `no_action` with `no_action_reason: "rate_capped"` rather than blocking the activation. The retweet defaults are sized from live observation (~35% retweet success rate, misleading platform refusals), not a documented limit — see `ROADMAP.md` |
| `experiment_design.experiment_name` | Used to build a readable `experiment_id` (`<experiment_name>::<account_name>`) in the runtime's `experiment_context` |

Each activation fetches one snapshot of every configured `data_collection.targets` (multiple targets are allowed here, unlike the scripted path). **Every individual post in that snapshot then gets its own independent Prompt Construction → Decision → Execution → Logging pass** — one LLM call per post, not one call for the whole batch. This matches the paper's own worked example ("for each X item in the feed, decide whether or not to engage") and avoids overwhelming the model with dozens of competing candidates in one prompt; live testing found a whole-batch-in-one-prompt design made the model default to `like` almost every cycle instead of reasoning through compound persona triggers. `activation_schedule` still governs the interval *between* activations (platform visits) — not between individual post evaluations within one activation, which happen back-to-back.

Each per-post prompt is persona + that one post + experiment context + a fixed behavioral-rules contract; the model returns a structured `{trigger_check, action}` decision (never `target_object` — since each prompt concerns exactly one known post, the runtime derives the correct id itself: the tweet's own id for like/retweet, its author's id for follow/mute, so there's no id left for the model to hallucinate or confuse). The action is executed through the same `execute_action()` dispatcher cold-start uses, and the full trace is logged to a separate `*_runtime_log.jsonl` file — one row per post evaluated, not one per activation (`data_collection.logging: minimal` omits the prompt/raw model output from that log).

`execute_action()` determines success from the actual response body, not just the HTTP status — X's GraphQL endpoints can return `200 OK` with an `errors` payload (e.g. a stale query hash, or a rejection like *"this request looks like it might be automated"*) when a mutation didn't really go through. Any such failure is logged clearly with the real reason, both to the console (`[action] retweet on '...' FAILED: ...`) and in the runtime log's `system_response`, rather than being recorded as a silent success.

**Requires a local Ollama server**: `ollama serve`, with the model pulled (`ollama pull qwen2.5:14b` for the default — see `DEFAULT_MODEL` in `src/runtime/decision.py` to use a different one; it must support tool calling for structured output to work). Live testing found `qwen2.5:7b` too small to reliably reason through compound persona triggers (follow/mute) once more than a couple of candidate tweets were in play — it defaulted to `like` almost every cycle; `qwen2.5:14b` needs more RAM/VRAM (~9GB on disk) but reasons over those conditions much more reliably. `main.py` checks `OLLAMA_BASE_URL` (default `http://localhost:11434`, see `.env.example`) is reachable at startup and fails fast with a clear message if not — **running via `docker compose up` needs `OLLAMA_BASE_URL` set explicitly**, since `localhost` inside the container doesn't reach your host machine's Ollama.

Since one activation now makes as many LLM calls as there are observed posts (rather than one), each activation takes noticeably longer — this is an intentional tradeoff for reliability, not a regression; realistic `activation_schedule` intervals (minutes to hours) comfortably absorb it.

Commented out by default in `config.yaml` — this makes real LLM calls and real follow/like/repost/mute actions against the live account.

Cold-start (above) runs this same cycle with a restricted action set when a persona is configured, matching the paper's design.

### Experiment Orchestrator (`experiment_design.treatment_arms`)

Setting `experiment_design.treatment_arms` (a non-empty list, e.g. `[control, treatment]`) opts an account **into** a shared, thread-safe `ExperimentOrchestrator` (roadmap Phase 5) — every account keeps running its own Agent Runtime/cold-start loop in its own thread exactly as above, but consults the orchestrator instead of static `sockpuppet_config` values for phase, treatment arm, and the follow-rate budget. An account with no `treatment_arms` set is entirely unaffected, even if an orchestrator exists for sibling accounts in the same process.

```yaml
experiment_design:
  experiment_name: mercury_muting
  treatment_arms:
    - control
    - treatment
  randomization:
    method: simple_random
    seed: 42
    ratios:
      control: 0.5
      treatment: 0.5
  recovery_policy:
    max_consecutive_failures: 5
    backoff: 1m
    max_backoff: 30m
  phase_durations:
    initialization: adaptive   # ends on initialization_params, not a clock
    pre_treatment: 3d
    treatment:                 # dict form: whichever comes first
      max_duration: 7d
      max_action_count: 10000
    post_treatment: 3d         # omit to run indefinitely with no further auto-transition

intervention:
  action: mute
  target_accounts: untrustworthy_sources
  selection_rule: random70
  execution_time: end_of_pre_treatment
  applies_to_arms:
    - treatment
```

| Key | Description |
|---|---|
| `experiment_design.treatment_arms` | The experiment's arms. Every opted-in account must agree on this and on `randomization`/`phase_durations` (validated at startup — one Experiment Orchestrator per process) |
| `experiment_design.randomization.method` | Assignment procedure. Only `simple_random` is implemented; anything else is rejected at startup rather than silently treated as simple random |
| `experiment_design.randomization.seed` | Makes the treatment-arm split reproducible. Assignment happens **at the end of the pre-treatment phase**, not at startup, and is then persisted (`data/state/_experiments/<experiment_name>/orchestrator_state.json`) — a later seed change never silently reassigns an already-committed experiment |
| `experiment_design.randomization.ratios` | Per-arm split (must sum to 1.0); omit for an even split across `treatment_arms` |
| `experiment_design.recovery_policy` | How an account recovers from a failed activation: waits `backoff` doubled per consecutive failure (capped at `max_backoff`), giving up after `max_consecutive_failures`, without touching any other account's schedule |
| `experiment_design.phase_durations` | Per phase, driving automatic `pre_treatment → treatment → post_treatment` transitions. Each entry is either a bare duration (`3d`) or `{max_duration, max_action_count}` to end on whichever comes first — `max_action_count` counts real, agent-selected actions (not `no_action`) by any one participating account. `initialization: adaptive` marks the cold-start phase as governed by `initialization_params` instead of a clock. A phase with no entry never auto-transitions |
| `intervention` | Top-level namespace (sibling to `experiment_design`), only read for opted-in accounts. Fires exactly once, at `execution_time` (`end_of_pre_treatment` / `end_of_treatment`), for every account whose arm is in `applies_to_arms`: samples per `selection_rule` (`all`, or `randomNN` for a random NN%) from `target_accounts` (a name from `account_lists`) and runs `action` against each sampled `user_id`, through the same `execute_action()` dispatcher the Agent Runtime uses |

What the orchestrator actually replaces for an opted-in account:
- **Phase & treatment arm** — real, shared state instead of a static per-account value. The arm split is drawn once, centrally, at the *end* of pre-treatment, so agents run a condition-blind baseline first; every account's `experiment_context` then reflects it.
- **Follow rate limit** — cold-start's/the Agent Runtime's follow decisions share ONE cross-account 15-per-15-minutes budget (`ExperimentOrchestrator.acquire_follow_slot`) instead of each account self-throttling against its own independent window — closes a real gap where N concurrently-running accounts could otherwise each believe they had their own fresh budget.
- **Intervention execution** — a bulk, orchestrator-triggered action (e.g. the Mercury case study's "mute 70% of a list") distinct from the persona's own organic decisions, guaranteed to run exactly once per account via an atomic pending→running→completed claim. Every target gets a real attempt (retried with backoff), and a phase never advances while an intervention it triggered is still running.
- **Failure recovery** — a failed activation is recorded centrally and that one agent backs off and retries per `recovery_policy`, instead of the exception killing its thread for good.
- **Termination** — finishing the last phase completes the experiment (`experiment_status: completed`), logs a termination event, and every account stops acting and exits, so the process shuts down cleanly rather than running on past the end of the experiment. `experiment_status` also supports `paused`, which freezes phase transitions and idles every agent without ending the experiment.

Every scheduling decision, treatment assignment, intervention execution, phase transition, account failure, and termination is recorded to a separate reproducibility log (`data/<date>/_orchestrator/_experiment/run_..._reproducibility_log.jsonl`).

## Follow list

Each account list is a JSON file shaped like `follow_user_ids.json`:

```json
{
  "users": [
    { "user_id": "155659213", "_comment": "Ronaldo" },
    { "user_id": "1178432333764009989", "_comment": "NOlivier17" }
  ]
}
```

`_comment` is optional and used for logging only. The scraper tracks which accounts it has already followed (`data/state/<account_name>/twitter_agent_state.json`, one file per account) and skips ones already followed on subsequent runs, so restarting the container doesn't re-send follow requests for accounts it followed in a previous run.

### Rate limits
Twitter enforces the following limits on follows:
- **15 follows per 15-minute window**
- **400 follows per day** (platform-wide, applies to web and API equally)

The scraper respects these limits — it stops immediately if rate limited and reports the reset time.

## Continuous scraping

The timeline scraper is designed to run indefinitely rather than stop when it reaches the end of what Twitter's pagination will offer:

- **Bad responses are retried, not fatal.** Non-JSON responses, rate limits (`429`), and malformed/empty response bodies no longer crash the process. The scraper retries the same cursor with backoff (`fetch_retry_backoff` seconds × attempt number, up to `fetch_max_retries` times).
- **Exhausted pagination restarts from the top.** When the bottom cursor stops advancing or a cursor keeps failing after all retries, the scraper logs it and restarts pagination from the top of the timeline instead of exiting — so it keeps collecting new tweets as they arrive rather than terminating.
- **`home` and `search` avoid re-collecting the same posts.** Both endpoints rank against a finite pool of candidates at any moment, so naively restarting from the top would otherwise just re-serve the same batch. `fetch_home()` and `fetch_search_timeline()` mirror what the real web app does to avoid this: they echo the tweet IDs from the page just received back to Twitter as `seenTweetIds` on the next request, telling the ranking backend not to re-serve them, and separately keep a local cache of every tweet ID collected during the run so any duplicate that slips through anyway is filtered out before being written to the output file. `following` (X's reverse-chronological feed, GraphQL `HomeLatestTimeline`) doesn't need this and is unaffected.
- **A batch that's all duplicates backs off exponentially, with jitter.** If the ranking backend still re-serves already-seen tweets despite the above (common when polling faster than the feed actually refreshes), `home`/`search` wait roughly `empty_batch_backoff_base × 2^n` seconds (capped at `empty_batch_backoff_max`) after each consecutive batch with zero new tweets, on top of the normal `scroll_delay`. Each wait is randomized by ±`empty_batch_backoff_jitter` (default ±20%) so the cadence isn't a perfectly clean power-of-two sequence — deliberately, since a mechanically exact doubling pattern is itself a detectable non-human signature. Any batch with at least one new tweet resets the backoff to zero, so a live feed isn't slowed down once it starts producing new content again.

> **Note:** Twitter periodically rotates the internal GraphQL query hash and feature flags each timeline endpoint expects (`TWITTER_HOME_TIMELINE_HASH`/`TWITTER_HOME_TIMELINE_FEATURES`, `TWITTER_SEARCH_TIMELINE_HASH`/`TWITTER_SEARCH_TIMELINE_FEATURES` in `src/scraper/twitter.py`). If collection degrades or plateaus, capture a fresh request for that endpoint from the browser DevTools Network tab (same request used to pull `TWITTER_BEARER_TOKEN`/`TWITTER_CSRF_TOKEN` above) and update the matching constants.

## Running

Once `.env`, `accounts.yaml`, and `config.yaml` are configured:
```bash
docker compose up --build
```

Every account in `accounts.yaml` starts and runs concurrently. Output is saved as JSONL files under `data/<date>/twitter/<account_name>/`.

`config.yaml` and `accounts.yaml` are bind-mounted into the container (see `docker-compose.yml`), so changes to either take effect on the next restart without rebuilding. If `.env` changes don't take effect:
```bash
docker compose build --no-cache
docker compose up
```
