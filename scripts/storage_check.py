"""Layer 1 for `app/storage.py` and the download contract. No DB, no network.

Run:  backend/.venv/Scripts/python.exe scripts/storage_check.py

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
