import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ApiClient, readSSE } from '../api-client.mjs';
const require = createRequire(import.meta.url), http = require('node:http');
const { createServer } = require('../server.cjs');
const reply = (data, status = 200) => new Response(JSON.stringify({ code: status < 400 ? 0 : 11401, message: 'test', data }), { status, headers: { 'Content-Type': 'application/json' } });
const memory = () => { const m = new Map(); return { getItem: k => m.get(k), setItem: (k,v) => m.set(k,v), removeItem: k => m.delete(k) }; };
const auth = { user: { id: '373814368841760768', nickname: '测试' }, token: { access_token: 'old', refresh_token: 'refresh-old' } };
async function start(s) { await new Promise(r => s.listen(0, '127.0.0.1', r)); return `http://127.0.0.1:${s.address().port}`; }
async function close(s) { s.closeAllConnections?.(); await new Promise(r => s.close(r)); }

test('SSE parses Chinese across every byte and CRLF boundaries', async () => {
  const bytes = new TextEncoder().encode(': ping\r\nevent: start\r\ndata: {"message_id":"373814368841760768"}\r\n\r\nid: 12-0\r\nevent: delta\r\ndata: {"delta":"你好，电路"}\r\n\r\nevent: done\r\nid: 13-0\r\ndata: [DONE]\r\n\r\n');
  const stream = new ReadableStream({ start(c) { for (const b of bytes) c.enqueue(new Uint8Array([b])); c.close(); } });
  const events = []; await readSSE(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }), e => { events.push(e); if (e.event === 'done') return false; });
  assert.deepEqual(events.map(e => e.event), ['start', 'delta', 'done']);
  assert.equal(JSON.parse(events[1].data).delta, '你好，电路'); assert.equal(events[1].id, '12-0'); assert.equal(events[0].id, undefined);
});
test('Concurrent 401s refresh exactly once and retry with new token', async () => {
  let refreshes = 0, requests = 0;
  const client = new ApiClient({ storage: memory(), fetcher: async (url, opts) => {
    if (url.endsWith('/auth/refresh')) { refreshes++; await new Promise(r => setTimeout(r, 15)); return reply({ ...auth, token: { access_token: 'new', refresh_token: 'refresh-new' } }); }
    requests++; return opts.headers.Authorization === 'Bearer new' ? reply({ id: auth.user.id }) : reply(null, 401);
  } }); client.save(auth);
  const responses = await Promise.all([client.json('/auth/me'), client.json('/auth/me')]);
  assert.equal(refreshes, 1); assert.equal(requests, 4); assert.equal(responses[0].id, '373814368841760768'); assert.equal(client.auth.token.refresh_token, 'refresh-new');
});
test('Network failure during token refresh retains the session for retry', async () => {
  const client = new ApiClient({ storage: memory(), fetcher: async url => { if (url.endsWith('/auth/refresh')) throw new TypeError('offline'); return reply(null, 401); } }); client.save(auth);
  await assert.rejects(client.json('/auth/me')); assert.equal(client.auth.token.refresh_token, 'refresh-old');
});
test('Invalid refresh token expires session; failed logout does not report local logout', async () => {
  let expired = 0;
  const client = new ApiClient({ storage: memory(), onExpire: () => expired++, fetcher: async () => reply(null, 401) }); client.save(auth);
  await assert.rejects(client.json('/auth/me')); assert.equal(client.auth, null); assert.equal(expired, 1);
  client.save(auth); await assert.rejects(client.logout()); assert.equal(client.loggedIn, true);
});

test('A delayed 401 uses the token already refreshed by a faster request', async () => {
  let resolveSlow, refreshes = 0;
  const slow = new Promise(r => { resolveSlow = r; });
  const client = new ApiClient({ storage: memory(), fetcher: async (url, opts) => {
    if (url.endsWith('/auth/refresh')) { refreshes++; return reply({ ...auth, token: { access_token: 'new', refresh_token: 'refresh-new' } }); }
    if (opts.headers.Authorization === 'Bearer new') return reply({ id: auth.user.id });
    if (url.endsWith('/slow')) return slow;
    return reply(null, 401);
  } }); client.save(auth);
  const waiting = client.json('/slow');
  await client.json('/fast');
  resolveSlow(reply(null, 401));
  assert.equal((await waiting).id, auth.user.id);
  assert.equal(refreshes, 1);
});

test('An old account response cannot expire or return data into a newly logged-in session', async () => {
  for (const status of [200, 401]) {
    let resolveOld, expired = 0;
    const pending = new Promise(r => { resolveOld = r; });
    const client = new ApiClient({ storage: memory(), fetcher: () => pending, onExpire: () => expired++ });
    client.save(auth);
    const request = client.json('/auth/me');
    client.save({ user: { id: 'another-user' }, token: { access_token: 'another-access', refresh_token: 'another-refresh' } });
    resolveOld(reply({ private: 'old-user-data' }, status));
    await assert.rejects(request, { status: 409 });
    assert.equal(client.auth.user.id, 'another-user');
    assert.equal(expired, 0);
  }
});
test('Proxy forwards auth, SSE and resume ID, blocks private files and foreign origin', async () => {
  const calls = [];
  const backend = http.createServer((req, res) => { calls.push({ url: req.url, headers: req.headers }); res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: delta\ndata: {"delta":"真实接口格式"}\n\n'); res.end('event: done\ndata: [DONE]\n\n'); });
  const backendUrl = await start(backend);
  const dist = mkdtempSync(join(tmpdir(), 'qixue-test-'));
  mkdirSync(join(dist, 'assets'));
  writeFileSync(join(dist, 'index.html'), '<title>启学</title><script src="/assets/index-test.js"></script>');
  writeFileSync(join(dist, 'assets/index-test.js'), 'console.log("fixture")');
  writeFileSync(join(dist, 'assets/index-test.css'), 'body { margin: 0 }');
  const front = createServer({ dist, backend: backendUrl, envValues: { VAPTCHA_VID: 'public-vid', VAPTCHA_VKEY: 'private-key', LLM_API_KEY: 'not-for-browser' } }); const url = await start(front);
  try {
    const config = await (await fetch(url + '/frontend-config.json')).json(); assert.deepEqual(Object.keys(config).sort(), ['captchaEnabled', 'captchaVid', 'version']); assert.equal(config.captchaEnabled, true); assert.equal(JSON.stringify(config).includes('private-key'), false);
    for (const file of ['/.env', '/server.cjs', '/README.md', '/../.env', '/src/main.js', '/node_modules/vue/index.js', '/assets/../../api-client.mjs']) assert.equal((await fetch(url + file)).status, 404);
    assert.equal((await fetch(url + '/api/v1/auth/login', { method: 'POST', headers: { Origin: 'http://foreign.invalid' } })).status, 403);
    const response = await fetch(url + '/api/v1/chat/stream/373814368841760768', { headers: { Authorization: 'Bearer test-token', 'Last-Event-ID': '123-0' } });
    const frames = []; await readSSE(response, e => { frames.push(e); }); assert.equal(frames[1].event, 'done');
    assert.equal(calls[0].headers.authorization, 'Bearer test-token'); assert.equal(calls[0].headers['last-event-id'], '123-0');
    const page = await fetch(url + '/'); assert.equal(page.status, 200); assert.match(await page.text(), /启学/);
    for (const [name, mime] of [['index-test.js', 'text/javascript'], ['index-test.css', 'text/css']]) {
      const asset = await fetch(url + '/assets/' + name);
      assert.equal(asset.status, 200); assert.match(asset.headers.get('content-type'), new RegExp(mime));
    }
  } finally { await close(front); await close(backend); rmSync(dist, { recursive: true, force: true }); }
});
