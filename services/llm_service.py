"""
LLM_helper — OpenAI-compatible client wrapper.

Supports two backend styles, selected automatically from the model name —
this mirrors the connection pattern used by wireless_ce_avatar/IntelAvatar's
services/llm_service.py so the same internal GNAI proxy config (key.py with
gnaigpt_token / gnaigpt_url / gnaigpt_model) works unchanged:

  - model starts with "claude"  -> Anthropic-style endpoint (GNAI's
                                    /api/providers/anthropic), wrapped so the
                                    rest of the app can still call
                                    client.chat.completions.create(...).
  - anything else                -> plain OpenAI-compatible endpoint.

Tool-calling is intentionally not implemented — this app only needs plain
chat completions (log Q&A, clarifying-question generation, skill synthesis).
"""
import httpx
import openai
from anthropic import Anthropic


def _convert_messages_to_anthropic(messages):
    """Split OpenAI-style messages into (system_text, anthropic_messages)."""
    system_parts = []
    converted = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            converted.append({"role": role, "content": content})
    return "\n\n".join(system_parts), converted


class _AnthropicMessageAdapter:
    def __init__(self, response):
        self.content = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ) or None


class _AnthropicChoiceAdapter:
    _STOP_REASON_MAP = {"end_turn": "stop", "max_tokens": "length"}

    def __init__(self, response):
        self.finish_reason = self._STOP_REASON_MAP.get(response.stop_reason, response.stop_reason)
        self.message = _AnthropicMessageAdapter(response)


class _AnthropicUsageAdapter:
    def __init__(self, usage):
        self.prompt_tokens = usage.input_tokens
        self.completion_tokens = usage.output_tokens
        self.total_tokens = usage.input_tokens + usage.output_tokens


class _AnthropicResponseAdapter:
    def __init__(self, response):
        self.choices = [_AnthropicChoiceAdapter(response)]
        self.usage = _AnthropicUsageAdapter(response.usage)


class _AnthropicCompletions:
    def __init__(self, client):
        self._client = client

    def create(self, model, messages, temperature=0.2, max_tokens=4000, **kwargs):
        system, filtered = _convert_messages_to_anthropic(messages)
        params = dict(model=model, messages=filtered, max_tokens=max_tokens, temperature=temperature)
        if system:
            # Mark the system prompt cacheable: in a multi-turn skill-building
            # chat (blueprints/chatbot) it's the same operation-pattern
            # stats + log excerpt resent on every single turn, easily the
            # largest chunk of every request. Anthropic's ephemeral cache
            # reuses that prefix instead of reprocessing it each message —
            # a real cost/latency win for exactly the "same context, many
            # turns" pattern this app's chat has. No effect (but harmless)
            # on short one-shot calls like the classify-style prompts.
            params["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        response = self._client.messages.create(**params)
        return _AnthropicResponseAdapter(response)


class _AnthropicChatAdapter:
    def __init__(self, client):
        self.completions = _AnthropicCompletions(client)


class AnthropicOpenAIAdapter:
    """Wraps an Anthropic client with an OpenAI-compatible `.chat.completions.create()`."""

    def __init__(self, anthropic_client):
        self._client = anthropic_client
        self.chat = _AnthropicChatAdapter(anthropic_client)


class LLM_helper:
    def __init__(self):
        self.client = None
        self.model = None
        # Token usage isn't shown anywhere in this app otherwise — the
        # per-call print + cumulative session total is the "small place" to
        # see the effect of things like the operation-delta trimming and the
        # prompt-caching above without wiring up a full dashboard.
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.session_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

    def set_up(self, gpt_token: str, gpt_url: str, model: str = "claude-4-6-sonnet") -> None:
        """Connect to the internal LLM proxy. Mirrors IntelAvatar's
        LLM_helper.set_up so the same GNAI key.py works unchanged. No extra
        corporate web proxy is used for the LLM call itself — the GNAI
        endpoint is reached directly, same as the reference app."""
        http_client = httpx.Client(proxy=None, verify=False, trust_env=False)
        if model.startswith("claude"):
            self.client = AnthropicOpenAIAdapter(Anthropic(
                base_url=gpt_url,
                auth_token=gpt_token,
                http_client=http_client,
            ))
        else:
            self.client = openai.OpenAI(
                api_key=gpt_token,
                base_url=gpt_url,
                http_client=http_client,
            )
        self.model = model

    @property
    def is_ready(self) -> bool:
        return self.client is not None

    def chat(self, messages: list, system_content: str = None,
             temperature: float = 0.2, max_tokens: int = 4000) -> str:
        if not self.is_ready:
            raise RuntimeError("LLM_helper is not configured (no key.py found — see README.md).")
        api_messages = []
        if system_content:
            api_messages.append({"role": "system", "content": system_content})
        api_messages.extend(messages)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self._record_usage(getattr(response, "usage", None))
        return response.choices[0].message.content or ""

    def _record_usage(self, usage) -> None:
        if usage is None:
            return
        p = getattr(usage, "prompt_tokens", 0) or 0
        c = getattr(usage, "completion_tokens", 0) or 0
        t = getattr(usage, "total_tokens", p + c) or (p + c)
        self.last_usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": t}
        self.session_usage["prompt_tokens"] += p
        self.session_usage["completion_tokens"] += c
        self.session_usage["total_tokens"] += t
        self.session_usage["calls"] += 1
        print(
            f" tokens: +{p} in / +{c} out / +{t} total "
            f"(session: {self.session_usage['total_tokens']} across {self.session_usage['calls']} calls)"
        )
