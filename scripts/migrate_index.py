"""Blue/green migration of a Pinecone index.

The right way to change an index's REGION or CLOUD, which are fixed at
creation. Builds the replacement alongside the original and copies vectors
across, so nothing is deleted and the old index stays queryable throughout.
This is why `create_index.py --recreate` refuses to touch a populated index:
the destructive path is never the correct one.

Cost depends entirely on WHAT is changing:

  region / cloud / index name   vectors copied verbatim, NO re-embedding
  namespace scheme              vectors copied into new namespaces, NO re-embedding
  dimension / embedding model   vectors are meaningless in the new space; you
                                must re-embed from chunks.text in Postgres
                                (see scripts/reindex_from_postgres.py)

Usage:
    python scripts/migrate_index.py --to-region ap-southeast-1 --new-name my-index-v2 --dry-run
    python scripts/migrate_index.py --to-region ap-southeast-1 --new-name my-index-v2

Afterwards, verify, point PINECONE_INDEX_NAME at the new index, redeploy, and
only then delete the old one by hand.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec

ROOT = Path(__file__).resolve().parent.parent
BATCH = 100


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=None, help="defaults to PINECONE_INDEX_NAME")
    p.add_argument("--new-name", required=True)
    p.add_argument("--to-region", required=True)
    p.add_argument("--to-cloud", default="aws")
    p.add_argument("--namespace-map", default=None,
                   help="python dict literal, e.g. \"{'user_1':'agent_1'}\"")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("PINECONE_API_KEY")
    if not key:
        print("ERROR: PINECONE_API_KEY not set")
        return 1

    pc = Pinecone(api_key=key)
    source = args.source or os.getenv("PINECONE_INDEX_NAME", "agentic-rag-ntu")

    names = {ix["name"] for ix in pc.list_indexes()}
    if source not in names:
        print(f"ERROR: source index '{source}' not found")
        return 1
    if args.new_name == source:
        print("ERROR: --new-name must differ from the source")
        return 1

    src_desc = pc.describe_index(source)
    dim = src_desc["dimension"]
    metric = src_desc["metric"]
    src_spec = (src_desc.get("spec") or {}).get("serverless", {})

    src = pc.Index(source)
    stats = src.describe_index_stats()
    namespaces = dict(stats.get("namespaces") or {})
    total = stats.get("total_vector_count") or 0

    ns_map = {}
    if args.namespace_map:
        import ast
        ns_map = ast.literal_eval(args.namespace_map)

    print(f"source     : {source}  ({src_spec.get('cloud')}/{src_spec.get('region')})")
    print(f"target     : {args.new_name}  ({args.to_cloud}/{args.to_region})")
    print(f"dimension  : {dim}   metric: {metric}   (carried over unchanged)")
    print(f"vectors    : {total} across {len(namespaces)} namespace(s)")
    for ns, meta in namespaces.items():
        dest = ns_map.get(ns, ns)
        arrow = f" -> {dest}" if dest != ns else ""
        print(f"   {ns or '(default)':30} {meta.get('vector_count')}{arrow}")
    print("\nVectors are copied verbatim - no embedding calls, so this costs")
    print("nothing in API usage and the numbers stay bit-identical.\n")

    if args.dry_run:
        print("--dry-run; nothing created or copied.")
        return 0

    if args.new_name not in names:
        print(f"Creating '{args.new_name}'...")
        # describe_index returns an IndexTags object whose `keys` attribute is
        # None rather than a method, so dict(tags) raises a thoroughly
        # misleading "'NoneType' object is not callable". Use to_dict().
        raw = src_desc.get("tags")
        raw = raw.to_dict() if hasattr(raw, "to_dict") else (raw or {})
        tags = {str(k): str(v) for k, v in raw.items()} or None
        pc.create_index(
            name=args.new_name,
            dimension=dim,
            metric=metric,
            spec=ServerlessSpec(cloud=args.to_cloud, region=args.to_region),
            tags=tags,
        )
        for _ in range(60):
            if pc.describe_index(args.new_name)["status"]["ready"]:
                break
            time.sleep(3)
        print("  ready.\n")
    else:
        print(f"'{args.new_name}' already exists; copying into it.\n")

    dst = pc.Index(args.new_name)

    copied = 0
    for ns in namespaces:
        dest_ns = ns_map.get(ns, ns)
        token, ns_count = None, 0
        while True:
            page = src.list_paginated(namespace=ns, limit=BATCH, pagination_token=token)
            ids = [v.id if hasattr(v, "id") else v["id"] for v in (page.vectors or [])]
            if ids:
                fetched = src.fetch(ids=ids, namespace=ns)
                vectors = [
                    {
                        "id": vid,
                        "values": list(v.values),
                        "metadata": dict(v.metadata) if v.metadata else {},
                    }
                    for vid, v in (fetched.vectors or {}).items()
                ]
                if vectors:
                    dst.upsert(vectors=vectors, namespace=dest_ns)
                    ns_count += len(vectors)
                    copied += len(vectors)
            token = getattr(page.pagination, "next", None) if page.pagination else None
            if not token:
                break
        print(f"  {ns or '(default)':30} -> {dest_ns or '(default)':30} {ns_count} copied")

    print(f"\nCopied {copied} vectors. Verifying...")
    for _ in range(20):
        got = dst.describe_index_stats().get("total_vector_count") or 0
        if got >= copied:
            break
        time.sleep(3)
    got = dst.describe_index_stats().get("total_vector_count") or 0
    print(f"  source={total}  target={got}  {'OK' if got >= copied else 'MISMATCH'}")

    print("\nNext steps (the old index is untouched and still serving):")
    print(f"  1. Spot-check queries against '{args.new_name}'")
    print(f"  2. Set PINECONE_INDEX_NAME={args.new_name} locally and on Render")
    print("  3. Redeploy, confirm /api/health and a real query")
    print(f"  4. Only then delete '{source}' by hand")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
