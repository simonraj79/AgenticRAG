/**
 * The HTTP host: Better Auth at /api/auth/*, the built SPA at everything else.
 *
 * THE CO-LOCATION IS THE FEATURE. Both are served from ONE origin, so the
 * session cookie Better Auth sets is first-party to the page that reads it.
 * Split them onto two Render subdomains and the Public Suffix List makes them
 * different sites, the cookie becomes third-party, and every browser that
 * blocks third-party cookies silently signs the user back out. That failure
 * looks like a broken login and is a deployment topology.
 *
 * Nothing else lives here. The FastAPI service keeps every application route;
 * this process only has to answer "who is this" and hand back files.
 */

import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { Hono } from "hono";

import { auth } from "./auth.js";

const app = new Hono();

/**
 * Better Auth owns this prefix entirely -- sign-in, the Google callback,
 * session, sign-out, /token and /jwks. Registered FIRST so no static rule can
 * shadow it: `serveStatic` below would happily answer /api/auth/jwks with the
 * SPA's index.html, and the Python verifier would then fail to parse a key set
 * that is actually an HTML document. That error names JSON, not routing.
 */
app.on(["GET", "POST"], "/api/auth/*", (c) => auth.handler(c.req.raw));

/**
 * Render's health check target. Deliberately not /api/health -- that belongs to
 * the FastAPI service, and two services answering the same path on different
 * hosts is a confusion waiting to be debugged at 2am.
 */
app.get("/healthz", (c) => c.json({ ok: true, service: "auth" }));

/**
 * The built SPA. `frontend/dist` is copied to ./public at build time (see
 * package.json in the frontend and the deploy notes), so this process ships the
 * exact bytes Vite produced.
 */
app.use("/*", serveStatic({ root: "./public" }));

/**
 * SPA fallback. A client-side route like /agents/<uuid> has no file behind it,
 * and without this it 404s on refresh -- which reads as a broken deep link
 * rather than as a missing rewrite rule. Registered last so it only ever
 * catches what nothing above matched.
 */
app.get("*", serveStatic({ path: "./public/index.html" }));

/**
 * `0.0.0.0`, never localhost. Binding the loopback interface passes every local
 * test and then fails Render's health check, because the check arrives from
 * outside the container. Same rule as the uvicorn command for the API.
 */
const port = Number(process.env.PORT ?? 3000);

serve({ fetch: app.fetch, port, hostname: "0.0.0.0" }, (info) => {
  // ASCII only -- this line is read in Render's log viewer and in a Windows
  // console, and the second one mangles anything else.
  console.log(`[auth] listening on 0.0.0.0:${info.port}`);
});
