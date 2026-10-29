const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { createServer, listenAddresses } = require('./server.cjs');
const vite = path.join(__dirname, 'node_modules/vite/bin/vite.js');
if (fs.existsSync(vite)) {
  const result = spawnSync(process.execPath, [vite, 'build'], { cwd: __dirname, stdio: 'inherit', windowsHide: true });
  if (result.status !== 0) process.exit(result.status || 1);
} else if (!fs.existsSync(path.join(__dirname, 'dist/index.html'))) {
  console.error('Install frontend dependencies first: npm ci --ignore-scripts --no-audit --no-fund');
  process.exit(1);
}
const tls = process.env.TLS_CERT && process.env.TLS_KEY ? { cert: fs.readFileSync(process.env.TLS_CERT), key: fs.readFileSync(process.env.TLS_KEY) } : undefined;
const port = Number(process.env.FRONTEND_PORT || 4318), host = process.env.FRONTEND_HOST || '127.0.0.1';
const urls = listenAddresses(host, port, tls ? 'https' : 'http'), url = urls[0];
createServer({ tls }).listen(port, host, () => {
  console.log(`\nQixue: ${urls.join('\n       ')}\nBackend: ${process.env.BACKEND_URL || 'http://127.0.0.1:8000'}\nKeep this window open. Ctrl+C to stop.\n`);
  if (process.platform === 'win32') { const p = spawn('explorer.exe', [url], { stdio: 'ignore', windowsHide: true }); p.on('error', () => {}); p.unref(); }
}).on('error', e => { console.error(e.code === 'EADDRINUSE' ? 'Port in use. Open the existing page or set FRONTEND_PORT.' : 'Cannot start frontend.'); process.exitCode = 1; });
