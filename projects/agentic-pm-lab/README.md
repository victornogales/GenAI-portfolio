# Agentic PM Lab

A hands-on lab for building AI agents the way a product manager should: small, fenced in by plain code, and measured with evals before anyone trusts them.

## The claim

A small agent, fenced in by deterministic code and measured with evals, can judge how well a CV fits a job spec with evidence a recruiter can check. Each build up the ladder has to prove its value on the same evals, not on a demo.

## The ladder

| Build | What it adds | Status | Folder |
|---|---|---|---|
| 0 | Spec qualifier: a single agent that assesses how well a CV fits a job spec | Working, evaluated | [build0_spec_qualifier](build0_spec_qualifier) |

## Build 0: the spec qualifier

Given a CV and a job spec, the agent returns a structured fit assessment: a verdict for every requirement (met, partial or missing), quotes from the CV as evidence, strengths, gaps, a 0-100 score and a recommendation (apply, apply with caveats, or skip).

### Is it an agent or a workflow?

An agent, in a deterministic harness. Claude decides which tools to call, in what order and when it's done; the code never scripts those steps. Everything around that loop is plain Python that sets hard limits: read-only tools, a required output schema, a 25-turn cap and rule-based checks on the answer.

### How it works

```
System prompt ─┐
Job spec ──────┼──▶  Agent loop (agent.py)
CV file ───────┘     ├─ Model (Claude): decides the next step
  (only via tools)   └─ Tools (CVTools): 5 read-only tools
                              │
                              ▼
               FitAssessment ──▶ Hard checks        Call log (LLMLog)
               (schema.py)       (checks.py)        tokens, cost, latency
```

| Component | File | Role |
|---|---|---|
| Agent loop | `build0_spec_qualifier/agent.py` | Sends messages, runs tool calls, stops on a final answer, refusal or turn limit |
| Tools | `build0_spec_qualifier/tools.py` | `list_sections`, `read_section`, `search_cv`, `verify_quote`, `calculate_experience` |
| Output schema | `build0_spec_qualifier/schema.py` | `FitAssessment`, enforced with structured outputs |
| System prompt | `build0_spec_qualifier/prompts/system.md` | How to split the spec, search for evidence and quote exactly |
| Hard checks | `build0_spec_qualifier/checks.py` | 7 rules, e.g. every quote must appear word for word in the CV |
| Call log | `shared/llm_log.py` | Logs tokens, cost, latency and tool calls for every model call (metadata only, never content) |
| Evals | `build0_spec_qualifier/evals/` | Golden cases, the runner and dated reports |

Design choices worth knowing:

- **The CV is only reachable through tools.** Claude has to look up and quote evidence instead of summarising from memory, which is what makes invented claims catchable.
- **Every quote is verified twice.** The agent calls `verify_quote` before using a quote, and `checks.py` rejects any quote that isn't in the CV.
- **Field order in the schema matters.** Per-requirement verdicts come before the score, so the score follows from the evidence.
- **Years of experience are computed, not estimated.** `calculate_experience` merges overlapping roles so time is counted once.

### Evals

Each golden case pairs a CV and a job spec with a hand-checked answer key. The runner runs the agent several times per case and a run passes only if every verdict and importance matches, the recommendation is an accepted one, the score is in range and every hard check passes.

| Report | Model | Result | What changed |
|---|---|---|---|
| [2026-10-29 15:41](build0_spec_qualifier/evals/results/2026-10-29_1541_claude-haiku-5-5.md) | Haiku 5.5 | 2 of 3 runs passed | One run skipped the spec's "Nice to have" section |
| [2026-10-29 15:47](build0_spec_qualifier/evals/results/2026-10-29_1547_claude-haiku-5-5.md) | Haiku 5.5 | 5 of 5 runs passed, 40/40 verdicts | System prompt and schema now say every requirement counts |

A Haiku 5.5 run costs about $0.004 and takes 30 to 50 seconds. Real-world cases (14 real job specs) are kept private and are being given answer keys; until a case has one, the runner skips it.

## Running it

The API key lives in the `.env` at the root of GenAI-portfolio and `python-dotenv` finds it from here. See `.env.example` for the variables. From this folder:

```
uv sync
```

Assess a CV against a job spec (Markdown or plain text):

```
uv run python -m build0_spec_qualifier.agent --cv data_public/demo_cv.md --spec data_public/demo_spec.md --model claude-haiku-5-5
```

Run the evals:

```
uv run python -m build0_spec_qualifier.evals.run_evals --model claude-haiku-5-5 --repeats 3
```

| Option | Applies to | Default |
|---|---|---|
| `--model` | both | `claude-opus-5-5` |
| `--effort` (`low` to `max`) | both | `high` |
| `--cv`, `--spec` | agent | required |
| `--case` (file name pattern) | evals | all cases |
| `--repeats` | evals | 1 |

## Layout

```
agentic-pm-lab/
├── shared/llm_log.py              call logger used by every build
├── build0_spec_qualifier/
│   ├── agent.py  tools.py  schema.py  checks.py
│   ├── prompts/system.md
│   └── evals/
│       ├── run_evals.py
│       ├── golden/                answer keys (only demo_* files are committed)
│       └── results/               dated eval reports
├── data_public/                   synthetic demo CV and job spec
└── data_private/                  real CV, specs and runs (never committed)
```

## Privacy

Real CVs and job specs stay on the machine. `data_private/`, non-demo golden files, eval reports that quote real specs (`private_*`) and the call log are all gitignored, and the call log records numbers only, never prompts or responses.
