"""Runs one agent turn against the Claude API (or a mock), streaming events.

Each agent keeps its own message history. Server tools (web search, web fetch,
code execution) run on Anthropic's side, so a turn is a single streamed request,
repeated only when the API pauses a long turn (stop_reason "pause_turn").
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import MODELS, WEB_SEARCH_PRICE

Emit = Callable[[dict], Awaitable[None]]

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
    {"type": "code_execution_20260521", "name": "code_execution"},
]

# Server-side fallback: if a model declines a request, the API retries it on a
# suitable fallback model inside the same call. Not offered for Haiku.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

MAX_PAUSE_CONTINUES = 6
TOOL_OUTPUT_PREVIEW = 1500


@dataclass
class AgentState:
    key: str  # "A" or "B"
    name: str  # display name, e.g. "Claude A"
    model: str
    effort: str
    system: str
    messages: list = field(default_factory=list)
    container_id: str | None = None


@dataclass
class TurnResult:
    text: str  # the reply text the other agent will read
    sources: list[dict]  # [{url, title}] cited or found
    files: list[dict]  # [{file_id, filename}] created in the sandbox
    cost_usd: float
    stop_reason: str | None
    error: str | None = None


def turn_cost(model: str, usage) -> float:
    price = MODELS.get(model) or MODELS["claude-opus-5-5"]
    inp, out = price["input"] / 1e6, price["output"] / 1e6
    cost = (usage.input_tokens or 0) * inp
    cost += (getattr(usage, "cache_creation_input_tokens", 0) or 0) * inp * 1.25
    cost += (getattr(usage, "cache_read_input_tokens", 0) or 0) * inp * 0.10
    cost += (usage.output_tokens or 0) * out
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        cost += (getattr(stu, "web_search_requests", 0) or 0) * WEB_SEARCH_PRICE
    return cost


def _short(s: str | None, n: int = TOOL_OUTPUT_PREVIEW) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n… ({len(s) - n} more characters)"


def _reply_text(blocks: list) -> str:
    """Text written after the last tool activity: that's the actual reply.

    Text before tool calls is usually narration ("Let me search for...")."""
    last_tool = -1
    for i, b in enumerate(blocks):
        if b.type not in ("text", "thinking", "redacted_thinking", "fallback"):
            last_tool = i
    tail = "".join(b.text for b in blocks[last_tool + 1 :] if b.type == "text").strip()
    if tail:
        return tail
    return "".join(b.text for b in blocks if b.type == "text").strip()


class ClaudeRunner:
    def __init__(self, api_key: str | None = None):
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)

    async def check_key(self) -> None:
        """Raises if the key is rejected. A free call: it only reads model info."""
        await self.client.models.retrieve("claude-opus-5-5")

    async def run_turn(self, agent: AgentState, user_content: list, emit: Emit) -> TurnResult:
        import anthropic

        agent.messages.append({"role": "user", "content": user_content})
        all_blocks: list = []
        sources: dict[str, str] = {}
        files: list[dict] = []
        cost = 0.0
        stop_reason = None

        for _ in range(MAX_PAUSE_CONTINUES + 1):
            kwargs = dict(
                model=agent.model,
                max_tokens=32000,
                system=[{"type": "text", "text": agent.system}],
                messages=agent.messages,
                tools=TOOLS,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": agent.effort},
                cache_control={"type": "ephemeral"},
            )
            if agent.container_id:
                kwargs["container"] = agent.container_id
            betas = []
            if agent.model in FALLBACK_MODELS:
                betas.append(FALLBACK_BETA)
                kwargs["fallbacks"] = "default"
            if betas:
                kwargs["betas"] = betas

            try:
                async with self.client.beta.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        await self._handle_event(agent, event, emit, sources, files)
                    msg = await stream.get_final_message()
            except anthropic.APIStatusError as e:
                # Undo the user message so a retry starts from a clean history.
                agent.messages.pop()
                return TurnResult("", [], files, cost, None, f"API error {e.status_code}: {e.message}")
            except anthropic.APIConnectionError as e:
                agent.messages.pop()
                return TurnResult("", [], files, cost, None, f"Connection error: {e}")

            cost += turn_cost(msg.model or agent.model, msg.usage)
            if msg.container is not None:
                agent.container_id = msg.container.id
            all_blocks.extend(msg.content)
            agent.messages.append(
                {"role": "assistant", "content": [b.to_dict(mode="json") for b in msg.content]}
            )
            stop_reason = msg.stop_reason
            if stop_reason != "pause_turn":
                break
            await emit({"type": "status", "agent": agent.key, "text": "Still working…"})

        error = None
        if stop_reason == "refusal":
            error = "The model declined this request."
        elif stop_reason == "max_tokens":
            error = "The reply hit the length limit and was cut off."

        return TurnResult(
            text=_reply_text(all_blocks),
            sources=[{"url": u, "title": t} for u, t in sources.items()],
            files=files,
            cost_usd=cost,
            stop_reason=stop_reason,
            error=error,
        )

    async def _handle_event(self, agent, event, emit, sources, files):
        t = event.type
        if t == "text":
            await emit({"type": "delta", "agent": agent.key, "text": event.text})
        elif t == "thinking":
            await emit({"type": "thinking", "agent": agent.key, "text": event.thinking})
        elif t == "content_block_stop":
            await self._handle_block(agent, event.content_block, emit, sources, files)

    async def _handle_block(self, agent, b, emit, sources, files):
        bt = b.type
        if bt == "server_tool_use":
            await emit({"type": "tool_call", "agent": agent.key, "name": b.name, "input": b.input,
                        "id": b.id})
        elif bt == "web_search_tool_result":
            content = b.content
            if isinstance(content, list):
                found = [{"url": r.url, "title": r.title} for r in content]
                for r in found:
                    sources.setdefault(r["url"], r["title"])
                await emit({"type": "tool_result", "agent": agent.key, "name": "web_search",
                            "results": found, "tool_use_id": b.tool_use_id})
            else:
                await emit({"type": "tool_result", "agent": agent.key, "name": "web_search",
                            "error": getattr(content, "error_code", "error"),
                            "tool_use_id": b.tool_use_id})
        elif bt == "web_fetch_tool_result":
            content = b.content
            url = getattr(content, "url", None)
            err = getattr(content, "error_code", None)
            if url:
                sources.setdefault(url, url)
            await emit({"type": "tool_result", "agent": agent.key, "name": "web_fetch", "url": url,
                        "error": err, "tool_use_id": b.tool_use_id})
        elif bt in ("bash_code_execution_tool_result", "code_execution_tool_result"):
            c = b.content
            ctype = getattr(c, "type", "")
            if ctype in ("bash_code_execution_result", "code_execution_result",
                         "encrypted_code_execution_result"):
                new_files = []
                for out in c.content or []:
                    fid = getattr(out, "file_id", None)
                    if fid:
                        new_files.append(await self._file_info(fid))
                files.extend(new_files)
                stdout = getattr(c, "stdout", None)
                await emit({"type": "tool_result", "agent": agent.key, "name": "code",
                            "stdout": _short(stdout) if stdout is not None
                            else "(Anthropic keeps this output private; Claude used it to filter search results.)",
                            "stderr": _short(c.stderr, 600), "return_code": c.return_code,
                            "tool_use_id": b.tool_use_id})
                for f in new_files:
                    await emit({"type": "file", "agent": agent.key, **f})
            else:
                await emit({"type": "tool_result", "agent": agent.key, "name": "code",
                            "error": getattr(c, "error_code", "error"),
                            "tool_use_id": b.tool_use_id})
        elif bt == "text":
            for c in getattr(b, "citations", None) or []:
                url = getattr(c, "url", None)
                if url:
                    sources.setdefault(url, getattr(c, "title", None) or url)
        elif bt == "fallback":
            await emit({"type": "status", "agent": agent.key,
                        "text": "The first model declined; a fallback model continued."})

    async def _file_info(self, file_id: str) -> dict:
        try:
            meta = await self.client.files.retrieve_metadata(file_id)
            return {"file_id": file_id, "filename": meta.filename}
        except Exception:
            return {"file_id": file_id, "filename": file_id}

    async def upload(self, filename: str, data: bytes, mime: str) -> str:
        meta = await self.client.files.upload(file=(filename, data, mime))
        return meta.id

    async def download(self, file_id: str) -> tuple[str, bytes]:
        meta = await self.client.files.retrieve_metadata(file_id)
        resp = await self.client.files.download(file_id)
        return meta.filename, await resp.read()


class MockRunner:
    """Fake agents for trying the app without an API key or credit.

    The second speaker disagrees once, then agrees."""

    async def run_turn(self, agent: AgentState, user_content: list, emit: Emit) -> TurnResult:
        n = len(agent.messages) // 2  # this agent's earlier turns in this debate
        agent.messages.append({"role": "user", "content": user_content})
        first_turn_overall = n == 0 and "answered:" not in str(user_content)

        async def say(kind: str, text: str):
            for i in range(0, len(text), 14):
                await emit({"type": kind, "agent": agent.key, "text": text[i : i + 14]})
                await asyncio.sleep(0.01)

        await say("thinking", "The question needs a source. I'll search first, then check the number with code.")
        await say("delta", "Let me look this up.")
        tag = f"{agent.key}{n}"
        await emit({"type": "tool_call", "agent": agent.key, "name": "code_execution",
                    "input": {"code": "r = await web_search({'query': 'mock search'})\nprint(len(r))"},
                    "id": f"c{tag}"})
        await emit({"type": "tool_call", "agent": agent.key, "name": "web_search",
                    "input": {"query": "mock search"}, "id": f"s{tag}"})
        await asyncio.sleep(0.2)
        found = [{"url": "https://example.com/source", "title": "Example source"}]
        await emit({"type": "tool_result", "agent": agent.key, "name": "web_search", "results": found,
                    "tool_use_id": f"s{tag}"})
        await emit({"type": "tool_result", "agent": agent.key, "name": "code", "stdout": "1",
                    "stderr": "", "return_code": 0, "tool_use_id": f"c{tag}"})
        await emit({"type": "tool_call", "agent": agent.key, "name": "bash_code_execution",
                    "input": {"command": "python -c 'print(6*7 - (0 if " + str(first_turn_overall) + " else 1))'"}})
        await asyncio.sleep(0.2)
        await emit({"type": "tool_result", "agent": agent.key, "name": "code",
                    "stdout": "42" if first_turn_overall else "41", "stderr": "", "return_code": 0})
        if first_turn_overall:
            verdict, body = "PROPOSED", "The answer is **42**, based on the example source."
            check = ""
        elif n == 0:
            verdict, body = "DISAGREE", "The answer is **41**. The other answer misread the source."
            check = "## Check\n- ❌ \"42\" is wrong: the source says 41 ([Example](https://example.com/source)).\n\n"
        else:
            verdict, body = "AGREE", "The answer is **41**."
            check = "## Check\n- ✅ 41 is correct; I re-read the source and confirmed it.\n\n"
        text = f"{check}## Answer\n{body}\n\nVERDICT: {verdict}"
        await say("delta", text)
        agent.messages.append({"role": "assistant", "content": text})
        return TurnResult(text, found, [], round(random.uniform(0.01, 0.05), 4), "end_turn")

    async def upload(self, filename: str, data: bytes, mime: str) -> str:
        return f"file_mock_{random.randint(1000, 9999)}"

    async def download(self, file_id: str) -> tuple[str, bytes]:
        return "mock.txt", b"mock file"
