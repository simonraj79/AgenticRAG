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
 *
 * ## The specimen
 *
 * The page's centre is not a headline or an abstract graphic, it is a worked
 * example of one turn: a question, an answer in the reading face, numbered
 * markers in the prose, and the passages those markers point at, in the margin.
 *
 * That is the whole product in one figure, and it is the honest version of the
 * claim above it. A visitor who reads nothing else can see that every sentence
 * is bound to a chunk of a named file with a retrieval score -- which is the
 * thing that would otherwise take a sign-in, an upload and a two-minute ingest
 * to discover.
 *
 * **It shows a refusal as well as an answer**, and that is deliberate. The
 * strapline promises the system will tell you when it is wrong; a landing page
 * that then shows only successes is asking to be taken on trust for the one
 * claim it is making. The second turn is the product declining, drawn exactly as
 * carefully as the first.
 *
 * It is labelled as an example. It is illustrative -- not a recorded
 * transcript -- and the caption says so rather than implying otherwise by
 * omission.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import { api, API_URL } from "../lib/api.ts";
import { signInWithGoogle } from "../lib/auth-client.ts";
import type { User } from "../lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import {
  ACCENT_TONE,
  BAD_TONE,
  BTN_PRIMARY,
  CARD,
  EYEBROW,
  FIELD,
  LINK,
  NOTICE,
  PILL,
  PILL_NEUTRAL,
  WARN_TONE,
} from "../lib/styles.ts";
import PipelineScene from "../components/PipelineScene.tsx";
import ThemeToggle from "../components/ThemeToggle.tsx";

const PILLARS = [
  {
    title: "Grounded, or it declines",
    body: "Answers come from your documents or not at all. A refusal is a correct outcome, recorded as one.",
  },
  {
    title: "Every turn is traceable",
    body: "Rewrite, retrieval, rerank scores and the exact chunks that reached the prompt — kept per query, not sampled.",
  },
  {
    title: "Scored on a golden set",
    body: "Faithfulness, answer relevance, context precision and recall, plus the weakest metric and what to do about it.",
  },
];

export default function Login({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  // Local to THIS component. `DevLoginBox` further down declares its own `busy`
  // and `error`; they are different concerns on the same page and sharing them
  // would let a failed dev-login disable the Google button.
  const [busy, setBusy] = useState(false);
  const [signInError, setSignInError] = useState<string | null>(null);

  return (
    <div className="relative min-h-screen overflow-hidden bg-canvas">
      <div className="relative mx-auto max-w-6xl px-5 py-10 sm:px-8 sm:py-14">
        {/* ------------------------------------------------------- brand row */}
        <div className="flex items-center gap-3">
          <Wordmark />
          <span className="text-base font-semibold tracking-tight text-ink">Groundwork</span>
          <span className={`${PILL} ${ACCENT_TONE} hidden sm:inline-flex`}>
            agentic RAG harness
          </span>
          {/* The only control on the page besides sign-in. A visitor who reads
              dark-on-light badly should not have to sign in to fix it. */}
          <span className="ml-auto">
            <ThemeToggle />
          </span>
        </div>

        {/* ------------------------------------------------------------ hero */}
        <div className="mt-12 grid gap-10 lg:grid-cols-[1.25fr_1fr] lg:items-start lg:gap-16">
          <section className="gw-rise">
            <h1 className="max-w-2xl text-3xl font-semibold tracking-tight text-ink sm:text-4xl lg:text-5xl">
              Retrieval over your own documents
              <span className="block text-muted">that tells you when it is wrong.</span>
            </h1>

            <p className="mt-6 max-w-xl font-serif text-base leading-relaxed text-muted">
              Upload a corpus, get an agent that answers only from it, then run a
              ten-question golden set and read the scorecard.
            </p>

            {/*
              The pipeline sits in the hero rather than below the fold, because
              the hero's left column is otherwise a headline and one sentence
              against a sign-in card that is three times taller -- and in a
              production build the dev-login box below that card is stripped, so
              the imbalance is a real page rather than a development artefact.
              It also puts the claim and the mechanism in one view: what this
              does, and the six steps it does it in.
            */}
            <PipelineScene />
          </section>

          {/* --------------------------------------------------------- sign in */}
          <section
            className="gw-rise w-full lg:max-w-sm lg:justify-self-end"
            style={{ animationDelay: "0.1s" }}
          >
            <div className={`${CARD} p-6 shadow-sm`}>
              <h2 className="text-lg font-semibold tracking-tight text-ink">Sign in</h2>
              <p className="mt-1.5 text-sm text-muted">
                Your agents, documents and eval runs are scoped to your account.
              </p>

              {/*
                A BUTTON, not a link, because Better Auth's sign-in is a POST that
                returns the provider URL rather than a URL the page can navigate
                to directly. `signInWithGoogle` performs the redirect itself.

                The old Authlib link is kept below and is deliberately not styled
                as an equal choice: both paths authenticate, the API accepts
                either, and this one is the one being cut over to. It is removed
                in the same commit that removes Authlib.
              */}
              <button
                type="button"
                data-testid="login-google"
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  setSignInError(null);
                  // No `await`: this navigates away on success, so there is no
                  // "after" to run. The catch exists for the failure case, where
                  // the page stays put and the user is owed an explanation.
                  signInWithGoogle(window.location.pathname).catch((cause) => {
                    setBusy(false);
                    setSignInError(
                      `Sign-in could not be started (${String(cause)}). ` +
                        `If this persists, the auth service may still be starting up.`,
                    );
                  });
                }}
                className={`${BTN_PRIMARY} mt-5 w-full py-3`}
              >
                <GoogleMark />
                {busy ? "Redirecting to Google…" : "Sign in with Google"}
              </button>

              {signInError ? (
                <p data-testid="login-error" role="alert" className={`${NOTICE} ${BAD_TONE} mt-3`}>
                  {signInError}
                </p>
              ) : null}

              <p className="mt-4 text-xs leading-relaxed text-faint">
                We store your Google account ID, email and name. Google&rsquo;s access and
                refresh tokens are discarded immediately after the identity check.
              </p>

              {/*
                THE PREVIOUS SIGN-IN PATH, still live on the backend. Present so
                that a failure in the new service is a degraded login rather than
                no login at all, and so both can be exercised side by side while
                the cutover is verified. Delete this together with app/auth/oauth.py.
              */}
              <p className="mt-3 text-xs text-faint">
                <a
                  data-testid="login-google-legacy"
                  href={`${API_URL}/api/auth/google/login`}
                  className={LINK}
                >
                  Having trouble? Use the previous sign-in
                </a>
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

        {/* -------------------------------------------------------- specimen */}
        <Specimen />

        {/* --------------------------------------------------------- pillars */}
        <dl className="mt-16 grid gap-8 sm:grid-cols-3 sm:gap-10">
          {PILLARS.map((pillar) => (
            <div key={pillar.title} className="border-t border-line pt-4">
              <dt className="text-sm font-semibold text-ink">{pillar.title}</dt>
              <dd className="mt-1.5 text-xs leading-relaxed text-muted">{pillar.body}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

/* ========================================================================== */
/* The specimen                                                                */
/* ========================================================================== */

/**
 * Two turns, drawn the way the product draws them.
 *
 * This deliberately mirrors `Message.tsx` rather than importing it: that
 * component takes a full `ChatMessage` with a query id, citations carrying
 * chunk ids, trace wiring and a live `TracePanel` that would fetch on mount.
 * Constructing a fake one to render a picture would couple the landing page to
 * the chat schema, so that every future field added to a turn breaks the page
 * that has no turns.
 *
 * The cost is that the two can drift, and it is bounded: both take their
 * geometry from `.gw-apparatus` and their colours from the same tokens, so a
 * drift is a spacing difference rather than a different-looking product.
 */
function Specimen() {
  return (
    <section className="mt-16" aria-labelledby="specimen-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2 border-b border-line pb-3">
        <h2 id="specimen-heading" className="text-lg font-semibold tracking-tight text-ink">
          What one turn looks like
        </h2>
        <p className="text-xs text-faint">
          An example, not a recorded transcript. Markers in the prose point at the passages
          beside them.
        </p>
      </div>

      <div className={`${CARD} mt-6 p-5 sm:p-7`}>
        {/* ------------------------------------------------ a grounded turn */}
        <div className="border-l-2 border-line-strong pl-3.5">
          <p className={EYEBROW}>Asked</p>
          <p className="mt-1 text-sm font-medium text-ink">
            What data rate does the Ka-band downlink hold in heavy rain?
          </p>
        </div>

        <div className="gw-apparatus mt-4">
          <div className="min-w-0">
            <div className="gw-prose">
              {/*
                Long enough to fill the column beside two source cards. That is
                not padding for its own sake: with `align-items: start` the row
                is as tall as its tallest track, so a two-line answer next to a
                two-card margin leaves a visible void that reads as a broken
                layout rather than as a short answer. A real turn runs several
                paragraphs; the specimen should not be the one place the
                apparatus looks unbalanced.
              */}
              <p>
                The Ka-band downlink runs at 200&nbsp;Mbps in clear sky and falls to
                40&nbsp;Mbps under heavy rain, a drop driven by roughly 12&nbsp;dB of rain
                fade on the link budget
                <SpecimenMarker n={1} />.
              </p>
              <p>
                That is why the operations brief schedules bulk downlink outside the
                monsoon window rather than attempting to hold the higher rate through it
                <SpecimenMarker n={2} />. The material does not give a per-month
                availability figure, so this answer does not offer one.
              </p>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line pt-3">
              <span className={PILL_NEUTRAL}>searched twice</span>
              <span className="ml-auto font-mono text-xs text-faint">
                14:32 &middot; 11.4s &middot; deepseek-v4-flash
              </span>
            </div>
          </div>

          <aside aria-label="Retrieved passages, example">
            <p className={`${EYEBROW} xl:border-b xl:border-line xl:pb-2`}>Sources</p>
            <p className="mt-1.5 mb-2.5 text-xs leading-relaxed text-muted">
              Every claim above is numbered to one of these.
            </p>
            <ol className="space-y-2">
              <SpecimenSource
                marker={1}
                filename="3.1-link-budget.md"
                chunk={4}
                rank={1}
                score={0.87}
                peak={0.87}
                text="Clear-sky Ka-band throughput is 200 Mbps. Rain fade of 12 dB reduces the achievable rate to 40 Mbps."
              />
              <SpecimenSource
                marker={2}
                filename="4.2-ops-brief.md"
                chunk={11}
                rank={2}
                score={0.81}
                peak={0.87}
                text="Bulk downlink is scheduled outside the monsoon window to avoid sustained rain fade."
              />
            </ol>
          </aside>
        </div>

        {/* ------------------------------------------------------ a refusal */}
        <div className="mt-8 border-t border-line pt-7">
          <div className="border-l-2 border-line-strong pl-3.5">
            <p className={EYEBROW}>Asked</p>
            <p className="mt-1 text-sm font-medium text-ink">
              Which launch vehicle was used?
            </p>
          </div>

          <div className="gw-apparatus mt-4">
            <div className="min-w-0">
              <div className="gw-prose">
                <p>
                  The provided material does not state which launch vehicle was used. It
                  covers the link budget and the operations schedule, but not the launch
                  segment.
                </p>
              </div>

              <p className="mt-4 border-t border-line pt-3 text-xs leading-relaxed text-muted">
                The agent declined because the retrieved context did not support an answer.
                That is a correct outcome, not an error.
              </p>

              <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
                <span className={`${PILL} ${WARN_TONE}`}>refused</span>
                <span className="ml-auto font-mono text-xs text-faint">
                  14:36 &middot; 6.2s
                </span>
              </div>
            </div>

            <aside aria-label="Passages checked, example">
              <p className={`${EYEBROW} xl:border-b xl:border-line xl:pb-2`}>Passages checked</p>
              <p className="mt-1.5 mb-2.5 text-xs leading-relaxed text-muted">
                The closest passages the agent checked. They did not support an answer.
              </p>
              <ol className="space-y-2">
                <SpecimenSource
                  marker={1}
                  filename="3.1-link-budget.md"
                  chunk={2}
                  rank={1}
                  score={0.54}
                  peak={0.54}
                  text="Spacecraft mass at separation is 1,180 kg, including 240 kg of propellant."
                />
              </ol>
            </aside>
          </div>
        </div>
      </div>
    </section>
  );
}

/** A citation marker, drawn exactly as `Message.tsx` draws it -- minus the
 *  button, because there is nothing here to open. `<sup>` rather than
 *  `align-super` on a span, so it is a real superscript to a screen reader. */
function SpecimenMarker({ n }: { n: number }) {
  return (
    <sup className="ml-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-accent-line bg-accent-soft px-1 font-mono text-xs font-semibold text-accent">
      {n}
    </sup>
  );
}

/** One apparatus entry. Mirrors `CitationCard`'s resting state. */
function SpecimenSource({
  marker,
  filename,
  chunk,
  rank,
  score,
  peak,
  text,
}: {
  marker: number;
  filename: string;
  chunk: number;
  rank: number;
  score: number;
  /** The best score in this turn's own set. The bar is a within-turn
   *  comparison, exactly as `CitationCard` draws it -- see the measurement
   *  note there. Passing it explicitly keeps the specimen honest about what
   *  the product actually renders. */
  peak: number;
  text: string;
}) {
  return (
    <li className="rounded-md border border-line bg-surface p-3">
      <div className="flex items-baseline gap-2">
        <span
          aria-hidden="true"
          className="inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-sm border border-accent-line bg-accent-soft px-1 font-mono text-xs font-semibold text-accent"
        >
          {marker}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-ink">{filename}</span>
      </div>
      <p className="mt-1.5 font-mono text-xs text-faint">
        chunk {chunk} &middot; rank {rank}
      </p>
      <div className="mt-2 flex items-center gap-2">
        <span className="h-0.5 min-w-0 flex-1 rounded-full bg-line" aria-hidden="true">
          <span className="gw-strata" style={{ width: `${Math.round((score / peak) * 100)}%` }} />
        </span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-muted">
          {score.toFixed(2)}
        </span>
      </div>
      <p className="mt-2 line-clamp-2 border-t border-line pt-2 font-serif text-xs leading-relaxed text-muted">
        {text}
      </p>
    </li>
  );
}

/* ========================================================================== */

/**
 * The mark: six strata narrowing to a point -- ground layers, and the pipeline
 * funnelling to one answer. Inline SVG so the page makes no third-party request
 * and the logo cannot be a render-blocking fetch.
 *
 * `currentColor` at descending opacity rather than a gradient with two hex
 * stops. The gradient was emerald-to-sky, which is a fixed pair of values and
 * therefore the one element on the page that could not follow the theme; as
 * `currentColor` the mark inherits `text-ink` and is correct in both without a
 * second definition.
 */
function Wordmark() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="shrink-0 text-ink"
    >
      <g fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
        <path d="M3 6.5h18" opacity="0.95" />
        <path d="M4.6 10.5h14.8" opacity="0.78" />
        <path d="M6.2 14.5h11.6" opacity="0.58" />
        <path d="M8 18.5h8" opacity="0.4" />
      </g>
    </svg>
  );
}

/**
 * The gated bypass, styled to look like what it is.
 *
 * A hazard border, a literal "DEV ONLY" badge and a sentence naming it an
 * authentication bypass. This is not decoration: the one genuine risk of a
 * dev-login affordance is that it stops reading as exceptional and someone
 * starts using it as the convenient way in, or copies the pattern into a
 * screenshot that ends up in a deployment. It should be impossible to confuse
 * with the button above it, at a glance, from across a room.
 *
 * It is the one surface in the app allowed to look worse than everything around
 * it, and the redesign deliberately did not tidy it up.
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
      className="mt-6 rounded-lg border-2 border-dashed border-warn bg-warn-soft p-5"
    >
      <div className="mb-3 flex items-center gap-2">
        <span className="rounded-sm bg-warn px-2 py-0.5 text-xs font-bold tracking-wider text-inverse">
          DEV ONLY
        </span>
        <span className="text-sm font-medium text-warn">Skip Google sign-in</span>
      </div>

      <p className="mb-4 text-xs leading-relaxed text-warn">
        This is an <strong>authentication bypass</strong>. It exists so the flow can be
        driven end to end without a human at a consent screen. It is disabled outside local
        development and stripped from production builds. Never rely on it.
      </p>

      <label className="text-xs font-medium text-warn" htmlFor="dev-login-email">
        Email to sign in as
      </label>
      <input
        id="dev-login-email"
        data-testid="dev-login-email"
        type="email"
        required
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        className={`${FIELD} mt-1`}
      />

      <button
        type="submit"
        data-testid="dev-login-submit"
        disabled={busy}
        className="mt-3 inline-flex min-h-11 w-full items-center justify-center rounded-md bg-warn px-4 py-2 text-sm font-semibold text-inverse transition hover:opacity-90 disabled:opacity-50"
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

/** Google's mark, inlined as SVG so the page makes no third-party request.
 *  These four hex values are Google's brand colours and are deliberately NOT
 *  tokens -- they are not ours to theme, and a themed Google mark is a wrong
 *  Google mark. */
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
