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
 * through `POST /api/agents/{id}/ask/stream`, whose first frame carries the
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
 * to. Streaming applies that guard roughly two hundred times a turn instead of
 * once -- see `send`.
 *
 * **The answer is read as it is written, and the last frame overrules it.** The
 * deltas are a draft: citation markers are normalised server-side only once the
 * whole answer and the finished citation list exist, so what is on screen mid
 * stream and what a reload shows genuinely differ. `done.result.answer` replaces
 * the concatenation, through the same `toMessage` the non-streaming path uses,
 * which is what makes the two impossible to render differently.
 */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { chat } from "../lib/api.ts";
import type { AskStreamHandlers } from "../lib/api.ts";
import type {
  AskResult,
  AskStreamPhase,
  AskStreamToolCall,
  AskStreamToolError,
  AskStreamToolResult,
  ChatMessage,
  Conversation,
  Handout,
} from "../lib/types.ts";
import { ConfirmDeleteButton, ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import Message from "../components/Message.tsx";
import HandoutDock from "../components/HandoutDock.tsx";
import HandoutsPanel from "../components/HandoutsPanel.tsx";
import SourceRail from "../components/SourceRail.tsx";
// The SAME markdown pipeline the finished answer renders through. A second
// component map for the streamed half is how a heading ends up one size while
// it is being written and another size a second later -- see lib/markdown.tsx,
// which exists precisely because that map had already been copied once.
import { Markdown } from "../lib/markdown.tsx";

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

/**
 * The turn in flight, as the RENDER needs it.
 *
 * **Every field is written at most once, and that is the constraint rather than
 * a coincidence.** The elapsed timer and the auto-scroll both used to take
 * `pending` as an effect dependency, so anything written at token rate would
 * tear those effects down and rebuild them two hundred times a turn -- the
 * timer's `startedAt` resetting on every token, `elapsed` pinned at 0, a wrong
 * number rendered confidently. Streamed text and the phase log therefore live in
 * their own state, and the effects key on a boolean rather than on this object.
 *
 * `queryId` and `conversationId` arrive together in the stream's first frame,
 * which is one identity change per turn, at a moment when nothing holds focus.
 */
type PendingTurn = {
  question: string;
  /** The ADDRESS at send time. `null` is the unsaved draft. */
  threadId: string | null;
  /** From `start`. Real from the first frame, because the `queries` row is
   *  flushed before the pipeline runs -- which is what lets a STOPPED turn keep
   *  a genuine identity rather than a synthetic one. */
  queryId: string | null;
  /** From `start`, and only written when the user is still on this turn's
   *  address. Once set it, not `threadId`, is where the bubble belongs: a draft
   *  becomes a real conversation the moment the first frame lands. */
  conversationId: string | null;
};

/**
 * The same turn as the ASYNC HANDLERS need it: a mutable record read from
 * callbacks that fire long after the closure was built.
 *
 * A ref rather than state because every consumer is a decision, not a render --
 * "is the user still on this address", "what text should a stopped turn keep",
 * "which query does this belong to". Reading those off state would read the
 * values as they were when `send` was called, which for `address` in particular
 * is the exact bug the address guard exists to prevent.
 */
type TurnFacts = {
  /** Follows the promotion: `null` until `start` names the conversation. */
  address: string | null;
  queryId: string | null;
  /** The concatenated deltas. A DRAFT -- `done.result.answer` replaces it. */
  text: string;
  /** From the `rewrite` phase, so a stopped turn can still show what was
   *  actually searched for. */
  rewritten: string | null;
  /** From the same frame. Carried separately because the banner renders on this
   *  and not on the string -- a stopped turn whose rewrite changed nothing must
   *  not claim it searched for something else. */
  rewrittenChanged: boolean | null;
  /** Counted from `tool_call` frames, so a stopped turn still carries the chip
   *  saying it searched. */
  toolSteps: number;
  startedAt: number;
};

/**
 * One line of the progress log.
 *
 * **Past tense is the mechanism, not the styling.** A completed line is a
 * receipt: it promises nothing. Only the last line is in the present tense, and
 * it names what is happening rather than when it will end -- because the honest
 * denominator for a progress bar does not exist here. The model decides how many
 * tool steps a turn takes, and generation is token-bound with a tenfold spread
 * between a terse agent and a coaching persona. A note that under-promises is
 * worse than none: the user starts counting against it and concludes it hung.
 *
 * `key` is what makes a `finished` frame update the line its `started` frame
 * created instead of appending a second one -- and it is why `rerank`, which
 * arrives as `finished` with no `started` before it, simply creates its line on
 * the spot.
 */
type ProgressEntry = {
  key: string;
  label: string;
  detail: string | null;
  /** Amber rather than grey, and it is not an error banner: a tool failure comes
   *  back to the model as a message, the loop continues, and the turn usually
   *  still ends in a real answer. */
  failed?: boolean;
};

/** How many completed receipts to keep on screen. A cap by RENDERING FEWER
 *  lines, never by giving the log its own scrollbar: `scripts/ui_check.py`
 *  asserts exactly one scrollable region inside the chat column, and a second
 *  one would also be the wrong answer for a reader. */
const PROGRESS_LINES = 5;

/** Auto-scroll only while the reader is already at the bottom. Anyone who has
 *  scrolled up to re-read an earlier turn must not be dragged back down every
 *  eighty milliseconds. */
const NEAR_BOTTOM_PX = 48;

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
  const [pending, setPending] = useState<PendingTurn | null>(null);
  /**
   * The answer so far, in its own state and deliberately NOT inside `pending`.
   * See `PendingTurn`: this is the value that changes at token rate, and every
   * effect that must not restart on a token keys on `pending` or on a boolean
   * derived from it.
   */
  const [streamText, setStreamText] = useState("");
  /** The phase log. Grows to a handful of lines, then stops -- see
   *  `PROGRESS_LINES`. */
  const [progress, setProgress] = useState<ProgressEntry[]>([]);
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
  const thread = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLTextAreaElement | null>(null);
  // Read by async handlers that need to know where the user is NOW, which the
  // closure's captured `activeId` cannot tell them -- that is the value at the
  // moment the request was sent, and comparing the two is the whole check.
  const activeIdNow = useRef<string | null>(null);
  const requestController = useRef<AbortController | null>(null);
  /** See `TurnFacts`. Reset at the top of every `send`; there is only ever one
   *  turn in flight, which `pending` enforces at the entry guard. */
  const turn = useRef<TurnFacts>({
    address: null,
    queryId: null,
    text: "",
    rewritten: null,
    rewrittenChanged: null,
    toolSteps: 0,
    startedAt: 0,
  });
  /** Whether the reader is pinned to the bottom. Maintained by the scroller's
   *  own `onScroll`, so scrolling up to re-read something turns auto-scroll off
   *  and scrolling back down turns it on again -- no button, no state, nothing
   *  to explain. */
  const stickToBottom = useRef(true);
  const scrollFrame = useRef<number | null>(null);
  useEffect(() => {
    activeIdNow.current = activeId;
  }, [activeId]);

  useEffect(
    () => () => {
      requestController.current?.abort();
      // Tied to the same unmount as the abort: a frame that fires after the tree
      // is gone reads a null ref and does nothing, but an outstanding frame on
      // every unmounted chat is a leak with no upper bound.
      if (scrollFrame.current !== null) window.cancelAnimationFrame(scrollFrame.current);
    },
    [],
  );

  /** Called by the scroller. Cheap enough to run on every scroll event: two
   *  layout reads and a comparison. */
  const noteScrollPosition = useCallback(() => {
    const element = thread.current;
    if (!element) return;
    stickToBottom.current =
      element.scrollHeight - element.scrollTop - element.clientHeight <= NEAR_BOTTOM_PX;
  }, []);

  /**
   * Follow the answer down, at most once per animation frame.
   *
   * Throttled because tokens arrive far faster than the browser paints, and
   * gated because the position is read from the ref the scroll handler
   * maintains rather than measured after the fact -- measuring afterwards would
   * see the newly appended text as "far from the bottom" and stop following on
   * exactly the long answers that need it.
   */
  const scheduleScroll = useCallback(() => {
    if (scrollFrame.current !== null) return;
    scrollFrame.current = window.requestAnimationFrame(() => {
      scrollFrame.current = null;
      if (!stickToBottom.current) return;
      // `behavior: "auto"`, unlike the message-level scroll below: a smooth
      // animation restarted every frame never arrives, and its intermediate
      // positions would fight the near-bottom check above.
      bottom.current?.scrollIntoView({ block: "nearest" });
    });
  }, []);

  const refreshList = useCallback(async () => {
    setConversations(await chat.list(agentId));
  }, [agentId]);

  /**
   * A conversation this client has been told about but the server has not
   * committed yet, or `null`.
   *
   * See the long note in `send`'s cancel branch for how the window opens. The
   * value is the id to wait for; `send` refuses to address it until the list
   * confirms it exists, which is the only evidence available -- the list query
   * runs in its own session, so a row it returns is by definition committed.
   */
  const [unsettledId, setUnsettledId] = useState<string | null>(null);

  /**
   * Poll the conversation list until `id` appears, then release it.
   *
   * Bounded rather than open-ended: a turn that dies server-side never commits,
   * and a thread the user can never send to again would be a worse failure than
   * the 404 this exists to prevent. Giving up releases the id, so the next send
   * attempts it and surfaces the real server error if there is one -- which is
   * the honest outcome, and the same "let the server be the authority" line the
   * settings sheet takes.
   */
  /** The thread on screen is the one waiting to commit. Derived rather than a
   *  second piece of state, so it cannot disagree with `unsettledId` -- and it
   *  is false on any OTHER thread, which stays fully usable meanwhile. */
  const settling = activeId !== null && activeId === unsettledId;

  const settleAddress = useCallback(
    (id: string) => {
      setUnsettledId(id);
      let attempts = 0;
      const tick = async () => {
        attempts += 1;
        try {
          const rows = await chat.list(agentId);
          setConversations(rows);
          if (rows.some((row) => row.id === id)) {
            setUnsettledId((current) => (current === id ? null : current));
            return;
          }
        } catch {
          // A failed poll is not worth an error banner over a turn the user
          // already stopped watching. The give-up below is the backstop.
        }
        if (attempts >= 20) {
          setUnsettledId((current) => (current === id ? null : current));
          return;
        }
        window.setTimeout(() => void tick(), 3_000);
      };
      window.setTimeout(() => void tick(), 3_000);
    },
    [agentId],
  );

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
  //
  // Keyed on the BOOLEAN, not on `pending` itself, and that is the difference
  // between a counter and a lie. `pending` gains its ids when the stream's first
  // frame lands, about a tenth of a second in; keying on its identity would tear
  // this effect down and start a fresh `startedAt` at that moment. Once, here --
  // but the same mistake made with a token-rate field pins the number at 0 for
  // the whole turn while still rendering confidently.
  const inFlight = pending !== null;
  useEffect(() => {
    if (!inFlight) {
      setElapsed(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      250,
    );
    return () => window.clearInterval(timer);
  }, [inFlight]);

  // `block: "nearest"` keeps the scroll inside the thread pane. `"end"` would
  // also scroll the window to bring the pane into view, yanking the page around
  // every time an answer arrives.
  //
  // This is the turn-level scroll -- two or three times a turn, when a message
  // folds in or the pending bubble appears. Following the answer as it streams
  // is `scheduleScroll`, which is throttled and gated; this one is neither,
  // because it fires when the thing the user just did produced something.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages.length, inFlight, activeId]);

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

  /** Add a line to the progress log, or update the one its `started` frame
   *  already created. See `ProgressEntry.key`. */
  function note(entry: ProgressEntry) {
    setProgress((current) => {
      const at = current.findIndex((existing) => existing.key === entry.key);
      if (at === -1) return [...current, entry];
      const next = current.slice();
      next[at] = entry;
      return next;
    });
    scheduleScroll();
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
    // The thread exists only inside a transaction that has not committed. See
    // `settleAddress`. Sending now would 404 on a turn that is working.
    if (settling) return;

    const threadId = activeId;
    const controller = new AbortController();
    requestController.current = controller;
    turn.current = {
      address: threadId,
      queryId: null,
      text: "",
      rewritten: null,
      rewrittenChanged: null,
      toolSteps: 0,
      startedAt: Date.now(),
    };
    setPending({ question: text, threadId, queryId: null, conversationId: null });
    setStreamText("");
    setProgress([]);
    setQuestion("");
    setError(null);
    // A new turn is a thing the user just did, so follow it even if they had
    // scrolled up to read something else.
    stickToBottom.current = true;

    /**
     * The address guard, applied per frame instead of once.
     *
     * A turn the user has navigated away from keeps being READ to completion --
     * that is what lets `done` still refresh the sidebar and seed the handout
     * panel -- it simply stops being rendered. Dropping out of the append path
     * rather than out of the read is the whole difference.
     */
    const onAddress = () => activeIdNow.current === turn.current.address;

    const handlers: AskStreamHandlers = {
      onStart: (event) => {
        turn.current.queryId = event.query_id;
        // Recorded above the guard because a STOPPED turn needs the real id even
        // if the user wandered off and came back; everything below moves the
        // user's view and must not happen behind their back.
        if (!onAddress()) return;

        const wasDraft = turn.current.address === null;
        // The turn's address follows the promotion. Without this the fold at the
        // end would compare a live `activeId` against a stale `null` and discard
        // an answer that is on screen.
        turn.current.address = event.conversation_id;
        if (wasDraft) {
          // Promoted HERE rather than at `done`: a new thread that only learns
          // its id thirty seconds later leaves the sidebar wrong for the whole
          // of it, and a stopped turn would have nowhere to live.
          setActiveId(event.conversation_id);
          activeIdNow.current = event.conversation_id;
        }
        setPending((current) =>
          current
            ? { ...current, queryId: event.query_id, conversationId: event.conversation_id }
            : current,
        );
      },

      onPhase: (event) => {
        if (event.name === "rewrite" && event.status === "finished") {
          turn.current.rewritten = event.rewritten_question ?? null;
          turn.current.rewrittenChanged = event.rewritten_changed ?? null;
        }
        if (!onAddress()) return;
        const line = phaseLine(event);
        if (line) note(line);
      },

      onTool: (event) => {
        if (event.type === "tool_call") turn.current.toolSteps += 1;
        if (!onAddress()) return;
        note(toolLine(event));
      },

      onToken: (delta) => {
        // Concatenated verbatim: the delta may span several model tokens and may
        // contain newlines, and anything trimmed here is a word joined to the
        // next one. Accumulated in the ref even off-address, so the ref is the
        // one true account of what this turn produced.
        turn.current.text += delta;
        if (!onAddress()) return;
        setStreamText(turn.current.text);
        scheduleScroll();
      },

      onAnswerReset: (event) => {
        // A user-visible retraction: a complete-looking half answer disappears
        // and is replaced. Bounded to once per turn server-side, and given a
        // line of its own rather than a silent wipe, because this is the most
        // interesting thing the agent loop does and hiding it would be a waste
        // as well as a surprise.
        turn.current.text = "";
        if (!onAddress()) return;
        setStreamText("");
        note({
          key: `reset-${event.seq}`,
          label: "Discarded that draft and searched instead",
          detail: `it said "${event.marker}"`,
        });
      },
    };

    try {
      const result: AskResult = threadId
        ? await chat.askStream(threadId, text, handlers, controller.signal)
        : await chat.askNewStream(agentId, text, handlers, controller.signal);

      // Before the guard, unlike everything below it: the title is derived
      // server-side from the first question and `updated_at` is what the list
      // sorts on, and both are just as true for a thread the user has left.
      void refreshList().catch(() => {
        // A stale sidebar is not worth an error banner over a delivered answer.
      });

      // The user moved to another thread while this was generating. The turn is
      // saved; it just does not belong on this screen.
      if (!onAddress()) return;

      setActiveId(result.conversation_id);
      activeIdNow.current = result.conversation_id;
      // `result.answer`, never the concatenated deltas, and through the same
      // `toMessage` a reloaded turn goes through. Citation markers are
      // normalised server-side after generation, so the two strings differ
      // whenever normalisation edited anything -- the live turn and the same
      // turn after a reload must not disagree.
      setMessages((current) => [...current, toMessage(text, result)]);
      // Only when there is something, so the array's identity is stable across
      // the ordinary turn that produces nothing -- see `turnHandouts` above.
      // Set AFTER the `activeIdNow` guard: a turn the user has navigated away
      // from is dropped entirely, and its handouts are still listed by the
      // panel's own poll, under the thread they actually belong to.
      //
      // From `done` only. Handouts are never streamed incrementally: a pending
      // handout row has no bytes, and the panel's own poll is the right watcher
      // for one that is still being written.
      if (result.handouts.length > 0) setTurnHandouts(result.handouts);
      onAnswered?.(result.query_id);
    } catch (cause) {
      const cancelled = cause instanceof DOMException && cause.name === "AbortError";
      const draft = turn.current.text;
      if (!onAddress()) return;

      /*
        Stop means the READER stopped, not the agent.

        The branch is "did any output occur?", never "was there an abort?" --
        and the two cases have opposite right answers. With nothing on screen,
        discarding costs nothing and handing the question back is a kindness.
        With half an answer on screen, discarding deletes text the user was
        actively reading, in response to a button they pressed to stop MORE
        text, and the same turn after a reload shows a complete answer they
        never saw. A truncated bubble with a label on it beats that silence.
      */
      if (cancelled && draft) {
        setMessages((current) => [...current, stoppedMessage(text, turn.current)]);
        /*
          The stopped turn is still running on the server, and its conversation
          row does not exist for anybody else until it commits.

          `onStart` announces the `conversation_id` about a tenth of a second in,
          but `run_turn` holds its single commit until the turn ends -- twenty-five
          to forty-five seconds later. In between, that id names a row created
          inside an uncommitted transaction, so `owned_conversation` resolves it
          in a fresh session and finds nothing.

          The old JSON route could not reach this state, because the client only
          learned the id after the commit. Streaming introduced the window, and
          the way in is exactly this branch: Stop clears `pending`, which
          re-enables the composer, so a follow-up sent in that window goes to an
          id the server will 404 -- a red banner on a turn that is working
          perfectly.

          So the address is marked unsettled and `send` waits for it, rather than
          being reverted. Reverting to the draft would be worse in a quieter way:
          the follow-up would open a SECOND conversation while the first one
          commits underneath it, and the user would be left with their thread
          split across two rows and no indication why.
        */
        if (turn.current.address) settleAddress(turn.current.address);
        return;
      }

      if (!cancelled) setError(errorMessage(cause));
      // Handed back rather than swallowed: a 15-second wait that ends in a
      // 500 should not also cost the user their typing. Only into an empty
      // box, though -- they may have started typing the next question while
      // this one was in flight, and restoring over that would be worse than
      // losing it.
      setQuestion((current) => (current === "" ? text : current));
    } finally {
      // All three land in the same batch as the fold above, because everything
      // from the last `await` to here runs in one continuation -- which is what
      // keeps the folded message and the in-flight bubble from ever being
      // mounted at the same time under the same key.
      if (requestController.current === controller) requestController.current = null;
      setPending(null);
      setStreamText("");
      setProgress([]);
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

  // `conversationId` wins once it exists, because a draft turn's address moves
  // the moment the stream's first frame names the conversation the server
  // created. Comparing against `threadId` forever would make the bubble vanish
  // one tenth of a second after it appeared, on every new thread.
  const showPending = pending !== null && (pending.conversationId ?? pending.threadId) === activeId;

  /** Whether any text has arrived. The phase log collapses to one line at this
   *  point and the answer takes the space -- which is the actual fix for "45
   *  seconds of blank waiting". A better forecast of the blankness is not. */
  const streaming = streamText !== "";
  /**
   * The line under the spinner, and the ONLY thing the live region announces.
   *
   * Falls back to the elapsed-seconds forecast when no phase frame has arrived:
   * against a backend with no streaming route, behind a proxy that buffered the
   * whole response, or simply in the first moments of a turn, `stageFor` is what
   * the user sees and the turn still works end to end.
   */
  const current = progress.length > 0 ? progress[progress.length - 1] : null;
  const liveLine = current ? current.label : stageFor(elapsed, messages.length > 0);
  /** Everything before the live line, oldest first, capped by rendering fewer
   *  rather than by scrolling. */
  const receipts = progress.slice(0, -1).slice(-PROGRESS_LINES);

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
          {/* The ONE scrollable region in this column, which
              `scripts/ui_check.py` asserts by structure. Neither the streamed
              bubble nor the phase log may take an `overflow-y` of its own; the
              `overflow-x-auto` that `pre` and `table` carry in the markdown map
              is a different axis and is safe -- removing it in a panic would
              break horizontal overflow at 320px instead. */}
          <div
            ref={thread}
            onScroll={noteScrollPosition}
            className="min-h-0 flex-1 overflow-y-auto p-4"
          >
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
                /*
                  Keyed by the real `query_id` once the first frame carries it,
                  and by a constant before that -- one key transition per turn,
                  at a moment when nothing inside holds focus. Prefixed so it can
                  never collide with a folded message's key even for the single
                  render where both might exist: a duplicate key is a console
                  error, and `scripts/ui_check.py` fails the whole suite on one.
                */
                <li
                  key={pending.queryId ? `pending-${pending.queryId}` : "pending"}
                  className="space-y-2.5"
                >
                  <div className="flex justify-end">
                    <div className="max-w-[85%] break-words rounded-2xl rounded-br-sm border border-slate-700 bg-slate-800/70 px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-100">
                      {pending.question}
                    </div>
                  </div>

                  {/*
                    What the turn has actually done, in the past tense, plus one
                    line in the present tense for what it is doing now.

                    Not a checklist with pending steps, and not for cosmetic
                    reasons: tool steps are MODEL-DECIDED. A turn may search
                    zero, one or three times, so a fixed set of greyed-out future
                    steps asserts a route the model has not committed to -- and
                    on a turn that takes a different route those steps stay grey
                    forever, which reads as a hang.

                    The elapsed counter stays, and it stays for the reason it was
                    added: a spinner with no number reads as hung at about four
                    seconds, and "the button does nothing" becomes the bug report
                    for a system working exactly as designed. It is also the one
                    number that cannot under-promise, because it measures the
                    past. The 30-60 s range goes the moment text arrives, because
                    by then it is answering a question the user has stopped
                    asking.
                  */}
                  <div className="rounded-2xl rounded-bl-sm border border-slate-800 bg-slate-900/50 p-4">
                    {!streaming && receipts.length > 0 && (
                      <ol
                        data-testid="turn-progress"
                        className="mb-3 space-y-1 border-b border-slate-800 pb-3"
                      >
                        {receipts.map((entry) => (
                          <li
                            key={entry.key}
                            className="flex flex-wrap items-baseline gap-x-2 text-xs"
                          >
                            <span className={entry.failed ? "text-amber-300" : "text-slate-400"}>
                              {entry.label}
                            </span>
                            {entry.detail && (
                              <span className="min-w-0 break-words text-slate-500">
                                {entry.detail}
                              </span>
                            )}
                          </li>
                        ))}
                      </ol>
                    )}

                    {/*
                      Phase transitions only -- four to eight announcements a
                      turn. The streamed text is deliberately OUTSIDE this
                      region: `aria-atomic="true"` re-reads the entire contents
                      on every mutation, so a screen reader would restart the
                      whole partial answer from the top on every token.
                    */}
                    <div role="status" aria-live="polite" aria-atomic="true">
                      {streaming ? (
                        <p className="text-xs text-slate-500">{liveLine}</p>
                      ) : (
                        <Spinner label={`${liveLine}...`} />
                      )}
                    </div>

                    {streaming && <StreamingAnswer text={streamText} />}

                    {streaming ? (
                      /*
                        Said plainly, because the text above is about to change
                        under the reader: markers become numbered chips, some
                        brackets are rewritten and some are deleted. That edit is
                        the single most likely way streaming feels WORSE than the
                        old wait, and an unannounced edit is how it happens.
                      */
                      <p className="mt-3 text-xs text-slate-500">
                        Writing... source links and citation numbers are added when the answer
                        finishes.
                      </p>
                    ) : (
                      /*
                        The range is deliberately wide, because the personas
                        changed it. A bare Stage-1 turn was measured at ~14.8 s
                        (embed 365 ms, Pinecone 394 ms, Cohere rerank ~830 ms,
                        generation 13.2 s -- 89% of the turn). A persona turn
                        measured 45 s, and the extra is almost entirely output
                        tokens: a Feynman answer carries an analogy, a worked
                        example and a named gap, so it emits roughly ten times
                        the text for the same retrieval. Generation is
                        token-bound, so verbosity IS latency here.
                      */
                      <p className="mt-2 text-xs text-slate-400">
                        {elapsed}s elapsed &middot; retrieval takes under a second; the rest is the
                        model writing. Coaching personas answer at length, so 30-60 s is normal
                        for them.
                      </p>
                    )}
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
                /*
                  "Stop", not "Stop waiting", now that there is something to
                  watch rather than something to wait for. It still stops the
                  READER and not the agent -- the turn finishes and is committed
                  server-side either way -- which is why a stopped turn keeps the
                  text it had and says so, rather than vanishing.
                */
                <button
                  type="button"
                  data-testid="chat-stop"
                  onClick={stopWaiting}
                  className="min-h-11 shrink-0 rounded-md border border-amber-700 bg-amber-950/50 px-3 py-2 text-sm font-semibold text-amber-200 transition hover:border-amber-500 hover:bg-amber-950"
                >
                  Stop
                </button>
              ) : (
                <button
                  type="submit"
                  data-testid="chat-send"
                  disabled={loadingThread || question.trim() === "" || settling}
                  className="min-h-11 shrink-0 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
                >
                  Send
                </button>
              )}
            </div>

            {/*
              Said out loud, because a disabled button with no explanation is the
              same defect in a smaller costume: the user presses it, nothing
              happens, and there is no error to read. `send` refuses this address
              until the server has committed it -- so the reason has to be on
              screen for the few seconds that takes.
            */}
            {settling && (
              <p
                data-testid="chat-settling"
                role="status"
                aria-live="polite"
                className="mt-2 text-xs text-slate-400"
              >
                The agent is still finishing that turn on the server. This thread
                accepts the next question as soon as it lands.
              </p>
            )}
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
    rewritten_changed: result.rewritten_changed,
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
    tool_calls: result.tool_calls,
  };
}

/**
 * The turn as it stood when the user pressed Stop.
 *
 * Client-only, and every field is chosen so that nothing here can be mistaken
 * for a finished turn. `refused` is false unconditionally -- refusal is decided
 * server-side by a POSITION-SENSITIVE detector reading the complete text, so
 * running a prefix through anything like it would read a caveat that was about
 * to be followed by content as a decline, and would corrupt the one signal
 * Stage 3 scores. `citations` is empty because they are built after generation:
 * there is no card for a chip to open yet, and `Message` already hides the
 * sources button on an empty array.
 *
 * `query_id` is the real one from the stream's first frame, which is what gives
 * this bubble a working trace panel -- the one place a user can find out what
 * the turn actually did before they stopped watching it. The synthetic fallback
 * is unreachable (no text can arrive before `start`) and exists so that a key
 * collision is impossible rather than merely unlikely.
 */
function stoppedMessage(question: string, turn: TurnFacts): ChatMessage {
  return {
    query_id: turn.queryId ?? `stopped-${turn.startedAt}`,
    question,
    answer: turn.text,
    refused: false,
    latency_ms: Date.now() - turn.startedAt,
    model_used: null,
    rewritten_question: turn.rewritten,
    rewritten_changed: turn.rewrittenChanged,
    created_at: new Date().toISOString(),
    citations: [],
    handouts: [],
    // Real, counted off the tool frames, so the chip still says the turn
    // searched -- which is often the most interesting thing about the half of
    // it that was read.
    tool_steps: turn.toolSteps,
    // Zero, and honestly so: an aborted turn has no GENERATE payload, which is
    // where the call count is recorded. `summariseToolActivity` takes the larger
    // of the two, so the chip falls back to the step count rather than claiming
    // the turn searched nothing.
    tool_calls: 0,
    stopped: true,
  };
}

/**
 * One phase frame as a line of the log, or `null` for a frame with nothing to
 * say.
 *
 * **The server sends facts; the sentence is written here.** That split is not
 * tidiness: the rule that a progress note must not under-promise is a COPY rule,
 * and it can only be enforced where the copy lives. It also keeps the wire free
 * of English.
 *
 * An unrecognised `name` returns null and renders as nothing, which is the rule
 * this codebase applies to every loose value the backend sends.
 */
function phaseLine(event: AskStreamPhase): ProgressEntry | null {
  const finished = event.status === "finished";
  const step = event.step ?? 1;
  const key = `phase-${event.name}-${event.name === "generate" ? step : 0}`;

  switch (event.name) {
    case "rewrite":
      return {
        key,
        // "Working out what the question refers to" was coreference-specific and
        // is now wrong on the majority of turns: on a first turn there is
        // nothing to refer to, and the step is repairing spelling and shorthand
        // instead. The copy names the OUTCOME, which covers both jobs.
        label: finished ? "Rewrote the question for search" : "Preparing the search query",
        // Three states, not two. `rewritten_changed` distinguishes "read it and
        // left it alone" from "came back with nothing", which the string alone
        // cannot: an unchanged rewrite and a raw question are the same text.
        detail: finished
          ? event.rewritten_changed && event.rewritten_question
            ? `"${event.rewritten_question}"`
            : "unchanged"
          : null,
      };
    case "retrieve":
      return {
        key,
        label: finished ? "Searched this agent's corpus" : "Searching this agent's corpus",
        detail: finished ? passagesFound(event) : null,
      };
    case "rerank":
      // Arrives as `finished` only -- reranking happens inside the retriever,
      // which stays the single construction seam and takes no emitter. The line
      // is created by the frame that ends it, which is why `note` upserts.
      return {
        key,
        label: "Reranked the passages",
        detail: event.chunk_count === undefined ? null : `kept ${event.chunk_count}`,
      };
    case "generate":
      /*
        "The model replied" rather than "wrote the answer", and the pedantry is
        load-bearing on a tool turn: step 1 of the agent loop frequently returns
        a tool call and no prose at all. A past tense claiming an answer was
        written would be false on exactly the turns that show this line more than
        once.
      */
      return {
        key,
        label: finished
          ? step > 1
            ? `The model replied (round ${step})`
            : "The model replied"
          : step > 1
            ? `Writing the answer (round ${step})`
            : "Writing the answer",
        detail: null,
      };
    default:
      return null;
  }
}

/** "8 passages", plus the top similarity when the frame carries one. Shown
 *  because this is a teaching artifact and the number is the retrieval story;
 *  never branched on, because the measured on-topic and off-topic bands overlap
 *  and `score_threshold` governs rewriting rather than refusing. */
function passagesFound(event: AskStreamPhase): string | null {
  if (event.chunk_count === undefined) return null;
  const passages = `${event.chunk_count} ${event.chunk_count === 1 ? "passage" : "passages"}`;
  return event.top_score === undefined || event.top_score === null
    ? passages
    : `${passages}, top score ${event.top_score.toFixed(2)}`;
}

/**
 * One tool frame as a line of the log.
 *
 * Keyed on step and tool so the call, its result and its failure are one line
 * rather than three -- **not on `call_id`, which only the `tool_call` frame
 * carries.** Keying the call one way and its result another is how a log ends up
 * showing "Searching the corpus" for the rest of the turn with "Searched the
 * corpus" underneath it.
 *
 * **One call per step is an ASSUMPTION here, not a guarantee, and the difference
 * is worth stating precisely.** `llm.py` sets
 * `disabled_params={"parallel_tool_calls": None}`, which means langchain-openai
 * never *sends* the field -- not that parallel calls are switched off. It is
 * omitted because the parameter is unadvertised on this route and would 404 the
 * request under `require_parameters`, so omitting it is the fix for a routing
 * problem and says nothing about model behaviour. The provider default governs,
 * and `run_agent_loop` itself iterates a list of calls per step and describes a
 * batch larger than one as merely "unusual".
 *
 * So if a step ever does emit two calls to the same tool, the second overwrites
 * the first in this log and the reader sees one search where two ran. That is
 * cosmetic, it has not been observed, and the honest fix if it ever is would be
 * to key the call on `call_id` and fall back to step+tool for the outcome --
 * which is only possible because the `tool_call` frame carries `call_id` and the
 * result frame does not.
 *
 * A failure is amber and NOT an error banner: a tool failure comes back to the
 * model as a message and never as an exception, so the loop carries on and the
 * turn usually still ends in a real answer.
 */
function toolLine(
  event: AskStreamToolCall | AskStreamToolResult | AskStreamToolError,
): ProgressEntry {
  const key = `tool-${event.step}-${event.tool}`;

  if (event.type === "tool_call") {
    return {
      key,
      label: toolVerb(event.tool, false),
      // Named because the difference matters and is invisible otherwise: the
      // loop FORCED this search after the answer admitted a gap. The model did
      // not decide to search, and crediting it with a decision the code made is
      // the misreading this whole feature is most likely to produce.
      detail: event.trigger === "gap_detected" ? "after the answer named a gap" : null,
    };
  }

  return {
    key,
    label: event.ok ? toolVerb(event.tool, true) : `${toolVerb(event.tool, true)} -- failed`,
    detail: event.summary || null,
    failed: !event.ok,
  };
}

/** The two tools this app ships, and a readable fallback for the third one
 *  somebody adds -- an unknown tool must not break the log. */
function toolVerb(tool: string, past: boolean): string {
  if (tool === "search_corpus") return past ? "Searched the corpus" : "Searching the corpus";
  if (tool === "run_python") return past ? "Ran Python" : "Running Python";
  return past ? `Ran ${tool}` : `Running ${tool}`;
}

/**
 * The answer as it is being written: settled markdown, then a plain-text tail.
 *
 * **A prefix of a markdown document is not a markdown document, and nothing
 * throws when you treat it as one.** An unclosed ``` fence turns everything
 * after it into one `<pre>`; a table header without its delimiter row renders as
 * a paragraph of literal pipes; `**bo` renders as two asterisks. remark is
 * total -- there is no string it rejects -- so a zero-console-errors check stays
 * green while the answer is unreadable. That is the error-shaped pass this
 * project keeps meeting: the check passes and the outcome is absent.
 *
 * So the buffer is split where the meaning of a block can no longer change, the
 * settled prefix is parsed as markdown, and the unsettled tail is rendered as
 * pre-wrapped text. Two or three lines of live text with a visible `**` in them
 * is not a defect at that scale; it is a cursor.
 */
function StreamingAnswer({ text }: { text: string }) {
  const { settled, tail } = useMemo(() => splitSettled(text), [text]);

  return (
    <div
      data-testid="streaming-answer"
      // Dimmer than a finished answer on purpose: this text is provisional and
      // is about to be replaced by the server's normalised version. `break-words`
      // for the same reason `Message` carries it -- an unspaced 60-character
      // identifier in the tail has nothing to scroll and takes the document's
      // width with it at 320px.
      className="mt-2 text-sm leading-relaxed break-words text-slate-300"
    >
      {settled && <SettledMarkdown source={settled} />}
      {tail && <span className="whitespace-pre-wrap break-words">{tail}</span>}
      <span
        aria-hidden="true"
        className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-slate-500 align-middle"
      />
    </div>
  );
}

/**
 * Memoised on the settled string, which is what makes this affordable.
 *
 * Re-parsing the whole answer on every token is O(n^2) over the turn plus a full
 * React subtree rebuild each time -- a 1,800-character persona answer is roughly
 * 450 re-parses of an ever-growing string, and a 6 kB answer with a long table
 * would not survive it. The settled prefix changes once per paragraph rather
 * than once per token, so this collapses to O(paragraphs x n).
 */
const SettledMarkdown = memo(Markdown);

/** A fenced code block's opening or closing line. Up to three spaces of
 *  indentation is still a fence; four makes it an indented code block. */
const FENCE_LINE = /^ {0,3}```/gm;

/**
 * Split the buffer at the last point where a block's meaning is fixed.
 *
 * The blank line is the rule: it terminates paragraphs, lists and tables alike,
 * so anything before the last one cannot be changed by what arrives next. The
 * settled half is the WHOLE prefix rather than a block at a time, which is what
 * keeps a loose list or a multi-paragraph quote parsing as one document instead
 * of reflowing when its second half lands.
 *
 * One exception, and it is the one that matters most: an ODD number of fences in
 * the prefix means the last one opens a block that is still being written, and
 * remark would render everything after it as a single `<pre>`. The split moves
 * back to before that fence, so a code block appears as plain text for a second
 * rather than swallowing the answer and popping back out.
 *
 * A partial TABLE needs no rule of its own, and this is worth stating so nobody
 * adds one: a table's rows are consecutive non-blank lines, so a table inside
 * the settled prefix is by construction already terminated by the blank line the
 * prefix was cut at.
 */
function splitSettled(text: string): { settled: string; tail: string } {
  const at = text.lastIndexOf("\n\n");
  if (at === -1) return { settled: "", tail: text };

  let cut = at + 2;
  const fences = text.slice(0, cut).match(FENCE_LINE);
  if (fences && fences.length % 2 === 1) {
    const opened = text.lastIndexOf("```", cut);
    // `lastIndexOf` returning -1 for the newline puts the cut at 0, which is
    // correct: the whole buffer is one unterminated code block.
    cut = opened === -1 ? 0 : text.lastIndexOf("\n", opened) + 1;
  }

  return { settled: text.slice(0, cut), tail: text.slice(cut) };
}

/**
 * The stage a turn is probably in, from measured timings: embedding finishes
 * around 0.4 s, retrieval and reranking by about 1.6 s, and everything after
 * that is generation.
 *
 * **Kept, and demoted to the fallback.** The stream reports what is actually
 * happening, so this is what fills the gap before the first phase frame lands --
 * and the whole turn when no phase frame ever lands, which is the ordinary
 * behaviour against a backend without the streaming route, or behind a proxy
 * that buffered the response into a single blob. It is a forecast, and a
 * forecast is what a client with nothing reported to it is left with.
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
