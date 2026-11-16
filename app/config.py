"""Settings, model list and prices."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "debates.sqlite3"

# Optional password for the whole site (HTTP Basic auth, any username).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# MOCK_LLM=1 runs fake agents so the UI can be tried without spending credit.
MOCK_LLM = os.environ.get("MOCK_LLM", "") == "1"

# USD per million tokens (input, output). Cache writes cost 1.25x input,
# cache reads 0.1x input. Update here if Anthropic changes prices.
MODELS = {
    "claude-opus-5-5": {"label": "Claude Opus 5.5", "input": 4.00, "output": 20.00},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5", "input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5", "input": 0.10, "output": 0.50},
}
WEB_SEARCH_PRICE = 10.00 / 1000  # USD per search

DEFAULTS = {
    "model_a": "claude-opus-5-5",
    "model_b": "claude-sonnet-5-5",
    "first": "A",
    "max_turns": 8,
    "effort": "high",
    "budget_usd": 2.00,
}

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MAX_TURNS_LIMIT = 20
MAX_BUDGET_LIMIT = 20.00
