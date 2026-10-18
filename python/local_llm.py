"""OpenAI-compatible LLM adapter for LiveKit (local textgen, LM Studio, Ollama,
llama.cpp, OpenRouter, Groq, OpenAI, ...).

One unified engine for BOTH modes:

- full-reply mode (LLM_STREAMING=0): waits for the complete answer, then
  forwards it as a single chunk.
- token-streaming mode (LLM_STREAMING=1): opens a true SSE stream and forwards
  every delta the moment it arrives, so the UI shows live text and Piper can
  start speaking early.

The request payload is deliberately MINIMAL (model / messages / stream /
temperature / max_tokens): oobabooga's OpenAI extension and other small local
servers reject extras like ``stream_options`` or ``tools``, which broke the
previous plugin-based streaming path.

Thinking mode (LLM_THINKING=1): ``<think>...</think>`` blocks and OpenRouter/
DeepSeek style ``reasoning`` deltas are captured separately, rendered by the UI
as a collapsible block, kept out of the spoken audio unless
LLM_SPEAK_THINKING=1, and saved into the chat log with the final answer.
"""
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

THINK_START = "<think>"
THINK_END = "</think>"


def mask_key(key: str) -> str:
    if not key:
        return "(none)"
    return key[:3] + "..." + key[-4:] if len(key) > 9 else "***"


def build_messages(chat_ctx) -> list[dict]:
    """OpenAI message list from a LiveKit ChatContext.

    to_provider_format() is what the official plugin uses (it folds the
    agent's instructions in correctly); the manual walk is the fallback for
    older builds where the format call is missing.
    """
    try:
        msgs, _ = chat_ctx.to_provider_format(format="openai")
        if isinstance(msgs, list) and msgs:
            return [dict(m) for m in msgs if isinstance(m, dict) and m.get("content")]
    except Exception:
        pass
    out = []
    for item in getattr(chat_ctx, "items", None) or []:
        if type(item).__name__ != "ChatMessage":
            continue
        text = (getattr(item, "text_content", None) or "").strip()
        role = str(getattr(item, "role", "user"))
        if role in ("assistant", "user", "system", "developer") and text:
            out.append({"role": role, "content": text})
    return out


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


def _partial_marker_len(text: str, marker: str) -> int:
    """Length of the longest suffix of text that is a proper prefix of marker."""
    best = 0
    for k in range(1, min(len(text), len(marker) - 1) + 1):
        if text.endswith(marker[:k]):
            best = k
    return best


class ThinkParser:
    """Incremental ``<think>...</think>`` splitter for streamed text.

    feed() consumes a delta and returns ``(answer_delta, think_delta)``.
    Tags split across chunk boundaries are handled via a small tail buffer.
    When keep=False the thinking text is still parsed (so it never leaks into
    the answer) but immediately discarded.
    """

    def __init__(self, keep: bool = True) -> None:
        self.keep = keep
        self.in_think = False
        self._buf = ""
        self._end = None
        self.think = ""   # accumulated thinking text
        self.answer = ""  # accumulated answer text

    def feed(self, piece: str) -> tuple[str, str]:
        self._buf += piece or ""
        answer_out, think_out = [], []
        while self._buf:
            if self._end is not None:
                idx = self._buf.find(self._end)
                if idx < 0:
                    keep = _partial_marker_len(self._buf, self._end)
                    emit = self._buf[: len(self._buf) - keep] if keep else self._buf
                    if emit and self.keep:
                        think_out.append(emit)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                emit = self._buf[:idx]
                if emit and self.keep:
                    think_out.append(emit)
                self._buf = self._buf[idx + len(self._end):]
                self._end = None
                self.in_think = False
                continue
            idx = self._buf.find(THINK_START)
            if idx >= 0:
                head = self._buf[:idx]
                if head:
                    answer_out.append(head)
                self._buf = self._buf[idx + len(THINK_START):]
                self._end = THINK_END
                self.in_think = True
                continue
            keep = _partial_marker_len(self._buf, THINK_START)
            emit = self._buf[: len(self._buf) - keep] if keep else self._buf
            if emit:
                answer_out.append(emit)
            self._buf = self._buf[len(self._buf) - keep:] if keep else ""
            break
        answer_delta = "".join(answer_out)
        think_delta = "".join(think_out)
        if answer_delta:
            self.answer += answer_delta
        if think_delta and self.keep:
            self.think += think_delta
        elif think_delta:
            think_delta = ""
        return answer_delta, think_delta

    def finish(self) -> tuple[str, str]:
        """Flush at end of stream; returns the final (answer, think) deltas."""
        rest, self._buf = self._buf, ""
        end, self._end = self._end, None
        self.in_think = False
        if not rest:
            return "", ""
        if end is None:
            self.answer += rest
            return rest, ""
        if self.keep:
            self.think += rest
            return "", rest
        return "", ""


class TextgenStream(llm.LLMStream):
    """LLMStream that talks to an OpenAI-compatible endpoint directly."""

    def __init__(self, owner, *, chat_ctx, tools, conn_options):
        super().__init__(owner, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._request_id = uuid.uuid4().hex[:12]

    async def _run(self) -> None:
        raise RuntimeError("TextgenStream uses _main_task, not _run")

    async def _main_task(self) -> None:
        owner = self._llm
        messages = build_messages(self._chat_ctx)
        if not messages:
            messages = [{"role": "system",
                         "content": "You are a helpful voice assistant."}]
        msg_id = "b" + self._request_id
        t0 = time.time()
        if owner.streaming:
            await self._run_streaming(owner, messages, msg_id, t0)
        else:
            await self._run_full(owner, messages, msg_id, t0)

    # ---- token streaming mode -------------------------------------------

    async def _run_streaming(self, owner, messages, msg_id, t0) -> None:
        parser = ThinkParser(keep=owner.thinking)
        reasoning_acc = ""          # OpenRouter/DeepSeek style reasoning field
        last_think_pub = 0.0
        spoke_any = False
        in_think_field = False

        try:
            stream = await owner.aclient.chat.completions.create(
                model=owner.model_name, messages=messages, stream=True,
                temperature=0.7, max_tokens=1024,
            )
            try:
                async for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    # separate reasoning field (when the provider splits it)
                    r = (getattr(delta, "reasoning", None)
                         or getattr(delta, "reasoning_content", None))
                    if isinstance(r, str) and r and owner.thinking:
                        reasoning_acc += r
                        if not in_think_field:
                            in_think_field = True
                        now = time.monotonic()
                        if now - last_think_pub > 0.15:
                            last_think_pub = now
                            if owner.on_think is not None:
                                try:
                                    owner.on_think(reasoning_acc, msg_id, False)
                                except Exception:
                                    pass
                    piece = delta.content if isinstance(delta.content, str) else ""
                    if not piece:
                        continue
                    answer_delta, think_delta = parser.feed(piece)
                    if think_delta and not owner.speak_thinking:
                        now = time.monotonic()
                        if now - last_think_pub > 0.15:
                            last_think_pub = now
                            if owner.on_think is not None:
                                try:
                                    owner.on_think(parser.think, msg_id, False)
                                except Exception:
                                    pass
                    speak = answer_delta
                    if think_delta and owner.speak_thinking:
                        speak = think_delta + speak
                    if speak:
                        spoke_any = True
                        self._event_ch.send_nowait(llm.ChatChunk(
                            id=msg_id,
                            delta=llm.ChoiceDelta(role="assistant", content=speak),
                        ))
                        if owner.on_reply is not None:
                            try:
                                # live text update (think goes separately via
                                # on_think; the final packet carries both)
                                owner.on_reply(parser.answer, msg_id, False, "")
                            except Exception:
                                pass
            finally:
                # the SDK stream needs an explicit aclose on early exit
                try:
                    await stream.close()
                except Exception:
                    pass
        except asyncio.CancelledError:
            # User interrupted: still publish what arrived so the turn is
            # saved to the chat log, then honor the cancellation.
            parser.finish()
            # reasoning-field text joins the <think> text for the record
            if in_think_field and reasoning_acc:
                parser.think = (reasoning_acc + "\n" + parser.think).strip()
            if parser.answer or parser.think:
                if owner.on_reply is not None:
                    try:
                        owner.on_reply(parser.answer, msg_id, True, parser.think)
                    except Exception:
                        pass
                if owner.on_think is not None:
                    try:
                        owner.on_think(parser.think, msg_id, True)
                    except Exception:
                        pass
            logger.info("LLM stream interrupted after %.2fs (%d chars) - saved, "
                        "not spoken", time.time() - t0, len(parser.answer))
            raise
        except Exception as exc:
            detail = _error_detail(exc)
            logger.error("LLM streaming request failed: %s", detail)
            if owner.on_error is not None:
                try:
                    owner.on_error(detail)
                except Exception:
                    pass
            if not spoke_any and not parser.answer:
                self._event_ch.send_nowait(llm.ChatChunk(
                    id=msg_id,
                    delta=llm.ChoiceDelta(
                        role="assistant",
                        content="Sorry, my language model did not answer. "
                                "Please try again."),
                ))
            return

        parser.finish()
        # reasoning-field text joins the <think> text for the record
        if in_think_field and reasoning_acc:
            parser.think = (reasoning_acc + "\n" + parser.think).strip()
        if owner.on_reply is not None:
            try:
                owner.on_reply(parser.answer, msg_id, True, parser.think)
            except Exception:
                pass
        if owner.thinking and owner.on_think is not None:
            try:
                owner.on_think(parser.think, msg_id, True)
            except Exception:
                pass
        logger.info("LLM stream done in %.2fs (%d answer chars, %d think chars)",
                    time.time() - t0, len(parser.answer), len(parser.think))

    # ---- full-reply mode --------------------------------------------------

    async def _run_full(self, owner, messages, msg_id, t0) -> None:
        # The LLM call runs in its own task. If the user interrupts the turn
        # (this coroutine gets cancelled), we wait briefly for the in-flight
        # answer so the finished reply is still SAVED to the chat log (it is
        # not spoken) - previously interrupted turns silently vanished from
        # the .jsonl. Cancellation itself is always honored afterwards.
        call = asyncio.ensure_future(
            asyncio.to_thread(owner.complete_blocking, messages))
        try:
            # shield(): cancelling THIS coroutine must not cancel the
            # in-flight request itself, otherwise the answer is unreachable.
            text = await asyncio.shield(call)
        except asyncio.CancelledError:
            rescued, rescued_think = None, ""
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
                parser = ThinkParser(keep=owner.thinking)
                parser.feed(rescued)
                parser.finish()
                rescued_think = parser.think
                logger.info("LLM reply ready in %.2fs (%d chars) "
                            "[interrupted - saved to the chat log, not spoken]",
                            time.time() - t0, len(rescued))
                if owner.on_reply is not None:
                    owner.on_reply(parser.answer, msg_id, True, rescued_think)
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
            self._event_ch.send_nowait(llm.ChatChunk(
                id=msg_id,
                delta=llm.ChoiceDelta(
                    role="assistant",
                    content="Sorry, my language model did not answer. "
                            "Please try again."),
            ))
            return
        dt = time.time() - t0
        parser = ThinkParser(keep=owner.thinking)
        parser.feed(text)
        parser.finish()
        logger.info("LLM reply ready in %.2fs (%d answer chars, %d think chars)",
                    dt, len(parser.answer), len(parser.think))
        if owner.on_reply is not None:
            owner.on_reply(parser.answer, msg_id, True, parser.think)
        speak = (parser.think if owner.speak_thinking else "") + parser.answer
        if speak:
            self._event_ch.send_nowait(llm.ChatChunk(
                id=msg_id,
                delta=llm.ChoiceDelta(role="assistant", content=speak),
            ))


class TextgenLLM(llm.LLM):
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 streaming: bool = False, thinking: bool = False,
                 speak_thinking: bool = False,
                 on_reply=None, on_think=None, on_error=None):
        super().__init__()
        import openai

        # max_retries=2: a one-off 500 or connection reset from oobabooga/
        # llama.cpp no longer kills the turn. timeout=30: a hung backend must
        # not stall the conversation forever.
        common = dict(base_url=base_url, api_key=api_key or "sk-no-key",
                      max_retries=2, timeout=30.0, default_headers=PROVIDER_HEADERS)
        self._client = openai.OpenAI(**common)
        self._aclient = openai.AsyncClient(**common)
        self._model_name = model
        self.streaming = bool(streaming)
        self.thinking = bool(thinking)
        self.speak_thinking = bool(speak_thinking)
        self.on_reply = on_reply      # (answer, msg_id, final, think)
        self.on_think = on_think      # (think_so_far, msg_id, final)
        self.on_error = on_error      # (detail)
        logger.info("textgen LLM adapter ready (model=%s, %s%s)",
                    model, "token streaming" if streaming else "full-reply mode",
                    ", thinking on" if thinking else "")

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def aclient(self):
        return self._aclient

    def complete_blocking(self, messages: list[dict]) -> str:
        r = self._client.chat.completions.create(
            model=self._model_name, messages=messages, temperature=0.7,
            max_tokens=1024,
        )
        return (r.choices[0].message.content or "").strip()

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        return TextgenStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options or APIConnectOptions(),
        )


def create_textgen_llm(*, base_url: str, api_key: str, model: str,
                       streaming: bool = False, thinking: bool = False,
                       speak_thinking: bool = False,
                       on_reply=None, on_think=None, on_error=None):
    """Empty key is fine for servers without auth (LM Studio, Ollama,
    llama.cpp); the openai client just needs a non-empty placeholder."""
    return TextgenLLM(base_url=base_url, api_key=api_key or "sk-no-key",
                      model=model, streaming=streaming, thinking=thinking,
                      speak_thinking=speak_thinking,
                      on_reply=on_reply, on_think=on_think, on_error=on_error)
