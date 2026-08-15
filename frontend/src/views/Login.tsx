/**
 * The landing page: what this is, and the one way in.
 *
 * The Google button is a full-page navigation rather than a `fetch`. It has to
 * be: the OAuth flow ends with Google redirecting the browser back to
 * `/api/auth/google/callback`, and that response is what carries the
 * `Set-Cookie`. An XHR could not follow the consent screen, and a cookie set on
 * an XHR response to a redirect chain would not stick.
 *
 * The dev box below it is an authentication bypass. See the styling notes on
 * `DevLoginBox` for why it is deliberately unattractive.
 *
 * **The hero claims a measurement, so it states the number.** A landing page for
 * a RAG harness that says "accurate answers" is indistinguishable from every
 * other one, and this project's actual differentiator is that it will tell you
 * when it is wrong. So the third pillar names the four Ragas metrics rather than
 * promising quality, and the strapline says "know when it is wrong" -- which is
 * the claim the Evaluate view can actually cash.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import { api, API_URL } from "../lib/api.ts";
import type { User } from "../lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import PipelineScene from "../components/PipelineScene.tsx";

/** The six stages, as text. Also drawn by `PipelineScene`, which is aria-hidden. */
const STAGE_NAMES = ["Ingest", "Embed", "Retrieve", "Rerank", "Generate", "Measure"];

const PILLARS = [
  {
    title: "Grounded, or it declines",
    body: "Answers come from your documents or not at all. A refusal is a correct outcome, recorded as one.",
  },
  {
    title: "Every turn is traceable",
    body: "Rewrite, retrieval, rerank scores and the exact chunks that reached the prompt -- kept per query, not sampled.",
  },
  {
    title: "Scored on a golden set",
    body: "Faithfulness, answer relevance, context precision and recall, plus the weakest metric and what to do about it.",
  },
];

export default function Login({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  return (
    <div className="relative min-h-screen overflow-hidden bg-slate-950">
      {/*
        Backdrop, in two layers. Both are `fixed` and outside the perspective
        chain in PipelineScene -- a blur anywhere between the perspective and
        the panes would silently flatten the scene (see index.css).
      */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 opacity-[0.55]"
        style={{
          backgroundImage:
            "radial-gradient(60% 45% at 22% 12%, rgba(16,185,129,0.16), transparent 70%)," +
            "radial-gradient(55% 45% at 82% 78%, rgba(56,189,248,0.13), transparent 70%)",
        }}
      />
      {/* A faint engineering grid. Masked so it fades out rather than ending on a line. */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 opacity-[0.18]"
        style={{
          backgroundImage:
            "linear-gradient(rgba(148,163,184,0.35) 1px, transparent 1px)," +
            "linear-gradient(90deg, rgba(148,163,184,0.35) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          maskImage: "radial-gradient(ellipse 80% 60% at 50% 35%, black, transparent 75%)",
          WebkitMaskImage: "radial-gradient(ellipse 80% 60% at 50% 35%, black, transparent 75%)",
        }}
      />

      <div className="relative mx-auto grid max-w-6xl gap-12 px-6 py-14 lg:grid-cols-[1.15fr_1fr] lg:items-center lg:gap-16 lg:py-20">
        {/* ---------------------------------------------------------- Hero */}
        <section className="gw-rise">
          <div className="flex items-center gap-2.5">
            <Wordmark />
            <span className="text-lg font-semibold tracking-tight text-slate-100">
              Groundwork
            </span>
            <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-0.5 text-[11px] font-medium tracking-wide text-emerald-300">
              agentic RAG harness
            </span>
          </div>

          <h1 className="mt-7 text-4xl font-semibold leading-[1.1] tracking-tight text-slate-50 sm:text-5xl">
            Retrieval over your own documents
            <span className="block bg-gradient-to-r from-emerald-300 via-emerald-200 to-sky-300 bg-clip-text text-transparent">
              that tells you when it is wrong.
            </span>
          </h1>

          <p className="mt-5 max-w-xl text-[15px] leading-relaxed text-slate-400">
            Upload a corpus, get an agent that answers only from it, then run a ten-question
            golden set and read the scorecard. Six stages, every one of them inspectable.
          </p>

          <PipelineScene />

          {/*
            The accessible copy of the scene above. `PipelineScene` is
            aria-hidden precisely because this list exists -- so the stage names
            are announced once, as text, in order.
          */}
          <ol className="-mt-2 flex flex-wrap items-center gap-x-2 gap-y-2 text-[11px] font-medium uppercase tracking-[0.14em] text-slate-500">
            {STAGE_NAMES.map((name, index) => (
              <li key={name} className="flex items-center gap-2">
                <span className={index === STAGE_NAMES.length - 1 ? "text-emerald-300" : ""}>
                  {name}
                </span>
                {index < STAGE_NAMES.length - 1 && (
                  <span aria-hidden="true" className="text-slate-700">
                    &rarr;
                  </span>
                )}
              </li>
            ))}
          </ol>

          <dl className="mt-10 grid gap-6 sm:grid-cols-3">
            {PILLARS.map((pillar) => (
              <div key={pillar.title} className="border-t border-slate-800 pt-4">
                <dt className="text-sm font-medium text-slate-200">{pillar.title}</dt>
                <dd className="mt-1.5 text-[13px] leading-relaxed text-slate-500">
                  {pillar.body}
                </dd>
              </div>
            ))}
          </dl>
        </section>

        {/* --------------------------------------------------------- Sign in */}
        <section className="gw-rise w-full justify-self-center lg:max-w-md" style={{ animationDelay: "0.12s" }}>
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6 shadow-2xl shadow-emerald-950/30 sm:p-8">
            <h2 className="text-lg font-semibold tracking-tight text-slate-100">Sign in</h2>
            <p className="mt-1.5 text-sm text-slate-500">
              Your agents, documents and eval runs are scoped to your account.
            </p>

            <a
              data-testid="login-google"
              href={`${API_URL}/api/auth/google/login`}
              className="mt-6 flex w-full items-center justify-center gap-3 rounded-lg bg-slate-100 px-4 py-3 text-sm font-medium text-slate-900 transition hover:bg-white"
            >
              <GoogleMark />
              Sign in with Google
            </a>
            <p className="mt-4 text-center text-xs leading-relaxed text-slate-500">
              We store your Google account ID, email and name. Google&rsquo;s access and
              refresh tokens are discarded immediately after the identity check.
            </p>
          </div>

          {/*
            `import.meta.env.DEV` is replaced by Vite with a literal `false` in a
            production build, so this entire subtree is removed by dead-code
            elimination -- it is not merely hidden at runtime. Combined with the
            backend's own three-way gate (opt-in flag, ENVIRONMENT=development,
            loopback client), the bypass has to fail twice independently before it
            could reach a deployed page.
          */}
          {import.meta.env.DEV && <DevLoginBox onAuthenticated={onAuthenticated} />}
        </section>
      </div>
    </div>
  );
}

/**
 * The mark: six stacked strata narrowing to a point -- ground layers, and the
 * pipeline funnelling to one answer. Inline SVG so the page makes no
 * third-party request and the logo cannot be a render-blocking fetch.
 */
function Wordmark() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" aria-hidden="true" className="shrink-0">
      <defs>
        <linearGradient id="gw-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#6ee7b7" />
          <stop offset="100%" stopColor="#38bdf8" />
        </linearGradient>
      </defs>
      <g fill="none" stroke="url(#gw-mark)" strokeWidth="1.6" strokeLinecap="round">
        <path d="M3 6.5h18" opacity="0.95" />
        <path d="M4.6 10.5h14.8" opacity="0.8" />
        <path d="M6.2 14.5h11.6" opacity="0.65" />
        <path d="M8 18.5h8" opacity="0.5" />
      </g>
    </svg>
  );
}

/**
 * The gated bypass, styled to look like what it is.
 *
 * Amber everywhere, a hazard-striped header, a literal "DEV ONLY" badge and a
 * sentence naming it an authentication bypass. This is not decoration: the one
 * genuine risk of a dev-login affordance is that it stops reading as
 * exceptional and someone starts using it as the convenient way in, or copies
 * the pattern into a screenshot that ends up in a deployment. It should be
 * impossible to confuse with the button above it, at a glance, from across a
 * room.
 */
function DevLoginBox({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const [email, setEmail] = useState("simoraj@gmail.com");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api<User>("/api/auth/dev-login", {
        method: "POST",
        json: { email: email.trim(), name: "Dev User" },
      });
      onAuthenticated(user);
    } catch (cause) {
      // A 404 here is the gate refusing, not a missing route -- the backend
      // answers 404 rather than 403 so a probe cannot tell the difference. Said
      // plainly, because otherwise the next twenty minutes go into debugging a
      // routing problem that does not exist.
      setError(
        `${errorMessage(cause)}\n\nA 404 means the backend refused: dev-login requires ` +
          `DEV_AUTH_ENABLED=true, ENVIRONMENT=development, and a request from localhost.`,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-6 rounded-xl border-2 border-dashed border-amber-500/70 bg-amber-950/20 p-5"
    >
      <div className="mb-3 flex items-center gap-2">
        <span className="rounded bg-amber-500 px-2 py-0.5 text-xs font-bold tracking-wider text-amber-950">
          DEV ONLY
        </span>
        <span className="text-sm font-medium text-amber-200">Skip Google sign-in</span>
      </div>

      <p className="mb-4 text-xs leading-relaxed text-amber-200/80">
        This is an <strong>authentication bypass</strong>. It exists so the flow can be
        driven end to end without a human at a consent screen. It is disabled outside
        local development and stripped from production builds. Never rely on it.
      </p>

      <label className="block text-xs font-medium text-amber-200/90" htmlFor="dev-login-email">
        Email to sign in as
      </label>
      <input
        id="dev-login-email"
        data-testid="dev-login-email"
        type="email"
        required
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        className="mt-1 w-full rounded-md border border-amber-700/60 bg-slate-950 px-3 py-2 text-sm text-amber-100 outline-none focus:border-amber-500"
      />

      <button
        type="submit"
        data-testid="dev-login-submit"
        disabled={busy}
        className="mt-3 w-full rounded-md bg-amber-500 px-4 py-2 text-sm font-semibold text-amber-950 transition hover:bg-amber-400 disabled:opacity-50"
      >
        {busy ? "Signing in…" : "Dev login"}
      </button>

      {busy && (
        <div className="mt-3">
          <Spinner label="Creating session" />
        </div>
      )}
      <div className="mt-3">
        <ErrorBanner error={error} />
      </div>
    </form>
  );
}

/** Google's mark, inlined as SVG so the page makes no third-party request. */
function GoogleMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}
