---
name: sf-ai-agentforce-persona
description: >
  Deep persona design for Agentforce agents with 50-point scoring.
  TRIGGER when: user designs agent personas, defines agent personality/identity,
  creates persona documents, encodes persona into Agent Builder fields or
  Agent Script, translates brand guidelines to agent voice, or asks about
  agent tone/voice/register.
  DO NOT TRIGGER when: building agent metadata (use sf-ai-agentforce), testing
  agents (use sf-ai-agentforce-testing), or Agent Script DSL
  (use sf-ai-agentscript).
license: MIT
allowed-tools: Read Write AskUserQuestion Glob Grep
metadata:
  version: "1.3"
  author: "cascadi"
  scoring: "50 points across 5 categories"
  last_validated: "2026-03-10"
  tags: "salesforce, agentforce, persona, identity, register, tone, voice, formality, warmth, personality-intensity, brevity, humor, chatting-style, archetype, brand-input, agent-script-encoding, agent-personality"
---

# Agent Persona Design

Use this skill when the user needs a **defined agent personality**, not implementation details: brand-to-persona translation, tone/voice design, persona documents, sample-dialog refinement, or persona encoding for Agent Builder / Agent Script.

## When This Skill Owns the Task

Use `sf-ai-agentforce-persona` when the work involves:
- defining who the agent is and how it sounds
- converting a brand guide, URL, prompt, or rough description into a persona
- refining register, warmth, humor, brevity, empathy, or other voice attributes
- generating a persona document and example dialogue
- encoding an existing persona into platform-specific fields

Delegate elsewhere when the user is:
- building agent metadata / topics / actions → [sf-ai-agentforce](../sf-ai-agentforce/SKILL.md)
- testing the finished agent → [sf-ai-agentforce-testing](../sf-ai-agentforce-testing/SKILL.md)
- editing `.agent` DSL behavior → [sf-ai-agentscript](../sf-ai-agentscript/SKILL.md)

---

## Required Context to Gather First

Ask for or infer:
- whether the user wants to design a new persona or encode an existing one
- source material available: brand guide, URL, prompt, prior persona doc, or free-text description
- audience / use case if not already implied
- preferred output: persona doc only, scorecard, or encoding guidance

---

## Two Entry Paths

### 1. Design flow
Use when the user provides:
- a brand guide
- a website or company description
- a rough text description
- a prior persona doc that still needs redesign / refinement

### 2. Encode flow
Use when the user provides a completed persona document and asks to turn it into:
- Agent Builder field values
- Agent Script system / topic / message guidance

If ambiguous, ask a single clarifying question: design a new persona, or encode an existing one?

---

## Recommended Workflow

## Design Workflow

The design loop is:
**input → draft → sample dialog → refine → download**

### 1. Accept almost any starting input
Valid inputs include:
- brand guide PDF or text
- URL
- prior persona doc
- free-text description
- existing prompt or `.agent` excerpt

Do not force a long intake if the input already contains enough signal.

### 2. Gather only missing context
Prefer extracting context before asking.
Ask only for what is still unclear, typically:
- internal vs external audience
- at least one use case / JTBD
- agent name if none is obvious

All questions should be skippable.

### 3. Draft from explicit persona signals
Draft around:
- identity traits
- register
- voice attributes
- tone and empathy
- brevity / humor / chatting style
- phrase book
- never-say list
- tone boundaries / tone flex

If no direct evidence exists, use the framework defaults or nearest archetype as a starting point.

### 4. Show sample dialog early
On the first reveal, show:
- **with persona** version
- **without persona** version

This makes the delta obvious. After that, regenerate only the persona version unless the user asks otherwise.

### 5. Refine in two modes
#### Conversational editing
Map requests like “warmer”, “less formal”, “shorter”, or “more personality” to specific attribute shifts.

#### Deterministic editing
If the user asks to see settings, show the attribute table and let them adjust values directly.

### 6. Use diff-based regeneration
After a targeted change:
- hold all unchanged attributes constant
- regenerate only the changed expression
- narrate what changed so the user can see the effect clearly

### 7. Download the persona doc
Write the final document to:
- `_local/generated/[agent-name]-persona.md`

Use:
- [assets/persona-template.md](assets/persona-template.md)

---

## Encode Workflow

Use this when a persona already exists and the user wants platform-ready output.

Gather only encoding-specific context:
- platform: Agent Builder or Agent Script
- company context
- surface / channel
- agent type
- optional topics
- optional actions

Write the encoding output to:
- `_local/generated/[agent-name]-persona-encoding.md`

Use:
- [assets/persona-encoding-template.md](assets/persona-encoding-template.md)

---

## Output Set

This skill can produce up to three Markdown files:
1. persona document
2. scorecard
3. encoding output

Default paths:
- `_local/generated/[agent-name]-persona.md`
- `_local/generated/[agent-name]-persona-scorecard.md`
- `_local/generated/[agent-name]-persona-encoding.md`

---

## Scoring Guidance

Scoring is on-demand, not automatic.

The 50-point rubric focuses on:
- identity coherence
- attribute consistency
- behavioral specificity
- phrase book quality
- sample quality

If a category scores low, explain exactly what to strengthen before encoding.

---

## Cross-Skill Integration

| Need | Delegate to | Reason |
|---|---|---|
| build topics / actions / metadata | [sf-ai-agentforce](../sf-ai-agentforce/SKILL.md) | implementation after persona design |
| encode behavior into `.agent` logic | [sf-ai-agentscript](../sf-ai-agentscript/SKILL.md) | deterministic script authoring |
| validate finished agent behavior | [sf-ai-agentforce-testing](../sf-ai-agentforce-testing/SKILL.md) | post-build testing |

---

## Reference Map

### Start here
- [references/persona-framework.md](references/persona-framework.md)
- [references/persona-encoding-guide.md](references/persona-encoding-guide.md)

### Templates
- [assets/persona-template.md](assets/persona-template.md)
- [assets/persona-encoding-template.md](assets/persona-encoding-template.md)

---

## Score Guide

| Score | Meaning |
|---|---|
| 45–50 | production-ready persona |
| 35–44 | strong foundation, refine before encoding |
| 25–34 | needs revision for coherence |
| < 25 | restart from identity and intent |
