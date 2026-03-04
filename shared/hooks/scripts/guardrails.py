#!/usr/bin/env python3
"""
PreToolUse Guardrails Hook for sf-skills (v4.0)

BLOCKING + AUTO-FIX guardrails that run BEFORE dangerous operations execute.
This hook implements three severity levels:

CRITICAL (BLOCK):
- DELETE FROM without WHERE clause
- UPDATE without WHERE clause
- Hardcoded credentials/API keys in commands
- Deploy to production without --checkonly/--dry-run

HIGH (AUTO-FIX):
- Production deploy → Add --dry-run flag
- Missing sharing keyword → Suggest fix

MEDIUM (WARN):
- Hardcoded Salesforce IDs
- Deprecated API usage

Output Format (PreToolUse):
{
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny" | "allow",
        "permissionDecisionReason": "...",
        "updatedInput": { ... },  # For auto-fix
        "additionalContext": "..."  # Warnings
    }
}

Usage:
Add to .claude/hooks.json:
{
    "hooks": {
        "PreToolUse": [{
            "matcher": "Bash|mcp__salesforce",
            "hooks": [{
                "type": "command",
                "command": "python3 ./shared/hooks/scripts/guardrails.py",
                "timeout": 5000
            }]
        }]
    }
}

Supports both Bash (sf CLI) and Salesforce MCP server tool calls.
For MCP tools, all string values in tool_input are scanned for dangerous patterns.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

try:
    from stdin_utils import read_stdin_safe
except ImportError:
    def read_stdin_safe(timeout_seconds=0.1):
        if sys.stdin.isatty():
            return {}
        try:
            return json.load(sys.stdin)
        except Exception:
            return {}

# Configuration
SCRIPT_DIR = Path(__file__).parent.parent
REGISTRY_FILE = SCRIPT_DIR / "skills-registry.json"

# Severity levels
CRITICAL = "CRITICAL"
HIGH = "HIGH"
MEDIUM = "MEDIUM"

# =============================================================================
# CRITICAL PATTERNS (BLOCK)
# =============================================================================

CRITICAL_PATTERNS = [
    # DELETE without WHERE - catastrophic data loss
    {
        "pattern": r"DELETE\s+FROM\s+\w+\s*(;|$|--)",
        "message": "DELETE without WHERE clause detected - this will delete ALL records",
        "suggestion": "Add a WHERE clause: DELETE FROM Object WHERE Id = 'xxx'",
        "context": "soql_dml"
    },
    # UPDATE without WHERE - catastrophic data modification
    {
        "pattern": r"UPDATE\s+\w+\s+SET\s+(?!.*WHERE)",
        "message": "UPDATE without WHERE clause detected - this will update ALL records",
        "suggestion": "Add a WHERE clause: UPDATE Object SET Field = 'value' WHERE Id = 'xxx'",
        "context": "soql_dml"
    },
    # Hardcoded API keys/secrets in commands
    {
        "pattern": r"(?:api[_-]?key|secret|password|token)\s*[=:]\s*['\"][a-zA-Z0-9]{16,}['\"]",
        "message": "Hardcoded credentials detected - use environment variables",
        "suggestion": "Use: export API_KEY='...' && sf command --api-key $API_KEY",
        "context": "security"
    },
    # Production deploy without checkonly
    {
        "pattern": r"sf\s+(?:project\s+)?deploy\s+(?:start|preview)?.*--target-org\s+(?:prod|production)[^-]*$",
        "message": "Production deployment without --dry-run or --check-only - dangerous!",
        "suggestion": "Add --dry-run flag for validation first",
        "context": "deploy"
    },
    # Force push to main/master
    {
        "pattern": r"git\s+push\s+(?:--force|-f)\s+(?:origin\s+)?(?:main|master)",
        "message": "Force push to main/master detected - this can destroy history",
        "suggestion": "Use --force-with-lease for safer force push, or push to a branch",
        "context": "git"
    },
    # Drop table/database
    {
        "pattern": r"DROP\s+(?:TABLE|DATABASE)\s+",
        "message": "DROP TABLE/DATABASE detected - destructive operation",
        "suggestion": "Consider using DELETE with backup instead",
        "context": "soql_dml"
    },
]

# =============================================================================
# HIGH PATTERNS (AUTO-FIX)
# =============================================================================

HIGH_PATTERNS = [
    # NOTE: Unbounded SOQL auto-fix removed — regex cannot reliably parse
    # SOQL inside shell-quoted strings with pipes. The greedy .+ pattern
    # matched past pipe boundaries, appending "LIMIT 200" as jq file args.
    # Claude's sf-soql skill already adds LIMIT to queries when appropriate.
    #
]

# =============================================================================
# MEDIUM PATTERNS (WARN)
# =============================================================================

MEDIUM_PATTERNS = [
    # Hardcoded Salesforce IDs
    {
        "pattern": r"['\"](?:001|003|005|006|00D|00e|500|a0[0-9A-Z])[a-zA-Z0-9]{12,15}['\"]",
        "message": "Hardcoded Salesforce ID detected - IDs vary between environments",
        "suggestion": "Use dynamic queries or Named Credentials instead of hardcoded IDs",
        "context": "salesforce"
    },
    # Deprecated sf commands
    {
        "pattern": r"\bsfdx\b",
        "message": "Deprecated SFDX command detected",
        "suggestion": "Use 'sf' commands instead of 'sfdx' (e.g., sf org display, sf project deploy start)",
        "context": "cli"
    },
    # Old API versions
    {
        "pattern": r"--api-version\s+(?:[1-4]\d|5[0-5])\b",
        "message": "Old API version detected (< v56)",
        "suggestion": "Consider using API v66+ for latest features",
        "context": "salesforce"
    },
    # SOQL without USER_MODE
    {
        "pattern": r"SELECT\s+.+FROM\s+\w+(?!.*WITH\s+(?:USER_MODE|SYSTEM_MODE))",
        "message": "SOQL without USER_MODE - consider adding for security",
        "suggestion": "Add 'WITH USER_MODE' for proper CRUD/FLS enforcement",
        "context": "soql_dml"
    },
]

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def load_registry() -> dict:
    """Load guardrails from skills-registry.json if available."""
    try:
        if REGISTRY_FILE.exists():
            with open(REGISTRY_FILE, 'r') as f:
                registry = json.load(f)
                return registry.get("guardrails", {})
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def is_sf_mcp_tool(tool_name: str) -> bool:
    """Check if the tool is a Salesforce MCP server tool."""
    return "salesforce" in tool_name.lower() or tool_name.startswith("mcp__salesforce")


def extract_checkable_text(tool_name: str, tool_input: dict) -> str:
    """Extract text to check for dangerous patterns.

    For Bash: returns the command string.
    For Salesforce MCP tools: returns all string values from tool_input
    concatenated, so the same pattern checks apply.
    """
    if tool_name == "Bash":
        return tool_input.get("command", "")

    # For MCP tools, concatenate all string values from the input
    parts = []
    for value in tool_input.values():
        if isinstance(value, str) and len(value) > 3:
            parts.append(value)
    return " ".join(parts)


def is_output_only_command(command: str) -> bool:
    """
    Check if command is just outputting/printing text (not executing DML).

    These commands should NOT be blocked even if they contain DML-like patterns,
    because they're just displaying text, not actually executing operations.
    """
    # Commands that just output text
    output_patterns = [
        r'^\s*echo\s+',           # echo "DELETE FROM..."
        r'^\s*printf\s+',         # printf "DELETE FROM..."
        r'^\s*cat\s*<<',          # cat <<EOF / heredoc
        r'^\s*print\s+',          # print (some shells)
        r"^\s*cat\s+['\"]",       # cat "file" (reading, not executing)
    ]
    return any(re.search(p, command, re.IGNORECASE) for p in output_patterns)


def is_sf_context(command: str) -> bool:
    """Check if command is Salesforce-related."""
    sf_indicators = [
        r'\bsf\b', r'\bsfdx\b', r'SELECT\s+', r'DELETE\s+FROM', r'UPDATE\s+\w+\s+SET',
        r'force-app', r'\.cls\b', r'\.trigger\b', r'\.flow-meta', r'scratch\s*org',
        r'--target-org', r'--source-org', r'apex\s+run', r'data\s+query'
    ]
    return any(re.search(p, command, re.IGNORECASE) for p in sf_indicators)


def check_critical(command: str) -> Optional[Dict[str, Any]]:
    """Check for CRITICAL patterns that should be BLOCKED."""
    for rule in CRITICAL_PATTERNS:
        if re.search(rule["pattern"], command, re.IGNORECASE):
            return {
                "severity": CRITICAL,
                "action": "block",
                "message": rule["message"],
                "suggestion": rule["suggestion"],
                "context": rule.get("context", "general")
            }
    return None


def check_high_and_fix(command: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Check for HIGH patterns and return auto-fixed command if applicable."""
    for rule in HIGH_PATTERNS:
        if re.search(rule["pattern"], command, re.IGNORECASE):
            # Apply the fix
            fixed_command = re.sub(
                rule["fix_pattern"],
                rule["replacement"],
                command,
                flags=re.IGNORECASE
            )
            # Only return if we actually changed something
            if fixed_command != command:
                return (fixed_command, {
                    "severity": HIGH,
                    "action": "auto_fix",
                    "message": rule["message"],
                    "original": command,
                    "fixed": fixed_command,
                    "context": rule.get("context", "general")
                })
    return None


def check_medium(command: str) -> list[Dict[str, Any]]:
    """Check for MEDIUM patterns that should generate warnings."""
    warnings = []
    for rule in MEDIUM_PATTERNS:
        if re.search(rule["pattern"], command, re.IGNORECASE):
            warnings.append({
                "severity": MEDIUM,
                "action": "warn",
                "message": rule["message"],
                "suggestion": rule["suggestion"],
                "context": rule.get("context", "general")
            })
    return warnings


def format_block_message(issue: dict) -> str:
    """Format a user-friendly block message."""
    lines = [
        f"\n{'═' * 60}",
        f"🛑 BLOCKED: {issue['message']}",
        f"{'═' * 60}",
        "",
        f"📛 Severity: {issue['severity']}",
        f"💡 Suggestion: {issue['suggestion']}",
        "",
        f"{'─' * 60}",
        "⚠️  This operation was blocked for safety.",
        "    If this is intentional, modify your command to be more specific.",
        f"{'═' * 60}\n"
    ]
    return "\n".join(lines)


def format_autofix_message(issue: dict) -> str:
    """Format a user-friendly auto-fix message."""
    lines = [
        f"\n{'═' * 60}",
        f"🔧 AUTO-FIX APPLIED: {issue['message']}",
        f"{'═' * 60}",
        "",
        f"📝 Original: {issue['original'][:80]}{'...' if len(issue['original']) > 80 else ''}",
        f"✅ Fixed:    {issue['fixed'][:80]}{'...' if len(issue['fixed']) > 80 else ''}",
        "",
        f"{'═' * 60}\n"
    ]
    return "\n".join(lines)


def format_warnings(warnings: list[dict]) -> str:
    """Format user-friendly warning messages."""
    if not warnings:
        return ""

    lines = [
        f"\n{'─' * 54}",
        "⚠️  GUARDRAIL WARNINGS",
        f"{'─' * 54}"
    ]

    for w in warnings:
        lines.append(f"• {w['message']}")
        lines.append(f"  💡 {w['suggestion']}")

    lines.append(f"{'─' * 54}\n")
    return "\n".join(lines)


# =============================================================================
# MAIN HOOK LOGIC
# =============================================================================

def main():
    """Main entry point for the PreToolUse hook."""
    # Read hook input from stdin with timeout to prevent blocking
    input_data = read_stdin_safe(timeout_seconds=0.1)
    if not input_data:
        # No input - allow by default
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        sys.exit(0)

    # Get the tool name and input
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # Only process Bash commands and Salesforce MCP tool calls
    if tool_name != "Bash" and not is_sf_mcp_tool(tool_name):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        sys.exit(0)

    # Extract text to check from tool input (handles both Bash and MCP)
    command = extract_checkable_text(tool_name, tool_input)
    if not command:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        sys.exit(0)

    # For Bash, check if this is SF-related (skip guardrails for non-SF commands)
    # MCP SF tools are inherently SF-related, so skip this check for them
    if tool_name == "Bash" and not is_sf_context(command):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        sys.exit(0)

    # Skip guardrails for output-only commands (echo, printf, etc.)
    # Only applies to Bash — MCP tools always execute
    if tool_name == "Bash" and is_output_only_command(command):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        sys.exit(0)

    # Check for CRITICAL issues (BLOCK)
    critical_issue = check_critical(command)
    if critical_issue:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": critical_issue["message"],
                "additionalContext": format_block_message(critical_issue)
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # Check for HIGH issues (AUTO-FIX)
    fix_result = check_high_and_fix(command)
    if fix_result:
        fixed_command, issue = fix_result
        # Create modified input
        modified_input = dict(tool_input)
        modified_input["command"] = fixed_command

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": modified_input,
                "additionalContext": format_autofix_message(issue)
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # Check for MEDIUM issues (WARN)
    warnings = check_medium(command)
    if warnings:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": format_warnings(warnings)
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # No issues found - allow
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow"
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
