"""Regression guard for the turso_memory CLI register_cli contract.

The framework passes the ALREADY-CREATED parser for ``hermes turso_memory``
to ``register_cli(subparser)``.  The function must NOT call
``subparser.add_parser(...)`` — it should call
``subparser.add_subparsers(...)`` to install subcommands.
"""
from __future__ import annotations

import argparse


def test_register_cli_stats_subcommand():
    """parse_args(["stats"]) must succeed and set .tm_cmd == "stats"."""
    from plugins.memory.turso_memory.cli import register_cli

    parser = argparse.ArgumentParser(prog="hermes turso-memory")
    register_cli(parser)
    args = parser.parse_args(["stats"])
    assert args.tm_cmd == "stats"


def test_register_cli_search_subcommand():
    """parse_args(["search", "foo"]) must succeed and set .query == "foo"."""
    from plugins.memory.turso_memory.cli import register_cli

    parser = argparse.ArgumentParser(prog="hermes turso-memory")
    register_cli(parser)
    args = parser.parse_args(["search", "foo"])
    assert args.tm_cmd == "search"
    assert args.query == "foo"


def test_register_cli_reindex_subcommand():
    """parse_args(["reindex"]) must succeed and set .tm_cmd == "reindex"."""
    from plugins.memory.turso_memory.cli import register_cli

    parser = argparse.ArgumentParser(prog="hermes turso-memory")
    register_cli(parser)
    args = parser.parse_args(["reindex"])
    assert args.tm_cmd == "reindex"


def test_register_cli_sets_func_default():
    """parser.get_default("func") must return the _run callable."""
    from plugins.memory.turso_memory.cli import register_cli, _run

    parser = argparse.ArgumentParser(prog="hermes turso-memory")
    register_cli(parser)
    assert parser.get_default("func") is _run
