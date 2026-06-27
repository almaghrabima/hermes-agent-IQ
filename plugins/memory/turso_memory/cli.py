"""`hermes turso-memory <stats|reindex|search>` — thin operational commands."""
from __future__ import annotations


def register_cli(subparser) -> None:
    """Build the ``hermes turso-memory`` argparse subcommand tree.

    Called by the plugin CLI registration system during argparse setup.
    *subparser* is the ALREADY-CREATED parser for ``hermes turso-memory`` —
    do NOT call ``subparser.add_parser(...)`` here.  Mirror honcho's exact
    pattern: call ``subparser.add_subparsers(...)`` directly.
    """
    sub = subparser.add_subparsers(dest="tm_cmd", required=True)
    sub.add_parser("stats", help="row counts + embedded coverage")
    s = sub.add_parser("search", help="hybrid search")
    s.add_argument("query")
    sub.add_parser("reindex", help="re-embed rows whose model != active encoder")
    pr = sub.add_parser("prune", help="delete memories whose learned weight < floor")
    pr.add_argument("--floor", type=float, default=0.6, help="weight floor (default 0.6)")
    subparser.set_defaults(func=_run)


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
        elif args.tm_cmd == "prune":
            n = store.prune(args.floor)
            print(f"pruned {n} memories (weight < {args.floor})")
        return 0
    finally:
        prov.shutdown()
