"""textgen LLM adapter (OpenAI-compatible localhost API) for LiveKit."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from livekit.agents import APIConnectOptions, llm

logger = logging.getLogger("local-llm")


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
        try:
            text = await asyncio.to_thread(owner.complete, messages)
        except Exception as exc:
            logger.error("textgen request failed: %s", exc)
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
    def __init__(self, *, base_url: str, api_key: str, model: str, on_reply=None):
        super().__init__()
        import openai

        self._client = openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
        self._model_name = model
        self.on_reply = on_reply
        logger.info("textgen LLM adapter ready (full-reply mode, model=%s)", model)

    @property
    def model(self) -> str:
        return self._model_name

    def complete(self, messages: list[dict]) -> str:
        r = self._client.chat.completions.create(
            model=self._model_name, messages=messages, temperature=0.7, max_tokens=512
        )
        return (r.choices[0].message.content or "").strip()

    def chat(self, *, chat_ctx, tools=None, conn_options=None):
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
        except Exception as exc:
            logger.warning("relay stream ended (%s)", exc)
            if self._sent and publish is not None:
                publish(self._sent, self._msg_id, True)


class RelayLLM(llm.LLM):
    def __init__(self, inner, publish_reply=None):
        super().__init__()
        self._inner = inner
        self.on_reply = publish_reply

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    def chat(self, *, chat_ctx, tools=None, conn_options=None):
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


def relay_llm(inner, publish_reply=None):
    return RelayLLM(inner, publish_reply)


def create_textgen_llm(*, base_url: str, api_key: str, model: str, on_reply=None):
    return TextgenLLM(base_url=base_url, api_key=api_key, model=model, on_reply=on_reply)
