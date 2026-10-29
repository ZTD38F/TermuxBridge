# TermuxBridge Pages deployment / rollback

Project: `termuxbridge`; production hostname: `https://termuxbridge.pages.dev/`; production branch `main`.

Validated Mode 2 production deployment on 2026-10-29: `712ebbd8-73b7-4562-880d-e2322f2f48db`.

Source: `pages/_worker.js`, pinned to `ZTD38F/TermuxBridge@97a15e0f55477f8a3f9ba40dd660caaf494112a5` (`install.sh`).

1. `node --test pages/site.test.mjs`
2. `bash pages/build-site.sh <new-empty-build-directory>` — allowlist exactly `_worker.js` and `index.html`.
3. Use existing authorized Cloudflare credential channel; deploy that build to Pages project `termuxbridge`, branch `main`. Do not expose credentials.
4. Independently check browser/Termux/curl user agents, `GET /`, `GET /install.sh`, HEAD, POST, `text/plain`, absence of Location, and byte-identical SHA-256 to pinned GitHub source.
5. Confirm `production` deployment ID from Cloudflare API and retain prior deployment metadata for rollback.

Old behavior (browser HTML UI + CLI 302 to GitHub) is retired and must not be reintroduced.
