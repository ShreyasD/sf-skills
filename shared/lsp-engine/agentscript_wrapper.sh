#!/usr/bin/env bash
# ============================================================================
# Agent Script LSP Wrapper for sf-skills Plugin
# ============================================================================
# This script discovers and invokes the Salesforce Agent Script Language Server
# from the VS Code extension. It's designed to be portable across user machines.
#
# IMPORTANT: VS Code Extension REQUIRED
# --------------------------------------
# Unlike LWC (which has a standalone npm package), the Agent Script Language
# Server is ONLY distributed bundled within the VS Code Agent Script extension.
# There is NO standalone npm package or separate download available.
#
# The server is located at:
#   ~/.vscode/extensions/salesforce.agent-script-language-client-*/server/server.js
#
# Prerequisites:
#   - VS Code with Agent Script extension installed (REQUIRED)
#   - Node.js 18+ installed
#
# Usage:
#   ./agentscript_wrapper.sh [--stdio]
#
# Environment:
#   LSP_LOG_FILE - Path to log file (optional, default: /dev/null)
#   NODE_PATH    - Custom Node.js binary path (optional, auto-detected)
# ============================================================================

set -euo pipefail

# Source VS Code extension directory discovery
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_vscode_common.sh"

# Configuration
LOG_FILE="${LSP_LOG_FILE:-/dev/null}"
NODE_BIN="${NODE_PATH:-}"

# Logging function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [lsp-engine] $*" >> "$LOG_FILE"
}

# Find Node.js binary
find_node() {
    if [[ -n "$NODE_BIN" ]] && [[ -x "$NODE_BIN" ]]; then
        echo "$NODE_BIN"
        return 0
    fi

    # Try common locations (cross-platform)
    local candidates=(
        "$(which node 2>/dev/null || true)"
        "/usr/local/bin/node"
        "/opt/homebrew/bin/node"
        "$HOME/.nvm/current/bin/node"
        "$HOME/.volta/bin/node"
        "/usr/bin/node"
    )

    for candidate in "${candidates[@]}"; do
        if [[ -n "$candidate" ]] && [[ -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

# Find VS Code extension directory (handles version updates)
find_vscode_extension() {
    local ext_base
    local pattern="salesforce.agent-script-language-client-*"

    if ! ext_base=$(find_vscode_ext_dir); then
        log "VS Code extensions directory not found. Searched: ~/.vscode/extensions, ~/.vscode-server/extensions, ~/.vscode-insiders/extensions, ~/.vscode-server-insiders/extensions, ~/.cursor/extensions"
        return 1
    fi

    # Find the newest version (sort by version number)
    local latest
    latest=$(find "$ext_base" -maxdepth 1 -type d -name "$pattern" 2>/dev/null | sort -V | tail -1)

    if [[ -n "$latest" ]] && [[ -f "$latest/server/server.js" ]]; then
        echo "$latest/server/server.js"
        return 0
    fi

    log "Agent Script extension not found in: $ext_base (set VSCODE_EXTENSIONS_DIR to override)"
    return 1
}

# Validate Node.js version (requires 18+)
validate_node_version() {
    local node_bin="$1"
    local version_output
    version_output=$("$node_bin" --version 2>/dev/null)

    # Extract major version (v22.21.0 -> 22)
    local major_version
    major_version=$(echo "$version_output" | grep -oE 'v([0-9]+)' | head -1 | tr -d 'v')

    if [[ -z "$major_version" ]]; then
        log "WARNING: Could not determine Node.js version"
        return 0  # Continue anyway
    fi

    if [[ "$major_version" -lt 18 ]]; then
        log "ERROR: Node.js 18+ required (found v$major_version)"
        echo "Error: Node.js 18+ required (found $version_output)" >&2
        return 1
    fi

    log "Node.js version: $version_output"
    return 0
}

# Main execution
main() {
    log "=== Agent Script LSP Wrapper Starting ==="

    # Find Node.js
    local node_bin
    if ! node_bin=$(find_node); then
        echo "Error: Node.js not found. Please install Node.js 18+." >&2
        log "ERROR: Node.js not found"
        exit 1
    fi
    log "Using Node.js: $node_bin"

    # Validate Node.js version
    if ! validate_node_version "$node_bin"; then
        exit 1
    fi

    # Discover LSP server from VS Code extension
    local server_path
    if ! server_path=$(find_vscode_extension); then
        echo "Error: Agent Script LSP server not found." >&2
        echo "Please install the VS Code Agent Script extension:" >&2
        echo "  1. Open VS Code" >&2
        echo "  2. Extensions (Cmd+Shift+X)" >&2
        echo "  3. Search: 'Agent Script' by Salesforce" >&2
        echo "  4. Install" >&2
        exit 1
    fi
    log "Server path: $server_path"

    # Execute the LSP server with stdio transport
    log "Starting LSP server..."
    exec "$node_bin" "$server_path" --stdio "$@"
}

main "$@"
