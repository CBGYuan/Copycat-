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

from services import pricing_service


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

    def stream_text(self, model, messages, temperature, max_tokens, record_usage):
        """Yield response text incrementally. Breaking out of this generator
        early (the caller's fetch was aborted) unwinds through the `with`
        block below, which closes the underlying stream/connection — the
        provider stops generating further tokens instead of finishing a
        response nobody reads, which is the whole point of a Stop button."""
        system, filtered = _convert_messages_to_anthropic(messages)
        params = dict(model=model, messages=filtered, max_tokens=max_tokens, temperature=temperature)
        if system:
            params["system"] = [{
                "type": "text", "text": system, "cache_control": {"type": "ephemeral"},
            }]
        with self._client.messages.stream(**params) as stream:
            for text in stream.text_stream:
                yield text
            # Only reached on a normal (non-cancelled) finish — the stream is
            # already fully consumed at this point, so this is a cheap local
            # read, not an extra wait. On early cancellation this line never
            # runs, which is the correct call: nobody can report token counts
            # the provider never fully committed to the response.
            final = stream.get_final_message()
            if final is not None and getattr(final, "usage", None):
                record_usage(_AnthropicUsageAdapter(final.usage))


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
        self.session_usage = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0,
            "cost_estimate_available": True, "estimated_cost_usd": 0.0,
            "cost_breakdown": {"input_usd": 0.0, "output_usd": 0.0},
            "unpriced_calls": 0, "models": [],
        }

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

    def transcribe_attachment(self, media_type: str, b64_data: str, hint: str = "") -> str:
        """One-shot: read a pasted/attached screenshot or PDF and return its
        content as plain text (markdown table for tabular content).

        This is the ONLY multimodal call in the app, and deliberately so. An
        engineer's evidence is often a config table screenshotted out of a
        spec — or the spec page itself as a PDF — which they were otherwise
        re-typing by hand before it could be taught. Everything downstream of
        an answer — chat history, an operation's `reason`, the exported
        expert_rules — is a plain string, so the attachment is turned into
        text HERE, at the edge. Nothing further down ever has to know a
        non-text payload existed.

        PDFs only work on the Anthropic path: its Messages API takes a native
        `document` block and reads the page's text AND its visual content
        (charts, scanned handwriting). The OpenAI-compatible chat-completions
        path has no equivalent inline block, so it is refused loudly rather
        than silently sent as something the endpoint will ignore.
        """
        if not self.is_ready:
            raise RuntimeError("LLM_helper is not configured (no key.py found — see README.md).")
        is_pdf = media_type == "application/pdf"
        if isinstance(self.client, AnthropicOpenAIAdapter):
            media_block = {
                "type": "document" if is_pdf else "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64_data},
            }
        elif is_pdf:
            raise RuntimeError(
                f"PDF attachments need a Claude model; the configured model is {self.model}. "
                "Attach a screenshot of the page instead."
            )
        else:
            media_block = {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64_data}"},
            }
        noun = "document" if is_pdf else "image"
        ask = (f"Transcribe this {noun} into plain text I can paste into a rules "
               "document. If it is a table, output a GitHub-style markdown table "
               "and keep every row — do not summarise, truncate, or add a row that "
               "is not visible. If a cell is unreadable, write [?] rather than "
               "guessing. Output only the transcription, no commentary.")
        if hint.strip():
            ask += f"\n\nContext for what this {noun} shows: {hint.strip()}"
        return self.chat(
            [{"role": "user", "content": [media_block, {"type": "text", "text": ask}]}],
            temperature=0.0,
            max_tokens=4000,
        )

    def chat_stream(self, messages: list, system_content: str = None,
                     temperature: float = 0.2, max_tokens: int = 4000):
        """Generator variant of chat() for callers that let the engineer
        cancel mid-response (blueprints/chatbot's /send_stream). Stopping
        iteration early (the HTTP client disconnected) propagates a
        GeneratorExit into whichever branch below is active, which closes
        that branch's own stream/connection — see AnthropicOpenAIAdapter's
        stream_text and the try/finally here."""
        if not self.is_ready:
            raise RuntimeError("LLM_helper is not configured (no key.py found — see README.md).")
        api_messages = []
        if system_content:
            api_messages.append({"role": "system", "content": system_content})
        api_messages.extend(messages)

        if isinstance(self.client, AnthropicOpenAIAdapter):
            yield from self.client.chat.completions.stream_text(
                model=self.model, messages=api_messages,
                temperature=temperature, max_tokens=max_tokens,
                record_usage=self._record_usage,
            )
            return

        stream = self.client.chat.completions.create(
            model=self.model, messages=api_messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True, stream_options={"include_usage": True},
        )
        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    text = getattr(delta, "content", None) if delta else None
                    if text:
                        yield text
                usage = getattr(chunk, "usage", None)
                if usage:
                    self._record_usage(usage)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def _record_usage(self, usage) -> None:
        if usage is None:
            return
        p = getattr(usage, "prompt_tokens", 0) or 0
        c = getattr(usage, "completion_tokens", 0) or 0
        t = getattr(usage, "total_tokens", p + c) or (p + c)
        estimate = pricing_service.estimate_usage_cost(self.model, p, c)
        self.last_usage = {
            "prompt_tokens": p, "completion_tokens": c, "total_tokens": t,
            **estimate,
        }
        self.session_usage["prompt_tokens"] += p
        self.session_usage["completion_tokens"] += c
        self.session_usage["total_tokens"] += t
        self.session_usage["calls"] += 1
        model_name = str(self.model or "")
        if model_name and model_name not in self.session_usage["models"]:
            self.session_usage["models"].append(model_name)
        if estimate["cost_estimate_available"]:
            breakdown = estimate["cost_breakdown"]
            self.session_usage["cost_breakdown"]["input_usd"] += breakdown["input_usd"]
            self.session_usage["cost_breakdown"]["output_usd"] += breakdown["output_usd"]
            if self.session_usage["unpriced_calls"] == 0:
                self.session_usage["estimated_cost_usd"] = (
                    self.session_usage["cost_breakdown"]["input_usd"]
                    + self.session_usage["cost_breakdown"]["output_usd"]
                )
            self.session_usage["rate_source"] = estimate["rate_source"]
            self.session_usage["rates_usd_per_mtok"] = estimate["rates_usd_per_mtok"]
        else:
            self.session_usage["unpriced_calls"] += 1
            self.session_usage["cost_estimate_available"] = False
            self.session_usage["estimated_cost_usd"] = None
        print(
            f" tokens: +{p} in / +{c} out / +{t} total "
            f"(session: {self.session_usage['total_tokens']} across {self.session_usage['calls']} calls)"
        )
