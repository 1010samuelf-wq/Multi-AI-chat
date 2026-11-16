# Multi-AI Chat: fact-check with two Claudes

Ask a question once. Two Claude models take turns: one answers, the other checks the answer claim by claim (with web search, by reading the actual pages, and by running code), and they go back and forth until one accepts the other's answer. You watch the whole conversation live.

- **You choose who answers first** (Claude A or Claude B) and which model each one is. The default is Opus 5.5 for A and Sonnet 5.5 for B.
- **Tools**, all run by Anthropic: web search, web fetch (reading a page), and a Python sandbox with pandas, openpyxl, pypdf, pdfplumber, reportlab and more.
- **Files**: upload PDF, Excel, CSV or images. Files the AIs create (Excel, PDF, charts) appear as download links.
- **Agreement rules** that discourage polite agreement: every reply has a claim-by-claim *Check*, a full *Answer*, and a `VERDICT: AGREE/DISAGREE`. An AI may only change its position for a concrete reason (a source, a calculation, a logic error).
- **Limits**: maximum turns and a spending cap for each question. The cost is shown live.
- **Notes**: type a note during a debate and both AIs get it on their next turn.
- **History**: past debates are saved and can be reopened.
- **Password** protection for the whole site.

## How a debate runs

```
Turn 1  Claude A (or B, your choice) answers         → "Proposed answer"
Turn 2  the other checks it, gives its own answer    → AGREE ends it, DISAGREE continues
Turn 3  the first one checks that reply               → AGREE ends it, DISAGREE continues
...     until someone says AGREE, the turn limit, or the spending cap
```

Each AI keeps its own conversation and its own sandbox. It sees the other's reply text and the sources it cited, and verifies them itself.

## Deploy on Fly.io

You need the [Fly CLI](https://fly.io/docs/flyctl/install/) and an API key from the [Claude Console](https://platform.claude.com) (**Get API key**).

1. Create the app from this folder (choose a name and region, and answer **no** to deploying now):

   ```bash
   fly launch --copy-config --no-deploy
   ```

2. Create the storage volume for saved debates (use the same region you chose):

   ```bash
   fly volumes create debate_data --size 1 --region iad
   ```

3. Store your secrets. They're encrypted on Fly, never in the code:

   ```bash
   fly secrets set ANTHROPIC_API_KEY=sk-ant-... APP_PASSWORD=choose-a-password
   ```

4. Deploy:

   ```bash
   fly deploy
   ```

5. Open `https://<your-app-name>.fly.dev`. The browser asks for a password: type anything as the username, and your `APP_PASSWORD` as the password.

**Cost on Fly:** one `shared-cpu-1x` machine with 512 MB, always on (so a debate keeps going after you close the page), about $4–5 a month, plus about $0.15 a month for the 1 GB volume.

## Run it on your computer

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080. To try the interface without spending credit, start it with `MOCK_LLM=1` and the AIs give fake answers.

## Settings

| Environment variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key (required unless `MOCK_LLM=1`) |
| `APP_PASSWORD` | Password for the site. Leave empty only on your own computer. |
| `DATA_DIR` | Where the debate database is stored (`/data` on Fly) |
| `MOCK_LLM` | `1` = demo mode with fake answers, no API calls |

Model prices used for the cost display are in `app/config.py`.

## Costs

Prices from Anthropic:

- Opus 5.5: $4 per million input tokens, $20 per million output tokens. Sonnet 5.5: $2 / $10.
- Web search: $10 per 1,000 searches.
- The code sandbox is free when web search is in the same request, which it always is here. Each organization also gets 1,550 free sandbox hours a month.

Prompt caching is on, so the parts of the conversation sent again on later turns are billed at about a tenth of the normal input price.

A typical debate costs a few cents to about a dollar, depending on the models, the number of turns, the searches, and the file sizes. The spending cap stops a debate before it goes over.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests cover the debate loop, and run the Claude code against a simulated API, so they need no key and spend no credit.

## Project layout

```
app/main.py      web server: start/stop debates, live stream, uploads, downloads
app/debate.py    the fact-check loop and the instructions given to both AIs
app/llm.py       calls to the Claude API (streaming, tools, files, cost)
app/store.py     SQLite storage for debates
app/static/      the web page
```
