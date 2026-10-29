import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import worker from './_worker.js';

const base=dirname(fileURLToPath(import.meta.url));
const manifest=JSON.parse(readFileSync(join(base,'manifest.json'),'utf8'));
const script=readFileSync(join(base,'..',manifest.filename));
const source=new URL(manifest.install_url);
const sourceParts=source.pathname.split('/').filter(Boolean);
const [owner,repo,branch,filename]=sourceParts;
const env={REDIRECT_URL:manifest.install_url};
const req=(path='/',agent='Mozilla/5.0',method='GET')=>new Request(
  `https://${manifest.host}${path}`,{method,headers:{'User-Agent':agent}});

async function withFetch(fake,callback){
  const original=globalThis.fetch;
  globalThis.fetch=fake;
  try{return await callback();}finally{globalThis.fetch=original;}
}

function fixture({commit='a'.repeat(40),bytes=script,log=[]}={}){
  return async(input,options={})=>{
    const u=new URL(input);
    log.push({url:u.toString(),options});
    assert.match(u.search,/\d{6}/,'request must carry six-digit cache buster');
    assert.equal(options.cache,'no-store');
    assert.equal(options.redirect,'manual');
    if(u.hostname==='github.com'){
      assert.equal(u.pathname,`/${owner}/${repo}/commits/${branch}.atom`);
      return new Response(`<feed><entry><id>Grit::Commit/${commit}</id></entry></feed>`,{
        status:200,headers:{'Content-Type':'application/atom+xml'}});
    }
    if(u.hostname==='raw.githubusercontent.com'){
      assert.equal(u.pathname,`/${owner}/${repo}/${commit}/${filename}`);
      return new Response(bytes,{status:200,headers:{'Content-Type':'text/plain'}});
    }
    throw Error('Unexpected upstream: '+u.hostname);
  };
}

for(const [path,agent] of [
  ['/','Mozilla/5.0 Chrome/141'],['/','WindowsPowerShell/5.1'],
  ['/','Termux/0.118'],['/','curl/8.7'],[`/${filename}`,'Mozilla/5.0'],
  ['/index.html','Mozilla/5.0'],
]){
  test(`Mode 2 ${path} ${agent}: fresh exact script, same Pages host`,async()=>{
    const calls=[];
    const result=await withFetch(fixture({log:calls}),()=>worker.fetch(req(path,agent),env));
    assert.equal(result.status,200);
    assert.equal(result.headers.get('content-type'),'text/plain; charset=utf-8');
    assert.equal(result.headers.get('location'),null);
    assert.equal(result.headers.get('cache-control'),'no-store');
    assert.equal(result.headers.get('x-content-type-options'),'nosniff');
    assert.equal(result.headers.get('x-cfpr-freshness-source'),'github-atom-sha');
    assert.equal(result.headers.get('x-cfpr-git-commit'),'a'.repeat(40));
    assert.deepEqual(Buffer.from(await result.arrayBuffer()),script);
    assert.equal(calls.length,2);
  });
}

test('HEAD is text/plain, no script body',async()=>{
  const res=await withFetch(fixture(),()=>worker.fetch(req(`/${filename}`,'curl/8.7','HEAD'),env));
  assert.equal(res.status,200);
  assert.match(res.headers.get('content-type'),/^text\/plain/);
  assert.equal(await res.text(),'');
});

test('POST is 405 and never reads GitHub',async()=>{
  const res=await withFetch(()=>{throw Error('POST must not fetch');},()=>worker.fetch(req('/','curl/8.7','POST'),env));
  assert.equal(res.status,405);
  assert.equal(res.headers.get('allow'),'GET, HEAD');
});

test('new GitHub commit changes served bytes without new Pages deployment',async()=>{
  const first=await withFetch(fixture({commit:'1'.repeat(40),bytes:Buffer.from('v1\n')}),()=>worker.fetch(req(),env));
  assert.equal(await first.text(),'v1\n');
  const next=await withFetch(fixture({commit:'2'.repeat(40),bytes:Buffer.from('v2\n')}),()=>worker.fetch(req(),env));
  assert.equal(await next.text(),'v2\n');
  assert.equal(next.headers.get('x-cfpr-git-commit'),'2'.repeat(40));
});

test('unavailable Atom and rate-limited GitHub API fail closed, not stale',async()=>{
  const res=await withFetch(async input=>{
    const host=new URL(input).hostname;
    if(host==='github.com')return new Response('unavailable',{status:404});
    if(host==='api.github.com')return new Response('rate limited',{status:403,headers:{'X-RateLimit-Remaining':'0'}});
    throw Error('Unexpected network request');
  },()=>worker.fetch(req(),env));
  assert.equal(res.status,502);
});

test('manifest declares mutable GitHub main, no hard-coded revision binding',()=>{
  assert.equal(manifest.mode,2);
  assert.equal(manifest.dynamic,true);
  assert.equal(manifest.source_ref,'main');
  assert.equal(manifest.freshness_strategy,'github-atom-sha');
  assert.equal(source.hostname,'raw.githubusercontent.com');
  assert.equal(branch,'main');
  assert.ok(!/\/[a-f0-9]{40}\//i.test(manifest.install_url));
  const deployed=readFileSync(join(base,'_worker.js'),'utf8');
  assert.ok(deployed.includes('env?.REDIRECT_URL'));
  assert.ok(deployed.includes('resolveGithubAtom'));
});

test('builder deploys Worker and safe fallback, not legacy HTML or redirects',()=>{
  const build=readFileSync(join(base,'build-site.sh'),'utf8');
  assert.ok(build.includes('_worker.js'));
  assert.ok(!build.includes(' _redirects '));
  const fallback=readFileSync(join(base,'index.html'),'utf8');
  assert.ok(!fallback.includes('Copy command'));
  assert.ok(!fallback.includes('Secure Install'));
});
