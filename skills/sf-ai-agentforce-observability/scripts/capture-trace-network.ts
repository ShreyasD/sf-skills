/**
 * capture-trace-network.ts
 *
 * Connects to a running Chrome instance via CDP (Chrome DevTools Protocol)
 * and captures all network requests related to Agentforce Builder trace
 * rendering. Exports structured JSON for analysis.
 *
 * Prerequisites:
 *   1. Launch Chrome with remote debugging:
 *      /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
 *        --remote-debugging-port=9222 \
 *        --user-data-dir=/tmp/chrome-debug
 *
 *   2. Navigate to your Salesforce org → Setup → Agentforce → Agents →
 *      [Agent] → Open in Builder
 *
 *   3. Run this script:
 *      npx tsx scripts/capture-trace-network.ts [options]
 *
 * Usage:
 *   npx tsx scripts/capture-trace-network.ts
 *   npx tsx scripts/capture-trace-network.ts --port 9222 --output ./captures
 *   npx tsx scripts/capture-trace-network.ts --duration 120
 *
 * The script will:
 *   - Connect to the Chrome instance
 *   - Start intercepting ALL network requests
 *   - Filter for Agentforce-relevant endpoints (aura, connect, einstein, etc.)
 *   - Wait for you to send a test message in the Builder
 *   - Capture the trace-related requests/responses
 *   - Export to timestamped JSON file
 */

import { chromium, type CDPSession, type Page, type BrowserContext } from "playwright";
import * as fs from "fs";
import * as path from "path";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

interface Config {
  /** Chrome remote debugging port */
  port: number;
  /** Output directory for capture files */
  outputDir: string;
  /** Capture duration in seconds (0 = manual stop via Ctrl+C) */
  duration: number;
  /** Include response bodies (can be large) */
  includeResponseBodies: boolean;
  /** Verbose logging to stderr */
  verbose: boolean;
}

function parseArgs(): Config {
  const args = process.argv.slice(2);
  const config: Config = {
    port: 9222,
    outputDir: "./captures",
    duration: 0,
    includeResponseBodies: true,
    verbose: false,
  };

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case "--port":
        config.port = parseInt(args[++i], 10);
        break;
      case "--output":
        config.outputDir = args[++i];
        break;
      case "--duration":
        config.duration = parseInt(args[++i], 10);
        break;
      case "--no-bodies":
        config.includeResponseBodies = false;
        break;
      case "--verbose":
        config.verbose = true;
        break;
      case "--help":
        console.log(`
Usage: npx tsx capture-trace-network.ts [options]

Options:
  --port <number>     Chrome remote debugging port (default: 9222)
  --output <dir>      Output directory for captures (default: ./captures)
  --duration <secs>   Auto-stop after N seconds (default: 0 = manual Ctrl+C)
  --no-bodies         Skip response body capture (smaller output)
  --verbose           Verbose logging to stderr
  --help              Show this help
`);
        process.exit(0);
    }
  }

  return config;
}

// ---------------------------------------------------------------------------
// Request/response matching keywords
// ---------------------------------------------------------------------------

/** URL patterns that indicate Agentforce trace-related requests */
const TRACE_URL_PATTERNS = [
  // Aura framework calls (most likely for Builder UI)
  /\/aura\?/i,
  // Connect API endpoints
  /\/services\/data\/v\d+\.\d+\/connect\//i,
  // Einstein AI endpoints
  /\/services\/data\/v\d+\.\d+\/einstein\//i,
  // Agent runtime API
  /\/einstein\/ai-agent\//i,
  // AI evaluations (testing API)
  /\/einstein\/ai-evaluations\//i,
  // LWR (Lightning Web Runtime)
  /\/lwr\//i,
  // Web runtime
  /\/webruntime\//i,
  // CometD / Bayeux (streaming)
  /\/cometd\//i,
  // Platform events
  /\/event\//i,
];

/** Aura action descriptors that may relate to trace/agent functionality */
const AURA_TRACE_DESCRIPTORS = [
  "trace",
  "step",
  "session",
  "agent",
  "copilot",
  "runtime",
  "bot",
  "reasoning",
  "planner",
  "action",
  "topic",
  "einstein",
  "GenAi",
  "AiAgent",
  "Builder",
  "orchestrat",
  "execution",
  "preview",
];

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

interface CapturedRequest {
  /** Monotonic capture index */
  index: number;
  /** ISO timestamp when request was sent */
  timestamp: string;
  /** CDP requestId for correlation */
  requestId: string;
  /** HTTP method */
  method: string;
  /** Full URL */
  url: string;
  /** Simplified URL (no query params) */
  urlPath: string;
  /** Request headers */
  requestHeaders: Record<string, string>;
  /** Request body (POST payloads) */
  requestBody: string | null;
  /** Parsed Aura descriptors (if Aura call) */
  auraDescriptors: string[];
  /** Response status code */
  responseStatus: number | null;
  /** Response headers */
  responseHeaders: Record<string, string>;
  /** Response body (if captured) */
  responseBody: string | null;
  /** Response content type */
  responseContentType: string | null;
  /** Whether this is an SSE stream */
  isSSE: boolean;
  /** Whether this is a WebSocket */
  isWebSocket: boolean;
  /** Duration in ms (if completed) */
  durationMs: number | null;
  /** Category tag */
  category: string;
  /** Why this request was captured */
  matchReason: string;
}

interface CaptureSession {
  /** When capture started */
  startTime: string;
  /** When capture ended */
  endTime: string | null;
  /** Chrome page URL at capture start */
  pageUrl: string;
  /** Total requests seen */
  totalRequestsSeen: number;
  /** Requests matched and captured */
  capturedRequests: CapturedRequest[];
  /** Summary statistics */
  summary: {
    byCategory: Record<string, number>;
    byMethod: Record<string, number>;
    auraDescriptorsFound: string[];
    uniqueEndpoints: string[];
    sseStreamsDetected: number;
    websocketsDetected: number;
  };
}

// ---------------------------------------------------------------------------
// Core capture logic
// ---------------------------------------------------------------------------

function isTraceRelevant(url: string, body: string | null): { relevant: boolean; reason: string } {
  // Check URL patterns
  for (const pattern of TRACE_URL_PATTERNS) {
    if (pattern.test(url)) {
      return { relevant: true, reason: `URL matches ${pattern.source}` };
    }
  }

  // Check Aura request body for trace-related descriptors
  if (body && url.includes("/aura")) {
    const bodyLower = body.toLowerCase();
    for (const descriptor of AURA_TRACE_DESCRIPTORS) {
      if (bodyLower.includes(descriptor.toLowerCase())) {
        return { relevant: true, reason: `Aura body contains "${descriptor}"` };
      }
    }
  }

  return { relevant: false, reason: "" };
}

function categorizeRequest(url: string, body: string | null): string {
  if (url.includes("/aura")) return "aura";
  if (url.includes("/connect/")) return "connect-api";
  if (url.includes("/einstein/ai-agent/")) return "agent-runtime";
  if (url.includes("/einstein/ai-evaluations/")) return "testing-api";
  if (url.includes("/einstein/")) return "einstein-api";
  if (url.includes("/cometd/")) return "streaming-cometd";
  if (url.includes("/lwr/") || url.includes("/webruntime/")) return "lwr";
  if (url.includes("/event/")) return "platform-event";
  return "other";
}

function extractAuraDescriptors(body: string | null): string[] {
  if (!body) return [];
  const descriptors: string[] = [];

  try {
    // Aura requests have message.actions[].descriptor format
    const parsed = JSON.parse(body.replace(/^message=/, ""));
    const actions = parsed?.message?.actions || parsed?.actions || [];
    for (const action of actions) {
      if (action.descriptor) {
        descriptors.push(action.descriptor);
      }
    }
  } catch {
    // Try regex fallback for URL-encoded Aura payloads
    const descriptorMatches = body.matchAll(/"descriptor"\s*:\s*"([^"]+)"/g);
    for (const match of descriptorMatches) {
      descriptors.push(match[1]);
    }
  }

  return descriptors;
}

async function main() {
  const config = parseArgs();
  const log = config.verbose
    ? (...args: unknown[]) => console.error("[capture]", ...args)
    : () => {};

  // Ensure output directory exists
  fs.mkdirSync(config.outputDir, { recursive: true });

  console.error(`
╔══════════════════════════════════════════════════════════════╗
║  Agentforce Builder Trace — Network Capture                 ║
╠══════════════════════════════════════════════════════════════╣
║  Connecting to Chrome on port ${String(config.port).padEnd(28)}║
║  Output: ${config.outputDir.padEnd(49)}║
║  Duration: ${config.duration > 0 ? `${config.duration}s` : "manual (Ctrl+C)"}${" ".repeat(Math.max(0, 47 - (config.duration > 0 ? `${config.duration}s` : "manual (Ctrl+C)").length))}║
╚══════════════════════════════════════════════════════════════╝
`);

  // Connect to running Chrome
  let browser;
  try {
    browser = await chromium.connectOverCDP(`http://localhost:${config.port}`);
  } catch (err) {
    console.error(`
❌ Could not connect to Chrome on port ${config.port}.

Make sure Chrome is running with remote debugging enabled:

  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
    --remote-debugging-port=${config.port} \\
    --user-data-dir=/tmp/chrome-debug

Then navigate to your Agentforce Builder page before running this script.
`);
    process.exit(1);
  }

  log("Connected to Chrome");

  // Get the active page (most recently used tab)
  const contexts: BrowserContext[] = browser.contexts();
  let targetPage: Page | null = null;

  for (const ctx of contexts) {
    const pages = ctx.pages();
    for (const p of pages) {
      const url = p.url();
      // Prefer page that looks like Salesforce Setup / Builder
      if (
        url.includes("lightning") ||
        url.includes("salesforce.com") ||
        url.includes("force.com")
      ) {
        targetPage = p;
        break;
      }
    }
    if (targetPage) break;
  }

  if (!targetPage) {
    // Fall back to the first available page
    const allPages = contexts.flatMap((c) => c.pages());
    targetPage = allPages[0] || null;
  }

  if (!targetPage) {
    console.error("❌ No browser pages found. Open a tab first.");
    process.exit(1);
  }

  console.error(`📍 Target page: ${targetPage.url()}\n`);

  // Create CDP session for low-level network interception
  const cdp: CDPSession = await targetPage.context().newCDPSession(targetPage);
  await cdp.send("Network.enable", {
    maxTotalBufferSize: 100 * 1024 * 1024, // 100MB buffer
    maxResourceBufferSize: 10 * 1024 * 1024, // 10MB per resource
  });

  // Tracking state
  const session: CaptureSession = {
    startTime: new Date().toISOString(),
    endTime: null,
    pageUrl: targetPage.url(),
    totalRequestsSeen: 0,
    capturedRequests: [],
    summary: {
      byCategory: {},
      byMethod: {},
      auraDescriptorsFound: [],
      uniqueEndpoints: [],
      sseStreamsDetected: 0,
      websocketsDetected: 0,
    },
  };

  const pendingRequests = new Map<string, CapturedRequest>();
  let captureIndex = 0;

  // ---------------------------------------------------------------------------
  // CDP Network event handlers
  // ---------------------------------------------------------------------------

  cdp.on("Network.requestWillBeSent", (params: any) => {
    session.totalRequestsSeen++;
    const url: string = params.request.url;
    const method: string = params.request.method;
    const body: string | null = params.request.postData || null;

    const { relevant, reason } = isTraceRelevant(url, body);
    if (!relevant) return;

    const urlObj = new URL(url);
    const captured: CapturedRequest = {
      index: captureIndex++,
      timestamp: new Date().toISOString(),
      requestId: params.requestId,
      method,
      url,
      urlPath: urlObj.pathname,
      requestHeaders: params.request.headers || {},
      requestBody: body,
      auraDescriptors: extractAuraDescriptors(body),
      responseStatus: null,
      responseHeaders: {},
      responseBody: null,
      responseContentType: null,
      isSSE: false,
      isWebSocket: false,
      durationMs: null,
      category: categorizeRequest(url, body),
      matchReason: reason,
    };

    pendingRequests.set(params.requestId, captured);

    // Log live
    const descriptorInfo =
      captured.auraDescriptors.length > 0
        ? ` [${captured.auraDescriptors.join(", ")}]`
        : "";
    console.error(
      `  📡 #${captured.index} ${method} ${urlObj.pathname}${descriptorInfo}`
    );
  });

  cdp.on("Network.responseReceived", (params: any) => {
    const captured = pendingRequests.get(params.requestId);
    if (!captured) return;

    captured.responseStatus = params.response.status;
    captured.responseHeaders = params.response.headers || {};
    captured.responseContentType =
      params.response.headers?.["content-type"] ||
      params.response.headers?.["Content-Type"] ||
      null;

    // Detect SSE streams
    if (
      captured.responseContentType?.includes("text/event-stream") ||
      captured.responseContentType?.includes("event-stream")
    ) {
      captured.isSSE = true;
      session.summary.sseStreamsDetected++;
      console.error(`  🌊 SSE stream detected: ${captured.urlPath}`);
    }

    // Calculate duration
    if (params.response.timing) {
      captured.durationMs = Math.round(
        (params.response.timing.receiveHeadersEnd || 0) -
          (params.response.timing.sendStart || 0)
      );
    }
  });

  cdp.on("Network.loadingFinished", async (params: any) => {
    const captured = pendingRequests.get(params.requestId);
    if (!captured) return;

    // Fetch response body
    if (config.includeResponseBodies) {
      try {
        const { body, base64Encoded } = await cdp.send(
          "Network.getResponseBody",
          { requestId: params.requestId }
        );
        captured.responseBody = base64Encoded
          ? Buffer.from(body, "base64").toString("utf-8")
          : body;
      } catch {
        log(`Could not get body for ${captured.urlPath}`);
      }
    }

    // Finalize and move to captured list
    pendingRequests.delete(params.requestId);
    session.capturedRequests.push(captured);

    log(
      `  ✅ #${captured.index} completed (${captured.responseStatus}, ${captured.durationMs ?? "?"}ms)`
    );
  });

  cdp.on("Network.loadingFailed", (params: any) => {
    const captured = pendingRequests.get(params.requestId);
    if (!captured) return;

    captured.responseStatus = -1;
    captured.responseBody = `FAILED: ${params.errorText}`;
    pendingRequests.delete(params.requestId);
    session.capturedRequests.push(captured);

    console.error(`  ❌ #${captured.index} failed: ${params.errorText}`);
  });

  // WebSocket tracking
  cdp.on("Network.webSocketCreated", (params: any) => {
    session.summary.websocketsDetected++;
    console.error(`  🔌 WebSocket opened: ${params.url}`);

    const captured: CapturedRequest = {
      index: captureIndex++,
      timestamp: new Date().toISOString(),
      requestId: params.requestId,
      method: "WS",
      url: params.url,
      urlPath: new URL(params.url).pathname,
      requestHeaders: {},
      requestBody: null,
      auraDescriptors: [],
      responseStatus: 101,
      responseHeaders: {},
      responseBody: null,
      responseContentType: "websocket",
      isSSE: false,
      isWebSocket: true,
      durationMs: null,
      category: "websocket",
      matchReason: "WebSocket connection",
    };
    session.capturedRequests.push(captured);
  });

  // Also capture WebSocket frames for content
  const wsFrames: { requestId: string; direction: string; data: string; timestamp: string }[] = [];

  cdp.on("Network.webSocketFrameReceived", (params: any) => {
    wsFrames.push({
      requestId: params.requestId,
      direction: "received",
      data: params.response?.payloadData || "",
      timestamp: new Date().toISOString(),
    });
    log(`  🔌 WS frame received (${params.response?.payloadData?.length || 0} chars)`);
  });

  cdp.on("Network.webSocketFrameSent", (params: any) => {
    wsFrames.push({
      requestId: params.requestId,
      direction: "sent",
      data: params.response?.payloadData || "",
      timestamp: new Date().toISOString(),
    });
    log(`  🔌 WS frame sent (${params.response?.payloadData?.length || 0} chars)`);
  });

  // Also capture EventSource (SSE) data via the Fetch domain
  await cdp.send("Fetch.enable", {
    patterns: [
      { urlPattern: "*", requestStage: "Response" },
    ],
    handleAuthRequests: false,
  }).catch(() => {
    // Fetch.enable may not be supported in all Chrome versions
    log("Fetch domain not available, SSE body capture may be limited");
  });

  cdp.on("Fetch.requestPaused", async (params: any) => {
    // Continue the request (don't block) but log if it's SSE
    try {
      await cdp.send("Fetch.continueRequest", {
        requestId: params.requestId,
      });
    } catch {
      // Ignore - request may have already been handled
    }
  });

  // ---------------------------------------------------------------------------
  // Capture timer / shutdown
  // ---------------------------------------------------------------------------

  console.error(`\n🎬 Capture started. Now send a test message in the Agentforce Builder.`);
  console.error(`   Press Ctrl+C to stop capturing.\n`);

  let running = true;

  const shutdown = async () => {
    if (!running) return;
    running = false;

    console.error(`\n⏹️  Stopping capture...\n`);

    session.endTime = new Date().toISOString();

    // Move any pending requests to captured
    for (const [, req] of pendingRequests) {
      req.responseBody = "(request still pending at capture end)";
      session.capturedRequests.push(req);
    }

    // Build summary
    const categories: Record<string, number> = {};
    const methods: Record<string, number> = {};
    const endpoints = new Set<string>();
    const allDescriptors = new Set<string>();

    for (const req of session.capturedRequests) {
      categories[req.category] = (categories[req.category] || 0) + 1;
      methods[req.method] = (methods[req.method] || 0) + 1;
      endpoints.add(`${req.method} ${req.urlPath}`);
      for (const d of req.auraDescriptors) {
        allDescriptors.add(d);
      }
    }

    session.summary = {
      byCategory: categories,
      byMethod: methods,
      auraDescriptorsFound: [...allDescriptors].sort(),
      uniqueEndpoints: [...endpoints].sort(),
      sseStreamsDetected: session.summary.sseStreamsDetected,
      websocketsDetected: session.summary.websocketsDetected,
    };

    // Write output
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const outFile = path.join(config.outputDir, `trace-capture-${timestamp}.json`);

    const output = {
      ...session,
      websocketFrames: wsFrames,
    };

    fs.writeFileSync(outFile, JSON.stringify(output, null, 2));

    // Print summary
    console.error(`
╔══════════════════════════════════════════════════════════════╗
║  Capture Summary                                            ║
╠══════════════════════════════════════════════════════════════╣
║  Total requests seen:    ${String(session.totalRequestsSeen).padEnd(35)}║
║  Requests captured:      ${String(session.capturedRequests.length).padEnd(35)}║
║  SSE streams:            ${String(session.summary.sseStreamsDetected).padEnd(35)}║
║  WebSockets:             ${String(session.summary.websocketsDetected).padEnd(35)}║
╠══════════════════════════════════════════════════════════════╣
║  By Category:                                               ║`);

    for (const [cat, count] of Object.entries(categories).sort(
      (a, b) => b[1] - a[1]
    )) {
      console.error(
        `║    ${cat.padEnd(25)} ${String(count).padEnd(30)}║`
      );
    }

    if (allDescriptors.size > 0) {
      console.error(
        `╠══════════════════════════════════════════════════════════════╣`
      );
      console.error(
        `║  Aura Descriptors Found:                                    ║`
      );
      for (const d of [...allDescriptors].sort()) {
        const truncated = d.length > 55 ? d.slice(0, 52) + "..." : d;
        console.error(`║    ${truncated.padEnd(56)}║`);
      }
    }

    console.error(
      `╠══════════════════════════════════════════════════════════════╣`
    );
    console.error(
      `║  Output: ${outFile.padEnd(51)}║`
    );
    console.error(
      `╚══════════════════════════════════════════════════════════════╝`
    );

    // Also write a compact endpoint summary
    const summaryFile = path.join(
      config.outputDir,
      `trace-endpoints-${timestamp}.txt`
    );
    const summaryLines = [
      `Agentforce Trace Capture — ${session.startTime}`,
      `Page: ${session.pageUrl}`,
      ``,
      `UNIQUE ENDPOINTS (${endpoints.size}):`,
      ...session.summary.uniqueEndpoints.map((e) => `  ${e}`),
      ``,
      `AURA DESCRIPTORS (${allDescriptors.size}):`,
      ...[...allDescriptors].sort().map((d) => `  ${d}`),
      ``,
      `CAPTURED REQUESTS (${session.capturedRequests.length}):`,
      ...session.capturedRequests.map(
        (r) =>
          `  #${r.index} [${r.category}] ${r.method} ${r.urlPath} → ${r.responseStatus} (${r.durationMs ?? "?"}ms)`
      ),
    ];
    fs.writeFileSync(summaryFile, summaryLines.join("\n"));
    console.error(`\n📄 Endpoint summary: ${summaryFile}`);

    await browser.close().catch(() => {});
    process.exit(0);
  };

  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  if (config.duration > 0) {
    setTimeout(shutdown, config.duration * 1000);
    console.error(`   Auto-stopping in ${config.duration} seconds.\n`);
  }

  // Keep alive
  await new Promise(() => {});
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
