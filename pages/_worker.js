export default { fetch: async function routedFetch(request, env, ctx) {
  const MODE = 2;
  const target = env?.REDIRECT_URL;
  if (!target) return new Response('REDIRECT_URL not set', {status:500});

  // Historical CLI-Wrangler (2026-05-17) used ?<random-number> to bypass
  // GitHub's edge cache. Generate SIX digits, preserving existing query data.
  // This is deliberately done BEFORE the initial upstream fetch: adding a
  // suffix only after fetching a cached URL cannot undo that cached response.
  // Signed URLs must remain byte-for-byte query-identical or signatures break.
  const freshUrl = value => {
    const url = new URL(value);
    for (const key of url.searchParams.keys()) {
      if (/^(x-amz-signature|x-goog-signature|signature|sig|token)$/i.test(key)) {
        return url.toString();
      }
    }
    const sixDigits = String(100000 + Math.floor(Math.random() * 900000));
    url.search += (url.search ? '&' : '?') + sixDigits;
    return url.toString();
  };

  // Public GitHub branch URLs are best resolved through GitHub's live Atom
  // commit feed, then read by immutable commit SHA. A raw branch URL can keep
  // serving cached bytes even with a different ?123456 query (max-age=300).
  const githubDetails = value => {
    const source = new URL(value);
    const chunks = source.pathname.split('/').filter(Boolean);
    let owner, repo, ref, parts;
    if (source.hostname === 'raw.githubusercontent.com' && chunks.length >= 4) {
      [owner, repo, ref] = chunks;
      parts = chunks.slice(3);
      if (ref === 'refs' && chunks[3] === 'heads' && chunks.length >= 6) {
        ref = chunks[4];
        parts = chunks.slice(5);
      }
    } else if (source.hostname === 'github.com' && chunks.length >= 5
        && (chunks[2] === 'raw' || chunks[2] === 'blob')) {
      [owner, repo, , ref] = chunks;
      parts = chunks.slice(4);
    } else {
      return null;
    }
    for (const key of source.searchParams.keys()) {
      if (/^(x-amz-signature|x-goog-signature|signature|sig|token)$/i.test(key)) return null;
    }
    if (!/^[a-z0-9_.-]+$/i.test(owner) || !/^[a-z0-9_.-]+$/i.test(repo)
        || !/^[a-z0-9_.-]+$/i.test(ref) || !parts.length
        || /^[0-9a-f]{40}$/i.test(ref)
        || parts.some(part => !part || part === '.' || part === '..')) return null;
    return {owner, repo, ref, parts, originalQuery:source.search};
  };

  const github = githubDetails(target);
  const githubContentsUrl = details => {
    const endpoint = new URL('https://api.github.com/repos/'
      + encodeURIComponent(details.owner) + '/' + encodeURIComponent(details.repo)
      + '/contents/' + details.parts.map(encodeURIComponent).join('/'));
    endpoint.searchParams.set('ref', details.ref);
    return endpoint.toString();
  };
  const resolveGithubAtom = async details => {
    const feed = 'https://github.com/' + encodeURIComponent(details.owner)
      + '/' + encodeURIComponent(details.repo) + '/commits/'
      + encodeURIComponent(details.ref) + '.atom';
    try {
      const response = await fetch(freshUrl(feed), {
        redirect:'manual', cache:'no-store', signal:AbortSignal.timeout(7000),
        headers:{Accept:'application/atom+xml',
                 'User-Agent':'CF-Pages-Router-Freshness/1.0',
                 'Cache-Control':'no-cache, no-store'},
      });
      if (response.status !== 200
          || !(response.headers.get('Content-Type') || '').toLowerCase().includes('atom+xml')
          || Number(response.headers.get('Content-Length') || 0) > 262144) return null;
      const xml = await response.text();
      if (xml.length > 262144) return null;
      const match = xml.match(/<entry>\s*<id>[^<]*Grit::Commit\/([0-9a-f]{40})<\/id>/i);
      if (!match) return null;
      const pinned = new URL('https://raw.githubusercontent.com/'
        + encodeURIComponent(details.owner) + '/' + encodeURIComponent(details.repo)
        + '/' + match[1] + '/' + details.parts.map(encodeURIComponent).join('/'));
      pinned.search = details.originalQuery;
      return {url:pinned.toString(), commit:match[1]};
    } catch { return null; }
  };

  if (MODE === 1) {
    const pinned = github ? await resolveGithubAtom(github) : null;
    // Redirect still degrades to the historical cache-buster if Atom is down.
    return new Response(null, {status:302, headers:{
      Location:freshUrl(pinned?.url || target), 'Cache-Control':'no-store',
    }});
  }

  const headers = {
    'Content-Type': (MODE === 4 ? 'text/html' : 'text/plain') + '; charset=utf-8',
    'X-Content-Type-Options': 'nosniff',
    'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
  };
  if (MODE !== 4) {
    headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'";
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return new Response('Method Not Allowed', {status:405, headers:{...headers, Allow:'GET, HEAD'}});
  }
  if (MODE === 3) {
    const ua = request.headers.get('User-Agent') || '';
    const mode = request.headers.get('Sec-Fetch-Mode') || '';
    const dest = request.headers.get('Sec-Fetch-Dest') || '';
    if (/Mozilla|AppleWebKit|Chrome|Safari|Edge|Opera/i.test(ua)
        || mode === 'navigate' || dest === 'document') {
      return new Response(null, {status:204, headers:{'Cache-Control':'no-store'}});
    }
  }

  // This is an upstream target sanity check, not network-layer DNS pinning.
  // Runtime Cloudflare fetch restrictions apply in addition to these checks.
  const safeTarget = value => {
    try {
      const u = new URL(value);
      const host = u.hostname.toLowerCase().replace(/\.$/, '');
      if (u.protocol !== 'https:' || u.username || u.password || !host) return false;
      if (host.includes(':') || /^[0-9.]+$/.test(host)) return false;
      if (!/^[a-z0-9.-]+\.[a-z0-9-]+$/.test(host)) return false;
      if (/(^|\.)(localhost|local|internal|test|invalid|onion|arpa)$/.test(host)) return false;
      if (/(^|\.)(nip\.io|sslip\.io|localtest\.me)$/.test(host)) return false;
      return true;
    } catch {
      return false;
    }
  };

  if (!safeTarget(target)) {
    return new Response('Invalid upstream target', {status:502, headers});
  }

  try {
    const pinned = github ? await resolveGithubAtom(github) : null;
    const contentsApi = github && !pinned ? githubContentsUrl(github) : null;
    const freshnessSource = pinned ? 'github-atom-sha'
      : contentsApi ? 'github-contents-api' : null;
    let url = pinned?.url || contentsApi || target;
    const signal = AbortSignal.timeout(12000);
    let upstream;
    // Allow a small number of valid HTTPS redirects, never unbounded follow.
    for (let hop = 0; hop <= 2; hop++) {
      if (!safeTarget(url)) {
        return new Response('Unsafe upstream redirect', {status:502, headers});
      }
      const githubApiHop = new URL(url).hostname === 'api.github.com';
      const upstreamHeaders = {
        Accept:githubApiHop ? 'application/vnd.github.raw+json'
          : MODE === 4 ? 'text/html' : 'text/plain',
        'Cache-Control':'no-cache, no-store',
        Pragma:'no-cache',
      };
      if (githubApiHop) {
        upstreamHeaders['User-Agent'] = 'CF-Pages-Router-Freshness/1.0';
        upstreamHeaders['X-GitHub-Api-Version'] = '2022-11-28';
        // Optional server-side secret for private repositories/rate limits.
        // Never forward this header if GitHub redirects to a different host.
        if (env?.GITHUB_TOKEN) upstreamHeaders.Authorization = 'Bearer ' + env.GITHUB_TOKEN;
      }
      // GitHub occasionally returns transient 403/404 immediately after a
      // branch update. Bound retries (new six-digit query on each attempt).
      // The shared 12-second AbortSignal still limits the entire operation.
      for (let retry = 0; retry < 3; retry++) {
        upstream = await fetch(freshUrl(url), {
          redirect:'manual', signal, cache:'no-store', headers:upstreamHeaders,
        });
        if (!githubApiHop || ![403, 404, 429, 500, 502, 503, 504].includes(upstream.status)
            || retry === 2 || upstream.headers.get('X-RateLimit-Remaining') === '0') break;
        await upstream.body?.cancel().catch(() => {});
        await new Promise(resolve => setTimeout(resolve, 250 * (retry + 1)));
      }
      if ([301, 302, 303, 307, 308].includes(upstream.status)) {
        const location = upstream.headers.get('Location');
        if (!location || hop === 2) {
          return new Response('Too many upstream redirects', {status:502, headers});
        }
        url = new URL(location, url).toString();
        continue;
      }
      break;
    }
    if (!upstream.ok || upstream.status !== 200) {
      return new Response('Upstream unavailable', {status:502, headers});
    }
    const mime = (upstream.headers.get('Content-Type') || '').toLowerCase();
    const githubRawMedia = contentsApi && mime.startsWith('application/vnd.github.raw');
    const githubHtmlRaw = pinned && MODE === 4 && /\.html?$/i.test(github.parts.at(-1))
      && mime.startsWith('text/plain');
    if (contentsApi && !githubRawMedia && new URL(url).hostname === 'api.github.com') {
      return new Response('GitHub API did not return raw file content', {status:502, headers});
    }
    if (MODE === 4) {
      if (!githubRawMedia && !githubHtmlRaw && mime && !mime.startsWith('text/html')
          && !mime.startsWith('application/xhtml+xml')) {
        return new Response('Expected HTML source', {status:502, headers});
      }
    } else if (mime.startsWith('text/html') || mime.startsWith('application/json')) {
      return new Response('Unexpected upstream content type', {status:502, headers});
    }

    const limit = 5242880;
    const length = Number(upstream.headers.get('Content-Length') || 0);
    if (!Number.isFinite(length) || length < 0 || length > limit) {
      return new Response('Upstream too large', {status:502, headers});
    }
    if (request.method === 'HEAD') {
      return new Response(null, {status:200, headers});
    }
    const reader = upstream.body?.getReader();
    if (!reader) return new Response('Missing upstream body', {status:502, headers});
    let size = 0;
    const chunks = [];
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > limit) {
        await reader.cancel().catch(() => {});
        return new Response('Upstream body too large', {status:502, headers});
      }
      chunks.push(part.value);
    }
    if (!size) return new Response('Empty upstream body', {status:502, headers});
    const body = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    if (freshnessSource) headers['X-CFPR-Freshness-Source'] = freshnessSource;
    if (pinned) headers['X-CFPR-Git-Commit'] = pinned.commit;
    return new Response(body, {status:200, headers});
  } catch {
    return new Response('Upstream fetch failed', {status:502, headers});
  }
} };