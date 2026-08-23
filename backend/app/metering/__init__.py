"""Metering: what every model call cost, recovered rather than computed.

This package exists because OpenRouter reports the real cost of every call and
`langchain-openai` discards it on the streaming path. Nothing here estimates a
chat cost, and nothing here changes a request body -- both are safety properties
rather than preferences, and both are pinned by harness cases
(`scripts/metering_check.py`, `scripts/llm_check.py` case 31).

    context.py   who/what/why, as a task-local ContextVar, plus the per-turn buffer
    chat.py      ChatOpenAI minus one lossy normalisation
    meter.py     the callback, the record, the sink

Design record: `new features/14-admin-observability/01-metering.md`.
"""

from app.metering.context import (
    CALL_KINDS,
    MeterScope,
    collect_usage,
    current_scope,
    emit_record,
    meter_as,
)
from app.metering.meter import (
    USAGE_KEY,
    LoggingSink,
    UsageMeter,
    UsageRecord,
    UsageSink,
)

__all__ = [
    "CALL_KINDS",
    "USAGE_KEY",
    "LoggingSink",
    "MeterScope",
    "UsageMeter",
    "UsageRecord",
    "UsageSink",
    "collect_usage",
    "current_scope",
    "emit_record",
    "meter_as",
]
