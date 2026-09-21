# TermuxBridge Architecture

## Core path

```text
ChatGPT
  ↓
Secure MCP Tunnel
  ↓
Android-compatible tunnel runtime
  ↓
local TermuxBridge MCP service
  ↓
Android / Termux capabilities
```

The historical Android deployment evolved away from a desktop-style stdio child-process design toward a standalone local HTTP MCP service because Android/Termux runtime restrictions caused incompatibilities in the full Linux tunnel client.

## Known local endpoints

Existing deployment conventions:

- MCP: `http://127.0.0.1:8765/mcp`
- Health: `http://127.0.0.1:8765/healthz`
- Chromium CDP, when browser automation is enabled: `127.0.0.1:9222`

Historical builds also used a local network/proxy helper around port `8877`. Exact current implementation must be imported from the phone before it is documented as current source code.

## Core capability classes

The verified bridge design has supported:

- bridge/runtime status
- bounded file listing and text reads
- text search
- protected text writes with backup/hash safeguards
- command execution as the Termux user
- long-running background jobs with status and log retrieval

Service-specific adapters have also been developed on top of the bridge. Those adapters are separate from the core bridge and must not be described as operational unless their published tool path and account-side end-to-end behavior are freshly verified.

## Google automation routing

Preferred routing:

```text
ChatGPT
  ↓
TermuxBridge
  ↓
API / HTTP adapter when available
  ↓
browser/CDP automation only when required
  ↓
Google account
```

Examples from the existing system:

- Google Tasks: API/HTTP preferred.
- Google Keep: browser/CDP fallback for unsupported account operations.
- Google Maps personal Saved Lists: Places/API for canonical place resolution plus authenticated browser automation for list writes where no complete official write API exists.

## Separation from ServerBridge

```text
TermuxBridge → Android / Termux
ServerBridge → VPS / Linux
```

Do not route VPS administration through the phone merely because Termux can SSH to the server when ServerBridge already provides the direct execution domain.

## Migration invariant

The exact working files on the phone must be imported before GitHub is considered a complete source snapshot. Historical chat reconstruction is documentation evidence, not a substitute for the exact runtime files.
