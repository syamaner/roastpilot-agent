"""Console entrypoint for the roastpilot-agent service."""

import argparse

from roastpilot_agent import __version__


def main() -> int:
    """Parse arguments and run the agent service.

    Serving the FastAPI app (and the ``--replay`` mode) lands in E7/E10;
    the scaffold entrypoint only supports ``--help`` and ``--version``.
    """
    parser = argparse.ArgumentParser(
        prog="roastpilot-agent",
        description="Deterministic agent harness for autonomous coffee roasting.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0
