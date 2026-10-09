#!/usr/bin/env python3
"""Entry point for running the hook straight from a source checkout. After a pip install, use `riskmon hook` instead."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from riskmon.hook import cli, fallback, main, pre_tool_output  # noqa: E402,F401

if __name__ == "__main__":
    cli()
