#!/usr/bin/env python3
# Author: joelsnl and Anthropic Claude
"""
HuaEPUB — Qt (PySide6) entry point.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.utils import sanitize_runtime_env
sanitize_runtime_env()

from gui.app import run


if __name__ == "__main__":
    raise SystemExit(run())
