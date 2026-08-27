"""Layer 1 for `app/storage.py` and the download contract. No DB, no network.

Run:  backend/.venv/Scripts/python.exe scripts/storage_check.py
      backend/.venv/Scripts/python.exe scripts/storage_check.py --live
      backend/.venv/Scripts/python.exe scripts/storage_check.py --cleanup

**`--live` is a SECOND mode and it breaks the "no DB, no network" promise on
purpose.** Cases 78-79b execute the real download route against the real
database through `httpx.ASGITransport`, because the keyless-download 500 that
change set 15 exists to fix is invisible to everything above it: an offline
harness reads source and introspects models, so a route that IMPORTS and does
not RUN passes every case in this file. `admin_check.py --live` was added for
the identical reason after `GET /api/admin/spend` 500'd on every request under
a green suite, and CLAUDE.md states the rule -- a layer-1 harness cannot prove a
query runs, only that it was written. The default invocation is unchanged and
still needs nothing.

**`--live` OWNS ITS FIXTURE IDENTITY, and that is a safety property rather than
tidiness.** `DATABASE_URL` points at the shared production database holding real
people's agents. An earlier draft of this mode picked its owner with
`select(User).limit(1)` -- no ORDER BY, so *which* real person got a
`storage_check --live fixture` agent in their dashboard was whatever Postgres
returned first and could differ between runs. It now creates and deletes its own
user, keyed on `google_sub = "storage-check-local"`, exactly as
`scripts/slice_check.py` does with `slice-check-local` and `ui_check.py` with
`ui-check@groundwork.local`. **Never `select(User).limit(1)` in a mode that
writes.**

`--cleanup` is the sweeper for the one hole a `finally` cannot cover: a hard
kill, or a database connection lost mid-run, leaves the fixture behind and
nothing on the request path will ever remove it. It deletes everything owned by
that one `google_sub` and prints what it found, so a leak is *removable* and not
merely findable. It is idempotent -- run it on a clean database and it says so.
`slice_check.py --cleanup` and `agentic_check.py --cleanup` set the convention;
this mode shipped without one, which is the gap this note exists to close.

Every case here runs against a FAKE S3 client that records calls. That is not a
convenience -- it is the only way to assert what this repo PUT IN a request, as
opposed to what Cloudflare did with it. `scripts/llm_check.py` draws the same
line for OpenRouter and says so: a structural harness "asserts what this repo put
in the request, never what the gateway did with it". The live half was measured
separately and is recorded in `new features/13-object-storage/PLAN.md` section 1.1.

**Case numbering starts at 71.** `scripts/deck_check.py` reserves blocks of ten
per feature file and its last block ends at 64; this file is separate because it
covers a different module, but it continues that numbering so a case id is unique
across both and a reader can tell at a glance which change set a red row belongs
to.

THREE STATES, not two. `deck_check.py` is pass/fail only, which is right for pure
functions. Storage cases can be genuinely unmeasurable -- boto3 absent, or a case
whose subject has not been built yet -- and `agentic_check.py:340-367` already
has the vocabulary for that. A row that could not be measured must never print
green, and must not fail the suite either.
"""

from __future__ import annotations

import inspect
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

failures: list[str] = []
unmeasured: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def not_measured(name: str, detail: str) -> None:
    print(f"[warn] {name} -- {detail}  <- NOT MEASURED")
    unmeasured.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# Defensive import, exactly as `deck_check.py:513-534` does it.
#
# The point is the "watch it fail" run. A plain `from app import storage` before
# that module exists aborts this file with a traceback and prints ZERO failures,
# which reads as a green suite to anything scanning output. A sentinel that is
# neither None nor a str lets every case below run and report red.
# ---------------------------------------------------------------------------
class _Missing:
    """Neither None nor a string, so no case accidentally passes against it."""

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        raise AttributeError(f"app.storage is not importable; wanted {name!r}")


try:
    from app import storage as _storage
except Exception as exc:  # noqa: BLE001
    print(f"[warn] app.storage did not import: {exc}")
    _storage = _Missing()  # type: ignore[assignment]

try:
    from app.api.handouts import _safe
except Exception as exc:  # noqa: BLE001
    print(f"[warn] app.api.handouts._safe did not import: {exc}")
    _safe = None  # type: ignore[assignment]


PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class FakeS3:
    """Records calls. Never touches the network.

    Mirrors `deck_check.py:1436-1491`'s `_FakeSession`: the way a layer-1 file
    asserts a WRITE with no live dependency is to fake the client, not to skip
    the assertion.
    """

    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.deletes: list[str] = []
        self.objects: set[str] = set()
        self.presigns: list[dict] = []

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects.add(kw["Key"])
        return {}

    def delete_object(self, **kw):
        self.deletes.append(kw["Key"])
        self.objects.discard(kw["Key"])
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.presigns.append({"op": op, "params": Params, "expires": ExpiresIn})
        disp = Params.get("ResponseContentDisposition", "")
        return f"https://fake.r2/{Params['Key']}?disp={disp}&X-Amz-Expires={ExpiresIn}"


# Captured ONCE, before anything replaces it. `with_fake` used to call
# `get_client.cache_clear()`, which works the first time and raises
# `AttributeError` on every call after -- because by then `get_client` is a
# plain lambda this file installed, not the lru_cache-wrapped original. The
# symptom was case 73 reporting NOT MEASURED while 71 and 72 passed, i.e. a
# harness bug that looked like an unbuilt feature.
_REAL_GET_CLIENT = getattr(_storage, "get_client", None)


def with_fake() -> FakeS3:
    """Swap the client for a recorder. Idempotent across repeated calls."""
    fake = FakeS3()
    _storage.get_client = lambda: fake  # type: ignore[assignment]
    return fake


print("\n-- 71-72  key derivation: the capability is ABSENT, not guarded --")

# The security property is not "keys are validated". It is that no function in
# `app/storage.py` accepts a string through which a caller could name someone
# else's object. Both key builders take uuid.UUID, so a request body string
# cannot reach one without surviving FastAPI's UUID parsing first -- the same
# structural argument as `SearchCorpusArgs` carrying exactly one field.
try:
    agent = uuid.UUID("11111111-1111-1111-1111-111111111111")
    other = uuid.UUID("99999999-9999-9999-9999-999999999999")
    item = uuid.UUID("22222222-2222-2222-2222-222222222222")

    hostile = '../../agents/' + str(other) + '/handouts/steal"\r\nX-Evil: 1'
    key = _storage.handout_key(agent, item, PPTX)

    check(
        "71. a handout key is built from ids alone and contains no caller string",
        str(agent) in key
        and str(item) in key
        and key.startswith(f"agents/{agent}/handouts/")
        and ".." not in key
        and hostile not in key,
        f"key={key}",
    )

    # The filename is the string that must NOT reach a key. It is model-written
    # for a handout and user-supplied for a document; the extension comes from
    # the mime table instead, which is a table this repo controls.
    doc = _storage.document_key(agent, item, "application/pdf")
    check(
        "71b. the extension comes from the mime type, never from a filename",
        doc.endswith(".pdf")
        and _storage.handout_key(agent, item, "application/x-unknown").endswith(str(item)),
        f"pdf={doc.rsplit('/', 1)[-1]} unknown-mime yields no suffix",
    )

    check(
        "71c. an agent prefix contains the agent id and nothing else variable",
        _storage.agent_prefix(agent) == f"agents/{agent}/"
        and _storage.agent_prefix(other) != _storage.agent_prefix(agent),
        f"prefix={_storage.agent_prefix(agent)}",
    )
except Exception as exc:  # noqa: BLE001
    not_measured("71. key derivation", f"app.storage unavailable: {exc}")


# ---------------------------------------------------------------------------
# 72. `_safe` moved surface. It used to sanitise a value FastAPI put in a
# Content-Disposition header; it now sanitises a value R2 puts in one. The
# injection argument in its docstring is identical either way, and the failure
# mode of forgetting is that a model-written filename reaches a response header.
# ---------------------------------------------------------------------------
try:
    if _safe is None:
        not_measured("72. _safe reaches the presign parameter", "_safe did not import")
    else:
        fake = with_fake()
        nasty = 'deck";\r\nSet-Cookie: a=b\r\n\r\n<script>.pptx'
        url = _storage.presigned_get_url(
            _storage.handout_key(agent, item, PPTX),
            filename=_safe(nasty),
            mime_type=PPTX,
        )
        disp = fake.presigns[0]["params"]["ResponseContentDisposition"]
        inner = disp[len('attachment; filename="') : -1]
        # Assert on the INJECTION, not on the words. The first draft of this case
        # also required `"Set-Cookie" not in disp`, and it went red against a
        # correctly sanitised value: `_safe` had collapsed the CRLF to `_`,
        # leaving the harmless filename `deck_Set-Cookie_a_b_script_.pptx`. The
        # letters survive; the escape does not, and only the escape is the
        # vulnerability. A test that greps for scary substrings measures
        # vocabulary rather than safety.
        check(
            "72. the disposition cannot escape its quoted value",
            "\r" not in disp
            and "\n" not in disp
            and disp.startswith('attachment; filename="')
            and disp.endswith('"')
            and '"' not in inner
            and ";" not in inner,
            f"disp={disp!r}",
        )
        # The seam sanitises whatever it is handed, so the control survives a
        # caller that forgets. This is the structural half of R-6.
        fake2 = with_fake()
        _storage.presigned_get_url(
            _storage.handout_key(agent, item, PPTX), filename=nasty, mime_type=PPTX
        )
        raw_disp = fake2.presigns[0]["params"]["ResponseContentDisposition"]
        check(
            "72d. an UNSANITISED filename is still safe -- the seam does not trust callers",
            "\r" not in raw_disp and "\n" not in raw_disp
            and '"' not in raw_disp[len('attachment; filename="') : -1],
            f"disp={raw_disp!r}",
        )
        check(
            "72b. the presign also pins the content type and an expiry",
            fake.presigns[0]["params"].get("ResponseContentType") == PPTX
            and isinstance(fake.presigns[0]["expires"], int)
            and 0 < fake.presigns[0]["expires"] <= 3600,
            f"type={fake.presigns[0]['params'].get('ResponseContentType')} "
            f"expires={fake.presigns[0]['expires']}s",
        )
        check(
            "72c. presigning READS -- it must never write or delete",
            fake.puts == [] and fake.deletes == [] and fake.presigns[0]["op"] == "get_object",
            f"puts={len(fake.puts)} deletes={len(fake.deletes)} op={fake.presigns[0]['op']}",
        )
except Exception as exc:  # noqa: BLE001
    not_measured("72. presign shape", f"{type(exc).__name__}: {exc}")


print("\n-- 73  the rollback path: an object written for a row that never landed --")

# PLAN.md section 3.5. The put happens BEFORE the commit, so a rollback leaves an
# object whose key is derived from an id that no longer exists anywhere -- an
# orphan that is unreachable rather than merely untidy. `delete_quietly` is the
# fourth step, and the property that matters is that it CANNOT RAISE: the caller
# is already handling an exception, and turning a recoverable rollback into a
# failed request in order to report a failed cleanup is strictly worse than the
# orphan it is preventing.
try:
    fake = with_fake()
    key = _storage.handout_key(agent, item, PPTX)
    _storage.put_object(key, b"PK\x03\x04 bytes", PPTX)
    _storage.delete_quietly(key)
    check(
        "73. delete_quietly removes an object written before a failed commit",
        key in fake.deletes and key not in fake.objects,
        f"put={len(fake.puts)} deleted={len(fake.deletes)}",
    )

    class Exploding(FakeS3):
        def delete_object(self, **kw):
            raise RuntimeError("R2 said no")

    boom = Exploding()
    _storage.get_client = lambda: boom  # type: ignore[assignment]
    raised = None
    try:
        _storage.delete_quietly(key)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check(
        "73b. delete_quietly NEVER raises -- it is called from an except block",
        raised is None,
        "swallowed and logged" if raised is None else f"raised {raised!r}",
    )

    _storage.get_client = lambda: FakeS3()  # type: ignore[assignment]
    none_raised = None
    try:
        _storage.delete_quietly(None)
    except Exception as exc:  # noqa: BLE001
        none_raised = exc
    check(
        "73c. delete_quietly(None) is a no-op, for a row that never got a key",
        none_raised is None,
        "no-op" if none_raised is None else f"raised {none_raised!r}",
    )
except Exception as exc:  # noqa: BLE001
    not_measured("73. rollback cleanup", f"{type(exc).__name__}: {exc}")


print("\n-- 74  configuration fails at LOAD, not at the first download --")

# The one required-secret gate in config.py, and it exists because there was no
# pattern for one. Every other secret is `str = ""` with no runtime check, which
# is right for them: an absent OPENROUTER_API_KEY fails the next model call
# loudly, naming itself. A blank R2 credential does not behave that way -- the
# app boots, the job runs, the bytes are generated, and it fails at the PUT
# inside a background task, surfacing as a handout stuck at `failed`.
try:
    from app.config import STORAGE_ROUTES, Settings

    good = dict(
        storage_route="r2",
        r2_account_id="acct",
        r2_access_key_id="akid",
        r2_secret_access_key="secret",
        r2_bucket="bucket",
    )

    def raises(**over) -> bool:
        try:
            Settings(_env_file=None, **{**good, **over})
            return False
        except Exception:  # noqa: BLE001
            return True

    missing_each = {
        field: raises(**{field: ""})
        for field in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket")
    }
    check(
        "74. storage_route='r2' with ANY credential blank raises at construction",
        all(missing_each.values()),
        f"{missing_each}",
    )
    check(
        "74b. storage_route='postgres' tolerates blank R2 config -- it is the rollback",
        not raises(storage_route="postgres", r2_account_id="", r2_access_key_id="",
                   r2_secret_access_key="", r2_bucket=""),
        "rollback road needs no bucket",
    )
    check(
        "74c. an unknown route is refused rather than falling through to a default",
        raises(storage_route="R2") and raises(storage_route="") and raises(storage_route="s3"),
        f"valid routes: {STORAGE_ROUTES}",
    )
except Exception as exc:  # noqa: BLE001
    not_measured("74. settings validation", f"{type(exc).__name__}: {exc}")


print("\n-- 75  the download contract, asserted against the route itself --")

# A5's sibling. These read the SOURCE of the download route rather than calling
# it, because calling it needs a database and this file has none. Reading source
# is a weak assertion and is used here only for properties that are structural:
# that the route still refuses a non-ready row, and that no presigned URL can
# reach a list response.
try:
    route_src = (ROOT / "backend/app/api/handouts.py").read_text(encoding="utf-8")

    check(
        "75. the download route still refuses a row that is not ready",
        'status != "ready"' in route_src,
        "the 409 the chart thumbnail and types.ts both depend on",
    )

    # Read the MODEL, not the source. The first draft grepped the class body for
    # "content" and went red -- because HandoutOut's docstring says, at length,
    # that there is deliberately no `content` field. The prose that documents the
    # guard tripped the test for the guard. `model_fields` is the fact; the
    # docstring is a claim about it.
    from app.api.handouts import HandoutOut

    fields = set(HandoutOut.model_fields)
    leaky = fields & {"content", "storage_key", "url", "download_url", "presigned_url"}
    check(
        "75b. HandoutOut exposes neither bytes nor a storage key nor a URL",
        not leaky,
        f"fields={sorted(fields)}"
        if not leaky
        else f"LEAKS {sorted(leaky)} -- a presigned URL in a 200-row list is 200 "
        "leaked capabilities",
    )
except Exception as exc:  # noqa: BLE001
    not_measured("75. download contract", f"{type(exc).__name__}: {exc}")


print("\n-- 76  the harness that would otherwise go green by deletion --")

# S11 in `agentic_check.py` asserts the string "handouts.content" appears in no
# SQL emitted by the list route. That assertion is correct today and becomes
# UNFALSIFIABLE the moment the column is dropped -- it would pass forever while
# measuring nothing. This case exists so that the day someone drops the column,
# something goes red on purpose.
try:
    agentic_src = (ROOT / "scripts/agentic_check.py").read_text(encoding="utf-8")
    models_src = (ROOT / "backend/app/db/models.py").read_text(encoding="utf-8")

    s11_greps_content = 'handouts.content' in agentic_src
    column_exists = "content: Mapped[bytes | None]" in models_src

    check(
        "76. S11's subject still exists -- `handouts.content` is not dropped",
        (not s11_greps_content) or column_exists,
        "S11 greps for a column that is gone; it can no longer fail"
        if s11_greps_content and not column_exists
        else f"greps={s11_greps_content} column={column_exists}",
    )
except Exception as exc:  # noqa: BLE001
    not_measured("76. S11 subject", f"{type(exc).__name__}: {exc}")



print("\n-- 77  the three guards against the WRONG fix for a keyless download --")

# ---------------------------------------------------------------------------
# Feature 03 of `new features/15-failure-paths/`. THE DEFECT: a handout row that
# is `ready` with `storage_key IS NULL` answered 500 on the R2 road, because the
# `with_content=not storage.enabled()` argument to `_load_owned` in
# `download_handout` decides whether to undefer `content` from a fact about the
# DEPLOYMENT, while the branch it is standing in for -- `if storage.enabled()
# and handout.storage_key:` -- is a fact about the ROW. Touching the unloaded
# attribute at the Postgres fallthrough raised `MissingGreenlet`, which names
# neither the column nor the line nor the table, and reaches the browser as a
# CORS error.
#
# THE ANCHORS ABOVE ARE NAMED, NOT NUMBERED, AND THAT IS DELIBERATE. Every
# citation in this block was a `handouts.py:NNN` line number written against the
# pre-fix tree, and the fix moved every one of them -- `:684`, cited twice as
# "the read that raised", landed after the edit on the redirect gate, which a
# reader chasing the defect would plausibly accept. A stale number is worse than
# no number, because it points confidently somewhere wrong. Cite the function,
# the argument or the case id; those survive an edit.
#
# All three cases in this block are GREEN TODAY and that is exactly why they are
# here -- build.md rule 3. Two fixes pass every live case below and cost
# something nothing else in this repository would ever notice:
#
#   R11  un-defer `content` on the mapper. `conversations.py:501-513` replays a
#        thread by selecting whole `Handout` ORM objects with no `undefer`, so
#        every message replay would start dragging bytea. The symptom is the app
#        getting slower months later with nothing to blame.
#   R10  default `with_content` to True, or delete the keyword because its one
#        caller stopped passing it. Same cost, one layer up.
#
# Read off the MAPPER and off the SIGNATURE, never off the source. That is case
# 75b's own lesson arriving one block later: grepping the class body for
# "content" went red against a docstring documenting the ABSENCE of a `content`
# field. A docstring is a claim; `__mapper__.attrs` is the fact.
# ---------------------------------------------------------------------------
try:
    from app.db.models import Handout as _Handout

    deferred_flags = {
        name: _Handout.__mapper__.attrs[name].deferred
        for name in ("content", "storage_key", "byte_size")
    }
    # BOTH halves in one condition, and the second half is not decoration. A
    # mapper that reported every column deferred would satisfy `content is True`
    # while meaning nothing. `storage_key` and `byte_size` are the two columns
    # `models.py:912-915` documents as deliberately NOT deferred -- the list
    # route reads both -- so their being False is what makes the flag
    # discriminating rather than constant.
    check(
        "77. `Handout.content` is still deferred ON THE MAPPER",
        deferred_flags["content"] is True
        and deferred_flags["storage_key"] is False
        and deferred_flags["byte_size"] is False,
        f"deferred={deferred_flags}",
    )

    from app.api.handouts import _load_owned as _load_owned_fn

    _sig = inspect.signature(_load_owned_fn)
    _param = _sig.parameters.get("with_content")
    # KEYWORD_ONLY is asserted, not assumed. `with_content` sits after a bare
    # `*` today; if it ever became positional, a caller could opt a route into
    # loading the bytea by argument ORDER, which is the one way to do it that
    # reads as correct at the call site.
    check(
        "77b. `_load_owned(*, with_content=...)` exists, is keyword-only, defaults False",
        _param is not None
        and _param.default is False
        and _param.kind is inspect.Parameter.KEYWORD_ONLY,
        f"signature={_sig}",
    )
except Exception as exc:  # noqa: BLE001
    not_measured("77. deferred-column guards", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 77c. THE DEFENCE-IN-DEPTH PREDICATE, ASSERTED INSTEAD OF CLAIMED.
#
# The fix for this defect added a second statement in `download_handout` that
# fetches a handout's bytes, and it repeats `Handout.agent_id == agent.id` even
# though `_load_owned` has already proved ownership. The comment above that
# statement argues the repetition is free and that the property worth having is
# *"no statement in this module fetches a handout without naming its agent"* --
# a property it describes as establishable by grep.
#
# NOBODY WAS GREPPING IT. MEASURED 2026-08-23: delete
# `Handout.agent_id == agent.id` from that refetch and BOTH modes of this file
# stay fully green -- offline all-pass, `--live` all-pass -- because
# `_load_owned` already scoped the row, so the behaviour is identical and no
# assertion about a response can see the deletion. A redundant safety check is
# invisible by construction: that is what makes it defence in depth, and it is
# also what makes it deletable by a tidy edit with nothing going red.
#
# Case 76 in this same file is the pattern -- "the harness that would otherwise
# go green by deletion" -- written for exactly this shape. This is its sibling
# one module over.
#
# AST, NOT REGEX, and the difference is load-bearing twice. A regex over source
# would match the string inside the very comment that makes the argument, so the
# guard would be satisfied by its own justification -- `deck_check.py` case 14
# has the same scar, where a scan matched its own source line. And a chain like
# `select(func.count()).select_from(Handout).where(...)` names `Handout` nowhere
# in the `select(...)` arguments, so anything anchored on those arguments would
# miss the quota query entirely.
#
# The rule asserted is the comment's own words: every `select(...)` chain in
# `app/api/handouts.py` that mentions `Handout` at all must also mention
# `Handout.agent_id`. Today four chains qualify and four comply.
# ---------------------------------------------------------------------------
try:
    import ast

    _src = (ROOT / "backend/app/api/handouts.py").read_text(encoding="utf-8")
    _tree = ast.parse(_src)

    # Keyed on `id()` because ast nodes are not hashable-by-value and two
    # syntactically identical subtrees must stay distinguishable. The tree is
    # held in `_tree` for the whole block, so no node is collected and no id is
    # reused underneath this map.
    _parents: dict[int, ast.AST] = {}
    for _node in ast.walk(_tree):
        for _child in ast.iter_child_nodes(_node):
            _parents[id(_child)] = _node

    def _outermost(node: ast.AST) -> ast.AST:
        """Climb to the end of a `select(...).where(...).order_by(...)` chain.

        Only through the `.value` of an Attribute and the `.func` of a Call --
        i.e. only along the fluent chain itself. Climbing further would swallow
        the enclosing `await db.scalar(...)` and, worse, an assignment's whole
        right-hand side, which is how a guard like this quietly starts matching
        text that has nothing to do with the statement.
        """
        while True:
            parent = _parents.get(id(node))
            if isinstance(parent, ast.Attribute) and parent.value is node:
                node = parent
            elif isinstance(parent, ast.Call) and parent.func is node:
                node = parent
            else:
                return node

    _chains: dict[int, str] = {}
    for _node in ast.walk(_tree):
        if (
            isinstance(_node, ast.Call)
            and isinstance(_node.func, ast.Name)
            and _node.func.id == "select"
        ):
            _chain = _outermost(_node)
            _chains[id(_chain)] = ast.unparse(_chain)

    _handout_chains = [c for c in _chains.values() if "Handout" in c]
    _unscoped = [c for c in _handout_chains if "Handout.agent_id" not in c]

    if not _handout_chains:
        # An empty capture is not a passing capture -- PLAN.md section 3.6, and
        # the same floor 78b and 78d carry. `not _unscoped` over zero chains is
        # green forever the day this module stops calling `select` by that name
        # or the file is renamed, and it would be green while measuring nothing.
        not_measured(
            "77c. every `select` of a handout in `app/api/handouts.py` names its agent",
            "no `select(...)` chain mentioning `Handout` was found, so nothing was inspected",
        )
    else:
        check(
            "77c. every `select` of a handout in `app/api/handouts.py` names its agent",
            not _unscoped,
            f"{len(_handout_chains)} chain(s) mention Handout, all name `agent_id`"
            if not _unscoped
            else f"{len(_unscoped)}/{len(_handout_chains)} UNSCOPED: "
            + " | ".join(ascii(" ".join(c.split())[:120]) for c in _unscoped),
        )
except Exception as exc:  # noqa: BLE001
    not_measured("77c. agent-scoped handout selects", f"{type(exc).__name__}: {exc}")


# ===========================================================================
# --live -- EXECUTE the download route against the real database.
#
# WHY THIS MODE EXISTS. Everything above this line reads source, introspects a
# pydantic model and drives a fake S3 client. None of that can execute a
# request, so a route that IMPORTS and does not RUN is invisible to it -- which
# is precisely how the keyless-download 500 survived a fully green file. It is
# the same argument `admin_check.py:262-268` makes for its own `--live` block
# after `GET /api/admin/spend` returned 500 on every request while every offline
# case passed, and it is the same misleading symptom: the handler raises before
# `CORSMiddleware` can add its headers, so the browser blames CORS.
#
# CLAUDE.md states the rule this mode is an instance of: a layer-1 harness
# cannot prove a query runs, only that it was written.
#
#     backend/.venv/Scripts/python.exe scripts/storage_check.py --live
#     backend/.venv/Scripts/python.exe scripts/storage_check.py --cleanup
# ===========================================================================
_LIVE = "--live" in sys.argv
_CLEANUP = "--cleanup" in sys.argv

if _LIVE or _CLEANUP:
    import asyncio
    import logging

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, select

    from app.auth.deps import current_user
    from app.config import settings as _settings
    from app.db.models import Agent, Handout, User
    from app.db.session import SessionLocal
    from app.main import app

    # ---------------------------------------------------------------------
    # THE FIXTURE IDENTITY. Read the module docstring before changing any of
    # these three constants: this harness writes into the SHARED production
    # database, so the row it hangs its fixture on must be one it created
    # itself. `google_sub` is the key rather than `email` for the same reason
    # the application keys on it -- it is unique and never reassigned -- and
    # `storage-check-local` cannot collide with a real Google `sub` (numeric)
    # nor with the dev-login shim's (`dev|<email>`).
    # ---------------------------------------------------------------------
    FIXTURE_SUB = "storage-check-local"
    FIXTURE_EMAIL = "storage-check@localhost"
    # Deliberately findable. These rows go into the SHARED database, and a crash
    # between insert and cleanup leaves a `ready` handout behind.
    # `seed_download_fixture.py` sets the convention: name the thing after the
    # harness that made it and say it is safe to delete. Naming makes a leak
    # FINDABLE; `--cleanup` is what makes it REMOVABLE, and the two are not the
    # same mitigation.
    FIXTURE_AGENT_NAME = "storage_check --live fixture"
    FIXTURE_NOTE = "Fixture for scripts/storage_check.py --live. Safe to delete."
    FIXTURE_CSV = "text/csv"
    # Bytes, not a string. `content` is compared for BYTE identity, so the
    # fixture carries a CRLF and a multi-byte character: anything that decoded
    # and re-encoded the body on the way out would survive a length check and
    # fail this one.
    FIXTURE_BYTES = "subsystem,kw\r\nTT&C,2.1\r\nPayload,7.8\r\n# \u2014 fixture\r\n".encode()

    async def _sweep(db) -> tuple[int, int, int]:  # noqa: ANN001
        """Delete EVERYTHING this harness owns. Returns (handouts, agents, users).

        Keyed on `google_sub`, never on the agent name, and that ordering is the
        point: the name is a label a human could have typed on a real agent,
        while the `sub` is a value only this file writes. Sweeping by name could
        delete somebody's work; sweeping by owner cannot.

        The DB would cascade anyway -- `agents.owner_user_id` and
        `handouts.agent_id` are both `ON DELETE CASCADE` -- but the deletes are
        written out so the counts can be PRINTED. A sweeper that removes rows
        silently is indistinguishable from one that found nothing, which is the
        failure this whole mode exists to stop being possible.
        """
        user = await db.scalar(select(User).where(User.google_sub == FIXTURE_SUB))
        if user is None:
            return (0, 0, 0)
        agent_ids = list(
            (await db.scalars(select(Agent.id).where(Agent.owner_user_id == user.id))).all()
        )
        n_handouts = 0
        if agent_ids:
            result = await db.execute(
                delete(Handout).where(Handout.agent_id.in_(agent_ids))
            )
            n_handouts = result.rowcount or 0
            await db.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        await db.delete(user)
        await db.commit()
        return (n_handouts, len(agent_ids), 1)

    async def _run_cleanup() -> None:
        """Standalone, idempotent, and safe to run at any time or after a crash.

        The escape hatch `--live` shipped without. CLAUDE.md's Background-jobs
        section records the shape this prevents: a run that wedges leaves rows
        nothing sweeps, and "the only escape hatch is the user deleting the row".
        """
        print("\n" + "=" * 74)
        print("--cleanup: removing everything owned by this harness")
        print("=" * 74)
        print(f"  google_sub = {FIXTURE_SUB!r}  email = {FIXTURE_EMAIL!r}")
        async with SessionLocal() as db:
            handouts, agents, users = await _sweep(db)
        if users:
            print(f"  DELETED {users} user, {agents} agent(s), {handouts} handout(s)")
        else:
            print("  nothing to clean up -- no fixture user in the database")

    async def _live() -> None:
        route = _settings.storage_route
        # PRINTING THIS IS MANDATORY, not informational. Run with
        # STORAGE_ROUTE=postgres and `with_content` is True, the defect is
        # structurally unreachable, and 78 would pass having measured nothing.
        # The tell for such a vacuous run is the ABSENCE of a printed route.
        print(f"  storage_route = {route!r}   <- decides which cases can be measured")
        on_r2 = route == "r2"

        # ------------------------------------------------------------------
        # THE OWNER IS CREATED, NEVER FOUND. `DATABASE_URL` points at the shared
        # production database; the first draft of this block chose its victim
        # with `select(User).limit(1)`, which has no ORDER BY, so which of the
        # real people in `users` got a "storage_check --live fixture" agent in
        # their dashboard was whatever Postgres returned first and could differ
        # between runs. That is a harness corrupting the data it is testing
        # against -- worse than the defect it was written to catch.
        #
        # `slice_check.py` (`slice-check-local`) and `ui_check.py`
        # (`ui-check@groundwork.local`) already had the pattern. Get-or-create
        # rather than plain create, because a previous hard kill may have left
        # the user behind and this mode has to stay re-runnable; the `finally`
        # below removes it either way, so a clean run leaves the database
        # byte-for-byte as it found it.
        # ------------------------------------------------------------------
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.google_sub == FIXTURE_SUB))
            created_user = user is None
            if user is None:
                user = User(
                    id=uuid.uuid4(),
                    google_sub=FIXTURE_SUB,
                    email=FIXTURE_EMAIL,
                    name="Storage Check",
                    role="user",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
        print(
            f"  fixture owner {'CREATED' if created_user else 'reused'}: "
            f"{FIXTURE_EMAIL} ({user.id}) -- deleted again below; "
            f"`--cleanup` sweeps it if this run is killed"
        )

        agent_id = uuid.uuid4()
        ids = {
            "keyless_ready": uuid.uuid4(),
            "keyless_empty": uuid.uuid4(),
            "keyless_pending": uuid.uuid4(),
            "keyed_ready": uuid.uuid4(),
        }
        keyed_key = _storage.handout_key(agent_id, ids["keyed_ready"], FIXTURE_CSV)

        # A whole disposable AGENT rather than rows grafted onto a real one.
        # Three reasons: a leak is then one obviously-named row in a dashboard
        # instead of a mystery file in somebody's working panel;
        # `handouts.agent_id` is ON DELETE CASCADE so one delete removes
        # everything; and `seed_download_fixture.py`'s idempotence check
        # ("if existing: nothing to do") would be confused by extra handouts
        # appearing under the agent it owns.
        async with SessionLocal() as db:
            db.add(
                Agent(
                    id=agent_id,
                    owner_user_id=user.id,
                    name=FIXTURE_AGENT_NAME,
                    description=FIXTURE_NOTE,
                )
            )
            for hid, row_status, key, body, label in [
                (ids["keyless_ready"], "ready", None, FIXTURE_BYTES, "keyless ready, real bytes"),
                (ids["keyless_empty"], "ready", None, None, "keyless ready, NO bytes"),
                (ids["keyless_pending"], "pending", None, FIXTURE_BYTES, "keyless, not ready"),
                (ids["keyed_ready"], "ready", keyed_key, FIXTURE_BYTES, "keyed ready"),
            ]:
                db.add(
                    Handout(
                        id=hid,
                        agent_id=agent_id,
                        kind="sheet",
                        title=f"{FIXTURE_AGENT_NAME}: {label}",
                        filename="keyless-fixture.csv",
                        mime_type=FIXTURE_CSV,
                        byte_size=len(body or b""),
                        status=row_status,
                        origin="recipe",
                        content=body,
                        storage_key=key,
                    )
                )
            await db.commit()
        print(f"  inserted fixture agent {agent_id} with {len(ids)} handout(s)")
        # NOTE: no object is ever PUT. 78c only signs a URL, and signing is a
        # local HMAC with no round trip -- the same property S11b's control
        # relies on. So there is nothing in the bucket to clean up, and 78c is
        # measurable on a machine that cannot reach R2 at all.

        def path_for(handout_id) -> str:  # noqa: ANN001
            return f"/api/agents/{agent_id}/handouts/{handout_id}/download"

        async def get(handout_id):  # noqa: ANN001, ANN202
            # `raise_app_exceptions=False` is R8 and it is not a style choice.
            # Under the RED run this route raises `MissingGreenlet`; a default
            # transport would propagate it OUT of this file, aborting with a
            # traceback and ZERO recorded failures -- green-by-abort, the shape
            # the `_Missing` sentinel at the top of this file already exists to
            # prevent. The defect must arrive as a printed [FAIL] naming a 500.
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://live") as client:
                return await client.get(path_for(handout_id))

        async def get_with_sql(handout_id):  # noqa: ANN001, ANN202
            """One request, plus every statement the engine emitted during it."""
            statements: list[str] = []

            class _Capture(logging.Handler):
                # `entry`, not `record`: `agentic_check.py`'s S11 names the same
                # trap -- there is a module-level `record()` helper in that file
                # and a parameter of that name inside a handler is a trap for
                # whoever next wants to report something from in here.
                def emit(self, entry):  # noqa: ANN001, ANN202
                    statements.append(entry.getMessage())

            sql_log = logging.getLogger("sqlalchemy.engine.Engine")
            handler = _Capture()
            prior_level = sql_log.level
            sql_log.addHandler(handler)
            sql_log.setLevel(logging.INFO)
            try:
                response = await get(handout_id)
            finally:
                sql_log.removeHandler(handler)
                sql_log.setLevel(prior_level)
            return response, statements

        # Identity is overridden; AUTHORISATION IS NOT. `owned_agent` still runs
        # and still compares `agents.owner_user_id` against this row, so a
        # regression that dropped the dependency surfaces as a 403 rather than
        # being hidden by the stub. Same argument as admin_check.py:311-315.
        app.dependency_overrides[current_user] = lambda: user
        try:
            print("\n-- 78  THE DEFECT: a `ready` row with no storage_key, on the R2 road --")

            if not on_r2:
                for case in (
                    "78. a `ready` keyless row downloads: 200, byte-identical, quoted disposition",
                    "78b. the bytea is READ on the keyless road and NOT read on the keyed one",
                    "78c. a `ready` KEYED row still answers 302 with a presigned Location",
                ):
                    not_measured(
                        case,
                        f"storage_route={route!r}: `with_content` is True on that road, so "
                        "the deferred read cannot fail and the redirect is never taken. "
                        "Set STORAGE_ROUTE=r2 (or unset it) and re-run",
                    )
            else:
                keyless_response, keyless_sql = await get_with_sql(ids["keyless_ready"])
                disposition = keyless_response.headers.get("content-disposition", "")
                check(
                    "78. a `ready` keyless row downloads: 200, byte-identical, quoted disposition",
                    keyless_response.status_code == 200
                    and keyless_response.content == FIXTURE_BYTES
                    and disposition.startswith('attachment; filename="')
                    and disposition.endswith('"')
                    and "\r" not in disposition
                    and "\n" not in disposition,
                    f"status={keyless_response.status_code} "
                    f"bytes={len(keyless_response.content)}/{len(FIXTURE_BYTES)} "
                    f"disposition={ascii(disposition)} body={ascii(keyless_response.text[:110])}",
                )

                keyed_response, keyed_sql = await get_with_sql(ids["keyed_ready"])
                keyless_loads = [s for s in keyless_sql if "handouts.content" in s]
                keyed_loads = [s for s in keyed_sql if "handouts.content" in s]
                if not keyless_sql or not keyed_sql:
                    # S11's trap inherited verbatim. `'handouts.content' not in
                    # statements` over an EMPTY capture is green forever if the
                    # `sqlalchemy.engine.Engine` logger stops emitting -- a
                    # logging config change, an echo default, a library rename.
                    not_measured(
                        "78b. the bytea is READ on the keyless road and NOT read on the keyed one",
                        f"the SQL log captured {len(keyless_sql)}/{len(keyed_sql)} "
                        "statements, so nothing was inspected",
                    )
                else:
                    # ONE MECHANISM, BOTH DIRECTIONS. The keyed leg is the guard
                    # against R10 -- a fix that undefers `content` at the
                    # `_load_owned` call in `download_handout`, unconditionally
                    # rather than on `with_content`, answers 78, 79 and 79b
                    # perfectly while
                    # putting the bytea read back on every download. The keyless
                    # leg is the POSITIVE CONTROL that stops the keyed leg being
                    # passed by a capture that never sees anything at all.
                    #
                    # AND THE FIRST DRAFT OF THIS CASE WENT GREEN ON THE RED RUN,
                    # which is why the third term is here. Measured 2026-08-23
                    # against unmodified code: the keyless request 500s, and the
                    # capture STILL contains
                    #
                    #     SELECT handouts.content AS handouts_content
                    #     FROM handouts WHERE handouts.id = $1::UUID
                    #
                    # because SQLAlchemy logs a statement in `_execute_context`
                    # BEFORE the driver adaptation reaches `await_only()` and
                    # raises `MissingGreenlet`. So "the SQL mentions the column"
                    # is satisfied by a deferred load that was ATTEMPTED and
                    # died -- a positive control satisfied by the defect it was
                    # written to control for, i.e. exactly the case-written-to-
                    # pass that build.md rule 2 forbids.
                    #
                    # A read that raised is not a read. The status code is what
                    # separates the two, so it is asserted HERE as well as in 78:
                    # it is not a duplicate of 78's byte comparison, it is the
                    # precondition for this case's own evidence being legible.
                    keyless_read_completed = keyless_response.status_code < 500
                    check(
                        "78b. the bytea is READ on the keyless road and NOT on the keyed one",
                        bool(keyless_loads) and keyless_read_completed and not keyed_loads,
                        f"keyless={len(keyless_loads)}/{len(keyless_sql)} stmts mention it "
                        f"(status={keyless_response.status_code}"
                        + (
                            "; the SELECT was logged and then died in MissingGreenlet, "
                            "so it is an ATTEMPT, not a read"
                            if not keyless_read_completed
                            else ""
                        )
                        + f"), keyed={len(keyed_loads)}/{len(keyed_sql)} "
                        f"(status={keyed_response.status_code})",
                    )

                # 78c. THE CASE THAT STOPS "just read Postgres always" FROM
                # PASSING 78. A fix that drops the branch and always undefers
                # answers 78, 79 and 79b perfectly while putting the bytea read
                # back on every download and deleting the entire reason the R2
                # route exists. Nothing else in this repository catches it: S8,
                # S8b, S8c, S28 and S29 all follow redirects, so they stay green
                # against a route that stopped emitting any.
                #
                # The keyed row is INSERTED by this harness rather than found in
                # the database. The audit specified this case as NOT MEASURED
                # when no keyed `ready` row exists -- but a guard that is absent
                # on a fresh environment is not a guard.
                presign_calls: list[str] = []
                real_presign = _storage.presigned_get_url

                def _counting(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
                    presign_calls.append(args[0] if args else kwargs.get("key", "?"))
                    return real_presign(*args, **kwargs)

                _storage.presigned_get_url = _counting  # type: ignore[assignment]
                try:
                    redirect_response = await get(ids["keyed_ready"])
                finally:
                    _storage.presigned_get_url = real_presign  # type: ignore[assignment]

                location = redirect_response.headers.get("location", "")
                check(
                    "78c. a `ready` KEYED row still answers 302 with a presigned Location",
                    redirect_response.status_code == 302
                    and len(presign_calls) == 1
                    and presign_calls[0] == keyed_key
                    and "X-Amz-Signature=" in location,
                    f"status={redirect_response.status_code} "
                    f"presign_calls={len(presign_calls)} "
                    f"signed={'X-Amz-Signature=' in location} "
                    f"location={ascii(location[:70])}",
                )

            print("\n-- 78d  THE OTHER ARM: bytes that arrive PRE-LOADED --")

            # ------------------------------------------------------------
            # The fix for 78 gave the Postgres fallthrough TWO ways for bytes to
            # arrive: fetched by hand when the load left `content` deferred, or
            # already present when it did not. Cases 78, 78b and 79b exercise
            # only the first. The second is the arm the `STORAGE_ROUTE=postgres`
            # ROLLBACK runs on -- which is the road this entire defect exists to
            # keep passable -- so leaving it unmeasured would be repairing a
            # rollback and then not checking it.
            #
            # It went unmeasured for a structural reason rather than by
            # oversight: a process has exactly one `storage_route`, so whichever
            # arm a run cannot reach is the arm it prints NOT MEASURED for, and
            # the R2 setting is the one anybody develops on. Patching
            # `storage.enabled` for a single request removes that. It is the ONLY
            # thing the handler reads about the route -- once to choose
            # `with_content`, once to choose the redirect branch -- so returning
            # False puts the route in precisely the state `STORAGE_ROUTE=postgres`
            # puts it in. The claim being proved is about the HANDLER, not about
            # a whole Postgres deployment, and it is worth saying so: nothing
            # here exercises a bucket-free CONFIG, only a bucket-free request.
            #
            # THE PAIR, per build.md rule 3. The positive is that the bytes come
            # back right; the negative is that they were fetched ONCE. Deleting
            # the `else` and refetching unconditionally leaves the bytes perfect
            # while making every rollback download pay a second statement for a
            # column it was already holding -- a regression no assertion about
            # the response body can see.
            #
            # MEASURED, 2026-08-23, against exactly that variant: 78, 78b, 78c,
            # 79 and 79b all stayed green -- status 200, 52/52 bytes, identical
            # disposition -- and this case alone went red, at 2 statements
            # mentioning the column where 1 is correct. That is the whole reason
            # it is counted statements and not bytes.
            # ------------------------------------------------------------
            _real_enabled = _storage.enabled
            if on_r2:
                _storage.enabled = lambda: False  # type: ignore[assignment]
            try:
                preloaded_response, preloaded_sql = await get_with_sql(ids["keyless_ready"])
            finally:
                _storage.enabled = _real_enabled  # type: ignore[assignment]

            preloaded_loads = [s for s in preloaded_sql if "handouts.content" in s]
            if not preloaded_sql:
                # 78b's trap, inherited for the same reason: counting matches in
                # an EMPTY capture is green forever the day the
                # `sqlalchemy.engine.Engine` logger stops emitting.
                not_measured(
                    "78d. on the rollback arm the bytes arrive PRE-LOADED, fetched exactly once",
                    "the SQL log captured 0 statements, so nothing was inspected",
                )
            else:
                check(
                    "78d. on the rollback arm the bytes arrive PRE-LOADED, fetched exactly once",
                    preloaded_response.status_code == 200
                    and preloaded_response.content == FIXTURE_BYTES
                    and len(preloaded_loads) == 1,
                    f"status={preloaded_response.status_code} "
                    f"bytes={len(preloaded_response.content)}/{len(FIXTURE_BYTES)} "
                    f"statements mentioning the column={len(preloaded_loads)}/"
                    f"{len(preloaded_sql)} (want exactly 1: the undeferred entity "
                    f"load, and NO hand-written refetch beside it)",
                )

            print("\n-- 79  the status gate still runs BEFORE anything reads content --")

            # GREEN TODAY, and the wrong fix it kills is "move the load up to
            # the `_load_owned` call, unconditionally, above the status gate".
            # The `handout.status != "ready"` gate is the only reason production's
            # keyless rows (every one of them `failed`) are safe, so a fix that
            # reorders those two gates trades one 500 for a wider one.
            # `download_ui_check.py` D4 is the browser sibling of this case.
            pending_response = await get(ids["keyless_pending"])
            check(
                "79. a keyless row that is NOT ready still answers 409, never 500",
                pending_response.status_code == 409
                and "not ready" in pending_response.text.lower(),
                f"status={pending_response.status_code} "
                f"body={ascii(pending_response.text[:110])}",
            )

            if not on_r2:
                not_measured(
                    "79b. a `ready` keyless row with NO bytes answers 409, never 500",
                    f"storage_route={route!r}: `content` is loaded on that road, so the "
                    "None check reads a loaded attribute and cannot raise",
                )
            else:
                # The SECOND latent 500 at the same place, and it is the one that
                # survives the obvious fix. The Postgres fallthrough's
                # `if content is None:` test reads the column in order to decide
                # whether to 409; before the fix that read was
                # `handout.content` on an attribute the load had left deferred,
                # so it raised before the 409 it exists to produce. The row with
                # neither a key nor bytes -- the exact row that 409 was written
                # for -- was the row that never reached its own error message.
                empty_response = await get(ids["keyless_empty"])
                check(
                    "79b. a `ready` keyless row with NO bytes answers 409, never 500",
                    empty_response.status_code == 409
                    and "no stored file" in empty_response.text.lower(),
                    f"status={empty_response.status_code} "
                    f"body={ascii(empty_response.text[:110])}",
                )
        finally:
            # R12. Keyed on the ids inserted, in a `finally`, so a raise
            # anywhere above still takes the rows with it -- and it now takes
            # the OWNER too, so a completed run leaves nothing at all behind.
            # A `finally` still does not survive a hard kill or a lost
            # connection, which is what `--cleanup` is for; the two are layers,
            # not alternatives.
            app.dependency_overrides.pop(current_user, None)
            async with SessionLocal() as db:
                await db.execute(delete(Handout).where(Handout.id.in_(list(ids.values()))))
                await db.execute(delete(Agent).where(Agent.id == agent_id))
                await db.commit()
                # Sweep by owner as well as by id. If a case above ever inserts
                # a row this list does not name, the id-keyed delete misses it
                # and this does not.
                swept = await _sweep(db)
            print(
                f"\n  cleaned up fixture agent {agent_id} and {len(ids)} handout(s); "
                f"swept owner ({swept[2]} user, {swept[1]} agent(s), "
                f"{swept[0]} handout(s) remaining)"
            )

    # ONE `asyncio.run`, NOT ONE PER MODE, and this was a real bug for the length
    # of one edit. `SessionLocal`'s engine builds its connection pool against the
    # loop that first used it; a second `asyncio.run(...)` gets a NEW loop, and
    # the pooled asyncpg connection then tries to schedule on the closed one --
    # `RuntimeError: Event loop is closed`, thrown from `call_soon` in
    # `base_events`, naming nothing about SQLAlchemy or about this file.
    #
    # `--cleanup` alone and `--live` alone both worked; only `--live --cleanup`
    # together crashed, which is exactly the combination a person reaches for
    # after a leak -- sweep, then re-run. Measured 2026-08-23. It aborted with a
    # traceback AFTER the mode header had printed, i.e. green-by-abort wearing a
    # different hat: `raise_app_exceptions=False` protects the REQUESTS inside
    # `_live`, and nothing protects the runner around it.
    async def _main() -> None:
        if _CLEANUP:
            await _run_cleanup()
        if _LIVE:
            print("\n" + "=" * 74)
            print("--live: the download route, EXECUTED against the real database")
            print("=" * 74)

            # The offline cases left a FakeS3 installed (73c ends with one). Put
            # the real client back before executing anything: 78c asserts a
            # PRESIGNED Location, and against the recorder that string would be
            # one this file wrote itself. This is the `_REAL_GET_CLIENT` comment
            # at the top of the file biting from the other direction -- there the
            # fake outlived a `cache_clear`, here it would outlive the whole
            # offline section.
            if _REAL_GET_CLIENT is not None:
                _storage.get_client = _REAL_GET_CLIENT  # type: ignore[assignment]

            await _live()

    asyncio.run(_main())


print()
print("=" * 74)
if unmeasured:
    print(f"{len(unmeasured)} NOT MEASURED -- treat as unknown, never as passing:")
    for row in unmeasured:
        print(f"  - {row}")
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all storage_check cases passed")
