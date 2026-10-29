import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { createServer } = require('./server.cjs');

// Share the exact same API proxy and public config in development and production.
function localBackend() {
  return { name: 'local-backend', configureServer(vite) {
    const gateway = createServer();
    vite.middlewares.use((req, res, next) => {
      if (req.url.startsWith('/api/v1/') || req.url === '/frontend-config.json') gateway.emit('request', req, res);
      else next();
    });
  } };
}

export default defineConfig({
  // Absolute URLs belong to public/ and must remain browser URLs on Windows too.
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } }), localBackend()],
  server: { host: '127.0.0.1', port: 4318, strictPort: true },
  test: { environment: 'jsdom', include: ['tests/**/*.spec.js'], setupFiles: ['./tests/setup.js'], clearMocks: true },
});
