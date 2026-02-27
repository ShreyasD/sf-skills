#!/usr/bin/env python3
"""
Trace Test Runner — Automated Builder trace capture via Playwright.

Launches Chromium, authenticates via sf CLI frontdoor, navigates to the
Agentforce Builder, sends test utterances, and captures execution traces
from the internal getSimulationPlanTraces Aura endpoint.

The Builder's JavaScript automatically calls the trace endpoint after each
agent response (triggered by the planId in the SSE INFORM event). This
module intercepts that response to extract the full PlanSuccessResponse
with 13 step types — far richer than the 5 types persisted to Data Cloud.

Usage (via CLI):
    python3 scripts/cli.py trace-test \\
      --org MyOrg --agent My_Agent \\
      --utterances utterances.yaml --output ./trace-results
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from rich.console import Console
from rich.status import Status

console = Console(stderr=True)

# Error patterns that indicate the agent failed to respond.
# Checked in the last chat bubble text during trace polling.
_AGENT_ERROR_PATTERNS = [
    "something went wrong",
    "unexpected error",
    "i apologize",
    "an error occurred",
    "unable to process",
    "i'm sorry, i encountered",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_sf_command(args: list[str]) -> dict:
    """Run an sf CLI command with --json and return parsed output."""
    cmd = ["sf"] + args + ["--json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        raise RuntimeError(
            "sf CLI not found. Install from: https://developer.salesforce.com/tools/salesforcecli"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"sf CLI timed out: {' '.join(cmd)}")

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"sf CLI error: {stderr[:500]}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"sf CLI returned invalid JSON: {result.stdout[:200]}")


def load_utterances(source: str) -> list[str]:
    """Load utterances from YAML file or comma-separated string.

    Supports:
      - YAML file: list of strings (["utterance1", "utterance2"])
      - Comma-separated: "utterance 1,utterance 2,utterance 3"
    """
    path = Path(source)
    if path.exists() and path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise RuntimeError(
                "PyYAML required for .yaml utterance files. "
                "Install with: pip install pyyaml"
            )
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, list):
            raise ValueError(f"Utterance YAML must be a list, got: {type(data).__name__}")
        return [str(item) for item in data]

    # Treat as comma-separated string
    return [u.strip() for u in source.split(",") if u.strip()]


# ---------------------------------------------------------------------------
# TraceTestRunner
# ---------------------------------------------------------------------------

class TraceTestRunner:
    """Orchestrates browser-based trace capture for Agentforce agents.

    Flow:
      1. Resolve agent metadata (BotDefinition SOQL via sf CLI)
      2. Launch Playwright Chromium (headless by default)
      3. Authenticate via sf org frontdoor
      4. Navigate to Agentforce Builder (direct URL, cached, or Studio)
      5. Register response interceptor for getSimulationPlanTraces
      6. For each utterance: type -> submit -> wait for trace -> parse
      7. Return list of PlanSuccessResponse dicts
    """

    def __init__(
        self,
        org_alias: str,
        agent_name: str,
        output_dir: Path,
        timeout: int = 30,
        verbose: bool = False,
        cdp_port: Optional[int] = None,
        headless: bool = True,
        builder_url: Optional[str] = None,
    ):
        self.org_alias = org_alias
        self.agent_name = agent_name
        self.output_dir = output_dir
        self.timeout = timeout
        self.verbose = verbose
        self.cdp_port = cdp_port
        self.headless = headless
        self.builder_url = builder_url

        self._playwright_ctx = None
        self._browser = None
        self._context = None
        self._page = None
        self._captured_traces: list[str] = []
        self._instance_url: Optional[str] = None
        self._agent_id: Optional[str] = None
        self._connected_existing = False

        # Ensure output directories exist
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "traces").mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Logging & screenshots
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.verbose:
            console.print(f"[dim]  {msg}[/dim]")

    def _screenshot(self, name: str) -> Path:
        """Save a debug screenshot to output_dir (not /tmp)."""
        path = self.output_dir / f"debug-{name}.png"
        try:
            self._page.screenshot(path=str(path))
        except Exception:
            pass
        return path

    # ------------------------------------------------------------------
    # 1. Agent metadata resolution
    # ------------------------------------------------------------------

    def _resolve_agent(self) -> dict[str, Any]:
        """Query BotDefinition to resolve agent ID and metadata.

        Returns dict with Id, DeveloperName, MasterLabel.
        """
        query = (
            f"SELECT Id, DeveloperName, MasterLabel "
            f"FROM BotDefinition "
            f"WHERE DeveloperName = '{self.agent_name}' "
            f"LIMIT 1"
        )

        result = _run_sf_command([
            "data", "query",
            "--query", query,
            "--target-org", self.org_alias,
        ])

        records = result.get("result", {}).get("records", [])
        if not records:
            raise RuntimeError(
                f"Agent '{self.agent_name}' not found. "
                f"Verify the DeveloperName in Setup > Agents."
            )

        agent = records[0]
        self._agent_id = agent["Id"]
        console.print(
            f"[green]\u2713[/green] Agent: {agent.get('MasterLabel', self.agent_name)} "
            f"({agent['Id'][:15]}...)"
        )
        return agent

    def _construct_builder_url(self) -> Optional[str]:
        """Construct the Builder URL via SOQL, skipping Studio navigation.

        Queries GenAiProject + GenAiProjectVersion to build the URL
        directly. Returns None if queries fail (falls back to Studio).
        """
        try:
            # Query project ID by DeveloperName (matches agent API name)
            proj_query = (
                f"SELECT Id FROM GenAiProject "
                f"WHERE DeveloperName = '{self.agent_name}' LIMIT 1"
            )
            proj_result = _run_sf_command([
                "data", "query", "--query", proj_query,
                "--target-org", self.org_alias,
            ])
            proj_records = proj_result.get("result", {}).get("records", [])
            if not proj_records:
                self._log("No GenAiProject found — falling back to Studio")
                return None
            project_id = proj_records[0]["Id"]

            # Query latest project version
            ver_query = (
                f"SELECT Id FROM GenAiProjectVersion "
                f"WHERE GenAiProjectId = '{project_id}' "
                f"ORDER BY CreatedDate DESC LIMIT 1"
            )
            ver_result = _run_sf_command([
                "data", "query", "--query", ver_query,
                "--target-org", self.org_alias,
            ])
            ver_records = ver_result.get("result", {}).get("records", [])
            if not ver_records:
                self._log("No GenAiProjectVersion found — falling back to Studio")
                return None
            version_id = ver_records[0]["Id"]

            url = (
                f"{self._instance_url}/AgentAuthoring/agentAuthoringBuilder.app"
                f"#/project?projectId={project_id}"
                f"&projectVersionId={version_id}"
            )
            self._log(f"Constructed Builder URL: {url[:80]}...")
            return url

        except RuntimeError as e:
            self._log(f"SOQL URL construction failed: {e}")
            return None

    # ------------------------------------------------------------------
    # 2. Browser launch
    # ------------------------------------------------------------------

    def _launch_browser(self) -> None:
        """Launch Playwright Chromium (headless by default, --headed for visible)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            console.print("[red]Error: playwright not installed.[/red]")
            console.print("Install with:")
            console.print("  pip install playwright")
            console.print("  playwright install chromium")
            raise SystemExit(1)

        mode = "headed" if not self.headless else "headless"
        self._log(f"Launching browser ({mode})...")

        self._playwright_ctx = sync_playwright().start()

        try:
            self._browser = self._playwright_ctx.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-first-run",
                    "--disable-infobars",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        except Exception as e:
            self._playwright_ctx.stop()
            self._playwright_ctx = None
            raise RuntimeError(
                f"Failed to launch Chromium: {e}\n"
                f"Run 'playwright install chromium' to install the browser."
            )

        self._context = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        console.print(f"[green]\u2713[/green] Browser launched ({mode})")

    def _connect_to_existing(self) -> None:
        """Connect to an already-running Chrome instance via CDP.

        Expects Chrome launched with --remote-debugging-port.
        Finds the Salesforce/Builder page among open tabs.
        Skips auth and navigation since the user is already there.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            console.print("[red]Error: playwright not installed.[/red]")
            console.print("Install with: pip install playwright")
            raise SystemExit(1)

        self._log(f"Connecting to Chrome on port {self.cdp_port}...")

        self._playwright_ctx = sync_playwright().start()

        try:
            self._browser = self._playwright_ctx.chromium.connect_over_cdp(
                f"http://localhost:{self.cdp_port}"
            )
        except Exception as e:
            self._playwright_ctx.stop()
            self._playwright_ctx = None
            raise RuntimeError(
                f"Could not connect to Chrome on port {self.cdp_port}: {e}\n"
                f"Ensure Chrome is running with --remote-debugging-port={self.cdp_port}"
            )

        # Find the Builder page among open tabs
        target_page = None
        for ctx in self._browser.contexts:
            for page in ctx.pages:
                url = page.url
                if "agentAuthoringBuilder" in url or "AgentAuthoring" in url:
                    target_page = page
                    break
                if "lightning" in url or "salesforce.com" in url or "force.com" in url:
                    target_page = page
            if target_page and "agentAuthoring" in target_page.url.lower():
                break

        if not target_page:
            # Fall back to first page
            all_pages = [p for ctx in self._browser.contexts for p in ctx.pages]
            if all_pages:
                target_page = all_pages[0]
            else:
                raise RuntimeError("No browser tabs found.")

        self._page = target_page
        self._context = target_page.context
        self._connected_existing = True

        console.print(f"[green]\u2713[/green] Connected to: {target_page.url[:80]}...")

    # ------------------------------------------------------------------
    # 3. Authentication
    # ------------------------------------------------------------------

    def _authenticate(self) -> None:
        """Authenticate via sf CLI frontdoor URL."""
        result = _run_sf_command([
            "org", "open",
            "--target-org", self.org_alias,
            "--url-only",
        ])

        frontdoor_url = result.get("result", {}).get("url", "")
        if not frontdoor_url:
            raise RuntimeError(
                f"Could not get frontdoor URL for org '{self.org_alias}'. "
                f"Run 'sf org login web --alias {self.org_alias}' first."
            )

        # Extract instance URL
        from urllib.parse import urlparse
        parsed = urlparse(frontdoor_url)
        self._instance_url = f"{parsed.scheme}://{parsed.netloc}"
        self._log(f"Instance: {self._instance_url}")

        # Navigate to frontdoor — sets session cookies.
        # Salesforce Lightning keeps long-polling connections open, so
        # "networkidle" never fires. Use "load" instead.
        self._page.goto(frontdoor_url, wait_until="load", timeout=60000)

        # Verify authentication succeeded
        current_url = self._page.url
        if "frontdoor" in current_url.lower():
            raise RuntimeError(
                "Authentication failed — still on frontdoor page. "
                "Try 'sf org login web' to refresh your session."
            )

        console.print(f"[green]\u2713[/green] Authenticated to {parsed.netloc}")

    # ------------------------------------------------------------------
    # 4. Builder navigation (direct URL / cached / Studio discovery)
    # ------------------------------------------------------------------

    def _load_cached_builder_url(self) -> Optional[str]:
        """Load a previously cached Builder URL for this org:agent pair."""
        cache_path = self.output_dir / ".builder-url-cache.json"
        if not cache_path.exists():
            return None
        try:
            cache = json.loads(cache_path.read_text())
            key = f"{self.org_alias}:{self.agent_name}"
            url = cache.get(key)
            if url:
                self._log(f"Cached Builder URL: {url[:80]}...")
            return url
        except (json.JSONDecodeError, OSError):
            return None

    def _save_builder_url_cache(self, url: str) -> None:
        """Cache the discovered Builder URL for future runs."""
        cache_path = self.output_dir / ".builder-url-cache.json"
        try:
            cache = {}
            if cache_path.exists():
                cache = json.loads(cache_path.read_text())
            key = f"{self.org_alias}:{self.agent_name}"
            cache[key] = url
            cache_path.write_text(json.dumps(cache, indent=2))
            self._log(f"Cached Builder URL for {key}")
        except (json.JSONDecodeError, OSError) as e:
            self._log(f"Could not write URL cache: {e}")

    def _try_direct_builder_url(self, url: str) -> bool:
        """Navigate directly to a Builder URL. Returns True on success."""
        self._log(f"Trying direct Builder URL: {url[:80]}...")
        try:
            self._page.goto(url, wait_until="load", timeout=60000)
            self._page.wait_for_timeout(10000)

            # Verify we're on the Builder page (not an error page)
            current = self._page.url
            if "agentAuthoringBuilder" in current or "AgentAuthoring" in current:
                console.print("[green]\u2713[/green] Agent Builder loaded (direct URL)")
                return True

            # Check page content — Builder shows agent name, topics, etc.
            page_text = self._page.evaluate("() => document.body.innerText")
            if "Preview" in page_text and "Page not found" not in page_text:
                console.print("[green]\u2713[/green] Agent Builder loaded (direct URL)")
                return True

        except Exception as e:
            self._log(f"Direct URL failed: {e}")

        return False

    def _navigate_to_builder(self) -> None:
        """Navigate to the Agentforce Builder for the target agent.

        Priority:
          0. SOQL-constructed URL (fastest, ~2s, no UI navigation)
          1. --builder-url (explicit) -> goto directly
          2. Cached URL from previous run -> goto directly
          3. Studio discovery (existing logic) -> cache on success
          4. If cached URL fails -> fall back to Studio, update cache
        """
        # --- Priority 0: Construct URL from SOQL (fastest, no UI) ---
        if not self.builder_url:
            constructed_url = self._construct_builder_url()
            if constructed_url:
                if self._try_direct_builder_url(constructed_url):
                    self._save_builder_url_cache(constructed_url)
                    return
                self._log("Constructed URL failed, trying other methods")

        # --- Priority 1: Explicit --builder-url ---
        if self.builder_url:
            if self._try_direct_builder_url(self.builder_url):
                self._save_builder_url_cache(self.builder_url)
                return
            self._log("Explicit --builder-url failed, falling back to Studio")

        # --- Priority 2: Cached URL ---
        cached_url = self._load_cached_builder_url()
        if cached_url:
            if self._try_direct_builder_url(cached_url):
                return
            self._log("Cached URL failed, falling back to Studio discovery")

        # --- Priority 3: Studio discovery ---
        self._navigate_via_studio()

    def _navigate_via_studio(self) -> None:
        """Navigate via Agentforce Studio agent list (original flow).

        The Studio URL varies by org: try standard-AgentforceStudio first,
        then EinsteinCopilot/home. The agent list shows MasterLabel, not
        DeveloperName, so we derive the display name.
        """
        self._log("Navigating to Agentforce Studio...")

        # --- Phase 1: Load the Studio agent list ---
        studio_loaded = False
        studio_urls = [
            # App-level URL (preferred — renders the full Builder experience)
            f"{self._instance_url}/lightning/n/standard-AgentforceStudio?c__nav=agents",
            # Setup URL (fallback)
            f"{self._instance_url}/lightning/setup/EinsteinCopilot/home",
        ]

        for url in studio_urls:
            self._log(f"Trying Studio: {url}")
            self._page.goto(url, wait_until="load", timeout=60000)
            self._page.wait_for_timeout(10000)
            page_text = self._page.evaluate("() => document.body.innerText")
            if "Page not found" not in page_text and "Agent" in page_text:
                studio_loaded = True
                self._log("Agentforce Studio loaded")
                break

        if not studio_loaded:
            raise RuntimeError(
                "Could not load Agentforce Studio. "
                "Verify Agentforce is enabled in the org."
            )

        # --- Phase 2: Find and click the agent in the list ---
        import re
        display_name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', self.agent_name)
        display_name_underscore = self.agent_name.replace("_", " ")

        search_names = list(dict.fromkeys([
            display_name,
            display_name_underscore,
            self.agent_name,
        ]))

        self._log(f"Searching for agent: {search_names}")

        for scroll_y in [0, 500, 1000, 1500, 2000]:
            self._page.evaluate(f"window.scrollTo(0, {scroll_y})")
            self._page.wait_for_timeout(1000)

            for name in search_names:
                try:
                    agent_link = self._page.locator(f'a:has-text("{name}")').first
                    if agent_link.is_visible(timeout=2000):
                        self._log(f"Found agent: '{name}' at scroll {scroll_y}")

                        href = agent_link.get_attribute("href") or ""
                        if "agentAuthoringBuilder" in href:
                            builder_url = f"{self._instance_url}{href}"
                            self._log(f"Builder URL: {builder_url}")
                            self._page.goto(
                                builder_url,
                                wait_until="load",
                                timeout=60000,
                            )
                            self._page.wait_for_timeout(15000)
                            # Cache for future runs
                            self._save_builder_url_cache(builder_url)
                        else:
                            agent_link.click()
                            self._page.wait_for_timeout(15000)

                        console.print(
                            f"[green]\u2713[/green] Agent Builder loaded"
                        )
                        return
                except Exception:
                    continue

        # If we still can't find it, screenshot and raise
        ss = self._screenshot("agent-not-found-in-list")
        raise RuntimeError(
            f"Could not find agent '{self.agent_name}' in the Studio list. "
            f"Searched for: {search_names}. "
            f"Screenshot: {ss}"
        )

    # ------------------------------------------------------------------
    # 5. Trace capture setup
    # ------------------------------------------------------------------

    def _setup_trace_capture(self) -> None:
        """Register response listener to intercept trace API responses.

        The Builder's JavaScript calls getSimulationPlanTraces automatically
        after each agent response. We intercept the Aura response to extract
        the PlanSuccessResponse containing the full 13-step-type trace.
        """
        def on_response(response):
            try:
                url = response.url
                if "/aura" not in url:
                    return
                post_data = response.request.post_data or ""
                if "getSimulationPlanTraces" not in post_data:
                    return
                body = response.text()
                self._captured_traces.append(body)
                self._log(f"Captured trace response ({len(body)} chars)")
            except Exception as e:
                self._log(f"Error in trace capture listener: {e}")

        self._page.on("response", on_response)
        self._log("Trace capture listener registered")

    # ------------------------------------------------------------------
    # 6. Test panel interaction
    # ------------------------------------------------------------------

    def _reset_conversation(self) -> None:
        """Reset the Builder simulator to start a fresh conversation."""
        try:
            reset_btn = self._page.locator('button[title="Reset Simulator"]').first
            if reset_btn.is_visible(timeout=3000):
                reset_btn.click()
                self._page.wait_for_timeout(3000)
                console.print("[green]\u2713[/green] Simulator reset")
                return
        except Exception:
            pass

        # Fallback: try "Reset Chat"
        try:
            reset_btn = self._page.locator('button[title="Reset Chat"]').first
            if reset_btn.is_visible(timeout=2000):
                reset_btn.click()
                self._page.wait_for_timeout(3000)
                console.print("[green]\u2713[/green] Chat reset")
                return
        except Exception:
            pass

        self._log("No reset button found — conversation may have existing history")

    def _open_test_panel(self) -> None:
        """Switch to the Preview tab in the Agentforce Builder.

        The Builder has two views, selectable via tabs at the top:
          - "Agent Defi..." tab: shows the agent definition (topics, etc.)
          - "Preview" tab: shows the test/simulation chat panel

        There's ALSO an "Agentforce" sidebar on the right that is a
        *builder assistant* (helps you edit the agent). This is NOT
        the test panel. We must:
          1. Click the "Preview" TAB at the top
          2. Close the Agentforce sidebar (it overlaps and has its own
             chat input that could be mistakenly targeted)
          3. Wait for the Preview chat to render
        """
        # First close the Agentforce sidebar to avoid confusion
        self._close_sidebar()

        # Click the Preview tab
        preview_selectors = [
            'text="Preview"',
            '[role="tab"]:has-text("Preview")',
            'button:has-text("Preview")',
            '[data-label="Preview"]',
            'a:has-text("Preview")',
        ]

        for selector in preview_selectors:
            try:
                tab = self._page.locator(selector).first
                if tab.is_visible(timeout=3000):
                    self._log(f"Found Preview tab: {selector}")
                    tab.click()
                    self._page.wait_for_timeout(8000)
                    console.print(f"[green]\u2713[/green] Preview tab opened")
                    return
            except Exception:
                continue

        # Screenshot on failure for debugging
        ss = self._screenshot("test-panel-not-found")
        self._log(f"Preview tab not found. Screenshot: {ss}")

    def _close_sidebar(self) -> None:
        """Close the Agentforce builder-assistant sidebar if present."""
        close_selectors = [
            'button[aria-label*="close" i]',
            'button[aria-label*="dismiss" i]',
            'button[aria-label*="Toggle Panel"]',
            'button[title*="Close" i]',
        ]
        for selector in close_selectors:
            try:
                btn = self._page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    self._page.wait_for_timeout(1000)
                    self._log(f"Closed sidebar: {selector}")
                    return
            except Exception:
                continue

        self._log("No sidebar to close (may already be hidden)")

    def _find_chat_input(self):
        """Locate the chat input element in the Builder test panel.

        The Builder uses LWC shadow DOM. Playwright's locators pierce
        shadow DOM by default in recent versions. We try multiple
        selector patterns and fall back to JS shadow traversal.
        """
        selectors = [
            'textarea[placeholder*="message" i]',
            'input[placeholder*="message" i]',
            'textarea[placeholder*="type" i]',
            'input[placeholder*="type" i]',
            'textarea[placeholder*="ask" i]',
            'input[placeholder*="ask" i]',
            'textarea[aria-label*="message" i]',
            'input[aria-label*="message" i]',
            'lightning-textarea textarea',
            'lightning-input-rich-text [contenteditable]',
        ]

        for selector in selectors:
            try:
                el = self._page.locator(selector).first
                if el.is_visible(timeout=1000):
                    return el
            except Exception:
                continue

        # Fallback: traverse shadow DOM via JavaScript
        handle = self._page.evaluate_handle('''() => {
            function findInput(root) {
                const tags = root.querySelectorAll(
                    'textarea, input[type="text"], [contenteditable="true"]'
                );
                for (const el of tags) {
                    const ph = (el.placeholder || '').toLowerCase();
                    const label = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (ph.includes('message') || ph.includes('type') ||
                        ph.includes('ask') || label.includes('message')) {
                        if (el.offsetParent !== null) return el;
                    }
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        const found = findInput(el.shadowRoot);
                        if (found) return found;
                    }
                }
                return null;
            }
            return findInput(document);
        }''')

        if handle:
            element = handle.as_element()
            if element:
                return element

        return None

    def _send_utterance(self, text: str) -> None:
        """Type and submit a test utterance in the Builder test panel."""
        input_el = self._find_chat_input()

        if input_el is None:
            ss = self._screenshot("chat-input-not-found")
            raise RuntimeError(
                "Could not find the test panel chat input. "
                "Ensure the Builder test panel is visible. "
                "You may need to click 'Test' or 'Preview' to open it. "
                f"Debug screenshot: {ss}"
            )

        input_el.fill("")
        input_el.fill(text)
        self._page.wait_for_timeout(300)

        # Prefer the explicit send button (aria-label="send")
        try:
            send_btn = self._page.locator('button[aria-label="send"]').first
            if send_btn.is_visible(timeout=1000):
                send_btn.click()
                self._log(f"Sent via send button: {text}")
                return
        except Exception:
            pass

        # Fallback to Enter key
        input_el.press("Enter")
        self._log(f"Sent via Enter key: {text}")

    # ------------------------------------------------------------------
    # 7. Agent error detection (fail-fast)
    # ------------------------------------------------------------------

    def _check_for_agent_error(self) -> Optional[str]:
        """Inspect the last chat bubble for agent error patterns.

        Returns the error text if an error is detected, None otherwise.
        Called during trace polling to fail-fast instead of waiting
        the full timeout when the agent errors immediately.
        """
        try:
            # Get the text of the last chat message bubble
            last_bubble = self._page.evaluate('''() => {
                const bubbles = document.querySelectorAll(
                    '[class*="chatMessage"], [class*="chat-message"], ' +
                    '[class*="message-bubble"], [class*="slds-chat-message"]'
                );
                if (bubbles.length === 0) return "";
                return bubbles[bubbles.length - 1].innerText || "";
            }''')

            if not last_bubble:
                return None

            lower = last_bubble.lower()
            for pattern in _AGENT_ERROR_PATTERNS:
                if pattern in lower:
                    return last_bubble.strip()[:200]

        except Exception:
            pass

        return None

    def _check_for_builder_error(self) -> Optional[str]:
        """Check if the Builder itself shows an error modal or lost the chat panel.

        Returns a description of the error, or None if no error.
        """
        try:
            # Check for error modals
            modal = self._page.locator('[class*="modal"][class*="error"], [role="alertdialog"]').first
            if modal.is_visible(timeout=500):
                text = modal.inner_text()
                self._screenshot("builder-error-modal")
                return f"Builder error modal: {text[:150]}"
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # 8. Trace waiting and parsing
    # ------------------------------------------------------------------

    def _wait_for_trace(self, initial_count: int) -> str:
        """Wait for a new trace response to appear in the capture buffer.

        Polls the buffer until a new trace arrives or timeout is reached.
        During polling, also checks for agent errors to fail-fast.
        """
        start = time.time()
        error_check_interval = 3.0  # Check for errors every 3s
        last_error_check = 0.0

        while len(self._captured_traces) <= initial_count:
            elapsed = time.time() - start
            if elapsed > self.timeout:
                raise TimeoutError(
                    f"No trace received within {self.timeout}s. "
                    f"The agent may still be processing, or the Builder "
                    f"did not fetch the trace."
                )

            # Periodically check for agent/builder errors (fail-fast)
            if elapsed - last_error_check >= error_check_interval:
                last_error_check = elapsed

                agent_error = self._check_for_agent_error()
                if agent_error:
                    self._log(f"Agent error detected: {agent_error}")
                    # Wait briefly for any partial trace to arrive
                    self._page.wait_for_timeout(2000)
                    if len(self._captured_traces) > initial_count:
                        # Got a partial trace — return it
                        break
                    raise RuntimeError(
                        f"Agent error: {agent_error}"
                    )

                builder_error = self._check_for_builder_error()
                if builder_error:
                    self._screenshot("builder-error")
                    raise RuntimeError(builder_error)

            # Let Playwright process events
            self._page.wait_for_timeout(500)

        return self._captured_traces[-1]

    def _parse_trace(self, aura_response: str) -> dict[str, Any]:
        """Parse PlanSuccessResponse from Aura JSON envelope.

        Aura wraps responses in: { actions: [{ returnValue: "..." }] }
        IMPORTANT: returnValue is a **JSON string** (double-serialized),
        not a nested dict. We must JSON.parse it a second time.
        """
        try:
            data = json.loads(aura_response)
        except json.JSONDecodeError:
            raise ValueError("Trace response is not valid JSON")

        # Navigate the Aura envelope to find PlanSuccessResponse
        actions = data.get("actions", [])
        for action in actions:
            rv = action.get("returnValue")
            if rv is None:
                continue

            # Case 1: returnValue is a JSON string (double-serialized)
            if isinstance(rv, str):
                try:
                    parsed_rv = json.loads(rv)
                    if isinstance(parsed_rv, dict):
                        if parsed_rv.get("type") == "PlanSuccessResponse" or "plan" in parsed_rv:
                            return parsed_rv
                except json.JSONDecodeError:
                    self._log(f"Could not parse returnValue string ({len(rv)} chars)")
                    continue

            # Case 2: returnValue is already a dict
            if isinstance(rv, dict):
                if rv.get("type") == "PlanSuccessResponse":
                    return rv
                if "plan" in rv:
                    return rv

        # Maybe the response IS the PlanSuccessResponse
        if data.get("type") == "PlanSuccessResponse" or "plan" in data:
            return data

        self._log("Warning: PlanSuccessResponse not found in Aura envelope")
        return {"raw_aura_response": data}

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def run(
        self,
        utterances: list[str],
        on_turn: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute the full trace capture flow.

        Args:
            utterances: Test utterance strings to send sequentially.
            on_turn: Optional callback fired after each trace is captured
                and parsed. Receives the parsed trace dict (or error entry).
                Use this to render per-turn panels immediately instead of
                waiting for all traces to complete.

        Returns:
            List of parsed trace dicts (one per utterance).
            Error entries have an "error" key instead of "plan".
        """
        traces: list[dict[str, Any]] = []

        try:
            # Phase 1: Setup with Rich status spinners
            if self.cdp_port:
                with console.status("[bold cyan]Connecting to Chrome...") as status:
                    self._connect_to_existing()
            else:
                with console.status("[bold cyan]Resolving agent...") as status:
                    self._resolve_agent()

                    status.update("[bold cyan]Launching browser...")
                    self._launch_browser()

                    status.update("[bold cyan]Authenticating...")
                    self._authenticate()

                    status.update("[bold cyan]Navigating to Builder...")
                    self._navigate_to_builder()

            with console.status("[bold cyan]Opening test panel...") as status:
                self._open_test_panel()
                self._reset_conversation()
                self._setup_trace_capture()

            console.print(
                f"\n[bold cyan]\U0001f504 Sending {len(utterances)} "
                f"utterance(s)...[/bold cyan]\n"
            )

            # Phase 2: Utterance loop
            for i, text in enumerate(utterances, 1):
                console.print(
                    f"[cyan]Turn {i}/{len(utterances)}:[/cyan] {text}"
                )

                initial_count = len(self._captured_traces)
                self._send_utterance(text)

                try:
                    raw = self._wait_for_trace(initial_count)
                    parsed = self._parse_trace(raw)
                    parsed["_utterance"] = text
                    parsed["_turn"] = i
                    traces.append(parsed)

                    # Persist raw trace
                    trace_path = self.output_dir / "traces" / f"turn-{i}.json"
                    trace_path.write_text(json.dumps(parsed, indent=2))

                    if on_turn:
                        on_turn(parsed)

                except TimeoutError as e:
                    console.print(f"  [yellow]\u26a0\ufe0f  {e}[/yellow]")
                    error_entry = {
                        "error": str(e),
                        "_utterance": text,
                        "_turn": i,
                    }
                    traces.append(error_entry)
                    trace_path = self.output_dir / "traces" / f"turn-{i}.json"
                    trace_path.write_text(json.dumps(error_entry, indent=2))

                    if on_turn:
                        on_turn(error_entry)

                except RuntimeError as e:
                    # Agent error (fail-fast) — capture partial trace
                    console.print(f"  [red]\u274c {e}[/red]")
                    error_entry = {
                        "error": str(e),
                        "_utterance": text,
                        "_turn": i,
                    }
                    traces.append(error_entry)
                    trace_path = self.output_dir / "traces" / f"turn-{i}.json"
                    trace_path.write_text(json.dumps(error_entry, indent=2))

                    if on_turn:
                        on_turn(error_entry)

                # Pause between utterances for the agent to reset
                if i < len(utterances):
                    self._page.wait_for_timeout(2000)

            console.print(
                f"\n[green]\u2713[/green] Captured "
                f"{sum(1 for t in traces if 'error' not in t)}"
                f"/{len(traces)} trace(s) successfully"
            )

        except Exception as e:
            console.print(f"\n[red]Error during trace capture: {e}[/red]")
            if self.verbose:
                import traceback
                console.print(traceback.format_exc())
            raise

        return traces

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Close browser and release Playwright resources.

        When connected to an existing browser (cdp_port mode), we
        disconnect without closing — the user's session stays open.
        """
        try:
            if self._browser:
                if self._connected_existing:
                    # Disconnect only — don't close the user's browser
                    pass
                else:
                    self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright_ctx:
                self._playwright_ctx.stop()
        except Exception:
            pass
        self._browser = None
        self._page = None
        self._context = None
        self._playwright_ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()
