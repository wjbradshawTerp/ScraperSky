"""Decision stage (roadmap Phase 4e, paper section 3.3 stage 3): submits the
constructed prompt to an LLM via LangChain and gets back a structured
Decision.

Uses a local Ollama model (langchain-ollama) -- no API key, no per-call
cost, per the project's standing choice of LangChain for the Agent Runtime
plus the user's preference for a free/local model. Requires `ollama serve`
running with DEFAULT_MODEL pulled (see README's Agent Runtime section).
Swap `model`/`base_url`, or replace ChatOllama with a different langchain
chat model entirely, without touching prompt.py or schema.py.
"""

import os

from langchain_ollama import ChatOllama

from runtime.schema import Decision

# qwen2.5:14b supports tool calling, which .with_structured_output() relies
# on -- not every Ollama model does. Swapped up from qwen2.5:7b (roadmap
# Phase 4 live testing, 2026-08-18): the 7b model reliably defaulted to its
# dominant persona behavior ("like") and essentially never reasoned through
# compound conditional triggers (follow/mute) once more than a couple of
# candidate tweets were in play -- a capability ceiling, not a prompt-wording
# problem. `ollama pull qwen2.5:14b` before first run.
DEFAULT_MODEL = "qwen2.5:14b"
DEFAULT_BASE_URL = "http://localhost:11434"


class DecisionEngine:
    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = None):
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
        self._llm = ChatOllama(model=model, base_url=self.base_url).with_structured_output(Decision)

    def decide(self, prompt: str) -> Decision:
        return self._llm.invoke(prompt)
