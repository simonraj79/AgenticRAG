/**
 * The FIVE decisions the address fix makes that AC1-AC5 do not pin.
 *
 * `AgentChat.address.test.tsx` is the acceptance harness for defect 01 and it
 * owns the headline behaviour: a rolled-back conversation is not reused (AC1), a
 * committed one is (AC2), a finished turn keeps its address and its answer
 * (AC3), and a Stop before the first token settles rather than strands (AC4,
 * AC5). This file holds the EDGES of the same fix -- the four gates on the
 * revert, one case each, plus the one cost the fix introduces.
 *
 * **The ids continue the AC series on purpose, and the rename is the finding.**
 * These cases were first written as R1-R3, which collided with
 * `new features/15-failure-paths/PLAN.md`'s own risk register (R1 = a metering
 * double-write, R2 = a commit flag set too early, R3 = a second pool
 * connection, running to R14) -- in the same folder, cited in the same source
 * comment, with nothing to disambiguate them. A grep for an id in this change
 * set must return one thing. AC6-AC10 are claimed for this file in PLAN.md
 * section 3.7's ledger beside AC1-AC5.
 *
 * **Four of the five are half of a pair, and the other half is in the
 * acceptance file.** That is deliberate and it is not a loophole: both files run
 * under `npm test`, so the pair is always executed together, and each case below
 * names the partner that a "just delete the feature" build would fail.
 *
 *   AC6  an INDETERMINATE failure waits instead of reverting  <-> AC1 (a
 *        server-reported failure with no committed row DOES revert). The only
 *        variable between them is `ApiError.status`.
 *   AC7  a stop in an EXISTING thread raises no settling wait <-> AC4 (a stop on
 *        a thread this turn created DOES raise one). The only variable is who
 *        created the address.
 *   AC8  an UNREADABLE conversation list keeps the address    <-> AC1 (a
 *        readable list that lacks the row reverts). The only variable is whether
 *        the evidence could be obtained.
 *   AC9  a revert that arrives AFTER the user opened another thread does not
 *        land                                                 <-> AC1 (the same
 *        verdict, on a user who stayed put, DOES land). The only variable is
 *        where `activeIdNow.current` points when the verdict returns.
 *   AC10 stands alone: it is the only case that runs the settling budget out on
 *        the path the fix ADDED, and it asserts the cost as well as the release.
 *
 * Each pair exists because the negative half alone is satisfied by a build with
 * the whole mechanism removed -- the scar this repo carries from
 * `refusal_pass = 0/2`, where the measurement punished the behaviour the persona
 * existed to produce.
 *
 * **On `chat-send` and what its disabled state actually measures.** Send is
 * `disabled={loadingThread || question.trim() === "" || settling}`, so
 * `toBeDisabled()` is satisfied by an EMPTY COMPOSER on its own and
 * `toBeEnabled()` fails on one. Every assertion about that button here is
 * therefore preceded by an assertion about what is in the composer: either the
 * question was restored (the paths that restore it) or this case typed one in.
 * Without that, a change to the question-restore rule reddens or greens a case
 * whose subject is the settling state, and the failure message names neither.
 *
 * Layout is absent here for the same reason it is absent there: jsdom computes
 * no layout and would answer a question about the banner's fit while lying.
 * `scripts/ui_check.py` owns that.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AskResult,
  AskStreamStart,
  ChatMessage,
  Conversation,
  ConversationDetail,
} from "../lib/types.ts";
import type { AskStreamHandlers } from "../lib/api.ts";

const AGENT_ID = "1f0d3b6a-9c2e-4f81-93aa-6d5b7c8e1a02";
/** The id the stream announces on a DRAFT turn -- the one whose commit is in
 *  doubt for the whole of a turn. */
const NEW_ID = "c-new";
/** A thread that was committed long before any of this ran. AC7's whole subject:
 *  nothing about it is ever in doubt, so nothing may ever wait for it. */
const OLD_ID = "c-old";
/** A second committed thread, for the one case where the user NAVIGATES while
 *  the browser is deciding about `c-new`. Distinct from `OLD_ID` so a failure
 *  message says which of the two the address landed on. */
const OTHER_ID = "c-other";
const QUERY_ID = "3a7c5e19-2b84-4d6f-8e10-5c9a2f7b3d64";

const FIRST = "What is the Ka-band downlink margin?";
const SECOND = "and what about the uplink?";

/** See the same fixture in `AgentChat.address.test.tsx`: `chat.list` is its own
 *  request and therefore its own server session, so a row it returns is by
 *  definition committed. Pushing a row in here is how a case says "the server
 *  committed it, now". */
let listRows: Conversation[] = [];
/** Whether `chat.list` is currently answering at all. AC8's mechanism -- the
 *  evidence exists and cannot be read, which is a different state from the
 *  evidence saying no. */
let listFails = false;
/**
 * A one-shot hold on the NEXT `chat.list` call, or `null`.
 *
 * AC9's mechanism, and the only way to reach the fourth gate from outside the
 * component. That gate re-checks `activeIdNow.current` after the verdict request
 * returns, so a case that wants to exercise it has to do something DURING the
 * round trip -- which means the round trip has to be holdable. Armed by the
 * stream script at the moment it raises, so exactly one call is held: the
 * verdict, never the mount effect's list and never a `refreshList`.
 */
let listGate: Promise<void> | null = null;
let openListGate: (() => void) | null = null;

function armListGate() {
  listGate = new Promise<void>((resolve) => {
    openListGate = resolve;
  });
}

function conversationRow(id: string, title = FIRST): Conversation {
  return {
    id,
    agent_id: AGENT_ID,
    title,
    message_count: 1,
    created_at: "2026-08-23T09:00:00.000Z",
    updated_at: "2026-08-23T09:00:45.000Z",
  };
}

/** One turn of history per fixture thread, whose answer NAMES its thread. AC9
 *  needs a transcript on screen that visibly belongs to somebody: the harm the
 *  fourth gate prevents is a draft address underneath another thread's
 *  messages, and an empty thread cannot show it. */
function loadedMessage(id: string): ChatMessage {
  return {
    query_id: `q-${id}`,
    question: FIRST,
    answer: `The stored transcript of ${id}.`,
    refused: false,
    latency_ms: 1_000,
    model_used: "deepseek/deepseek-v4-flash-0731",
    rewritten_question: null,
    rewritten_changed: null,
    created_at: "2026-08-23T09:00:45.000Z",
    citations: [],
    handouts: [],
    tool_steps: 0,
    tool_calls: 0,
  };
}

vi.mock("../lib/api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api.ts")>();
  return {
    // Spread rather than replaced: `ApiError` is a real class that `AgentChat`
    // now reads `status` off, and a hand-rolled stand-in would fail the
    // `instanceof` and route every case down the indeterminate branch -- which
    // would make AC6 pass while proving nothing at all.
    ...actual,
    // `useAgentDocuments` and `handouts.list` reach the module-local `api`,
    // which the spread does not redirect. An unstubbed member leaves an
    // unhandled rejection that can fail an unrelated case later in the file.
    api: vi.fn(async () => [] as unknown),
    chat: {
      list: vi.fn(async () => {
        if (listFails) throw new actual.ApiError(500, "Could not list conversations");
        if (listGate) {
          const held = listGate;
          // One call only. A gate left armed would hold the settling poll too,
          // and the case would be measuring this fixture.
          listGate = null;
          await held;
        }
        return listRows.slice();
      }),
      load: vi.fn(async (id: string): Promise<ConversationDetail> => {
        const row = listRows.find((candidate) => candidate.id === id);
        if (!row) throw new Error(`AgentChat.test.tsx: no fixture thread ${id}`);
        return { ...row, messages: [loadedMessage(id)] };
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

// After the mock declaration for readability only -- `vi.mock` is hoisted.
import { ApiError, chat } from "../lib/api.ts";
import AgentChat from "./AgentChat.tsx";

const askStream = vi.mocked(chat.askStream);
const askNewStream = vi.mocked(chat.askNewStream);
const list = vi.mocked(chat.list);

// --------------------------------------------------------------------------
// The stream, scripted
// --------------------------------------------------------------------------

const ANSWER = "The downlink margin is 3.1 dB at maximum slant range.";
/** The prefix a stopped turn keeps. Deliberately a fragment of `ANSWER`, the way
 *  a real truncation is. */
const PARTIAL = "The downlink margin is ";

function startFrame(conversationId: string): AskStreamStart {
  return {
    type: "start",
    seq: 0,
    query_id: QUERY_ID,
    conversation_id: conversationId,
  };
}

function askResult(conversationId: string): AskResult {
  return {
    query_id: QUERY_ID,
    conversation_id: conversationId,
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

type Script = (
  handlers: AskStreamHandlers,
  signal: AbortSignal | undefined,
) => Promise<AskResult>;

/** Both stream functions take `(id, question, handlers, signal)`. Which id was
 *  passed is read off `mock.calls` rather than acted on here. */
function scripted(script: Script) {
  return (
    _id: string,
    _question: string,
    handlers: AskStreamHandlers,
    signal?: AbortSignal,
  ) => script(handlers, signal);
}

/**
 * `start` lands, then the connection gives out -- no terminal frame either way.
 *
 * This is `api.ts:574` verbatim, and its status of 0 is the whole case. The
 * message it carries says the quiet part out loud: the turn "may still be
 * recorded". Nothing about the row was decided, so nothing may be reverted.
 */
function streamEndsWithoutTerminalFrame(): Script {
  return async (handlers) => {
    handlers.onStart?.(startFrame(NEW_ID));
    await Promise.resolve();
    throw new ApiError(
      0,
      "The answer stream ended before the turn finished. It may still be recorded -- reload the conversation to check.",
    );
  };
}

/** `start` lands, then the server itself reports the turn dead -- `stream.py`'s
 *  `except Exception` branch, which runs only after its `async with
 *  SessionLocal()` has already unwound.
 *
 *  `holdVerdict` arms the one-shot hold on the next `chat.list`, which is the
 *  verdict request the `catch` is about to make. AC9 uses it to stop time
 *  exactly where the fourth gate lives. */
function serverReportsFailure({ holdVerdict = false } = {}): Script {
  return async (handlers) => {
    handlers.onStart?.(startFrame(NEW_ID));
    await Promise.resolve();
    if (holdVerdict) armListGate();
    throw new ApiError(
      500,
      "RuntimeError: 404 No endpoints found that can handle the requested parameters.",
    );
  };
}

/**
 * `start` lands, ONE delta reaches the screen, and then nothing until the reader
 * aborts.
 *
 * **The token is the case, and its absence was the defect in this file.** The
 * pre-fix gate was `if (cancelled && draft)`, so the settling wait ran only on a
 * stop that had already produced output. A script that never called `onToken`
 * therefore took the same no-settle path the fixed build takes, and AC7 was
 * green against the build it was cited as measuring -- measured 2026-08-23 by
 * checking out `HEAD`'s `AgentChat.tsx` and running this file. One delta puts
 * the case back on the branch it is about.
 */
function abortsAfterOneToken(conversationId: string): Script {
  return async (handlers, signal) => {
    handlers.onStart?.(startFrame(conversationId));
    handlers.onToken?.(PARTIAL);
    return await new Promise<AskResult>((_resolve, reject) => {
      signal?.addEventListener("abort", () =>
        // The exact value `fetch` rejects with; a plain `Error` takes the other
        // branch in `AgentChat` and would prove nothing.
        reject(new DOMException("The user aborted a request.", "AbortError")),
      );
    });
  };
}

function succeeds(conversationId: string): Script {
  return async (handlers) => {
    handlers.onStart?.(startFrame(conversationId));
    await Promise.resolve();
    if (!listRows.some((row) => row.id === conversationId)) {
      listRows = [...listRows, conversationRow(conversationId)];
    }
    handlers.onToken?.(ANSWER);
    return askResult(conversationId);
  };
}

/**
 * The default for `askStream`: the follow-up succeeds in whatever thread it was
 * ADDRESSED to.
 *
 * Reading the id off the call rather than pinning one is what stops a fixture
 * rescuing a build that got the address wrong: a `succeeds(NEW_ID)` default
 * answers in `c-new` even when the component asked about `c-old`, and the
 * `done` frame would then quietly promote the view onto a thread nobody
 * requested.
 */
function succeedsInThreadAddressed() {
  return (
    id: string,
    _question: string,
    handlers: AskStreamHandlers,
    signal?: AbortSignal,
  ) => succeeds(id)(handlers, signal);
}

// --------------------------------------------------------------------------
// Driving it
// --------------------------------------------------------------------------

const composer = () => screen.getByTestId("chat-input");
const sendButton = () => screen.queryByTestId("chat-send");
const settlingBanner = () => screen.queryByTestId("chat-settling");

/** Three advances rather than one: a verdict reached by consulting `chat.list`
 *  resolves across several awaits before the composer comes back, and one flush
 *  would leave the component mid-decision so the assertion after it measures
 *  this helper instead of the build. */
async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
  });
}

/** Typing and sending are separate steps here, unlike in the acceptance file,
 *  because several cases need to assert Send's state with a KNOWN composer
 *  between the two -- see the note on the compound `disabled` predicate in this
 *  file's header. */
async function type(text: string) {
  await act(async () => {
    fireEvent.change(composer(), { target: { value: text } });
  });
}

async function submit() {
  await act(async () => {
    fireEvent.keyDown(composer(), { key: "Enter" });
  });
  await settle();
}

async function ask(text: string) {
  await type(text);
  await submit();
}

/** Release the held `chat.list` and let the `catch` finish deciding. */
async function releaseVerdict() {
  await act(async () => {
    openListGate?.();
    openListGate = null;
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function open(railTab: "sources" | "threads" = "sources") {
  render(<AgentChat agentId={AGENT_ID} initialRailTab={railTab} />);
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  listRows = [];
  listFails = false;
  listGate = null;
  openListGate = null;
  // jsdom implements no scrolling, and `AgentChat` calls `scrollIntoView` from
  // `scheduleScroll` and from the turn-level scroll effect, on mount and on
  // every turn. Unstubbed, every case here goes red naming a missing method --
  // which LOOKS like a caught defect.
  Element.prototype.scrollIntoView = vi.fn();
  askStream.mockImplementation(succeedsInThreadAddressed());
  askNewStream.mockImplementation(scripted(succeeds(NEW_ID)));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("what the address fix must NOT do", () => {
  it("AC6 -- a stream that ended without a terminal frame waits, it does not revert", async () => {
    askNewStream.mockImplementation(scripted(streamEndsWithoutTerminalFrame()));

    await open();
    await ask(FIRST);

    // The failure is reported. Asserted first because everything below is about
    // a build that handled the failure, never about one that swallowed it.
    expect(screen.getByTestId("error-banner")).toBeInTheDocument();
    expect(
      listRows.some((row) => row.id === NEW_ID),
      "AC6 premise: the row has not been committed AT THE MOMENT OF THE FAILURE, " +
        "which is exactly the state AC1 reverts on",
    ).toBe(false);

    /*
      And yet this must not revert, because the two failures know different
      things. AC1's fixture throws an `ApiError` with a real status, which can
      only have come from `api.ts:546` -- the server's own error frame, emitted
      after `stream.py`'s session has unwound, so the transaction has finished
      resolving before the client is told. This one throws status 0, which
      `api.ts` uses for a dropped connection and for a body that ended with no
      terminal frame, and whose message tells the user the turn "may still be
      recorded". `stream.py` is explicit that it is: "the turn runs to completion
      and commits. Nothing is cancelled."

      So the row is absent and still coming, which is Stop's situation exactly --
      and the answer is Stop's answer. Reverting here would open a second
      conversation while the first commits underneath it and split the thread
      across two rows with nothing on screen to explain it (PLAN.md R7).
    */
    expect(
      settlingBanner(),
      "AC6: no settling banner, so a network blip was read as a rollback and the " +
        "address was thrown away while the turn was still running",
    ).not.toBeNull();
    // The composer's CONTENT, asserted before its button. This path restores the
    // question, so `toBeDisabled()` below is a statement about `settling`; on an
    // empty composer it would pass under any build at all.
    expect(
      composer(),
      "AC6 premise: the failed turn handed the question back, so the only thing " +
        "left that can disable Send is the settling wait",
    ).toHaveValue(FIRST);
    expect(sendButton(), "AC6: Send must refuse an address still in doubt").toBeDisabled();

    // The turn finishes server-side, exactly as its own error message said it
    // might. This is the only evidence a browser can have of that.
    listRows = [conversationRow(NEW_ID)];
    await settle(3_000);

    expect(settlingBanner()).toBeNull();
    expect(sendButton()).toBeEnabled();

    await ask(SECOND);

    expect(
      askStream,
      "AC6: the follow-up belongs in the thread that survived, not in a new one",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(NEW_ID);
    expect(
      askNewStream,
      "AC6: a second askNewStream means the address was reverted after all",
    ).toHaveBeenCalledTimes(1);
  });

  it("AC7 -- a stop in an existing thread does not park the composer behind a settling wait", async () => {
    // The thread exists before the component mounts, so the mount effect opens
    // it and the turn below is a follow-up rather than a promotion.
    listRows = [conversationRow(OLD_ID, "An older thread")];
    askStream.mockImplementation(scripted(abortsAfterOneToken(OLD_ID)));

    await open();
    expect(
      chat.load,
      "AC7 premise: the fixture thread must be open, or this is a draft turn and " +
        "the case measures AC4 again",
    ).toHaveBeenCalledWith(OLD_ID);

    await ask(FIRST);
    // The second premise, and the one this case was missing. The pre-fix gate
    // was `cancelled && draft`, so a token-less stop took the SAME no-settle
    // path the fixed build takes and this case could not see the behaviour it
    // is cited for. Output on screen is what opens that gate.
    expect(
      // `queryBy`, not `getBy`, throughout this file's premises: `getBy` THROWS
      // before `expect` is reached, so the message explaining what the premise
      // was for never reaches the reader of the red run.
      screen.queryByTestId("streaming-answer"),
      "AC7 premise: a delta must have reached the screen, or the pre-fix build " +
        "passes this case without ever running the branch it is about",
    ).not.toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-stop"));
    });
    await settle();

    /*
      AC4's mechanism must not fire here, and the difference is WHO CREATED THE
      ADDRESS rather than what the user pressed. `c-old` was committed long
      before this turn began: `owned_conversation` resolves it in any session,
      the next question cannot 404 on it, and there is nothing whatsoever to wait
      for.

      The cost of getting this wrong is quiet, which is why it gets a case. A
      settling wait started here disables the composer for a full poll interval
      and explains itself with a banner saying the server is "still finishing
      that turn" -- of a thread it finished yesterday. The user is locked out of
      a working thread by the repair for a bug that thread cannot have.
    */
    expect(
      screen.queryByText(new RegExp(PARTIAL.trim())),
      "AC7 premise: the truncated bubble is what proves the draft was non-empty " +
        "at the gate, which is the whole difference from AC4",
    ).not.toBeNull();
    expect(
      settlingBanner(),
      "AC7: an existing thread was marked unsettled, so the fix waits for a commit " +
        "that happened before the turn started",
    ).toBeNull();

    /*
      Send is asserted only once there is something to send, and that is not
      pedantry. A stop WITH output folds a truncated bubble and deliberately does
      NOT hand the question back -- so the composer here is empty by design, and
      `toBeEnabled()` on it fails against the CORRECT build for a reason that has
      nothing to do with settling. Typing first leaves exactly one thing that
      could still disable the button.
    */
    await type(SECOND);
    expect(
      sendButton(),
      "AC7: the composer must come straight back on a thread whose id was never in doubt",
    ).toBeEnabled();

    // The follow-up answers wherever it was addressed -- see
    // `succeedsInThreadAddressed`. The aborting script has done its job.
    askStream.mockImplementation(succeedsInThreadAddressed());
    await submit();

    // The positive half, in-file: the address is not merely un-blocked, it is
    // still the right one and still usable.
    expect(askStream).toHaveBeenCalledTimes(2);
    expect(askStream.mock.calls[1][0]).toBe(OLD_ID);
    expect(
      askNewStream,
      "AC7: the follow-up opened a new thread, which is the revert firing where " +
        "there was nothing to revert",
    ).not.toHaveBeenCalled();
  });

  it("AC8 -- an unreadable conversation list keeps the address rather than guessing", async () => {
    askNewStream.mockImplementation(scripted(serverReportsFailure()));

    await open();
    // Readable at mount and unreadable afterwards, so the failure under test is
    // the VERDICT request and not the arrival of the component.
    listFails = true;

    await ask(FIRST);

    expect(screen.getByTestId("error-banner")).toBeInTheDocument();

    /*
      This is the same failure AC1 reverts on -- a server-reported death, no
      committed row -- with one thing changed: the evidence could not be read.
      "The list did not contain it" and "the list could not be fetched" are
      different states, and only the first one is evidence.

      They resolve differently because the two mistakes are not symmetric.
      Keeping a dead address costs one 404 banner on the next question, which New
      chat escapes in a click. Reverting a live one orphans a committed thread
      holding the user's question, silently, and reads as data loss rather than
      as a bug in a revert (PLAN.md R4). A build that treats an unreadable list
      as an absent row takes the expensive mistake every time a request times
      out.
    */
    expect(
      composer(),
      "AC8 premise: the question was handed back, so Send's state below is about " +
        "the address and not about an empty box",
    ).toHaveValue(FIRST);
    expect(sendButton(), "AC8: the composer must still come back").toBeEnabled();

    await ask(SECOND);

    expect(
      askStream,
      "AC8: an unreadable list was read as proof of a rollback, and the address was " +
        "thrown away on no evidence",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(NEW_ID);
    expect(askNewStream).toHaveBeenCalledTimes(1);
  });

  it("AC9 -- a verdict that lands after the user opened another thread does not revert it", async () => {
    // Two committed threads' worth of history, one of them open at mount.
    listRows = [conversationRow(OTHER_ID, "A thread that already existed")];
    askNewStream.mockImplementation(scripted(serverReportsFailure({ holdVerdict: true })));

    await open("threads");
    expect(chat.load, "AC9 premise: the fixture thread is open").toHaveBeenCalledWith(
      OTHER_ID,
    );

    // Back to a draft, so the question below PROMOTES -- the only shape from
    // which the revert is reachable at all.
    await act(async () => {
      fireEvent.click(screen.getByTestId("conversation-new"));
    });
    await ask(FIRST);

    /*
      The turn has failed and the verdict request is being held open. That is the
      premise, and Stop still being on screen is how it is read: `pending` is
      cleared in the `finally`, which cannot run while the `catch` is suspended
      on `conversationCommitted`'s await. Without this the click below would
      happen AFTER the verdict and the case would measure nothing.
    */
    expect(
      screen.queryByTestId("chat-stop"),
      "AC9 premise: the verdict round trip must still be in flight, or the " +
        "navigation below happens after the decision it is meant to race",
    ).not.toBeNull();
    expect(list, "AC9 premise: mount list, then the held verdict list").toHaveBeenCalledTimes(
      2,
    );

    // The user does exactly what the round trip gives them time for.
    await act(async () => {
      fireEvent.click(screen.getByTestId("conversation-item"));
    });
    await settle();
    expect(
      screen.queryByText(`The stored transcript of ${OTHER_ID}.`),
      "AC9 premise: the other thread is open and its transcript is on screen",
    ).not.toBeNull();

    // Now the verdict lands: `c-new` is NOT in the list, so every earlier
    // condition on the revert is satisfied and only the fourth one is left.
    await releaseVerdict();

    /*
      The revert must not fire, and the harm if it does is not merely a wrong
      sidebar. `activeId` would go to `null` while `messages` holds the OTHER
      thread's transcript -- a state the source comment beside the revert calls
      unreachable and leaves `messages` alone because of -- so the user reads
      thread B under a "New conversation" label, and their next question opens a
      THIRD conversation. That is PLAN.md R4/R7's split thread arriving through
      the repair rather than through the defect.

      `New conversation` is the placeholder the rail renders for `activeId ===
      null`, so its absence is the assertion, and it is on the missing outcome
      rather than on an error: nothing throws on the ungated path.
    */
    expect(
      screen.queryByText("New conversation"),
      "AC9: the address was reverted onto a draft while another thread's " +
        "transcript is on screen -- the user's next question opens a third row",
    ).toBeNull();

    await ask(SECOND);

    expect(
      askStream,
      "AC9: the follow-up belongs in the thread the user opened",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(OTHER_ID);
    expect(
      askNewStream,
      "AC9: a second askNewStream means the revert landed on a user who had " +
        "already navigated away from the address being reverted",
    ).toHaveBeenCalledTimes(1);
  });

  it("AC10 -- an indeterminate failure releases the composer when the row never arrives, and still does not revert", async () => {
    askNewStream.mockImplementation(scripted(streamEndsWithoutTerminalFrame()));

    await open();
    await ask(FIRST);

    /*
      THE COST OF THE FIX, ASSERTED RATHER THAN LEFT IN PROSE.

      Routing `status === 0` into `settleAddress` buys the R7 protection AC6
      measures, and it is not free: the user sees TWO banners for one turn. The
      error banner carries `api.ts:574`'s text -- the turn "may still be
      recorded" -- and the settling banner directly under it says the agent is
      "still finishing that turn on the server". Both are true and they read as
      one hedge and one reassurance stacked on each other.

      It is asserted here so that a future edit to either string has to come to
      this case and read why they coexist, rather than discovering the overlap in
      a screenshot. `scripts/ui_check.py` A5 owns whether the pair still FITS a
      390x844 viewport; jsdom computes no layout and cannot answer that.
    */
    expect(screen.getByTestId("error-banner")).toBeInTheDocument();
    expect(
      settlingBanner(),
      "AC10 premise: the indeterminate path must actually enter the settling wait",
    ).not.toBeNull();

    /*
      And the second half of the cost: the budget is 20 attempts three seconds
      apart, so on a turn whose row never commits this thread refuses input for
      about sixty seconds. The user is not locked out of the AGENT -- `settling`
      is derived as `activeId === unsettledId`, so New chat and every other
      thread stay usable -- but this thread is unusable for the whole of it, and
      nothing before this case ran the budget out on this path. AC5 runs it out
      on the ABORT path, where the user chose to wait.
    */
    await settle(70_000);

    expect(
      settlingBanner(),
      "AC10: the poll budget expired and the thread is still refusing input",
    ).toBeNull();
    await type(SECOND);
    expect(sendButton()).toBeEnabled();
    await submit();

    /*
      Give-up RELEASES; it never reverts. CLAUDE.md measures persona turns at
      30-60 s, so a turn that outran a 60 s budget is very likely still running
      and about to commit -- exactly the reasoning AC5 carries on the abort path,
      and the one place a reader is most tempted to "finish the job" by throwing
      the address away at the end of the wait.
    */
    expect(
      askStream,
      "AC10: give-up threw the address away instead of merely releasing it, which " +
        "splits the thread across two rows the moment the turn commits",
    ).toHaveBeenCalledTimes(1);
    expect(askStream.mock.calls[0][0]).toBe(NEW_ID);
    expect(askNewStream).toHaveBeenCalledTimes(1);
  });
});
