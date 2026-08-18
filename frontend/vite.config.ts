import react from "@vitejs/plugin-react";
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

// The backend (FastAPI, ADR 0007 D-A11) serves /api; the dev server proxies so the reader fetches
// EPUB bytes + position/view routes same-origin.
export default defineConfig({
  plugins: [react()],
  build: {
    // Foliate's PDF adapter loads its bundled layer CSS at module initialization.
    target: "es2022",
  },
  server: {
    port: 5173,
    headers: securityHeaders,
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
