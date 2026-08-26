"""Prompt Construction (roadmap Phase 4e, paper section 3.3 stage 2):

    Prompt = Persona Prompt + Platform State + Experiment Context + Behavioral Rules

`persona_prompt` and `experiment_context` are supplied by the caller (see
TwitterScraper.run_agent_runtime); `RUNTIME_BEHAVIORAL_RULES` here is the
runtime's own fixed operating contract -- available actions and how to weigh
them -- as distinct from the *persona's* behavioral tendencies, which live
inside the persona prompt text itself (paper section 3.2 treats profile
summary and behavioral tendencies as one persistent block).

One prompt = one observed post (roadmap Phase 4, per-post redesign, 2026-08-18):
the paper's own worked Mercury Project example describes the decision process
as "for each X item in the feed, decide whether or not to engage" -- a
per-item process, not one decision spanning an entire feed snapshot. Live
testing found the previous whole-batch-in-one-prompt design overwhelmed the
local model (it defaulted to `like` almost every cycle rather than reasoning
through compound persona triggers against ~28 competing candidates at once).
"""

RUNTIME_BEHAVIORAL_RULES = """\
You control a social media account. Right now you are looking at exactly ONE \
post from your feed. For this post, choose exactly ONE action:
- like: favorite this post
- retweet: repost this post
- follow: follow this post's author
- mute: mute this post's author
- no_action: do nothing about this post

Rules:
- Choose no_action if nothing about THIS post warrants acting on, given your \
persona and behavioral tendencies. You will see many other posts in this \
session -- it's fine, and expected, to no_action most of them.
- Only choose from the ALLOWED ACTIONS list below -- some actions (e.g. \
mute) are unavailable during certain experiment phases.
- Check YOUR RECENT ACTIONS below -- if you already performed this exact \
action on this post, choose a different action on it (or no_action) instead \
of repeating it.
- Before defaulting to your most common action, explicitly check THIS post \
against EVERY specific trigger condition in your persona (especially any \
"always"/"almost always" mute or follow rules) -- don't just apply whatever \
generic reaction you'd normally have.\
"""


def _format_tweet(tweet: dict) -> str:
    author = tweet.get("author") or {}
    text = (tweet.get("text") or "").replace("\n", " ")
    if len(text) > 240:
        text = text[:240] + "..."
    return (
        f"[tweet_id: {tweet.get('tweet_id')}, author_user_id: {author.get('user_id')}, "
        f"author_username: {author.get('username')}, "
        f"author_followers: {author.get('followers', 0)}]: {text}"
    )


def format_experiment_context(context: dict) -> str:
    return "\n".join(f"{key}: {value}" for key, value in context.items())


def format_recent_interactions(recent_interactions) -> str:
    if not recent_interactions:
        return "(none yet this run)"
    return "\n".join(
        f"- {entry.get('action')} on {entry.get('target')}"
        for entry in recent_interactions
    )


def construct_prompt(
    persona_prompt: str, tweet: dict, target_name: str, experiment_context: dict,
    allowed_actions, recent_interactions=None,
) -> str:
    behavioral_rules = f"{RUNTIME_BEHAVIORAL_RULES}\n\nALLOWED ACTIONS this cycle: {', '.join(allowed_actions)}"
    return (
        "=== PERSONA ===\n"
        f"{persona_prompt.strip()}\n\n"
        "=== EXPERIMENT CONTEXT ===\n"
        f"{format_experiment_context(experiment_context)}\n\n"
        "=== YOUR RECENT ACTIONS (avoid repeating these) ===\n"
        f"{format_recent_interactions(recent_interactions)}\n\n"
        f"=== OBSERVED POST (from {target_name}) ===\n"
        f"{_format_tweet(tweet)}\n\n"
        "=== BEHAVIORAL RULES ===\n"
        f"{behavioral_rules}\n\n"
        "Respond with your decision for this post."
    )
