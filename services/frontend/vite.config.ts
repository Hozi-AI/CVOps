import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Test config lives in vitest.config.ts (jsdom + MSW harness), kept separate so
// this dev-server config stays focused on the edge/proxy wiring below.
export default defineConfig({
  plugins: [react()],
  server: {
    // Accept the dev-VM Host header that the nginx edge forwards — Vite 5
    // otherwise rejects it with "Blocked request. This host is not allowed".
    host: true,
    allowedHosts: true,
    // The browser reaches the app through the edge on :80, so the HMR websocket
    // must connect back through :80, not directly to Vite's :5173.
    // VITE_HMR_CLIENT_PORT overrides it; set it to `auto` to let the client use
    // the page's own port (a box whose :80 belongs to something else — there a
    // dead socket on :80 makes Vite's reconnect ping hit that server, get a
    // 200, and reload the page every couple of seconds).
    hmr: {
      clientPort:
        process.env.VITE_HMR_CLIENT_PORT === 'auto'
          ? undefined
          : Number(process.env.VITE_HMR_CLIENT_PORT || 80),
    },
    proxy: {
      // The API owns the /api/v1 prefix, so this is a pass-through (no rewrite) —
      // same as the nginx edge. /api/v1/* is forwarded verbatim to the API.
      '/api/v1': {
        target: 'http://localhost:8000',
      },
    },
  },
})
