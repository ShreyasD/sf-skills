#!/usr/bin/env python3
"""
analyze_trace_capture.py

Analyzes captured network data from either:
  1. capture-trace-network.ts output (JSON)
  2. Chrome DevTools HAR export

Identifies Agentforce trace endpoints, maps response schemas to STDM fields,
and generates a structured report.

Usage:
    python3 analyze_trace_capture.py captures/trace-capture-*.json
    python3 analyze_trace_capture.py --har agentforce-trace.har
    python3 analyze_trace_capture.py captures/trace-capture-*.json --curl-templates
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AURA_TRACE_KEYWORDS = {
    "trace", "step", "session", "agent", "copilot", "runtime",
    "bot", "reasoning", "planner", "action", "topic", "einstein",
    "genai", "aiagent", "builder", "orchestrat", "execution",
    "preview", "message", "conversation",
}

STDM_FIELD_MAPPING = {
    # Builder trace fields → STDM DMO fields
    "startExecutionTime": "ssot__StartTimestamp__c (on Step or Interaction)",
    "endExecutionTime": "ssot__EndTimestamp__c",
    "type": "ssot__AiAgentInteractionStepType__c (loosely mapped)",
    "agent_name": "ssot__AiAgentMoment__dlm.ssot__AiAgentApiName__c",
    "enabled_tools": "Derived from topic/action config (not persisted)",
    "topic": "ssot__AIAgentInteraction__dlm.ssot__TopicApiName__c",
    "input": "ssot__AIAgentInteractionStep__dlm.ssot__InputValueText__c",
    "output": "ssot__AIAgentInteractionStep__dlm.ssot__OutputValueText__c",
    "error": "ssot__AIAgentInteractionStep__dlm.ssot__ErrorMessageText__c",
    "variables": "ssot__PreStepVariableText__c / ssot__PostStepVariableText__c",
    "sessionId": "ssot__AIAgentSession__dlm.ssot__Id__c",
    "interactionId": "ssot__AIAgentInteraction__dlm.ssot__Id__c",
    "stepId": "ssot__AIAgentInteractionStep__dlm.ssot__Id__c",
    "generationId": "ssot__AIAgentInteractionStep__dlm.ssot__GenerationId__c",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_capture_json(filepath: Path) -> list[dict]:
    """Load capture JSON from capture-trace-network.ts output."""
    data = json.loads(filepath.read_text())
    return data.get("capturedRequests", [])


def load_har(filepath: Path) -> list[dict]:
    """Load HAR file and normalize entries to our capture format."""
    har = json.loads(filepath.read_text())
    entries = har.get("log", {}).get("entries", [])

    normalized = []
    for i, entry in enumerate(entries):
        req = entry.get("request", {})
        resp = entry.get("response", {})

        url = req.get("url", "")
        method = req.get("method", "")
        parsed_url = urlparse(url)

        # Extract request body
        post_data = req.get("postData", {})
        request_body = post_data.get("text", None)

        # Extract response body
        response_content = resp.get("content", {})
        response_body = response_content.get("text", None)

        # Build header dicts
        req_headers = {h["name"]: h["value"] for h in req.get("headers", [])}
        resp_headers = {h["name"]: h["value"] for h in resp.get("headers", [])}

        # Detect SSE
        content_type = resp_headers.get("content-type", resp_headers.get("Content-Type", ""))
        is_sse = "event-stream" in content_type

        normalized.append({
            "index": i,
            "timestamp": entry.get("startedDateTime", ""),
            "requestId": f"har-{i}",
            "method": method,
            "url": url,
            "urlPath": parsed_url.path,
            "requestHeaders": req_headers,
            "requestBody": request_body,
            "auraDescriptors": _extract_aura_descriptors(request_body),
            "responseStatus": resp.get("status"),
            "responseHeaders": resp_headers,
            "responseBody": response_body,
            "responseContentType": content_type,
            "isSSE": is_sse,
            "isWebSocket": False,
            "durationMs": entry.get("time"),
            "category": _categorize(url),
            "matchReason": "HAR import",
        })

    return normalized


def _extract_aura_descriptors(body: str | None) -> list[str]:
    """Extract Aura action descriptors from request body."""
    if not body:
        return []
    descriptors = []
    for match in re.finditer(r'"descriptor"\s*:\s*"([^"]+)"', body):
        descriptors.append(match.group(1))
    return descriptors


def _categorize(url: str) -> str:
    """Categorize a URL into a known category."""
    if "/aura" in url:
        return "aura"
    if "/connect/" in url:
        return "connect-api"
    if "/einstein/ai-agent/" in url:
        return "agent-runtime"
    if "/einstein/ai-evaluations/" in url:
        return "testing-api"
    if "/einstein/" in url:
        return "einstein-api"
    if "/cometd/" in url:
        return "streaming-cometd"
    if "/lwr/" in url or "/webruntime/" in url:
        return "lwr"
    if "/event/" in url:
        return "platform-event"
    return "other"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_trace_relevant(requests: list[dict]) -> list[dict]:
    """Filter to only Agentforce trace-related requests."""
    relevant = []
    for req in requests:
        url_lower = req["url"].lower()
        body_lower = (req.get("requestBody") or "").lower()
        resp_lower = (req.get("responseBody") or "").lower()

        # Check URL patterns
        is_relevant = any(kw in url_lower for kw in AURA_TRACE_KEYWORDS)

        # Check Aura descriptors
        if not is_relevant and req.get("auraDescriptors"):
            for desc in req["auraDescriptors"]:
                if any(kw in desc.lower() for kw in AURA_TRACE_KEYWORDS):
                    is_relevant = True
                    break

        # Check request body for agent-related content
        if not is_relevant and body_lower:
            is_relevant = any(kw in body_lower for kw in AURA_TRACE_KEYWORDS)

        # Check response body for trace step indicators
        if not is_relevant and resp_lower:
            step_indicators = [
                "enabledtoolsstep", "llm_step", "action_step", "topic_step",
                "startexecutiontime", "endexecutiontime", "aicopilot__",
                "reactinitialprompt", "reacttopicprompt", "reactvalidationprompt",
                "tracedata", "tracestep",
            ]
            is_relevant = any(ind in resp_lower for ind in step_indicators)

        if is_relevant:
            relevant.append(req)

    return relevant


# ---------------------------------------------------------------------------
# Schema extraction
# ---------------------------------------------------------------------------

def extract_response_schema(body: str | None, max_depth: int = 4) -> dict | None:
    """Extract JSON schema structure from response body."""
    if not body:
        return None
    try:
        data = json.loads(body)
        return _schema_from_value(data, depth=0, max_depth=max_depth)
    except (json.JSONDecodeError, RecursionError):
        return None


def _schema_from_value(value: Any, depth: int, max_depth: int) -> Any:
    """Recursively extract schema from a JSON value."""
    if depth >= max_depth:
        return "..."
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        # Detect likely UUIDs, timestamps, etc.
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            return "datetime"
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", value, re.I):
            return "uuid"
        if len(value) > 200:
            return f"string({len(value)} chars)"
        return "string"
    if isinstance(value, list):
        if len(value) == 0:
            return ["(empty)"]
        return [_schema_from_value(value[0], depth + 1, max_depth)]
    if isinstance(value, dict):
        schema = {}
        for k, v in value.items():
            schema[k] = _schema_from_value(v, depth + 1, max_depth)
        return schema
    return str(type(value).__name__)


# ---------------------------------------------------------------------------
# Curl template generation
# ---------------------------------------------------------------------------

def generate_curl_template(req: dict) -> str:
    """Generate a curl command to replay a captured request."""
    parts = ["curl"]

    # Method
    if req["method"] != "GET":
        parts.append(f"-X {req['method']}")

    # URL
    parts.append(f"'{req['url']}'")

    # Key headers (skip noisy ones)
    skip_headers = {
        "accept-encoding", "connection", "host", "user-agent",
        "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
        "referer", "origin", "accept-language", "cache-control",
        "pragma", "cookie",
    }
    for name, value in sorted(req.get("requestHeaders", {}).items()):
        if name.lower() not in skip_headers:
            # Redact tokens
            if name.lower() == "authorization":
                value = "Bearer <YOUR_TOKEN>"
            parts.append(f"-H '{name}: {value}'")

    # Body
    body = req.get("requestBody")
    if body:
        # Truncate very long bodies
        if len(body) > 2000:
            body = body[:2000] + "...(truncated)"
        parts.append(f"--data-raw '{body}'")

    return " \\\n  ".join(parts)


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

def parse_sse_events(body: str | None) -> list[dict]:
    """Parse Server-Sent Events from response body."""
    if not body:
        return []

    events = []
    current_event: dict[str, str] = {"event": "", "data": "", "id": ""}

    for line in body.split("\n"):
        line = line.strip()
        if not line:
            # Empty line = event boundary
            if current_event["data"]:
                events.append(dict(current_event))
            current_event = {"event": "", "data": "", "id": ""}
        elif line.startswith("event:"):
            current_event["event"] = line[6:].strip()
        elif line.startswith("data:"):
            data_part = line[5:].strip()
            if current_event["data"]:
                current_event["data"] += "\n" + data_part
            else:
                current_event["data"] = data_part
        elif line.startswith("id:"):
            current_event["id"] = line[3:].strip()

    # Final event
    if current_event["data"]:
        events.append(current_event)

    return events


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    requests: list[dict],
    filtered: list[dict],
    output_path: Path | None = None,
) -> str:
    """Generate a structured analysis report."""
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)

    emit("=" * 70)
    emit("AGENTFORCE BUILDER TRACE — NETWORK ANALYSIS REPORT")
    emit("=" * 70)
    emit()

    # Overview
    emit(f"Total requests analyzed:    {len(requests)}")
    emit(f"Trace-relevant requests:    {len(filtered)}")
    emit()

    # Category breakdown
    categories = Counter(r["category"] for r in filtered)
    emit("REQUESTS BY CATEGORY:")
    emit("-" * 40)
    for cat, count in categories.most_common():
        emit(f"  {cat:<25} {count:>5}")
    emit()

    # Method breakdown
    methods = Counter(r["method"] for r in filtered)
    emit("REQUESTS BY METHOD:")
    emit("-" * 40)
    for method, count in methods.most_common():
        emit(f"  {method:<10} {count:>5}")
    emit()

    # Unique endpoints
    endpoints = sorted(set(f"{r['method']} {r['urlPath']}" for r in filtered))
    emit(f"UNIQUE ENDPOINTS ({len(endpoints)}):")
    emit("-" * 70)
    for ep in endpoints:
        emit(f"  {ep}")
    emit()

    # Aura descriptors
    all_descriptors = sorted(
        set(d for r in filtered for d in r.get("auraDescriptors", []))
    )
    if all_descriptors:
        emit(f"AURA DESCRIPTORS ({len(all_descriptors)}):")
        emit("-" * 70)
        for d in all_descriptors:
            # Highlight trace-relevant descriptors
            is_trace = any(kw in d.lower() for kw in AURA_TRACE_KEYWORDS)
            marker = "★" if is_trace else " "
            emit(f"  {marker} {d}")
        emit()

    # SSE streams
    sse_requests = [r for r in filtered if r.get("isSSE")]
    if sse_requests:
        emit(f"SSE STREAMS ({len(sse_requests)}):")
        emit("-" * 70)
        for r in sse_requests:
            emit(f"  #{r['index']} {r['method']} {r['urlPath']}")
            events = parse_sse_events(r.get("responseBody"))
            if events:
                event_types = Counter(e["event"] for e in events if e["event"])
                emit(f"    Event types: {dict(event_types)}")
                emit(f"    Total events: {len(events)}")
                # Show first event data
                if events[0].get("data"):
                    try:
                        first_data = json.loads(events[0]["data"])
                        schema = extract_response_schema(json.dumps(first_data), max_depth=3)
                        emit(f"    First event schema: {json.dumps(schema, indent=6)}")
                    except json.JSONDecodeError:
                        emit(f"    First event data: {events[0]['data'][:200]}")
        emit()

    # Response schemas (for non-SSE requests with JSON responses)
    emit("RESPONSE SCHEMAS:")
    emit("-" * 70)
    seen_schemas: set[str] = set()
    for r in filtered:
        if r.get("isSSE"):
            continue
        body = r.get("responseBody")
        if not body:
            continue
        schema = extract_response_schema(body, max_depth=3)
        if schema:
            schema_key = json.dumps(schema, sort_keys=True)
            if schema_key not in seen_schemas:
                seen_schemas.add(schema_key)
                emit(f"  Endpoint: {r['method']} {r['urlPath']}")
                emit(f"  Status:   {r['responseStatus']}")
                emit(f"  Schema:")
                emit(textwrap.indent(json.dumps(schema, indent=2), "    "))
                emit()
    emit()

    # STDM field mapping candidates
    emit("STDM FIELD MAPPING CANDIDATES:")
    emit("-" * 70)
    found_fields: dict[str, list[str]] = defaultdict(list)
    for r in filtered:
        body = r.get("responseBody") or ""
        body_lower = body.lower()
        for trace_field, stdm_field in STDM_FIELD_MAPPING.items():
            if trace_field.lower() in body_lower:
                found_fields[trace_field].append(
                    f"#{r['index']} {r['method']} {r['urlPath']}"
                )

    for trace_field, stdm_field in STDM_FIELD_MAPPING.items():
        if trace_field in found_fields:
            sources = found_fields[trace_field]
            emit(f"  {trace_field}")
            emit(f"    → STDM: {stdm_field}")
            emit(f"    Found in: {', '.join(sources[:3])}")
            emit()
    emit()

    # Auth patterns
    emit("AUTH PATTERNS:")
    emit("-" * 70)
    auth_headers = set()
    for r in filtered:
        headers = r.get("requestHeaders", {})
        for name in headers:
            if name.lower() in ("authorization", "x-sfdc-session", "cookie"):
                auth_headers.add(name)
        # Check for Aura token
        if "aura.token" in (r.get("requestBody") or ""):
            auth_headers.add("aura.token (in body)")
    for ah in sorted(auth_headers):
        emit(f"  {ah}")
    emit()

    # Timing analysis
    timed = [r for r in filtered if r.get("durationMs") is not None]
    if timed:
        durations = [r["durationMs"] for r in timed]
        emit("TIMING ANALYSIS:")
        emit("-" * 40)
        emit(f"  Requests with timing:  {len(timed)}")
        emit(f"  Min duration:          {min(durations)}ms")
        emit(f"  Max duration:          {max(durations)}ms")
        emit(f"  Avg duration:          {sum(durations) / len(durations):.0f}ms")
        emit()

        # Slowest requests
        slowest = sorted(timed, key=lambda r: r["durationMs"], reverse=True)[:5]
        emit("  Slowest requests:")
        for r in slowest:
            emit(f"    {r['durationMs']:>6}ms  {r['method']} {r['urlPath']}")
        emit()

    report = "\n".join(lines)

    if output_path:
        output_path.write_text(report)
        print(f"Report written to: {output_path}", file=sys.stderr)

    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze Agentforce Builder trace network captures"
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to capture JSON or HAR file",
    )
    parser.add_argument(
        "--har",
        action="store_true",
        help="Input is a HAR file (auto-detected by .har extension)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output report file path",
    )
    parser.add_argument(
        "--curl-templates",
        action="store_true",
        help="Generate curl replay templates for each captured request",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Output report as JSON instead of text",
    )
    parser.add_argument(
        "--show-bodies",
        action="store_true",
        help="Include response bodies in output (can be very large)",
    )

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # Load data
    is_har = args.har or args.input.suffix == ".har"
    if is_har:
        print(f"Loading HAR file: {args.input}", file=sys.stderr)
        requests = load_har(args.input)
    else:
        print(f"Loading capture file: {args.input}", file=sys.stderr)
        requests = load_capture_json(args.input)

    print(f"Loaded {len(requests)} requests", file=sys.stderr)

    # Filter
    filtered = filter_trace_relevant(requests)
    print(f"Found {len(filtered)} trace-relevant requests", file=sys.stderr)

    # Generate report
    report = generate_report(requests, filtered, args.output)

    if not args.output:
        print(report)

    # Curl templates
    if args.curl_templates:
        curl_dir = args.input.parent / "curl-templates"
        curl_dir.mkdir(exist_ok=True)

        for req in filtered:
            curl = generate_curl_template(req)
            filename = f"{req['index']:03d}-{req['category']}-{req['method'].lower()}.sh"
            (curl_dir / filename).write_text(f"#!/bin/bash\n# {req['urlPath']}\n\n{curl}\n")

        print(f"\nCurl templates written to: {curl_dir}", file=sys.stderr)

    # JSON report
    if args.json_report:
        json_out = {
            "total_requests": len(requests),
            "trace_relevant": len(filtered),
            "endpoints": sorted(set(f"{r['method']} {r['urlPath']}" for r in filtered)),
            "aura_descriptors": sorted(
                set(d for r in filtered for d in r.get("auraDescriptors", []))
            ),
            "categories": dict(Counter(r["category"] for r in filtered).most_common()),
            "sse_streams": [
                {
                    "url": r["urlPath"],
                    "events": len(parse_sse_events(r.get("responseBody"))),
                }
                for r in filtered
                if r.get("isSSE")
            ],
            "stdm_field_matches": {
                field: any(
                    field.lower() in (r.get("responseBody") or "").lower()
                    for r in filtered
                )
                for field in STDM_FIELD_MAPPING
            },
        }

        if args.show_bodies:
            json_out["requests"] = filtered

        json_path = (args.output or args.input).with_suffix(".analysis.json")
        json_path.write_text(json.dumps(json_out, indent=2))
        print(f"JSON report written to: {json_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
