# TermuxBridge Cloudflare Pages — Mode 2 (Proxy S / Text Plain)

Canonical GitHub source for https://termuxbridge.pages.dev/.

- `GET /` and `GET /install.sh`: HTTP 200 with **raw `install.sh`**, `text/plain; charset=utf-8`, no redirects, same host in browser and Termux.
- Immutable upstream: `https://raw.githubusercontent.com/ZTD38F/TermuxBridge/97a15e0f55477f8a3f9ba40dd660caaf494112a5/install.sh`.
- Installer SHA-256: `740d330e86a2f80d4b849388cfe05c5d1f41fe40e1fa6dfa74e5552d13ad37c2`.
- Identical bytes for browser, Termux, curl, wget. The `_worker.js` fails closed for upstream errors, HTML, JSON, redirects, oversize/empty content, and sets CSP, nosniff and no-store. POST yields 405.
- `index.html` is only a static safety fallback. The old Secure Install HTML UI, app.js, style.css and `_redirects` are NOT deployed.
- The Pages hostname is the text/script endpoint, **not** an installation-instructions dashboard. The Termux `gpt` launcher and all on-device actions remain in TermuxBridge, not in this Cloudflare Worker.
- Publishing the script does not prove it has been executed successfully on an Android device.

Tests: `node --test pages/site.test.mjs`
Build: `bash pages/build-site.sh <new-output-directory>`

This is a direct-upload Cloudflare Pages project: merging this source does **not** deploy automatically. Deploy only from CI-tested source and verify the public hostname independently.
