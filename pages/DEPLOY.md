# TermuxBridge Pages rollout and rollback

Production: `https://termuxbridge.pages.dev/` (Cloudflare Pages project `termuxbridge`, branch `main`).
- Verified **dynamic Mode 2** deployment, 2026-10-29: `5383dae0-d28b-45e4-8208-67ce31d25b0e`.
- Prior **pinned script** deployment for rollback: `712ebbd8-73b7-4562-880d-e2322f2f48db`.
- GitHub source: `ZTD38F/TermuxBridge:main/install.sh`. The Worker reads GitHub Atom for the current SHA and then raw content by immutable SHA (fresh query parameter on every network request).

1. `node --test pages/site.test.mjs` and `bash pages/build-site.sh <unused-empty-directory>`.
2. Confirm the existing Cloudflare Pages project has `REDIRECT_URL=https://raw.githubusercontent.com/ZTD38F/TermuxBridge/main/install.sh` in both production and preview settings. Do not print tokens.
3. Deploy the allowlisted build directory to existing project `termuxbridge` with branch `main`, using the authorized Cloudflare API. This is a **direct upload project**; merging GitHub changes does not itself deploy.
4. Verify public `GET /`, `GET /install.sh`, HEAD, POST, browser and CLI user agents, MIME, no Location, `X-CFPR-Git-Commit`, source SHA-256 matching the actual current GitHub file and Cloudflare production deployment ID.
5. For rollback, retain the prior pinned Worker source and production deployment `712ebbd8-73b7-4562-880d-e2322f2f48db`. Old pinned Workers **do not** follow `main`; returning to the old deployment is a deliberate freshness regression.

Never reintroduce old HTML installer behavior or silently overwrite unrelated Pages projects. The six-digit nonce alone does not defeat GitHub raw CDN's five-minute branch cache; the Atom-SHA lookup is essential.
