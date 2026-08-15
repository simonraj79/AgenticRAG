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
import type { AskResult, ChatMessage, Conversation } from "../lib/types.ts";
import { ConfirmDeleteButton, ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import Message from "../components/Message.tsx";

export default function AgentChat({
  agentId,
  onAnswered,
}: {
  agentId: string;
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
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const bottom = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLTextAreaElement | null>(null);
  // Read by async handlers that need to know where the user is NOW, which the
  // closure's captured `activeId` cannot tell them -- that is the value at the
  // moment the request was sent, and comparing the two is the whole check.
  const activeIdNow = useRef<string | null>(null);
  useEffect(() => {
    activeIdNow.current = activeId;
  }, [activeId]);

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
    setPending({ question: text, threadId });
    setQuestion("");
    setError(null);

    try {
      const result: AskResult = threadId
        ? await chat.ask(threadId, text)
        : await chat.askNew(agentId, text);

      // The user moved to another thread while this was generating. The turn is
      // saved; it just does not belong on this screen.
      if (activeIdNow.current !== threadId) return;

      setActiveId(result.conversation_id);
      activeIdNow.current = result.conversation_id;
      setMessages((current) => [...current, toMessage(text, result)]);
      onAnswered?.(result.query_id);
      // The title is derived server-side from the first question, and
      // `updated_at` is what the list sorts on -- neither is knowable here.
      void refreshList().catch(() => {
        // A stale sidebar is not worth an error banner over a delivered answer.
      });
    } catch (cause) {
      if (activeIdNow.current === threadId) {
        setError(errorMessage(cause));
        // Handed back rather than swallowed: a 15-second wait that ends in a
        // 500 should not also cost the user their typing. Only into an empty
        // box, though -- they may have started typing the next question while
        // this one was in flight, and restoring over that would be worse than
        // losing it.
        setQuestion((current) => (current === "" ? text : current));
      }
    } finally {
      setPending(null);
    }
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

  return (
    <div className="grid gap-4 md:grid-cols-[15rem_1fr]">
      <aside className="flex max-h-[70vh] flex-col rounded-xl border border-slate-800 bg-slate-900/30 p-2">
        <button
          type="button"
          data-testid="conversation-new"
          onClick={startDraft}
          className="mb-2 rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 transition hover:border-slate-600"
        >
          + New chat
        </button>

        <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto">
          {activeId === null && (
            <li className="rounded-md border border-emerald-800/60 bg-emerald-950/20 px-3 py-2 text-sm text-emerald-300">
              New conversation
              <span className="block text-xs text-slate-500">saved on your first question</span>
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
                    <span className="block text-xs text-slate-500">
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
                      className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs text-slate-300 transition hover:border-slate-600"
                    >
                      Rename
                    </button>
                    <ConfirmDeleteButton
                      testId="conversation-delete"
                      label="Delete"
                      confirmLabel="Confirm"
                      onConfirm={() => void remove(conversation.id)}
                    />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </aside>

      <section className="flex h-[70vh] flex-col rounded-xl border border-slate-800 bg-slate-900/30">
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="mb-4">
            <ErrorBanner error={error} />
          </div>

          {loadingThread && <Spinner label="Loading conversation" />}

          {!loadingThread && messages.length === 0 && !showPending && (
            <p className="rounded-lg border border-dashed border-slate-800 px-4 py-10 text-center text-sm text-slate-500">
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
                  <div className="max-w-[85%] rounded-2xl rounded-br-sm border border-slate-700 bg-slate-800/70 px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-100">
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
                  <Spinner label={`${stageFor(elapsed, messages.length > 0)}...`} />
                  <p className="mt-2 text-xs text-slate-500">
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
          <div className="flex items-end gap-2">
            <textarea
              id="chat-question"
              ref={input}
              data-testid="chat-input"
              rows={2}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Ask a question. Enter sends, Shift+Enter adds a line."
              className="min-h-[3rem] flex-1 resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
            <button
              type="submit"
              data-testid="chat-send"
              disabled={pending !== null || loadingThread || question.trim() === ""}
              className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
            >
              {pending ? "Thinking..." : "Send"}
            </button>
          </div>
        </form>
      </section>
    </div>
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
