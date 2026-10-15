import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { defineConfig } from "vitest/config";

const securityHeaders = {
  "Content-Security-Policy": [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "frame-src 'self' blob:",
    "form-action 'self'",
    "script-src 'self'",
    "script-src-attr 'none'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data: blob:",
    "media-src 'self' data: blob:",
    "connect-src 'self' ws:",
    "worker-src 'self' blob:",
  ].join("; "),
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
};

// React Refresh injects a small inline preamble in development. Permit only its exact bytes;
// the production/preview policy remains script-src 'self', including for EPUB content.
const refreshHash = createHash("sha256")
  .update(react.preambleCode.replace("__BASE__", "/"))
  .digest("base64");
const developmentHeaders = {
  ...securityHeaders,
  "Content-Security-Policy": securityHeaders["Content-Security-Policy"].replace(
    "script-src 'self'",
    `script-src 'self' 'sha256-${refreshHash}'`,
  ),
};

// The backend (FastAPI, ADR 0007 D-A11) serves /api; the dev server proxies so the reader fetches
// EPUB bytes + position/view routes same-origin.
export default defineConfig({
  plugins: [
    react(),
    {
      name: "development-offline-reset",
      apply: "serve",
      configureServer(server) {
        // An older production worker may already control this origin. Replace it with a worker
        // that intercepts nothing and unregisters itself; dev modules must always reach Vite.
        server.middlewares.use("/sw.js", (_request, response) => {
          response.setHeader("Content-Type", "application/javascript");
          response.setHeader("Cache-Control", "no-store");
          response.end(`
            self.addEventListener("install", () => self.skipWaiting());
            self.addEventListener("activate", (event) => {
              event.waitUntil(self.clients.claim().then(() => self.registration.unregister()));
            });
          `);
        });
      },
    },
  ],
  build: {
    // Foliate's PDF adapter loads its bundled layer CSS at module initialization.
    target: "es2022",
  },
  server: {
    port: 5173,
    headers: developmentHeaders,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  preview: { headers: securityHeaders },
  test: {
    environment: "jsdom",
    globals: true, // testing-library auto-cleanup hooks into the global afterEach
    setupFiles: ["src/test-setup.ts"], // registers jest-axe's toHaveNoViolations
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["src/vendor/**", "node_modules/**"],
  },
});
