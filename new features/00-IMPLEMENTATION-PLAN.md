# Implementation plan — agentic tools + Handouts

Master plan for four shipped-together features. Each has its own document; this one holds
the **audit**, the **contracts** the feature documents agree on, and the **sequence** the
build follows.

| # | Feature | Document |
|---|---|---|
| 1 | Agentic tool loop — the model decides, up to a bounded step count | [01-agentic-tool-loop.md](01-agentic-tool-loop.md) |
| 2 | `run_python` — sandboxed code interpreter | [02-code-interpreter.md](02-code-interpreter.md) |
| 3 | `search_corpus` — retrieval the model drives itself | [03-corpus-search-tool.md](03-corpus-search-tool.md) |
| 4 | **Handouts** — the generated-asset panel | [04-handouts-panel.md](04-handouts-panel.md) |
| 5 | UI/UX overhaul, desktop and mobile | [05-ui-ux-overhaul.md](05-ui-ux-overhaul.md) |
| 6 | Test plan — offline, frontend unit, agentic and Playwright layers | [06-test-plan.md](06-test-plan.md) |

---

## 1. What this adds, in one paragraph

Groundwork answers questions from a corpus. Today a turn is a fixed pipeline:
contextualise, retrieve, generate. After this change the generation step becomes a
**bounded agent loop** — the model is handed two tools and decides for itself whether to
search the corpus again or to write and run Python. Anything that Python produces (a chart,
a slide deck, a data table) is captured as a **Handout**: a durable, downloadable asset
listed in a panel beside the chat, alongside handouts the user asks for directly from a
menu of recipes. The trace gains `TOOL_CALL` / `TOOL_RESULT` events so the loop is as
inspectable as the rest of the pipeline, which is the whole point of this codebase.

**Why these two tools and not others.** `search_corpus` is the agentic form of PRD open
item 7 — the score-triggered rewrite loop that was specified and never built. Handing the
model the retriever lets it do multi-hop questions the fixed pipeline structurally cannot
("compare X and Y" needs two searches). `run_python` is the one tool that changes what the
product can *output* rather than what it can *find*: text in, artefacts out.

---

## 2. Audit — what exists today

Three parallel audits read the whole codebase. Findings that constrain this build:

### 2.1 The turn path

```
POST /api/agents/{agent_id}/ask          POST /api/conversations/{id}/ask
                  \                                    /
                   \                                  /
                    ask.run_turn(db, *, agent, user, session, conversation, question)
                          |
                          |  history = _recent_history(db, conversation.id)   # 6 turns
                          |  Query row inserted + db.flush()   (trace_events.query_id NOT NULL)
                          |  trace = TraceRecorder(db, query.id)
                          |
                          +--> pipeline.answer_question(agent, question, history=history)
                          |         1. contextualize_question()   -> rewritten | None
                          |         2. retriever.aretrieve()      -> Retrieval
                          |         3. chain.ainvoke()            -> answer text     <-- INSERT POINT
                          |
                          |  trace.record(REWRITE | RETRIEVE | SCORE_CHECK | RERANK | GENERATE | REFUSE)
                          |  QueryChunk rows, CitationOut list, normalise_citation_markers()
                          |  ONE db.commit()
                          v
                       AskOut
```

**The split that must survive.** `pipeline.py` never touches the database; `run_turn` does
all the writing in a single transaction. `TraceRecorder` is constructed in `run_turn` and
is not reachable from the pipeline. So the loop **accumulates tool activity as plain data on
`AnswerResult`**, and `run_turn` turns it into rows. Every feature document assumes this.

### 2.2 Exact signatures the build writes against

```python
# app/rag/pipeline.py
async def answer_question(agent, question, *, rerank=None, history=None, **model_overrides) -> AnswerResult
@dataclass
class AnswerResult:
    question: str; answer: str
    documents: list[Document]; scored: list[tuple[Document, float]]
    rewritten_question: str | None; model: str; reranked: bool
    latency_ms: int; contextualize_ms: int; retrieval_ms: int; generation_ms: int
    # properties: search_query, top_score, similarity_scores, rerank_scores

# app/rag/llm.py  -- the ONLY place a chat model is constructed
def build_chat_model(model, *, temperature=None, top_p=None, top_k=None,
                     max_tokens=None, reasoning_effort=None, **overrides) -> ChatOpenAI

# app/rag/retriever.py  -- the ONLY place a retriever is built
async def aretrieve(agent, query, *, rerank=None, k=None) -> Retrieval
def get_vector_store(agent) -> PineconeVectorStore     # namespace=agent.namespace, never a string

# app/rag/trace.py
class TraceRecorder:
    def record(self, event_type, payload=None, score=None, duration_ms=None) -> TraceEvent  # SYNC
EVENT_TYPES = frozenset({RETRIEVE, SCORE_CHECK, REWRITE, RERANK, GENERATE, REFUSE})

# app/api/deps.py
OwnedAgent = Annotated[Agent, Depends(owned_agent)]    # binds a path param named agent_id
```

### 2.3 Constraints discovered, and what they cost

| Finding | Consequence for this build |
|---|---|
| `TraceRecorder.record` **raises** `ValueError` on an unknown `event_type` | Extend `EVENT_TYPES` first, or every tool event is a 500. **No migration** — the column is `String(32)` with no CHECK, payload is JSONB |
| `build_chat_model` returns a genuine `ChatOpenAI` | `.bind_tools()` works. **Never pass `parallel_tool_calls`** — `disabled_params={"parallel_tool_calls": None}` exists precisely because OpenRouter advertises no such parameter |
| `openrouter_require_parameters=True` | Load-bearing for tools, not incidental. Off, a provider that cannot do `tools` silently drops the field and returns prose |
| Verified against OpenRouter, 2026-08-16 | **14 of 19** `google/gemma-4-31b-it` endpoints advertise both `tools` and `top_k` — the intersection the current request needs. Routing has headroom |
| Retrieval is scoped by the `Agent` **object**, never a namespace string | A retrieval tool must close over the agent. This is PRD §7, not a convention |
| Migration HEAD is `b8d2f47a91c5` | New revision sets `down_revision = 'b8d2f47a91c5'` |
| All PKs are `UUID(as_uuid=True)`, Python-side `default=uuid.uuid4` | Pass `id=uuid.uuid4()` explicitly whenever the id is needed before flush |
| Background jobs take **ids and bytes only**, open their own `SessionLocal()`, scheduled **after** `db.commit()` | The handout generation job copies `app/rag/jobs.py` exactly |
| `main.py` calls `include_router(x)` with no arguments | A new router declares its own `prefix="/api"` |
| Frontend has **no router** — `View` union in `App.tsx` | The panel is state inside `AgentChat`, not a route |
| Tailwind **v4, CSS-first**: no `tailwind.config.js`, no `@theme`, no `tailwind.config` at all | New tokens go in `index.css` under the `gw-` prefix or nowhere |
| `verbatimModuleSyntax` + `allowImportingTsExtensions` | Every relative import carries `.ts`/`.tsx`; types use `import type` |
| Reduced-motion rule kills all transitions with `!important` | The drawer must never depend on `transitionend` |
| No portal root, no focus trap, no scroll lock, no Escape handler exist | The drawer writes all four from scratch |
| `min-h-11` is the tap-target contract, with **9 real violations** | Fixed in feature 5, listed there with file:line |
| `matplotlib` and `python-pptx` are **not installed**; `pandas` 3.0.5, `numpy` 2.5.2, `PIL` 12.3.0 already are | Two new direct dependencies, ~55 MB added to the Render build |

---

## 3. Architecture after the change

```
                            ask.run_turn
                                 |
              pipeline.answer_question(agent, question, history)
                                 |
        1. contextualize  ->  2. aretrieve  ->  3. GENERATION
                                                     |
                          agent.tools_enabled ?  ----+----  no  --> single chain.ainvoke()   (unchanged Stage 1/2)
                                 |
                                yes
                                 v
                    app/rag/agent_loop.run_agent_loop()
                                 |
              +------------------+-------------------+
              |                                      |
     bind_tools([search_corpus, run_python])    ContextLedger
              |                                 (ordered, deduped by chunk_id;
     step 1..max_tool_steps:                     owns the [n] marker numbering)
       AIMessage.tool_calls ?                          ^
         yes -> execute -> ToolMessage -> re-invoke    |
         no  -> done, text is the answer               |
              |                                        |
              +--> search_corpus(query, k) -> aretrieve(agent, ...) -> merge into ledger
              |
              +--> run_python(code, purpose, filename)
                        |
                   tools/sandbox.run()
                        |
                   subprocess: python -I  _sandbox_child.py
                     - env stripped of every secret
                     - AST allowlist (parent) + import hook (child)
                     - sockets neutered
                     - RLIMIT_CPU/AS/FSIZE/NPROC on POSIX
                     - 30 s wall clock, then kill
                     - cwd = fresh temp dir, the only writable path
                        |
                   harvest files -> SandboxArtifact(name, mime, bytes)
                        |
                   accumulate onto AnswerResult.artifacts
                                 |
                                 v
                        back in run_turn:
                          trace.record(TOOL_CALL / TOOL_RESULT / TOOL_ERROR)
                          INSERT handouts rows (bytes live in Postgres)
                          AskOut.handouts = [HandoutRef, ...]
                                 |
                                 v
                    frontend: HandoutsPanel lists them,
                    docked at xl+, drawer below
```

---

## 4. Shared contracts

These are the interfaces every feature document depends on. **Change them here, not in a
feature document.**

### 4.1 New settings — `app/config.py`

```python
agent_tools_enabled: bool = True        # global kill switch; agent column still gates per-agent
agent_max_tool_steps: int = 3           # tool round-trips per turn before the loop is closed
sandbox_timeout_s: float = 30.0         # wall clock per run_python call
sandbox_max_output_chars: int = 8_000   # stdout+stderr returned to the model
sandbox_max_artifact_bytes: int = 5_242_880    # 5 MB, per file
sandbox_max_total_bytes: int = 15_728_640      # 15 MB, all files from one run
sandbox_memory_mb: int = 768            # RLIMIT_AS on POSIX; ignored on Windows
handout_max_per_agent: int = 200        # quota; oldest are refused, never silently dropped
```

### 4.2 New trace event types — `app/rag/trace.py`

```python
TOOL_CALL   = "TOOL_CALL"     # payload: {step, tool, args, call_id}
TOOL_RESULT = "TOOL_RESULT"   # payload: {step, tool, call_id, ok: True, summary, ...tool-specific}
TOOL_ERROR  = "TOOL_ERROR"    # payload: {step, tool, call_id, ok: False, error, error_kind}
```
Added to `EVENT_TYPES`. No migration.

`TOOL_RESULT` payload per tool:
- `search_corpus`: `{returned: int, new_chunks: int, top_score: float | None, markers: [int]}`
- `run_python`: `{artifacts: [{filename, mime_type, byte_size}], stdout_chars: int, exit_code: int}`

### 4.3 `AnswerResult` — new fields (all default-empty, so existing callers are unaffected)

```python
tool_calls: list[ToolInvocation] = field(default_factory=list)
artifacts:  list[SandboxArtifact] = field(default_factory=list)
tool_ms: int = 0
tool_steps: int = 0
stopped_reason: str | None = None    # "max_steps" | "tool_error" | None
```

```python
@dataclass
class ToolInvocation:
    step: int; call_id: str; tool: str
    args: dict[str, Any]
    ok: bool
    summary: str                 # one line, safe to render
    detail: dict[str, Any]       # goes into the TOOL_RESULT payload
    duration_ms: int
    error: str | None = None
```

**`generation_ms` stops meaning "the whole model call" once a loop runs.** `tool_ms` holds
time spent *inside* tools; `generation_ms` holds summed model time. The trace must add up to
the turn rather than overlap, which is why these are separate fields and not one.

### 4.4 Database — one new table, two new agent columns

```python
class Handout(Base):
    __tablename__ = "handouts"
    id             = _pk()
    created_at     = _created_at()
    agent_id        -> agents.id        ON DELETE CASCADE, index, NOT NULL
    conversation_id -> conversations.id ON DELETE CASCADE, index, NULL
    query_id        -> queries.id       ON DELETE SET NULL, NULL
    created_by_user_id -> users.id      ON DELETE SET NULL, NULL
    kind         String(16)  NOT NULL      # 'chart' | 'deck' | 'sheet' | 'table'
    title        String(200) NOT NULL
    filename     String(255) NOT NULL
    mime_type    String(128) NOT NULL
    byte_size    Integer     NOT NULL default 0
    status       String(16)  NOT NULL default 'ready'   # 'pending' | 'ready' | 'failed'
    origin       String(16)  NOT NULL default 'tool'    # 'tool' | 'recipe'
    content      LargeBinary NULL      # DEFERRED -- never loaded by a list query
    preview_text Text        NULL      # markdown body for 'sheet', caption otherwise
    source_code  Text        NULL      # the Python that produced it
    meta         JSONB       NULL
    error        Text        NULL
    __table_args__ = (Index("ix_handouts_agent_created", "agent_id", desc("created_at")),
                      Index("ix_handouts_conversation", "conversation_id"))

# agents
tools_enabled  Boolean NOT NULL server_default 'true'   # migration backfills EXISTING rows to false
max_tool_steps Integer NOT NULL server_default '3'
```

**`content` must be `deferred()`.** A list query that eagerly loads bytea returns megabytes
per row; the panel lists 200 of them.

**`tools_enabled` backfill is deliberately asymmetric.** New agents get `true` from the
server default; the migration sets every *existing* row to `false`. Agents whose eval runs
are already recorded in EVAL.md keep behaving exactly as they were measured, and anything
created after this ships is agentic out of the box.

### 4.5 API surface

All nested under `agent_id`, so ownership is free via `OwnedAgent` — no route can be
expressed without naming an agent, which is the §7 constraint made structural.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| `GET` | `/api/agents/{agent_id}/handouts` | `?conversation_id=&kind=&limit=` | `list[HandoutOut]` |
| `POST` | `/api/agents/{agent_id}/handouts` | `HandoutRequest` | **202** `HandoutOut` (status `pending`) |
| `GET` | `/api/agents/{agent_id}/handouts/{handout_id}` | — | `HandoutDetail` (adds `preview_text`, `source_code`) |
| `GET` | `/api/agents/{agent_id}/handouts/{handout_id}/download` | — | `Response` with `Content-Disposition: attachment` |
| `DELETE` | `/api/agents/{agent_id}/handouts/{handout_id}` | — | `{"ok": true}` |

```python
class HandoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: Literal["chart", "deck", "sheet", "table"]
    brief: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    conversation_id: uuid.UUID | None = None   # ground the handout in a thread's answers

class HandoutOut(BaseModel):        # from_attributes=True; never includes `content`
    id: uuid.UUID; kind: str; title: str; filename: str; mime_type: str
    byte_size: int; status: str; origin: str; error: str | None
    conversation_id: uuid.UUID | None; query_id: uuid.UUID | None
    created_at: datetime
```

`AskOut` gains `handouts: list[HandoutOut] = []` and `tool_steps: int = 0` — both defaulted,
so an agent with tools off serialises exactly as before.

### 4.6 Frontend contract

```ts
export type Handout = {
  id: string; kind: string; title: string; filename: string; mime_type: string;
  byte_size: number; status: string; origin: string; error: string | null;
  conversation_id: string | null; query_id: string | null; created_at: string;
};
export type HandoutDetail = Handout & { preview_text: string | null; source_code: string | null };

// lib/api.ts
handouts.list(agentId, opts?): Promise<Handout[]>
handouts.load(agentId, handoutId): Promise<HandoutDetail>
handouts.create(agentId, req): Promise<Handout>      // 202
handouts.remove(agentId, handoutId): Promise<{ok: boolean}>
handouts.downloadUrl(agentId, handoutId): string     // a URL for <a href>, not a request
```

`AskResult` gains `handouts: Handout[]` and `tool_steps: number`.

---

## 5. Build sequence

Six phases. **Arrows are hard dependencies** — anything not connected can run in parallel.

```
PHASE A  foundations (no feature depends on another inside this phase)
  A1 config settings + trace event types                        [small, unblocks everything]
  A2 models.py: Handout + agent columns  ->  A3 alembic migration -> alembic upgrade head
  A4 requirements: matplotlib, python-pptx  (+ RESTORE the pywin32 marker after freeze)
        |
        v
PHASE B  the sandbox, standalone and independently testable
  B1 app/tools/sandbox.py + _sandbox_child.py
  B2 harness: scripts/sandbox_check.py -- 12 cases incl. 5 hostile ones
        |
        v
PHASE C  tools + loop            PHASE D  handouts backend  (needs A2/A3 + B1)
  C1 tools/corpus.py               D1 handouts/recipes.py
  C2 tools/interpreter.py          D2 handouts/jobs.py
  C3 tools/registry.py             D3 api/handouts.py + main.py wiring
  C4 rag/agent_loop.py + ContextLedger
  C5 pipeline.py branch
  C6 ask.py: trace events, handout rows, AskOut fields
        |                                   |
        +-----------------+-----------------+
                          v
PHASE E  frontend  (needs the 4.5/4.6 contracts only -- can START during C/D)
  E1 types.ts + api.ts                    [contract-only, start immediately after this doc]
  E2 components/Drawer.tsx primitive      [independent]
  E3 HandoutsPanel + HandoutCard
  E4 AgentChat layout integration
  E5 TracePanel tool events + Message tool summary
  E6 responsive/tap-target sweep          [independent of E1-E5]
                          |
                          v
PHASE F  test, iterate, document
  F1 backend harness: scripts/agentic_check.py end-to-end
  F2 frontend unit tests: Vitest + Testing Library
  F3 Playwright MCP: desktop 1440x900, tablet 834x1112, mobile 390x844,
     narrow mobile 320x844
  F4 fix, re-run from the lowest layer touched, repeat until clean
  F5 CLAUDE.md / PRD.md / EVAL.md / README.md updates
  F6 commit + push
```

**Critical path**: A1 -> B1 -> C4 -> C6 -> E4 -> F3. Everything else has slack.

**Parallelisation actually used**: E1/E2/E6 run alongside C and D, because they depend only
on the contracts in §4 and on files no backend task touches.

---

## 6. Risk register

| Risk | Likelihood | Mitigation | Detected by |
|---|---|---|---|
| Gemma emits a tool call the loop cannot parse | Low — `function_calling` is already proven here for structured output | Loop treats an unparseable call as `TOOL_ERROR`, appends a `ToolMessage` saying so, and continues; never crashes the turn | F1 |
| `404 No endpoints found` after adding `tools` | Low — 14 eligible endpoints verified | Do not add any parameter beyond `tools`/`tool_choice`; never pass `parallel_tool_calls` | F1, first run |
| Loop never terminates / burns tokens | Medium | Hard `max_tool_steps` counter; the final step re-invokes with `tool_choice="none"` so an answer is forced | F1 |
| Sandbox subprocess hangs on Render's single worker | Medium | `asyncio.to_thread` + `subprocess.run(timeout=)` + `kill()`; the ingest path already established that pattern | F1 |
| matplotlib import cost dominates latency | High — ~1.5 s cold | Only pre-import what the code's AST actually names; document the floor | F1 timings |
| Render build gets slower / larger | Certain, ~55 MB | Accepted and recorded; matplotlib uses the `Agg` backend, no GUI toolkit | build log |
| `pip freeze` flattens the `pywin32` marker again | **Certain — it has happened twice** | `grep -n pywin32 backend/requirements.txt` is step two of the freeze, not a thing to remember | A4 |
| Three-column chat squeezes the thread | High if done at `md` | Dock only at `xl` (1280px); drawer below. Chat tab container widens to `xl:max-w-[90rem]` unconditionally | F3 tablet run |
| Drawer traps focus badly / no Escape | High — none of these primitives exist | `Drawer.tsx` writes focus trap, Escape, scroll lock and restore-focus once, tested in isolation | F2, F3 |
| bytea rows bloat the list query | Certain if missed | `content` is `deferred()`; `HandoutOut` has no `content` field at all | F1 |
| Deleting a conversation destroys its handouts | By design (CASCADE) | Documented; handouts survive *document* deletion, which is the case PRD item 18 is about | — |

---

## 7. Definition of done

- [ ] `alembic upgrade head` applies cleanly and `downgrade` reverses it
- [ ] An agent with `tools_enabled=false` produces a byte-identical turn shape to today
- [ ] An agent with `tools_enabled=true` answers a multi-hop question using two `search_corpus` calls, visible in the trace
- [ ] "Chart the figures you just cited" produces a PNG handout, downloadable, with its source code shown
- [ ] Every one of the 12 sandbox harness cases behaves as specified, including all 5 hostile ones
- [ ] Handouts panel: docked at 1440px, drawer at 834px and 390px, keyboard-operable, Escape closes, focus returns
- [ ] Zero horizontal scroll at 320px on every view
- [ ] Every interactive control is >= 44px (the 9 known violations fixed)
- [ ] Frontend unit tests and production build clean
- [ ] Playwright run clean at four viewports with no console errors
- [ ] CLAUDE.md gains the gotchas this build discovered; PRD open items 7 and 13 updated
- [ ] Committed and pushed

---

## 7a. As built — where the plan was wrong

Recorded because a plan that is only ever read forwards teaches nothing. Everything below
was found by building or testing, not by thinking harder.

**The big one: prompting could not make the model use `search_corpus`.** §4 assumed a bound
tool and good guidance would be enough. Measured, it was not — three prompt variants
including an explicit *"you MUST call search_corpus"* produced zero tool calls, and so did
`tool_choice="any"`. Only a **named** tool forced one. The cause is structural: every system
prompt here is refusal-first, which is what makes the product trustworthy, and a model
drilled to treat a missing fact as a cue to *decline* will not spontaneously treat it as a
cue to *search*. So the loop gained a **gap trigger** — `detect_gap` on the answer, then one
forced named search — which is not in any feature document above. See
[01-agentic-tool-loop.md](01-agentic-tool-loop.md) and CLAUDE.md's *The agent loop*.

| Planned | As built | Why |
|---|---|---|
| Tool use follows from `TOOL_GUIDANCE` | Gap trigger + forced named tool call | Prompting measurably does not work on this model |
| Refusal markers stay in `app/api/ask.py` | Moved to `app/rag/refusal.py`, with `detect_refusal` **and** `detect_gap` | `agent_loop` cannot import `app.api`, and a second copy of a list already wrong three times is the thing to prevent structurally |
| `AnswerResult.artifacts: list[SandboxArtifact]` | `list[ToolArtifact]` | `SandboxArtifact` is frozen and cannot carry `title`/`source_code`/`step` |
| `ContextLedger.seed` before the branch | Inside the tools branch | Keeps "byte-identical when tools are off" structural rather than asserted |
| `format_context` delegated to | Gained a keyword-only `markers=None` | One renderer; omitting the argument is provably the old output |
| `stopped_reason="tool_error"` | Needed `MAX_CONSECUTIVE_FAILED_STEPS = 2` | Every per-call failure continues the loop, so nothing could ever set it |
| `tools_enabled: bool = True` on `AgentTunables` | `bool \| None = None` | A non-None default travels in every create request and states the default in two places that can drift; this frontend has no client generation, so the benefit was never collected |
| Import allowlist is the sandbox's outer layer | Plus `DENIED_MODULE_ATTRS` on attribute access | `matplotlib.os` and `numpy.ctypeslib.ctypes` passed every check |
| `RLIMIT_NPROC` = current usage | current + 64 | A *thread* counts as a task on Linux; a tight limit kills numpy/OpenBLAS at import, on production only |
| Panel issues two filtered list requests | One request, partitioned client-side | Two requests 30 ms apart can put a new handout in both lists or neither |
| `_load_messages` is 3 statements | 4 | Handouts join; still bounded by *things a message has*, never by message count |

**A tenancy hole the plan did not call out.** `conversation_id` arrives in the `POST
/handouts` body and is the one client-supplied id `OwnedAgent` does not cover — unchecked,
another user's conversation would be rendered into this user's slide deck. Now verified as a
`(conversation_id, agent_id)` pair before insert, and filtered again inside the job, which is
reachable with two bare ids and no route in front of it.

**Three test scenarios were wrong before the code was.** S3 passed twice while proving
nothing: first because the fixtures chunked to one chunk per file, then because
`retrieve_k=3` over seven chunks still returned both topics. A test that the feature is
*needed* has to make the feature *necessary* — S3 now starves retrieval to `k=1` itself. And
S7 failed against correct behaviour, because `_detect_refusal` did not know the phrase
`"does not state"` — the third recurrence of that gap.

---

## 8. What this deliberately does not do

Recorded so the omissions read as decisions rather than oversights.

- **No SSE streaming.** PRD item 13, still open. A tool loop makes it *more* valuable — the
  user now waits through tool calls too — but it is a change to the response type of two
  routes and belongs in its own change.
- **No object storage.** Handout bytes go in Postgres, capped and quota'd. PRD item 10 is
  the right long-term answer and is untouched here.
- **No MCP tools.** `langchain-mcp-adapters` is installed and still unimported. Adding a
  third tool is a registry entry after this lands.
- **No eval coverage of tool use.** Ragas scores faithfulness of an answer; it has no
  opinion on whether the right tool was called. Measuring tool choice needs a trajectory
  metric, which is Stage 4, not Stage 3.
- **No sandbox container.** Hardened subprocess, honestly documented in
  [02-code-interpreter.md §5](02-code-interpreter.md). It mitigates; it does not isolate.
