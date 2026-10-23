#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Launcher script for the SOF Topology2 GTK4 Visual Graph Editor & Testbench GUI."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tplg2_gui.main import main

if __name__ == "__main__":
    raise SystemExit(main())
