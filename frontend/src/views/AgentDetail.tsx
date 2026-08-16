/**
 * Agent detail: a workspace shell, not a page.
 *
 * The agent record lives here rather than in each view because the corpus
 * changes it -- uploading or deleting moves `document_count` and `status` -- and
 * a count that disagrees with the list underneath it is the kind of small
 * wrongness that makes a demo look unfinished. One owner, one refetch,
 * `onCorpusChanged`.
 *
 * ---
 *
 * **What changed, and the measurement that forced it.**
 *
 * This file used to render a document-flow header above a measured panel: back
 * link, icon, name, badges, persona line, description, parameter summary,
 * pedagogy note, and two `<Reveal>` panels holding the retrieval parameters and
 * the system prompt. Measured in Playwright at 1440x900:
 *
 *     disclosures closed   chrome 576 px   64.0% of the viewport   chat 324 px
 *     disclosures OPEN     chrome 1092 px  121.3%                  chat  24 px
 *
 * With both open the chat panel held **zero pixels of visible thread**, because
 * the panel is sized as the complement of the chrome and the complement of 1092
 * in a 900px viewport is negative. And the interaction that triggers it is the
 * one this workshop is built around -- "change chunk_size, watch the answer
 * change" begins by opening `Retrieval parameters`, which deleted the answer.
 *
 * Nothing threw. No console error, no failed request, no horizontal overflow,
 * no React warning. The page rendered perfectly and the product was gone. That
 * is `new features/loop.md` T2 arriving in a module that has never seen a tool:
 * **the error-shaped check passes while the outcome you wanted is absent**, and
 * the assertion that catches it is not "did anything throw?" but "is the thread
 * taller than zero?" -- which is now `A2` in `scripts/ui_check.py`.
 *
 * A second finding worth keeping, because it is the reverse of how the
 * responsive work was framed: **desktop was worse than mobile.** The old
 * `compactHeader` collapsed the reference material with `hidden sm:block`, so
 * the fix applied only below 640px. 576 px of chrome at 1440, 289 px at 390.
 *
 * **So the fix is structural rather than a shorter header.** The chrome is now
 * `AgentBar`, a single flex row that cannot grow, and everything that used to
 * expand lives behind `[...]` in a sheet -- where opening it costs the workspace
 * nothing, because an overlay has no height in the flow. Chrome at 1440 is
 * 576 -> ~125 px and the workspace is 324 -> ~775 px, and neither number now
 * depends on what is open.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { agents } from "../lib/api.ts";
import type { Agent } from "../lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import AgentBar from "../components/AgentBar.tsx";
import type { ViewId } from "../components/AgentBar.tsx";
import AgentSettingsSheet from "../components/AgentSettingsSheet.tsx";
import EmptyAgentWorkspace from "../components/EmptyAgentWorkspace.tsx";
import AgentChat from "./AgentChat.tsx";
import AgentDocuments from "./AgentDocuments.tsx";
import AgentEvaluate from "./AgentEvaluate.tsx";

/**
 * The shortest the workspace may be, in px.
 *
 * A floor on a measurement that had none is the whole of the bug above. The bar
 * is one row and cannot realistically push the shell's top past the fold, so in
 * practice this never binds -- which is exactly why it is worth having: a guard
 * that costs one expression against a failure whose only symptom is a product
 * that silently is not there.
 */
const MIN_WORKSPACE_PX = 320;

export default function AgentDetail({
  agentId,
  onBack,
}: {
  agentId: string;
  onBack: () => void;
}) {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewId>("workspace");
  const [settingsOpen, setSettingsOpen] = useState(false);

  /** The `[...]` button, so the sheet returns focus to it on close. Held here
   *  because the button and the sheet are siblings rather than parent and
   *  child -- `Drawer` restores focus to whatever was active when it opened,
   *  and this ref is what makes that provably the right element. */
  const settingsRef = useRef<HTMLButtonElement>(null);

  const shellRef = useRef<HTMLDivElement>(null);
  const [shellHeight, setShellHeight] = useState<string | null>(null);

  /**
   * The shell's height, measured rather than guessed -- and now measured
   * somewhere safe.
   *
   * The mechanism is unchanged and still right: `rect.top + scrollY` is the
   * distance from the top of the DOCUMENT, which is where the viewport's top
   * edge sits at `scrollTop = 0`, so `100dvh` minus it is exactly the room left.
   * The `dvh` unit stays in the calc rather than being resolved here because a
   * mobile browser collapsing its URL bar changes it continuously and CSS tracks
   * that for free.
   *
   * What changed is WHAT is measured. This ref is on the shell, which sits
   * directly under the sticky nav, instead of on a panel sitting under 500 px of
   * header. The only thing above it is the nav, and the nav's height is a fact
   * about the window rather than about this agent -- so the measurement no
   * longer moves when a description is long, when a name wraps, or when a
   * disclosure is opened.
   *
   * A constant offset would still be wrong, which is why this is not simply
   * `calc(100dvh - 69px)`: the nav wraps to two rows at 320px. Measuring costs
   * one `getBoundingClientRect` and is right in both cases.
   *
   * No `ResizeObserver` any more. It existed to catch the header growing when a
   * `<Reveal>` was toggled, and there is no longer a header that grows -- only
   * the nav above, which changes on resize, which `resize` already covers.
   */
  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;

    const measure = () => {
      const top = shell.getBoundingClientRect().top + window.scrollY;
      setShellHeight(`max(${MIN_WORKSPACE_PX}px, calc(100dvh - ${Math.round(top)}px))`);
    };

    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [agent]);

  const loadAgent = useCallback(async () => {
    try {
      setAgent(await agents.get(agentId));
    } catch (cause) {
      // 403 means authenticated-but-not-owner and 404 means gone. Both are
      // shown as-is: the tenancy boundary refusing is worth seeing, not
      // smoothing over into "not found".
      setError(errorMessage(cause));
    }
  }, [agentId]);

  /**
   * Stable across renders, and that stability is load-bearing rather than an
   * optimisation: `useAgentDocuments` polls on a backoff timer whose effect
   * lists this callback as a dependency, so a fresh arrow function each render
   * would tear the timer down and restart it at the shortest interval every
   * time the agent refetched -- a backoff that never backs off.
   */
  const handleCorpusChanged = useCallback(() => {
    void loadAgent();
  }, [loadAgent]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadAgent().finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [loadAgent]);

  if (loading) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <Spinner label="Loading agent" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="mx-auto max-w-6xl space-y-4 px-6 py-10">
        <ErrorBanner error={error ?? "Agent not found."} />
        <button
          type="button"
          onClick={onBack}
          // `min-h-11`: this is the ONLY control on the page when an agent fails
          // to load -- the one screen where a missed tap leaves the user with no
          // way forward at all.
          className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600"
        >
          Back to agents
        </button>
      </div>
    );
  }

  return (
    <div
      ref={shellRef}
      data-testid="agent-shell"
      style={shellHeight ? { height: shellHeight } : undefined}
      // `min-h-0` on the shell and on the workspace below it is what lets the
      // height resolve at all: a flex item's default `min-height: auto` refuses
      // to shrink below its content, so without it a long thread would push the
      // composer out of a box that is otherwise exactly the right size.
      //
      // A null height is a safe fallback rather than a broken one -- the shell
      // grows to its content and the page scrolls, which is what every other
      // view in this app does.
      className="flex min-h-0 flex-col"
    >
      <AgentBar
        agent={agent}
        view={view}
        onView={setView}
        onBack={onBack}
        onOpenSettings={() => setSettingsOpen(true)}
        settingsRef={settingsRef}
      />

      {error && (
        <div className="shrink-0 px-3 pt-3 sm:px-6">
          <ErrorBanner error={error} />
        </div>
      )}

      {/*
        `mx-auto w-full max-w-[100rem]` rather than the old `max-w-6xl`.

        Every view in this app was capped at 1152px, which on a 1440px monitor
        left 144px of gutter on each side and on a 1920px monitor left a third of
        the screen empty. That cap is defensible for a dashboard of cards and
        indefensible for a workspace holding a source list, a conversation and a
        handout dock. 1600px is where a 17rem rail, a readable thread and the
        dock all have room without the thread becoming a line of text too wide to
        track back to the start of.
      */}
      <div className="mx-auto flex min-h-0 w-full max-w-[100rem] flex-1 flex-col px-3 py-3 sm:px-6 sm:py-4">
        {/*
          Mounted on demand and keyed on the agent, for two different reasons.

          On demand: `AgentEvaluate` polls a run on an interval and `AgentChat`
          holds a conversation, and neither should be live while the other is on
          screen.

          Keyed: switching agents while an eval run is in flight must start a
          fresh component rather than let the old poll write a scorecard into the
          new agent's view.
        */}
        {view === "workspace" &&
          (agent.document_count === 0 ? (
            <EmptyAgentWorkspace onAddSource={() => setView("sources")} />
          ) : (
            <AgentChat
              key={agent.id}
              agentId={agent.id}
              // Handed down rather than fetched: this component already owns
              // the agent record, and the composer's `@mention` popup needs the
              // roster to filter without a round trip. Null on every classic
              // agent, which switches the popup off entirely.
              specialists={agent.specialists}
              onCorpusChanged={handleCorpusChanged}
              initialRailTab="threads"
            />
          ))}

        {/*
          The two document-shaped views get their own scroll container, which
          they did not need before: the shell around them now has a definite
          height, so a long page inside it must scroll itself rather than
          scrolling the document.

          Sources keeps a wider cap than Evaluate because its table has six
          columns and used to render them in 1152px with 288px of gutter beside
          it; a scorecard and a golden-set editor genuinely do not read better
          wider, so those stay at `max-w-6xl`.
        */}
        {view === "sources" && (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto min-w-0 max-w-[80rem]">
              <AgentDocuments agentId={agent.id} onCorpusChanged={handleCorpusChanged} />
            </div>
          </div>
        )}

        {view === "evaluate" && (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto min-w-0 max-w-6xl">
              <AgentEvaluate key={agent.id} agent={agent} />
            </div>
          </div>
        )}
      </div>

      <AgentSettingsSheet
        agent={agent}
        open={settingsOpen}
        onClose={() => {
          setSettingsOpen(false);
          // Returned explicitly as well as by the Drawer's own restore. The
          // Drawer restores whatever was focused when it opened, which is
          // normally this button -- but the sheet can also be closed by the
          // backdrop after focus has moved elsewhere, and landing focus back on
          // the control that opened the surface is the behaviour the pattern
          // promises in every case, not most.
          settingsRef.current?.focus();
        }}
        // The PATCH response is a full agent record, the same shape GET returns,
        // so the bar and the sheet both update without a refetch.
        onSaved={setAgent}
      />
    </div>
  );
}
