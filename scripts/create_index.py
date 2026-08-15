"""Provision the Pinecone index for this project.

Idempotent: safe to re-run. If the index already exists, it verifies the
configuration matches what the PRD requires and reports any drift rather than
recreating anything.

Usage:  python scripts/create_index.py [--dry-run]

Requires PINECONE_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec

# --- Configuration. These four values are immutable once the index exists. ---
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "agentic-rag-ntu")
DIMENSION = 768          # gemini-embedding-2 @ output_dimensionality=768
METRIC = "cosine"
CLOUD = "aws"
# us-east-1, not Singapore: Pinecone's free plan only permits us-east-1.
# ap-southeast-1 requires the Builder plan ($20/mo). Decision recorded in PRD §6.2.
# Consequence: a trans-Pacific hop per retrieval from the Singapore backend.
REGION = "us-east-1"
EMBEDDING_MODEL = "gemini-embedding-2"

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        print("ERROR: PINECONE_API_KEY not set. Copy .env.example to .env and fill it in.")
        return 1

    index_name = os.getenv("PINECONE_INDEX_NAME", INDEX_NAME)
    pc = Pinecone(api_key=api_key)

    existing = {ix["name"] for ix in pc.list_indexes()}
    if index_name in existing:
        desc = pc.describe_index(index_name)
        print(f"Index '{index_name}' already exists - verifying configuration.\n")
        _report(desc)
        _check_drift(desc)
        return 0

    print(f"Index '{index_name}' does not exist. Will create:")
    print(f"  dimension : {DIMENSION}")
    print(f"  metric    : {METRIC}")
    print(f"  cloud     : {CLOUD}")
    print(f"  region    : {REGION}")
    print(f"  tags      : embedding_model={EMBEDDING_MODEL}, dimension={DIMENSION}\n")

    if dry_run:
        print("--dry-run set; nothing created.")
        return 0

    desc = pc.create_index(
        name=index_name,
        dimension=DIMENSION,
        metric=METRIC,
        spec=ServerlessSpec(cloud=CLOUD, region=REGION),
        tags={
            "embedding_model": EMBEDDING_MODEL,
            "dimension": str(DIMENSION),
            "project": "agentic-rag-ntu",
        },
    )
    print("Created.\n")
    _report(desc)
    return 0


def _report(desc) -> None:
    print(f"  name      : {desc['name']}")
    print(f"  dimension : {desc['dimension']}")
    print(f"  metric    : {desc['metric']}")
    print(f"  host      : {desc['host']}")
    print(f"  spec      : {desc.get('spec')}")
    print(f"  tags      : {desc.get('tags')}")
    print(f"  status    : {desc.get('status')}")


def _check_drift(desc) -> None:
    """The three values that cannot be changed after creation."""
    problems = []
    if desc["dimension"] != DIMENSION:
        problems.append(f"dimension is {desc['dimension']}, PRD requires {DIMENSION}")
    if desc["metric"] != METRIC:
        problems.append(f"metric is {desc['metric']}, PRD requires {METRIC}")
    spec = desc.get("spec") or {}
    serverless = spec.get("serverless") if isinstance(spec, dict) else None
    if serverless and serverless.get("region") != REGION:
        problems.append(f"region is {serverless.get('region')}, PRD requires {REGION}")

    if problems:
        print("\n  ** CONFIGURATION DRIFT - none of these can be altered in place: **")
        for p in problems:
            print(f"    - {p}")
        print("  Fixing means deleting the index and re-ingesting everything.")
    else:
        print("\n  Configuration matches the PRD.")


if __name__ == "__main__":
    raise SystemExit(main())
