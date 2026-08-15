# Feature 4 — Handouts

> Shared contracts: [00-IMPLEMENTATION-PLAN.md §4](00-IMPLEMENTATION-PLAN.md).
> Backend and frontend in one document, because the panel is only as good as what fills it.

---

## 1. The idea, and the name

NotebookLM calls this Studio. Groundwork calls it **Handouts**, because Groundwork is a
teaching product — its five seeded personas are pedagogies, its corpus is lecture material —
and a handout is the thing an instructor makes *from* a lesson *for* you. It needs no
explanation to a workshop attendee, which "Studio", "Artifacts" and "Canvas" all do.

A handout arrives one of two ways, and the panel does not distinguish them visually beyond
a small origin label:

- **`origin="tool"`** — the agent wrote Python mid-conversation and it produced a file. The
  user asked "chart those numbers" and got a chart, as part of the answer.
- **`origin="recipe"`** — the user pressed a button in the panel and described what they
  wanted. No conversation turn involved.

---

## 2. Backend

### 2.1 Table

Full definition in [00-IMPLEMENTATION-PLAN.md §4.4](00-IMPLEMENTATION-PLAN.md). Three
details that are easy to get wrong:

```python
content: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
```

**`deferred()` is not optional.** The panel lists up to 200 rows; a list query that eagerly
loads bytea returns tens of megabytes and the failure looks like a slow network. The
`HandoutOut` schema has no `content` field, so nothing can serialise it by accident either —
two independent guards, because this is the kind of mistake that only shows up under real
data.

```python
source_code: Mapped[str | None] = mapped_column(Text)
```

**The code that made the artefact is stored and shown.** NotebookLM does not do this; for a
product whose entire purpose is making a pipeline inspectable, hiding the generation step
would be the one place the product stopped practising what it teaches. It is also the
fastest way for a user to understand why a chart is wrong.

```python
query_id -> queries.id ON DELETE SET NULL
```

`SET NULL`, not `CASCADE`. A handout outlives the turn that produced it — deleting a
conversation should not silently destroy a slide deck the user downloaded a week ago.
(`conversation_id` *is* `CASCADE`, which is a deliberate asymmetry: a handout with no
`query_id` still lists fine, but a handout pointing at a deleted conversation would be
unreachable in the UI. Deleting a thread is an explicit user action; deleting a query is not
something the UI offers at all.)

### 2.2 Migration

```
revision = <new>
down_revision = 'b8d2f47a91c5'
```

`upgrade()`:
1. `op.create_table("handouts", ...)` — Core only, no `app.db.*` import
2. two indexes via `op.f(...)`
3. `op.add_column("agents", sa.Column("tools_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")))`
4. `op.add_column("agents", sa.Column("max_tool_steps", sa.Integer(), nullable=False, server_default=sa.text("3")))`
5. **`op.execute("UPDATE agents SET tools_enabled = false")`** — the asymmetric backfill.
   Existing agents keep the behaviour their eval runs were measured against; new agents get
   `true` from the server default. Without this line, every historical scorecard in
   [EVAL.md §10](../EVAL.md) silently stops being reproducible.

`downgrade()` drops both columns, both indexes and the table. Named FK constants as module
globals, matching the house style of `b8d2f47a91c5`.

### 2.3 The four recipes — `app/handouts/recipes.py`

| Recipe | Produces | Path |
|---|---|---|
| `chart` | `.png` | LLM writes matplotlib -> sandbox |
| `deck` | `.pptx` | LLM writes python-pptx -> sandbox |
| `table` | `.csv` | LLM writes pandas/csv -> sandbox |
| `sheet` | `.md` | LLM writes markdown **directly — no sandbox** |

`sheet` bypassing the sandbox is deliberate. It is the recipe most likely to be used, it is
the cheapest, and it means the panel still does something useful if matplotlib fails to
install on a fresh machine. Its markdown lands in both `content` (as UTF-8 bytes, so it
downloads) and `preview_text` (so it renders inline without a second request).

```python
@dataclass(frozen=True)
class Recipe:
    key: str
    label: str                 # "Slide deck"
    blurb: str                 # one line shown under the button
    kind: str                  # -> handouts.kind
    extension: str
    mime_type: str
    uses_sandbox: bool
    prompt: str                # {brief}, {context}, {conversation}

RECIPES: dict[str, Recipe]
```

### 2.4 Grounding — where the material comes from

A handout must be as grounded as an answer, or it becomes the one place the product
hallucinates freely.

```python
async def gather_material(db, agent, brief, conversation_id) -> Material:
    retrieval   = await aretrieve(agent, brief)          # the corpus, searched by the brief
    conversation = await _recent_answers(db, conversation_id, limit=6) if conversation_id else []
    return Material(context=format_context(retrieval.documents),
                    conversation=..., chunk_ids=[...])
```

Both halves matter. The corpus half means "make me a deck about the power budget" retrieves
the power budget even from a fresh panel with no conversation open. The conversation half
means "chart what we just discussed" works, which is the case the panel exists for.

`meta` records `{"chunk_ids": [...], "recipe": ..., "brief": ..., "model": ...}` so a handout
can be traced back to the chunks that produced it.

### 2.5 The job — `app/handouts/jobs.py`

Copies `app/rag/jobs.py` exactly, because that pattern is already load-bearing here.

```python
async def run_handout_job(agent_id: uuid.UUID, handout_id: uuid.UUID,
                          user_id: uuid.UUID | None, recipe_key: str,
                          brief: str, conversation_id: uuid.UUID | None) -> None:
    """NEVER raises. Ids and plain values only -- no ORM objects, no session.
    Opens its own SessionLocal() because the request's session is closed by the time
    a BackgroundTask runs."""
```

Flow, with the status write in a `finally`:

```
load agent by id; load handout on the (id, agent_id) PAIR
  -> gather_material
  -> build_chat_model(agent.generation_model or settings.generation_model)
  -> ask for the recipe's output
  -> if uses_sandbox:
         static_check + sandbox.run(code)
         on failure: ONE retry, feeding the traceback back to the model
         pick the primary artifact by the recipe's extension
     else:
         the model's markdown IS the content
  -> write content, byte_size, filename, mime_type, preview_text, source_code, status="ready"
finally:
  if status is still "pending": status="failed", error=<message>
```

**A background task that dies silently leaves a row at `pending` forever**, which reads as
progress and never gets investigated. The `finally` is the same guard `ingest.py` and
`eval/jobs.py` both carry, for the same reason.

**One retry, with the traceback.** The single most valuable property of a code interpreter
is that a failure is recoverable — the model reads its own `NameError` and fixes it. One
retry buys most of that; more retries buy latency. Both attempts' code is kept, joined, in
`source_code`, so a user reading it sees the correction.

### 2.6 Routes — `app/api/handouts.py`

```python
router = APIRouter(prefix="/api/agents", tags=["handouts"])   # same shape as documents.py
```

Every path carries `{agent_id}`, so `agent: OwnedAgent` authorises all five with no
hand-written ownership check. Full table in
[00-IMPLEMENTATION-PLAN.md §4.5](00-IMPLEMENTATION-PLAN.md).

**Create** follows the documents.py handoff exactly:

```python
db.add(handout)                      # status="pending", explicit id=uuid.uuid4()
await db.commit()                    # commit BEFORE scheduling
await db.refresh(handout)            # server_default created_at
background.add_task(run_handout_job, agent.id, handout.id, user.id,
                    body.recipe, body.brief, body.conversation_id)
return handout                       # 202
```

Quota is checked before the insert: `count(*) where agent_id = ...` against
`settings.handout_max_per_agent`, 409 with a message naming the limit. **Refused, never
silently evicted** — a panel that deletes the user's oldest deck to make room for a chart is
worse than one that says no.

**Download**:

```python
@router.get("/{agent_id}/handouts/{handout_id}/download")
async def download_handout(agent: OwnedAgent, handout_id: uuid.UUID, db: DbSession) -> Response:
    row = await _load_owned(db, agent, handout_id, with_content=True)   # undefer here, only here
    if row.status != "ready" or row.content is None:
        raise HTTPException(409, "This handout is not ready yet")
    return Response(content=row.content, media_type=row.mime_type,
                    headers={"Content-Disposition": f'attachment; filename="{_safe(row.filename)}"'})
```

`_safe()` strips anything outside `[A-Za-z0-9._-]` and caps the length. A filename derived
from a model-written `purpose` string reaching a `Content-Disposition` header unescaped is a
header-injection hole, and the model writes that string.

**The download route is a cookie-authenticated `GET`,** so a plain `<a href download>` works
and no blob juggling is needed in the browser. `credentials: "include"` is already the
codebase-wide default and same-site `none` cookies are already configured.

### 2.7 Handouts created by the tool loop

`run_turn` already holds `result.artifacts` and the open transaction:

```python
for art in result.artifacts:
    db.add(Handout(id=uuid.uuid4(), agent_id=agent.id, conversation_id=conversation.id,
                   query_id=query.id, created_by_user_id=user.id,
                   kind=_kind_for(art.mime_type), title=art.title or "Generated file",
                   filename=art.filename, mime_type=art.mime_type,
                   byte_size=art.byte_size, content=art.content,
                   source_code=art.source_code, origin="tool", status="ready",
                   meta={"tool": "run_python", "step": art.step}))
```

Same transaction as the turn, one commit. If the turn rolls back, so do its handouts —
which is correct: an artefact attributed to an answer that was never stored is orphaned.

---

## 3. Frontend

### 3.1 Where it lives

```
>= xl (1280px)          docked third column in the chat grid
<  xl                   right-side overlay drawer, opened from a toolbar button
```

The chat grid today is `md:grid-cols-[15rem_minmax(0,1fr)]` inside `max-w-6xl` (1152px).
Adding a 22rem column at `md` would leave the thread about 200px wide. So:

- The chat tab's container widens to `xl:max-w-[90rem]` **unconditionally** — not only when
  the panel is open, because a layout that reflows when you open a panel is worse than one
  that is simply wider. This is an independent win: every view is currently capped at
  1152px on a 1920px monitor.
- At `xl`: `xl:grid-cols-[15rem_minmax(0,1fr)_22rem]`, leaving ~600px of thread.
- Below `xl`: `<HandoutsDrawer>` — an overlay, `z-40`, above the `z-20` sticky nav.

A single `<HandoutsPanel>` renders in both; only the chrome around it differs.

### 3.2 `Drawer.tsx` — four primitives that do not exist yet

The audit found **no focus trap, no Escape handler, no scroll lock and no portal root**
anywhere in the codebase. The drawer is the first modal-ish surface, so it writes them once,
in one file, tested in isolation.

```tsx
export function Drawer({ open, onClose, title, children, testId }: {
  open: boolean; onClose: () => void; title: string;
  children: ReactNode; testId?: string;
})
```

| Behaviour | Implementation note |
|---|---|
| Escape closes | `keydown` on `document`, added only while open, removed on cleanup |
| Focus moves in on open | Focus the panel's heading (`tabIndex={-1}`), **exactly one element** |
| Focus is trapped | `Tab`/`Shift+Tab` wrap across `querySelectorAll(FOCUSABLE)` |
| Focus returns on close | The element that had focus at open time is stored in a ref and refocused |
| Background does not scroll | `document.body.style.overflow = "hidden"`, restored on cleanup |
| Backdrop click closes | Backdrop is a sibling with its own `onClick`, not a wrapper |
| `role="dialog" aria-modal="true" aria-labelledby` | Heading carries the id |

Two constraints from the audit that shape this:

**Focus exactly one element per transition.** StrictMode double-invokes effects; a
step-change effect that focuses a heading and then an input fired a blur between them on
the second invocation, and that blur forged a "field has been visited" flag. The drawer
focuses the heading and nothing else.

**Never depend on `transitionend`.** `index.css` kills every transition with `!important`
under `prefers-reduced-motion`. Visibility is state, animation is decoration:
`open ? "translate-x-0" : "translate-x-full pointer-events-none"` plus
`aria-hidden`/`inert`, never a callback.

No portal is added. `index.html` has one `#root` and the drawer renders inside the view
tree with `fixed inset-0 z-40`, which is sufficient — the only stacking context above it is
the `z-20` nav.

### 3.3 `HandoutsPanel.tsx`

```tsx
export default function HandoutsPanel({ agentId, conversationId, onCountChange }: {
  agentId: string;
  conversationId: string | null;
  onCountChange?: (n: number) => void;
})
```

Three regions, top to bottom:

```
+--------------------------------------+
|  Make a handout                      |
|  [ 📊 Chart ] [ 📑 Slide deck ]      |    4 recipe buttons, 2x2 grid, each >= 44px
|  [ 📋 Table ] [ 📄 Study sheet ]     |
|                                      |
|  <brief textarea, shown after a       |    "What should it cover?"
|   recipe is picked>                  |
|  [ Make it ]         [ Cancel ]      |
+--------------------------------------+
|  In this conversation           (2)  |    filtered to conversationId
|    [thumb] Budget by subsystem       |
|            chart · 41 KB · 2 min ago |
|            [Download] [Code] [x]     |
+--------------------------------------+
|  All handouts                   (7)  |    everything else, collapsed by default
+--------------------------------------+
```

**Polling.** Creation returns 202 with `status="pending"`. The panel polls
`handouts.list` every 3 s **while any row is pending**, and stops when none are — the same
shape `AgentEvaluate` already uses for eval runs. Not a fixed interval; not forever.

**Row states, all three rendered:**
- `pending` — spinner, "Making this…", elapsed seconds, no download link
- `ready` — thumbnail (for `chart`, an `<img>` at the download URL), size, download, "Code", delete
- `failed` — rose border, the `error` text, a "Try again" button that re-POSTs the same brief

**"Code" reveal.** Uses the existing `<Reveal>` from `ui.tsx`, fetching `HandoutDetail`
on first open — same fetch-on-first-open shape as `TracePanel`. The code renders in the
`font-mono text-xs` + `overflow-auto` block the codebase already uses for trace payloads.

**Sheet preview.** `preview_text` renders through the existing `react-markdown` +
`remark-gfm` setup, reusing the hand-rolled `Components` map from `Message.tsx`. That map
must be **extracted to `lib/markdown.tsx`** and imported by both, not copied — there is no
`@tailwindcss/typography` in this project and two divergent copies of a 70-line component
map is how they drift.

### 3.4 Chat integration

`AgentChat.tsx` gains:

```ts
const [panelOpen, setPanelOpen] = useState(false);   // drawer state, < xl only
const [handoutCount, setHandoutCount] = useState(0);
```

A toolbar button above the thread, `data-testid="handouts-toggle"`, showing the count. It is
`xl:hidden` — at `xl` the panel is docked and a toggle would be meaningless.

**A new handout from a turn must reach the panel.** `AskResult` carries
`handouts: Handout[]`; `send()` passes them up so the panel prepends without waiting for a
poll. On mobile, where the panel is closed, the count badge increments and the answer bubble
carries an inline "1 handout" chip that opens the drawer — otherwise the file the user just
asked for is invisible on the device most likely to be used.

### 3.5 Empty state

```
No handouts yet.
Ask the agent to chart something, or pick a recipe above.
```

Uses the existing `<EmptyState>`. It names the two routes in, because a panel with four
buttons and no explanation of the conversational path teaches only half the feature.

---

## 4. Files

| File | Change |
|---|---|
| `app/db/models.py` | `Handout`; `agents.tools_enabled`, `agents.max_tool_steps` |
| `alembic/versions/<rev>_handouts_and_agent_tools.py` | **new** |
| `app/handouts/__init__.py`, `recipes.py`, `jobs.py` | **new** |
| `app/api/handouts.py` | **new** |
| `app/main.py` | include the router |
| `app/api/ask.py` | persist `result.artifacts`; `AskOut.handouts` |
| `app/config.py` | `handout_max_per_agent` |
| `frontend/src/lib/types.ts` | `Handout`, `HandoutDetail`, `HandoutRecipe`; extend `AskResult` |
| `frontend/src/lib/api.ts` | `handouts` namespace |
| `frontend/src/lib/markdown.tsx` | **new** — the `Components` map extracted from `Message.tsx` |
| `frontend/src/components/Drawer.tsx` | **new** |
| `frontend/src/components/HandoutsPanel.tsx` | **new** |
| `frontend/src/components/HandoutCard.tsx` | **new** |
| `frontend/src/components/Message.tsx` | import the extracted map; inline handout chip |
| `frontend/src/views/AgentChat.tsx` | three-column grid, drawer, toggle, count |
| `frontend/src/views/AgentDetail.tsx` | `xl:max-w-[90rem]` on the chat tab |

---

## 5. Acceptance

1. `POST /handouts` with `recipe="sheet"` -> 202, row `pending`, then `ready` with markdown in both `content` and `preview_text`.
2. `recipe="chart"` -> a `.png` that opens, `source_code` non-empty, `meta.chunk_ids` non-empty.
3. `recipe="deck"` -> a `.pptx` that PowerPoint opens without a repair prompt.
4. A recipe whose first code attempt raises -> one retry, `source_code` contains both attempts, final status `ready`.
5. Download sets `Content-Disposition: attachment` with a sanitised filename; a `purpose` of `"a"; rm -rf /` produces a safe filename and no header break.
6. Listing 50 handouts issues **no** query that selects `content` (assert via SQL echo).
7. Quota: creating one past `handout_max_per_agent` returns 409 naming the limit; nothing is deleted.
8. Deleting the conversation removes its handouts; deleting a *document* leaves them intact.
9. Panel: docked at 1440px, drawer at 834px and 390px; Escape closes; focus returns to the toggle.
10. A turn that produces a handout shows it in the panel without a manual refresh.
