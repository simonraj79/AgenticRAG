# 01 — The phantom conversation

Feature 01 of [PLAN.md](PLAN.md), and the last of the three to be built — see PLAN.md §4 for
why a client fix is the highest layer and why it waits on 02.

> **Filename note.** PLAN.md's header and its §4 table link this file as
> `01-streamed-turn-address.md`. It is the same feature under the name the build step
> assigned; the two links in PLAN.md want repointing at `01-phantom-conversation.md`, which is
> a one-line edit in a file this session does not own. Recorded here rather than fixed here so
> the dead link is findable.
>
> The **acceptance** harness is `frontend/src/views/AgentChat.address.test.tsx`, per
> PLAN.md §3.7, which settles that name explicitly and names the audit's other spelling as the
> error. Every acceptance criterion below cites the settled name.
>
> `frontend/src/views/AgentChat.test.tsx` **also exists and is not that error.** It is the
> general view file PLAN.md §3.7 says the settled name "leaves room for", and it holds the
> five edge cases AC6–AC10 — the four conditions on the revert, and the cost of one of them.
> Two files rather than one because their fixtures are opposites: the acceptance file's
> `chat.load` throws by design, so that no case there can open an existing thread, and AC7 and
> AC9 need exactly that.

---

## What the user gets

A question asked on a brand-new thread that fails — an OpenRouter 404, a sandbox death, a
failing commit — costs the user that answer and nothing else. Today it also costs them the
thread: the browser is still holding the conversation id the server announced and then rolled
back, so the *next* question is addressed to a row that never existed and comes back as a red
banner about a thread the user can see on their own screen.

And a turn stopped in its first few seconds — before a single token — hands the composer back
with that same uncommitted id and no warning. The banner that exists for exactly this
situation never renders, because the code path that raises it sits behind a condition only a
*partly answered* turn can satisfy.

---

## Why this is one feature and not two

Both holes are the same promotion (`AgentChat.tsx:592-603`) going unaccompanied on the way
out, and both are reached through the same `catch` (`:713`). They are separated only by which
branch of that `catch` fails to fire:

| | hole (a) | hole (b) |
|---|---|---|
| How the turn ends | `error` frame → `ApiError` thrown at `api.ts:562` | Stop pressed before the first token |
| `cancelled` at `:714` | false | **true** |
| `draft` at `:715` | may be `""` or partial | `""` — nothing streamed yet |
| `cancelled && draft` (`:729`) | false | **false**, because `draft` is falsy |
| Reaches `settleAddress` (`:754`) | no | **no** |
| Is the server row coming? | **No — the transaction rolled back** | **Yes — the turn is still running** |
| Correct response | **revert** the address | **settle** it, as `:731-753` already argues |

The last two rows are why they must not share a code path, and they are the whole design.
PLAN.md §3.4 owns that contract; it is not restated here.

---

## Technical detail

> **Every `AgentChat.tsx` line number in this section is HEAD's — the code BEFORE the fix —
> and every one of them is now wrong by roughly 190 lines.** They are left as written because
> this section is the design argument that was made against that build, and rewriting the
> citations would quietly rewrite the argument's subject. The shipped positions are in
> [As built — the fix](#as-built--the-fix-and-what-a-review-then-found), cited by function
> name rather than by line, for the reason this note exists at all: a 188-line insertion moved
> every pointer in the feature, including the ones the acceptance harness used to explain
> itself. `ask.py`'s numbers moved too, and for a second reason — feature 02 rewrapped
> `run_turn` in the same change set, so the single commit went from `:1410` to `:1636`.

### 1. The window, restated only as far as the client needs it

`stream.py:206-214` builds the `Conversation` with a fresh `uuid4()` inside a session it
opened itself, `flush`es it, and `ask.py:739-743` emits the one and only `start` frame
carrying that id — at roughly 0.1 s. `ask.py:1410` is the *sole* commit on the turn path.
Between those two moments the row exists in exactly one uncommitted transaction, and
`SessionLocal`'s `async with` rolls it back on any raise.

The client is told about the row 25–45 seconds before it becomes real, and nothing tells it
when the row stops being coming. That asymmetry is the feature.

**No backend change.** PLAN.md §7's first bullet: `stream.py` creating the row inside its own
session and letting it roll back is correct. S37 exists to keep it that way, not to change it.

### 2. What the client already has, and it is exactly one thing

`chat.list(agentId)` runs as its own request, therefore its own server session, therefore
**a row it returns is committed**. That is the only committed/rolled-back discriminator the
browser can reach, and `settleAddress` (`:400-425`) is already built around it — it polls the
list every 3 s, up to 20 attempts, holding `unsettledId` while it waits and releasing it
either when the row appears or on give-up.

So the fix does not need new evidence. It needs the existing evidence consulted on a path that
currently consults nothing.

### 3. Hole (a) — revert, on evidence

In the `catch` at `:713`, on the non-cancelled branch, when **all** of these hold:

- the turn began as a draft and was promoted this turn — the promotion at `:600-603` is the
  only writer of that fact, so the fix reads it rather than re-deriving it;
- the user has not navigated away (`onAddress()`, already checked at `:716`);
- `chat.list(agentId)` has been asked and **has not** returned the promoted id.

…then `activeId` returns to `null` and **`activeIdNow.current` is written in the same
statement**, per PLAN.md §3.4's first mechanical rule. Four existing writers pair them
(`:499/500`, `:516/517`, `:601/602`, `:694/695`); the revert is the fifth.

The list request is awaited *before* the composer is handed back, so there is no window in
which Send is enabled against an address the client is still deciding about. That falls out of
the existing control flow rather than needing a new guard: the `finally` at `:765-770` is what
clears `pending`, and it runs after the `catch` completes.

**The evidence requirement is not defensive padding.** Four concrete paths commit and *then*
raise — `stream.py:247`'s `result.model_dump(mode="json")` and the session close, both outside
the `async with` and inside the `try`; and `ask.py:1422-1423`'s handout `db.refresh` loop and
the `AskOut(...)` construction at `:1425`, both after the commit. On every one of them the row
is real and reverting orphans a live thread. That is AC2, and R4.

### 4. Hole (b) — settle, never revert

The branch at `:729` reads `cancelled && draft`. It is asking two questions at once, and only
one of them is about text:

- **Is there text to keep?** That is `draft`, and it decides whether a truncated bubble is
  folded into the thread. Unchanged.
- **Is there an uncommitted address to settle?** That is `turn.current.address` having been
  promoted this turn, and it is true whether or not a token arrived.

So `settleAddress(turn.current.address)` moves out from under the `draft` condition. Nothing
else about the abort path changes: no revert, no error banner, and the three existing
`settling` behaviours (`send` refuses at `:548`, Send is disabled at `:1331`, `chat-settling`
renders at `:1346`) start applying to a case they were written for and never saw.

**Why give-up must not revert**, restated because it is the single most tempting wrong edit:
`settleAddress` gives up at 20 × 3 s ≈ 60 s, and CLAUDE.md measures persona turns at 30–60 s.
On the abort path the turn is *still running* and will commit. Reverting on give-up would open
a second conversation while the first commits underneath it. AC5 therefore asserts the
composer is **usable**, never that the address reverted. PLAN.md §3.4, R7.

### 5. What the promotion is, so the fix cannot be "delete it"

`onStart` writes `turn.current.address = event.conversation_id` *unconditionally* (`:599`) and
calls `setActiveId`/`activeIdNow` only `if (wasDraft)` (`:600-603`). Deleting the second half
does not merely make the sidebar wrong for the length of the turn: `onAddress()` at `:716`
then compares a null `activeIdNow.current` against a promoted `turn.current.address`, returns
false, and **the finished answer is discarded**. AC3 asserts the answer is on screen *and*
that the following question is addressed to the promoted id — both, because either alone is
weaker than the pair. R5.

The same early return fires inside the `catch` at `:716`, one line above the `setQuestion`
restore at `:759-761`, so a build without the promotion also silently eats the user's typing
on every failed first turn. That is a third symptom, it is not in the audit, and it was found
by making AC3 red rather than by reading the code — see the as-built notes.

---

## Contracts consumed

By reference into [PLAN.md](PLAN.md). None restated.

| Contract | Where |
|---|---|
| A revert is evidence-based; hole (a) and hole (b) do not share a code path | §3.4 |
| `activeIdNow.current` is written in the same statement as every `setActiveId`; `settling` stays derived | §3.4 |
| No setting, no migration, no trace event | §3.1, §3.2, §3.8 |
| Harness id ledger — AC1–AC5 in the new vitest file; S37/S38 in `agentic_check.py`; S34–S36 reserved and not to be taken | §3.7 |
| Three states, never two; a committed/rolled-back assertion is made from a SECOND, independent session | §3.6 |
| Build order — 01 is built last, after 02, because S37/S38 drive the `run_turn` that 02 rewraps | §4 |
| `frontend/src/lib/api.ts` is not touched; the documented 404 route-fallback stays | §2, §7 |

---

## Acceptance criteria

Every criterion names a harness file and a case id. The **kind** column is PLAN.md §4.1's
vocabulary, and it is what makes "watch it fail" honest for the cases that cannot be red.

### `frontend/src/views/AgentChat.address.test.tsx` — run by `cd frontend && npm test`

The ids are carried as a prefix inside each `it()` name, because this repo's vitest files name
their cases in prose and an acceptance criterion needs something citable.

| Case | Kind | Asserts |
|---|---|---|
| **AC1** | **RED today** | New thread. `start` announces `c-new`, then an `error` frame, and `chat.list` **never** returns `c-new`. The next send calls `chat.askNewStream(agentId, …)` and **never** `chat.askStream("c-new", …)` |
| **AC2** | GREEN today, **RED under an unconditional revert** | Identical, except `chat.list` **does** return `c-new` from the moment `start` lands — the committed-then-raised case. The next send **must** call `chat.askStream("c-new", …)` |
| **AC3** | GREEN today, **RED with the promotion deleted** | A stream ending in `done` keeps the promoted id: the answer is folded into the thread, and the next send is addressed to `c-new` |
| **AC4** | **RED today** | Stop after `start`, before any token. `chat-settling` renders and `chat-send` is disabled; once `chat.list` returns `c-new` the banner clears and the next send calls `chat.askStream("c-new", …)` |
| **AC5** | GREEN today, **RED under a fix that never releases `unsettledId`** | Stop before the first token, and `chat.list` never returns `c-new`. After the poll budget expires the composer is **usable** — `chat-send` is enabled and a keystroke issues a second stream request. It asserts usability, never a revert (R7) |

**AC1, AC2 and AC3 are one indivisible triple.** AC1 alone is satisfied by three different
builds — the correct fix, an unconditional revert, and a build with the promotion deleted.
AC2 kills the second; AC3 kills the third. Neither is optional, and a review that drops one
has re-created a case written to pass.

### `frontend/src/views/AgentChat.test.tsx` — run by the same `cd frontend && npm test`

**Added in the refine pass, and the ids are the finding as much as the cases are.** These
five were first written as R1–R3 in a file with no ledger entry at all — colliding with
PLAN.md §8's own risk register (R1 a metering double-write, R2 a commit flag set too early,
R3 a second pool connection, running to R14), in the same folder, cited in the same source
comment. A grep for an id in this change set has to return one thing, so they continue the
AC series and PLAN.md §3.7's ledger gains the row that claims them.

The four revert conditions get one case each. **The fourth had none until a reviewer replaced
it with `if (true)` and watched all 54 frontend tests stay green** — which is the whole
argument for the rule that a condition names a case or a later reader deletes it as
defensive.

| Case | Kind | Asserts |
|---|---|---|
| **AC6** | **RED against HEAD** | Condition 2. `start` lands, then the body ends with no terminal frame (`ApiError` status 0). The address is **settled**, not reverted: `chat-settling` renders, the restored question is asserted *before* `chat-send` is read, and once `chat.list` returns `c-new` the follow-up goes to `chat.askStream("c-new", …)` |
| **AC7** | **RED against HEAD** | Condition 1. A stop in an **existing** thread, **after one delta** — the token is the case, because the pre-fix gate was `cancelled && draft` and a token-less stop took the same no-settle path the fix takes. No `chat-settling`, and the follow-up stays in `c-old` |
| **AC8** | GREEN today, **RED under a fix that reads an unreadable list as an absent row** | Condition 3's failure half. `chat.list` throws on the verdict request; the address is **kept**, because "the list did not contain it" and "the list could not be read" are different states and only the first is evidence |
| **AC9** | GREEN today, **RED under `if (true)` on the fourth gate** | Condition 4. The verdict `chat.list` is held open, the user opens another thread inside that round trip, and the verdict then says "not committed". The revert must not land: no `New conversation` placeholder, and the follow-up goes to `c-other` |
| **AC10** | **RED against HEAD**, and **RED under a give-up that reverts** | The COST of condition 2, measured. Two banners for one turn are asserted together, the 60 s budget is run out on the indeterminate path — which no case did before — and the address survives it: the follow-up still goes to `chat.askStream("c-new", …)` |

**AC6/AC7 and AC1/AC4 are two pairs whose only variable is one thing each.** AC6 against AC1
varies `ApiError.status` and nothing else; AC7 against AC4 varies who created the address and
nothing else. That is what makes each of them a measurement of a condition rather than of the
feature as a whole.

**Every assertion about `chat-send` is preceded by one about the composer's contents**, and
that is not decoration: `disabled={loadingThread || question.trim() === "" || settling}`, so
`toBeDisabled()` is satisfied by an empty box on its own and `toBeEnabled()` fails on one. The
first draft of AC7 asserted `toBeEnabled()` immediately after a stop that folds a truncated
bubble and deliberately does *not* hand the question back — so it would have gone red against
the correct build for a reason that has nothing to do with its subject.

### `scripts/agentic_check.py` — S37 and S38

Both are **PREMISE** cases in PLAN.md §4.1's sense: they characterise the server the client
fix rests on, and S37 is *true today*. If S37 were red the client fix would be unnecessary.

| Case | Asserts |
|---|---|
| **S37** | Drive the real `stream._run_turn_streamed` with a failure injected **before generation** (no model call, no cost). Read `conversation_id` off the `start` frame drained from the queue, then assert **from a second, independent session** that no `Conversation` with that id exists |
| **S38** | The same path on a turn that succeeds: the `start` frame's `conversation_id` is present from the independent session, and the `done` frame's `result.conversation_id` equals it |

**S37 without S38 is passed by a build that never creates the row at all.** Same shape as the
AC1/AC3 pair, backend side. Neither may be written without the other.

**They are written in the build session, not in this one**, for two reasons stated in
PLAN.md §4: they drive `run_turn`, which feature 02 is rewrapping in a `finally` as this is
written, so writing them first means writing them twice; and `scripts/agentic_check.py` is
outside this session's file ownership. Nothing is lost from the watch-it-fail step by
deferring them — a PREMISE case is green by construction, so neither could have been red
today.

---

## What must keep working

| What | Where it is asserted | Why it is at risk |
|---|---|---|
| The frontend baseline — 5 test files / 46 tests | `cd frontend && npm test` | A new file must not perturb it. Measured green at audit time and again immediately before AC1 was written |
| `agentic_check.py` **S1** (`s1_classic_path`) — `emit=None` leaves the trace shape byte-identical | `scripts/agentic_check.py` | `run_turn`'s docstring claims that structurally. Nothing in this feature edits its single `emit(events.START, …)` block, and nothing may |
| `agentic_check.py` **S26** (`s26_critic_exempts_pedagogy`) — the only existing user of the emit seam, via `frame_collector()` | `scripts/agentic_check.py` | It reads frame order and payloads. A changed `start` payload or a reordered frame reddens it. The `start` frame is read here, never changed |
| `ui_check.py` **A5** — the composer is inside the viewport at 390×844, scrollTop 0 | `python scripts/ui_check.py` | `chat-settling` sits inside the composer form and grows it. Hole (b)'s fix makes that banner render in a case it never rendered in before; it must still fit |
| `ui_check.py` **A8** — every control ≥ 44px, desktop and phone | `python scripts/ui_check.py` | Send gains no new class here, but any edit near the `chat-send` button is one `min-h-11` away from breaking the convention |
| `ui_check.py` **A10** — zero console errors across all four viewports | `python scripts/ui_check.py` | A revert that races `activeIdNow` can surface as a React warning or an unhandled rejection from an in-flight `chat.load`. R6's cousin, in the browser |
| `MentionPopup.test.tsx` — Enter still sends with the popup shut, and after a completed mention | `cd frontend && npm test` | Both drive the composer's submit path, which is where a new send guard would go. Nothing here adds one: `send`'s existing `if (settling) return` is reused rather than joined |
| The three existing `settling` behaviours — `send`'s `if (settling) return`, the `chat-send` `disabled` predicate, the `chat-settling` banner | **AC4** and **AC10**, and `ui_check.py` A5 | A fix that reverted on the abort path instead of settling would delete all three, and every error-shaped check would stay green |
| A follow-up in an existing thread still lands in that thread | **AC2** (same mechanism: the address is kept), and the by-eye step in PLAN.md §5.1 | `wasDraft` is false for a follow-up, so there is nothing to revert — but a revert written without the draft condition would revert one |

---

## What this deliberately does not do

- **It does not touch `frontend/src/lib/api.ts`.** The `error` frame already arrives as a
  thrown `ApiError` with the right shape (`api.ts:544-562`). The two-round-trip cost of the
  documented 404 route-fallback (`if (response.status === 404 && onMissingRoute)`, `api.ts:469`) is R14 and is not this feature's: the fix repairs
  the address so the fallback is never entered, rather than making the error legible.
- **It does not revert on the abort path**, and it does not revert on `settleAddress`'s
  give-up. PLAN.md §3.4, §7, R7.
- **It does not promote `settling` to stored state.** The derivation
  `activeId !== null && activeId === unsettledId` is what keeps every *other* thread usable
  while one settles — which is also the whole of the escape hatch from the settling wait's
  sixty-second budget, so it is load-bearing rather than tidy (AC10).
- **It does not change `backend/app/api/stream.py`.** PLAN.md §7.
- **It does not add a setting, a migration or a trace event.** PLAN.md §3.1, §3.2, §3.8.
- **It does not test layout in jsdom.** Whether the second banner state still fits a 390×844
  viewport is `ui_check.py` A5's question; jsdom computes no layout and would answer it while
  lying. `HandoutCard.test.tsx` and `MentionPopup.test.tsx` both carry that boundary in their
  headers and this file keeps it.

---

## As built — the red run

> **Line numbers in this section are HEAD's too** — see the note under *Technical detail*. It
> is a record of a run against a build that no longer exists, so its citations are left as
> they were taken.

*Phase 4 record, 2026-08-23. No fix is written; `AgentChat.tsx` is byte-identical to `HEAD`
(`git diff -- frontend/src/views/AgentChat.tsx` is empty), and `frontend/src/lib/api.ts` was
never opened for editing.*

**AC1 and AC4 are red; AC2, AC3 and AC5 are green, which is exactly what the criteria table
predicts.** The three green cases are not filler and they were not taken on trust — each was
made to go red under the wrong build it exists to kill, and those three experiments are the
second half of this record.

### The red run, verbatim

`cd frontend && npm test`:

```
 ❯ src/views/AgentChat.address.test.tsx (5 tests | 2 failed) 185ms
     × AC1 -- a rolled-back conversation is not the address of the next question 81ms
     × AC4 -- Stop before the first token settles the address instead of stranding it 17ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/views/AgentChat.address.test.tsx > the streamed turn's address > AC1 -- a rolled-back conversation is not the address of the next question
AssertionError: AC1: the second question was addressed to the rolled-back conversation c-new, which no request can resolve -- the user gets a 404 banner on a thread they can see: expected "vi.fn()" to not be called at all, but actually been called 1 times

Received:

  1st vi.fn() call:

    Array [
      "c-new",
      "and what about the uplink?",
      Object {
        "onAnswerReset": [Function onAnswerReset],
        "onPhase": [Function onPhase],
        "onStart": [Function onStart],
        "onToken": [Function onToken],
        "onTool": [Function onTool],
      },
      AbortSignal { ... },
    ]

Number of calls: 1

 FAIL  src/views/AgentChat.address.test.tsx > the streamed turn's address > AC4 -- Stop before the first token settles the address instead of stranding it
AssertionError: AC4: no chat-settling banner after a token-less Stop, so the composer is handed back addressed to a conversation the server has not committed: expected null not to be null

 Test Files  1 failed | 5 passed (6)
      Tests  2 failed | 49 passed (51)
```

Three things in that output are load-bearing:

- **AC1's failure prints `"c-new"` and the second question as the arguments of a
  `chat.askStream` call that should not exist.** That is the defect itself — the browser
  addressing its next question to a row the server rolled back — and it is not a render error
  and not a missing mock member. **R6 discharged**: jsdom has no
  `Element.prototype.scrollIntoView`, which `AgentChat.tsx` calls from `scheduleScroll` and
  from the turn-level scroll effect, so an
  unstubbed render throws before any assertion runs and the red row names `scrollIntoView`
  rather than the defect. The stub is in the harness's `beforeEach` with the reason written
  beside it, and this failure text is the proof it was not what made AC1 red.
- **AC4's failure is `expected null not to be null`** on `queryByTestId("chat-settling")` —
  the **absence** of the banner. That is [loop.md](../loop.md) T2's shape exactly: the
  assertion is on the missing outcome, never on the presence of an error. Nothing throws on
  that path today, no console error appears, and the composer simply comes back usable and
  wrong.
- **`1 failed | 5 passed (6)` files, `49 passed` tests.** The baseline of 5 files / 46 tests
  is intact — 46 + 5 new = 51 — so the new file perturbs nothing. `npx tsc -b` also exits 0,
  so `npm run build` is unaffected.

### The three green guards, each made to go red

Each experiment patched `AgentChat.tsx`, ran the file, and was reverted; the file is pristine
now. This is the half of build.md rule 3 that is usually skipped — a case asserting a thing
does *not* happen is worthless until something has made it happen.

| Experiment | The wrong build | Result |
|---|---|---|
| **A** | Revert unconditionally: `setActiveId(null); activeIdNow.current = null;` immediately after `setError` in the `catch` | **AC1 goes GREEN and AC2 goes RED** — `AC2: the conversation is committed and reachable, so the follow-up belongs in it. Reverting here orphans a live thread: expected "vi.fn()" to be called 1 times, but got 0 times`. R4 confirmed |
| **B** | Delete the promotion — the two lines `setActiveId(event.conversation_id); activeIdNow.current = …` inside `onStart`'s `if (wasDraft)` | **All five red.** AC3 fails on its own designed assertion — `Unable to find an element with the text: /downlink margin is 3\.1 dB/` — i.e. the finished answer was discarded, which is the *larger* half of that bug and the one prose would have missed. R5 confirmed, and then some |
| **C** | Settle without ever releasing: `if (cancelled && turn.current.address) setUnsettledId(turn.current.address);` and no poll | **AC4's first half goes GREEN** (banner up, Send disabled) and AC4 then fails at `the banner must clear once the row exists`, while **AC5 goes RED**: `the poll budget expired and the composer is still refusing input -- the user is locked out of their own agent`. R7 confirmed |

**Experiment B is the correction to R5 that is worth carrying.** R5 predicts AC1 is *passed*
by a build with the promotion deleted, so AC3 is needed to kill it. Measured, AC1 fails there
too — but on its precondition (`expect(sendButton()).toBeEnabled()`), not on the address. The
mechanism is the one §5 of this file describes: with no promotion, `onAddress()` at `:716` is
false inside the `catch` as well, so the early return skips the `setQuestion` restore at
`:759-761` and the user's typing is silently thrown away. That is a *third* symptom of
deleting the promotion, it is not in the audit, and only AC3's answer-on-screen assertion
names the real one. The register's reasoning stands; its prediction of which case catches it
was one case short.

### Two harness properties that were not obvious before writing it

Recorded because the next person mounting a view of this size in jsdom will hit both.

- **`chat.list` has to be a mutable script, not a constant.** `AgentChat`'s mount effect
  (`:424-448`) opens the most recently active thread, so a list holding a row at mount makes
  the first question a *follow-up* — `wasDraft` is false, nothing is promoted, and the case
  passes having measured nothing. Every case here starts the list empty and adds `c-new` only
  from inside the stream script, which is also the honest way to express "the server committed
  it, and then the turn raised": the commit happens at the moment the server would have made
  it, in the same script that then throws.
- **The composer is driven by `keyDown` Enter, not by clicking Send.** Both are real user
  paths; only the first is independent of jsdom's form-submission behaviour, and it is the one
  `MentionPopup.test.tsx` already pins. Send's *disabled* state is asserted directly in AC4
  and AC5, so nothing about the button goes uncovered by that choice.

### What is not in this record

**S37 and S38 are not written.** PLAN.md §4 orders them after feature 02, whose `finally`
around `run_turn` they would have to be rewritten against, and `scripts/agentic_check.py` is
outside this session's file ownership. Nothing is lost from the watch-it-fail step: both are
PREMISE cases in PLAN.md §4.1's sense and green by construction, so neither could have been
red today. They remain acceptance criteria of this feature and must land with the fix.

---

## As built — the fix, and what a review then found

*Phase 5 record, 2026-08-23. The fix shipped, every harness went green, and an adversarial
review then found six real problems in it anyway — several proven by deleting a line and
watching the whole suite stay green. This section is the repair of those, and the mutation
runs that show each repair is a measurement rather than a claim.*

### Where the shipped code is, by anchor

The 188-line insertion into `send`'s `catch` moved every line number in this document, so
these are named rather than numbered. All in `frontend/src/views/AgentChat.tsx`.

| Thing | Anchor |
|---|---|
| The promotion | `onStart`'s `if (wasDraft)` — `setActiveId` and `activeIdNow.current` in adjacent statements |
| "did THIS turn create the address" | `const promotedAddress = threadId === null ? turn.current.address : null` |
| Hole (b), the abort path | `if (promotedAddress) settleAddress(promotedAddress)`, **outside** the `draft` test |
| Hole (a), the failure path | `const serverReportedFailure = …` and the `if (promotedAddress) { … }` block under it |
| The evidence | `conversationCommitted` — one `chat.list`, and a list that cannot be read answers "committed" |
| The wait | `settleAddress` — 20 attempts × 3 s, releasing on the row's appearance or on give-up |

### The four conditions on the revert, and the case that pins each

| # | Condition | Case |
|---|---|---|
| 1 | This turn CREATED the address | `AgentChat.test.tsx` **AC7** / `AgentChat.address.test.tsx` **AC1** |
| 2 | The server reported the turn DEAD (`ApiError.status !== 0`) | **AC6** / **AC1**, and its cost is **AC10** |
| 3 | The row is ABSENT from a freshly fetched list | **AC2** / **AC8** |
| 4 | The user is still ON that address when the verdict returns | **AC9** |

### The four mutation runs

Each patched one line, ran `cd frontend && npm test` from the repo root's `frontend/`, and was
reverted. The green baseline is **7 files / 56 tests**; before this pass it was 7 / 54.

| Mutation | Red | Failure text |
|---|---|---|
| **A.** `AgentChat.tsx` reverted to `HEAD` — the real pre-fix build | AC6, AC7, AC9 (premise), AC10 (premise); AC1, AC4 in the acceptance file | `AC7: an existing thread was marked unsettled, so the fix waits for a commit that happened before the turn started: expected <p data-testid="chat-settling" …> to be null` |
| **B.** the fourth gate → `if (true)` | **AC9 alone**, 55/56 | `AC9: the address was reverted onto a draft while another thread's transcript is on screen — the user's next question opens a third row: expected <li …> to be null` |
| **C.** `settleAddress`'s give-up also reverts (`setActiveId(null); activeIdNow.current = null;`) | **AC10 alone**, 55/56 — **AC5 stayed green**, which is the point of adding AC10 | `AC10: give-up threw the address away instead of merely releasing it, which splits the thread across two rows the moment the turn commits: expected "vi.fn()" to be called 1 times, but got 0 times` |
| **D.** `promotedAddress` drops its `threadId === null` half | **AC7 alone**, 55/56 | same text as A's AC7 row |

**Mutation A is the one that mattered, and the correction it carries is the review's headline
finding.** AC7 (then called R2) was cited in a source comment as pinning the narrowing of the
settling wait, and it was **green against the build it was cited as narrowing**. The pre-fix
gate was `if (cancelled && draft)`, so the wait ran only on a stop that had already produced
OUTPUT — and R2's script never called `onToken`, so it took the same no-settle path the fixed
build takes. One delta before the abort puts the case back on the branch it is about, at the
cost of the `toBeEnabled()` assertion beside it: a stop WITH output folds a truncated bubble
and deliberately does not restore the question, so that half was measuring the composer's
contents. It now types the next question first.

**Mutation C is the one that justifies a new case rather than a wider old one.** AC5 already
runs the poll budget out — on the ABORT path, asserting only that *some* second stream request
issues. It counts `askStream` and `askNewStream` together, so a give-up that reverted would
pass it. AC10 runs the budget out on the path the fix ADDED and asserts *which* function the
follow-up reached.

### The compound `disabled` predicate, and the four assertions it was quietly deciding

`chat-send` is `disabled={loadingThread || question.trim() === "" || settling}`, so
**`toBeDisabled()` is satisfied by an empty composer on its own** and `toBeEnabled()` fails on
one. Four assertions in these two files read that button as a statement about `settling` while
actually depending on the *question-restore* rule, which is a different behaviour on a
different line. Each now asserts the composer's contents first, so the red names the half that
broke.

| Mutation | Before | After |
|---|---|---|
| **E.** the token-less stop stops handing the question back | AC4 red on `expected button to be enabled`-shaped noise; AC5 red | AC4 red on `AC4 premise: the stopped turn handed the question back`; AC5 unchanged |
| **F.** the failure path stops handing the question back | AC1, AC2, AC6, AC8 red, none naming the restore | the same four red, each on `… premise: the failed turn handed the question back` |

The asymmetry is worth keeping: `toBeDisabled()` on an empty composer is a **false pass** and
`toBeEnabled()` on one is merely noise, so AC4 is the assertion that actually needed the
premise and the other three needed the legibility.

### The two costs, and why both are now in the harness

The `finally`'s comment disclosed one of them and it was the cheaper one, which reads as
though the other does not exist.

1. **One extra `chat.list` round trip** with the in-flight bubble still up, on a promoted turn
   the server reported dead. This was disclosed from the start.
2. **Up to ~60 s of a thread that refuses input**, on a promoted turn that failed
   *indeterminately* — `status === 0`, i.e. a dropped connection or an intermediary's idle
   timeout closing the body with no terminal frame. During it the user sees **two banners for
   one turn**: `api.ts:574`'s "It may still be recorded" and the settling banner's "still
   finishing that turn on the server". Both are true. **AC10 asserts the pair**, so an edit to
   either string arrives at a red case rather than at a screenshot.

   *One correction to the finding that raised this, checked rather than assumed:* the two are
   not stacked on each other. `ErrorBanner` renders inside the thread pane, which is the
   `overflow-y-auto flex-1` element, and `chat-settling` renders inside the composer form
   below it — so the pair costs the composer **no** height and `ui_check.py` A5 is not newly at
   risk. What is true is that one turn produces two separate pieces of copy about its own
   status, at opposite ends of the column.

The escape hatch is structural rather than added: `settling` is derived as
`activeId !== null && activeId === unsettledId`, so New chat and every other thread stay
usable throughout. That derivation is now named in "What this deliberately does not do" as
load-bearing for this reason, not only for the one it was written for.

### Two things this pass did NOT change, and why

- **No behaviour changed.** Every repair here is a case, a comment or a citation. The reviewer
  found no wrong behaviour in the shipped fix — it found behaviour that nothing was measuring,
  which is the more expensive of the two to leave.
- **`frontend/src/lib/api.ts` is still untouched.** The two-banner overlap could be softened by
  changing the status-0 message, and that is R14's territory and a product decision: the fix
  repairs the address so the 404 route-fallback is never entered, rather than making the error
  legible.

### Still not written

**S37 and S38.** The reason in "What is not in this record" stands — they are PREMISE cases in
`scripts/agentic_check.py`, which is outside this session's ownership, and they must land with
this feature.
