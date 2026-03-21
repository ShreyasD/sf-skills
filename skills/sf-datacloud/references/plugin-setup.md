# Data Cloud Community Plugin Setup

The sf-datacloud family uses a **community `sf data360` CLI runtime**. sf-skills does not vendor or fork that plugin.

## Why this setup exists

- keeps sf-skills focused on skills, prompts, docs, and templates
- lets the upstream runtime continue to evolve independently
- avoids MCP and keeps execution deterministic

## Prerequisites

- Node.js 18+
- yarn
- Salesforce CLI (`sf`)
- git
- a Data Cloud-enabled org authenticated with `sf org login web -a <alias>`

## Recommended setup path

The command examples below assume the skill is installed under `~/.claude/skills/` via the full Claude Code installer. If you are working from a cloned sf-skills repo instead, run the same scripts from that checkout path.

If you use the Python installer, it can install this optional runtime for you:

```bash
python3 ~/.claude/sf-skills-install.py --with-datacloud-runtime
```

Or use the helper script directly:

```bash
bash ~/.claude/skills/sf-datacloud/scripts/bootstrap-plugin.sh
```

By default it clones the plugin into `~/.sf-community-tools/datacloud/sf-cli-plugin-data360`, installs dependencies, compiles it, and links it into the local Salesforce CLI.

## Manual setup

```bash
git clone https://github.com/gthoppae/sf-cli-plugin-data360.git
cd sf-cli-plugin-data360
yarn install
npx tsc
sf plugins link .
```

## Verification

```bash
sf data360 man
bash ~/.claude/skills/sf-datacloud/scripts/verify-plugin.sh
bash ~/.claude/skills/sf-datacloud/scripts/verify-plugin.sh myorg
```

`sf data360 doctor` is useful, but it is not the only readiness signal. On partially provisioned orgs it can fail even when other read-only Data Cloud commands still work. The helper script treats `doctor` as advisory and falls back to additional smoke checks.

## Output-noise tip

When using linked community plugins, stderr can include warning noise. For normal usage, prefer:

```bash
sf data360 dmo list --all -o myorg 2>/dev/null
sf data360 segment list -o myorg 2>/dev/null
```

## What to do if the plugin is missing

1. run the bootstrap script
2. re-open your shell if `sf` plugin discovery is stale
3. verify with `sf data360 man`
4. only then start live Data Cloud work

## Scope reminder

This setup is for the Data Cloud product family:
- `sf-datacloud`
- `sf-datacloud-connect`
- `sf-datacloud-prepare`
- `sf-datacloud-harmonize`
- `sf-datacloud-segment`
- `sf-datacloud-act`
- `sf-datacloud-retrieve`

For STDM/session tracing/parquet work, use `sf-ai-agentforce-observability` instead of this plugin-focused family.
