"""Coverage for the cancellable streaming chat path (/chatbot/send_stream and
LLM_helper.chat_stream). The point of this feature is that clicking Stop mid-
response actually closes the upstream provider stream instead of just making
the browser stop waiting for a response that keeps being generated (and
billed) regardless -- so the core thing worth proving here is: closing the
generator early closes the underlying stream/connection.
"""
import json
import unittest
from types import SimpleNamespace

from services.llm_service import AnthropicOpenAIAdapter, LLM_helper


# ---- Fakes standing in for the OpenAI-compatible streaming client ---------

class _FakeOpenAIChunk:
    def __init__(self, content=None, usage=None):
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=content))] if content is not None else []
        self.usage = usage


class _FakeOpenAIStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._chunks:
            raise StopIteration
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class _FakeOpenAIClient:
    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: stream))


# ---- Fakes standing in for the Anthropic streaming client ------------------

class _FakeAnthropicStreamCM:
    def __init__(self, texts, final_usage=None):
        self._texts = list(texts)
        self.closed = False
        self._final_usage = final_usage

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    @property
    def text_stream(self):
        return iter(self._texts)

    def get_final_message(self):
        return SimpleNamespace(usage=self._final_usage)


class _FakeAnthropicClient:
    def __init__(self, stream_cm):
        self.messages = SimpleNamespace(stream=lambda **kw: stream_cm)


class ChatStreamOpenAITests(unittest.TestCase):
    def test_yields_text_and_closes_stream_on_normal_completion(self):
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
        stream = _FakeOpenAIStream([
            _FakeOpenAIChunk("Hello "),
            _FakeOpenAIChunk("world"),
            _FakeOpenAIChunk(usage=usage),
        ])
        helper = LLM_helper()
        helper.client = _FakeOpenAIClient(stream)
        helper.model = "gpt-4"

        text = "".join(helper.chat_stream(messages=[{"role": "user", "content": "hi"}]))

        self.assertEqual(text, "Hello world")
        self.assertTrue(stream.closed)
        self.assertEqual(helper.last_usage["total_tokens"], 12)

    def test_closing_the_generator_early_closes_the_stream(self):
        """This is the actual token-savings mechanism behind the Stop button:
        the caller (chatbot_routes.send_stream) stops iterating once the
        client disconnects, and that alone must close the upstream stream."""
        stream = _FakeOpenAIStream([_FakeOpenAIChunk("a"), _FakeOpenAIChunk("b"), _FakeOpenAIChunk("c")])
        helper = LLM_helper()
        helper.client = _FakeOpenAIClient(stream)
        helper.model = "gpt-4"

        gen = helper.chat_stream(messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(next(gen), "a")
        self.assertFalse(stream.closed)
        gen.close()

        self.assertTrue(stream.closed)


class ChatStreamAnthropicTests(unittest.TestCase):
    def test_yields_text_and_records_usage_on_normal_completion(self):
        final_usage = SimpleNamespace(input_tokens=3, output_tokens=4)
        cm = _FakeAnthropicStreamCM(["Hel", "lo"], final_usage=final_usage)
        helper = LLM_helper()
        helper.client = AnthropicOpenAIAdapter(_FakeAnthropicClient(cm))
        helper.model = "claude-4-6-sonnet"

        text = "".join(helper.chat_stream(messages=[{"role": "user", "content": "hi"}]))

        self.assertEqual(text, "Hello")
        self.assertTrue(cm.closed)
        self.assertEqual(helper.last_usage["total_tokens"], 7)

    def test_closing_the_generator_early_closes_the_stream_without_reading_usage(self):
        cm = _FakeAnthropicStreamCM(["a", "b", "c"], final_usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        helper = LLM_helper()
        helper.client = AnthropicOpenAIAdapter(_FakeAnthropicClient(cm))
        helper.model = "claude-4-6-sonnet"

        gen = helper.chat_stream(messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(next(gen), "a")
        gen.close()

        self.assertTrue(cm.closed)
        # Cancelled before the stream was exhausted -- get_final_message() is
        # never reached, so usage from a response nobody finished is never
        # reported as if it were.
        self.assertEqual(helper.last_usage["total_tokens"], 0)


class _StreamingFakeLlm:
    def __init__(self, text, is_ready=True):
        self._text = text
        self.is_ready = is_ready
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        self.session_usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "calls": 1}

    def chat_stream(self, **_kwargs):
        for ch in self._text:
            yield ch


class SendStreamRouteTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        from configs.global_configs import app_config
        self.app_config = app_config
        self._real_llm = app_config.llm_helper
        self.app = create_app()

    def tearDown(self):
        self.app_config.llm_helper = self._real_llm

    def _client_with_state(self, wsid):
        from services import session_store
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["wsid"] = wsid
        session_store._STORE[wsid] = session_store.WorkingState()
        return client

    def _extract_done_payload(self, body: str) -> dict:
        marker = "event: done\ndata: "
        idx = body.index(marker)
        return json.loads(body[idx + len(marker):].split("\n\n")[0])

    def test_happy_path_streams_a_done_frame_with_the_full_reply(self):
        self.app_config.llm_helper = _StreamingFakeLlm("Hello from the model")
        client = self._client_with_state("stream-happy")

        resp = client.post("/chatbot/send_stream", json={
            "message": "hi", "allow_without_baseline": True,
        })

        self.assertEqual(resp.status_code, 200)
        payload = self._extract_done_payload(resp.get_data(as_text=True))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["reply"], "Hello from the model")
        self.assertIn("decision_ledger", payload)

    def test_rejects_empty_message(self):
        self.app_config.llm_helper = _StreamingFakeLlm("irrelevant")
        client = self._client_with_state("stream-empty")

        resp = client.post("/chatbot/send_stream", json={"message": ""})

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["success"])

    def test_requires_baseline_unless_explicitly_allowed(self):
        self.app_config.llm_helper = _StreamingFakeLlm("irrelevant")
        client = self._client_with_state("stream-no-baseline")

        resp = client.post("/chatbot/send_stream", json={"message": "hi"})

        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["baseline_required"])

    def test_503_when_llm_is_not_configured(self):
        self.app_config.llm_helper = None
        client = self._client_with_state("stream-no-llm")

        resp = client.post("/chatbot/send_stream", json={
            "message": "hi", "allow_without_baseline": True,
        })

        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
