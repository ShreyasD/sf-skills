#!/usr/bin/env python3
"""
Agent Script Syntax Validator
=============================

PostToolUse hook that validates .agent files for common syntax errors and
high-value production gotchas documented in the sf-ai-agentscript skill.

Checks include:
- Mixed tabs and spaces
- Lowercase booleans (must be True/False)
- Missing required blocks
- Missing/invalid config fields
- Service vs Employee agent rules for default_agent_user
- Org-aware Service Agent user verification against the configured validation org
- Exactly one start_agent
- Duplicate or colliding topic/start_agent names
- Naming rule violations
- Invalid `connections:` wrapper (must use `connection messaging:`)
- Variables declared as both mutable and linked
- Reserved/context-colliding variable names
- Undefined @topic references
- Multiline topic/start_agent description gotcha
- before_reasoning/after_reasoning instructions wrapper gotcha
- Empty list literal expression gotcha
- `@inputs` in `set` gotcha
- Bare action names in `run`
- Action metadata warnings for @utils.transition
- Prompt output `is_displayable: True` gotcha
- `date` type in action I/O runtime gotcha
- is_required advisory hint
- Employee-agent-only / service-agent-only patterns
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class AgentScriptValidator:
    """Validates Agent Script syntax and common production gotchas."""

    NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
    VARIABLE_DECL_PATTERN = re.compile(
        r"^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(mutable|linked)\b",
        re.IGNORECASE,
    )
    BLOCK_NAME_PATTERN = re.compile(r"^(topic|start_agent)\s+([A-Za-z][A-Za-z0-9_]*)\s*:")
    ACTION_DECL_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)$")
    IO_FIELD_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z][A-Za-z0-9_\[\]]*)\s*(?:#.*)?$")

    RESERVED_VARIABLE_NAMES = {
        "Locale",
        "Channel",
        "Status",
        "Origin",
    }

    INVALID_TRANSITION_PROPERTIES = {
        "label",
        "require_user_confirmation",
        "include_in_progress_indicator",
        "progress_indicator_message",
        "is_required",
        "is_user_input",
        "is_displayable",
        "is_used_by_planner",
    }

    TOP_LEVEL_ENDERS = (
        "config:",
        "variables:",
        "system:",
        "knowledge:",
        "language:",
        "connection ",
        "connections:",
        "topic ",
        "start_agent ",
    )

    def __init__(self, content: str, file_path: str):
        self.content = content
        self.file_path = file_path
        self.lines = content.split("\n")
        self.errors: List[Tuple[int, str, str]] = []
        self.warnings: List[Tuple[int, str, str]] = []

        self.config_fields: Dict[str, Tuple[str, int]] = {}
        self.topic_names: Dict[str, int] = {}
        self.start_agent_names: Dict[str, int] = {}
        self.variable_names: Dict[str, int] = {}
        self.defined_topics: Set[str] = set()
        self.connection_messaging_lines: List[int] = []
        self.messaging_linked_var_lines: List[int] = []
        self.action_definitions: List[Dict] = []
        self.multiline_description_issues: List[Tuple[int, str]] = []
        self.lifecycle_instruction_wrappers: List[Tuple[int, str]] = []
        self.action_input_required_flags: List[Tuple[int, str, str]] = []
        self.validation_org = self._resolve_validation_org()
        self._user_query_cache: Dict[str, Dict] = {}
        self._query_cache: Dict[str, Dict] = {}

        self._parse_structure()

    def validate(self) -> dict:
        """Run all validations and return results."""
        self._check_mixed_indentation()
        self._check_boolean_case()
        self._check_required_blocks()
        self._check_config_fields()
        self._check_start_agent_count()
        self._check_name_collisions()
        self._check_naming_rules()
        self._check_invalid_connections_wrapper()
        self._check_mutable_linked_conflict()
        self._check_reserved_variable_names()
        self._check_undefined_variables()
        self._check_undefined_topics()
        self._check_multiline_descriptions()
        self._check_lifecycle_instruction_wrappers()
        self._check_post_action_position()
        self._check_empty_list_literals()
        self._check_inputs_in_set()
        self._check_bare_run_actions()
        self._check_action_metadata_context()
        self._check_prompt_output_displayability()
        self._check_date_type_in_action_io()
        self._check_is_required_advisories()
        self._check_employee_agent_gotchas()
        self._check_service_agent_user_in_org()
        self._check_service_agent_target_permissions()

        return {
            "success": len(self.errors) == 0,
            "errors": self.errors,
            "warnings": self.warnings,
            "file_path": self.file_path,
            "checklist": self._build_checklist(),
        }

    @staticmethod
    def _indent(raw_line: str) -> int:
        expanded = raw_line.expandtabs(4)
        return len(expanded) - len(expanded.lstrip(" "))

    @staticmethod
    def _strip_quotes(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
            return value[1:-1]
        return value

    @staticmethod
    def _parse_validation_org_from_skill(skill_path: Path) -> Optional[str]:
        try:
            content = skill_path.read_text()
        except Exception:
            return None

        frontmatter_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not frontmatter_match:
            return None

        metadata_match = re.search(r'^\s*validation_org:\s*"?([^"\n]+)"?\s*$', frontmatter_match.group(1), re.MULTILINE)
        if metadata_match:
            return metadata_match.group(1).strip()
        return None

    def _resolve_validation_org(self) -> Optional[str]:
        for env_name in ("AGENTSCRIPT_VALIDATION_ORG", "SF_TARGET_ORG", "TARGET_ORG"):
            value = os.environ.get(env_name)
            if value:
                return value.strip()

        skill_path = Path(__file__).resolve().parents[2] / "SKILL.md"
        return self._parse_validation_org_from_skill(skill_path)

    def _effective_agent_type(self) -> Optional[str]:
        agent_type = self.config_fields.get("agent_type")
        default_agent_user = self.config_fields.get("default_agent_user")
        if agent_type:
            return agent_type[0]
        if default_agent_user:
            return "AgentforceServiceAgent"
        return None

    def _run_soql_query(self, soql: str) -> Dict:
        if soql in self._query_cache:
            return self._query_cache[soql]

        if not self.validation_org:
            result = {"ok": False, "reason": "no_validation_org"}
            self._query_cache[soql] = result
            return result

        try:
            proc = subprocess.run(
                ["sf", "data", "query", "--query", soql, "-o", self.validation_org, "--json"],
                text=True,
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            result = {"ok": False, "reason": "query_failed", "detail": str(exc)}
            self._query_cache[soql] = result
            return result

        if proc.returncode != 0:
            detail = (proc.stdout or proc.stderr or "sf data query failed").strip()
            result = {"ok": False, "reason": "query_failed", "detail": detail}
            self._query_cache[soql] = result
            return result

        try:
            payload = json.loads(proc.stdout or "{}")
        except Exception:
            result = {"ok": False, "reason": "query_failed", "detail": "Could not parse sf data query JSON output."}
            self._query_cache[soql] = result
            return result

        result = {"ok": True, "payload": payload, "records": ((payload.get("result") or {}).get("records") or []) if isinstance(payload, dict) else []}
        self._query_cache[soql] = result
        return result

    def _query_user_in_org(self, username: str) -> Dict:
        if username in self._user_query_cache:
            return self._user_query_cache[username]

        escaped = username.replace("'", "\\'")
        soql = (
            "SELECT Username, IsActive, UserType, Profile.Name "
            f"FROM User WHERE Username = '{escaped}' LIMIT 1"
        )

        query = self._run_soql_query(soql)
        if not query.get("ok"):
            result = {"ok": False, "reason": query.get("reason"), "detail": query.get("detail")}
            self._user_query_cache[username] = result
            return result

        records = query.get("records") or []
        if not records:
            result = {"ok": False, "reason": "missing"}
            self._user_query_cache[username] = result
            return result

        record = records[0]
        is_active = record.get("IsActive") is True
        user_type = record.get("UserType")
        profile_name = ((record.get("Profile") or {}).get("Name"))

        if not is_active:
            result = {"ok": False, "reason": "inactive", "record": record}
        elif user_type == "AutomatedProcess":
            result = {"ok": False, "reason": "automated_process", "record": record}
        elif profile_name != "Einstein Agent User":
            result = {"ok": False, "reason": "wrong_profile", "record": record}
        else:
            result = {"ok": True, "reason": "valid", "record": record}

        self._user_query_cache[username] = result
        return result

    def _query_permission_set_assignments(self, username: str) -> Dict:
        escaped = username.replace("'", "\\'")
        soql = (
            "SELECT PermissionSetId, PermissionSet.Name "
            f"FROM PermissionSetAssignment WHERE Assignee.Username = '{escaped}'"
        )
        return self._run_soql_query(soql)

    def _query_permission_set(self, permset_name: str) -> Dict:
        escaped = permset_name.replace("'", "\\'")
        soql = f"SELECT Id, Name FROM PermissionSet WHERE Name = '{escaped}' LIMIT 1"
        return self._run_soql_query(soql)

    def _query_apex_classes(self, class_names: List[str]) -> Dict[str, Dict]:
        names = sorted({name for name in class_names if name})
        if not names:
            return {}
        quoted = ", ".join(f"'{name.replace("'", "\\'")}'" for name in names)
        soql = f"SELECT Id, Name FROM ApexClass WHERE Name IN ({quoted})"
        query = self._run_soql_query(soql)
        if not query.get("ok"):
            return {}
        return {record.get('Name'): record for record in (query.get('records') or [])}

    def _query_setup_entity_access(self, parent_id: str, setup_entity_ids: List[str], setup_entity_type: str = "ApexClass") -> Dict[str, Dict]:
        ids = sorted({entity_id for entity_id in setup_entity_ids if entity_id})
        if not ids:
            return {}
        quoted = ", ".join(f"'{entity_id}'" for entity_id in ids)
        soql = (
            "SELECT SetupEntityId, SetupEntityType "
            f"FROM SetupEntityAccess WHERE ParentId = '{parent_id}' "
            f"AND SetupEntityType = '{setup_entity_type}' AND SetupEntityId IN ({quoted})"
        )
        query = self._run_soql_query(soql)
        if not query.get("ok"):
            return {}
        return {record.get('SetupEntityId'): record for record in (query.get('records') or [])}

    def _query_active_flows(self, flow_names: List[str]) -> Dict[str, Dict]:
        names = sorted({name for name in flow_names if name})
        if not names:
            return {}
        quoted = ", ".join(f"'{name.replace("'", "\\'")}'" for name in names)
        soql = f"SELECT ApiName, ActiveVersionId FROM FlowDefinitionView WHERE ApiName IN ({quoted})"
        query = self._run_soql_query(soql)
        if not query.get("ok"):
            return {}
        return {record.get('ApiName'): record for record in (query.get('records') or [])}

    def _agent_identifier(self) -> Optional[str]:
        if "developer_name" in self.config_fields:
            return self.config_fields["developer_name"][0]
        if "agent_name" in self.config_fields:
            return self.config_fields["agent_name"][0]
        return None

    def _collect_targets(self) -> Dict[str, List[Tuple[str, int, str]]]:
        apex_targets: List[Tuple[str, int, str]] = []
        flow_targets: List[Tuple[str, int, str]] = []
        other_targets: List[Tuple[str, int, str]] = []

        for action in self.action_definitions:
            target = action.get("target")
            if not target:
                continue
            line = action.get("target_line") or action.get("line") or 1
            if target.startswith("apex://"):
                apex_ref = target[len("apex://"):].strip()
                apex_class_name = apex_ref.split(".", 1)[0]
                apex_targets.append((apex_class_name, line, target))
            elif target.startswith("flow://"):
                flow_name = target[len("flow://"):].strip()
                flow_targets.append((flow_name, line, target))
            else:
                other_targets.append((target, line, target))

        return {
            "apex": apex_targets,
            "flow": flow_targets,
            "other": other_targets,
        }

    def _add_error(self, line_num: int, message: str):
        self.errors.append((line_num, "error", message))

    def _add_warning(self, line_num: int, message: str):
        self.warnings.append((line_num, "warning", message))

    def _flush_action(self, current_action: Optional[Dict]):
        if current_action:
            self.action_definitions.append(current_action)

    def _parse_structure(self):
        """Parse enough structure to support context-aware validation rules."""
        current_top: Optional[str] = None
        current_action: Optional[Dict] = None
        actions_mode: Optional[str] = None
        actions_indent: Optional[int] = None
        reasoning_indent: Optional[int] = None
        current_io: Optional[Dict] = None
        current_io_field: Optional[Dict] = None
        lifecycle_block: Optional[Dict] = None

        for i, raw_line in enumerate(self.lines, 1):
            indent = self._indent(raw_line)
            stripped = raw_line.strip()

            if not stripped or stripped.startswith("#"):
                continue

            # Close nested contexts when indentation decreases
            if lifecycle_block and indent <= lifecycle_block["indent"]:
                lifecycle_block = None

            if current_io_field and indent <= current_io_field["indent"]:
                current_io_field = None

            if current_io and indent <= current_io["indent"]:
                current_io = None

            if current_action and indent <= current_action["indent"]:
                self._flush_action(current_action)
                current_action = None
                current_io = None
                current_io_field = None

            if actions_mode and actions_indent is not None and indent <= actions_indent:
                actions_mode = None
                actions_indent = None

            if reasoning_indent is not None and indent <= reasoning_indent:
                reasoning_indent = None

            # Top-level blocks
            if indent == 0:
                current_top = None
                lifecycle_block = None

                if stripped == "config:":
                    current_top = "config"
                    continue
                if stripped == "variables:":
                    current_top = "variables"
                    continue
                if stripped == "knowledge:":
                    current_top = "knowledge"
                    continue
                if stripped == "language:":
                    current_top = "language"
                    continue
                if stripped == "system:":
                    current_top = "system"
                    continue
                if stripped == "connections:":
                    current_top = "connections"
                    continue
                if stripped.startswith("connection messaging:"):
                    self.connection_messaging_lines.append(i)
                    current_top = "connection"
                    continue
                if stripped.startswith("connection "):
                    current_top = "connection"
                    continue

                block_match = self.BLOCK_NAME_PATTERN.match(stripped)
                if block_match:
                    kind, name = block_match.groups()
                    if kind == "topic":
                        self.topic_names.setdefault(name, i)
                        self.defined_topics.add(name)
                    else:
                        self.start_agent_names.setdefault(name, i)
                        self.defined_topics.add(name)
                    current_top = kind
                    continue

            # Parse config fields
            if current_top == "config" and indent > 0:
                field_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", stripped)
                if field_match:
                    field, value = field_match.groups()
                    self.config_fields[field] = (self._strip_quotes(value), i)
                continue

            # Parse variables block
            if current_top == "variables" and indent > 0:
                var_match = self.VARIABLE_DECL_PATTERN.match(stripped)
                if var_match:
                    name, modifier = var_match.groups()
                    self.variable_names.setdefault(name, i)
                if "source:" in stripped and ("@MessagingSession." in stripped or "@MessagingEndUser." in stripped):
                    self.messaging_linked_var_lines.append(i)
                continue

            # Parse topic/start_agent internals
            if current_top in {"topic", "start_agent"} and indent > 0:
                if stripped.startswith("description:"):
                    desc_value = stripped.split(":", 1)[1].strip()
                    if desc_value in {"|", ">", "|-", ">-"}:
                        self.multiline_description_issues.append((i, current_top))
                    elif desc_value == "":
                        self.multiline_description_issues.append((i, current_top))
                    continue

                if stripped in {"before_reasoning:", "after_reasoning:"}:
                    lifecycle_block = {"name": stripped[:-1], "indent": indent}
                    continue

                if lifecycle_block and indent > lifecycle_block["indent"] and stripped.startswith("instructions:"):
                    self.lifecycle_instruction_wrappers.append((i, lifecycle_block["name"]))
                    continue

                if stripped == "reasoning:":
                    reasoning_indent = indent
                    continue

                if stripped == "actions:":
                    if reasoning_indent is not None and indent > reasoning_indent:
                        actions_mode = "reasoning"
                    else:
                        actions_mode = "definition"
                    actions_indent = indent
                    continue

                if actions_mode in {"definition", "reasoning"} and actions_indent is not None and indent > actions_indent:
                    if current_action is None or indent == current_action["indent"]:
                        action_match = self.ACTION_DECL_PATTERN.match(stripped)
                        if action_match:
                            name, remainder = action_match.groups()
                            if name not in {"inputs", "outputs", "target", "description", "label", "source"}:
                                current_action = {
                                    "name": name,
                                    "line": i,
                                    "indent": indent,
                                    "kind": "definition",
                                    "inline": remainder.strip(),
                                    "target": None,
                                    "target_line": None,
                                    "has_available_when": False,
                                    "io_fields": [],
                                    "invalid_transition_properties": [],
                                    "required_input_lines": [],
                                    "prompt_output_display_lines": [],
                                    "date_io_lines": [],
                                }
                                inline = remainder.strip()
                                if inline.startswith("@utils.transition"):
                                    current_action["kind"] = "utility_transition"
                                elif inline.startswith("@topic."):
                                    current_action["kind"] = "delegation"
                                continue

                    if current_action is not None and indent > current_action["indent"]:
                        if stripped.startswith("target:"):
                            target = self._strip_quotes(stripped.split(":", 1)[1].strip())
                            current_action["target"] = target
                            current_action["target_line"] = i
                            continue

                        if stripped.startswith("available when"):
                            current_action["has_available_when"] = True
                            continue

                        if stripped == "inputs:":
                            current_io = {"name": "inputs", "indent": indent}
                            current_io_field = None
                            continue

                        if stripped == "outputs:":
                            current_io = {"name": "outputs", "indent": indent}
                            current_io_field = None
                            continue

                        if current_io and indent > current_io["indent"]:
                            if current_io_field is None or indent == current_io_field["indent"]:
                                field_match = self.IO_FIELD_PATTERN.match(stripped)
                                if field_match:
                                    field_name, field_type = field_match.groups()
                                    current_io_field = {
                                        "name": field_name,
                                        "type": field_type,
                                        "indent": indent,
                                        "section": current_io["name"],
                                    }
                                    current_action["io_fields"].append({
                                        "name": field_name,
                                        "type": field_type,
                                        "section": current_io["name"],
                                        "line": i,
                                    })
                                    if current_io["name"] in {"inputs", "outputs"} and field_type == "date":
                                        current_action["date_io_lines"].append((i, field_name, current_io["name"]))
                                    continue

                            if current_io_field and indent > current_io_field["indent"]:
                                if stripped.startswith("is_displayable:") and stripped.split(":", 1)[1].strip() == "True":
                                    current_action["prompt_output_display_lines"].append((i, current_io_field["name"]))
                                if stripped.startswith("is_required:") and stripped.split(":", 1)[1].strip() == "True":
                                    current_action["required_input_lines"].append((i, current_io_field["name"]))
                                continue

                        if current_action["kind"] == "utility_transition":
                            property_name = stripped.split(":", 1)[0].strip()
                            if property_name in self.INVALID_TRANSITION_PROPERTIES:
                                current_action["invalid_transition_properties"].append((i, property_name))
                            continue

        self._flush_action(current_action)

    def _check_mixed_indentation(self):
        has_tabs = False
        has_spaces = False
        tab_line = None
        space_line = None

        for i, line in enumerate(self.lines, 1):
            leading = len(line) - len(line.lstrip())
            if leading > 0:
                leading_chars = line[:leading]
                if "\t" in leading_chars:
                    has_tabs = True
                    if tab_line is None:
                        tab_line = i
                if " " in leading_chars:
                    has_spaces = True
                    if space_line is None:
                        space_line = i

        if has_tabs and has_spaces:
            self._add_error(
                tab_line or 1,
                f"Mixed tabs and spaces detected. Tabs first seen on line {tab_line}, spaces first seen on line {space_line}. Use consistent indentation (all tabs OR all spaces).",
            )

    def _check_boolean_case(self):
        bool_pattern = re.compile(r"=\s*(true|false)\s*(?:#|$)", re.IGNORECASE)
        for i, line in enumerate(self.lines, 1):
            match = bool_pattern.search(line)
            if not match:
                continue
            value = match.group(1)
            if value.lower() == "true" and value != "True":
                self._add_error(i, f"Boolean must be capitalized: use 'True' instead of '{value}'")
            elif value.lower() == "false" and value != "False":
                self._add_error(i, f"Boolean must be capitalized: use 'False' instead of '{value}'")

    def _check_required_blocks(self):
        required = {
            "system": False,
            "config": False,
            "start_agent": False,
        }
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("system:"):
                required["system"] = True
            elif stripped.startswith("config:"):
                required["config"] = True
            elif stripped.startswith("start_agent "):
                required["start_agent"] = True

        missing = [name for name, present in required.items() if not present]
        if missing:
            self._add_error(
                1,
                f"Missing required blocks: {', '.join(missing)}. Every agent needs config, system, and exactly one start_agent.",
            )

    def _check_config_fields(self):
        developer_name = self.config_fields.get("developer_name")
        legacy_agent_name = self.config_fields.get("agent_name")
        agent_description = self.config_fields.get("agent_description")
        legacy_description = self.config_fields.get("description")
        agent_type = self.config_fields.get("agent_type")
        default_agent_user = self.config_fields.get("default_agent_user")

        if not developer_name and not legacy_agent_name:
            self._add_error(1, "Missing agent identifier in config block. Use either 'developer_name' or legacy 'agent_name'.")
        if not agent_description and not legacy_description:
            self._add_error(1, "Missing agent description in config block. Use either 'agent_description' or legacy 'description'.")

        if not agent_type:
            if not default_agent_user:
                self._add_error(
                    1,
                    "Missing both 'agent_type' and 'default_agent_user'. Without agent_type the compiler defaults to a Service Agent, which requires default_agent_user.",
                )
            else:
                self._add_warning(
                    1,
                    "Missing 'agent_type'. This compiles when 'default_agent_user' is present because the compiler defaults to a Service Agent, but setting agent_type explicitly is safer.",
                )
            return

        agent_type_value, agent_type_line = agent_type
        if agent_type_value not in {"AgentforceServiceAgent", "AgentforceEmployeeAgent"}:
            self._add_error(
                agent_type_line,
                f"Invalid agent_type '{agent_type_value}'. Use 'AgentforceServiceAgent' or 'AgentforceEmployeeAgent'.",
            )
            return

        if agent_type_value == "AgentforceServiceAgent" and not default_agent_user:
            self._add_error(
                agent_type_line,
                "Service Agents require 'default_agent_user' in config. Set it to a valid Einstein Agent User.",
            )

        if agent_type_value == "AgentforceEmployeeAgent" and default_agent_user:
            self._add_error(
                default_agent_user[1],
                "Employee Agents must NOT include 'default_agent_user'. Remove it entirely; Employee Agents run as the logged-in user.",
            )

    def _check_start_agent_count(self):
        count = len(self.start_agent_names)
        if count == 0:
            self._add_error(1, "Missing start_agent block. Every agent needs exactly one start_agent.")
        elif count > 1:
            first_line = sorted(self.start_agent_names.values())[1]
            self._add_error(first_line, f"Found {count} start_agent blocks. Exactly one start_agent is allowed.")

    def _check_name_collisions(self):
        for name, line in self.start_agent_names.items():
            if name in self.topic_names:
                self._add_error(
                    line,
                    f"Name collision: start_agent '{name}' has the same API name as a topic. This is a proven publish blocker because generated GenAiPluginDefinition metadata collides; use unique names.",
                )

    def _check_name_rules(self, name: str, line: int, kind: str):
        if not self.NAME_PATTERN.match(name):
            self._add_error(
                line,
                f"Invalid {kind} '{name}'. Names must start with a letter and contain only letters, numbers, and underscores (max 80 chars).",
            )
            return
        if "__" in name:
            self._add_error(line, f"Invalid {kind} '{name}'. Consecutive underscores are not allowed.")
        if name.endswith("_"):
            self._add_error(line, f"Invalid {kind} '{name}'. Names cannot end with an underscore.")

    def _check_naming_rules(self):
        if "developer_name" in self.config_fields:
            name, line = self.config_fields["developer_name"]
            self._check_name_rules(name, line, "developer_name")
        elif "agent_name" in self.config_fields:
            name, line = self.config_fields["agent_name"]
            self._check_name_rules(name, line, "agent_name")

        for name, line in self.topic_names.items():
            self._check_name_rules(name, line, "topic name")

        for name, line in self.start_agent_names.items():
            self._check_name_rules(name, line, "start_agent name")

        for name, line in self.variable_names.items():
            self._check_name_rules(name, line, "variable name")

    def _check_invalid_connections_wrapper(self):
        for i, line in enumerate(self.lines, 1):
            if line.strip() == "connections:":
                self._add_error(i, "Invalid top-level block 'connections:'. Use 'connection messaging:' (singular) instead.")

    def _check_mutable_linked_conflict(self):
        pattern = re.compile(r"mutable\s+linked|linked\s+mutable", re.IGNORECASE)
        for i, line in enumerate(self.lines, 1):
            if pattern.search(line):
                self._add_error(
                    i,
                    "Variable cannot be both 'mutable' AND 'linked'. Use 'mutable' for changeable state and 'linked' for external read-only data.",
                )

    def _check_reserved_variable_names(self):
        for name, line in self.variable_names.items():
            if name in self.RESERVED_VARIABLE_NAMES:
                self._add_warning(
                    line,
                    f"Variable name '{name}' may conflict with platform context mappings. Prefer a prefixed name like 'customer_{name.lower()}'.",
                )

    def _check_undefined_variables(self):
        ref_pattern = re.compile(r"@variables\.([A-Za-z_][A-Za-z0-9_]*)")
        executable_prefixes = (
            "|",
            "if ",
            "set ",
            "run ",
            "with ",
            "available when",
            "transition ",
            "handoff:",
        )
        for i, line in enumerate(self.lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not (
                stripped.startswith(executable_prefixes)
                or (stripped.startswith("instructions:") and "@variables." in stripped)
            ):
                continue
            for match in ref_pattern.finditer(line):
                variable_name = match.group(1)
                if variable_name not in self.variable_names:
                    self._add_error(
                        i,
                        f"Reference to undefined variable '@variables.{variable_name}'. Declare it in the variables block before use.",
                    )

    def _check_undefined_topics(self):
        ref_pattern = re.compile(r"@topic\.([A-Za-z][A-Za-z0-9_]*)")
        for i, line in enumerate(self.lines, 1):
            for match in ref_pattern.finditer(line):
                topic_name = match.group(1)
                if topic_name not in self.defined_topics:
                    self._add_warning(
                        i,
                        f"Reference to undefined topic '@topic.{topic_name}'. Ensure this topic exists in the agent script.",
                    )

    def _check_multiline_descriptions(self):
        for line, block_kind in self.multiline_description_issues:
            self._add_warning(
                line,
                f"{block_kind} description appears multiline or block-scalar based. Keep topic/start_agent description on a single line; multiline descriptions are a known parser gotcha.",
            )

    def _check_lifecycle_instruction_wrappers(self):
        for line, lifecycle_name in self.lifecycle_instruction_wrappers:
            self._add_warning(
                line,
                f"{lifecycle_name} should contain direct content, not an 'instructions:' wrapper. Put procedural lines directly under {lifecycle_name}:.",
            )

    def _check_post_action_position(self):
        in_instructions = False
        seen_pipe_text = False

        for i, line in enumerate(self.lines, 1):
            stripped = line.strip()
            if "instructions:" in stripped:
                in_instructions = True
                seen_pipe_text = False
                continue

            if in_instructions:
                if stripped.startswith(("actions:", "topic ", "start_agent ", "before_reasoning:", "after_reasoning:")):
                    in_instructions = False
                    continue
                if stripped.startswith("|"):
                    seen_pipe_text = True
                if seen_pipe_text and stripped.startswith("if ") and "@variables." in stripped:
                    if any(token in stripped for token in ["_status", "_done", "_complete", "_processed"]):
                        self._add_warning(
                            i,
                            "Post-action check appears after LLM instructions. Consider moving it to the top of instructions so it triggers on topic re-entry after action completion.",
                        )

    def _check_empty_list_literals(self):
        compare_pattern = re.compile(r"^\s*if\s+.*(?:==|!=)\s*\[\]\s*:?\s*(?:#.*)?$")
        set_pattern = re.compile(r"^\s*set\s+.+?=\s*\[\]\s*(?:#.*)?$")
        for i, line in enumerate(self.lines, 1):
            if compare_pattern.search(line):
                self._add_error(i, "Empty list literal '[]' is not supported in expressions. Use len(@variables.list) == 0 instead.")
            elif set_pattern.search(line):
                self._add_error(i, "Resetting with 'set ... = []' is a known parser gotcha. Use a temporary empty variable workaround instead.")

    def _check_inputs_in_set(self):
        pattern = re.compile(r"^\s*set\s+@variables\.[^=]+\s*=\s*@inputs\.")
        for i, line in enumerate(self.lines, 1):
            if pattern.search(line):
                self._add_warning(i, "Using @inputs in set is a deploy-breaking anti-pattern. Capture input with @utils.setVariables or bind from @variables instead.")

    def _check_bare_run_actions(self):
        pattern = re.compile(r"^\s*run\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:#.*)?$")
        for i, line in enumerate(self.lines, 1):
            match = pattern.search(line)
            if match:
                self._add_error(i, f"Bare action name '{match.group(1)}' in run. Use '@actions.{match.group(1)}' explicitly.")

    def _check_action_metadata_context(self):
        for action in self.action_definitions:
            if action["kind"] != "utility_transition":
                continue
            for line, property_name in action["invalid_transition_properties"]:
                self._add_error(
                    line,
                    f"'{property_name}' is not valid on @utils.transition actions. Use it only on target-backed action definitions with 'target:'.",
                )

    def _check_prompt_output_displayability(self):
        for action in self.action_definitions:
            target = action.get("target") or ""
            if not (target.startswith("prompt://") or target.startswith("generatePromptResponse://")):
                continue
            for line, field_name in action["prompt_output_display_lines"]:
                self._add_warning(
                    line,
                    f"Output '{field_name}' uses 'is_displayable: True' on a prompt target. This can cause blank agent responses; prefer 'is_displayable: False' with 'is_used_by_planner: True'.",
                )

    def _check_date_type_in_action_io(self):
        for action in self.action_definitions:
            for line, field_name, section in action["date_io_lines"]:
                self._add_warning(
                    line,
                    f"Field '{field_name}' uses 'date' in action {section}. Runtime support is unreliable; prefer 'object' with complex_data_type_name: \"lightning__dateType\" for action I/O.",
                )

    def _check_is_required_advisories(self):
        for action in self.action_definitions:
            if action["has_available_when"]:
                continue
            for line, field_name in action["required_input_lines"]:
                self._add_warning(
                    line,
                    f"Input '{field_name}' uses 'is_required: True', but that is only a planner hint. Add an 'available when' guard if this input must gate execution.",
                )

    def _check_employee_agent_gotchas(self):
        agent_type = self.config_fields.get("agent_type", (None, None))[0]
        if agent_type != "AgentforceEmployeeAgent":
            return

        for line in self.connection_messaging_lines:
            self._add_warning(
                line,
                "Employee Agents typically should not include 'connection messaging:'. That block is generally for Service Agents / Messaging flows.",
            )

        for line in self.messaging_linked_var_lines:
            self._add_warning(
                line,
                "Messaging-linked variables are usually unnecessary for Employee Agents and may cause configuration issues if Messaging is not set up.",
            )

    def _check_service_agent_user_in_org(self):
        default_agent_user = self.config_fields.get("default_agent_user")
        effective_agent_type = self._effective_agent_type()

        if effective_agent_type != "AgentforceServiceAgent" or not default_agent_user:
            return

        username, line = default_agent_user
        if not self.validation_org:
            self._add_warning(
                line,
                "Could not run org-aware default_agent_user validation because no validation org is configured. Set AGENTSCRIPT_VALIDATION_ORG (or validation_org in SKILL.md) to enforce Service Agent user checks.",
            )
            return

        query_result = self._query_user_in_org(username)
        reason = query_result.get("reason")
        record = query_result.get("record") or {}

        if query_result.get("ok"):
            return

        if reason == "missing":
            self._add_warning(
                line,
                f"Service Agent default_agent_user '{username}' was not found in org '{self.validation_org}'. This is configuration drift and should be fixed, but some downstream org states may still allow publish/preview.",
            )
        elif reason == "inactive":
            self._add_error(
                line,
                f"Service Agent default_agent_user '{username}' exists in '{self.validation_org}' but is inactive. Use an active Einstein Agent User.",
            )
        elif reason == "automated_process":
            self._add_error(
                line,
                f"Service Agent default_agent_user '{username}' resolves to UserType=AutomatedProcess in '{self.validation_org}'. That is not a valid Einstein Agent User for Service Agent publishing.",
            )
        elif reason == "wrong_profile":
            profile_name = ((record.get("Profile") or {}).get("Name")) or "null"
            self._add_error(
                line,
                f"Service Agent default_agent_user '{username}' exists in '{self.validation_org}' but Profile.Name is '{profile_name}', not 'Einstein Agent User'.",
            )
        elif reason == "query_failed":
            detail = query_result.get("detail", "sf data query failed")
            self._add_warning(
                line,
                f"Could not verify default_agent_user '{username}' against org '{self.validation_org}'. sf data query failed: {detail}",
            )

    def _check_service_agent_target_permissions(self):
        default_agent_user = self.config_fields.get("default_agent_user")
        effective_agent_type = self._effective_agent_type()
        if effective_agent_type != "AgentforceServiceAgent" or not default_agent_user:
            return

        username, user_line = default_agent_user
        if not self.validation_org:
            return

        user_result = self._query_user_in_org(username)
        if not user_result.get("ok"):
            return

        targets = self._collect_targets()
        apex_targets = targets["apex"]
        flow_targets = targets["flow"]
        other_targets = targets["other"]
        if not apex_targets and not flow_targets and not other_targets:
            return

        assignments = self._query_permission_set_assignments(username)
        if not assignments.get("ok"):
            detail = assignments.get("detail", "sf data query failed")
            self._add_warning(
                user_line,
                f"Could not verify permission set assignments for Service Agent user '{username}' in '{self.validation_org}'. sf data query failed: {detail}",
            )
            return

        assigned_names = {((record.get("PermissionSet") or {}).get("Name")) for record in (assignments.get("records") or [])}
        assigned_names.discard(None)

        if not any(name == "AgentforceServiceAgentUser" or "AgentforceServiceAgentUser" in name for name in assigned_names):
            self._add_warning(
                user_line,
                f"Service Agent user '{username}' is missing a verified AgentforceServiceAgentUser permission set/group assignment in '{self.validation_org}'. Recommend assigning it before publish to avoid runtime/publish surprises.",
            )

        agent_identifier = self._agent_identifier()
        custom_permset_name = f"{agent_identifier}_Access" if agent_identifier else None
        custom_assigned = bool(custom_permset_name and custom_permset_name in assigned_names)

        if apex_targets and not custom_assigned:
            first_line = apex_targets[0][1]
            self._add_warning(
                first_line,
                f"This Service Agent uses apex:// targets but user '{username}' is not assigned custom permission set '{custom_permset_name}'. Recommend creating/assigning '{custom_permset_name}' with <classAccesses> for every Apex class target.",
            )

        if flow_targets and not custom_assigned:
            first_line = flow_targets[0][1]
            self._add_warning(
                first_line,
                f"This Service Agent uses flow:// targets but user '{username}' is not assigned custom permission set '{custom_permset_name}'. Flow internals still need object/field/Apex access; verify permissions explicitly.",
            )

        if custom_assigned and custom_permset_name:
            permset_query = self._query_permission_set(custom_permset_name)
            permset_records = permset_query.get("records") if permset_query.get("ok") else []
            permset_id = permset_records[0].get("Id") if permset_records else None

            if apex_targets and permset_id:
                apex_names = [name for name, _, _ in apex_targets]
                apex_by_name = self._query_apex_classes(apex_names)
                if apex_by_name:
                    access_by_id = self._query_setup_entity_access(permset_id, [record.get("Id") for record in apex_by_name.values()], "ApexClass")
                    for apex_name, line, raw_target in apex_targets:
                        apex_record = apex_by_name.get(apex_name)
                        if not apex_record:
                            self._add_warning(
                                line,
                                f"Could not verify Apex target '{raw_target}' in org '{self.validation_org}'. If this is namespaced or packaged, verify '{custom_permset_name}' includes explicit class access for it.",
                            )
                            continue
                        if apex_record.get("Id") not in access_by_id:
                            self._add_warning(
                                line,
                                f"Apex target '{raw_target}' is not granted in permission set '{custom_permset_name}'. Recommend adding <classAccesses><apexClass>{apex_name}</apexClass><enabled>true</enabled></classAccesses> and reassigning if needed.",
                            )

            elif apex_targets and not permset_id:
                first_line = apex_targets[0][1]
                self._add_warning(
                    first_line,
                    f"User '{username}' appears assigned to '{custom_permset_name}', but the permission set record could not be resolved in '{self.validation_org}'. Verify the permission set exists and includes class access entries.",
                )

        active_flows = self._query_active_flows([name for name, _, _ in flow_targets]) if flow_targets else {}
        for flow_name, line, raw_target in flow_targets:
            record = active_flows.get(flow_name)
            if not record:
                self._add_error(
                    line,
                    f"Flow target '{raw_target}' was not found in org '{self.validation_org}'. Deploy the FlowDefinition before publish.",
                )
                continue
            if not record.get("ActiveVersionId"):
                self._add_error(
                    line,
                    f"Flow target '{raw_target}' exists in '{self.validation_org}' but has no active version. Activate the Flow before publish.",
                )

        for raw_target, line, _ in other_targets:
            self._add_warning(
                line,
                f"Target '{raw_target}' uses a protocol the validator cannot permission-check automatically. Verify the Service Agent user '{username}' has all required access before publish.",
            )

    def _issue_texts(self, severity: str) -> List[str]:
        issues = self.errors if severity == "error" else self.warnings
        return [message for _, _, message in issues]

    def _checklist_entry(self, section: str, label: str, error_terms: List[str], warning_terms: Optional[List[str]] = None, success_detail: str = "No issues found.", na_detail: Optional[str] = None, applicable: bool = True, confidence: Optional[str] = None) -> Dict[str, str]:
        if not applicable:
            return {"section": section, "status": "info", "icon": "ℹ️", "label": label, "detail": na_detail or "Not applicable.", "confidence": confidence or "Informational"}

        warning_terms = warning_terms or error_terms
        error_matches = [msg for msg in self._issue_texts("error") if any(term in msg for term in error_terms)]
        if error_matches:
            return {"section": section, "status": "error", "icon": "❌", "label": label, "detail": error_matches[0], "confidence": confidence or "Blocking"}

        warning_matches = [msg for msg in self._issue_texts("warning") if any(term in msg for term in warning_terms)]
        if warning_matches:
            return {"section": section, "status": "warning", "icon": "⚠️", "label": label, "detail": warning_matches[0], "confidence": confidence or "Warning"}

        return {"section": section, "status": "ok", "icon": "✅", "label": label, "detail": success_detail, "confidence": confidence or "Check"}

    def _build_checklist(self) -> List[Dict[str, str]]:
        effective_agent_type = self._effective_agent_type()
        targets = self._collect_targets()
        has_targets = bool(targets["apex"] or targets["flow"] or targets["other"])
        service_agent = effective_agent_type == "AgentforceServiceAgent"
        employee_agent = effective_agent_type == "AgentforceEmployeeAgent"

        return [
            self._checklist_entry("Structure", "Indentation consistency", ["Mixed tabs and spaces"], success_detail="Tabs/spaces usage is consistent.", confidence="Compiler rule"),
            self._checklist_entry("Structure", "Boolean capitalization", ["Boolean must be capitalized"], success_detail="Boolean literals use True/False correctly.", confidence="Compiler rule"),
            self._checklist_entry("Structure", "Required top-level blocks", ["Missing required blocks"], success_detail="config, system, and start_agent blocks are present.", confidence="Compiler rule"),
            self._checklist_entry("Structure", "Exactly one start_agent", ["Missing start_agent block", "Exactly one start_agent is allowed"], success_detail="Exactly one start_agent block is defined.", confidence="Compiler rule"),
            self._checklist_entry("Structure", "Topic/start_agent naming collisions", ["Name collision"], success_detail="No topic/start_agent API-name collisions detected.", confidence="Proven publish blocker"),
            self._checklist_entry("Structure", "Naming rule compliance", ["Invalid developer_name", "Invalid agent_name", "Invalid topic name", "Invalid start_agent name", "Invalid variable name"], success_detail="Names follow Agent Script naming rules."),
            self._checklist_entry("Structure", "Invalid connections wrapper", ["Invalid top-level block 'connections:'"], success_detail="No invalid plural connections wrapper detected."),
            self._checklist_entry("Structure", "Variable safety", ["Variable cannot be both 'mutable' AND 'linked'", "Reference to undefined variable"], ["may conflict with platform context mappings"], success_detail="Variable declarations and executable references look consistent."),
            self._checklist_entry("Structure", "Topic references", ["Reference to undefined topic"], success_detail="All @topic references resolve to defined topics."),
            self._checklist_entry("Structure", "Description formatting", ["description appears multiline"], success_detail="Topic/start_agent descriptions stay on a single safe line."),
            self._checklist_entry("Structure", "Lifecycle hook formatting", ["should contain direct content, not an 'instructions:' wrapper"], success_detail="before_reasoning/after_reasoning blocks use direct content."),
            self._checklist_entry("Agent identity", "Config field completeness", ["Missing agent identifier", "Missing agent description", "Invalid agent_type"], ["Missing 'agent_type'."], success_detail="Agent identifier, description, and agent_type shape look valid.", confidence="Compiler rule"),
            self._checklist_entry("Agent identity", "Service vs Employee agent semantics", ["Service Agents require 'default_agent_user'", "Employee Agents must NOT include 'default_agent_user'", "Missing both 'agent_type' and 'default_agent_user'"], ["Missing 'agent_type'. This compiles when 'default_agent_user' is present"], success_detail="Agent type and default_agent_user relationship is valid.", confidence="Compiler / platform rule"),
            self._checklist_entry("Agent identity", "Service Agent user exists in validation org", ["Service Agent default_agent_user"], ["Could not run org-aware default_agent_user validation", "Could not verify default_agent_user", "was not found in org"], success_detail="default_agent_user resolves to an active Einstein Agent User.", na_detail="Not applicable to Employee Agents or agents without default_agent_user.", applicable=service_agent and bool(self.config_fields.get("default_agent_user")), confidence="Configuration drift / publish risk"),
            self._checklist_entry("Targets & permissions", "Required Service Agent permission assignments", ["AgentforceServiceAgentUser permission set/group assignment", "not assigned custom permission set"], ["not assigned custom permission set"], success_detail="Required system/custom permission assignments are present for the detected targets.", na_detail="No Service Agent target-backed actions require permission assignment checks.", applicable=service_agent and has_targets, confidence="Likely runtime risk"),
            self._checklist_entry("Targets & permissions", "Apex target permission coverage", ["Apex target '", "could not verify Apex target", "not assigned custom permission set"], ["could not verify Apex target", "not assigned custom permission set"], success_detail="All apex:// targets are covered by the assigned custom permission set.", na_detail="No apex:// targets detected.", applicable=service_agent and bool(targets["apex"]), confidence="Likely runtime risk"),
            self._checklist_entry("Targets & permissions", "Flow target readiness", ["Flow target '"] , ["Flow target '", "uses flow:// targets but user"], success_detail="All flow:// targets exist and have an active version.", na_detail="No flow:// targets detected.", applicable=bool(targets["flow"]), confidence="Proven publish blocker"),
            self._checklist_entry("Targets & permissions", "Other target protocol review", ["cannot permission-check automatically"], success_detail="All detected target protocols are fully supported by automatic checks.", na_detail="No non-apex/non-flow targets detected.", applicable=bool(targets["other"]), confidence="Manual review"),
            self._checklist_entry("Runtime gotchas", "Collection / set gotchas", ["Empty list literal '[]'", "Resetting with 'set ... = []'"], ["Using @inputs in set"], success_detail="No known collection/set gotchas detected.", confidence="Compiler / deploy rule"),
            self._checklist_entry("Runtime gotchas", "Action invocation syntax", ["Bare action name"], success_detail="run statements reference @actions explicitly.", confidence="Compiler rule"),
            self._checklist_entry("Runtime gotchas", "Utility action metadata", ["is not valid on @utils.transition actions"], success_detail="@utils.transition metadata usage is valid.", confidence="Compiler rule"),
            self._checklist_entry("Runtime gotchas", "Prompt output displayability", ["is_displayable: True"], success_detail="Prompt outputs avoid risky displayability settings.", na_detail="No prompt targets detected.", applicable=any((action.get("target") or "").startswith(("prompt://", "generatePromptResponse://")) for action in self.action_definitions)),
            self._checklist_entry("Runtime gotchas", "Action I/O date typing", ["uses 'date' in action"], success_detail="No risky 'date' action I/O declarations detected."),
            self._checklist_entry("Runtime gotchas", "Planner-required input hints", ["uses 'is_required: True'"], success_detail="Required inputs are either guarded or not using the risky planner-only hint."),
            self._checklist_entry("Runtime gotchas", "Employee-agent-only / service-agent-only patterns", ["Employee Agents typically should not include 'connection messaging:'", "Messaging-linked variables are usually unnecessary for Employee Agents"], success_detail="No agent-type-specific Messaging configuration issues detected.", na_detail="Not applicable outside Employee Agent checks.", applicable=employee_agent),
        ]


def format_output(result: dict) -> str:
    """Format validation results for Claude."""
    lines = []
    file_name = Path(result["file_path"]).name
    checklist = result.get("checklist") or []

    if result["success"]:
        lines.append(f"✅ Agent Script Validation Passed: {file_name}")
    else:
        lines.append(f"❌ Agent Script validation found blocking issues in {file_name}")

    error_count = sum(1 for item in checklist if item.get("status") == "error")
    warning_count = sum(1 for item in checklist if item.get("status") == "warning")
    info_count = sum(1 for item in checklist if item.get("status") == "info")
    ok_count = sum(1 for item in checklist if item.get("status") == "ok")

    lines.append(f"📊 Summary: {error_count} blocking, {warning_count} warnings, {info_count} informational, {ok_count} passing checks")

    if result["errors"]:
        recommendation = "Fix blocking issues before preview/publish."
    elif result["warnings"]:
        recommendation = "Safe to preview. Review warnings before publish."
    else:
        recommendation = "Safe to preview and publish."

    lines.append(f"➡️ Recommended next step: {recommendation}")
    lines.append("")
    lines.append("Validation checklist")
    lines.append("--------------------")

    current_section = None
    for item in checklist:
        section = item.get("section", "General")
        if section != current_section:
            if current_section is not None:
                lines.append("")
            lines.append(f"{section}:")
            current_section = section
        confidence = item.get("confidence")
        confidence_text = f" [{confidence}]" if confidence else ""
        lines.append(f"  {item['icon']} {item['label']}{confidence_text} — {item['detail']}")

    if result["errors"]:
        lines.append("")
        lines.append("Blocking issues")
        lines.append("---------------")
        for line_num, severity, message in result["errors"]:
            lines.append(f"❌ Line {line_num}: {message}")
        lines.append("")
        lines.append("Fix the blocking issues above before validate/preview/publish.")

    if result["warnings"]:
        lines.append("")
        lines.append("Warnings")
        lines.append("--------")
        for line_num, severity, message in result["warnings"]:
            lines.append(f"⚠️ Line {line_num}: {message}")

    return "\n".join(lines)


def main():
    """Main hook entry point."""
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path.endswith(".agent"):
        sys.exit(0)

    if not os.path.exists(file_path):
        sys.exit(0)

    try:
        with open(file_path, "r") as f:
            content = f.read()
    except Exception as e:
        print(f"⚠️ Could not read {file_path}: {e}")
        sys.exit(0)

    validator = AgentScriptValidator(content, file_path)
    result = validator.validate()

    output = format_output(result)
    if output:
        print(output)

    sys.exit(0)


if __name__ == "__main__":
    main()
