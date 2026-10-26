# SPDX-License-Identifier: BSD-3-Clause
"""Colorized ALSA Topology2 (.conf) Text Editor with live syntax highlighting and Diagram sync."""

from __future__ import annotations
import re
from typing import Callable, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango


# Pre-compiled regex patterns for ALSA Topology2 (.conf) syntax colorization
RE_BRACE = re.compile(r"[\{\}\[\]]")
RE_NUMBER = re.compile(
    r"\b(?:0x[0-9A-Fa-f]+|[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){7,15}|-?\d+(?:\.\d+)?)\b"
)
RE_KEYWORD = re.compile(
    r"\b(?:Class|Object|DefineAttribute|Define|IncludeByKey|SubTreeCopy|CombineArrays|"
    r"attributes|constraints|tree|source|sink|index|direction|instance|type|uuid|value|"
    r"token_ref|name|rate|channels|formats|rates)\b"
)
RE_CATEGORY = re.compile(
    r"\b(?:Widget|Pipeline|Control|Dai|PCM|Base|VendorToken|route|mixer|bytes|enum|"
    r"input_audio_format|output_audio_format|hw_config|fe_dai|pcm_caps|tlv|scale|ops|"
    r"data|manifest|virtual)\b"
)
RE_DIRECTIVE = re.compile(
    r"(?:!(?:constructor|mandatory|immutable|deprecated|valid_values|tuple_values|route)\b|\bunique\b)"
)
RE_VARIABLE = re.compile(r"\$(?:\{[A-Za-z0-9_]+\}|[A-Za-z0-9_]+)")
RE_INCLUDE = re.compile(r"<[^>\n]+>")
RE_STRING = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
RE_COMMENT = re.compile(r"/\*[\s\S]*?\*/|#[^\n]*")
RE_DOXY = re.compile(r"##[^\n]*")


def check_alsaconf_syntax_balance(text: str) -> Tuple[bool, Optional[int], str]:
    """Quick structural syntax check for balanced braces/brackets and quotes.

    Returns:
        (is_valid, error_line_1based, message)
    """
    stack: list[Tuple[str, int]] = []
    pairs = {"{": "}", "[": "]"}
    closing = {"}": "{", "]": "["}

    in_squote = False
    in_dquote = False
    in_comment = False
    line_no = 1

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            line_no += 1
            in_comment = False
            in_squote = False
            in_dquote = False
            i += 1
            continue
        if in_comment:
            i += 1
            continue
        if not in_squote and not in_dquote:
            if ch == "#":
                in_comment = True
                i += 1
                continue
            if ch == "/" and i + 1 < n and text[i + 1] == "*":
                end_c = text.find("*/", i + 2)
                if end_c == -1:
                    return (False, line_no, "Unclosed block comment '/*'")
                line_no += text[i : end_c + 2].count("\n")
                i = end_c + 2
                continue

        if ch == "'" and not in_dquote:
            in_squote = not in_squote
            i += 1
            continue
        if ch == '"' and not in_squote:
            in_dquote = not in_dquote
            i += 1
            continue

        if not in_squote and not in_dquote:
            if ch in pairs:
                stack.append((ch, line_no))
            elif ch in closing:
                if not stack:
                    return (False, line_no, f"Unexpected closing '{ch}' on line {line_no}")
                top_ch, top_ln = stack.pop()
                if top_ch != closing[ch]:
                    return (
                        False,
                        line_no,
                        f"Mismatched '{ch}' on line {line_no} (opened '{top_ch}' on line {top_ln})",
                    )
        i += 1

    if stack:
        top_ch, top_ln = stack[-1]
        return (False, top_ln, f"Unclosed '{top_ch}' opened on line {top_ln}")
    return (True, None, "Valid Topology2 (.conf) syntax")


class TopologyTextEditor(Gtk.Box):
    """Colorized ALSA Topology2 (.conf) source editor widget with live Diagram synchronization."""

    def __init__(
        self,
        on_text_committed: Optional[Callable[[str], Tuple[bool, str]]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_vexpand(True)
        self.set_hexpand(True)

        self.on_text_committed = on_text_committed
        self._suppress_change_signal: bool = False
        self._highlight_timer_id: Optional[int] = None
        self._sync_timer_id: Optional[int] = None
        self.is_modified_in_text: bool = False
        self._last_synced_text: str = ""

        # 1. Top Editor Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        toolbar.set_margin_top(4)
        toolbar.set_margin_bottom(4)

        self.file_lbl = Gtk.Label(label="Topology2 (.conf) Source", xalign=0.0)
        self.file_lbl.add_css_class("heading")
        self.file_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.file_lbl.set_hexpand(True)
        toolbar.append(self.file_lbl)

        self.status_lbl = Gtk.Label(label="✔ Synced with Diagram", xalign=1.0)
        self.status_lbl.add_css_class("monospace")
        toolbar.append(self.status_lbl)

        self.cursor_lbl = Gtk.Label(label="Ln 1, Col 1", xalign=1.0)
        self.cursor_lbl.add_css_class("dim-label")
        self.cursor_lbl.add_css_class("monospace")
        toolbar.append(self.cursor_lbl)

        self.apply_btn = Gtk.Button(label="⟳ Apply to Diagram")
        self.apply_btn.set_tooltip_text(
            "Parse edited .conf text and update the visual graph diagram immediately (Shortcut: Ctrl+Enter)"
        )
        self.apply_btn.connect("clicked", lambda _b: self.commit_to_diagram())
        toolbar.append(self.apply_btn)

        self.append(toolbar)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # 2. Main Scrolled Editor Area (Line-number gutter + Monospace TextView)
        editor_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        editor_hbox.set_vexpand(True)
        editor_hbox.set_hexpand(True)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_vexpand(True)
        self.scroll.set_hexpand(True)
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self.text_view = Gtk.TextView()
        self.text_view.set_monospace(True)
        self.text_view.set_editable(True)
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text_view.set_left_margin(12)
        self.text_view.set_right_margin(12)
        self.text_view.set_top_margin(8)
        self.text_view.set_bottom_margin(12)

        self.buffer = self.text_view.get_buffer()
        self._init_syntax_tags()

        self.buffer.connect("changed", self._on_buffer_changed)
        self.buffer.connect("notify::cursor-position", self._on_cursor_moved)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.text_view.add_controller(key_ctrl)

        self.scroll.set_child(self.text_view)
        editor_hbox.append(self.scroll)
        self.append(editor_hbox)

    def _init_syntax_tags(self) -> None:
        table = self.buffer.get_tag_table()
        if table.lookup("syn_keyword") is not None:
            return

        self.buffer.create_tag("syn_brace", foreground="#ffd166", weight=Pango.Weight.BOLD)
        self.buffer.create_tag("syn_number", foreground="#b5cea8")
        self.buffer.create_tag("syn_keyword", foreground="#569cd6", weight=Pango.Weight.BOLD)
        self.buffer.create_tag("syn_category", foreground="#c586c0", weight=Pango.Weight.BOLD)
        self.buffer.create_tag("syn_directive", foreground="#ff79c6", weight=Pango.Weight.BOLD)
        self.buffer.create_tag("syn_variable", foreground="#7ec8ff", weight=Pango.Weight.BOLD)
        self.buffer.create_tag("syn_include", foreground="#ffb86c")
        self.buffer.create_tag("syn_string", foreground="#e6db74")
        self.buffer.create_tag("syn_comment", foreground="#6a9955", style=Pango.Style.ITALIC)
        self.buffer.create_tag("syn_doxy", foreground="#4ec9b0", style=Pango.Style.ITALIC)
        self.buffer.create_tag(
            "syn_error_line",
            background="#421c22",
            underline=Pango.Underline.ERROR,
        )

    def set_text(self, text: str, header_title: str = "") -> None:
        """Populate the text editor without triggering a feedback loop to the diagram."""
        self._cancel_timers()
        self._suppress_change_signal = True
        try:
            self.buffer.set_text(text)
            self._last_synced_text = text
            self.is_modified_in_text = False
            if header_title:
                self.file_lbl.set_text(header_title)
            self.apply_syntax_highlighting()
            self.status_lbl.set_text("✔ Synced with Diagram")
        finally:
            self._suppress_change_signal = False

    def get_text(self) -> str:
        start_it = self.buffer.get_start_iter()
        end_it = self.buffer.get_end_iter()
        return self.buffer.get_text(start_it, end_it, True)

    def scroll_to_hint(self, hint: Optional[str]) -> bool:
        """Move the cursor to the first occurrence of hint in the buffer and scroll it into view."""
        if not hint:
            return False
        text = self.get_text()
        if not text:
            return False
        idx = text.find(hint)
        if idx < 0:
            # Try unquoted fallback if hint had quotes
            unquoted = hint.replace('"', "").replace("'", "")
            if unquoted:
                idx = text.find(unquoted)
        if idx < 0:
            return False
        it = self.buffer.get_iter_at_offset(idx)
        self.buffer.place_cursor(it)
        self.text_view.scroll_to_iter(it, 0.15, True, 0.0, 0.25)
        return True

    def _cancel_timers(self) -> None:
        if self._highlight_timer_id is not None:
            GLib.source_remove(self._highlight_timer_id)
            self._highlight_timer_id = None
        if self._sync_timer_id is not None:
            GLib.source_remove(self._sync_timer_id)
            self._sync_timer_id = None

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl_held and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.commit_to_diagram()
            return True
        return False

    def _on_cursor_moved(self, *_args: object) -> None:
        mark = self.buffer.get_insert()
        it = self.buffer.get_iter_at_mark(mark)
        ln = it.get_line() + 1
        col = it.get_line_offset() + 1
        self.cursor_lbl.set_text(f"Ln {ln}, Col {col}")

    def _on_buffer_changed(self, _buf: Gtk.TextBuffer) -> None:
        if self._suppress_change_signal:
            return
        self.is_modified_in_text = True
        self.status_lbl.set_text("✎ Editing text…")

        if self._highlight_timer_id is not None:
            GLib.source_remove(self._highlight_timer_id)
        self._highlight_timer_id = GLib.timeout_add(90, self._on_debounced_highlight)

        if self._sync_timer_id is not None:
            GLib.source_remove(self._sync_timer_id)
        self._sync_timer_id = GLib.timeout_add(480, self._on_debounced_sync)

    def _on_debounced_highlight(self) -> bool:
        self._highlight_timer_id = None
        self.apply_syntax_highlighting()
        return False

    def _on_debounced_sync(self) -> bool:
        self._sync_timer_id = None
        self.commit_to_diagram()
        return False

    def commit_to_diagram(self) -> bool:
        """Validate syntax and invoke on_text_committed to update the visual graph diagram."""
        if self._sync_timer_id is not None:
            GLib.source_remove(self._sync_timer_id)
            self._sync_timer_id = None

        text = self.get_text()
        self.apply_syntax_highlighting()

        ok_syn, err_line, syn_msg = check_alsaconf_syntax_balance(text)
        if not ok_syn:
            if err_line is not None:
                self._highlight_error_line(err_line)
            self.status_lbl.set_text(f"⚠ {syn_msg}")
            return False

        if text == self._last_synced_text:
            self.is_modified_in_text = False
            self.status_lbl.set_text("✔ Synced with Diagram")
            return True

        if self.on_text_committed is not None:
            ok, msg = self.on_text_committed(text)
            if ok:
                self._last_synced_text = text
                self.is_modified_in_text = False
                self.status_lbl.set_text(f"✔ {msg}")
                return True
            else:
                self.status_lbl.set_text(f"⚠ {msg}")
                return False
        return True

    def _highlight_error_line(self, line_1based: int) -> None:
        ok, s_it = self.buffer.get_iter_at_line(max(0, line_1based - 1))
        if not ok:
            return
        e_it = s_it.copy()
        if not e_it.ends_line():
            e_it.forward_to_line_end()
        self.buffer.apply_tag_by_name("syn_error_line", s_it, e_it)

    def apply_syntax_highlighting(self) -> None:
        """Apply ALSA Topology2 (.conf) syntax colorization tags across the buffer."""
        start_it = self.buffer.get_start_iter()
        end_it = self.buffer.get_end_iter()
        self.buffer.remove_all_tags(start_it, end_it)
        text = self.buffer.get_text(start_it, end_it, True)
        if not text:
            return

        # Convert byte/char offsets using Gtk.TextBuffer iter_at_offset (char-based in Python)
        def _apply_pattern(regex: re.Pattern, tag_name: str, clear_under: bool = False) -> None:
            for m in regex.finditer(text):
                s_idx, e_idx = m.start(), m.end()
                s_it = self.buffer.get_iter_at_offset(s_idx)
                e_it = self.buffer.get_iter_at_offset(e_idx)
                if clear_under:
                    self.buffer.remove_all_tags(s_it, e_it)
                self.buffer.apply_tag_by_name(tag_name, s_it, e_it)

        _apply_pattern(RE_BRACE, "syn_brace")
        _apply_pattern(RE_NUMBER, "syn_number")
        _apply_pattern(RE_KEYWORD, "syn_keyword")
        _apply_pattern(RE_CATEGORY, "syn_category")
        _apply_pattern(RE_DIRECTIVE, "syn_directive")
        _apply_pattern(RE_VARIABLE, "syn_variable")
        _apply_pattern(RE_INCLUDE, "syn_include", clear_under=True)
        _apply_pattern(RE_STRING, "syn_string", clear_under=True)
        # Re-apply variable tags inside strings like '$index' or "$PLATFORM"
        _apply_pattern(RE_VARIABLE, "syn_variable", clear_under=False)
        _apply_pattern(RE_COMMENT, "syn_comment", clear_under=True)
        _apply_pattern(RE_DOXY, "syn_doxy", clear_under=True)
