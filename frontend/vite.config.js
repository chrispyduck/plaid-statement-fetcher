import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'https://statement-fetcher.localhost:8765',
        changeOrigin: true,
        secure: false,
      },
      '/healthz': {
        target: 'https://statement-fetcher.localhost:8765',
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
