# ScraperSky → Agentic Sockpuppet Research Platform: Roadmap

*Living project roadmap. Repo: `wjbradshawTerp/ScraperSky`, branch `main`.*

## 1. The goal

ScraperSky started as a single-account X/Twitter scraper. The target is to grow it into the infrastructure described in the "Agentic Sockpuppet Experiment" research paper (Do Won Kim, design; Will Bradshaw, implementation) — a platform that runs many LLM-driven sockpuppet accounts through a controlled **Observation → Decision → Execution → Logging** loop, under an **Experiment Orchestrator** that manages experimental phases and randomized interventions.

The paper's architecture has five modules:
1. **Configuration Layer** — researcher-facing config defining the experiment (`experiment_design`, `sockpuppet_config`, `intervention`, `data_collection` namespaces).
2. **Sockpuppet Initialization & Persona Construction** — cold-start behavior (follow/engage until stopping criteria met, using a *restricted* action set) + a persistent `persona_prompt` (profile summary + behavioral rules) that governs an agent's decisions for the whole experiment.
3. **Agent Runtime** — the per-activation decision cycle: (1) Observation of platform state, (2) Prompt Construction (persona + platform state + experiment context + behavioral rules), (3) Decision (LLM call → structured `{action, target_object, system_response, execution_status}`), (4) Execution, (5) Logging. Activation timing is sampled from a configurable distribution (e.g. truncated power-law), not a fixed schedule. The *same* five-stage cycle runs during cold-start, pre-treatment, and post-treatment — only the allowed action set and experiment phase differ.
4. **Experiment Orchestrator** — global experiment state: account provisioning, per-agent activation scheduling, treatment assignment/randomization, phase transitions (init/pre-treatment → treatment → post-treatment), failure recovery, reproducibility logging.
5. **Data Collection & Storage** — immutable JSONL records: platform observations (for_you_feed, home_timeline, engagement_log, intervention_history) and full runtime logs (prompt, model output, action, execution result).

**Case study to reproduce**: the "Mercury Project" muting experiment. Sockpuppets are seeded from real Mercury participants' pre-treatment behavior (persona = summarized engagement/following patterns), split into control/treatment arms, and after a pre-treatment observation period, treatment agents mute a random 70% of a list of "untrustworthy" accounts they follow. Both arms continue being observed through a post-treatment period. The goal is to causally measure the effect of muting on subsequent exposure/engagement.

**Standing decisions** (already made with the user, don't re-litigate):
- Build order: **bugfixes → config → observation/action split → multi-account → LLM Agent Runtime → orchestrator** (roughly mirrors the paper's own module dependency order).
- Agent Runtime will use **LangChain** (user's explicit choice over a raw Anthropic-API-only design).
- GUI is **deprioritized** — paper only calls for a config file + a "Monitor & Control" component, not a UI. Revisit only after the orchestrator exists.
- Account provisioning / cookie automation is real ToS/detection risk — sequence it deliberately with its own risk review, and the user should confirm IRB/ethics approval covers automated multi-account behavior before scaling past 1 account. This is a process question for the user, not something to resolve in code.
- Multi-account credential storage was resolved as: **one gitignored `accounts.yaml`** with a list of account entries (credentials + optional per-account overrides), not per-account `.env` files or suffixed env vars. See Phase 3 below.
- Platform action rate limits (from live testing): **follow — 15/15min, 400/day**. Likes/mutes: no rate-limit issues detected so far. These constrain `initialization_params.min_follows` and any future activation-scheduling validation (Phase 4/5) — a configured `min_follows` must be reachable within `max_initialization_days` given the follow rate limit.

## 2. What's been done

### Phase 0 — Data integrity & safety bugfixes ✅
- Fixed `build_tweet_object()` in `src/scraper/twitter.py`: author `username`/`display_name` were 100% null in every collected tweet because X moved those fields from `user.legacy` to `user.core`. Now checks `core` first, falls back to `legacy`, degrades to `None`/`0` without crashing if both are missing.
- Removed a hardcoded unconditional `retweet()` call that fired on every process start in the old "follows" mode.
- Added `src/storage/agent_state.py` (`AgentState` class): persists `following_list`/`muted_accounts` as JSON so `follow_all()` doesn't re-submit follow requests for accounts it already follows across restarts.

### Phase 1 — Configuration Layer ✅ (partial vs. paper schema — widened in Phase 4/6)
- Introduced `config.yaml` at repo root. Moved every non-secret behavioral setting out of `.env` into it: `platform`, `scroll_delay`, `fetch_max_retries`, `fetch_retry_backoff`, `timezone`, `account_lists` (named, multi-list-capable — currently just `follow_list` → `follow_user_ids.json`).
- `src/config.py`'s `Settings` class loads `config.yaml`; `validate()` reports `.env`/`config.yaml` problems separately.
- Added `pyyaml` (YAML parsing) and `tzdata` (fixes `zoneinfo` having no IANA data on native Windows — needed for local runs/tests outside Docker) via `poetry add`.
- Only `account_lists` and `data_collection.targets` have real implementations today. The paper's `experiment_design`, `sockpuppet_config.persona_prompt`/`initialization_params`/`activation_schedule`, `intervention`, and `data_collection.observation_frequency`/`logging`/`engagement_log`/`intervention_history` namespaces are unbuilt — see the exact field list in Phase 4/6 below.

### Phase 2 — Split "modes" into observation vs. action ✅ (Search target on hold — see Known Issues)
- `config.yaml`'s old single `mode: home`/`follows` replaced with two independent knobs:
  - `data_collection.targets` — a list (`for_you_feed` / `home_timeline` / `search`); **exactly one entry supported for now** (validated at startup in `main.py`) — the scraper is still single-threaded/continuous-loop per account, true simultaneous multi-target polling needs the Agent Runtime's scheduler (Phase 4).
  - `actions.follow_all` — independently controls whether `follow_all()` runs at startup.
- `scrape_home()`/`scrape_follows()` renamed to `fetch_for_you_feed()`/`fetch_home_timeline()`, selected via an `OBSERVATION_HANDLERS` dict instead of `if self.mode == ...`.
- Added `execute_action(action, target)` on `TwitterScraper`: generic dispatcher over `favorite_tweet`/`retweet`/`follow_user`/`mute_user`, with agent-state bookkeeping (`mark_followed`/`mark_muted`) centralized inside it. `follow_all()` now calls through it. **This is the shape the paper's LLM decision output calls, but currently returns a bare `bool` — needs to return a structured result; see Phase 4.**
- `parse_timeline()` generalized to accept a `timeline_path` (was hardcoded to Home's response shape) so Search's different response nesting works without duplicating entry-parsing logic.
- Added a `search` target: `TWITTER_SEARCH_TIMELINE_HASH`, `fetch_search_latest()`/`fetch_search_timeline()`, `SEARCH_TIMELINE_PATH`. Fully wired into `config.yaml`/`main.py`/`OBSERVATION_HANDLERS`.
- Added `x-client-transaction-id` and `content-type: application/json` headers to **GET** timeline requests (previously only POST action methods sent them) — a plausible fix for Search's stricter anti-bot checks, doesn't hurt Home/Follows either way.
- Improved failure diagnostics: non-JSON responses now dump response headers, not just status + truncated body.

### Phase 3 — Multi-account support ✅ (committed: `6f0e50a`)
- Credentials live in one gitignored **`accounts.yaml`** (template committed as `accounts.yaml.example`), a list of account entries — not per-account `.env` files, not suffixed env vars.
- `src/config.py`: `Account` class + `Settings.load_accounts()`. Each account entry needs `name`/`auth_token`/`bearer_token`/`csrf_token`; every other `config.yaml` top-level key is an optional per-account override, falling back to `config.yaml`'s value when omitted. Dict-valued keys shallow-merge; scalar keys replace outright.
- `src/scraper/base.py`/`src/scraper/twitter.py`: take a single `Account` instead of raw `targets`/`actions`. Builds `httpx.Client` from that account's tokens, reads retry/scroll-delay/timezone off `self.account`, prefixes console output with `[account_name]`.
- `src/storage/file_manager.py`: namespaced per-account output (`data/<date>/twitter/<account_name>/`).
- `src/storage/agent_state.py`: instantiated with a per-account path (`data/state/<account_name>/twitter_agent_state.json`).
- `src/main.py`: loads all accounts, validates each one's resolved config up front, runs each account's `scraper.run()` in its own `threading.Thread` — a minimal concurrency stand-in, **not** the real per-agent activation scheduler (that's Phase 5).
- `.env` now holds only Docker volume-mount paths — no secrets.
- Verified via scratch fixtures (not the live API). Not yet re-verified via a real `docker compose up` with multiple live accounts.

### Known issues / open threads
- **`search` target 404s against the live API.** Everything is built correctly (query hash, variables matching a captured browser cURL, response-path parsing) — looks like a live anti-bot/request-signing problem, not a design gap. **The user is debugging this independently — do not pick it back up unless asked.** `for_you_feed`/`home_timeline` confirmed working pre-Phase-3; not yet re-verified live post-Phase-3.
- **For You tab pagination is not indefinite.** Per the user's implementation notes: it eventually reaches a point with no cursor and needs manual intervention to keep going. Predates the Agent Runtime; the activation model (Phase 4 — bounded fetch per activation instead of one continuous scroll) likely sidesteps most of this, but confirm it's actually resolved once Phase 4 lands rather than assuming so.
- Config schema gap (see Phase 1 note above) — tracked in Phase 4/6 below, not a surprise.

## 3. Remaining roadmap

**Phase 4 — Agent Runtime (LLM decision loop)** *(next)*

Implement the paper's five-stage cycle using **LangChain**: Observation → Prompt Construction → Decision → Execution → Logging. The *same* cycle drives cold-start, pre-treatment, and post-treatment — only the allowed action set and experiment-phase context differ. Concrete sub-tasks, refined against the paper's exact schemas:

- **4a. Cold-start initialization mode.** A restricted-action variant of the runtime: follow / like / repost / other lightweight engagement only (no mute/unmute). Loops until `initialization_params` stopping criteria are met (`min_follows`, `min_engagements`, `max_initialization_days`), sampling candidate accounts from the configured `account_lists`. Validate configured `min_follows` is reachable within `max_initialization_days` given the live rate limit (15 follows/15min, 400/day) — reject infeasible configs at startup rather than silently stalling.
- **4b. `AgentState` schema expansion** ([src/storage/agent_state.py](src/storage/agent_state.py)). Currently only `following_list`/`muted_accounts`. Add: `persona_prompt`, `account_metadata`, `interaction_history`, `initialization_progress`, `internal_memory` (per the paper's `agent_state` schema).
- **4c. `execute_action()` structured return** ([src/scraper/twitter.py:340](src/scraper/twitter.py:340)). Currently returns a bare `bool`. Needs to return `{execution_status, system_response}` (or similar) so Decision/Logging have something to record — the paper's `Decision` object is `{action, target_object, system_response, execution_status}`, not just `{action, target_object}`.
- **4d. `No Action` as a first-class decision.** The LLM decision schema must support a no-op explicitly (one of the paper's listed action types), and the dispatcher must handle it without hitting the network.
- **4e. Prompt construction**: `Prompt = Persona Prompt + Platform State + Experiment Context + Behavioral Rules`. `experiment_context` needs `experiment_id`, `phase`, `treatment_arm`, `intervention_status`, `current_time`, `runtime_constraints` — none of which exist yet; most will initially be stubbed/static until Phase 5's orchestrator provides real values.
- **4f. Config schema widening** (Configuration Layer, Phase 1 continuation) — add the fields the paper specifies but `config.yaml`/`accounts.yaml` don't yet parse:
  - `experiment_design`: `experiment_name`, `treatment_arms`, `randomization.method`, `phase_durations.{initialization,pre_treatment,post_treatment}`
  - `sockpuppet_config`: `persona_prompt`, `initialization_params.{min_follows,min_engagements,max_initialization_days}`, `activation_schedule.{distribution,min_interval,max_interval}` (`account_lists` already exists)
  - `intervention`: `action`, `target_accounts`, `selection_rule`, `execution_time`
  - `data_collection`: `observation_frequency`, `logging` (`targets` already exists)
- Simultaneous multi-target observation (deferred since Phase 2) gets built here too, via the per-cycle scheduler — each account currently only observes one target at a time.
- Persona prompt construction from Mercury participant data is largely a data/prompt-engineering task and can proceed in parallel once the runtime shape (4a–4e) is fixed.

**Phase 5 — Experiment Orchestrator**
- Global experiment state (`experiment_id`, `current_phase`, `scheduler`, `activation_queue`, `treatment_assignment`, `intervention_schedule`, `experiment_status`), shared across all agents (unlike per-agent runtime state).
- Seven responsibilities per the paper: (1) account provisioning (`agent_id`/`persona_prompt`/`experiment_id`/`initialization_params` per account), (2) runtime scheduling (real per-agent randomized activation intervals — NOT an OS cron job; supersedes Phase 3's one-thread-per-account stand-in; must also respect the 15/15min follow rate limit across concurrently-active agents), (3) treatment assignment/randomization, (4) intervention execution, (5) phase transitions (deterministic state machine: init/pre-treatment → treatment → post-treatment), (6) failure recovery (isolated per-agent, doesn't halt the experiment), (7) reproducibility logging (every scheduling decision, assignment, intervention, phase transition, failure, and termination event).
- This is what actually runs the Mercury muting case study end-to-end.

**Phase 6 — Data Collection & Storage expansion**
- Extend `FileManager`'s JSONL output to the paper's exact schemas (schema-widening, not a storage migration — JSONL stays the format):
  - **Observation schema**: `observation_id`, `timestamp_utc`, `experiment_id`, `agent_id`, `phase`, `treatment_arm`, (+ the platform-specific payload).
  - **Runtime log schema**: `log_id`/`observation_id`, `timestamp_utc`, `platform_observation`, `constructed_prompt`, `model_output`, `selected_action`, `target_object`, `execution_result`, `system_response`, `execution_status`.

**Ongoing / lower priority (not blocking anything):** structured logging (replace bare `print()`/`self._log()`), a test suite, run-archival (zip old runs), a status CLI (in place of the deprioritized GUI).

## 4. Orientation — key files

- `config.yaml` — default behavioral config shared by every account unless overridden. Non-secret, git-shareable. Read this first to understand default runtime behavior.
- `accounts.yaml` — gitignored, per-account credentials + optional per-account overrides of anything in `config.yaml`. `accounts.yaml.example` is the committed template.
- `src/config.py` — `Account` class (resolved per-account credentials + merged config); `Settings` singleton (`settings`) loads `config.yaml`/`.env` and exposes `load_accounts()`.
- `src/main.py` — entry point; validates every account's resolved config, then runs each account's scraper in its own thread.
- `src/scraper/base.py` — `BaseScraper` ABC, takes a single `Account` (`self.account`), abstract `run`/`fetch_for_you_feed`/`fetch_home_timeline`.
- `src/scraper/twitter.py` — GraphQL timeline fetchers, `parse_timeline()`, `execute_action()`, all four platform actions, all keyed off `self.account`.
- `src/scraper/x_client.py` — `x-client-transaction-id` generator (X's anti-automation request signing). No per-account state, safe to call from multiple threads.
- `src/storage/file_manager.py` — JSONL writer, namespaced by account name; timezone is a per-instance param.
- `src/storage/agent_state.py` — per-account state persistence (currently `following_list`/`muted_accounts`; see Phase 4b for the fields still missing).
- `README.md` — user-facing setup/config docs, kept in sync with `config.yaml`/`accounts.yaml`'s actual schema each phase.

## 5. Working agreements worth knowing

- Don't run `docker compose up` / hit the live X API autonomously — it performs real actions (follows, timeline fetches) against the user's real account(s) with live credentials. Verify logic locally (unit-style scratch scripts, synthetic fixtures) and ask the user to run live verification themselves.
- Only commit/push when explicitly asked, and only the files actually intended (check `git status`/`git diff` before staging — this repo has real secrets in gitignored `.env`/`accounts.yaml` that must never get staged).
- Corrections to this roadmap are welcome and expected — several past checklist items have turned out to be stale (e.g. an old note about Search being blocked by a missing header that was already resolved before Phase 2 started). Treat this file as living documentation, not a fixed contract.
