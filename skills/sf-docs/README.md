# sf-docs

Official Salesforce documentation retrieval skill for `sf-skills`.

## Contract

`sf-docs` is a **core skill** in the suite.

- mandatory with `sf-skills`
- uses **qmd first** when available and the local corpus is ready
- falls back to **Salesforce-aware retrieval** when qmd is missing or weak
- keeps downloaded/scraped docs **local-only**, not in the public repo

## Runtime Modes

### qmd-enabled

- detect qmd
- detect local corpus readiness
- run qmd query first
- accept results only when they are strong enough
- otherwise fall back to targeted official HTML/PDF retrieval

### no-qmd

- classify likely doc family first
- target likely official guide/article roots
- prefer official URLs and PDFs over third-party summaries
- avoid broad crawling during normal question answering

## Quick Start

### Prerequisites

- Python 3.10+
- qmd (optional — enables faster local search; without it, the skill uses Salesforce-aware scraping fallback)

### Try it now (no setup required)

Test sf-docs immediately without any corpus setup:

```bash
python3 skills/sf-docs/scripts/cli.py retrieve \
  --query "System.StubProvider" \
  --mode no_qmd \
  --live-scrape
```

### Full local corpus setup

The pilot corpus covers 7 guides: Apex Developer Guide, Apex Reference,
REST API, Metadata API, Object Reference, LWC, and Agentforce.
See [references/pilot-scope.md](./references/pilot-scope.md) for details.

#### 1. Discover guides

```bash
python3 skills/sf-docs/scripts/cli.py discover \
  --output ~/.sf-docs/manifest/guides.json \
  --pretty
```

Verify: `~/.sf-docs/manifest/guides.json` exists with 7 guides.

#### 2. Sync corpus

```bash
python3 skills/sf-docs/scripts/cli.py sync \
  --download-pdf \
  --normalize
```

Verify: `~/.sf-docs/normalized/md/` contains guide folders (e.g. `apexcode/`, `api_rest/`).

#### 3. Bootstrap qmd (optional)

```bash
python3 skills/sf-docs/scripts/cli.py bootstrap-qmd --embed
```

Verify: `python3 skills/sf-docs/scripts/cli.py status` shows qmd collection indexed.

#### 4. Test retrieval

```bash
python3 skills/sf-docs/scripts/cli.py retrieve \
  --query "Find official Salesforce REST API authentication docs"
```

## CLI Reference

### Check qmd/corpus status

```bash
python3 skills/sf-docs/scripts/cli.py status
```

### Diagnose runtime lookup behavior

```bash
python3 skills/sf-docs/scripts/cli.py diagnose \
  --query "Find official Salesforce REST API authentication docs"
```

### Run end-to-end retrieval (qmd-first mode)

```bash
python3 skills/sf-docs/scripts/cli.py retrieve \
  --query "Find official Salesforce REST API authentication docs" \
  --mode qmd_first
```

### Run no-qmd retrieval for Help article discovery

```bash
python3 skills/sf-docs/scripts/cli.py retrieve \
  --query "Find official Salesforce Help documentation about Messaging for In-App and Web allowed domains, CORS allowlist, and allowed origins." \
  --mode no_qmd
```

### Execute the core benchmark and write results

```bash
python3 skills/sf-docs/scripts/cli.py run-benchmark \
  --benchmark skills/sf-docs/assets/retrieval-benchmark.json \
  --results skills/sf-docs/assets/retrieval-benchmark.results.json
```

### Execute the robustness benchmark

```bash
python3 skills/sf-docs/scripts/cli.py run-benchmark \
  --benchmark skills/sf-docs/assets/retrieval-benchmark.robustness.json \
  --results skills/sf-docs/assets/retrieval-benchmark.robustness.results.json
```

### Score retrieval benchmark results

```bash
python3 skills/sf-docs/scripts/cli.py score-benchmark \
  --benchmark skills/sf-docs/assets/retrieval-benchmark.json \
  --results skills/sf-docs/assets/retrieval-benchmark.results.json
```

> See [references/cli-workflow.md](./references/cli-workflow.md) for the recommended operator sequence.

## Key References

- [SKILL.md](./SKILL.md)
- [references/local-corpus-layout.md](./references/local-corpus-layout.md)
- [references/discovery-manifest.md](./references/discovery-manifest.md)
- [references/qmd-integration.md](./references/qmd-integration.md)
- [references/runtime-workflow.md](./references/runtime-workflow.md)
- [references/ingestion-workflow.md](./references/ingestion-workflow.md)
- [references/salesforce-scraper-techniques.md](./references/salesforce-scraper-techniques.md)
- [references/pilot-scope.md](./references/pilot-scope.md)
- [references/benchmark-protocol.md](./references/benchmark-protocol.md)
- [references/cli-workflow.md](./references/cli-workflow.md)
