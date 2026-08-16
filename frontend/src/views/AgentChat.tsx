/**
 * Chat: a real thread against one agent, with its conversations beside it.
 *
 * This replaces a single-shot Ask form whose failure was structural rather than
 * cosmetic -- asking a second question destroyed the first answer, so there was
 * nothing for "what about the second one?" to refer to and no way to see that
 * the agent had understood the reference. History-aware retrieval is only
 * demonstrable if the history is on screen.
 *
 * **A new chat is a draft, not a row.** "New chat" clears the thread and does
 * not touch the server; the conversation is created by the first question,
 * through the one-shot `POST /api/agents/{id}/ask` that returns the
 * `conversation_id` it created. Creating the row eagerly would litter the list
 * with empty untitled threads every time someone clicked the button and thought
 * better of it -- and there is nothing to name a thread after until its first
 * question exists.
 *
 * **Requests are addressed to a thread, and discarded if the user has left
 * it.** A turn takes 10-15 seconds, which is long enough to switch
 * conversations mid-flight. The answer is still recorded server-side; what must
 * not happen is it being appended to whatever thread happens to be on screen
 * when it lands, which would put an answer under a question it does not belong
 * to.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { chat } from "../lib/api.ts";
import type { AskResult, ChatMessage, Conversation, Handout } from "../lib/types.ts";
import { ConfirmDeleteButton, ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import Message from "../components/Message.tsx";
import HandoutDock from "../components/HandoutDock.tsx";
import HandoutsPanel from "../components/HandoutsPanel.tsx";
import SourceRail from "../components/SourceRail.tsx";

/**
 * What the rail is showing. The rail is ONE column whose content switches,
 * which is the structural difference from the layout this replaces.
 *
 * The old chat tab was `xl:grid-cols-[15rem_minmax(0,1fr)_22rem]` -- a narrow
 * fixed left rail, an elastic middle, a wider fixed right rail. That is
 * NotebookLM's Sources | Chat | Studio at almost exactly its proportions, and it
 * was the strongest single signal in the codebase. Two tracks with a switchable
 * first one is a different idea rather than the same one recoloured: NotebookLM
 * shows Sources and Studio simultaneously and permanently, because its sources
 * carry per-source checkboxes that scope the next question. Nothing here scopes
 * anything -- an agent retrieves over its whole namespace, always -- so a
 * permanent panel would be spending a third of the screen on a filter that does
 * not exist.
 *
 * `sources` is the default because it answers the question a first-time visitor
 * has, which is what this agent knows. It is also the tab that makes the empty
 * corpus visible, and an empty corpus is why a brand-new agent refuses
 * everything.
 */
type RailTab = "sources" | "threads";

export default function AgentChat({
  agentId,
  onCorpusChanged,
  initialRailTab = "sources",
  onAnswered,
}: {
  agentId: string;
  /** Uploading or deleting a source moves the agent's `document_count` and
   *  `status`, which the bar above renders. Handed up so one owner refetches,
   *  exactly as the Documents tab did before the corpus moved into the rail. */
  onCorpusChanged?: () => void;
  /** Which rail tab to open on. Decided by the owner because it depends on the
   *  agent record -- an empty corpus is the thing that has to be fixed first, so
   *  it opens on Sources; otherwise Threads is the more useful landing and the
   *  sources are one tap away. Read once, as an initial value: recomputing it
   *  would swap the rail out from under a user the moment their first upload
   *  moved `document_count` off zero, which is precisely while they are watching
   *  that upload. */
  initialRailTab?: RailTab;
  /** Optional: hands each new `query_id` up, for a shell that still keeps a
   *  separate trace view. The trace is available inline on every message, so
   *  nothing here depends on anybody listening. */
  onAnswered?: (queryId: string) => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  /** `null` means the unsaved draft -- see the header note. */
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<{ question: string; threadId: string | null } | null>(
    null,
  );
  const [elapsed, setElapsed] = useState(0);
  const [loadingThread, setLoadingThread] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  /**
   * The Handouts dock, at every width. There is no longer a docked third column
   * to make this meaningless at `xl` -- the dock IS the surface now, and it is
   * the same one on a phone and on a 1920px monitor.
   *
   * **Nothing in this file ever sets it to `true`.** That is a constraint, not
   * an oversight, and it protects the rename input three hundred lines below:
   * that input cancels on blur by design, so any code path that moves focus
   * discards a rename in progress. A user CLICKING the toggle is an ordinary
   * click-away and cancels the rename exactly like clicking a conversation
   * does -- deliberate, visible, and the behaviour they already expect. An
   * effect opening the dock on their behalf would be the same blur with nobody
   * having asked for it, which is the silent discard worth avoiding. So when a
   * turn produces a handout the count increments and the dock stays shut.
   */
  const [dockOpen, setDockOpen] = useState(false);

  /** Which of the two things the rail is showing. See `RailTab`. */
  const [railTab, setRailTab] = useState<RailTab>(initialRailTab);
  const [handoutCount, setHandoutCount] = useState(0);
  /**
   * Handouts from the turn that just landed, handed to the panel so it can
   * prepend them without waiting for its three-second poll.
   *
   * Held in state rather than passed straight through because the panel takes
   * it as an effect dependency: a fresh `[]` on every render would re-run that
   * effect on every keystroke in the composer.
   */
  const [turnHandouts, setTurnHandouts] = useState<Handout[]>([]);

  // Stable identity, because HandoutsPanel lists it as an effect dependency --
  // a fresh arrow function each render would call it on every render instead of
  // on every change to the count.
  const handleCountChange = useCallback((count: number) => setHandoutCount(count), []);

  const bottom = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLTextAreaElement | null>(null);
  // Read by async handlers that need to know where the user is NOW, which the
  // closure's captured `activeId` cannot tell them -- that is the value at the
  // moment the request was sent, and comparing the two is the whole check.
  const activeIdNow = useRef<string | null>(null);
  const requestController = useRef<AbortController | null>(null);
  useEffect(() => {
    activeIdNow.current = activeId;
  }, [activeId]);

  useEffect(() => () => requestController.current?.abort(), []);

  const refreshList = useCallback(async () => {
    setConversations(await chat.list(agentId));
  }, [agentId]);

  // Open the most recently active thread on arrival. Landing on an empty
  // composer when there is history sitting one click away makes the agent look
  // like it has forgotten everything.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const rows = await chat.list(agentId);
        if (cancelled) return;
        setConversations(rows);

        const latest = rows[0];
        if (!latest) return;

        const detail = await chat.load(latest.id);
        if (cancelled) return;
        setActiveId(detail.id);
        setMessages(detail.messages);
      } catch (cause) {
        if (!cancelled) setError(errorMessage(cause));
      } finally {
        if (!cancelled) setLoadingThread(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [agentId]);

  // Elapsed seconds while a turn is in flight. A quarter-second tick rather
  // than a full second so "0s" is not on screen for a whole second at the
  // start, which reads as a frozen counter.
  useEffect(() => {
    if (!pending) {
      setElapsed(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      250,
    );
    return () => window.clearInterval(timer);
  }, [pending]);

  // `block: "nearest"` keeps the scroll inside the thread pane. `"end"` would
  // also scroll the window to bring the pane into view, yanking the page around
  // every time an answer arrives.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages.length, pending, activeId]);

  async function openConversation(id: string) {
    if (id === activeId) return;
    setError(null);
    setLoadingThread(true);
    setActiveId(id);
    activeIdNow.current = id;
    setMessages([]);
    setHistoryOpen(false);
    try {
      const detail = await chat.load(id);
      if (activeIdNow.current !== id) return;
      setMessages(detail.messages);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      if (activeIdNow.current === id) setLoadingThread(false);
    }
  }

  function startDraft() {
    setError(null);
    setActiveId(null);
    activeIdNow.current = null;
    setMessages([]);
    setLoadingThread(false);
    setHistoryOpen(false);
    input.current?.focus();
  }

  async function send() {
    const text = question.trim();
    // `loadingThread` guards a real race: on arrival the composer is usable
    // before the most recent thread has finished loading, and a question sent
    // in that window would be addressed to the draft, then discarded when the
    // load resolves and moves the user into a thread the answer does not belong
    // to. The turn would be saved server-side and invisible here, which is the
    // worst of both.
    if (!text || pending || loadingThread) return;

    const threadId = activeId;
    const controller = new AbortController();
    requestController.current = controller;
    setPending({ question: text, threadId });
    setQuestion("");
    setError(null);

    try {
      const result: AskResult = threadId
        ? await chat.ask(threadId, text, controller.signal)
        : await chat.askNew(agentId, text, controller.signal);

      // The user moved to another thread while this was generating. The turn is
      // saved; it just does not belong on this screen.
      if (activeIdNow.current !== threadId) return;

      setActiveId(result.conversation_id);
      activeIdNow.current = result.conversation_id;
      setMessages((current) => [...current, toMessage(text, result)]);
      // Only when there is something, so the array's identity is stable across
      // the ordinary turn that produces nothing -- see `turnHandouts` above.
      // Set AFTER the `activeIdNow` guard: a turn the user has navigated away
      // from is dropped entirely, and its handouts are still listed by the
      // panel's own poll, under the thread they actually belong to.
      if (result.handouts.length > 0) setTurnHandouts(result.handouts);
      onAnswered?.(result.query_id);
      // The title is derived server-side from the first question, and
      // `updated_at` is what the list sorts on -- neither is knowable here.
      void refreshList().catch(() => {
        // A stale sidebar is not worth an error banner over a delivered answer.
      });
    } catch (cause) {
      const cancelled = cause instanceof DOMException && cause.name === "AbortError";
      if (activeIdNow.current === threadId) {
        if (!cancelled) setError(errorMessage(cause));
        // Handed back rather than swallowed: a 15-second wait that ends in a
        // 500 should not also cost the user their typing. Only into an empty
        // box, though -- they may have started typing the next question while
        // this one was in flight, and restoring over that would be worse than
        // losing it.
        setQuestion((current) => (current === "" ? text : current));
      }
    } finally {
      if (requestController.current === controller) requestController.current = null;
      setPending(null);
    }
  }

  function stopWaiting() {
    requestController.current?.abort();
  }

  async function rename(id: string) {
    const title = draftTitle.trim();
    setRenamingId(null);
    if (!title) return;
    try {
      await chat.rename(id, title);
      await refreshList();
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  async function remove(id: string) {
    try {
      await chat.remove(id);
      if (activeIdNow.current === id) startDraft();
      await refreshList();
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Shift+Enter newlines, Enter sends. `isComposing` guards an IME candidate
    // window, where Enter commits the candidate and must not also send.
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void send();
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void send();
  }

  const showPending = pending !== null && pending.threadId === activeId;

  // One instance, rendered docked or inside the drawer. See `useIsWideViewport`
  // for why this is a variable rather than two JSX blocks with `xl:hidden`.
  const panel = (
    <HandoutsPanel
      agentId={agentId}
      conversationId={activeId}
      onCountChange={handleCountChange}
      seed={turnHandouts}
    />
  );

  return (
    <>
      {/*
        `flex` below `md`, `grid` at `md` and up, which is not a stylistic
        preference -- it is what stops the thread pane from growing without
        bound in each mode.

        Below `md` the column holds a toggle, a collapsible rail and the thread,
        stacked; `flex-1 min-h-0` on the thread is what makes it take the space
        the other two do not use. In a one-column GRID the same three would be
        auto-sized rows, and an auto row is sized by its content -- a thread of
        forty turns would size the row to forty turns and push the composer far
        below the fold, which is exactly the bug being fixed.

        At `md` the explicit `grid-rows-[minmax(0,1fr)]` pins the single row to
        the container's height rather than to its content, for the same reason.
        Without it an `auto` row's max track sizing function is `max-content`,
        so a long thread would again grow the row past the viewport.

        `min-h-0` on the container itself is what lets any of that resolve: this
        is a flex item of the height context AgentDetail establishes, and a flex
        item's default `min-height: auto` refuses to shrink below its content.
      */}
      {/*
        TWO tracks, and there is deliberately no `xl:` variant. Re-adding a third
        column is a one-token edit, which is exactly why `scripts/ui_check.py`
        asserts the track count rather than trusting a comment: the silhouette it
        would restore is the thing this layout exists to remove.

        `17rem` rather than the old `15rem` because the rail now holds filenames,
        which are longer than conversation titles and were already truncating.
      */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 md:grid md:grid-cols-[17rem_minmax(0,1fr)] md:grid-rows-[minmax(0,1fr)]">
        <button
          type="button"
          data-testid="rail-toggle"
          aria-expanded={historyOpen}
          aria-controls="agent-rail"
          onClick={() => setHistoryOpen((open) => !open)}
          className="min-h-11 shrink-0 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-left text-sm font-medium text-slate-200 transition hover:border-slate-600 md:hidden"
        >
          {historyOpen
            ? "Hide sources and threads"
            : `Sources and threads (${conversations.length})`}
        </button>

        {/*
          `max-h-56` below `md`, and it is a pixel cap rather than a viewport
          fraction on purpose. Opened on a phone this is a disclosure sitting
          ABOVE the thread inside a fixed-height column, so without a cap a long
          list would consume the whole column and leave the thread at zero.
          224px is about five rows, and selecting a thread closes the rail
          anyway. At `md` it is a grid column and the row height is the cap.
        */}
        <aside
          id="agent-rail"
          className={`${historyOpen ? "flex" : "hidden"} max-h-56 min-h-0 min-w-0 flex-col rounded-xl border border-slate-800 bg-slate-900/30 p-2 md:flex md:max-h-none`}
        >
          {/*
            The switch. `role="tablist"` rather than two plain buttons because
            these two panels are alternatives sharing one region, which is what
            the tab pattern describes -- and it is what tells a screen reader
            that picking one replaces the other rather than adding to it.
          */}
          <div role="tablist" aria-label="Rail" className="mb-2 flex gap-1">
            {(
              [
                { id: "sources" as const, label: "Sources", testId: "rail-tab-sources" },
                { id: "threads" as const, label: "Threads", testId: "rail-tab-threads" },
              ]
            ).map((entry) => {
              const active = railTab === entry.id;
              return (
                <button
                  key={entry.id}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  data-testid={entry.testId}
                  onClick={() => setRailTab(entry.id)}
                  className={`min-h-11 flex-1 rounded-md px-2 text-xs font-medium tracking-wide uppercase transition ${
                    active
                      ? "bg-slate-800 text-slate-100"
                      : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"
                  }`}
                >
                  {entry.label}
                </button>
              );
            })}
          </div>

          {/*
            Sources stays MOUNTED behind the Threads tab, hidden rather than
            unmounted, for the same reason the handout dock does -- and the same
            mistake was available here.

            `SourceRail` owns the poll that watches an ingest finish, and
            finishing is what fires `onCorpusChanged` and moves the agent's
            `document_count` and `status` in the bar above. Unmounting it on a
            tab switch would stop the watch on a job that is still running, so a
            user who uploads a file and flicks to Threads would come back to a
            row still saying "processing" and a bar still saying "empty",
            forever. The poll costs nothing once nothing is pending: it stops on
            its own.

            Threads is conditionally rendered, because it has no timer and
            nothing to report -- the conversation list is state on this
            component and survives the unmount.
          */}
          {/* `flex` / `hidden` rather than a `contents` toggle: both are display
              utilities of equal specificity, so which one won would depend on
              their order in the generated stylesheet rather than on this string.
              The same swap the rail itself uses one element up. */}
          <div
            className={`${
              railTab === "sources" ? "flex" : "hidden"
            } min-h-0 min-w-0 flex-1 flex-col`}
          >
            <SourceRail agentId={agentId} onCorpusChanged={onCorpusChanged} />
          </div>

          {railTab === "threads" && (
            <>
              <button
                type="button"
                data-testid="conversation-new"
                onClick={startDraft}
                className="mb-2 min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 transition hover:border-slate-600"
              >
                + New chat
              </button>

              <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto">
            {activeId === null && (
              <li className="rounded-md border border-emerald-800/60 bg-emerald-950/20 px-3 py-2 text-sm text-emerald-300">
                New conversation
                <span className="block text-xs text-slate-400">saved on your first question</span>
              </li>
            )}

            {conversations.map((conversation) => {
              const active = conversation.id === activeId;
              const renaming = conversation.id === renamingId;

              return (
                <li key={conversation.id}>
                  {renaming ? (
                    <input
                      autoFocus
                      value={draftTitle}
                      onChange={(event) => setDraftTitle(event.target.value)}
                      // Blur cancels rather than saves. Saving on blur means
                      // clicking a different conversation writes a title the user
                      // never confirmed, and Escape stops working -- unmounting
                      // the input is itself a blur. Enter is the commit.
                      onBlur={() => setRenamingId(null)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void rename(conversation.id);
                        if (event.key === "Escape") setRenamingId(null);
                      }}
                      className="w-full rounded-md border border-slate-600 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 outline-none"
                    />
                  ) : (
                    <button
                      type="button"
                      data-testid="conversation-item"
                      onClick={() => void openConversation(conversation.id)}
                      className={`w-full rounded-md px-3 py-2 text-left transition ${
                        active
                          ? "border border-slate-600 bg-slate-800/70"
                          : "border border-transparent hover:bg-slate-900"
                      }`}
                    >
                      <span className="block truncate text-sm text-slate-200">
                        {conversation.title ?? "Untitled"}
                      </span>
                      <span className="block text-xs text-slate-400">
                        {conversation.message_count}{" "}
                        {conversation.message_count === 1 ? "turn" : "turns"}
                      </span>
                    </button>
                  )}

                  {active && !renaming && (
                    <div className="mt-1 flex items-center gap-1 px-1 pb-1">
                      <button
                        type="button"
                        onClick={() => {
                          setDraftTitle(conversation.title ?? "");
                          setRenamingId(conversation.id);
                        }}
                        aria-label={`Rename ${conversation.title ?? "untitled conversation"}`}
                        className="min-h-11 rounded border border-slate-700 bg-slate-900 px-2 py-2 text-xs text-slate-300 transition hover:border-slate-600"
                      >
                        Rename
                      </button>
                      <ConfirmDeleteButton
                        testId="conversation-delete"
                        label="Delete"
                        confirmLabel="Confirm"
                        accessibleLabel={`Delete ${conversation.title ?? "untitled conversation"}`}
                        accessibleConfirmLabel={`Confirm deletion of ${conversation.title ?? "untitled conversation"}`}
                        onConfirm={() => void remove(conversation.id)}
                      />
                    </div>
                  )}
                </li>
              );
            })}
              </ul>
            </>
          )}
        </aside>

        {/*
          No `h-[70dvh] md:h-[70vh]` any more, and dropping it is the whole fix.

          A magic viewport fraction is a height this pane invents for itself with
          no knowledge of what is above it. On a 390x844 phone the sticky nav, the
          agent header and the tab strip already used most of the screen, so a
          pane 70% of the viewport tall did not fit in what was left -- the
          document scrolled AND the pane inside it scrolled, and the composer
          started below the fold. The user scrolled the page to find the input,
          then scrolled the pane to read the answer.

          `flex-1 min-h-0` asks for "whatever is left" instead, which is only
          answerable because AgentDetail gives the chat tab a real height. At `md`
          this is a grid item and `flex-1` is inert -- the row is
          `minmax(0,1fr)` and stretches it -- but `min-h-0` is what both modes
          need, because the default `min-height: auto` on a flex or grid item
          refuses to shrink below its content and would push the composer out of
          the box again.
        */}
        <section
          aria-busy={showPending}
          // Named so `scripts/ui_check.py` can scope "exactly one scrollable
          // region" to this column and mean it. Scoped by tag instead, the
          // assertion counted two `<textarea>` elements -- which carry
          // `overflow-y: auto` intrinsically -- and one of them belonged to the
          // settings sheet, which also renders `<section>`. It failed on a
          // layout that was correct, which is the failure mode that teaches its
          // reader to stop believing the suite.
          data-testid="chat-column"
          className="flex min-h-0 min-w-0 max-w-full flex-1 flex-col rounded-xl border border-slate-800 bg-slate-900/30"
        >
          {/*
            The toggle bar that used to sit here is gone with the column it
            belonged to. A button with a count pill, in a thin strip above the
            thread, opening a right-hand panel, was NotebookLM's mobile Studio
            chrome -- and it cost a whole row of vertical space to say something
            the dock beneath the composer now says in place.
          */}
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <div className="mb-4">
              <ErrorBanner error={error} />
            </div>

            {loadingThread && <Spinner label="Loading conversation" />}

            {!loadingThread && messages.length === 0 && !showPending && (
              <p className="rounded-lg border border-dashed border-slate-800 px-4 py-10 text-center text-sm text-slate-400">
                Ask this agent a question. Follow-ups can refer back to earlier turns -- the
                question that gets embedded is shown above each answer whenever it differs
                from what you typed.
              </p>
            )}

            <ol className="space-y-6">
              {messages.map((message) => (
                <Message key={message.query_id} message={message} />
              ))}

              {showPending && pending && (
                <li className="space-y-2.5">
                  <div className="flex justify-end">
                    <div className="max-w-[85%] break-words rounded-2xl rounded-br-sm border border-slate-700 bg-slate-800/70 px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-100">
                      {pending.question}
                    </div>
                  </div>

                  {/*
                    Elapsed seconds and a named stage, not an indefinite spinner.
                    A spinner with no number reads as hung at about four seconds,
                    and "the button does nothing" becomes the bug report for a
                    system working exactly as designed.

                    The number here is deliberately a RANGE, and a wide one,
                    because the personas changed it. A bare Stage-1 turn was
                    measured at ~14.8 s (embed 365 ms, Pinecone 394 ms, Cohere
                    rerank ~830 ms, Gemma 13.2 s -- 89% generation). A persona
                    turn measured 45 s, and the extra is almost entirely output
                    tokens: a Feynman answer carries an analogy, a worked example
                    and a named gap, so it emits roughly ten times the text for
                    the same retrieval. Generation is token-bound, so verbosity
                    IS latency here.

                    Quoting the old 10-15 s figure under a persona would be a
                    promise the system cannot keep, and a progress note that
                    under-promises is worse than none -- the user starts counting
                    against it at 16 s and concludes it has hung.
                  */}
                  <div className="rounded-2xl rounded-bl-sm border border-slate-800 bg-slate-900/50 p-4">
                    <div role="status" aria-live="polite" aria-atomic="true">
                      <Spinner label={`${stageFor(elapsed, messages.length > 0)}...`} />
                    </div>
                    <p className="mt-2 text-xs text-slate-400">
                      {elapsed}s elapsed &middot; retrieval takes under a second; the rest is the
                      model writing. Coaching personas answer at length, so 30-60 s is normal
                      for them.
                    </p>
                  </div>
                </li>
              )}
            </ol>

            <div ref={bottom} />
          </div>

          <form onSubmit={onSubmit} className="border-t border-slate-800 p-3">
            <label className="sr-only" htmlFor="chat-question">
              Question
            </label>
            <div className="flex min-w-0 items-end gap-2">
              <textarea
                id="chat-question"
                ref={input}
                data-testid="chat-input"
                rows={2}
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Ask a question. Enter sends, Shift+Enter adds a line."
                className="min-h-12 min-w-0 flex-1 resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
              />
              {pending ? (
                <button
                  type="button"
                  data-testid="chat-stop"
                  onClick={stopWaiting}
                  className="min-h-11 shrink-0 rounded-md border border-amber-700 bg-amber-950/50 px-3 py-2 text-sm font-semibold text-amber-200 transition hover:border-amber-500 hover:bg-amber-950"
                >
                  Stop waiting
                </button>
              ) : (
                <button
                  type="submit"
                  data-testid="chat-send"
                  disabled={loadingThread || question.trim() === ""}
                  className="min-h-11 shrink-0 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
                >
                  Send
                </button>
              )}
            </div>
          </form>

          {/*
            AFTER the composer, and that order is the guarantee rather than a
            preference. The dock is `shrink-0`, so every pixel it takes when it
            opens comes out of the thread above it and never out of the composer
            below -- which is what keeps "the composer is fully inside the
            viewport at 390x844 with the page at scrollTop 0" true whether the
            dock is open or shut.

            One panel, one frame, at every width. The old arrangement needed a
            media query in JavaScript to decide between a docked column and a
            fixed drawer, because those are two different places in the DOM and
            no breakpoint utility moves a node -- and rendering both would have
            meant two live polls, two `onCountChange` callbacks fighting over one
            badge, and each turn's handouts prepended twice. Collapsing to one
            surface deletes that whole problem along with the column.
          */}
          <HandoutDock
            count={handoutCount}
            open={dockOpen}
            onToggle={() => setDockOpen((open) => !open)}
          >
            {panel}
          </HandoutDock>
        </section>
      </div>
    </>
  );
}

/**
 * Fold an answer into the thread without re-reading it.
 *
 * `AskOut` carries no server timestamp, and fetching the conversation again
 * just to learn one would add a round trip to the end of an already 15-second
 * wait. The client clock is close enough for a line of small grey text; every
 * other field comes from the response.
 */
function toMessage(question: string, result: AskResult): ChatMessage {
  return {
    query_id: result.query_id,
    question,
    answer: result.answer,
    refused: result.refused,
    latency_ms: result.latency_ms,
    model_used: result.model_used,
    rewritten_question: result.rewritten_question,
    created_at: new Date().toISOString(),
    citations: result.citations,
    // Both carried straight through rather than defaulted to `[]` / `0` here.
    // `AskResult` and `ChatMessage` hold the same two fields for the same turn,
    // and a fresh turn that renders without its handout chip while the same
    // turn re-read from the server renders with one is the exact divergence
    // this fold exists to avoid -- the server defaults both to empty for an
    // agent with tools off, so there is nothing to substitute for.
    handouts: result.handouts,
    tool_steps: result.tool_steps,
  };
}

/**
 * The stage a turn is probably in, from measured timings: embedding finishes
 * around 0.4 s, retrieval and reranking by about 1.6 s, and everything after
 * that is generation.
 *
 * `hasHistory` shifts every boundary because a follow-up is contextualised
 * before it is embedded -- the model rewrites "what is its power budget?" into
 * a standalone question first, measured at 3.8 s. Without this the label reads
 * "Embedding the question" for four seconds while the system is in fact
 * rewriting it, which is the one stage a user would actually want named: it is
 * the step that explains why the answer is about something they did not type.
 *
 * Probably, not certainly. It is a progress hint, not a trace; the real
 * per-turn timeline is on the answer once it arrives.
 */
function stageFor(seconds: number, hasHistory: boolean): string {
  if (hasHistory) {
    if (seconds < 4) return "Working out what the question refers to";
    if (seconds < 5) return "Embedding the rewritten question";
    if (seconds < 6) return "Searching this agent's corpus and reranking";
    return "Generating the answer";
  }
  if (seconds < 1) return "Embedding the question";
  if (seconds < 2) return "Searching this agent's corpus and reranking";
  return "Generating the answer";
}
