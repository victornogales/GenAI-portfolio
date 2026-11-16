# Job scout

Finds new roles that fit your CV on company job boards, and adds the good ones to the Build 1 pipeline with a fit assessment. It's the "scout" from Build 4, built early as a standalone daily run.

## How it works

A funnel, cheapest step first, so a daily run costs cents:

| Step | What happens | Cost |
|---|---|---|
| 1. Fetch | Every open posting from the companies in the watchlist | Free |
| 2. Skip seen | Postings already judged on an earlier run are skipped | Free |
| 3. Rules | Title and location must match the profile | Free |
| 4. Triage | A Haiku 5.5 call per posting: shortlist or skip, with a score and a reason | ~$0.0003 each |
| 5. Assess | The Build 0 agent on the shortlist, best score first, up to `--max-assess` | ~$0.01 each |
| 6. Add | Roles that pass every hard check and aren't a "skip" go into the pipeline as `saved` | Free |

It's a workflow, not an agent: the code decides the steps and the model only judges inside steps 4 and 5. Job posts are untrusted text from the web, so the triage call has no tools, Build 0's tools only read the CV, and every write is plain code through `build1_mcp_server/store.py`.

Each added role gets the job link, a note with the triage reason and where its spec is saved, and Build 0's fit assessment. Changes are audited as actor `scout` (the assessment as `build0`), plus one `scout_run` audit row per run with its counts.

## Sources

Company job boards on **Greenhouse**, **Lever** and **Ashby**, through their free public JSON APIs, which exist for this kind of use. No keys needed.

LinkedIn and Indeed are deliberately not scraped: their terms forbid it and they block bots. Job-alert emails and UK job-board APIs (Adzuna, Reed) are possible later sources.

## Setup

The configuration lives in `data_private/scout/` (gitignored). A demo copy is in `data_public/scout_demo/`.

**`watchlist.toml`:** the companies to check.

```toml
[[companies]]
name = "Telnyx"
ats = "greenhouse"
slug = "telnyx54"
```

To find a company's board, open one of its job posts. The slug is in the link:

| Link | `ats` |
|---|---|
| `job-boards.greenhouse.io/<slug>/jobs/...` | `greenhouse` |
| `jobs.lever.co/<slug>/...` | `lever` |
| `jobs.ashbyhq.com/<slug>/...` | `ashby` |

Companies on other systems (Workday, SmartRecruiters, their own sites) aren't supported yet.

**`profile.toml`:** what to look for.

| Section | Meaning |
|---|---|
| `[titles] include` / `exclude` | At least one include phrase must appear in the title, and no exclude phrase |
| `[locations] include` / `exclude` | The same for the location. Postings with no location are kept for triage |
| `[triage] profile` | A short summary of you for the triage model. The full CV is used later, by Build 0 |
| `[triage] min_score` | Shortlisted postings scoring below this are skipped |

Matching ignores case and works on whole words, so `uk` doesn't match "Duke".

The API key comes from the `.env` at the root of GenAI-portfolio, as for Build 0.

## Running it

From the `agentic-pm-lab` folder:

```
uv run python -m job_scout.run --dry-run
```

fetches, screens and triages, then writes a report. It changes nothing: no roles added, no postings marked as seen, no assessments.

```
uv run python -m job_scout.run --max-assess 3
```

is a real run. Each run writes a dated report to `data_private/scout/runs/`.

| Option | Default |
|---|---|
| `--dry-run` | off |
| `--max-assess` | 5 Build 0 assessments per run. Shortlisted postings over the cap wait for the next run, without being triaged again |
| `--model` | `claude-haiku-5-5`, for triage and assessment |
| `--config` | `data_private/scout` |
| `--db` | `PIPELINE_DB`, else `data_private/pipeline.db` |
| `--cv` | `CV_PATH`, else `data_private/vnogales-cv-master.md` |

To try it with the demo data:

```
uv run python -m job_scout.run --dry-run --config data_public/scout_demo --cv data_public/demo_cv.md --db data_public/pipeline_demo.db
```

## Reviewing what it found

In Claude Desktop or Claude Code with the Build 1 server connected, use the **`new_leads`** prompt. It lists the roles the scout added in the last 7 days (or the number of days you give), best fit first, with the strongest matches, the most serious gap and a suggested call: keep and apply, keep and research, or archive. It changes nothing until you say which to keep; then it uses `update_stage`, `set_next_action` or `archive_role`.

## Running it every day

On Windows, Task Scheduler can start it each morning. From PowerShell, with your own path:

```
schtasks /Create /TN "Job scout" /SC DAILY /ST 07:30 /TR "C:\Users\<you>\.local\bin\uv.exe --directory C:\path\to\agentic-pm-lab run python -m job_scout.run --max-assess 5"
```

The computer has to be on at that time. Remove it with `schtasks /Delete /TN "Job scout"`.

## Cost

The first run triages every matching posting on the watchlist's boards, so it costs a little more; `--max-assess` caps the expensive step. After that, only new postings are judged: with around 20 new postings a day getting past the rules and up to 5 assessments, a day costs under $0.10. Each report shows the run's actual cost.

## Files

| File | Role |
|---|---|
| `sources.py` | One fetcher per job board, returning a common `Posting` |
| `filters.py` | The title and location rules |
| `triage.py` | The Haiku triage call |
| `run.py` | The funnel, the report and the command line |
| `tests/` | Offline tests: parsers on synthetic board data, the rules, and the whole funnel with fake boards, triage and agent |
