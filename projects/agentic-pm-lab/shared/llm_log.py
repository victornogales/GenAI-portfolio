"""Wraps every LLM call and logs tokens, cost, latency and tool use.

Use LLMLog instead of calling client.messages directly:

    log = LLMLog(run_id="build0-demo")
    response = log.create(label="agent_turn_1", model=..., max_tokens=..., messages=...)
    parsed = log.parse(label="final_answer", output_format=FitAssessment, ...)
    print(log.totals())

Each call appends one JSON line to logs/llm_calls.jsonl. Only metadata is
logged (never prompts or responses), so CV contents stay out of the log.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "llm_calls.jsonl"

# USD per million tokens: (input, output, cache read).
# Anthropic list prices as of 2026-10. Cache writes are billed at 1.25x input.
# Haiku 5.5: cache read assumed at 10% of input, and prompts over 100K tokens
# cost more (0.50 / 2.50), which this table ignores.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(model: str, usage: Any) -> float | None:
    """Cost of one call in USD, or None if the model isn't in PRICES."""
    prices = PRICES.get(model)
    if prices is None:
        return None
    input_price, output_price, cache_read_price = prices
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    total = (
        usage.input_tokens * input_price
        + usage.output_tokens * output_price
        + cache_read * cache_read_price
        + cache_write * input_price * CACHE_WRITE_MULTIPLIER
    )
    return round(total / 1_000_000, 6)


def tool_calls(response: Any) -> list[str]:
    """Names of the tools the model asked to use in this response."""
    return [
        block.name
        for block in response.content
        if block.type in ("tool_use", "server_tool_use")
    ]


class LLMLog:
    def __init__(
        self,
        run_id: str | None = None,
        client: Anthropic | None = None,
        log_path: Path = DEFAULT_LOG_PATH,
    ):
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.client = client or Anthropic()
        self.log_path = Path(log_path)
        self.records: list[dict] = []

    def create(self, label: str = "", **kwargs) -> Any:
        """client.messages.create(**kwargs), logged."""
        return self._call(self.client.messages.create, label, kwargs)

    def parse(self, label: str = "", **kwargs) -> Any:
        """client.messages.parse(**kwargs), logged. Read .parsed_output on the result."""
        return self._call(self.client.messages.parse, label, kwargs)

    def totals(self) -> dict:
        """Sums across every call made through this LLMLog."""
        costs = [r["cost_usd"] for r in self.records if r["cost_usd"] is not None]
        return {
            "run_id": self.run_id,
            "calls": len(self.records),
            "errors": sum(1 for r in self.records if r["error"]),
            "input_tokens": sum(r["input_tokens"] for r in self.records),
            "output_tokens": sum(r["output_tokens"] for r in self.records),
            "cache_read_tokens": sum(r["cache_read_tokens"] for r in self.records),
            "cache_write_tokens": sum(r["cache_write_tokens"] for r in self.records),
            "cost_usd": round(sum(costs), 6),
            "cost_complete": len(costs) == len(self.records),
            "latency_ms": sum(r["latency_ms"] for r in self.records),
            "tool_calls": sum(len(r["tool_calls"]) for r in self.records),
        }

    def _call(self, method, label: str, kwargs: dict) -> Any:
        start = time.perf_counter()
        try:
            response = method(**kwargs)
        except Exception as exc:
            self._write(label, kwargs.get("model", ""), start, error=exc)
            raise
        self._write(label, kwargs.get("model", ""), start, response=response)
        return response

    def _write(self, label: str, requested_model: str, start: float, response=None, error=None):
        latency_ms = round((time.perf_counter() - start) * 1000)
        usage = response.usage if response is not None else None
        # Bill against the model that actually answered (it can differ if a fallback ran).
        model = response.model if response is not None else requested_model
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "label": label,
            "model": model,
            "stop_reason": response.stop_reason if response is not None else None,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "cache_read_tokens": (usage.cache_read_input_tokens or 0) if usage else 0,
            "cache_write_tokens": (usage.cache_creation_input_tokens or 0) if usage else 0,
            "cost_usd": estimate_cost(model, usage) if usage else 0.0,
            "latency_ms": latency_ms,
            "tool_calls": tool_calls(response) if response is not None else [],
            "error": f"{type(error).__name__}: {error}" if error else None,
        }
        self.records.append(record)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
