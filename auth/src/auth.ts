/**
 * Better Auth configuration. Google identity in, a verifiable JWT out.
 *
 * This service exists because Better Auth is a TypeScript library and the API
 * it authenticates for is FastAPI. It is deliberately small: it owns identity
 * and nothing else. No business logic, no retrieval, no access to the
 * application's own tables -- the only rows it writes are its own.
 *
 * WHY IT SITS IN FRONT OF THE SPA RATHER THAN BESIDE IT.
 *
 * `onrender.com` is on the Public Suffix List, so two Render subdomains are not
 * merely different ORIGINS, they are different SITES. A session cookie set by a
 * separate auth host would be third-party on every request the SPA makes, and
 * browsers that block third-party cookies -- Safari, Incognito, Brave, Firefox
 * strict -- would drop it. The symptom is a user who signs in with Google
 * successfully and lands straight back on the login page, which this project
 * has already diagnosed once the expensive way.
 *
 * Serving the SPA's static assets from this same process makes the cookie
 * first-party and closes that permanently. `src/index.ts` is where that
 * co-location actually happens; this file only has to know that `baseURL` and
 * the SPA's origin are the same string.
 */

import { betterAuth } from "better-auth";
import { jwt } from "better-auth/plugins";
import { Pool } from "pg";

/** Fail at boot with the variable's name, never at first request with a stack. */
function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. This service cannot start without it. ` +
        `See auth/.env.example.`,
    );
  }
  return value;
}

const DATABASE_URL = required("DATABASE_URL");
const BASE_URL = required("BETTER_AUTH_URL");

/**
 * Render's INTERNAL Postgres endpoint presents a SELF-SIGNED certificate; the
 * external one has a valid public cert. This is the same trap the Python side
 * documents in `app/config.py` -- and it is the nastiest kind, because local
 * development always uses the EXTERNAL endpoint, so a verifying client passes
 * every local test and fails only once deployed.
 *
 * The tell is the hostname shape, exactly as it is in Python: internal hosts
 * have no dots (`dpg-xxx-a`), external ones are fully qualified. Verify when it
 * is an FQDN, relax when it is not. The connection is encrypted either way --
 * relaxing verification is not disabling TLS.
 */
function sslConfig(url: string): { rejectUnauthorized: boolean } | false {
  const host = (() => {
    try {
      return new URL(url).hostname;
    } catch {
      return "";
    }
  })();
  if (host === "localhost" || host === "127.0.0.1") return false;
  return { rejectUnauthorized: host.includes(".") };
}

/**
 * `sslmode` is stripped from the query string rather than passed through.
 * node-postgres and libpq disagree about it, and the failure mode of getting it
 * wrong is a handshake error that reads like a network fault. TLS is configured
 * through the `ssl` option above, which is the only place it is decided.
 */
function withoutSslMode(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.searchParams.delete("sslmode");
    return parsed.toString();
  } catch {
    return url;
  }
}

export const auth = betterAuth({
  // The same Render Postgres the FastAPI service uses. Better Auth creates and
  // owns `user`, `session`, `account`, `verification` and `jwks` here; it never
  // reads or writes the application's tables. The reverse direction -- FastAPI
  // reading `account` once per user -- is documented in app/auth/identity.py.
  database: new Pool({
    connectionString: withoutSslMode(DATABASE_URL),
    ssl: sslConfig(DATABASE_URL),
  }),

  baseURL: BASE_URL,
  secret: required("BETTER_AUTH_SECRET"),

  // Absent, Better Auth answers `Invalid Origin` on the sign-in POST -- a 403
  // that names the origin and not the setting. Localhost is listed because the
  // Vite dev server proxies /api/auth here and the browser's origin is 5173.
  trustedOrigins: [BASE_URL, "http://localhost:5173"],

  socialProviders: {
    google: {
      clientId: required("GOOGLE_OAUTH_CLIENT_ID"),
      clientSecret: required("GOOGLE_OAUTH_CLIENT_SECRET"),
      // Identity only -- no Gmail, no Calendar, nothing to authorise later.
      // The tokens Google returns are never persisted for the same reason the
      // Authlib callback dropped them: storing a credential buys a leak in
      // exchange for a capability nothing uses.
      scope: ["openid", "email", "profile"],
    },
  },

  plugins: [
    /**
     * THE LOAD-BEARING PLUGIN. FastAPI cannot call `auth.api.getSession()` --
     * that is a TypeScript function holding a database handle in another
     * process. The JWT plugin publishes a JWKS at `/api/auth/jwks`, so Python
     * can verify a login offline, with no round trip to this service and no
     * shared secret. `app/auth/jwt.py` is the other end.
     *
     * Signing defaults to EdDSA (Ed25519). If that is ever changed here, the
     * Python verifier follows automatically -- it reads the algorithm off the
     * KEY rather than off the token -- but `requirements.in` pins
     * `pyjwt[crypto]` for exactly this reason and must keep supporting whatever
     * is chosen.
     */
    jwt(),
  ],

  advanced: {
    // Render terminates TLS in front of this process, so NODE_ENV is the only
    // honest signal available here. Secure cookies locally would break plain
    // http://localhost development, and browsers treat localhost as a
    // trustworthy origin regardless.
    useSecureCookies: process.env.NODE_ENV === "production",
  },

  session: {
    // Fourteen days, matching SESSION_LIFETIME in app/auth/session.py. The two
    // systems run side by side through the cutover and a user whose cookie
    // outlived their Better Auth session -- or the reverse -- would experience
    // it as an intermittent, unreproducible logout.
    expiresIn: 60 * 60 * 24 * 14,
    updateAge: 60 * 60 * 24,
  },
});

export type Session = typeof auth.$Infer.Session;
