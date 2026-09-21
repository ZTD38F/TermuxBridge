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

Until the first exact import of the existing phone runtime is completed, the phone deployment remains the authoritative source for any file that has not yet been copied into this repository. Do not reconstruct missing runtime files from memory and present them as exact originals.

After the initial import:

1. Changes are made in or synchronized to this repository.
2. The repository version is reviewed before deployment.
3. Deployment to Termux is verified on the phone.
4. Runtime fixes made directly on the phone must be synchronized back to GitHub before they are considered complete.
5. Secrets, tokens, cookies, browser profiles and generated logs are never committed.

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

## Current migration state

The repository has now been initialized as the canonical project location.

The next required migration step is an **exact, secret-filtered snapshot of the working phone directory** `~/termux-mcp-bridge`. Runtime files should be imported from the device rather than recreated from historical chat descriptions.
