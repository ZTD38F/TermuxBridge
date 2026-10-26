# TermuxBridge Cloudflare Pages source

This folder is the **GitHub source of truth** for the `https://termuxbridge.pages.dev/` Pages deployment.

- Browser homepage: static `index.html`, `app.js`, `style.css` with CSP and nosniff.
- CLI root (`curl`, `wget`, PowerShell, Termux): HTTP 302 to immutable installer.
- `/install.sh`: HTTP 302 to `https://raw.githubusercontent.com/ZTD38F/TermuxBridge/5a76c51a3c24c473147492a0d7260e51d47bd7de/install.sh`.
- `.ASSETS.fetch` handles all other paths. No encoded PowerShell commands.
- Always keep the installer SHA pinned to a tested Git revision. Review updates and use the existing Sonoryx governance/lease + E2E process, not an independent periodic worker.
- A published HTTPS page does **not** prove installation was tested on a real device.

For deployment, build an allowlisted output directory from these files: `_worker.js`, `_headers`, `_redirects`, `index.html`, `app.js`, `style.css`. The `manifest.json` is metadata and must be checked against the website/source hash. Never upload token files.
