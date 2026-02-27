#!/usr/bin/env python3
"""
Trace Analyzer — Analysis engine for Builder execution traces.

Takes parsed PlanSuccessResponse traces (captured by TraceTestRunner)
and produces structured insights: conversation timelines, grounding
reports, variable diffs, action results, routing analysis, timing
breakdowns, safety scores, and AgentScript improvement suggestions.

The Builder trace contains 13 step types with data that is NOT persisted
to Data Cloud STDM (full LLM prompts, variable change reasons, safety
score breakdowns, grounding evaluations). This analyzer extracts
actionable insights from that ephemeral data.

Usage (via CLI):
    python3 scripts/cli.py trace-test \\
      --org MyOrg --agent My_Agent \\
      --utterances utterances.yaml --output ./trace-results
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Step type constants (13 types from Builder trace)
# ---------------------------------------------------------------------------

USER_INPUT = "UserInputStep"
SESSION_INIT = "SessionInitialStateStep"
NODE_ENTRY = "NodeEntryStateStep"
VARIABLE_UPDATE = "VariableUpdateStep"
BEFORE_REASONING = "BeforeReasoningStep"
BEFORE_REASONING_ITER = "BeforeReasoningIterationStep"
ENABLED_TOOLS = "EnabledToolsStep"
LLM_STEP = "LLMStep"
TRANSITION = "TransitionStep"
FUNCTION_STEP = "FunctionStep"
AFTER_REASONING = "AfterReasoningStep"
REASONING = "ReasoningStep"
PLANNER_RESPONSE = "PlannerResponseStep"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_step_data(step: dict) -> dict:
    """Extract the data payload from a trace step.

    Some step types nest data under a 'data' key, others
    have fields at the top level.
    """
    return step.get("data", step)


def _ms_to_s(ms: Optional[int | float]) -> str:
    """Convert milliseconds to human-readable seconds string."""
    if ms is None:
        return "?"
    return f"{ms / 1000:.1f}s"


def _duration_ms(step: dict) -> Optional[int]:
    """Calculate step duration from start/end execution timestamps."""
    start = step.get("startExecutionTime")
    end = step.get("endExecutionTime")
    if start is not None and end is not None:
        return int(end - start)
    return None


# ---------------------------------------------------------------------------
# TraceAnalyzer
# ---------------------------------------------------------------------------

class TraceAnalyzer:
    """Analyzes parsed Builder execution traces.

    Accepts a list of PlanSuccessResponse dicts (one per conversation
    turn) and provides methods to extract insights from each.
    """

    def __init__(self, traces: list[dict[str, Any]]):
        self.traces = [t for t in traces if "error" not in t]
        self.all_traces = traces  # includes error entries

    # ------------------------------------------------------------------
    # Per-turn extraction helpers
    # ------------------------------------------------------------------

    def _steps_of_type(
        self, trace: dict, step_type: str
    ) -> list[dict]:
        """Filter steps by type within a single trace."""
        return [
            s for s in trace.get("plan", [])
            if s.get("type") == step_type
        ]

    # ------------------------------------------------------------------
    # 1. Conversation timeline
    # ------------------------------------------------------------------

    def conversation_timeline(self) -> list[dict[str, Any]]:
        """Build a per-turn summary of the conversation.

        Returns list of dicts with:
          turn, utterance, topic, step_count, llm_count, llm_ms,
          action_count, response, grounding, safety_score
        """
        timeline = []

        for trace in self.all_traces:
            turn = trace.get("_turn", 0)
            utterance = trace.get("_utterance", "")

            if "error" in trace:
                timeline.append({
                    "turn": turn,
                    "utterance": utterance,
                    "error": trace["error"],
                })
                continue

            plan = trace.get("plan", [])
            topic = trace.get("topic", "unknown")

            # LLM steps
            llm_steps = self._steps_of_type(trace, LLM_STEP)
            llm_ms = sum(
                _get_step_data(s).get("execution_latency", 0)
                or _duration_ms(s) or 0
                for s in llm_steps
            )

            # Action steps
            actions = self._steps_of_type(trace, FUNCTION_STEP)
            action_ms = sum(
                s.get("executionLatency", 0) or _duration_ms(s) or 0
                for s in actions
            )

            # Response
            response_steps = self._steps_of_type(trace, PLANNER_RESPONSE)
            response_text = ""
            safety_score = None
            if response_steps:
                resp = response_steps[-1]
                response_text = resp.get("message", "")
                scores = resp.get("safetyScore", {})
                if isinstance(scores, dict):
                    inner = scores.get("safetyScore", scores)
                    safety_score = inner.get("safety_score")

            # Grounding
            reasoning_steps = self._steps_of_type(trace, REASONING)
            grounding = "N/A"
            grounding_reason = ""
            if reasoning_steps:
                last = reasoning_steps[-1]
                grounding = last.get("category", "N/A")
                grounding_reason = last.get("reason", "")

            timeline.append({
                "turn": turn,
                "utterance": utterance,
                "topic": topic,
                "step_count": len(plan),
                "llm_count": len(llm_steps),
                "llm_ms": llm_ms,
                "action_count": len(actions),
                "action_ms": action_ms,
                "response": response_text,
                "grounding": grounding,
                "grounding_reason": grounding_reason,
                "safety_score": safety_score,
            })

        return timeline

    # ------------------------------------------------------------------
    # 2. Grounding report
    # ------------------------------------------------------------------

    def grounding_report(self) -> list[dict[str, Any]]:
        """Identify UNGROUNDED responses with reasons."""
        results = []
        for trace in self.traces:
            for step in self._steps_of_type(trace, REASONING):
                category = step.get("category", "")
                if category == "UNGROUNDED":
                    results.append({
                        "turn": trace.get("_turn"),
                        "utterance": trace.get("_utterance"),
                        "category": category,
                        "reason": step.get("reason", ""),
                        "topic": trace.get("topic"),
                    })
        return results

    # ------------------------------------------------------------------
    # 3. Variable diff report
    # ------------------------------------------------------------------

    def variable_diff_report(self) -> list[dict[str, Any]]:
        """Track variable state changes across turns.

        Excludes internal AgentScriptInternal_* variables.
        """
        diffs = []
        for trace in self.traces:
            for step in self._steps_of_type(trace, VARIABLE_UPDATE):
                data = _get_step_data(step)
                updates = data.get("variable_updates", [])
                for update in updates:
                    name = update.get("variable_name", "")
                    if name.startswith("AgentScriptInternal_"):
                        continue
                    diffs.append({
                        "turn": trace.get("_turn"),
                        "variable": name,
                        "old_value": update.get("variable_past_value"),
                        "new_value": update.get("variable_new_value"),
                        "reason": update.get("variable_change_reason", ""),
                    })
        return diffs

    # ------------------------------------------------------------------
    # 4. Action report
    # ------------------------------------------------------------------

    def action_report(self) -> list[dict[str, Any]]:
        """Extract action calls with inputs, outputs, errors, latency."""
        results = []
        for trace in self.traces:
            for step in self._steps_of_type(trace, FUNCTION_STEP):
                func = step.get("function", {})
                latency = step.get("executionLatency") or _duration_ms(step)
                results.append({
                    "turn": trace.get("_turn"),
                    "name": func.get("name", "unknown"),
                    "input": func.get("input", {}),
                    "output": func.get("output", {}),
                    "errors": func.get("errors"),
                    "latency_ms": latency,
                    "topic": trace.get("topic"),
                })
        return results

    # ------------------------------------------------------------------
    # 5. Routing report
    # ------------------------------------------------------------------

    def routing_report(self) -> list[dict[str, Any]]:
        """Analyze topic transitions and routing decisions."""
        results = []
        for trace in self.traces:
            for step in self._steps_of_type(trace, TRANSITION):
                data = _get_step_data(step)
                results.append({
                    "turn": trace.get("_turn"),
                    "from_agent": data.get("from_agent", ""),
                    "to_agent": data.get("to_agent", ""),
                    "transition_type": data.get("transition_type", ""),
                    "transition_mode": data.get("transition_mode", ""),
                })
        return results

    # ------------------------------------------------------------------
    # 6. Timing report
    # ------------------------------------------------------------------

    def timing_report(self) -> list[dict[str, Any]]:
        """Per-turn timing breakdown: LLM, actions, overhead."""
        results = []
        for trace in self.traces:
            plan = trace.get("plan", [])
            if not plan:
                continue

            timestamps = [s.get("startExecutionTime") for s in plan]
            timestamps += [s.get("endExecutionTime") for s in plan]
            timestamps = [t for t in timestamps if t is not None]
            total_ms = (max(timestamps) - min(timestamps)) if timestamps else 0

            llm_ms = sum(
                _get_step_data(s).get("execution_latency", 0)
                or _duration_ms(s) or 0
                for s in self._steps_of_type(trace, LLM_STEP)
            )
            action_ms = sum(
                s.get("executionLatency", 0) or _duration_ms(s) or 0
                for s in self._steps_of_type(trace, FUNCTION_STEP)
            )
            overhead_ms = max(0, total_ms - llm_ms - action_ms)

            results.append({
                "turn": trace.get("_turn"),
                "total_ms": total_ms,
                "llm_ms": llm_ms,
                "action_ms": action_ms,
                "overhead_ms": overhead_ms,
            })
        return results

    # ------------------------------------------------------------------
    # 7. Safety report
    # ------------------------------------------------------------------

    def safety_report(self) -> list[dict[str, Any]]:
        """Extract safety scores and category breakdowns per turn."""
        results = []
        for trace in self.traces:
            for step in self._steps_of_type(trace, PLANNER_RESPONSE):
                scores = step.get("safetyScore", {})
                inner = scores.get("safetyScore", scores) if isinstance(scores, dict) else {}
                results.append({
                    "turn": trace.get("_turn"),
                    "is_safe": step.get("isContentSafe"),
                    "safety_score": inner.get("safety_score"),
                    "category_scores": inner.get("category_scores", {}),
                })
        return results

    # ------------------------------------------------------------------
    # 8. AgentScript suggestions
    # ------------------------------------------------------------------

    def agentscript_suggestions(self) -> list[dict[str, str]]:
        """Generate actionable fix recommendations based on trace analysis.

        Produces suggestions for:
        - UNGROUNDED responses (missing knowledge/instructions)
        - Delayed routing (weak topic matching criteria)
        - Action errors (input mapping issues)
        - High LLM latency (overly complex instructions)
        - Missing transitions (need transition-to rules)
        """
        suggestions: list[dict[str, str]] = []

        # Check grounding issues
        for item in self.grounding_report():
            suggestions.append({
                "type": "UNGROUNDED",
                "turn": str(item["turn"]),
                "message": (
                    f"Turn {item['turn']} generated ungrounded content. "
                    f"Reason: {item['reason'][:120]}"
                ),
                "fix": (
                    "Ensure topic routing invokes a data-fetching action "
                    "before generating responses about specific records. "
                    "Add knowledge sources or instructions to ground the response."
                ),
            })

        # Check action errors
        for item in self.action_report():
            if item["errors"]:
                error_msg = str(item["errors"])[:120]
                suggestions.append({
                    "type": "ACTION_ERROR",
                    "turn": str(item["turn"]),
                    "message": (
                        f"{item['name']} failed: {error_msg}"
                    ),
                    "fix": (
                        f"Check action input mapping for {item['name']}. "
                        f"Verify that required variables are resolved before "
                        f"the action is invoked. Input was: "
                        f"{json.dumps(item['input'])[:100]}"
                    ),
                })

        # Check timing anomalies
        for item in self.timing_report():
            if item["llm_ms"] > 5000:
                suggestions.append({
                    "type": "TIMING",
                    "turn": str(item["turn"]),
                    "message": (
                        f"Turn {item['turn']} had high LLM latency: "
                        f"{_ms_to_s(item['llm_ms'])}"
                    ),
                    "fix": (
                        "Consider simplifying topic instructions, reducing "
                        "the number of available actions, or splitting "
                        "complex topics into focused sub-topics."
                    ),
                })

        # Check routing patterns
        routing = self.routing_report()
        for item in routing:
            if item["transition_mode"] == "DELAYED":
                suggestions.append({
                    "type": "DELAYED_ROUTING",
                    "turn": str(item["turn"]),
                    "message": (
                        f"Delayed routing from {item['from_agent']} to "
                        f"{item['to_agent']}"
                    ),
                    "fix": (
                        "Add stronger topic matching criteria (scope, "
                        "instructions, classification description) to "
                        "enable immediate routing."
                    ),
                })

        # Check for unsafe content
        for item in self.safety_report():
            if item["is_safe"] is False:
                suggestions.append({
                    "type": "UNSAFE",
                    "turn": str(item["turn"]),
                    "message": f"Turn {item['turn']} was flagged as unsafe.",
                    "fix": (
                        "Review the agent's instructions and guardrails. "
                        "Add explicit safety instructions to the topic or "
                        "enable Trust Layer guardrails."
                    ),
                })

        return suggestions

    # ------------------------------------------------------------------
    # Output: Per-turn Rich panel (inline feedback during capture)
    # ------------------------------------------------------------------

    def render_turn_panel(
        self, turn_data: dict[str, Any], target_console: Optional[Console] = None
    ) -> None:
        """Render a compact Rich panel for a single turn's trace.

        Called by cli.py after each utterance capture for immediate
        feedback. Shows: turn, utterance, topic, step count, LLM time,
        grounding verdict, and response preview.
        """
        c = target_console or console

        turn = turn_data.get("_turn", "?")
        utterance = turn_data.get("_utterance", "")

        if "error" in turn_data:
            c.print(f"  [red]\u274c Error: {turn_data['error'][:100]}[/red]")
            return

        plan = turn_data.get("plan", [])
        topic = turn_data.get("topic", "unknown")
        step_count = len(plan)

        # LLM timing
        llm_steps = [s for s in plan if s.get("type") == LLM_STEP]
        llm_ms = sum(
            _get_step_data(s).get("execution_latency", 0)
            or _duration_ms(s) or 0
            for s in llm_steps
        )

        # Grounding
        reasoning_steps = [s for s in plan if s.get("type") == REASONING]
        grounding = "N/A"
        if reasoning_steps:
            grounding = reasoning_steps[-1].get("category", "N/A")

        # Response preview
        response_steps = [s for s in plan if s.get("type") == PLANNER_RESPONSE]
        response = ""
        if response_steps:
            response = response_steps[-1].get("message", "")

        # Grounding icon
        if grounding == "GROUNDED":
            g_str = "[green]\u2705 GROUNDED[/green]"
        elif grounding == "UNGROUNDED":
            g_str = "[yellow]\u26a0\ufe0f  UNGROUNDED[/yellow]"
        elif grounding == "SMALL_TALK":
            g_str = "[dim]\U0001f4ac SMALL_TALK[/dim]"
        else:
            g_str = f"[dim]{grounding}[/dim]"

        # Compact output
        metrics = f"Steps: {step_count} | LLM: {_ms_to_s(llm_ms)} | {g_str}"
        c.print(f"  [green]\u2713[/green] Topic: {topic} | {metrics}")

        if response:
            preview = response[:120].replace("\n", " ")
            if len(response) > 120:
                preview += "..."
            c.print(f"  [dim]Response: {preview}[/dim]")

    # ------------------------------------------------------------------
    # Output: Rich terminal report
    # ------------------------------------------------------------------

    def render_terminal(self, target_console: Optional[Console] = None) -> None:
        """Render a full Rich-formatted terminal report."""
        c = target_console or console
        timeline = self.conversation_timeline()
        total_turns = len(self.all_traces)
        agent_name = ""
        if self.traces:
            # Try to get agent name from LLM steps
            for step in self.traces[0].get("plan", []):
                data = _get_step_data(step)
                name = data.get("agent_name", "")
                if name and name != "Topic Selector":
                    agent_name = name
                    break

        header = f"TRACE TEST REPORT"
        if agent_name:
            header += f" \u2014 {agent_name}"
        header += f" ({total_turns} turn{'s' if total_turns != 1 else ''})"

        c.print(f"\n[bold cyan]\U0001f4ca {header}[/bold cyan]")
        c.print("\u2550" * 60)

        # Per-turn details
        for entry in timeline:
            turn = entry.get("turn", "?")
            utterance = entry.get("utterance", "")

            if "error" in entry:
                c.print(
                    f"\n[red]\U0001f504 TURN {turn}:[/red] \"{utterance}\"\n"
                    f"   [red]Error: {entry['error']}[/red]"
                )
                continue

            topic = entry.get("topic", "unknown")
            steps = entry.get("step_count", 0)
            llm_ms = entry.get("llm_ms", 0)
            action_count = entry.get("action_count", 0)
            action_ms = entry.get("action_ms", 0)
            grounding = entry.get("grounding", "N/A")
            safety = entry.get("safety_score")
            response = entry.get("response", "")

            # Turn header
            metrics = f"Steps: {steps} | LLM: {_ms_to_s(llm_ms)}"
            if action_count:
                metrics += f" | Actions: {action_count} ({_ms_to_s(action_ms)})"

            c.print(
                f"\n[cyan]\U0001f504 TURN {turn}:[/cyan] \"{utterance}\"\n"
                f"   Topic: {topic} | {metrics}"
            )

            # Grounding status
            if grounding == "GROUNDED":
                reason = entry.get("grounding_reason", "")
                c.print(f"   [green]\u2705 GROUNDED[/green] \u2014 {reason[:80]}")
            elif grounding == "UNGROUNDED":
                reason = entry.get("grounding_reason", "")
                c.print(f"   [yellow]\u26a0\ufe0f  UNGROUNDED[/yellow] \u2014 {reason[:80]}")
            elif grounding == "SMALL_TALK":
                c.print(f"   [dim]\U0001f4ac SMALL_TALK[/dim]")

            # Action errors
            for act in self.action_report():
                if act["turn"] == turn and act["errors"]:
                    c.print(
                        f"   [red]\u274c ACTION ERROR:[/red] "
                        f"{act['name']} \u2014 {str(act['errors'])[:60]}"
                    )

            # Response preview
            if response:
                preview = response[:100].replace("\n", " ")
                if len(response) > 100:
                    preview += "..."
                c.print(f"   Response: {preview}")

        # Summary section
        c.print(f"\n[bold cyan]\U0001f4ca SUMMARY[/bold cyan]")
        c.print("\u2500" * 50)

        timing = self.timing_report()
        actions = self.action_report()
        safety_items = self.safety_report()
        grounding_items = self.grounding_report()

        total_time = sum(t["total_ms"] for t in timing) if timing else 0
        total_llm = sum(t["llm_ms"] for t in timing) if timing else 0
        llm_count = sum(
            len(self._steps_of_type(t, LLM_STEP)) for t in self.traces
        )
        failed_actions = [a for a in actions if a["errors"]]

        # Count grounding categories
        grounding_counts: dict[str, int] = {}
        for trace in self.traces:
            for step in self._steps_of_type(trace, REASONING):
                cat = step.get("category", "N/A")
                grounding_counts[cat] = grounding_counts.get(cat, 0) + 1

        avg_safety = None
        safety_scores = [s["safety_score"] for s in safety_items if s["safety_score"] is not None]
        if safety_scores:
            avg_safety = sum(safety_scores) / len(safety_scores)

        c.print(f"  Turns:         {total_turns}")
        c.print(f"  Total time:    {_ms_to_s(total_time)}")
        c.print(
            f"  LLM calls:     {llm_count}"
            + (f" (avg {_ms_to_s(total_llm / llm_count)})" if llm_count else "")
        )
        c.print(
            f"  Actions:       {len(actions)}"
            + (f" ({len(failed_actions)} failed)" if failed_actions else "")
        )
        if grounding_counts:
            parts = ", ".join(
                f"{count} {cat}" for cat, count in grounding_counts.items()
            )
            c.print(f"  Grounding:     {parts}")
        if avg_safety is not None:
            c.print(f"  Safety:        avg {avg_safety:.2f}")

        # Suggestions section
        suggestions = self.agentscript_suggestions()
        if suggestions:
            c.print(f"\n[bold cyan]\U0001f527 AGENTSCRIPT SUGGESTIONS[/bold cyan]")
            c.print("\u2500" * 50)

            for i, s in enumerate(suggestions, 1):
                c.print(
                    f"  {i}. [{s['type']}] {s['message']}\n"
                    f"     Fix: {s['fix']}"
                )
                c.print()

    # ------------------------------------------------------------------
    # Output: JSON
    # ------------------------------------------------------------------

    def to_json(self, path: Path) -> dict[str, Any]:
        """Write full analysis to JSON file and return the dict."""
        analysis = {
            "timeline": self.conversation_timeline(),
            "grounding": self.grounding_report(),
            "variables": self.variable_diff_report(),
            "actions": self.action_report(),
            "routing": self.routing_report(),
            "timing": self.timing_report(),
            "safety": self.safety_report(),
            "suggestions": self.agentscript_suggestions(),
            "summary": self.to_summary(),
        }
        path.write_text(json.dumps(analysis, indent=2, default=str))
        return analysis

    # ------------------------------------------------------------------
    # Output: Summary
    # ------------------------------------------------------------------

    def to_summary(self) -> dict[str, Any]:
        """Single-dict summary for quick pass/fail checks."""
        timing = self.timing_report()
        actions = self.action_report()
        suggestions = self.agentscript_suggestions()

        total_turns = len(self.all_traces)
        successful_turns = len(self.traces)
        failed_actions = [a for a in actions if a["errors"]]
        ungrounded = self.grounding_report()

        # Pass/fail: no ungrounded, no action errors, no unsafe
        unsafe = [
            s for s in self.safety_report()
            if s.get("is_safe") is False
        ]
        passed = (
            not ungrounded
            and not failed_actions
            and not unsafe
            and successful_turns == total_turns
        )

        return {
            "status": "PASS" if passed else "FAIL",
            "turns_total": total_turns,
            "turns_captured": successful_turns,
            "action_errors": len(failed_actions),
            "ungrounded_count": len(ungrounded),
            "unsafe_count": len(unsafe),
            "suggestion_count": len(suggestions),
            "total_time_ms": sum(t["total_ms"] for t in timing) if timing else 0,
        }

    def render_summary_line(self) -> str:
        """Single-line pass/fail string for CI output."""
        s = self.to_summary()
        status = s["status"]
        icon = "\u2705" if status == "PASS" else "\u274c"
        return (
            f"{icon} {status}: "
            f"{s['turns_captured']}/{s['turns_total']} turns, "
            f"{s['action_errors']} action errors, "
            f"{s['ungrounded_count']} ungrounded, "
            f"{s['suggestion_count']} suggestions"
        )
