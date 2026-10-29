const http = require('node:http');
const https = require('node:https');
const fs = require('node:fs');
const path = require('node:path');
const { pipeline } = require('node:stream');

function envFile(filename) {
  try { return Object.fromEntries(fs.readFileSync(filename, 'utf8').split(/\r?\n/).filter(l => /^\s*[A-Z_]+\s*=/.test(l)).map(l => { const i = l.indexOf('='); return [l.slice(0, i).trim(), l.slice(i + 1).trim().replace(/^(['"])(.*)\1$/, '$2')]; })); }
  catch (e) { if (e.code !== 'ENOENT') throw e; return {}; }
}
function createServer(options = {}) {
  const dist = options.dist || path.join(__dirname, 'dist');
  const backend = new URL(options.backend || process.env.BACKEND_URL || 'http://127.0.0.1:8000');
  if (!['http:', 'https:'].includes(backend.protocol) || backend.username || backend.password || backend.search || backend.hash || backend.pathname !== '/') throw new Error('BACKEND_URL must be an http(s) origin.');
  const values = options.envValues || envFile(process.env.BACKEND_ENV_FILE || path.join(__dirname, '..', '.env'));
  const captchaVid = process.env.CAPTCHA_VID || values.VAPTCHA_VID || '';
  const captchaEnabled = process.env.CAPTCHA_ENABLED === 'true' || Boolean(captchaVid && values.VAPTCHA_VKEY);
  const tls = options.tls;
  const send = (res, status, value) => { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)); };
  const handler = (req, res) => {
    const origin = `${tls ? 'https' : 'http'}://${req.headers.host}`;
    res.setHeader('X-Content-Type-Options', 'nosniff'); res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self' https://*.vaptcha.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.vaptcha.com; connect-src 'self' https://*.vaptcha.com; frame-src https://*.vaptcha.com; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
    if (!/^(127\.0\.0\.1|localhost)(:\d+)?$/.test(req.headers.host || '') || (req.headers.origin && req.headers.origin !== origin)) return send(res, 403, { code: 10403, message: '请从本机页面访问。', data: null });
    if (req.url.startsWith('/api/v1/') && ['GET', 'POST', 'PATCH', 'DELETE'].includes(req.method)) {
      if (Number(req.headers['content-length']) > 120000) return send(res, 413, { code: 10413, message: '请求过大，请缩短内容。', data: null });
      const headers = { accept: req.headers.accept || 'application/json', 'accept-encoding': 'identity' };
      for (const key of ['authorization', 'content-type', 'last-event-id', 'x-request-id']) if (req.headers[key]) headers[key] = req.headers[key];
      const transport = backend.protocol === 'https:' ? https : http;
      const upstream = transport.request(new URL(req.url, backend), { method: req.method, headers }, response => {
        const h = { 'Content-Type': response.headers['content-type'] || 'application/json', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' };
        if (response.headers['x-request-id']) h['X-Request-ID'] = response.headers['x-request-id'];
        res.writeHead(response.statusCode, h); pipeline(response, res, () => {});
      });
      upstream.setTimeout(90000, () => upstream.destroy());
      upstream.on('error', () => { if (!res.headersSent) send(res, 502, { code: 22502, message: '后端暂时无法连接，请确认后端和Redis已启动。', data: null }); else res.destroy(); });
      let size = 0; req.on('data', chunk => { size += chunk.length; if (size > 120000) upstream.destroy(); });
      req.on('aborted', () => upstream.destroy()); res.on('close', () => { if (!res.writableEnded) upstream.destroy(); });
      req.pipe(upstream); return;
    }
    if (req.url === '/frontend-config.json' && req.method === 'GET') return send(res, 200, { captchaEnabled, captchaVid, version: '2.0.0' });
    const pathname = req.url.split('?')[0];
    const types = { '.html': 'text/html', '.svg': 'image/svg+xml', '.js': 'text/javascript', '.css': 'text/css' };
    const filename = pathname === '/' ? 'index.html' : pathname === '/icon.svg' ? 'icon.svg' : /^\/assets\/[A-Za-z0-9_-]+\.(js|css)$/.test(pathname) ? pathname.slice(1) : null;
    if (req.method !== 'GET' || !filename) return send(res, 404, { code: 10404, message: '未找到页面。', data: null });
    fs.readFile(path.join(dist, filename), (err, content) => {
      if (err) return send(res, filename === 'index.html' ? 503 : 404, { message: filename === 'index.html' ? '前端尚未构建，请先运行 npm ci 和 npm run build。' : '未找到页面文件。' });
      res.writeHead(200, { 'Content-Type': types[path.extname(filename)] + '; charset=utf-8', 'Cache-Control': 'no-store' }); res.end(content);
    });
  };
  return tls ? https.createServer(tls, handler) : http.createServer(handler);
}
module.exports = { createServer };
if (require.main === module) {
  const tls = process.env.TLS_CERT && process.env.TLS_KEY ? { cert: fs.readFileSync(process.env.TLS_CERT), key: fs.readFileSync(process.env.TLS_KEY) } : undefined;
  const port = Number(process.env.FRONTEND_PORT || 4318);
  createServer({ tls }).listen(port, '127.0.0.1', () => console.log(`Qixue frontend: ${tls ? 'https' : 'http'}://127.0.0.1:${port}`)).on('error', e => { console.error(e.code === 'EADDRINUSE' ? 'Port in use. Set FRONTEND_PORT to another port.' : 'Cannot start frontend server.'); process.exitCode = 1; });
}
