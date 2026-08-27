/**
 * The admin console. Everything, across every user.
 *
 * **The design rule that shapes every number on this page: a total without its
 * denominator is a lie in the making.** 76 of this system's queries predate
 * metering and can never be backfilled -- OpenRouter's generation ids were
 * never stored, so `GET /generation?id=` has nothing to look up. If those
 * render as zero-cost turns, historic spend is understated and the first
 * metered week looks like a spike. So every aggregate here shows what it rests
 * on, and `Coverage` is deliberately the largest tile on the page rather than a
 * footnote. EVAL.md documents the identical failure for `scored_count`, where a
 * metric's mean had its own denominator and the scorecard's footnote did not,
 * and the weakest-metric pointer then sent a reader to fix the wrong thing.
 *
 * **Reading a transcript is an act, not a hover.** It is the one call that
 * returns another person's words, and the backend writes an `audit_log` row for
 * it. So it is never prefetched, never fired from a list render, and the button
 * says what it does.
 *
 * No router: this project has none by choice (see `App.tsx`), so the console is
 * a `view` in the same discriminated union as everything else.
 */

import { useCallback, useEffect, useState } from "react";
import { admin } from "../lib/api.ts";
import type {
  AdminAccount,
  AdminAgent,
  AdminAuditEntry,
  AdminConversation,
  AdminEvalRun,
  AdminOverview,
  AdminSpend,
  AdminTrajectory,
  AdminTranscript,
  AdminUser,
  Measured,
  Spend,
} from "../lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import {
  ACCENT_TONE,
  BTN_QUIET,
  BTN_SECONDARY,
  BTN_SM,
  CARD,
  EYEBROW,
  NEUTRAL_TONE,
  NOTICE,
  OK_TONE,
  PILL,
  PILL_NEUTRAL,
  TAB,
  TAB_ACTIVE,
  TAB_INACTIVE,
  WARN_TONE,
} from "../lib/styles.ts";

type Tab =
  | "overview"
  | "users"
  | "agents"
  | "conversations"
  | "spend"
  | "evals"
  | "trajectory"
  | "audit";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "users", label: "Users" },
  { id: "agents", label: "Agents" },
  { id: "conversations", label: "Conversations" },
  { id: "spend", label: "Spend" },
  { id: "evals", label: "Evaluations" },
  { id: "trajectory", label: "Trajectory" },
  { id: "audit", label: "Audit" },
];

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

/**
 * Money, at a precision that does not round a real cost to nothing.
 *
 * A single turn measured $0.00048199 here. `toFixed(2)` renders that as "$0.00"
 * -- which is the exact failure this whole feature exists to avoid, arriving in
 * the last two characters of the pipeline. Below a cent, show four significant
 * figures.
 */
function usd(value: number | null | undefined): string {
  if (value == null) return "--";
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toPrecision(3)}`;
  return `$${value.toFixed(2)}`;
}

function num(value: number | null | undefined): string {
  return value == null ? "--" : value.toLocaleString();
}

function pct(value: number | null | undefined): string {
  return value == null ? "--" : `${(value * 100).toFixed(1)}%`;
}

function when(iso: string | null | undefined): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

// --------------------------------------------------------------------------
// Pieces
// --------------------------------------------------------------------------

function Tile({
  label,
  value,
  hint,
  wide,
}: {
  label: string;
  value: string;
  hint?: string;
  wide?: boolean;
}) {
  return (
    <div className={`${CARD} p-4 ${wide ? "sm:col-span-2" : ""}`}>
      <div className={EYEBROW}>{label}</div>
      <div className="mt-1 font-mono text-2xl font-semibold tabular-nums text-ink">
        {value}
      </div>
      {hint ? <div className="mt-1 text-xs text-muted">{hint}</div> : null}
    </div>
  );
}

/**
 * A measured value, rendered so the denominator cannot be lost.
 *
 * `measured === 0` is shown as "not measured" rather than as the value, because
 * a mean over nothing is not a small number -- it is an absent one.
 */
function MeasuredTile({
  label,
  m,
  format,
  wide,
}: {
  label: string;
  m: Measured;
  format: (v: number | null) => string;
  wide?: boolean;
}) {
  const unmeasured = m.total - m.measured;
  return (
    <Tile
      label={label}
      value={m.measured === 0 ? "not measured" : format(m.value)}
      hint={
        unmeasured > 0
          ? `${num(m.measured)} of ${num(m.total)} measured -- ${num(unmeasured)} predate metering`
          : `${num(m.measured)} of ${num(m.total)} measured`
      }
      wide={wide}
    />
  );
}

/**
 * Cost, or an honest statement that there is none to show.
 *
 * **`$0` and "not priced" are different facts and this is where they nearly got
 * collapsed.** The backend coalesces a NULL sum to `0.0` so the JSON type stays
 * a number, so a group of rerank calls -- which Cohere bills in search units and
 * reports no cost for -- arrived as `cost_usd: 0` and rendered as `$0`, i.e. as
 * FREE. That is the precise failure this feature was built to prevent, leaking
 * back in at the last render step, after every harness was green.
 *
 * `priced_calls` is the discriminator: zero priced calls out of N means nothing
 * in this group carries a reported cost.
 */
function cost(spend: Spend): string {
  if (spend.calls > 0 && spend.priced_calls === 0) return "not priced";
  return usd(spend.cost_usd);
}

function SpendCell({ spend }: { spend: Spend }) {
  const unpriced = spend.calls - spend.priced_calls;
  return (
    <div className="tabular-nums">
      <div className="font-mono font-medium text-ink">{cost(spend)}</div>
      <div className="text-xs text-muted">
        {num(spend.prompt_tokens + spend.completion_tokens)} tok / {num(spend.calls)} calls
        {unpriced > 0 ? ` (${num(unpriced)} unpriced)` : ""}
      </div>
    </div>
  );
}

function Table({
  head,
  children,
}: {
  head: string[];
  children: React.ReactNode;
}) {
  // The wrapper scrolls, never the page. Wide content inside its own
  // overflow-x container is the standing rule in this codebase.
  return (
    <div className={`${CARD} overflow-x-auto`}>
      <table className="min-w-full text-sm">
        <thead className="border-b border-line bg-sunken">
          <tr>
            {head.map((h) => (
              <th
                key={h}
                className="px-3 py-2 text-left text-xs font-semibold text-faint"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">{children}</tbody>
      </table>
    </div>
  );
}

/** A hook that loads once per tab and surfaces the error rather than eating it. */
function useLoad<T>(load: () => Promise<T>, deps: unknown[]): {
  data: T | null;
  error: string | null;
  loading: boolean;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    load()
      .then((value) => {
        if (!cancelled) setData(value);
      })
      .catch((cause) => {
        if (!cancelled) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading };
}

// --------------------------------------------------------------------------
// Tabs
// --------------------------------------------------------------------------

function Overview() {
  const { data, error, loading } = useLoad<[AdminOverview, AdminAccount]>(
    () => Promise.all([admin.overview(), admin.account()]),
    [],
  );
  if (loading) return <Spinner label="Loading overview" />;
  if (error) return <ErrorBanner error={error} />;
  if (!data) return null;
  const [o, account] = data;

  return (
    <div className="space-y-6">
      {/* `grid-cols-1` is the BASE deliberately. At 320px a two-column base put
          a currency value and its label into ~146px, which is where this page's
          only horizontal-overflow risk lived. */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Users"
          value={num(o.users)}
          hint={
            o.dev_identities > 0
              ? `${num(o.dev_identities)} are dev-login identities`
              : undefined
          }
        />
        <Tile label="Agents" value={num(o.agents)} hint={`${num(o.documents)} documents`} />
        <Tile
          label="Conversations"
          value={num(o.conversations)}
          hint={`${num(o.queries)} turns`}
        />
        <Tile
          label="Handouts"
          value={num(o.handouts)}
          hint={`${num(o.eval_runs)} eval runs`}
        />
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {/* Deliberately the widest tile on the page. It is the number that says
            how much of everything else can be trusted. */}
        <MeasuredTile
          label="Metering coverage"
          m={o.coverage}
          format={pct}
          wide
        />
        <Tile
          label="Recorded spend"
          value={usd(o.spend.cost_usd)}
          hint={`${num(o.spend.calls)} model calls since ${when(o.since)}`}
        />
        <MeasuredTile label="Refusal rate" m={o.refusal_rate} format={pct} />
      </div>

      <section className={`${CARD} p-5`}>
        <h3 className="text-sm font-semibold text-ink">
          Reconciliation against OpenRouter
        </h3>
        <p className="mt-1 text-xs text-muted">{account.note}</p>
        {account.openrouter.ok ? (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile label="Recorded here" value={usd(account.recorded.cost_usd)} />
            <Tile
              label="This key, month"
              value={usd(account.openrouter.key_usage_monthly)}
            />
            <Tile label="Account total" value={usd(account.openrouter.total_usage)} />
            <Tile
              label="Credits"
              value={usd(account.openrouter.total_credits)}
              hint="purchased, account-wide"
            />
          </div>
        ) : (
          // Reported, never raised. A page that 500s on a provider outage
          // teaches its reader to ignore the page.
          <p className={`${NOTICE} ${WARN_TONE} mt-3`}>
            Could not reach OpenRouter: {account.openrouter.error}. The recorded
            figure of {usd(account.recorded.cost_usd)} is unaffected.
          </p>
        )}
      </section>
    </div>
  );
}

function Users() {
  const { data, error, loading } = useLoad<AdminUser[]>(() => admin.users(), []);
  if (loading) return <Spinner label="Loading users" />;
  if (error) return <ErrorBanner error={error} />;

  return (
    <Table head={["User", "Role", "Agents", "Threads", "Turns", "Spend", "Last seen"]}>
      {(data ?? []).map((u) => (
        <tr key={u.id} className={u.is_active ? "" : "opacity-50"}>
          <td className="px-3 py-2">
            <div className="font-medium text-ink">{u.email}</div>
            <div className="text-xs text-muted">
              {u.name ?? "no name"}
              {/* Surfaced because this database holds two rows for two real
                  people -- the dev shim keys on `dev|<email>`, so signing in
                  for real creates a second row. "15 users" is not 15 people. */}
              {u.is_dev_identity ? " -- dev-login identity" : ""}
            </div>
          </td>
          <td className="px-3 py-2">
            <span className={u.role === "admin" ? `${PILL} ${ACCENT_TONE}` : PILL_NEUTRAL}>
              {u.role}
            </span>
          </td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(u.agents)}</td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(u.conversations)}</td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(u.queries)}</td>
          <td className="px-3 py-2">
            <SpendCell spend={u.spend} />
          </td>
          <td className="px-3 py-2 text-xs text-muted">{when(u.last_login_at)}</td>
        </tr>
      ))}
    </Table>
  );
}

function Agents() {
  const { data, error, loading } = useLoad<AdminAgent[]>(() => admin.agents(), []);
  if (loading) return <Spinner label="Loading agents" />;
  if (error) return <ErrorBanner error={error} />;

  return (
    <Table head={["Agent", "Owner", "Docs", "Threads", "Turns", "Evals", "Spend"]}>
      {(data ?? []).map((a) => (
        <tr key={a.id}>
          <td className="px-3 py-2 font-medium text-ink">{a.name}</td>
          <td className="px-3 py-2 text-muted">{a.owner_email}</td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(a.documents)}</td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(a.conversations)}</td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(a.queries)}</td>
          <td className="px-3 py-2 font-mono tabular-nums">{num(a.eval_runs)}</td>
          <td className="px-3 py-2">
            <SpendCell spend={a.spend} />
          </td>
        </tr>
      ))}
    </Table>
  );
}

function Conversations() {
  const { data, error, loading } = useLoad<AdminConversation[]>(
    () => admin.conversations({ limit: 100 }),
    [],
  );
  const [open, setOpen] = useState<AdminTranscript | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [readError, setReadError] = useState<string | null>(null);

  // Never prefetched, never on hover. This call writes an audit row.
  const read = useCallback(async (id: string) => {
    setOpening(id);
    setReadError(null);
    try {
      setOpen(await admin.transcript(id));
    } catch (cause) {
      setReadError(errorMessage(cause));
    } finally {
      setOpening(null);
    }
  }, []);

  if (loading) return <Spinner label="Loading conversations" />;
  if (error) return <ErrorBanner error={error} />;

  if (open) {
    return (
      <div className="space-y-4">
        <button
          type="button"
          onClick={() => setOpen(null)}
          className={BTN_QUIET}
        >
          Back to conversations
        </button>
        <header className={`${CARD} p-4`}>
          <h3 className="text-sm font-semibold text-ink">{open.title ?? "Untitled"}</h3>
          <p className="text-sm text-muted">
            {open.user_email} &middot; {open.agent_name} &middot; {when(open.created_at)}
          </p>
          <p className="mt-2 text-xs text-muted">
            Thread cost {usd(open.spend.cost_usd)} over {num(open.spend.calls)} model
            calls. This read was recorded in the audit log.
          </p>
        </header>
        <ol className="space-y-3">
          {open.turns.map((t) => (
            <li key={t.id} className={`${CARD} p-4`}>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                {/* User-supplied text in a `flex-wrap` row: without a break rule
                    a single long token pushes the row past 320px. */}
                <p className="min-w-0 font-medium break-words text-ink">{t.question}</p>
                <span className="font-mono text-xs tabular-nums text-muted">
                  {/* "not measured" and "$0" are different facts. */}
                  {t.measured ? usd(t.cost_usd) : "not measured"}
                  {t.latency_ms != null ? ` -- ${(t.latency_ms / 1000).toFixed(1)}s` : ""}
                  {t.refused ? " -- refused" : ""}
                </span>
              </div>
              {/* An answer assembled out of the user's corpus, so it is set in
                  serif -- the provenance signal the rest of the app uses. */}
              <p className="mt-2 font-serif text-sm whitespace-pre-wrap text-muted">
                {t.answer ?? "(no answer recorded)"}
              </p>
              <p className="mt-2 font-mono text-xs text-faint">
                {t.model_used ?? "model not recorded"}
                {t.prompt_tokens != null
                  ? ` -- ${num(t.prompt_tokens)}/${num(t.completion_tokens)} tokens`
                  : ""}
              </p>
            </li>
          ))}
        </ol>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <ErrorBanner error={readError} />
      <p className="text-xs text-muted">
        Opening a thread shows another person&rsquo;s questions and answers, and
        records that you read it.
      </p>
      <Table head={["Thread", "User", "Agent", "Turns", "Spend", "Updated", ""]}>
        {(data ?? []).map((c) => (
          <tr key={c.id}>
            <td className="px-3 py-2 font-medium text-ink">
              {c.title ?? "Untitled"}
            </td>
            <td className="px-3 py-2 text-muted">{c.user_email}</td>
            <td className="px-3 py-2 text-muted">{c.agent_name}</td>
            <td className="px-3 py-2 font-mono tabular-nums">
              {num(c.turns)}
              {c.refusals > 0 ? (
                <span className="text-xs text-muted"> ({c.refusals} refused)</span>
              ) : null}
            </td>
            <td className="px-3 py-2">
              <SpendCell spend={c.spend} />
            </td>
            <td className="px-3 py-2 text-xs text-muted">{when(c.updated_at)}</td>
            <td className="px-3 py-2">
              <button
                type="button"
                onClick={() => read(c.id)}
                disabled={opening === c.id}
                className={`${BTN_SECONDARY} ${BTN_SM} whitespace-nowrap`}
              >
                {opening === c.id ? "Opening..." : "Read transcript"}
              </button>
            </td>
          </tr>
        ))}
      </Table>
    </div>
  );
}

const GROUPS = ["call_kind", "model", "user", "agent", "provider"];

function SpendTab() {
  const [groupBy, setGroupBy] = useState("call_kind");
  const { data, error, loading } = useLoad<AdminSpend>(
    () => admin.spend(groupBy, 30),
    [groupBy],
  );

  /*
    The largest day in the window, and the scale every bar is drawn against.

    It used to be `Math.max(1, ...)`. That floor is the difference between a
    chart and a decoration: daily spend here is fractions of a cent, so a peak
    clamped to one dollar put every bar at ~0.05% -- under the 2% minimum below,
    which meant all of them rendered at exactly the same stub width. The bars
    were also invisible at the time (`bg-emerald-950/500` emitted no rule), so  (palette-check: ignore -- quoting the old class)
    fixing only the colour would have shipped a chart that is visible and still
    conveys nothing.

    The guard that remains is against division by zero, not against small
    numbers: a window with no spend at all has no meaningful scale, and every
    bar is then legitimately empty.
  */
  const costs = (data?.daily ?? []).map((d) => d.cost_usd);
  const peak = costs.length > 0 ? Math.max(...costs) : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {GROUPS.map((g) => (
          <button
            key={g}
            type="button"
            // The class says it LOOKS selected; the attribute says it IS. See
            // the note on `TAB` in `lib/styles.ts`.
            aria-pressed={groupBy === g}
            onClick={() => setGroupBy(g)}
            className={`${TAB} ${groupBy === g ? TAB_ACTIVE : TAB_INACTIVE}`}
          >
            {g.replace("_", " ")}
          </button>
        ))}
      </div>

      {loading ? <Spinner label="Loading spend" /> : null}
      <ErrorBanner error={error} />

      {data ? (
        <>
          <Table head={[groupBy.replace("_", " "), "Cost", "Prompt", "Completion", "Calls"]}>
            {data.groups.map((g) => (
              <tr key={g.key ?? "unattributed"}>
                <td className="px-3 py-2 font-medium text-ink">
                  {/* NULL is a real answer -- an unattributed call, or one
                      metered before the provider was recoverable. Coercing it
                      to "unknown" in SQL would hide that it is a distinct case. */}
                  {g.key ?? <span className="text-faint">unattributed</span>}
                </td>
                <td className="px-3 py-2 font-mono tabular-nums">
                  {g.priced_calls === 0 && g.calls > 0 ? (
                    // Cohere reports search units, not cost. Saying "$0" here
                    // would report a real expense as free.
                    <span className="text-muted">
                      not priced
                      <span className="ml-1 text-xs">({num(g.calls)} units)</span>
                    </span>
                  ) : (
                    usd(g.cost_usd)
                  )}
                </td>
                <td className="px-3 py-2 font-mono tabular-nums">
                  {g.prompt_tokens > 0 ? num(g.prompt_tokens) : <span className="text-faint">--</span>}
                </td>
                <td className="px-3 py-2 font-mono tabular-nums">
                  {g.completion_tokens > 0 ? num(g.completion_tokens) : <span className="text-faint">--</span>}
                </td>
                <td className="px-3 py-2 font-mono tabular-nums">{num(g.calls)}</td>
              </tr>
            ))}
          </Table>

          <section className={`${CARD} p-5`}>
            <h3 className="text-sm font-semibold text-ink">
              Daily cost, last {data.days} days
            </h3>
            {data.daily.length === 0 ? (
              <p className="mt-2 text-sm text-muted">
                Nothing recorded yet in this window.
              </p>
            ) : (
              // A plain bar list rather than a chart library. Six rectangles do
              // not justify a dependency on a static site whose entire config
              // surface is one backend URL.
              //
              // The bar used to carry `bg-emerald-950/500`. `/500` is not a  (palette-check: ignore -- quoting the old class, not using it)
              // valid opacity modifier, so Tailwind emitted no rule at all and
              // every bar on this chart rendered invisible -- a class that
              // failed silently, which is the entire argument for the token
              // layer. It is now a filled accent bar sitting in its own sunken
              // track, so the bars share a baseline and an empty day is still
              // legible as a track with nothing in it.
              <ul className="mt-3 space-y-2">
                {data.daily.map((d) => (
                  <li key={d.day} className="text-xs">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="font-mono tabular-nums text-muted">{d.day}</span>
                      <span className="font-mono tabular-nums text-muted">
                        {usd(d.cost_usd)} / {num(d.calls)} calls
                      </span>
                    </div>
                    <span className="mt-1 block h-2 w-full overflow-hidden rounded-full bg-sunken">
                      <span
                        className="block h-full rounded-full bg-accent"
                        style={{ width: peak > 0 ? `${Math.max(2, (d.cost_usd / peak) * 100)}%` : "0%" }}
                      />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}

function Evals() {
  const { data, error, loading } = useLoad<AdminEvalRun[]>(() => admin.evalRuns(), []);
  if (loading) return <Spinner label="Loading evaluations" />;
  if (error) return <ErrorBanner error={error} />;

  return (
    <div className="space-y-3">
      {/* EVAL.md's warnings travel with these numbers. The one that has already
          misled a reader of this system is repeated here rather than left in a
          document nobody has open. */}
      <p className={`${NOTICE} ${NEUTRAL_TONE}`}>
        Read these against EVAL.md. A perfect context precision/recall on a small
        corpus means &ldquo;not yet measured&rdquo;, not excellent retrieval, and
        faithfulness scores a teaching persona&rsquo;s analogies as unsupported
        claims by construction.
      </p>
      <Table head={["Agent", "Owner", "Status", "Judge", "Generator", "Scored", "When"]}>
        {(data ?? []).map((r) => (
          <tr key={r.id}>
            <td className="px-3 py-2 font-medium text-ink">{r.agent_name}</td>
            <td className="px-3 py-2 text-muted">{r.owner_email}</td>
            <td className="px-3 py-2 text-muted">{r.status}</td>
            <td className="px-3 py-2 font-mono text-xs text-muted">
              {r.judge_model ?? "--"}
            </td>
            <td className="px-3 py-2 font-mono text-xs text-muted">
              {r.generation_model ?? "--"}
            </td>
            <td className="px-3 py-2 font-mono tabular-nums">
              {num(r.scored_count)}
              {r.error_count ? (
                <span className="text-xs text-warn"> ({r.error_count} err)</span>
              ) : null}
            </td>
            <td className="px-3 py-2 text-xs text-muted">{when(r.created_at)}</td>
          </tr>
        ))}
      </Table>
    </div>
  );
}

/**
 * The trajectory rubric -- change set 16, PRD open item 23.
 *
 * The sibling of the Evaluations tab rather than a replacement: that one reports
 * whether an ANSWER was faithful to its context, this one reports whether the
 * agent did the right thing to produce it.
 *
 * Three rendering rules, each of which has shipped wrong in this file before and
 * each of which `Admin.trajectory.test.tsx` asserts in BOTH directions:
 *
 *  1. **"not measured" is not 0.** A pass rate over zero authored references is
 *     absent, not bad.
 *  2. **A binary is not a mean.** `goal_accuracy` is 1 or 0 per turn, so it
 *     renders "7 / 9". Showing `0.78` beside a faithfulness mean invites a
 *     comparison between two numbers that are not commensurable.
 *  3. **"not recorded" is not "off".** A run predating `eval_runs.tools_enabled`
 *     cannot say what tools were doing, and cannot-say is a third state.
 */
function Trajectory() {
  const { data, error, loading } = useLoad<AdminTrajectory>(
    () => admin.trajectory(30),
    [],
  );

  if (loading) return <Spinner label="Loading trajectory" />;
  if (error) return <ErrorBanner error={error} />;
  if (!data) return null;

  // Deliberately NOT `value * 100`. The underlying metric is binary per turn, so
  // the fraction it actually is remains the honest rendering.
  const rate = (ok: number, of: number) =>
    of === 0 ? "not measured" : `${num(ok)} / ${num(of)}`;

  // `value` is the mean of a set of 1s and 0s, so multiplying it back by the
  // denominator recovers the numerator. Rounded because floating-point division
  // has already happened server-side.
  const passed = (m: Measured) => Math.round((m.value ?? 0) * m.measured);

  return (
    <div className="space-y-4" data-testid="trajectory-panel">
      <p className={`${NOTICE} ${NEUTRAL_TONE}`}>
        Whether agents achieved what they were asked, and whether they used their
        tools the way the golden set says they should. Goal accuracy is judged by{" "}
        <span className="font-mono text-ink">AgentGoalAccuracyWithReference</span> and
        is binary per turn, so it is a pass rate rather than a mean -- it is not
        comparable with faithfulness. Everything else here is counted from the
        trace, not scored by a model.
      </p>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div
          className={`${CARD} p-4`}
          data-testid="trajectory-metric-card"
          data-metric="goal_accuracy"
        >
          <div className={EYEBROW}>Goal achieved</div>
          <div className="mt-1 font-mono text-2xl font-semibold tabular-nums text-ink">
            {data.goal_accuracy.measured === 0 ? (
              <span data-testid="trajectory-unmeasured">not measured</span>
            ) : (
              rate(passed(data.goal_accuracy), data.goal_accuracy.measured)
            )}
          </div>
          <div className="mt-1 text-xs text-muted">
            {data.goal_accuracy.measured === 0
              ? "no turn carried a reference answer to judge against"
              : `${num(data.goal_accuracy.measured)} of ${num(data.goal_accuracy.total)} turns judged`}
          </div>
        </div>

        <div
          className={`${CARD} p-4`}
          data-testid="trajectory-metric-card"
          data-metric="tool_use_ok"
        >
          <div className={EYEBROW}>Tool use as expected</div>
          <div className="mt-1 font-mono text-2xl font-semibold tabular-nums text-ink">
            {data.tool_use_ok.measured === 0
              ? "not measured"
              : rate(passed(data.tool_use_ok), data.tool_use_ok.measured)}
          </div>
          <div className="mt-1 text-xs text-muted">
            {data.tool_use_ok.measured === 0
              ? "no golden question set expected_tool_use"
              : `${num(data.tool_use_ok.measured)} of ${num(data.tool_use_ok.total)} turns graded`}
          </div>
        </div>

        <MeasuredTile
          label="Calls per step"
          m={data.calls_per_step}
          format={(v) => (v === null ? "--" : v.toFixed(2))}
        />

        <Tile
          label="Forced by gap trigger"
          value={num(data.gap_forced)}
          hint={`${num(data.searched)} turns searched, ${num(data.budget_exhausted)} hit the step budget`}
        />
      </div>

      <section className={`${CARD} p-5`}>
        <h3 className="text-sm font-semibold text-ink">Runs by tool configuration</h3>
        <p className="mt-1 text-xs text-muted">
          Two runs with tools toggled between them are not comparable, and until
          change set 16 nothing recorded which was which.{" "}
          <span className="font-medium text-ink">Not recorded</span> means the run
          predates the column; it does not mean tools were off.
        </p>
        {/*
          Three pills, three tones, and the third must stay visually distinct
          from the second: "not recorded" is a run that cannot say, which is a
          different fact from a run that said "off". Collapsing them is the
          defect this panel exists to prevent, so a neutral pill and a warn pill
          carry the two rather than one class carrying both.
        */}
        <div className="mt-3 flex flex-wrap gap-2">
          <span className={`${PILL} ${OK_TONE}`}>
            tools on: {num(data.run_config.tools_on)}
          </span>
          <span className={PILL_NEUTRAL}>
            tools off: {num(data.run_config.tools_off)}
          </span>
          <span className={`${PILL} ${WARN_TONE}`} data-testid="trajectory-not-recorded">
            not recorded: {num(data.run_config.not_recorded)}
          </span>
        </div>
      </section>

      {data.agents.length === 0 ? (
        <p className="text-sm text-muted">
          Nothing recorded yet in this window. Run an evaluation on an agent that
          has tools enabled.
        </p>
      ) : (
        <Table
          head={[
            "Agent",
            "Owner",
            "Turns",
            "Goal achieved",
            "Tool use",
            "Searched",
            "Gap-forced",
          ]}
        >
          {data.agents.map((a) => (
            <tr key={a.agent_name} data-testid="trajectory-row">
              <td className="px-3 py-2 font-medium text-ink">{a.agent_name}</td>
              <td className="px-3 py-2 text-xs text-muted">{a.owner_email}</td>
              <td className="px-3 py-2 font-mono tabular-nums">{num(a.turns)}</td>
              <td className="px-3 py-2 font-mono tabular-nums">
                {rate(a.goal_ok, a.goal_measured)}
              </td>
              <td className="px-3 py-2 font-mono tabular-nums">
                {rate(a.tool_ok, a.tool_measured)}
              </td>
              <td className="px-3 py-2 font-mono tabular-nums">{num(a.searched)}</td>
              <td className="px-3 py-2 font-mono tabular-nums">{num(a.gap_forced)}</td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}

function Audit() {
  const { data, error, loading } = useLoad<AdminAuditEntry[]>(() => admin.audit(200), []);
  if (loading) return <Spinner label="Loading audit log" />;
  if (error) return <ErrorBanner error={error} />;

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">
        Includes this console&rsquo;s own transcript reads. An admin surface that
        logs everyone else and not itself is a surveillance tool, not an
        accountability one.
      </p>
      <Table head={["When", "Actor", "Action", "Resource", "Detail"]}>
        {(data ?? []).map((e) => (
          <tr key={e.id}>
            <td className="px-3 py-2 text-xs text-muted">{when(e.created_at)}</td>
            <td className="px-3 py-2 text-muted">{e.actor_email ?? "system"}</td>
            <td className="px-3 py-2 font-mono text-xs font-medium text-ink">
              {e.action}
            </td>
            <td className="px-3 py-2 font-mono text-xs text-muted">
              {e.resource_type}
              {e.resource_id ? ` ${e.resource_id.slice(0, 8)}` : ""}
            </td>
            <td className="px-3 py-2 font-mono text-xs text-muted">
              {e.metadata ? JSON.stringify(e.metadata) : ""}
            </td>
          </tr>
        ))}
      </Table>
    </div>
  );
}

// --------------------------------------------------------------------------
// The view
// --------------------------------------------------------------------------

export default function Admin({ onBack }: { onBack: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 p-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink sm:text-3xl">
            Admin
          </h1>
          <p className="mt-1 text-sm text-muted">
            Everything, across every user. Reads of other people&rsquo;s
            transcripts are recorded.
          </p>
        </div>
        <button type="button" onClick={onBack} className={BTN_QUIET}>
          Back to agents
        </button>
      </header>

      {/* The shared tab treatment. This row used to be an emerald underline --
          the fourth of five looks for "this one is selected" in one product. */}
      <nav className="flex flex-wrap gap-1 border-b border-line pb-2" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`${TAB} ${tab === t.id ? TAB_ACTIVE : TAB_INACTIVE}`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "overview" ? <Overview /> : null}
      {tab === "users" ? <Users /> : null}
      {tab === "agents" ? <Agents /> : null}
      {tab === "conversations" ? <Conversations /> : null}
      {tab === "spend" ? <SpendTab /> : null}
      {tab === "evals" ? <Evals /> : null}
      {tab === "trajectory" ? <Trajectory /> : null}
      {tab === "audit" ? <Audit /> : null}
    </div>
  );
}
