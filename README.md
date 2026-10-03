# TermuxBridge

TermuxBridge is the canonical ChatGPT ↔ Android/Termux execution bridge.

## Architecture

```text
ChatGPT
  ↓
Secure MCP Tunnel
  ↓
TermuxBridge
  ↓
Android / Termux
  ├─ local MCP HTTP server
  ├─ files and text operations
  ├─ command execution
  ├─ background jobs
  └─ service-specific adapters / browser automation
```

The deployed bridge currently runs inside the Termux application sandbox, not as Android root.

## Canonical identity

- Project name: **TermuxBridge**
- Repository: **ZTD38F/TermuxBridge**
- Default branch: `main`
- Existing phone deployment path is retained during migration to avoid breaking the working installation:
  `~/termux-mcp-bridge`
- Local MCP endpoint used by the existing deployment:
  `http://127.0.0.1:8765/mcp`

## Source-of-truth policy

GitHub is the canonical source for TermuxBridge code, documentation, installer logic and version history.

The first exact, secret-filtered import of the working phone runtime is complete. The tracked files under `runtime/` are the canonical bridge runtime source.

Normal workflow:

1. Changes are made in this repository.
2. CI validates Python, shell syntax, update safety and credential patterns.
3. The `stable` branch is the deployment channel for the phone.
4. The phone updater stages the exact stable commit, validates it, backs up the current managed files, restarts the bridge, verifies health, and rolls back on failure.
5. Runtime fixes made directly on the phone must still be synchronized back to GitHub.
6. Secrets, tokens, cookies, browser profiles, logs, tunnel binaries and generated state are never committed.

## Operating principles

- Inspect current state before mutation.
- Prefer the smallest targeted fix over reinstalling the bridge.
- Do not reinstall an existing working setup from scratch unless a verified failure requires it.
- Separate MCP health, tunnel health, proxy/TLS health and application health.
- Verify the real target state after every meaningful change.
- Prefer APIs/HTTP over GUI automation when supported.
- Browser/CDP automation is reserved for account features without adequate official APIs.
- Never claim a capability is operational merely because code exists; verify the exposed tool and the real end-to-end result.

## User-facing command

The intended daily interface is deliberately minimal:

```bash
gpt
```

The `gpt` command should establish/re-establish the bridge connection without requiring the user to remember internal start/status/tunnel commands.

## Automatic updates

TermuxBridge uses a conservative stable-channel updater.

```text
GitHub stable commit
      ↓
download to staging
      ↓
Python + Bash validation
      ↓
backup current managed runtime
      ↓
stop bridge
      ↓
activate new runtime
      ↓
start + health/status verification
      ↓
success, or automatic rollback
```

The updater manages only the tracked bridge runtime files. It never replaces `secrets/`, authenticated browser profiles, logs, PID files, local jobs, or the locally downloaded tunnel binaries.

The phone uses Android's Termux:API job scheduler with a persistent daily job. Manage it with:

```bash
termuxbridgectl auto-update-status
termuxbridgectl auto-update-enable
termuxbridgectl auto-update-disable
```

Manual checks and updates:

```bash
termuxbridgectl check
termuxbridgectl update
```

## Security

Never commit:

- OpenAI or tunnel API keys
- OAuth access/refresh tokens
- cookies or session data
- Google credentials or OTP codes
- Chromium user profiles
- private tunnel URLs
- generated logs containing sensitive data
- local environment files containing secrets

See `SECURITY.md` and `.gitignore`.

## Current deployment state

The canonical runtime snapshot has been imported from the working phone without secrets or generated state. GitHub is now the source of truth for the managed TermuxBridge runtime. The current phone deployment should follow the verified `stable` branch through the transactional updater.
