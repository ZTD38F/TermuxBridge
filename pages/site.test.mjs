import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join, dirname} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
const base = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(readFileSync(join(base, 'manifest.json'), 'utf8'));
const installer = manifest.install_url;
const worker = (await import(pathToFileURL(join(base, '_worker.js')).href)).default;
const html = readFileSync(join(base, 'index.html'), 'utf8');
const client = readFileSync(join(base, 'app.js'), 'utf8');
const headers = readFileSync(join(base, '_headers'), 'utf8');
const redirects = readFileSync(join(base, '_redirects'), 'utf8');
const calls = [];
const env = {ASSETS: {fetch: async (request) => {calls.push(new URL(request.url).pathname); return new Response('STATIC', {status: 200, headers: {'Content-Type':'text/html'}});}}};
const req = (path, agent, method='GET') => new Request('https://' + manifest.host + path, {method, headers: {'User-Agent': agent}});
const root = '/' + manifest.filename;
for (const [label, path, ua] of [
  ['explicit filename browser', root, 'Mozilla/5.0'],
  ['PowerShell 5.1 root', '/', 'Mozilla/5.0 (Windows NT 10.0; WindowsPowerShell/5.1.0)'],
  ['PowerShell 7 root', '/', 'PowerShell/7.4.0'],
  ['curl root', '/', 'curl/8.5.0'],
  ['wget root', '/', 'Wget/1.21.2'],
  ['Termux root', '/', 'Termux/0.118'],
  ['HTTPie root', '/', 'HTTPie/3.0']]) {
  test(label, async () => {
    const old = calls.length; const res = await worker.fetch(req(path, ua), env);
    assert.equal(res.status, 302); assert.equal(res.headers.get('Location'), installer);
    assert.equal(res.headers.get('Cache-Control'), 'no-store');
    assert.equal(res.headers.get('X-Content-Type-Options'), 'nosniff');
    assert.equal(calls.length, old);
  });
}
test('HEAD explicit installer redirects', async () => {
  const res = await worker.fetch(req(root, 'curl/8', 'HEAD'), env);
  assert.equal(res.status, 302); assert.equal(res.headers.get('Location'), installer);
});
test('browser sees static homepage', async () => {
  const res = await worker.fetch(req('/', 'Mozilla/5.0 Chrome/120'), env);
  assert.equal(res.status, 200); assert.equal(await res.text(), 'STATIC');
});
test('normal assets and query strings are passed through', async () => {
  for (const path of ['/app.js','/style.css','/README.md','/?ref=spoof']) {
    const n = calls.length; const res = await worker.fetch(req(path, 'Mozilla/5.0'), env);
    assert.equal(res.status, 200); assert.equal(calls.length, n+1);
  }
});
test('non-GET method never triggers installer', async () => {
  const n = calls.length; const res = await worker.fetch(req('/', 'curl/8.5', 'POST'), env);
  assert.equal(res.status, 200);assert.equal(calls.length, n+1);
});
test('immutable source and page metadata agree', () => {
  assert.match(installer, /^https:\/\/raw\.githubusercontent\.com\/ZTD38F\/[A-Za-z0-9-]+\/[a-f0-9]{40}\/install\.(ps1|sh)$/);
  assert.ok(html.includes(manifest.sha) && html.includes(installer));
  assert.ok(html.includes('/' + manifest.filename));
  assert.ok(!html.includes(' -EncodedCommand '));
  assert.ok(!client.includes(' -EncodedCommand '));
  assert.ok(redirects.includes(installer));
});
test('HTML has allowlisted JavaScript and security headers', () => {
  assert.ok(html.includes('src="/app.js"'));
  assert.ok(!html.includes('onclick='));
  assert.match(headers, /script-src 'self'/);
  assert.match(headers, /X-Content-Type-Options: nosniff/);
  assert.ok(client.includes('navigator.clipboard.writeText'));
});
