# SPDX-License-Identifier: BSD-3-Clause
"""Entry point for the GTK4 Topology2 Visual Editor & Testbench Runner."""

from __future__ import annotations
import sys
from typing import List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk

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
        self.win.present()


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    initial_dir = args[0] if args else None
    app = Topology2GuiApp(initial_dir=initial_dir)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
