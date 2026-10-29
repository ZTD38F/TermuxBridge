// Mode 2 — Cloudflare Pages Router: Proxy S / Text Plain.
// This site is an endpoint for the raw, pinned TermuxBridge PowerShell installer.
const SOURCE = 'https://raw.githubusercontent.com/ZTD38F/TermuxBridge/97a15e0f55477f8a3f9ba40dd660caaf494112a5/install.sh';
const SECURITY = {
  'Content-Type': 'text/plain; charset=utf-8',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
  'Referrer-Policy': 'no-referrer',
  'Cache-Control': 'no-store',
};
export default {
  async fetch(request) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method Not Allowed', {status:405,headers:{...SECURITY,Allow:'GET, HEAD'}});
    }
    try {
      const upstream = await fetch(SOURCE, {redirect:'manual', signal:AbortSignal.timeout(12000),headers:{Accept:'text/plain'}});
      if (upstream.status !== 200 || !upstream.ok) {
        return new Response('Upstream source unavailable', {status:502,headers:SECURITY});
      }
      const type=upstream.headers.get('content-type') || '';
      if (/text\/html|application\/json/i.test(type)) {
        return new Response('Invalid upstream source type', {status:502,headers:SECURITY});
      }
      const length=Number(upstream.headers.get('content-length') || 0);
      if (length > 512000) return new Response('Installer too large', {status:502,headers:SECURITY});
      if (request.method === 'HEAD') return new Response(null,{status:200,headers:SECURITY});
      const data=await upstream.arrayBuffer();
      if (!data.byteLength || data.byteLength > 512000) {
        return new Response('Invalid upstream size',{status:502,headers:SECURITY});
      }
      return new Response(data,{status:200,headers:SECURITY});
    } catch {
      return new Response('Upstream fetch failed',{status:502,headers:SECURITY});
    }
  },
};
