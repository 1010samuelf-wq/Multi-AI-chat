import asyncio
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

import pytest

from app.debate import Debate, parse_answer, parse_verdict
from app.llm import MockRunner, TurnResult


SETTINGS = {"model_a": "claude-opus-5-5", "model_b": "claude-sonnet-5-5", "first": "A",
            "max_turns": 8, "effort": "high", "budget_usd": 2.0}


def test_parse_verdict_and_answer():
    text = "## Check\n- ✅ ok\n\n## Answer\nParis is the capital.\n\nVERDICT: **AGREE**"
    assert parse_verdict(text) == "AGREE"
    assert parse_answer(text) == "Paris is the capital."
    assert parse_verdict("no verdict here") is None
    assert parse_verdict("VERDICT: AGREE ... later VERDICT: DISAGREE") == "DISAGREE"


async def _run(debate):
    await debate.run()
    return [e for e in debate.events if e["type"] not in ("delta", "thinking")]


@pytest.mark.asyncio
async def test_mock_debate_reaches_agreement():
    d = Debate(MockRunner(), "What is the answer?", dict(SETTINGS), [])
    events = await _run(d)
    turns = [e for e in events if e["type"] == "turn_end"]
    assert [t["agent"] for t in turns] == ["A", "B", "A"]
    assert [t["verdict"] for t in turns] == ["PROPOSED", "DISAGREE", "AGREE"]
    end = events[-1]
    assert end["type"] == "end" and end["status"] == "agreed"
    assert "41" in end["final_answer"]


@pytest.mark.asyncio
async def test_b_can_go_first():
    d = Debate(MockRunner(), "Q", {**SETTINGS, "first": "B"}, [])
    events = await _run(d)
    turns = [e for e in events if e["type"] == "turn_end"]
    assert turns[0]["agent"] == "B"


class NeverAgree:
    async def run_turn(self, agent, content, emit):
        agent.messages.append({"role": "user", "content": content})
        agent.messages.append({"role": "assistant", "content": "x"})
        return TurnResult("## Answer\nNo.\n\nVERDICT: DISAGREE", [], [], 0.10, "end_turn")


@pytest.mark.asyncio
async def test_turn_limit():
    d = Debate(NeverAgree(), "Q", {**SETTINGS, "max_turns": 4}, [])
    events = await _run(d)
    assert len([e for e in events if e["type"] == "turn_end"]) == 4
    assert events[-1]["status"] == "no_consensus"


@pytest.mark.asyncio
async def test_budget_cap():
    d = Debate(NeverAgree(), "Q", {**SETTINGS, "budget_usd": 0.25}, [])
    events = await _run(d)
    assert len([e for e in events if e["type"] == "turn_end"]) == 3  # 0.10 x3 >= 0.25
    assert events[-1]["status"] == "budget"


@pytest.mark.asyncio
async def test_second_agent_sees_first_answer_and_notes():
    seen = {}

    class Spy(NeverAgree):
        async def run_turn(self, agent, content, emit):
            seen.setdefault(agent.key, []).append(content[-1]["text"])
            return await super().run_turn(agent, content, emit)

    d = Debate(Spy(), "Capital of France?", {**SETTINGS, "max_turns": 3}, [])
    await d.add_note("Use official sources")
    await d.run()
    assert "You go first" in seen["A"][0]
    assert "Capital of France?" in seen["B"][0]
    assert "Claude A answered" in seen["B"][0]
    assert "Use official sources" in seen["A"][0] and "Use official sources" in seen["B"][0]
    # The note is delivered once per agent.
    assert "Use official sources" not in seen["A"][1]


@pytest.mark.asyncio
async def test_stop():
    class Slow(NeverAgree):
        async def run_turn(self, agent, content, emit):
            await asyncio.sleep(10)

    d = Debate(Slow(), "Q", dict(SETTINGS), [])
    d.task = asyncio.create_task(d.run())
    await asyncio.sleep(0.05)
    d.stop()
    await d.task
    assert d.events[-1]["status"] == "stopped"
