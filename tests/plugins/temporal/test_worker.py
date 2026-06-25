import argparse
from plugins.temporal import worker


def test_setup_worker_parser_adds_worker_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="temporal_command")
    worker.setup_worker_parser(sub)
    ns = p.parse_args(["worker"])
    assert ns.temporal_command == "worker"
