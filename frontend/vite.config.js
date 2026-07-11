import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8080',
      // Prometheus exposition is served at the root (not under /api), so it
      // must be proxied explicitly — otherwise fetch('/metrics') hits the
      // Vite SPA fallback and returns index.html instead of the metrics text.
      '/metrics': 'http://localhost:8080',
      // Health endpoint is also root-level; the Enterprise Shell StatusBar
      // reads it for real backend/DB/Redis dependency status. Frontend-only
      // change — no backend API is modified.
      '/health': 'http://localhost:8080',
    },
  },
});
