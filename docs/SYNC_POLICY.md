# TermuxBridge GitHub Sync Policy

## Canonical repository

`ZTD38F/TermuxBridge` is the canonical code and version-history repository.

## Required invariant

After the initial phone import is complete:

> No durable TermuxBridge code/configuration change is considered finished until the verified working version is represented in GitHub.

## Normal workflow

### Repository-first change

1. Read current repository state.
2. Modify the smallest required files.
3. Review the change.
4. Deploy/update the phone.
5. Verify local health.
6. Verify ChatGPT ↔ tunnel ↔ MCP end-to-end.
7. Keep the verified repository revision as the canonical state.

### Emergency phone-side fix

If a fix must be made directly in Termux:

1. inspect and back up the affected file;
2. apply the smallest fix;
3. verify the phone runtime;
4. compare the changed file with GitHub;
5. synchronize the exact working change back to GitHub;
6. verify the repository content after the commit.

Phone-only fixes are temporary drift until step 5 is complete.

## Never synchronize

- secrets
- OAuth tokens
- cookies/session material
- authenticated browser profiles
- runtime PID/socket files
- generated logs unless explicitly redacted and needed as test fixtures
- temporary backups

## Versioning

Use Git commits as the authoritative history. Releases/tags may be added once the exact phone runtime has been imported and the installer is reproducible.

## Naming

The project name is **TermuxBridge**.

Legacy names such as `Termux Safe Bridge v2`, `Termux_Safe_Bridge_v2` and `Termux_MCP_Bridge` are historical identifiers only and should not be used for new repository-facing documentation or releases.
