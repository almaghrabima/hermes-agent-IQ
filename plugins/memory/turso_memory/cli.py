"""`hermes turso-memory <stats|reindex|search>` — thin operational commands."""
from __future__ import annotations


def register_cli(subparsers) -> None:
    p = subparsers.add_parser("turso-memory", help="Inspect the turso_memory store")
    sub = p.add_subparsers(dest="tm_cmd", required=True)
    sub.add_parser("stats", help="row counts + embedded coverage")
    s = sub.add_parser("search", help="hybrid search")
    s.add_argument("query")
    sub.add_parser("reindex", help="re-embed rows whose model != active encoder")
    p.set_defaults(func=_run)


def _run(args) -> int:
    from plugins.memory.turso_memory import TursoMemoryProvider

    prov = TursoMemoryProvider()
    prov.initialize(session_id="cli")
    try:
        store = prov._store
        if store is None:
            print("error: store not initialised")
            return 1
        if args.tm_cmd == "stats":
            total = store.count()
            embedded = store._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            print(f"memories: {total}  embedded: {embedded}")
        elif args.tm_cmd == "search":
            import json

            print(json.dumps(prov._recall(args.query, 10), indent=2))
        elif args.tm_cmd == "reindex":
            n = prov._reindex()
            print(f"re-embedded {n} rows")
        return 0
    finally:
        prov.shutdown()
