/**
 * Where a streamed turn's answer gets ADDRESSED, when the turn does not finish.
 *
 * Acceptance criteria AC1-AC5 of
 * `new features/15-failure-paths/01-phantom-conversation.md`. The ids are the
 * prefix of each `it()` name, because the rest of this repo's vitest files name
 * their cases in prose and an acceptance criterion has to be able to cite one.
 *
 * **The subject is a window, not a component.** `POST .../ask/stream` announces
 * the conversation id it created in its FIRST frame, about a tenth of a second
 * in, from inside a transaction that does not commit until the turn ends
 * twenty-five to forty-five seconds later. Every one of those seconds is a
 * moment in which the browser is holding an id no other request can resolve. On
 * a turn that ends in `done` the row commits and the id was always right. On a
 * turn that raises, the transaction rolls back and the id names nothing --
 * forever -- and the client is never told.
 *
 * **So every case here is about the SECOND question, and about which of
 * `askStream` / `askNewStream` it reaches.** Not about component state: the
 * address the next request actually used is the only observation that cannot be
 * satisfied by a build whose `activeId` and whose `activeIdNow` ref have quietly
 * come apart, which is a real failure mode in this file
 * (`new features/15-failure-paths/PLAN.md` section 3.4) and one that throws
 * nothing, logs nothing and simply appends answers to the wrong thread.
 *
 * **Three of the five cases are green today and none of them is filler.** AC1
 * ("the dead id is not reused") is satisfied by at least three different builds:
 * the correct fix, a fix that reverts the address unconditionally, and a build
 * where the promotion inside `onStart`'s `if (wasDraft)` was simply deleted. AC2
 * kills the second and AC3 kills the third. A review that drops either has
 * turned AC1 back into a case written to pass.
 *
 * **Anchors, not line numbers.** Every citation into `AgentChat.tsx` below names
 * a function or a `const`, because the fix this file accepts moved every line in
 * `send`'s `catch` by roughly 190 and invalidated the whole first draft of these
 * comments -- including the ones the harness used to explain itself.
 *
 * Layout is deliberately absent. Whether the settling banner still fits a
 * 390x844 viewport is `scripts/ui_check.py` A5's question; jsdom computes no
 * layout and would answer it while lying -- the same boundary
 * `HandoutCard.test.tsx` and `MentionPopup.test.tsx` draw in their headers.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AskResult,
  AskStreamStart,
  Conversation,
} from "../lib/types.ts";
import type { AskStreamHandlers } from "../lib/api.ts";

const AGENT_ID = "1f0d3b6a-9c2e-4f81-93aa-6d5b7c8e1a02";

/** The id the server announces in `start` and may or may not commit. Short and
 *  unmistakable, because it is what the failure messages have to print. */
const NEW_ID = "c-new";
const QUERY_ID = "3a7c5e19-2b84-4d6f-8e10-5c9a2f7b3d64";

const FIRST = "What is the Ka-band downlink margin?";
const SECOND = "and what about the uplink?";

/**
 * What `chat.list` returns, as a MUTABLE script rather than a constant.
 *
 * This is the mechanism of the whole file. `chat.list` runs as its own request,
 * therefore its own server session, so **a row it returns is by definition
 * committed** -- it is the only committed/rolled-back discriminator a browser
 * can reach. Committing the fixture conversation is therefore expressed by
 * pushing a row into this array from inside the stream script, at the exact
 * moment the server would have committed it.
 *
 * It also has to start EMPTY in every case. `AgentChat`'s mount effect opens the
 * most recently active thread, so a list that already holds a row at mount makes
 * the first question a follow-up in an existing conversation -- `wasDraft` is
 * false, nothing is promoted, and the case measures nothing while passing.
 */
let listRows: Conversation[] = [];

function conversationRow(id: string): Conversation {
  return {
    id,
    agent_id: AGENT_ID,
    title: FIRST,
    message_count: 1,
    created_at: "2026-08-23T09:00:00.000Z",
    updated_at: "2026-08-23T09:00:45.000Z",
  };
}

vi.mock("../lib/api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api.ts")>();
  return {
    // Spread rather than replaced, and that is not laziness: `ApiError` is a
    // real class the module under test compares against, `errorMessage` reads
    // `instanceof Error` off it, and a hand-rolled stand-in would make the error
    // path render something no user ever sees.
    ...actual,
    // `useAgentDocuments` and `handouts.list` both reach the module-local `api`,
    // which the spread above does NOT redirect -- `handouts` closes over the
    // real one. Both are stubbed by hand below for that reason. A missed member
    // leaves an unhandled rejection that can fail an unrelated assertion later
    // in the file, or pass one for the wrong reason.
    api: vi.fn(async () => [] as unknown),
    chat: {
      list: vi.fn(async () => listRows.slice()),
      load: vi.fn(async () => {
        throw new Error(
          "AgentChat.address.test.tsx: no case here opens an existing thread; " +
            "a call to chat.load means the fixture list was not empty at mount",
        );
      }),
      rename: vi.fn(async () => conversationRow(NEW_ID)),
      remove: vi.fn(async () => ({ ok: true })),
      ask: vi.fn(),
      askNew: vi.fn(),
      askStream: vi.fn(),
      askNewStream: vi.fn(),
      trace: vi.fn(async () => []),
    },
    handouts: {
      ...actual.handouts,
      list: vi.fn(async () => []),
    },
  };
});

// Imported AFTER the mock declaration for readability only -- `vi.mock` is
// hoisted above every import in this file by the transform.
import { ApiError, chat } from "../lib/api.ts";
import AgentChat from "./AgentChat.tsx";

const askStream = vi.mocked(chat.askStream);
const askNewStream = vi.mocked(chat.askNewStream);
const list = vi.mocked(chat.list);

// --------------------------------------------------------------------------
// The stream, scripted
// --------------------------------------------------------------------------

function startFrame(): AskStreamStart {
  return { type: "start", seq: 0, query_id: QUERY_ID, conversation_id: NEW_ID };
}

const ANSWER = "The downlink margin is 3.1 dB at maximum slant range.";

function askResult(): AskResult {
  return {
    query_id: QUERY_ID,
    conversation_id: NEW_ID,
    answer: ANSWER,
    refused: false,
    latency_ms: 31_400,
    model_used: "deepseek/deepseek-v4-flash-0731",
    rewritten_question: null,
    rewritten_changed: null,
    citations: [],
    handouts: [],
    tool_steps: 1,
    tool_calls: 1,
  };
}

/** Both stream functions take `(id, question, handlers, signal)`, so one adapter
 *  serves either. The id is deliberately ignored here: which id was passed is
 *  what the assertions read off `mock.calls`, never something the script acts
 *  on. */
type Script = (
  handlers: AskStreamHandlers,
  signal: AbortSignal | undefined,
) => Promise<AskResult>;

function scripted(script: Script) {
  return (
    _id: string,
    _question: string,
    handlers: AskStreamHandlers,
    signal?: AbortSignal,
  ) => script(handlers, signal);
}

/**
 * `start` lands, and then the turn raises.
 *
 * `commits` is the whole difference between AC1 and AC2, and it is the server
 * fact the client cannot see directly: on a turn that raises before
 * `_run_turn`'s single `await db.commit()` the row is rolled back and the id is
 * dead (`commits: false`); on one that raises AFTER it -- `stream.py:247`'s
 * `model_dump`, the session close, `ask.py`'s post-commit handout `db.refresh`
 * loop, the `AskOut(...)` construction -- the row is live and reverting would
 * orphan it (`commits: true`).
 *
 * `ask.py` is cited by anchor rather than by line because feature 02 is rewrapping
 * `run_turn` in the same change set: the commit was at `:1410` when this file was
 * written and is at `:1636` as it ships, and the handout refresh moved with it.
 *
 * The thrown value is a real `ApiError`, because that is literally what the
 * `error`-frame branch of `apiStream` (`api.ts:546`) turns an `error` frame
 * into.
 */
function failsAfterStart({ commits }: { commits: boolean }): Script {
  return async (handlers) => {
    handlers.onStart?.(startFrame());
    // One tick, so the promotion has been applied before the failure arrives.
    // In the real thing this gap is 25-45 seconds.
    await Promise.resolve();
    if (commits) listRows = [conversationRow(NEW_ID)];
    throw new ApiError(
      502,
      "The model provider returned 404 No endpoints found that can handle the requested parameters.",
    );
  };
}

/** The happy path: `start`, one delta, `done`. The row commits, exactly as the
 *  server would, so `refreshList()` on the success path sees it. */
function succeeds(): Script {
  return async (handlers) => {
    handlers.onStart?.(startFrame());
    await Promise.resolve();
    listRows = [conversationRow(NEW_ID)];
    handlers.onToken?.(ANSWER);
    return askResult();
  };
}

/**
 * `start` lands and then nothing, until the reader aborts.
 *
 * No `onToken`, and that is the case rather than an omission: the PRE-FIX cancel
 * branch tested `cancelled && draft`, so a Stop pressed before the first token
 * had an empty draft and fell straight past the branch that settles the address.
 * CLAUDE.md measures 5-8 seconds of quiet before the first token on a tool turn,
 * against a `start` frame at ~0.1 s, so this window is most of a turn's first ten
 * seconds.
 *
 * The mirror image is `AgentChat.test.tsx` AC7, which aborts AFTER one delta:
 * that is the case the old gate DID reach, and emitting the token is what makes
 * it able to see the behaviour the fix narrowed.
 */
function abortsAfterStart(): Script {
  return async (handlers, signal) => {
    handlers.onStart?.(startFrame());
    return await new Promise<AskResult>((_resolve, reject) => {
      signal?.addEventListener("abort", () =>
        // The exact value `fetch` rejects with, because `AgentChat` branches on
        // `cause instanceof DOMException && cause.name === "AbortError"` and a
        // plain `Error` would take the other branch and prove nothing.
        reject(new DOMException("The user aborted a request.", "AbortError")),
      );
    });
  };
}

// --------------------------------------------------------------------------
// Driving it
// --------------------------------------------------------------------------

const sendButton = () => screen.queryByTestId("chat-send");
const settlingBanner = () => screen.queryByTestId("chat-settling");
const streamCalls = () => askStream.mock.calls.length + askNewStream.mock.calls.length;

/**
 * Let everything that is going to happen, happen.
 *
 * Three advances rather than one. `advanceTimersByTimeAsync` flushes the
 * microtask queue as it goes, and a fix that consults `chat.list` from inside
 * the `catch` resolves across several awaits before the composer comes back --
 * one flush would leave the component mid-decision and the assertion after it
 * would be measuring this helper rather than the build.
 */
async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
  });
}

/**
 * Ask a question the way a user does.
 *
 * Enter on the textarea rather than a click on Send, for the reason
 * `MentionPopup.test.tsx` uses the same path: it runs the composer's real
 * `onKeyDown` chain without depending on jsdom's form-submission behaviour.
 * Send's own DISABLED state is asserted directly where it matters (AC4, AC5),
 * so nothing about the button goes uncovered by choosing this path.
 */
async function ask(text: string) {
  const box = screen.getByTestId("chat-input");
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  await settle();
}

/** A fresh draft on an agent with no threads. Every case starts here. */
async function open() {
  render(<AgentChat agentId={AGENT_ID} />);
  await settle();
  // The premise of every case below. Without it the first question is a
  // follow-up and the promotion under test never runs.
  expect(list, "the fixture list must be empty at mount").toHaveBeenCalled();
  expect(chat.load).not.toHaveBeenCalled();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  listRows = [];
  /*
    jsdom implements no scrolling, so `Element.prototype.scrollIntoView` does
    not exist -- and `AgentChat` calls it from `scheduleScroll` and from the
    turn-level scroll effect, on mount and on every turn. Unstubbed, the render
    throws before a single assertion runs and every case in this file goes red
    naming `scrollIntoView`.

    That matters more than a missing stub usually does: a red row LOOKS like a
    caught bug, and this file's whole job is to be red for the right reason
    before the fix is written. Risk R6 in `PLAN.md`.
  */
  Element.prototype.scrollIntoView = vi.fn();
  // A default for both, so a case that only cares about the FIRST turn does not
  // have to describe the second one. Cases override what they are about.
  askStream.mockImplementation(scripted(succeeds()));
  askNewStream.mockImplementation(scripted(succeeds()));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the streamed turn's address", () => {
  it("AC1 -- a rolled-back conversation is not the address of the next question", async () => {
    askNewStream.mockImplementation(scripted(failsAfterStart({ commits: false })));

    await open();
    await ask(FIRST);

    /*
      The turn is over and the composer is back -- the precondition for the
      second question, and what makes the assertions below about the ADDRESS
      rather than about whether the user could type at all.

      Stated as TWO assertions because `chat-send` carries a compound predicate
      (`loadingThread || question.trim() === "" || settling`) and the failed turn
      hands the question back. Without the first line, a change to the
      question-restore rule reddens this case with `expected button to be
      enabled` and names neither the restore nor the address.
    */
    expect(
      screen.getByTestId("chat-input"),
      "AC1 premise: the failed turn handed the question back",
    ).toHaveValue(FIRST);
    expect(sendButton()).toBeEnabled();
    expect(
      listRows.some((row) => row.id === NEW_ID),
      "AC1 premise: the fixture never commits the conversation",
    ).toBe(false);

    await ask(SECOND);

    expect(
      askStream,
      `AC1: the second question was addressed to the rolled-back conversation ${NEW_ID}, ` +
        "which no request can resolve -- the user gets a 404 banner on a thread they can see",
    ).not.toHaveBeenCalled();
    expect(
      askNewStream,
      "AC1: the second question must open a new thread instead",
    ).toHaveBeenCalledTimes(2);
    expect(askNewStream.mock.calls[1][0]).toBe(AGENT_ID);
  });

  it("AC2 -- a conversation that committed and THEN raised keeps its address", async () => {
    askNewStream.mockImplementation(scripted(failsAfterStart({ commits: true })));

    await open();
    await ask(FIRST);

    // Both halves, for the reason spelled out in AC1.
    expect(
      screen.getByTestId("chat-input"),
      "AC2 premise: the failed turn handed the question back",
    ).toHaveValue(FIRST);
    expect(sendButton()).toBeEnabled();
    expect(
      listRows.some((row) => row.id === NEW_ID),
      "AC2 premise: the fixture commits the conversation before raising",
    ).toBe(true);

    await ask(SECOND);

    // The negative half of AC1, inverted. An unconditional revert passes AC1 and
    // fails here, and the cost it would carry in production is a live thread the
    // user was reading silently orphaned -- read as data loss, not as a bug in a
    // revert (PLAN.md R4).
    expect(
      askStream,
      "AC2: the conversation is committed and reachable, so the follow-up belongs in it. " +
        "Reverting here orphans a live thread",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(NEW_ID);
    expect(askNewStream).toHaveBeenCalledTimes(1);
  });

  it("AC3 -- a turn that ends in done keeps the promoted address, and its answer", async () => {
    await open();
    await ask(FIRST);

    /*
      Two assertions, and the first is the one that kills a build with the
      promotion deleted. `onStart` sets `turn.current.address` unconditionally
      and calls `setActiveId`/`activeIdNow` only inside `if (wasDraft)`. Delete
      that second half and the `onAddress()` guard on the success path compares
      a null `activeIdNow` against a promoted address, returns false, and the
      finished answer is DISCARDED -- the sidebar being wrong is the smaller
      half of that bug.
    */
    expect(
      screen.getByText(/downlink margin is 3\.1 dB/),
      "AC3: the finished answer was not folded into the thread",
    ).toBeInTheDocument();

    await ask(SECOND);

    expect(
      askStream,
      "AC3: a successful new thread must accept its follow-up in the same thread",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(NEW_ID);
    expect(askNewStream).toHaveBeenCalledTimes(1);
  });

  it("AC4 -- Stop before the first token settles the address instead of stranding it", async () => {
    askNewStream.mockImplementation(scripted(abortsAfterStart()));

    await open();
    await ask(FIRST);

    // Mid-turn: Stop is on screen instead of Send, and nothing has streamed.
    const stop = screen.getByTestId("chat-stop");
    expect(screen.queryByTestId("streaming-answer")).toBeNull();

    await act(async () => {
      fireEvent.click(stop);
    });
    await settle();

    /*
      Stop stops the READER, not the agent: the drain loop's docstring in
      `stream.py` ("WHAT HAPPENS WHEN THE CLIENT DISCONNECTS") records that the
      turn "runs to completion and commits. Nothing is cancelled." So the row IS
      coming, and the right response is to WAIT for it -- never to revert, which
      would open a second conversation while the first commits underneath and
      split the thread across two rows (PLAN.md section 3.4, R7).

      Both assertions are on the ABSENCE of the outcome that was wanted, which is
      loop.md T2: nothing throws on this path today, no console error, the
      composer simply comes back usable and addressed to a row nobody else can
      resolve.
    */
    expect(
      settlingBanner(),
      "AC4: no chat-settling banner after a token-less Stop, so the composer is " +
        "handed back addressed to a conversation the server has not committed",
    ).not.toBeNull();
    /*
      The composer's CONTENTS first, and the asymmetry is why only this one
      needs it. Send is `disabled={loadingThread || question.trim() === "" ||
      settling}`, so `toBeDisabled()` is satisfied by an EMPTY BOX on its own --
      a false pass -- while `toBeEnabled()` (AC5, and the release assertion
      below) merely fails noisily on one. A token-less stop hands the question
      back, so this is the fact that leaves `settling` as the only thing the
      assertion under it can be about.
    */
    expect(
      screen.getByTestId("chat-input"),
      "AC4 premise: the stopped turn handed the question back, so the disabled " +
        "assertion below is about the settling wait and not about an empty composer",
    ).toHaveValue(FIRST);
    expect(
      sendButton(),
      "AC4: Send must refuse an address the server has not committed yet",
    ).toBeDisabled();

    // The turn finishes server-side and the row commits. This is the ONLY
    // evidence a browser can have of that, and it is what releases the wait.
    listRows = [conversationRow(NEW_ID)];
    await settle(3_000);

    expect(settlingBanner(), "AC4: the banner must clear once the row exists").toBeNull();
    expect(sendButton()).toBeEnabled();

    await ask(SECOND);

    expect(
      askStream,
      "AC4: once settled, the follow-up belongs in the thread that was stopped",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(NEW_ID);
  });

  it("AC5 -- a stop whose row never appears still hands the composer back", async () => {
    askNewStream.mockImplementation(scripted(abortsAfterStart()));

    await open();
    await ask(FIRST);

    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-stop"));
    });
    await settle();

    /*
      The poll budget in full: 20 attempts, 3 s apart, ~60 s. `listRows` is never
      given the row, so every attempt fails and the wait gives up.

      **This case asserts USABILITY, and deliberately not a revert.** Give-up is
      not evidence of a rollback: CLAUDE.md measures persona turns at 30-60 s, so
      a turn that outran the budget is still running and about to commit.
      Reverting here is the split-thread failure again. What must be true is only
      that the user is not locked out of their own agent -- and a fix that sets
      `unsettledId` without a release path fails exactly this.
    */
    await settle(70_000);

    expect(
      sendButton(),
      "AC5: the poll budget expired and the composer is still refusing input -- " +
        "the user is locked out of their own agent",
    ).toBeEnabled();

    await ask(SECOND);

    expect(
      streamCalls(),
      "AC5: the second question was swallowed by send()'s settling guard",
    ).toBe(2);
  });
});
