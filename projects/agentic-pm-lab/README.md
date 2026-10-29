# Agentic PM Lab

## The claim

_TODO: the one-sentence claim this lab sets out to prove._

## The ladder

| Build | What it adds | Folder |
|---|---|---|
| 0 | Spec qualifier: assesses how well a CV fits a job spec | [build0_spec_qualifier](build0_spec_qualifier) |

## Layout

- `shared/`: code used by every build. `llm_log.py` wraps each model call and records tokens, cost, latency and tool use.
- `build0_spec_qualifier/`: the first build (agent loop, tools, schema, prompts, hard checks, evals).
- `data_public/`: synthetic CV and specs for the public demo. Committed.
- `data_private/`: your real CV and specs. Never committed.

## Setup

The API key lives in the `.env` at the root of GenAI-portfolio, and `python-dotenv` finds it from here, so no project-level `.env` is needed. See `.env.example` for the variables.

From this folder:

```
uv sync
uv run python -m build0_spec_qualifier.agent
uv run python -m build0_spec_qualifier.evals.run_evals
```
