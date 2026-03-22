# Shared Hooks Architecture

This directory contains the centralized hook system for sf-skills, providing intelligent skill discovery and guardrails across all 19 Salesforce skills.

## Overview

```
shared/hooks/
├── skills-registry.json              # Single source of truth for all skill metadata
├── scripts/
│   ├── guardrails.py                 # PreToolUse hook (block/warn dangerous operations)
│   ├── validator-dispatcher.py       # PostToolUse hook (routes to skill-specific validators)
│   ├── llm-eval.py                   # LLM-powered semantic evaluation (Haiku)
│   ├── session-init.py               # Session initialization hook
│   ├── lsp-prewarm.py                # LSP server pre-warming
│   ├── org-preflight.py              # Org connectivity preflight check
│   ├── api-version-check.py          # API version validation
│   ├── naming_validator.py           # Naming convention enforcement
│   ├── security_validator.py         # Security pattern detection
│   └── stdin_utils.py               # Shared stdin reading utility
├── docs/
│   ├── hook-lifecycle-diagram.md     # Visual lifecycle diagram with all SF-Skills hooks
│   └── hooks-frontmatter-schema.md   # Hook configuration format (legacy reference)
└── README.md                         # This file
```

## Architecture v5.0.0

### Proactive vs Reactive Hooks

```
┌─────────────────────────────────────────────────────────────────────────┐
│ PROACTIVE LAYER                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  User Request → PreToolUse Hook → Block or Warn → Tool Executes         │
│                       ↓                                                 │
│                 guardrails.py                                           │
│                       ↓                                                 │
│        ┌─────────────────────────────────┐                              │
│        │ CRITICAL: Block dangerous DML   │  (6 patterns)                │
│        │ HIGH: (empty — see note below)  │                              │
│        │ MEDIUM: Warn on anti-patterns   │  (4 patterns)                │
│        └─────────────────────────────────┘                              │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ REACTIVE LAYER                                                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Tool Executes → PostToolUse Hook → Validate                            │
│                        ↓                                                │
│              validator-dispatcher.py                                     │
│                        ↓                                                │
│        ┌─────────────────────────────────┐                              │
│        │ Match file_path → run validator │                              │
│        │ .cls → apex-lsp + scoring       │                              │
│        │ .flow-meta.xml → flow-validate  │                              │
│        │ .js (lwc/) → lwc-lsp + scoring  │                              │
│        └─────────────────────────────────┘                              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Hook Types

### 1. PreToolUse (Guardrails)

**Purpose:** Block dangerous operations before execution or warn on anti-patterns.

**Location:** `scripts/guardrails.py`

**Severity Levels:**

| Severity | Action | Count | Examples |
|----------|--------|-------|----------|
| CRITICAL | Block | 6 patterns | DELETE without WHERE, UPDATE without WHERE, hardcoded credentials, production deploy without --dry-run, force push to main, DROP TABLE |
| HIGH | (empty) | 0 | Was unbounded SOQL auto-fix — removed because regex cannot reliably parse SOQL inside shell-quoted strings with pipes. The sf-soql skill handles LIMIT enforcement instead. |
| MEDIUM | Warn | 4 patterns | Hardcoded Salesforce IDs, deprecated `sfdx` usage, old API versions (<v56), SOQL without USER_MODE |

**How it works:**
```python
# Returns JSON to block or warn
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",        # or "allow"
    "permissionDecisionReason": "DELETE without WHERE detected",
    "additionalContext": "..."           # Warnings (for MEDIUM)
  }
}
```

### 2. PostToolUse (Validator Dispatcher)

**Purpose:** Route Write/Edit operations to skill-specific validators based on file patterns.

**Location:** `scripts/validator-dispatcher.py`

**Architecture:** The dispatcher uses a centralized `VALIDATOR_REGISTRY` — a list of `(regex_pattern, skill_name, validator_path)` tuples. When a file is written or edited:

1. Extract `file_path` from the hook's `tool_input`
2. Match against all regex patterns in `VALIDATOR_REGISTRY`
3. Execute matching validators sequentially (8s timeout per validator)
4. Return combined validation output

**Current Registry (15 entries across 7 skills):**

| File Pattern | Skill | Validator |
|-------------|-------|-----------|
| `.agent` | sf-ai-agentscript | agentscript-syntax-validator.py |
| `.cls` | sf-apex | apex-lsp-validate.py + post-tool-validate.py |
| `.trigger` | sf-apex | apex-lsp-validate.py + post-tool-validate.py |
| `.soql` | sf-soql | post-tool-validate.py |
| `.flow-meta.xml` | sf-flow | post-tool-validate.py |
| `/lwc/**/*.js` | sf-lwc | lwc-lsp-validate.py + post-tool-validate.py |
| `/lwc/**/*.html` | sf-lwc | template_validator.py |
| `.object-meta.xml` | sf-metadata | validate_metadata.py |
| `.field-meta.xml` | sf-metadata | validate_metadata.py |
| `.permissionset-meta.xml` | sf-metadata | validate_metadata.py |
| `.namedCredential-meta.xml` | sf-integration | validate_integration.py |

### 3. LLM-Powered Hooks (Haiku)

**Purpose:** Semantic evaluation for complex patterns that can't be detected by regex.

**Location:** `scripts/llm-eval.py`

**Use Cases:**
- Code quality scoring
- Security review (SOQL injection, FLS bypass detection)
- Deployment risk assessment

---

## Skills Registry Schema (v5.0.0)

```json
{
  "version": "5.0.0",
  "guardrails": {
    "dangerous_dml": {
      "patterns": ["DELETE FROM \\w+ (;|$)", "UPDATE \\w+ SET .* (?<!WHERE.*)$"],
      "severity": "CRITICAL",
      "action": "block",
      "message": "Destructive DML without WHERE clause detected"
    }
  },
  "skills": { ... }
}
```

---

## Adding a New Skill

### 1. Add to skills-registry.json

```json
"sf-newskill": {
  "keywords": ["keyword1", "keyword2"],
  "intentPatterns": ["create.*pattern", "build.*pattern"],
  "filePatterns": ["\\.ext$"],
  "priority": "medium",
  "description": "Description of the skill"
}
```

### 2. Add validator to VALIDATOR_REGISTRY

If your skill needs PostToolUse validation, add entries to the `VALIDATOR_REGISTRY` list in `scripts/validator-dispatcher.py`:

```python
# In validator-dispatcher.py VALIDATOR_REGISTRY list:
(
    r"\.yourext$",              # File pattern regex
    "sf-newskill",              # Skill name
    "sf-newskill/hooks/scripts/your-validator.py"  # Validator path (relative to ~/.claude/skills/)
),
```

### 3. Create the validator script

Place your validator at `skills/sf-newskill/hooks/scripts/your-validator.py`. It receives hook context via stdin (JSON) and should output validation results to stdout.

---

## Design Rationale

### Why Proactive + Reactive?

1. **Prevention is better than cure** - Block dangerous operations before damage
2. **User experience** - Warn on common anti-patterns without blocking
3. **Safety net** - PostToolUse catches issues that slip through

### Why Centralized Dispatcher?

1. **No frontmatter parsing** - Validators route by file pattern, not SKILL.md YAML
2. **Single configuration point** - All routing in one `VALIDATOR_REGISTRY` list
3. **Predictable execution** - Sequential with per-validator timeout (8s)
4. **Easy to extend** - Add a tuple to the registry, drop a validator script

### Why Advisory, Not Automatic?

1. **User agency** - Users stay in control of skill invocations
2. **Transparency** - Claude explains why it's suggesting skills
3. **Flexibility** - Users can override suggestions based on context
4. **Claude is smart** - The model follows well-structured suggestions

### Why Single Registry?

1. **DRY** - No duplicate configuration across all sf-* skills
2. **Consistency** - All skills use the same schema
3. **Maintainability** - One place to update skill metadata
4. **Discoverability** - Easy to see all skill relationships

---

## Troubleshooting

### Hook Not Firing

1. Verify the hook scripts exist and are executable:
   ```bash
   /bin/ls -la shared/hooks/scripts/guardrails.py
   /bin/ls -la shared/hooks/scripts/validator-dispatcher.py
   ```

2. Check that the file pattern matches in `VALIDATOR_REGISTRY`:
   ```bash
   python3 -c "
   import re
   pattern = r'\.cls$'
   print(bool(re.search(pattern, 'MyClass.cls')))
   "
   ```

3. Check validator timeout — each validator has 8s; slow LSP servers may need tuning

### Guardrail Too Aggressive

1. Check `CRITICAL_PATTERNS` in `scripts/guardrails.py`
2. Adjust severity or add exceptions for your use case
3. Output-only commands (`echo`, `printf`, heredocs) are automatically excluded

### Validator Not Found

The dispatcher looks for validators at `~/.claude/skills/<skill>/<path>`. Verify:
```bash
/bin/ls ~/.claude/skills/sf-apex/hooks/scripts/
```

---

## License

MIT License. See [LICENSE](../../LICENSE) file.
Copyright (c) 2024-2026 Jag Valaiyapathy
