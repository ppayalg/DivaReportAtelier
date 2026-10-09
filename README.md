# Diva Atelier AI

Agentic report generation for dress and fashion reporting. You describe a report once as a
**configuration**: its input variables, its sections, where each section gets its facts, the house
style, the quality bar and who approves it. A small team of agents then plans the report, gathers
context, writes each section, scores it, revises what falls short, and routes the result through
review. When people keep correcting the same thing, a feedback agent proposes the configuration
change that fixes it at the source.

It ships with four example configurations (bridal gowns, evening gowns, cocktail dresses, everyday
dresses), a knowledge base to retrieve from, and the dashboard UI. It has **no runtime
dependencies**: standard library only.

## Quick start

Needs Python 3.10 or newer. Runs offline with a deterministic mock model, so no API key is needed.

```bash
git clone <your-repo-url> diva-atelier-ai && cd diva-atelier-ai
make demo                 # writes var/bridal.md
make serve                # dashboard + API on http://127.0.0.1:8000
make check                # validate configs, run all tests, check the frontend, smoke-test the server
```

No `make`? The same commands are in the [Makefile](Makefile); use `PYTHONPATH=src` or
`pip install -e .` to get the `diva-atelier` command.

### Generate a report

```bash
diva-atelier generate configs/bridal.json --vars examples/bridal_vars.json --author priya --out report.md
diva-atelier generate configs/cocktail.json --vars examples/cocktail_vars.json --format html --out report.html
```

Sections with a condition appear or vanish with the inputs. Set `made_to_measure_offered` to `false`
in `examples/bridal_vars.json` and section 3.2 is skipped, with the reason recorded in the report.

### Use a real model

```bash
export DIVA_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-...
diva-atelier generate configs/bridal.json --vars examples/bridal_vars.json --llm anthropic
```

The model defaults to `claude-sonnet-5-5`; override with `DIVA_MODEL`. With a real provider the QA
agent also asks the model to judge each draft, blended with its deterministic checks.

### Close the feedback loop

```bash
diva-atelier insights configs/bridal.json --feedback data/feedback/bridal.json
```

From twelve sample comments and edits this proposes: add `cheap → accessibly priced` to the glossary,
require a sell-through verdict in the Executive Summary, require an actual-vs-target table in Style
Performance, and raise the Collection Overview paragraph limit from 2 to 3. Over HTTP,
`POST /api/configs/bridal/insights/<id>/apply` applies one, bumps the version and resets the
configuration to `Draft` for review.

## How it works

```
variables ─► PlannerAgent ─► ContextAgent ─► WriterAgent ─► QAAgent ─► ApprovalAgent ─► report
  config        (which          (retrieve       (draft +       (score,      (section +       + trace
                sections)       + fallbacks)    glossary)      revise)      global review)
                                                                  ▲   │
                                                                  └───┘ revise until it passes or runs out
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for what each agent guarantees,
[docs/CONFIG_SCHEMA.md](docs/CONFIG_SCHEMA.md) for the configuration format and
[docs/API.md](docs/API.md) for the HTTP API.

Two design rules to know before changing anything:

* **Guardrails live in code, not in prompts.** The glossary, paragraph limits, unresolved
  placeholders and required tables are enforced or checked deterministically, whatever the model does.
* **Failures stay visible.** A section that errors, escalates, or never reaches its quality bar keeps
  a status and a reason, and the report status reflects the worst section.

## Project layout

```
src/diva_atelier/
  schema.py  conditions.py  templating.py     configuration model, validation, {variables}
  agents/    planner context writer qa approval insights orchestrator
  llm/       base mock anthropic_client       provider-neutral interface + two providers
  retrieval/ base memory                      Retriever protocol + offline implementation
  store.py   service.py   server.py  cli.py   persistence, application service, HTTP, CLI
configs/     four example configurations (the same JSON the dashboard saves)
data/        knowledge/*.jsonl and feedback/*.json sample data
examples/    input variables for each configuration
frontend/    the dashboard (single HTML file, served by `serve`)
tests/       70 tests: unit, agent behaviour, HTTP server, Anthropic client, CLI
docs/  scripts/  .github/workflows/ci.yml  Dockerfile  docker-compose.yml
```

## Dashboard

`frontend/index.html` works two ways. Opened as a file, it shows built-in sample data and sends
nothing anywhere. Served by `diva-atelier serve`, it loads configurations from the API and saves edits
back, with the server's validation applied. If `DIVA_API_TOKEN` is set it asks for the token once per tab.

## Docker

```bash
docker compose up --build        # http://127.0.0.1:8000, data in the diva-data volume
```

Copy `.env.example` to `.env` to set the provider, key and token.

## Pushing this to Git

```bash
git init -b main
git add .
git commit -m "Initial commit: Diva Atelier AI"
git remote add origin <your-repo-url>
git push -u origin main
```

CI (`.github/workflows/ci.yml`) then runs on Python 3.10 to 3.13. No license file is included; add
the one you intend to use before publishing.

## Known limits

Be aware of these before relying on it:

* **The mock model is a stand-in, not a language model.** It proves the pipeline and keeps tests
  deterministic. Report quality comes from the model you configure.
* **The Anthropic client is tested against a local stub, not the live API.** Run one real generation
  with your key before depending on it.
* **No user accounts.** `DIVA_API_TOKEN` is one shared secret. Approval requests trust the `user` and
  `role` the caller sends, so put real identity (SSO, a gateway) in front before using approvals for
  anything that matters.
* **The server is `http.server`** with a JSON file store: fine for a team tool or a demo, not for
  multiple instances or heavy traffic. Put TLS and a reverse proxy in front off localhost.
* **Retrieval is lexical.** Implement `Retriever` over your vector database for real semantic search.
* **The dashboard does not yet start generation or approvals,** and its Configuration Insights screen
  shows built-in sample items. Those run through the API and CLI today.
* **Tested here on Python 3.12 only;** CI covers 3.10 to 3.13.
