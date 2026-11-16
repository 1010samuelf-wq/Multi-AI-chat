"""The fact-check loop: two agents take turns until one accepts the other's answer."""

from __future__ import annotations

import asyncio
import re
import time
import uuid

from . import store
from .config import MODELS
from .llm import AgentState, TurnResult

SYSTEM_PROMPT = """You are {name}, one of two AI fact-checkers working on a user's question together. The other one is {other}. You take turns: one writes an answer, the other checks it, and you continue until you both agree the answer is correct. The user reads the whole conversation.

Your tools:
- web_search and web_fetch: use them to verify facts that could be wrong, disputed, or out of date, and to read the actual pages behind a claim.
- code_execution: a Python sandbox (pandas, numpy, openpyxl, xlsxwriter, pypdf, pdfplumber, reportlab, matplotlib, python-docx and more; no internet). Use it for any calculation and for reading or creating Excel and PDF files. Files the user attached are already in the sandbox. To hand a file to the user, save or copy it into $OUTPUT_DIR in the same command, e.g. `cp report.xlsx "$OUTPUT_DIR/"`. Each of you has your own sandbox; mention any file you create so the other can check it.

How to check:
- Check claims, not wording. Verify each important claim (search, fetch, compute) or say it rests on well-established knowledge.
- Never agree just to be polite or to end the debate. Agreeing with a wrong answer is the worst possible outcome.
- Don't invent objections either. If the answer is right, say so.
- Change your position only for a concrete reason: a source, a calculation, or a clear error in logic. If the other side's objection has no evidence, hold your position and explain why.
- Keep it concise.

Format every reply exactly like this:

## Check
Go through the other's latest reply claim by claim: ✅ verified (and how), ❌ wrong (why, with evidence), ⚠️ uncertain or unverifiable. Leave this section out only on the very first answer of the conversation.

## Answer
Your complete current best answer to the user's question. It must stand on its own, because the user may read only this part. Link the sources for key facts.

VERDICT: AGREE  or  VERDICT: DISAGREE

VERDICT: AGREE means the other's latest Answer is correct and complete, and your Answer says the same thing in substance (small wording differences are fine). Use DISAGREE if you corrected, added or removed anything that matters."""

VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(AGREE|DISAGREE|PROPOSED)", re.IGNORECASE)
ANSWER_RE = re.compile(r"##\s*Answer\s*\n(.*?)(?=\n\s*\**VERDICT:|\Z)", re.IGNORECASE | re.DOTALL)


def parse_verdict(text: str) -> str | None:
    matches = VERDICT_RE.findall(text or "")
    return matches[-1].upper() if matches else None


def parse_answer(text: str) -> str:
    m = ANSWER_RE.search(text or "")
    return (m.group(1) if m else text or "").strip()


def file_blocks(files: list[dict]) -> list[dict]:
    """Content blocks that put uploaded files in front of an agent."""
    blocks: list[dict] = []
    for f in files:
        blocks.append({"type": "container_upload", "file_id": f["file_id"]})
        mime = f.get("mime", "")
        if mime == "application/pdf":
            blocks.append({"type": "document", "source": {"type": "file", "file_id": f["file_id"]},
                           "title": f["filename"]})
        elif mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            blocks.append({"type": "image", "source": {"type": "file", "file_id": f["file_id"]}})
    return blocks


def _handoff(other_name: str, result: TurnResult, verb: str) -> str:
    parts = [f"{other_name} {verb}:\n<<<\n{result.text}\n>>>"]
    if result.sources:
        listed = "\n".join(f"- {s['title']}: {s['url']}" for s in result.sources[:25])
        parts.append(f"Sources {other_name} found or cited:\n{listed}")
    if result.files:
        parts.append(f"{other_name} created these files in its own sandbox: "
                     + ", ".join(f["filename"] for f in result.files)
                     + ". You can't open its sandbox; check the method and numbers it describes, "
                       "and recreate the file in yours if you need to verify it.")
    return "\n\n".join(parts)


class Debate:
    def __init__(self, runner, question: str, settings: dict, files: list[dict],
                 user_id: int | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.runner = runner
        self.question = question
        self.settings = settings
        self.files = files
        self.events: list[dict] = []
        self.seq = 0
        self.cond = asyncio.Condition()
        self.status = "running"
        self.cost = 0.0
        self.stop_requested = False
        self.user_notes: dict[str, list[str]] = {"A": [], "B": []}
        self.task: asyncio.Task | None = None
        self.last: TurnResult | None = None

        names = {"A": "Claude A", "B": "Claude B"}
        self.agents = {
            k: AgentState(
                key=k,
                name=names[k],
                model=settings[f"model_{k.lower()}"],
                effort=settings["effort"],
                system=SYSTEM_PROMPT.format(name=names[k], other=names["B" if k == "A" else "A"]),
            )
            for k in ("A", "B")
        }
        self.user_id = user_id
        # The current turn's process, in order (text, thinking, tool use), saved
        # with the turn so a finished chat shows everything that happened.
        self.cur_turn: dict | None = None
        self.cur_steps: list[dict] = []
        store.create_debate(self.id, question, {**settings, "files": files}, user_id)

    # ---- events -------------------------------------------------------------

    # Token-level events are kept in memory for live viewers only; everything
    # else is also saved so finished debates can be replayed from the database.
    LIVE_ONLY = {"delta", "thinking"}

    STEP_TYPES = {"delta", "thinking", "tool_call", "tool_result", "file", "status"}

    async def emit(self, event: dict) -> None:
        event = {**event, "seq": self.seq, "ts": time.time()}
        self.seq += 1
        self.events.append(event)
        if self.cur_turn is not None and event["type"] in self.STEP_TYPES:
            self._record_step(event)
        if event["type"] not in self.LIVE_ONLY:
            store.add_event(self.id, event["seq"], event)
        async with self.cond:
            self.cond.notify_all()

    def _record_step(self, ev: dict) -> None:
        t = ev["type"]
        last = self.cur_steps[-1] if self.cur_steps else None
        if t in ("delta", "thinking"):
            kind = "text" if t == "delta" else "thinking"
            if last and last["kind"] == kind:
                last["text"] += ev["text"]
            else:
                self.cur_steps.append({"kind": kind, "text": ev["text"]})
        else:
            step = {k: v for k, v in ev.items() if k not in ("seq", "ts", "agent")}
            step["kind"] = t
            self.cur_steps.append(step)

    # ---- loop ---------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            await self._finish("stopped", self.last, "Stopped by you.")
        except Exception as e:  # keep the server alive whatever happens
            await self._finish("error", None, f"Unexpected error: {e!r}")

    async def add_note(self, note: str) -> None:
        for k in self.user_notes:
            self.user_notes[k].append(note)
        await self.emit({"type": "user_note", "text": note})

    def stop(self) -> None:
        self.stop_requested = True
        if self.task and not self.task.done():
            self.task.cancel()

    async def _run(self) -> None:
        s = self.settings
        order = [s["first"], "B" if s["first"] == "A" else "A"]
        await self.emit({
            "type": "start",
            "question": self.question,
            "files": self.files,
            "agents": {k: {"name": a.name, "model": a.model, "label": MODELS[a.model]["label"]}
                       for k, a in self.agents.items()},
            "order": order,
            "settings": s,
        })

        last: TurnResult | None = None
        for turn in range(s["max_turns"]):
            if self.stop_requested:
                return await self._finish("stopped", last, "Stopped by you.")
            if self.cost >= s["budget_usd"]:
                return await self._finish("budget", last,
                                          f"Stopped: the spending cap of ${s['budget_usd']:.2f} was reached.")

            key = order[turn % 2]
            agent = self.agents[key]
            other = self.agents[order[(turn + 1) % 2]]
            content = self._turn_content(turn, agent, other, last)

            if turn == 0:
                task = "Answering your question"
            elif turn == 1:
                task = f"Checking {other.name}'s answer"
            else:
                task = f"Checking {other.name}'s reply"
            self.cur_turn, self.cur_steps = {"agent": key, "turn": turn + 1}, []
            await self.emit({"type": "turn_start", "agent": key, "turn": turn + 1,
                             "name": agent.name, "model": agent.model, "task": task})
            result = await self.runner.run_turn(agent, content, self.emit)
            self.cost += result.cost_usd
            verdict = parse_verdict(result.text)
            if turn == 0:
                verdict = "PROPOSED"
            elif verdict not in ("AGREE", "DISAGREE"):
                verdict = "DISAGREE"  # unclear or cut off: keep checking
            await self.emit({
                "type": "turn_end", "agent": key, "turn": turn + 1, "text": result.text,
                "steps": self._take_steps(),
                "verdict": verdict, "sources": result.sources, "files": result.files,
                "turn_cost": result.cost_usd, "total_cost": self.cost, "error": result.error,
            })
            store.update_debate(self.id, cost_usd=self.cost)

            if result.error and not result.text:
                return await self._finish("error", last, result.error)

            last = result
            self.last = result
            if verdict == "AGREE":
                return await self._finish("agreed", result,
                                          f"{agent.name} and {other.name} agree.")

        await self._finish("no_consensus", last,
                           f"No agreement after {s['max_turns']} turns. The last answer is shown; "
                           "read the Check sections for what's still disputed.")

    def _turn_content(self, turn: int, agent: AgentState, other: AgentState,
                      last: TurnResult | None) -> list[dict]:
        blocks: list[dict] = []
        text_parts: list[str] = []
        if not agent.messages:  # this agent's first turn: give it the question and files
            blocks.extend(file_blocks(self.files))
            text_parts.append(f"The user's question:\n{self.question}")
            if self.files:
                text_parts.append("Attached files (also in your sandbox): "
                                  + ", ".join(f["filename"] for f in self.files))
        if turn == 0:
            text_parts.append("You go first. Write your answer (no Check section). "
                              "End with VERDICT: PROPOSED.")
        else:
            verb = "answered" if turn == 1 else "replied"
            text_parts.append(_handoff(other.name, last, verb))
            text_parts.append(f"Check {other.name}'s latest reply and respond in the required format.")
        if self.user_notes[agent.key]:
            notes = "\n".join(f"- {n}" for n in self.user_notes[agent.key])
            text_parts.append(f"The user (the person asking) added a note. Take it into account, "
                              f"but still verify any factual claim in it:\n{notes}")
            self.user_notes[agent.key] = []
        blocks.append({"type": "text", "text": "\n\n".join(text_parts)})
        return blocks

    def _take_steps(self) -> list[dict]:
        steps, self.cur_steps, self.cur_turn = self.cur_steps, [], None
        return steps

    async def _finish(self, status: str, last: TurnResult | None, message: str) -> None:
        if self.cur_turn is not None:  # stopped mid-turn: keep what it did so far
            turn = self.cur_turn
            await self.emit({"type": "turn_end", **turn, "text": "", "steps": self._take_steps(),
                             "verdict": None, "sources": [], "files": [], "turn_cost": 0,
                             "total_cost": self.cost, "error": "Stopped before finishing."})
        self.status = status
        final = parse_answer(last.text) if last else None
        store.update_debate(self.id, status=status, cost_usd=self.cost, final_answer=final)
        await self.emit({"type": "end", "status": status, "message": message,
                         "final_answer": final, "total_cost": self.cost})


class Registry:
    """Debates currently running in this server process."""

    def __init__(self, linger: float = 120):
        self.active: dict[str, Debate] = {}
        self.linger = linger

    def start(self, debate: Debate) -> None:
        self.active[debate.id] = debate

        async def _run():
            try:
                await debate.run()
            finally:
                # Keep it a little while so live viewers get the final events.
                await asyncio.sleep(self.linger)
                self.active.pop(debate.id, None)

        debate.task = asyncio.create_task(_run())

    def get(self, debate_id: str) -> Debate | None:
        return self.active.get(debate_id)
