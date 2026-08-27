import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 5173 is NOT a preference. `cors_origins` on the backend is an allowlist,
    // so a second Vite instance auto-incrementing to 5174 is a DIFFERENT origin
    // and every request fails CORS -- surfacing as "Cannot reach the API" on a
    // backend that is running perfectly.
    port: 5173,

    /**
     * Everything under /api/auth is the Better Auth Node service.
     *
     * This proxy is what makes local development match production: there, one
     * process serves both the SPA and /api/auth, so the session cookie is
     * first-party. Here, Vite serves the SPA and forwards /api/auth to :3000 --
     * different process, SAME ORIGIN as far as the browser is concerned, which
     * is the only property that matters.
     *
     * Without it the browser would talk to localhost:3000 directly, the cookie
     * would be cross-origin in development and first-party in production, and
     * the one bug this architecture exists to prevent would be unreproducible
     * on a laptop.
     *
     * `changeOrigin` stays FALSE on purpose: Better Auth checks the `Origin`
     * header against `trustedOrigins`, and rewriting it would hide a
     * misconfiguration locally that then fires on deploy.
     */
    proxy: {
      "/api/auth": {
        target: "http://localhost:3000",
        changeOrigin: false,
      },
    },
  },
});
