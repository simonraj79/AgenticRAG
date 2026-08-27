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
    <div
      className={`rounded-lg border border-slate-800 bg-slate-900 p-4 ${
        wide ? "sm:col-span-2" : ""
      }`}
    >
      <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
        {value}
      </div>
      {hint ? <div className="mt-1 text-xs text-slate-400">{hint}</div> : null}
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
      <div className="font-medium text-slate-100">{cost(spend)}</div>
      <div className="text-xs text-slate-400">
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
    <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-900">
      <table className="min-w-full text-sm">
        <thead className="border-b border-slate-800 bg-slate-900/60">
          <tr>
            {head.map((h) => (
              <th
                key={h}
                className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-400"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">{children}</tbody>
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
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
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

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
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

      <section className="rounded-lg border border-slate-800 bg-slate-900 p-4">
        <h3 className="text-sm font-semibold text-slate-100">
          Reconciliation against OpenRouter
        </h3>
        <p className="mt-1 text-xs text-slate-400">{account.note}</p>
        {account.openrouter.ok ? (
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
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
          <p className="mt-3 rounded border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-200">
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
            <div className="font-medium text-slate-100">{u.email}</div>
            <div className="text-xs text-slate-400">
              {u.name ?? "no name"}
              {/* Surfaced because this database holds two rows for two real
                  people -- the dev shim keys on `dev|<email>`, so signing in
                  for real creates a second row. "15 users" is not 15 people. */}
              {u.is_dev_identity ? " -- dev-login identity" : ""}
            </div>
          </td>
          <td className="px-3 py-2">
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                u.role === "admin"
                  ? "bg-emerald-900/40 text-emerald-300"
                  : "bg-slate-800 text-slate-400"
              }`}
            >
              {u.role}
            </span>
          </td>
          <td className="px-3 py-2 tabular-nums">{num(u.agents)}</td>
          <td className="px-3 py-2 tabular-nums">{num(u.conversations)}</td>
          <td className="px-3 py-2 tabular-nums">{num(u.queries)}</td>
          <td className="px-3 py-2">
            <SpendCell spend={u.spend} />
          </td>
          <td className="px-3 py-2 text-xs text-slate-400">{when(u.last_login_at)}</td>
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
          <td className="px-3 py-2 font-medium text-slate-100">{a.name}</td>
          <td className="px-3 py-2 text-slate-400">{a.owner_email}</td>
          <td className="px-3 py-2 tabular-nums">{num(a.documents)}</td>
          <td className="px-3 py-2 tabular-nums">{num(a.conversations)}</td>
          <td className="px-3 py-2 tabular-nums">{num(a.queries)}</td>
          <td className="px-3 py-2 tabular-nums">{num(a.eval_runs)}</td>
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
          className="min-h-11 rounded border border-slate-700 px-3 text-sm font-medium hover:bg-slate-800"
        >
          Back to conversations
        </button>
        <header className="rounded-lg border border-slate-800 bg-slate-900 p-4">
          <h3 className="font-semibold text-slate-100">{open.title ?? "Untitled"}</h3>
          <p className="text-sm text-slate-400">
            {open.user_email} &middot; {open.agent_name} &middot; {when(open.created_at)}
          </p>
          <p className="mt-2 text-xs text-slate-400">
            Thread cost {usd(open.spend.cost_usd)} over {num(open.spend.calls)} model
            calls. This read was recorded in the audit log.
          </p>
        </header>
        <ol className="space-y-3">
          {open.turns.map((t) => (
            <li key={t.id} className="rounded-lg border border-slate-800 bg-slate-900 p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <p className="font-medium text-slate-100">{t.question}</p>
                <span className="text-xs tabular-nums text-slate-400">
                  {/* "not measured" and "$0" are different facts. */}
                  {t.measured ? usd(t.cost_usd) : "not measured"}
                  {t.latency_ms != null ? ` -- ${(t.latency_ms / 1000).toFixed(1)}s` : ""}
                  {t.refused ? " -- refused" : ""}
                </span>
              </div>
              <p className="mt-2 whitespace-pre-wrap text-sm text-slate-300">
                {t.answer ?? "(no answer recorded)"}
              </p>
              <p className="mt-2 text-xs text-slate-500">
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
      <p className="text-xs text-slate-400">
        Opening a thread shows another person&rsquo;s questions and answers, and
        records that you read it.
      </p>
      <Table head={["Thread", "User", "Agent", "Turns", "Spend", "Updated", ""]}>
        {(data ?? []).map((c) => (
          <tr key={c.id}>
            <td className="px-3 py-2 font-medium text-slate-100">
              {c.title ?? "Untitled"}
            </td>
            <td className="px-3 py-2 text-slate-400">{c.user_email}</td>
            <td className="px-3 py-2 text-slate-400">{c.agent_name}</td>
            <td className="px-3 py-2 tabular-nums">
              {num(c.turns)}
              {c.refusals > 0 ? (
                <span className="text-xs text-slate-400"> ({c.refusals} refused)</span>
              ) : null}
            </td>
            <td className="px-3 py-2">
              <SpendCell spend={c.spend} />
            </td>
            <td className="px-3 py-2 text-xs text-slate-400">{when(c.updated_at)}</td>
            <td className="px-3 py-2">
              <button
                type="button"
                onClick={() => read(c.id)}
                disabled={opening === c.id}
                className="min-h-11 rounded border border-slate-700 px-3 text-sm font-medium hover:bg-slate-800 disabled:opacity-50"
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

  const peak = Math.max(1, ...(data?.daily ?? []).map((d) => d.cost_usd));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {GROUPS.map((g) => (
          <button
            key={g}
            type="button"
            onClick={() => setGroupBy(g)}
            className={`min-h-11 rounded border px-3 text-sm font-medium ${
              groupBy === g
                ? "border-emerald-500 bg-emerald-950/50 text-emerald-200"
                : "border-slate-700 hover:bg-slate-800"
            }`}
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
                <td className="px-3 py-2 font-medium text-slate-100">
                  {/* NULL is a real answer -- an unattributed call, or one
                      metered before the provider was recoverable. Coercing it
                      to "unknown" in SQL would hide that it is a distinct case. */}
                  {g.key ?? <span className="text-slate-500">unattributed</span>}
                </td>
                <td className="px-3 py-2 tabular-nums">
                  {g.priced_calls === 0 && g.calls > 0 ? (
                    // Cohere reports search units, not cost. Saying "$0" here
                    // would report a real expense as free.
                    <span className="text-slate-400">
                      not priced
                      <span className="ml-1 text-xs">({num(g.calls)} units)</span>
                    </span>
                  ) : (
                    usd(g.cost_usd)
                  )}
                </td>
                <td className="px-3 py-2 tabular-nums">
                  {g.prompt_tokens > 0 ? num(g.prompt_tokens) : <span className="text-slate-500">--</span>}
                </td>
                <td className="px-3 py-2 tabular-nums">
                  {g.completion_tokens > 0 ? num(g.completion_tokens) : <span className="text-slate-500">--</span>}
                </td>
                <td className="px-3 py-2 tabular-nums">{num(g.calls)}</td>
              </tr>
            ))}
          </Table>

          <section className="rounded-lg border border-slate-800 bg-slate-900 p-4">
            <h3 className="text-sm font-semibold text-slate-100">
              Daily cost, last {data.days} days
            </h3>
            {data.daily.length === 0 ? (
              <p className="mt-2 text-sm text-slate-400">
                Nothing recorded yet in this window.
              </p>
            ) : (
              // A plain bar list rather than a chart library. Six rectangles do
              // not justify a dependency on a static site whose entire config
              // surface is one backend URL.
              <ul className="mt-3 space-y-1">
                {data.daily.map((d) => (
                  <li key={d.day} className="flex items-center gap-2 text-xs">
                    <span className="w-24 shrink-0 tabular-nums text-slate-400">
                      {d.day}
                    </span>
                    <span
                      className="h-3 rounded-sm bg-emerald-950/500"
                      style={{ width: `${Math.max(2, (d.cost_usd / peak) * 100)}%` }}
                    />
                    <span className="tabular-nums text-slate-400">
                      {usd(d.cost_usd)} / {num(d.calls)} calls
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
      <p className="rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
        Read these against EVAL.md. A perfect context precision/recall on a small
        corpus means &ldquo;not yet measured&rdquo;, not excellent retrieval, and
        faithfulness scores a teaching persona&rsquo;s analogies as unsupported
        claims by construction.
      </p>
      <Table head={["Agent", "Owner", "Status", "Judge", "Generator", "Scored", "When"]}>
        {(data ?? []).map((r) => (
          <tr key={r.id}>
            <td className="px-3 py-2 font-medium text-slate-100">{r.agent_name}</td>
            <td className="px-3 py-2 text-slate-400">{r.owner_email}</td>
            <td className="px-3 py-2">{r.status}</td>
            <td className="px-3 py-2 text-xs text-slate-400">{r.judge_model ?? "--"}</td>
            <td className="px-3 py-2 text-xs text-slate-400">
              {r.generation_model ?? "--"}
            </td>
            <td className="px-3 py-2 tabular-nums">
              {num(r.scored_count)}
              {r.error_count ? (
                <span className="text-xs text-amber-300"> ({r.error_count} err)</span>
              ) : null}
            </td>
            <td className="px-3 py-2 text-xs text-slate-400">{when(r.created_at)}</td>
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
      <p className="rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
        Whether agents achieved what they were asked, and whether they used their
        tools the way the golden set says they should. Goal accuracy is judged by{" "}
        <span className="text-slate-300">AgentGoalAccuracyWithReference</span> and
        is binary per turn, so it is a pass rate rather than a mean -- it is not
        comparable with faithfulness. Everything else here is counted from the
        trace, not scored by a model.
      </p>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div
          className="rounded-lg border border-slate-800 bg-slate-900 p-4"
          data-testid="trajectory-metric-card"
          data-metric="goal_accuracy"
        >
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
            Goal achieved
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
            {data.goal_accuracy.measured === 0 ? (
              <span data-testid="trajectory-unmeasured">not measured</span>
            ) : (
              rate(passed(data.goal_accuracy), data.goal_accuracy.measured)
            )}
          </div>
          <div className="mt-1 text-xs text-slate-400">
            {data.goal_accuracy.measured === 0
              ? "no turn carried a reference answer to judge against"
              : `${num(data.goal_accuracy.measured)} of ${num(data.goal_accuracy.total)} turns judged`}
          </div>
        </div>

        <div
          className="rounded-lg border border-slate-800 bg-slate-900 p-4"
          data-testid="trajectory-metric-card"
          data-metric="tool_use_ok"
        >
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
            Tool use as expected
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
            {data.tool_use_ok.measured === 0
              ? "not measured"
              : rate(passed(data.tool_use_ok), data.tool_use_ok.measured)}
          </div>
          <div className="mt-1 text-xs text-slate-400">
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

      <section className="rounded-lg border border-slate-800 bg-slate-900 p-4">
        <h3 className="text-sm font-semibold text-slate-100">
          Runs by tool configuration
        </h3>
        <p className="mt-1 text-xs text-slate-400">
          Two runs with tools toggled between them are not comparable, and until
          change set 16 nothing recorded which was which.{" "}
          <span className="text-slate-300">Not recorded</span> means the run
          predates the column; it does not mean tools were off.
        </p>
        <div className="mt-3 flex flex-wrap gap-2 text-sm">
          <span className="rounded bg-emerald-900/40 px-2 py-0.5 text-emerald-300">
            tools on: {num(data.run_config.tools_on)}
          </span>
          <span className="rounded bg-slate-800 px-2 py-0.5 text-slate-300">
            tools off: {num(data.run_config.tools_off)}
          </span>
          <span
            className="rounded bg-amber-950/40 px-2 py-0.5 text-amber-200"
            data-testid="trajectory-not-recorded"
          >
            not recorded: {num(data.run_config.not_recorded)}
          </span>
        </div>
      </section>

      {data.agents.length === 0 ? (
        <p className="text-sm text-slate-400">
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
              <td className="px-3 py-2 text-slate-100">{a.agent_name}</td>
              <td className="px-3 py-2 text-xs text-slate-400">{a.owner_email}</td>
              <td className="px-3 py-2 tabular-nums text-slate-300">{num(a.turns)}</td>
              <td className="px-3 py-2 tabular-nums text-slate-300">
                {rate(a.goal_ok, a.goal_measured)}
              </td>
              <td className="px-3 py-2 tabular-nums text-slate-300">
                {rate(a.tool_ok, a.tool_measured)}
              </td>
              <td className="px-3 py-2 tabular-nums text-slate-300">
                {num(a.searched)}
              </td>
              <td className="px-3 py-2 tabular-nums text-slate-300">
                {num(a.gap_forced)}
              </td>
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
      <p className="text-xs text-slate-400">
        Includes this console&rsquo;s own transcript reads. An admin surface that
        logs everyone else and not itself is a surveillance tool, not an
        accountability one.
      </p>
      <Table head={["When", "Actor", "Action", "Resource", "Detail"]}>
        {(data ?? []).map((e) => (
          <tr key={e.id}>
            <td className="px-3 py-2 text-xs text-slate-400">{when(e.created_at)}</td>
            <td className="px-3 py-2 text-slate-300">{e.actor_email ?? "system"}</td>
            <td className="px-3 py-2 text-xs font-medium text-slate-100">{e.action}</td>
            <td className="px-3 py-2 text-xs text-slate-400">
              {e.resource_type}
              {e.resource_id ? ` ${e.resource_id.slice(0, 8)}` : ""}
            </td>
            <td className="px-3 py-2 text-xs text-slate-400">
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
    <div className="mx-auto w-full max-w-6xl space-y-5 p-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Admin</h1>
          <p className="text-sm text-slate-400">
            Everything, across every user. Reads of other people&rsquo;s
            transcripts are recorded.
          </p>
        </div>
        <button
          type="button"
          onClick={onBack}
          className="min-h-11 rounded border border-slate-700 px-3 text-sm font-medium hover:bg-slate-800"
        >
          Back to agents
        </button>
      </header>

      <nav className="flex flex-wrap gap-1 border-b border-slate-800" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`min-h-11 px-3 text-sm font-medium ${
              tab === t.id
                ? "border-b-2 border-emerald-500 text-emerald-200"
                : "text-slate-400 hover:text-slate-100"
            }`}
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
