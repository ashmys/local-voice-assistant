"""textgen LLM adapter (OpenAI-compatible localhost API) for LiveKit."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from livekit.agents import APIConnectOptions, llm

logger = logging.getLogger("local-llm")

# OpenRouter asks cloud clients to identify themselves; every other
# OpenAI-compatible server simply ignores these.
PROVIDER_HEADERS = {
    "HTTP-Referer": "http://127.0.0.1:8090",
    "X-Title": "Local Voice Assistant",
}


def mask_key(key: str) -> str:
    if not key:
        return "(none)"
    return key[:3] + "..." + key[-4:] if len(key) > 9 else "***"


def to_openai_messages(chat_ctx) -> list[dict]:
    msgs = []
    for item in getattr(chat_ctx, "items", None) or []:
        if type(item).__name__ != "ChatMessage":
            continue
        text = (getattr(item, "text_content", None) or "").strip()
        role = getattr(item, "role", "user")
        if role == "assistant" or role == "user" or role == "system" or role == "developer":
            if text:
                msgs.append({"role": str(role), "content": text})
    return msgs


def _error_detail(exc: Exception) -> str:
    """The most useful one-liner we can get out of an openai exception."""
    body = getattr(exc, "body", None)
    if body:
        try:
            if isinstance(body, str):
                return body[:300]
            return json.dumps(body)[:300]
        except Exception:
            pass
    msg = getattr(exc, "message", None) or str(exc)
    return (msg or type(exc).__name__)[:300]


class TextgenStream(llm.LLMStream):
    def __init__(self, owner, *, chat_ctx, tools, conn_options):
        super().__init__(owner, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._request_id = uuid.uuid4().hex[:12]

    async def _run(self) -> None:
        raise RuntimeError("TextgenStream uses _main_task, not _run")

    async def _main_task(self) -> None:
        owner = self._llm
        messages = to_openai_messages(self._chat_ctx)
        if not messages:
            messages = [{"role": "system", "content": "You are a helpful voice assistant."}]
        t0 = time.time()
        # The LLM call runs in its own task. If the user interrupts the turn
        # (this coroutine gets cancelled), we wait briefly for the in-flight
        # answer so the finished reply is still SAVED to the chat log (it is
        # not spoken) - previously interrupted turns silently vanished from
        # the .jsonl. Cancellation itself is always honored afterwards.
        call = asyncio.ensure_future(asyncio.to_thread(owner.complete, messages))
        try:
            # shield(): cancelling THIS coroutine must not cancel the
            # in-flight request itself, otherwise the answer is unreachable.
            text = await asyncio.shield(call)
        except asyncio.CancelledError:
            rescued = None
            try:
                rescued = await asyncio.wait_for(asyncio.shield(call), timeout=8.0)
            except asyncio.TimeoutError:
                call.cancel()
            except Exception as resc_exc:
                call.cancel()
                if owner.on_error is not None:
                    try:
                        owner.on_error(_error_detail(resc_exc))
                    except Exception:
                        pass
            if rescued is not None:
                logger.info("LLM reply ready in %.2fs (%d chars) "
                            "[interrupted - saved to the chat log, not spoken]",
                            time.time() - t0, len(rescued))
                if owner.on_reply is not None:
                    owner.on_reply(rescued, "b" + self._request_id)
            else:
                logger.info("LLM turn interrupted before an answer arrived")
            raise  # honor the interruption
        except Exception as exc:
            logger.error("textgen request failed: %s", _error_detail(exc))
            if owner.on_error is not None:
                try:
                    owner.on_error(_error_detail(exc))
                except Exception:
                    pass
            text = "Sorry, my language model did not answer. Please try again."
        dt = time.time() - t0
        logger.info("LLM reply ready in %.2fs (%d chars)", dt, len(text))
        if owner.on_reply is not None:
            owner.on_reply(text, "b" + self._request_id)
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id="b" + self._request_id,
                delta=llm.ChoiceDelta(role="assistant", content=text),
            )
        )


class TextgenLLM(llm.LLM):
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 on_reply=None, on_error=None):
        super().__init__()
        import openai

        # max_retries=2: a one-off 500 or connection reset from oobabooga/
        # llama.cpp no longer kills the turn. timeout=30: a hung backend must
        # not stall the conversation forever.
        self._client = openai.OpenAI(
            base_url=base_url, api_key=api_key, max_retries=2, timeout=30.0,
            default_headers=PROVIDER_HEADERS,
        )
        self._model_name = model
        self.on_reply = on_reply
        self.on_error = on_error
        logger.info("textgen LLM adapter ready (full-reply mode, model=%s)", model)

    @property
    def model(self) -> str:
        return self._model_name

    def complete(self, messages: list[dict]) -> str:
        r = self._client.chat.completions.create(
            model=self._model_name, messages=messages, temperature=0.7, max_tokens=1024
        )
        return (r.choices[0].message.content or "").strip()

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        return TextgenStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options or APIConnectOptions(),
        )


class RelayStream(llm.LLMStream):
    def __init__(self, owner, inner_stream, *, chat_ctx, tools, conn_options):
        super().__init__(owner, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._inner = inner_stream
        self._msg_id = "b" + uuid.uuid4().hex[:12]
        self._sent = ""

    async def _run(self) -> None:
        raise RuntimeError("RelayStream uses _main_task, not _run")

    async def _main_task(self) -> None:
        publish = self._llm.on_reply
        try:
            async for chunk in self._inner:
                delta = getattr(chunk, "delta", None)
                content = getattr(delta, "content", None) if delta else None
                if content:
                    self._sent += content
                    if publish is not None:
                        publish(self._sent, self._msg_id, False)
                self._event_ch.send_nowait(chunk)
            if self._sent and publish is not None:
                publish(self._sent, self._msg_id, True)
        except asyncio.CancelledError:
            # The user interrupted the reply - still keep what was generated
            # so the saved conversation is not missing this turn entirely.
            if self._sent and publish is not None:
                publish(self._sent, self._msg_id, True)
            raise
        except Exception as exc:
            detail = _error_detail(exc)
            logger.warning("relay stream ended (%s)", detail)
            on_error = getattr(self._llm, "on_error", None)
            if on_error is not None and not self._sent:
                try:
                    on_error(detail)
                except Exception:
                    pass
            if self._sent and publish is not None:
                publish(self._sent, self._msg_id, True)


class RelayLLM(llm.LLM):
    def __init__(self, inner, publish_reply=None, on_error=None):
        super().__init__()
        self._inner = inner
        self.on_reply = publish_reply
        self.on_error = on_error

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        inner_stream = self._inner.chat(
            chat_ctx=chat_ctx, tools=tools, conn_options=conn_options
        )
        return RelayStream(
            self,
            inner_stream,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options or APIConnectOptions(),
        )


def relay_llm(inner, publish_reply=None, on_error=None):
    return RelayLLM(inner, publish_reply, on_error)


def create_textgen_llm(*, base_url: str, api_key: str, model: str,
                       on_reply=None, on_error=None):
    # Empty key is fine for servers without auth (LM Studio, Ollama,
    # llama.cpp); the openai client just needs a non-empty placeholder.
    return TextgenLLM(base_url=base_url, api_key=api_key or "sk-no-key",
                      model=model, on_reply=on_reply, on_error=on_error)
