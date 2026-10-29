import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import worker from './_worker.js';

const base = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(readFileSync(join(base, 'manifest.json'), 'utf8'));
const bytes = readFileSync(join(base, '..', 'install.sh'));
const upstream = manifest.install_url;
const req = (path = '/', agent = 'Mozilla/5.0', method = 'GET') =>
  new Request('https://' + manifest.host + path, {
    method, headers: {'user-agent': agent},
  });

async function withFetch(impl, callback) {
  const original = globalThis.fetch;
  globalThis.fetch = impl;
  try { return await callback(); }
  finally { globalThis.fetch = original; }
}

function upstreamOK(url) {
  assert.equal(url, upstream);
  return new Response(bytes, {
    status: 200,
    headers: {'content-type': 'text/plain; charset=utf-8',
              'content-length': String(bytes.length)},
  });
}

for (const [path, agent] of [
  ['/', 'Mozilla/5.0 Chrome/141'],
  ['/', 'WindowsPowerShell/5.1'],
  ['/', 'PowerShell/7.4'],
  ['/', 'curl/8.5'],
  ['/install.sh', 'Mozilla/5.0'],
  ['/index.html', 'Mozilla/5.0'],
]) {
  test('Mode 2 serves raw installer without redirect: ' + path + ', ' + agent, async () => {
    const result = await withFetch(async (url) => upstreamOK(url), async () =>
      worker.fetch(req(path, agent))
    );
    assert.equal(result.status, 200);
    assert.match(result.headers.get('content-type'), /^text\/plain/);
    assert.equal(result.headers.get('location'), null);
    assert.equal(result.headers.get('x-content-type-options'), 'nosniff');
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.deepEqual(Buffer.from(await result.arrayBuffer()), bytes);
  });
}

test('HEAD returns 200 text/plain and no body', async () => {
  const result = await withFetch(async (url) => upstreamOK(url), async () =>
    worker.fetch(req('/install.sh', 'Mozilla/5.0', 'HEAD'))
  );
  assert.equal(result.status, 200);
  assert.match(result.headers.get('content-type'), /^text\/plain/);
  assert.equal(await result.text(), '');
});

test('POST returns 405 and never fetches source', async () => {
  const result = await withFetch(() => {
    throw new Error('unexpected upstream request');
  }, async () => worker.fetch(req('/', 'curl/8.5', 'POST')));
  assert.equal(result.status, 405);
  assert.equal(result.headers.get('allow'), 'GET, HEAD');
  assert.equal(result.headers.get('location'), null);
});

for (const [name, response] of [
  ['HTML upstream', () => new Response('<html>bad</html>', {status:200,headers:{'content-type':'text/html'}})],
  ['JSON upstream', () => new Response('{"error":true}', {status:200,headers:{'content-type':'application/json'}})],
  ['redirect upstream', () => new Response(null, {status:302,headers:{location:'https://example.com'}})],
  ['error upstream', () => new Response('bad gateway', {status:503})],
  ['empty upstream', () => new Response('', {status:200,headers:{'content-type':'text/plain'}})],
  ['oversized upstream', () => new Response('x', {status:200,headers:{'content-type':'text/plain','content-length':'512001'}})],
]) {
  test('fail closed on ' + name, async () => {
    const result = await withFetch(async () => response(), async () =>
      worker.fetch(req())
    );
    assert.equal(result.status, 502);
    assert.match(result.headers.get('content-type'), /^text\/plain/);
    assert.equal(result.headers.get('location'), null);
  });
}

test('network failure returns 502', async () => {
  const result = await withFetch(async () => { throw new Error('network'); }, async () =>
    worker.fetch(req())
  );
  assert.equal(result.status, 502);
});

test('source is pinned to committed GitHub SHA and matches manifest', () => {
  assert.match(manifest.sha, /^[a-f0-9]{40}$/);
  assert.equal(manifest.mode, 2);
  assert.equal(manifest.host, 'termuxbridge.pages.dev');
  assert.match(upstream, /^https:\/\/raw\.githubusercontent\.com\/ZTD38F\/TermuxBridge\/[a-f0-9]{40}\/install\.sh$/);
  assert.ok(upstream.includes('/' + manifest.sha + '/'));
  assert.ok(readFileSync(join(base, '_worker.js'), 'utf8').includes(upstream));
  assert.equal(manifest.sha256, '740d330e86a2f80d4b849388cfe05c5d1f41fe40e1fa6dfa74e5552d13ad37c2');
});

test('production asset builder does not publish old redirects or HTML installer UI', () => {
  const build = readFileSync(join(base, 'build-site.sh'), 'utf8');
  assert.ok(build.includes('_worker.js'));
  assert.ok(!build.includes(' _redirects '));
  const fallback = readFileSync(join(base, 'index.html'), 'utf8');
  assert.ok(!fallback.includes('Copy command'));
  assert.ok(!fallback.includes('Secure Install'));
});
