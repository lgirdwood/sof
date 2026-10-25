# SPDX-License-Identifier: BSD-3-Clause
"""Entry point for the GTK4 Topology2 Visual Editor & Testbench Runner."""

from __future__ import annotations
import argparse
import sys
from typing import List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk

from .model import debug_log_action, enable_debug_logging, get_debug_log_path, is_debug_enabled
from .window import Topology2MainWindow


APP_CSS = """
window {
    background-color: #10131a;
    color: #e2e8f0;
}
.heading {
    font-weight: bold;
    color: #93c5fd;
}
.high-level-panel {
    border: 2px solid #475569;
    border-radius: 6px;
    background-color: #141821;
}
.high-level-panel > border {
    border: none;
}
.sub-panel-frame, frame {
    border: 1px solid #334155;
    border-radius: 4px;
}
frame > border {
    border: none;
}
paned > separator {
    background-color: #334155;
    min-width: 5px;
    min-height: 5px;
}
paned > separator:hover {
    background-color: #60a5fa;
}
treeview {
    background-color: #181c26;
    color: #e2e8f0;
}
treeview:selected {
    background-color: #2563eb;
    color: #ffffff;
}
textview {
    background-color: #0f1219;
    color: #cbd5e1;
    font-size: 11px;
}
.obj-item-title {
    font-weight: bold;
    font-size: 11.5px;
    color: #f8fafc;
}
.obj-item-badge {
    font-size: 10px;
    font-weight: bold;
    color: #60a5fa;
}
.obj-item-desc {
    font-size: 10px;
    color: #94a3b8;
}
popover.zoom-info-popover > contents {
    background-color: #111827;
    border: 2px solid #3b82f6;
    border-radius: 10px;
    padding: 14px;
}
.zoom-info-title {
    font-weight: bold;
    font-size: 16px;
    color: #f8fafc;
}
.zoom-info-badge {
    font-size: 13px;
    font-weight: bold;
    color: #60a5fa;
}
.zoom-info-desc {
    font-size: 13.5px;
    color: #e2e8f0;
}
.zoom-info-meta {
    font-size: 12.5px;
    color: #93c5fd;
}
.zoom-info-hint {
    font-size: 12px;
    color: #fbbf24;
    font-weight: bold;
}
"""


class Topology2GuiApp(Gtk.Application):
    def __init__(self, initial_dir: Optional[str] = None):
        super().__init__(
            application_id="org.thesofproject.Tplg2VisualEditor",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.initial_dir = initial_dir
        self.win: Optional[Topology2MainWindow] = None

    def do_activate(self) -> None:
        display = Gdk.Display.get_default()
        if display:
            provider = Gtk.CssProvider()
            provider.load_from_string(APP_CSS)
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        if not self.win:
            self.win = Topology2MainWindow(self, initial_dir=self.initial_dir)
            self.win.maximize()
            if is_debug_enabled():
                self.win.append_log(f"[DEBUG] Action & Command debug logging active: {get_debug_log_path()}")
        self.win.present()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tplg2-gui.py",
        description="SOF Topology2 GTK4 Visual Graph Editor & Testbench Studio",
    )
    parser.add_argument(
        "initial_dir",
        nargs="?",
        default=None,
        help="Optional path to tools/topology/topology2 directory",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging of all user actions and command outputs to a text file (default: tplg2-gui-debug.log)",
    )
    parser.add_argument(
        "--debug-file",
        default="tplg2-gui-debug.log",
        metavar="FILE",
        help="Path to the debug log file (implies --debug when explicitly specified)",
    )
    raw_args = list(argv if argv is not None else sys.argv[1:])
    parsed = parser.parse_args(raw_args)

    if parsed.debug or any(a.startswith("--debug-file") for a in raw_args):
        log_path = enable_debug_logging(parsed.debug_file)
        print(f"[tplg2-gui] Debug logging enabled -> {log_path}", flush=True)
        debug_log_action("APP", "Application startup", f"initial_dir={parsed.initial_dir or '(auto)'}")

    app = Topology2GuiApp(initial_dir=parsed.initial_dir)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
