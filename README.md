# TermuxBridge

TermuxBridge is the canonical ChatGPT ↔ Android/Termux execution bridge.

## Architecture

```text
ChatGPT
  ↓
Secure MCP Tunnel (long-lived)
  ↓
authenticated local supervisor/router :8765
  ├─ active runtime generation A :18771
  └─ candidate runtime generation B :18772
       ↓
Android / Termux tools, jobs and adapters
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

The command is now shipped as `runtime/gpt` and installed to
`$PREFIX/bin/gpt` by the transactional updater. The previous local command
is preserved once at `~/termux-mcp-bridge/gpt.previous`. This prevents
obsolete launcher-side checks of a token-protected `/healthz` endpoint.

### Startup self-healing

`gpt` probes the authenticated supervisor and backend, reuses a fully healthy
stack, and performs a controlled full restart if necessary. During **full
startup only**, `recover_bridge_port.py` may send **SIGTERM** to exactly one
same-UID, path-verified TermuxBridge listener on `8765` or `8877`, and only
after its loopback server fingerprint matches. It refuses unknown or ambiguous
listeners, PID-reused processes, other users' processes and SIGKILL.
`stop_bridge.sh` also checks executable identity before honoring any PID file.

An HTTP `403` from the legacy backend's unauthenticated `/healthz` is
**expected**: backend checks use `X-Bridge-Backend-Token`, supervisor checks
use `X-Bridge-Token`. Do not make MCP authorization optional to work around
a launcher bug. Errors from the remote control plane (expired credentials,
denied access) cannot be safely resolved by rotating tokens automatically;
re-authorize the connection using the provider's supported flow.

If a pre-update `gpt` binary still uses the obsolete health probe, perform
one verified update with `termuxbridgectl update-now` to install the managed
launcher.

### Startup lock and interrupted-update recovery (v1.2.5)

Startup uses a new `.start.v2.lock`, because older processes may continue
holding the original `.start.lock` through inherited file descriptors. Every
new daemon explicitly closes descriptor 9 before backgrounding, preventing
stale daemon processes from locking out future `gpt` invocations.

An interrupted runtime update at `STAGED` can be resumed: a previously
staged immutable generation is reused only after it is verified byte-for-byte
against the signed/checksummed release files. Neither an active nor
different-generation directory is overwritten. A busy updater or startup
reports exit code 75 rather than silently indicating success.

If your terminal prints `Bridge start is already in progress` and reports
`UPDATE phase STAGED`, install this release using the standard verified
updater and allow it to finish before rerunning `gpt`:

```bash
bash ~/termux-mcp-bridge/update_termux.sh --force
hash -r
gpt
termuxbridgectl status
```

Do not delete `.start.lock` or `.update.lock` and do not kill unrelated
processes. If the update fails, inspect only the sanitized status first;
never share raw secret files or credentials.

### Tunnel recovery (v1.2.4)

The local MCP service, router and HTTPS proxy have **separate** lifecycle from
the remote ChatGPT control-plane tunnel. The managed `gpt` launcher no longer
tears down a healthy local MCP merely because the tunnel client exited.

A separate `tunnel_watchdog.py` adopts an already running, verified tunnel
process or starts a new one. Unexpected tunnel exits trigger exponential,
bounded reconnect (up to five minutes between attempts), while authorization
failures enter `NEEDS_ATTENTION` with a five-minute retry interval. No tokens
are rotated automatically, and the watchdog never kills unrelated processes.

`termuxbridgectl status` reports the watchdog and a **sanitized** tunnel
state/reason without printing sensitive logs. `CLIENT_RUNNING` denotes an
alive local tunnel client, **not** proof that ChatGPT can reach the device.
Actual remote connectivity must be verified with a live MCP tool call.

A forced update to the currently installed commit now revalidates the release
and reinstalls the managed launcher and startup scripts, rather than silently
keeping a legacy `gpt` command:

```bash
termuxbridgectl update-now
hash -r
type -a gpt
gpt
termuxbridgectl status
```

If an old launcher still prints `🔄 GPT Bridge...`, it is not the managed
`runtime/gpt` from this repository. Inspect `type -a gpt` for shell aliases
or other PATH entries; prefer the canonical `$PREFIX/bin/gpt` installation.
Do not paste the raw tunnel log or secret files into support chats.

## Automatic updates

Automatic update is a built-in TermuxBridge product function. Ordinary runtime
updates do **not** stop the OpenAI tunnel.

The updater downloads an immutable candidate, validates Python/shell and MCP
tool compatibility, starts it on the inactive backend port, switches the
supervisor route atomically, drains requests already assigned to the old
generation, observes the candidate, and only then commits `current`.
A transaction journal under `state/update.json` makes interrupted updates
recoverable. Local supervisor/backend traffic is authenticated with separate
machine-local secrets.

Tunnel-client changes are classified separately as `TRANSPORT_UPDATE`.
They are checksum-verified and use a blue/green handoff against the same
supervisor; runtime releases never replace the tunnel binary implicitly.

Useful commands:

```bash
termuxbridgectl check
termuxbridgectl status
termuxbridgectl update-status
termuxbridgectl update-now
termuxbridgectl auto-update-status
termuxbridgectl auto-update-enable
termuxbridgectl auto-update-disable
```

The first upgrade from the legacy flat runtime performs one controlled
migration to the supervisor topology. Subsequent ordinary runtime updates keep
the tunnel process alive.

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


## Local photo gallery

TermuxBridge can expose the Android shared-storage photo library to ChatGPT as
read-only MCP image tools. Source photos are never modified. A private SQLite
metadata index is stored under `~/.termux-mcp-bridge/gallery.sqlite3`.

Gallery tools:

- `gallery_scan` — refresh the local metadata index.
- `gallery_status` — report indexed image/album counts.
- `gallery_albums` / `gallery_list` — browse and filter indexed photos.
- `gallery_thumbnail` — return one bounded preview as MCP image content.
- `gallery_get_image` — return one higher-quality bounded preview.
- `gallery_get_images` — return up to 12 selected photos in one call.
- `gallery_contact_sheet` — render up to 64 labeled thumbnails for efficient visual scanning.

The image-rendering tools use Pillow when available in the Termux Python
environment. Metadata indexing remains isolated from the original photos.

For MCP clients that cached the older tool list, the existing `read_text` tool
also accepts read-only virtual paths such as `gallery://image/123` and
`gallery://contact-sheet?limit=36&offset=0`. This keeps image access usable
without requiring an immediate connector/tool-schema refresh.
Cloud-only Google Photos items that are not present in Android shared storage
are outside this local gallery source.


## Self-maintenance and troubleshooting (v1.2.14)

An Android JobScheduler job (ID 38038) runs approximately hourly and survives
reboots, but Android may defer execution due to background restrictions,
power saving, or unavailable network. It rechecks the verified stable GitHub
release without automatically installing unpublished branch code.

Use `termuxbridgectl maintenance-status` for timestamp, result, last successful
check, consecutive failures, local process/health summary, and free storage.
`CLIENT_RUNNING` proves only the local tunnel-client process is alive: it does
**not** prove the remote ChatGPT control plane can reach Termux. Remote access
must be tested with a real MCP call.

Privacy maintenance removes the obsolete plaintext command-argument array
from historical bridge job metadata, retaining job IDs, PIDs, timestamps,
logs, and working directories. It does not alter running jobs or logs.

The updater stages every Python helper from the checksum-verified release
rather than relying on a fixed legacy file list. For an already-installed
generation, a missing helper may be restored from that exact verified release;
existing divergent immutable files are not silently overwritten.

The GitHub Release workflow runs all regression tests before publishing.


## Reliable Core, Phase 1 (v1.2.15)

Five production capabilities are implemented without changing the existing
MCP tool schemas:

1. **Bounded streaming execution.** Existing `run_command` and
   `run_command_extended` now drain merged stdout/stderr incrementally and
   retain a configurable output tail rather than calling
   `subprocess.run(capture_output=True)`. Timeouts and cancellation terminate
   only the subprocess group created for that operation.
2. **Resource and concurrency budgets.** At most **two concurrent direct
   executions per backend generation** can hold the local synchronous slot
   semaphore; the durable queue runs **one additional task worker**. Command
   children receive an `RLIMIT_CPU` budget, with 2048 MiB default RSS watchdog
   accounting across subprocess-group members. Android rejects tight
   `RLIMIT_AS` virtual-memory limits for even trivial commands, so this bridge
   intentionally uses actual process-group RSS monitoring instead. RSS checks
   are best-effort, not hard Android cgroup quotas. Android system permissions
   and app-level limits remain authoritative.
3. **SQLite durable queue.** New MCP tools: `queue_submit`, `queue_list`,
   `queue_status`, `queue_cancel` and `queue_retry`. The private
   `state/queue.sqlite3` stores task IDs, state, priority, timestamps, and
   validated script paths, **not raw argv or command output**. Queue tasks are
   either `health` or a pre-existing `.sh` script under the bridge's
   install root. The queue worker is a single leased owner across old and
   candidate MCP generations during blue/green updates.
4. **Cancellation and recovery.** A queued task may be cancelled immediately;
   a running task receives a cancellation request, then an owned subprocess
   group receives SIGTERM (SIGKILL only if that group remains alive after the
   grace period). When the worker restarts, interrupted idempotent `health`
   tasks may be requeued; interrupted scripts move to `NEEDS_REVIEW`.
   `queue_retry` is an explicit operator action to avoid repeating a script
   whose side effects may have already happened. This is **at-least-once**
   recovery for idempotent jobs, not exactly-once execution.
5. **Conservative release rollback.** During hourly maintenance, three
   consecutive backend-specific failures can trigger the guarded
   `rollback_guard.py`. It requires a healthy authenticated supervisor and
   an existing previously validated generation. The previous backend is
   started on the inactive port and checked via direct health and routed
   MCP `tools/list` before state is committed. The unsuccessful release is
   quarantined to stop the hourly updater reinstalling it. Explicit operator
   `--force` can override quarantine. General network outages, tunnel
   authorization failures and unrelated app errors are **not** grounds for
   automatic rollback.

Use the normal `termuxbridgectl maintenance-status` to inspect runtime health
and `termuxbridgectl update-now` for a manual verified update. The rollback
mechanism is covered by isolated integration tests that simulate broken and
healthy MCP generations on disposable loopback ports. It must not be tested by
intentionally breaking the user's live bridge.

The server still runs as the normal Android/Termux application UID; neither
root nor background ChatGPT model inference is required. Client applications
may need to refresh their MCP tool discovery to display new queue tools.

## Ecosystem · Phase 3 (v1.2.18)

### Built-in adapters and compatibility

The phone, gallery and Google adapters are independently imported through the
fixed allowlist in `runtime/adapter_runtime.py`. A broken or absent optional
adapter no longer aborts the core bridge. Each adapter declares an API version
and ABI; unsupported major versions, mismatching ABI, collisions and tools
outside their registered namespace are rejected. Three unexpected execution
errors isolate that adapter for 60 seconds; successful operations reset the
failure count. Existing MCP tools and input schemas remain unchanged. This
is **exception/circuit-breaker isolation**, not a separate operating-system
process sandbox or a hard execution timeout; Android permissions still apply.
Adapter versions in the registry identify the **TermuxBridge adapter API**,
not installed Google/Android app versions.

### Local diagnostics panel

```bash
termuxbridgectl dashboard
termuxbridgectl dashboard-html
```

The second command creates `~/termux-mcp-bridge/state/dashboard.html` (0600):
a static offline page with local process health, queued jobs, adapter ABI and
availability, power policy, update state and backup count. No CDN, JavaScript,
web listener, secret tokens or personal app content is included.
The optional MCP tool `ecosystem_dashboard` can return the same snapshot.
A running local tunnel process is **not** proof of remote ChatGPT connectivity.

### Private, integrity-checked local backups

```bash
termuxbridgectl backup-create
termuxbridgectl backup-list
termuxbridgectl backup-verify BACKUP_FILENAME
termuxbridgectl backup-restore BACKUP_FILENAME       # dry-run
termuxbridgectl backup-restore BACKUP_FILENAME --yes # explicit restore
```

Allowed data: non-secret local energy, notification and adapter settings,
plus a consistent SQLite *snapshot* of the queued tasks.
Secret/token files, raw logs, mobile gallery, app accounts and phone file
contents are excluded. Backups are local (0700 directory, 0600 files), bounded
in size and count (last five retained) and HMAC-SHA256 protected by a derived
key from this installation's router token. **HMAC gives integrity, not
encryption**: do not upload the archives or copy them to an untrusted location.
They cannot be verified on another installation without the same local key.
Automatic backups run at most once per day after a healthy maintenance check,
provided sufficient storage is available.

Restoration defaults to **verification-only**. Only the three non-critical
JSON settings files can be restored explicitly; the live SQLite queue, process
pointers, release routes, running jobs, executable files and secrets are
**never restored automatically**. This design avoids duplicating side effects
and destroying a running deployment.

### Safe chaos testing

```bash
termuxbridgectl chaos-test
```

Runs synthetic fault injection in a disposable temporary directory: adapter
crash/circuit breaker, independent healthy adapter, backup corruption and
checksum rejection. The full GitHub regression suite additionally tests
malformed adapter contracts, cross-namespace tools, symlinks, restore dry-runs,
queue snapshots, backup retention, privacy and static dashboard escaping.
Neither mode intentionally kills the live tunnel or disables Android services.

Phase 3 only introduces allowlisted, versioned built-in adapters. It does not
load arbitrary third-party Python files or bypass Android permission dialogs.
