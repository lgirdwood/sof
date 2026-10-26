# SPDX-License-Identifier: BSD-3-Clause
"""Recursive parser and graph builder for ALSA Topology2 (.conf) and CMake targets."""

from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .model import (
    AttrConstraint,
    AttrDef,
    CMakeTargetVariant,
    ConfigNode,
    GraphEdge,
    GraphNode,
    GraphPort,
    GraphViewModel,
    MachineTopologyEntry,
    PipelineContainer,
    TplgClassDef,
)


TOKEN_RE = re.compile(
    r"""
    (?P<doxy>\#\#[^\n]*)|
    (?P<comment>\#[^\n]*)|
    (?P<ccomment>/\*[\s\S]*?\*/)|
    (?P<include><[^>\n]+>)|
    (?P<squote>'[^']*')|
    (?P<dquote>"[^"]*")|
    (?P<lbrace>\{)|
    (?P<rbrace>\})|
    (?P<lbrack>\[)|
    (?P<rbrack>\])|
    (?P<word>[^\s\{\}\[\]<>"'#]+)|
    (?P<ws>\s+)
    """,
    re.VERBOSE,
)


def tokenize_alsaconf(text: str) -> List[Tuple[str, str]]:
    """Tokenize alsaconf / topology2 text into (kind, value) pairs."""
    tokens: List[Tuple[str, str]] = []
    for m in TOKEN_RE.finditer(text):
        kind = m.lastgroup
        val = m.group()
        if kind in ("ws", "comment", "ccomment"):
            continue
        tokens.append((kind, val))
    return tokens


def strip_quotes(val: str) -> str:
    if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
        return val[1:-1]
    return val


def split_dotted_key(raw: str) -> List[str]:
    """Split a dotted key like Object.Widget."gain"."1" into ['Object', 'Widget', 'gain', '1']."""
    parts: List[str] = []
    cur: List[str] = []
    in_q: Optional[str] = None
    for ch in raw:
        if in_q:
            if ch == in_q:
                in_q = None
            else:
                cur.append(ch)
        else:
            if ch in ('"', "'"):
                in_q = ch
            elif ch == ".":
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
    if cur or raw.endswith("."):
        parts.append("".join(cur))
    return [p for p in parts if p != "" or len(parts) > 2]


class AstStmt:
    """AST node representing a statement in a Topology2 .conf file."""
    def __init__(
        self,
        stmt_type: str,
        key: str = "",
        value: Any = None,
        children: Optional[List["AstStmt"]] = None,
        doxy: str = "",
        source_file: str = "",
    ):
        self.stmt_type = stmt_type  # "include", "searchdir", "block", "array", "kv"
        self.key = key
        self.value = value
        self.children: List["AstStmt"] = children if children is not None else []
        self.doxy = doxy
        self.source_file = source_file
        self.provenance: str = ""


class TokenStream:
    def __init__(self, tokens: List[Tuple[str, str]], source_file: str = ""):
        self.tokens = tokens
        self.pos = 0
        self.source_file = source_file

    def peek(self, offset: int = 0) -> Optional[Tuple[str, str]]:
        idx = self.pos + offset
        if 0 <= idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def next(self) -> Optional[Tuple[str, str]]:
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def eof(self) -> bool:
        return self.pos >= len(self.tokens)


def _read_compound_key(stream: TokenStream) -> str:
    """Read a key that may have adjacent quoted segments like Class.Widget.\"gain\"."""
    tok = stream.next()
    if tok is None:
        return ""
    key = tok[1]
    if key == "!" and not stream.eof():
        nxt0 = stream.peek()
        if nxt0 is not None and nxt0[0] in ("word", "dquote", "squote"):
            key = "!" + stream.next()[1]
    # In alsaconf, Class.Widget."gain" or mixer."1" can be tokenized as word('Class.Widget.') + dquote('"gain"')
    while not stream.eof():
        nxt = stream.peek()
        if nxt is None:
            break
        if key.endswith(".") and nxt[0] in ("dquote", "squote", "word"):
            key += stream.next()[1]
        elif nxt[0] == "word" and nxt[1].startswith("."):
            key += stream.next()[1]
        else:
            break
    return key


def parse_stmt_list(
    stream: TokenStream,
    stop_tokens: Set[str] = frozenset(),
    in_array: bool = False,
) -> List[AstStmt]:
    stmts: List[AstStmt] = []
    pending_doxy: List[str] = []

    while not stream.eof():
        pk = stream.peek()
        if pk is None:
            break
        kind, val = pk
        if kind in stop_tokens:
            break

        if kind == "doxy":
            stream.next()
            clean = val.lstrip("#").strip()
            if clean:
                pending_doxy.append(clean)
            continue

        if kind == "include":
            stream.next()
            inner = val[1:-1].strip()
            if inner.startswith("searchdir:"):
                stmts.append(
                    AstStmt(
                        "searchdir",
                        key=inner.split(":", 1)[1].strip(),
                        source_file=stream.source_file,
                    )
                )
            else:
                stmts.append(
                    AstStmt(
                        "include",
                        key=inner,
                        source_file=stream.source_file,
                    )
                )
            pending_doxy.clear()
            continue

        if kind == "lbrace":
            # Anonymous block `{ ... }` inside an array
            stream.next()
            children = parse_stmt_list(stream, {"rbrace"}, in_array=False)
            if stream.peek() and stream.peek()[0] == "rbrace":
                stream.next()
            stmts.append(
                AstStmt(
                    "block",
                    key="{}",
                    children=children,
                    doxy="\n".join(pending_doxy),
                    source_file=stream.source_file,
                )
            )
            pending_doxy.clear()
            continue

        if kind in ("rbrace", "rbrack"):
            # Unmatched closing delimiter; consume to prevent infinite loop
            stream.next()
            break

        key = _read_compound_key(stream)
        doxy_str = "\n".join(pending_doxy)
        pending_doxy.clear()

        nxt = stream.peek()
        if nxt is not None and nxt[0] == "lbrace":
            stream.next()
            children = parse_stmt_list(stream, {"rbrace"}, in_array=False)
            if stream.peek() and stream.peek()[0] == "rbrace":
                stream.next()
            stmts.append(
                AstStmt(
                    "block",
                    key=key,
                    children=children,
                    doxy=doxy_str,
                    source_file=stream.source_file,
                )
            )
        elif nxt is not None and nxt[0] == "lbrack":
            stream.next()
            children = parse_stmt_list(stream, {"rbrack"}, in_array=True)
            if stream.peek() and stream.peek()[0] == "rbrack":
                stream.next()
            stmts.append(
                AstStmt(
                    "array",
                    key=key,
                    children=children,
                    doxy=doxy_str,
                    source_file=stream.source_file,
                )
            )
        elif in_array:
            # Standalone scalar element inside `[ ... ]`
            stmts.append(
                AstStmt(
                    "kv",
                    key=strip_quotes(key),
                    value=strip_quotes(key),
                    doxy=doxy_str,
                    source_file=stream.source_file,
                )
            )
        else:
            # Scalar key-value outside array
            if nxt is not None and nxt[0] in ("word", "squote", "dquote"):
                val_tok = stream.next()
                stmts.append(
                    AstStmt(
                        "kv",
                        key=strip_quotes(key),
                        value=strip_quotes(val_tok[1]),
                        doxy=doxy_str,
                        source_file=stream.source_file,
                    )
                )
            else:
                stmts.append(
                    AstStmt(
                        "kv",
                        key=strip_quotes(key),
                        value="",
                        doxy=doxy_str,
                        source_file=stream.source_file,
                    )
                )

    return stmts


def parse_conf_text(text: str, source_file: str = "") -> List[AstStmt]:
    tokens = tokenize_alsaconf(text)
    stream = TokenStream(tokens, source_file=source_file)
    return parse_stmt_list(stream)


def parse_conf_file(path: str) -> List[AstStmt]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_conf_text(text, source_file=path)


class Topology2Workspace:
    """Loads and indexes an entire tools/topology/topology2 directory."""

    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        self._ast_cache: Dict[str, List[AstStmt]] = {}

        # Categorized collections for the 4 left-hand sub-panes
        self.base_classes: Dict[str, TplgClassDef] = {}       # Pane 1: Base, Control, PCM, Dai
        self.processing_modules: Dict[str, TplgClassDef] = {} # Pane 2: Class.Widget.*
        self.pipeline_classes: Dict[str, TplgClassDef] = {}   # Pane 3: Class.Pipeline.*
        self.machine_topologies: List[MachineTopologyEntry] = []  # Pane 4: top-level .conf + CMake variants

        # Global lookup of all classes by "Category.Name"
        self.all_classes: Dict[str, TplgClassDef] = {}
        self.global_defines: Dict[str, str] = {}
        self.platform_defaults: Dict[str, str] = {}
        self.platform_definitions: List[Dict[str, object]] = []

    def get_ast(self, abs_path: str) -> List[AstStmt]:
        abs_path = os.path.abspath(abs_path)
        if abs_path not in self._ast_cache:
            if os.path.isfile(abs_path):
                self._ast_cache[abs_path] = parse_conf_file(abs_path)
            else:
                self._ast_cache[abs_path] = []
        return self._ast_cache[abs_path]

    def resolve_include(self, include_str: str, search_dirs: List[str], current_file: str = "") -> Optional[str]:
        candidates = []
        if current_file:
            candidates.append(os.path.join(os.path.dirname(current_file), include_str))
        candidates.append(os.path.join(self.root_dir, include_str))
        for sdir in search_dirs:
            candidates.append(os.path.join(self.root_dir, sdir, include_str))
        for cand in candidates:
            if os.path.isfile(cand):
                return os.path.abspath(cand)
        return None

    def scan_all(self) -> None:
        """Scan the entire topology2 directory and populate all 4 left-pane collections."""
        self.base_classes.clear()
        self.processing_modules.clear()
        self.pipeline_classes.clear()
        self.machine_topologies.clear()
        self.all_classes.clear()
        self.global_defines.clear()
        self.platform_defaults.clear()
        self.platform_definitions.clear()

        # 1. Load common definitions first (so $HDA_HOST_OUTPUT_CLASS etc. are known)
        self.global_defines["DEEP_BUF_JACK_RATE"] = "48000"
        common_defs = os.path.join(self.root_dir, "include", "common", "common_definitions.conf")
        if os.path.isfile(common_defs):
            for stmt in self.get_ast(common_defs):
                if stmt.stmt_type == "block" and stmt.key == "Define":
                    for ch in stmt.children:
                        if ch.stmt_type == "kv":
                            self.global_defines[ch.key] = str(ch.value)

        # 2. Scan include/ directory for all Class.<Category>.<Name> definitions
        include_dir = os.path.join(self.root_dir, "include")
        if os.path.isdir(include_dir):
            for dirpath, _dirnames, filenames in os.walk(include_dir):
                for fname in sorted(filenames):
                    if not fname.endswith(".conf"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    self._extract_classes_from_file(fpath)

        # Resolve SubTreeCopy.baseclass inheritance for pipeline classes
        self._resolve_pipeline_inheritance()

        # 3. Parse CMake target variants in production/ and development/
        variants_by_conf = self._parse_cmake_targets()

        # 4. Scan top-level .conf files and development/*.conf for Machine Topologies
        top_confs: List[Tuple[str, str]] = []
        for fname in sorted(os.listdir(self.root_dir)):
            if fname.endswith(".conf") and fname != "dmic-wov-feature.conf":
                top_confs.append((fname[:-5], os.path.join(self.root_dir, fname)))
        dev_dir = os.path.join(self.root_dir, "development")
        if os.path.isdir(dev_dir):
            for fname in sorted(os.listdir(dev_dir)):
                if fname.endswith(".conf"):
                    rel_name = f"development/{fname[:-5]}"
                    top_confs.append((rel_name, os.path.join(dev_dir, fname)))

        for base_name, fpath in top_confs:
            rel_path = os.path.relpath(fpath, self.root_dir)
            variants = variants_by_conf.get(base_name, []) + variants_by_conf.get(os.path.basename(base_name), [])
            # Deduplicate variants by output_name
            seen_out: Set[str] = set()
            unique_vars: List[CMakeTargetVariant] = []
            for v in variants:
                if v.output_name not in seen_out:
                    seen_out.add(v.output_name)
                    unique_vars.append(v)
            self.machine_topologies.append(
                MachineTopologyEntry(
                    name=os.path.basename(base_name),
                    conf_path=fpath,
                    rel_path=rel_path,
                    variants=unique_vars,
                )
            )

        # 5. Scan platform/ directory for Platform Topology Definitions & Defaults
        plat_dir = os.path.join(self.root_dir, "platform")
        if os.path.isdir(plat_dir):
            for dirpath, _dirnames, filenames in os.walk(plat_dir):
                for fname in sorted(filenames):
                    if not fname.endswith(".conf"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    rel_p = os.path.relpath(fpath, self.root_dir)
                    self._extract_classes_from_file(fpath)
                    stmts = self.get_ast(fpath)
                    defs_map: Dict[str, str] = {}
                    has_objs = False
                    for st in stmts:
                        if st.stmt_type == "block" and st.key == "Define":
                            for ch in st.children:
                                if ch.stmt_type == "kv":
                                    defs_map[ch.key] = str(ch.value)
                        elif st.key.startswith("Object."):
                            has_objs = True
                    if fname.endswith("-default.conf"):
                        self.platform_defaults.update(defs_map)
                    stem = fname[:-5]
                    if stem in ("tgl", "mtl", "lnl", "ptl"):
                        pcat = "soc"
                    elif stem.startswith("dmic"):
                        pcat = "dmic"
                    elif stem.startswith("hdmi"):
                        pcat = "hdmi"
                    elif stem.startswith("sdw"):
                        pcat = "sdw"
                    elif stem.startswith(("bt", "ssp", "nocodec", "uaol")):
                        pcat = "bt_ssp"
                    else:
                        pcat = "feature"
                    self.platform_definitions.append(
                        {
                            "name": fname,
                            "stem": stem,
                            "rel_path": rel_p,
                            "abs_path": fpath,
                            "category": pcat,
                            "defines": defs_map,
                            "has_objects": has_objs,
                        }
                    )
            self.platform_definitions.sort(key=lambda d: str(d["rel_path"]))

        # 6. Aggregate per-module and per-pin input/output audio formats across pipelines, bench configs, and topologies
        self._aggregate_module_pin_audio_formats()

    def _aggregate_module_pin_audio_formats(self) -> None:
        """Populate default pin counts and aggregate input_audio_format / output_audio_format specs per module."""
        if not self.processing_modules:
            return

        # 1. Normalize default num_input_pins and num_output_pins where class definitions omit them
        default_pin_overrides: Dict[str, Tuple[int, int]] = {
            "siggen": (0, 1),
            "wov": (1, 0),
            "crossover": (1, 2),
            "muxdemux": (1, 2),
            "pipeline": (0, 0),
        }
        for mname, cdef in self.processing_modules.items():
            ov_in, ov_out = default_pin_overrides.get(mname, (1, 1))
            if cdef.defaults.get("num_input_pins") is None:
                cdef.defaults["num_input_pins"] = ov_in
            if cdef.defaults.get("num_output_pins") is None:
                cdef.defaults["num_output_pins"] = ov_out

        in_by_mod: Dict[str, List[ConfigNode]] = {m: [] for m in self.processing_modules}
        out_by_mod: Dict[str, List[ConfigNode]] = {m: [] for m in self.processing_modules}

        search_dirs = [
            "include",
            "include/common",
            "include/components",
            "include/controls",
            "include/pipelines",
            "include/pipelines/cavs",
            "include/bench",
            "platform",
            "platform/intel",
        ]

        def add_fmt_cfg(target_list: List[ConfigNode], cfg_n: ConfigNode) -> None:
            if any(
                ch.node_type == "scalar"
                or ch.key.startswith(("in_", "out_", "input_pin", "output_pin", "ibs", "obs"))
                for ch in cfg_n.children
            ):
                target_list.append(cfg_n.clone())
            else:
                for ch in cfg_n.children:
                    target_list.append(ch.clone())

        # Seed from any format objects already embedded inside Class.Widget.<mname>
        for mname, cdef in self.processing_modules.items():
            for sub in cdef.sub_objects:
                if "input_audio_format" in sub.key:
                    add_fmt_cfg(in_by_mod[mname], sub)
                elif "output_audio_format" in sub.key:
                    add_fmt_cfg(out_by_mod[mname], sub)

        def extract_from_widget_stmts(mname: str, stmts: List[AstStmt], cur_file: str) -> None:
            if mname not in self.processing_modules:
                return
            for st in stmts:
                if st.stmt_type == "block" and st.key == "{}":
                    extract_from_widget_stmts(mname, st.children, cur_file)
                elif st.stmt_type == "include":
                    inc_p = self.resolve_include(st.key, search_dirs, cur_file)
                    if inc_p:
                        extract_from_widget_stmts(mname, self.get_ast(inc_p), inc_p)
                elif st.stmt_type == "block" and st.key.startswith("IncludeByKey."):
                    for ch in st.children:
                        if ch.stmt_type == "kv" and ch.value:
                            for bfmt in ("s16", "s24", "s32"):
                                val = str(ch.value).replace("$BENCH_MODULE_FORMAT", bfmt)
                                inc_p = self.resolve_include(val, search_dirs, cur_file)
                                if inc_p:
                                    extract_from_widget_stmts(mname, self.get_ast(inc_p), inc_p)
                                if "$BENCH_MODULE_FORMAT" not in str(ch.value):
                                    break
                        elif ch.stmt_type == "block":
                            extract_from_widget_stmts(mname, ch.children, cur_file)
                elif st.stmt_type in ("block", "array"):
                    if "input_audio_format" in st.key:
                        add_fmt_cfg(in_by_mod[mname], self._ast_to_config_node(st, origin="default"))
                    elif "output_audio_format" in st.key:
                        add_fmt_cfg(out_by_mod[mname], self._ast_to_config_node(st, origin="default"))

        def walk_file_stmts(stmts: List[AstStmt], cur_file: str) -> None:
            for st in stmts:
                if st.stmt_type in ("block", "array"):
                    parts = split_dotted_key(st.key)
                    if len(parts) >= 2 and parts[0] == "Object" and parts[1] == "Widget":
                        if len(parts) == 2:
                            for wch in st.children:
                                wp = split_dotted_key(wch.key)
                                if wp:
                                    extract_from_widget_stmts(wp[0], wch.children, cur_file)
                        else:
                            extract_from_widget_stmts(parts[2], st.children, cur_file)
                    else:
                        walk_file_stmts(st.children, cur_file)

        # Scan all pipeline widget definitions
        for pdef in self.pipeline_classes.values():
            for w in pdef.widgets:
                extract_from_widget_stmts(str(w["class_name"]), w["stmts"], pdef.source_file)

        # Scan bench, development, and production .conf files
        for sub_dir_name in ("include/bench", "development", "production"):
            sdir = os.path.join(self.root_dir, sub_dir_name)
            if not os.path.isdir(sdir):
                continue
            for dirpath, _dirnames, filenames in os.walk(sdir):
                for fname in sorted(filenames):
                    if fname.endswith(".conf"):
                        fpath = os.path.join(dirpath, fname)
                        walk_file_stmts(self.get_ast(fpath), fpath)

        def dedup_entries(entries: List[ConfigNode], prefix: str) -> List[ConfigNode]:
            pin_key = "input_pin_index" if prefix == "in" else "output_pin_index"
            seen: Set[Tuple[str, ...]] = set()
            unique: List[ConfigNode] = []
            for e in entries:
                p_idx = str(e.get_scalar(pin_key, "0")).strip()
                r_ch = e.get_child(f"{prefix}_rate")
                if r_ch and r_ch.children:
                    r_sig = ",".join(str(x.key or x.value) for x in r_ch.children)
                else:
                    r_sig = str(e.get_scalar(f"{prefix}_rate", "48000"))
                c_ch = e.get_child(f"{prefix}_channels")
                if c_ch and c_ch.children:
                    c_sig = ",".join(str(x.key or x.value) for x in c_ch.children)
                else:
                    c_sig = str(e.get_scalar(f"{prefix}_channels", "2"))
                b_sig = str(e.get_scalar(f"{prefix}_bit_depth", "16"))
                vb_sig = str(e.get_scalar(f"{prefix}_valid_bit_depth", b_sig))
                st_sig = str(e.get_scalar(f"{prefix}_sample_type", "LSB_INTEGER"))
                il_sig = str(e.get_scalar(f"{prefix}_interleaving_style", "interleaved"))
                sig = (p_idx, r_sig, c_sig, b_sig, vb_sig, st_sig, il_sig)
                if sig not in seen:
                    seen.add(sig)
                    clone_e = e.clone()
                    clone_e.key = f"[{len(unique)}]"
                    if clone_e.get_scalar(pin_key) is None:
                        clone_e.set_scalar(pin_key, "0", origin="default")
                    if clone_e.get_scalar(f"{prefix}_rate") is None and clone_e.get_child(f"{prefix}_rate") is None:
                        clone_e.set_scalar(f"{prefix}_rate", "48000", origin="default")
                    if clone_e.get_scalar(f"{prefix}_channels") is None and clone_e.get_child(f"{prefix}_channels") is None:
                        clone_e.set_scalar(f"{prefix}_channels", "2", origin="default")
                    if clone_e.get_scalar(f"{prefix}_bit_depth") is None:
                        clone_e.set_scalar(f"{prefix}_bit_depth", "16", origin="default")
                    if clone_e.get_scalar(f"{prefix}_valid_bit_depth") is None:
                        clone_e.set_scalar(
                            f"{prefix}_valid_bit_depth",
                            str(clone_e.get_scalar(f"{prefix}_bit_depth", "16")),
                            origin="default",
                        )
                    if clone_e.get_scalar(f"{prefix}_interleaving_style") is None:
                        clone_e.set_scalar(f"{prefix}_interleaving_style", "interleaved", origin="default")
                    unique.append(clone_e)
            return unique

        def make_default_entries(mname: str, prefix: str, num_pins: int) -> List[ConfigNode]:
            pin_key = "input_pin_index" if prefix == "in" else "output_pin_index"
            depths = [(24, 32)] if mname == "muxdemux" else [(16, 16), (24, 32), (32, 32)]
            res: List[ConfigNode] = []
            for p_idx in range(max(1, min(num_pins, 2))):
                for vb, b in depths:
                    entry = ConfigNode(key=f"[{len(res)}]", node_type="object", category_info="default", origin="default")
                    entry.set_scalar(pin_key, str(p_idx), origin="default")
                    entry.set_scalar(f"{prefix}_rate", "48000", origin="default")
                    entry.set_scalar(f"{prefix}_channels", "2", origin="default")
                    entry.set_scalar(f"{prefix}_bit_depth", str(b), origin="default")
                    entry.set_scalar(f"{prefix}_valid_bit_depth", str(vb), origin="default")
                    entry.set_scalar(f"{prefix}_interleaving_style", "interleaved", origin="default")
                    entry.set_scalar(f"{prefix}_sample_type", "LSB_INTEGER", origin="default")
                    res.append(entry)
            return res

        for mname, cdef in self.processing_modules.items():
            num_in = int(str(cdef.defaults.get("num_input_pins", 1) or 0))
            num_out = int(str(cdef.defaults.get("num_output_pins", 1) or 0))

            in_unique = dedup_entries(in_by_mod[mname], "in") if num_in > 0 else []
            out_unique = dedup_entries(out_by_mod[mname], "out") if num_out > 0 else []

            if num_in > 0 and not in_unique:
                in_unique = make_default_entries(mname, "in", num_in)
            if num_out > 0 and not out_unique:
                out_unique = make_default_entries(mname, "out", num_out)

            # Remove any existing input/output_audio_format sub-objects and replace with aggregated format arrays
            cdef.sub_objects = [
                s for s in cdef.sub_objects
                if "input_audio_format" not in s.key and "output_audio_format" not in s.key
            ]
            if in_unique:
                in_arr = ConfigNode(
                    key="Object.Base.input_audio_format",
                    node_type="array",
                    category_info=f"{len(in_unique)} formats",
                    origin="default",
                    children=in_unique,
                )
                cdef.sub_objects.append(in_arr)
                if cdef.defaults.get("num_input_audio_formats") is None:
                    cdef.defaults["num_input_audio_formats"] = len(in_unique)
            if out_unique:
                out_arr = ConfigNode(
                    key="Object.Base.output_audio_format",
                    node_type="array",
                    category_info=f"{len(out_unique)} formats",
                    origin="default",
                    children=out_unique,
                )
                cdef.sub_objects.append(out_arr)
                if cdef.defaults.get("num_output_audio_formats") is None:
                    cdef.defaults["num_output_audio_formats"] = len(out_unique)

            self._rebuild_class_config_tree(cdef)

    def _extract_classes_from_file(self, fpath: str) -> None:
        stmts = self.get_ast(fpath)
        rel_path = os.path.relpath(fpath, self.root_dir)
        for stmt in stmts:
            if stmt.stmt_type == "block" and stmt.key.startswith("Class."):
                parts = split_dotted_key(stmt.key)
                if len(parts) >= 3:
                    cat = parts[1]
                    cname = parts[2]
                    cdef = self._build_class_def(cat, cname, stmt, fpath, rel_path)
                    self.all_classes[f"{cat}.{cname}"] = cdef
                    if cat == "Widget":
                        self.processing_modules[cname] = cdef
                    elif cat == "Pipeline":
                        self.pipeline_classes[cname] = cdef
                    else:
                        self.base_classes[f"{cat}.{cname}"] = cdef

    @staticmethod
    def _extract_file_description(fpath: str, doxy: str) -> str:
        lines: List[str] = []
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    s = raw.strip()
                    if not s:
                        continue
                    if not s.startswith("#"):
                        break
                    c = s.lstrip("#").strip()
                    c = re.sub(r"^\\struct\s+\S+\s*", "", c).strip()
                    c = re.sub(r"^\\brief\s*", "", c).strip()
                    if not c or c.startswith("SPDX"):
                        continue
                    if any(
                        c.startswith(p)
                        for p in ("All attributes", "by alsatplg", "Usage:", "Object.", "Where N", '"Object.')
                    ):
                        break
                    if "All attributes defined herein" in c:
                        c = c.split("All attributes defined herein")[0].strip()
                        if c:
                            lines.append(c)
                        break
                    lines.append(c)
        except Exception:
            pass
        if not lines and doxy:
            for raw in doxy.splitlines():
                c = re.sub(r"^\\(struct|brief)\s*\S*\s*", "", raw.strip()).strip()
                if c:
                    lines.append(c)
        if len(lines) >= 2 and lines[0].lower().rstrip(".") in lines[1].lower():
            lines = lines[1:]
        return " — ".join(lines[:2])

    def _build_class_def(
        self,
        category: str,
        name: str,
        class_stmt: AstStmt,
        fpath: str,
        rel_path: str,
    ) -> TplgClassDef:
        desc = self._extract_file_description(fpath, class_stmt.doxy)
        cdef = TplgClassDef(
            category=category,
            name=name,
            source_file=fpath,
            rel_path=rel_path,
            description=desc,
        )
        # Flatten inline includes inside the class block (e.g. <include/components/widget-common.conf>)
        expanded_stmts = self._expand_class_body(class_stmt.children, fpath)
        self._populate_class_from_stmts(cdef, expanded_stmts)
        if not cdef.description:
            if category == "Widget":
                wtype = str(cdef.defaults.get("type", "effect"))
                cdef.description = f"SOF {name} processing module ({wtype})."
            elif category == "Pipeline":
                direction = str(cdef.defaults.get("direction", "capture" if "capture" in name else "playback"))
                cdef.description = f"{direction.capitalize()} pipeline: {name}."
        return cdef

    def _expand_class_body(self, stmts: List[AstStmt], current_file: str) -> List[AstStmt]:
        result: List[AstStmt] = []
        search_dirs = [
            "include",
            "include/common",
            "include/components",
            "include/dais",
            "include/pipelines",
            "include/pipelines/cavs",
        ]
        for st in stmts:
            if st.stmt_type == "include":
                inc_path = self.resolve_include(st.key, search_dirs, current_file)
                if inc_path:
                    result.extend(self._expand_class_body(self.get_ast(inc_path), inc_path))
            else:
                if st.children:
                    st = AstStmt(
                        stmt_type=st.stmt_type,
                        key=st.key,
                        value=st.value,
                        children=self._expand_class_body(st.children, current_file),
                        doxy=st.doxy,
                        source_file=st.source_file,
                    )
                result.append(st)
        return result

    def _populate_class_from_stmts(self, cdef: TplgClassDef, stmts: List[AstStmt]) -> None:
        for st in stmts:
            if st.stmt_type == "block" and st.key.startswith("DefineAttribute"):
                parts = split_dotted_key(st.key)
                attr_name = parts[1] if len(parts) > 1 else st.key
                adef = cdef.attributes.get(attr_name, AttrDef(name=attr_name))
                if st.doxy:
                    adef.description = st.doxy
                for ch in st.children:
                    if ch.stmt_type == "kv":
                        if ch.key == "type":
                            adef.attr_type = str(ch.value)
                        elif ch.key == "token_ref":
                            adef.token_ref = str(ch.value)
                            if "." in adef.token_ref and adef.attr_type == "string":
                                ref_t = adef.token_ref.split(".")[-1]
                                if ref_t in ("word", "short", "byte"):
                                    adef.attr_type = "int"
                                elif ref_t == "bool":
                                    adef.attr_type = "bool"
                        elif ch.key == "value":
                            cdef.defaults[attr_name] = ch.value
                    elif ch.stmt_type == "block" and ch.key == "constraints":
                        constraint = AttrConstraint()
                        for cch in ch.children:
                            if cch.stmt_type == "array" and cch.key.lstrip("!") == "valid_values":
                                constraint.valid_values = [str(x.key) for x in cch.children if x.key]
                            elif cch.stmt_type == "array" and cch.key.lstrip("!") == "tuple_values":
                                constraint.tuple_values = [str(x.key) for x in cch.children if x.key]
                        adef.constraints = constraint
                cdef.attributes[attr_name] = adef

            elif st.stmt_type == "block" and st.key == "attributes":
                for ch in st.children:
                    flag_name = ch.key.lstrip("!")
                    if ch.stmt_type == "array":
                        items = [str(x.key) for x in ch.children if x.key]
                        if flag_name == "constructor":
                            cdef.constructor_attrs = items
                        for item in items:
                            adef = cdef.attributes.get(item, AttrDef(name=item))
                            if flag_name == "constructor":
                                adef.is_constructor = True
                            elif flag_name == "mandatory":
                                adef.is_mandatory = True
                            elif flag_name == "immutable":
                                adef.is_immutable = True
                            elif flag_name == "deprecated":
                                adef.is_deprecated = True
                            cdef.attributes[item] = adef
                    elif ch.stmt_type == "kv" and flag_name == "unique":
                        u_attr = str(ch.value)
                        cdef.unique_attr = u_attr
                        adef = cdef.attributes.get(u_attr, AttrDef(name=u_attr))
                        adef.is_unique = True
                        cdef.attributes[u_attr] = adef

            elif st.stmt_type == "block" and st.key.startswith("SubTreeCopy"):
                for ch in st.children:
                    if ch.stmt_type == "kv" and ch.key == "source":
                        cdef.extends_class = str(ch.value)
                    elif ch.stmt_type == "block" and ch.key == "tree":
                        self._populate_class_from_stmts(cdef, ch.children)

            elif st.stmt_type in ("block", "array") and (
                st.key.startswith("Object.")
                or st.key.startswith("CombineArrays.Object.")
                or st.key in ("!route", "route")
            ):
                self._extract_class_embedded_objects(cdef, st)

            elif st.stmt_type == "kv":
                cdef.defaults[st.key] = st.value

        # Sync defaults & attributes into cdef.config_tree
        self._rebuild_class_config_tree(cdef)

    def _extract_class_embedded_objects(self, cdef: TplgClassDef, st: AstStmt) -> None:
        parts = split_dotted_key(st.key)
        if parts[0].lstrip("!") == "route":
            if st.stmt_type == "array":
                for item in st.children:
                    if item.stmt_type == "block":
                        rdict = {ch.key: str(ch.value) for ch in item.children if ch.stmt_type == "kv"}
                        if "source" in rdict and "sink" in rdict:
                            cdef.routes.append(rdict)
            elif st.stmt_type == "block":
                rdict = {ch.key: str(ch.value) for ch in st.children if ch.stmt_type == "kv"}
                if "source" in rdict and "sink" in rdict:
                    cdef.routes.append(rdict)
            return

        if len(parts) >= 2 and parts[0] == "CombineArrays" and parts[1] == "Object":
            cdef.sub_objects.append(self._ast_to_config_node(st, origin="default"))
            return

        if len(parts) >= 2 and parts[0] == "Object":
            obj_cat = parts[1]
            if obj_cat == "Widget":
                if len(parts) == 2:
                    # Object.Widget { host-copier."1" { ... } gain."1" { ... } }
                    for wch in st.children:
                        wparts = split_dotted_key(wch.key)
                        wclass = wparts[0]
                        winst = wparts[1] if len(wparts) > 1 else "1"
                        cdef.widgets.append(
                            {
                                "class_name": wclass,
                                "instance": winst,
                                "stmts": wch.children,
                            }
                        )
                elif len(parts) >= 3:
                    wclass = parts[2]
                    winst = parts[3] if len(parts) > 3 else "1"
                    cdef.widgets.append(
                        {
                            "class_name": wclass,
                            "instance": winst,
                            "stmts": st.children,
                        }
                    )
            elif obj_cat == "Base":
                if len(parts) == 2:
                    for bch in st.children:
                        bparts = split_dotted_key(bch.key)
                        if bparts and bparts[0].lstrip("!") == "route":
                            self._extract_class_embedded_objects(cdef, bch)
                        else:
                            cdef.sub_objects.append(self._ast_to_config_node(bch, origin="default"))
                elif len(parts) >= 3 and parts[2].lstrip("!") == "route":
                    if st.stmt_type == "array":
                        for item in st.children:
                            if item.stmt_type == "block":
                                rdict = {ch.key: str(ch.value) for ch in item.children if ch.stmt_type == "kv"}
                                if "source" in rdict and "sink" in rdict:
                                    cdef.routes.append(rdict)
                    elif st.stmt_type == "block":
                        rdict = {ch.key: str(ch.value) for ch in st.children if ch.stmt_type == "kv"}
                        if "source" in rdict and "sink" in rdict:
                            cdef.routes.append(rdict)
                else:
                    cdef.sub_objects.append(self._ast_to_config_node(st, origin="default"))
            elif obj_cat == "Control":
                if len(parts) == 2:
                    for cch in st.children:
                        cparts = split_dotted_key(cch.key)
                        ctype = cparts[0]
                        cinst = strip_quotes(cparts[1]) if len(cparts) > 1 else "1"
                        ccfg = self._ast_to_config_node(cch, origin="default")
                        ccfg.key = f"{ctype}.{cinst}"
                        cdef.controls.append(
                            {
                                "control_type": ctype,
                                "instance": cinst,
                                "config": ccfg,
                            }
                        )
                elif len(parts) >= 3:
                    ctype = parts[2]
                    cinst = strip_quotes(parts[3]) if len(parts) > 3 else "1"
                    ccfg = self._ast_to_config_node(st, origin="default")
                    ccfg.key = f"{ctype}.{cinst}"
                    cdef.controls.append(
                        {
                            "control_type": ctype,
                            "instance": cinst,
                            "config": ccfg,
                        }
                    )
            else:
                cdef.sub_objects.append(self._ast_to_config_node(st, origin="default"))

    def _rebuild_class_config_tree(self, cdef: TplgClassDef) -> None:
        root = ConfigNode(key=cdef.full_name, node_type="object", category_info=cdef.rel_path)
        for aname, adef in cdef.attributes.items():
            default_val = cdef.defaults.get(aname, "")
            valid_vals = adef.constraints.valid_values if adef.constraints else []
            root.set_scalar(
                key=aname,
                value=default_val,
                category_info=f"{adef.attr_type}  {adef.flags_summary}".strip(),
                origin="default",
                is_immutable=adef.is_immutable,
                is_mandatory=adef.is_mandatory,
                is_constructor=adef.is_constructor,
                valid_values=valid_vals,
            )
        for k, v in cdef.defaults.items():
            if k not in cdef.attributes:
                root.set_scalar(key=k, value=v, category_info="default", origin="default")
        if cdef.controls:
            ctrl_group = ConfigNode(key="Object.Control", node_type="object", category_info=f"{len(cdef.controls)} controls")
            for ctrl in cdef.controls:
                ctrl_group.children.append(ctrl["config"].clone())
            root.children.append(ctrl_group)
        for sub in cdef.sub_objects:
            root.children.append(sub.clone())
        cdef.config_tree = root

    def _resolve_pipeline_inheritance(self) -> None:
        """Resolve SubTreeCopy.baseclass inheritance for Pipeline classes."""
        resolved: Set[str] = set()

        def resolve_one(pname: str) -> None:
            if pname in resolved or pname not in self.pipeline_classes:
                return
            resolved.add(pname)
            pdef = self.pipeline_classes[pname]
            if not pdef.extends_class:
                return
            parts = split_dotted_key(pdef.extends_class)
            base_name = parts[-1] if parts else ""
            if base_name in self.pipeline_classes:
                resolve_one(base_name)
                base_def = self.pipeline_classes[base_name]
                # Merge attributes and defaults from base_def
                merged_attrs = {k: v for k, v in base_def.attributes.items()}
                merged_attrs.update(pdef.attributes)
                pdef.attributes = merged_attrs

                merged_defs = dict(base_def.defaults)
                merged_defs.update(pdef.defaults)
                pdef.defaults = merged_defs

                if not pdef.constructor_attrs:
                    pdef.constructor_attrs = list(base_def.constructor_attrs)

                # Merge widgets: keep base widgets and append new widgets
                existing_w = {(w["class_name"], w["instance"]) for w in base_def.widgets}
                combined_widgets = list(base_def.widgets)
                for w in pdef.widgets:
                    if (w["class_name"], w["instance"]) not in existing_w:
                        combined_widgets.append(w)
                pdef.widgets = combined_widgets

                # Merge routes: keep base routes unless overridden from same source, then add child routes
                child_sources = {r.get("source") for r in pdef.routes}
                combined_routes = [r for r in base_def.routes if r.get("source") not in child_sources]
                combined_routes.extend(pdef.routes)
                pdef.routes = combined_routes
                self._rebuild_class_config_tree(pdef)

        for pname in list(self.pipeline_classes.keys()):
            resolve_one(pname)

    def _ast_to_config_node(
        self,
        stmt: AstStmt,
        defines: Optional[Dict[str, str]] = None,
        origin: str = "instance",
    ) -> ConfigNode:
        defs = defines or {}
        if stmt.stmt_type == "kv":
            val = self._substitute_vars(str(stmt.value), defs)
            return ConfigNode(
                key=stmt.key,
                value=val,
                node_type="scalar",
                category_info=origin,
                origin=origin,
            )
        node = ConfigNode(
            key=stmt.key,
            value="",
            node_type="array" if stmt.stmt_type == "array" else "object",
            category_info=stmt.stmt_type,
            origin=origin,
        )
        for idx, ch in enumerate(stmt.children):
            if ch.stmt_type == "block" and ch.key == "{}":
                elem = self._ast_to_config_node(ch, defs, origin=origin)
                elem.key = f"[{idx}]"
                node.children.append(elem)
            else:
                node.children.append(self._ast_to_config_node(ch, defs, origin=origin))
        return node

    def _substitute_vars(self, text: str, defines: Dict[str, str]) -> str:
        if "$" not in text:
            return text
        # Replace $VAR with value from defines or global_defines
        def repl(m: re.Match) -> str:
            vname = m.group(1)
            if vname in defines:
                return str(defines[vname])
            if vname in self.global_defines:
                return str(self.global_defines[vname])
            return m.group(0)

        return re.sub(r"\$([A-Za-z0-9_]+)", repl, text)

    def _parse_cmake_targets(self) -> Dict[str, List[CMakeTargetVariant]]:
        """Parse production/tplg-targets-*.cmake and development/tplg-targets*.cmake."""
        by_conf: Dict[str, List[CMakeTargetVariant]] = {}
        tuple_re = re.compile(r'"([^";]+)\\;([^";]+)(?:\\;([^"]*))?"')

        for sub_cat in ("production", "development"):
            sub_dir = os.path.join(self.root_dir, sub_cat)
            if not os.path.isdir(sub_dir):
                continue
            for fname in sorted(os.listdir(sub_dir)):
                if not fname.endswith(".cmake"):
                    continue
                fpath = os.path.join(sub_dir, fname)
                rel_cmake = f"{sub_cat}/{fname}"
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
                # Join backslash-continued lines
                joined = re.sub(r"\\\s*\n\s*", "", raw)
                for m in tuple_re.finditer(joined):
                    in_conf = m.group(1).strip()
                    out_name = m.group(2).strip()
                    defs_raw = (m.group(3) or "").strip()
                    # Skip unexpanded CMake foreach loop templates; we handle bench targets separately below
                    if "${" in in_conf or "${" in out_name:
                        continue
                    defs_dict = self._parse_defines_str(defs_raw)
                    var = CMakeTargetVariant(
                        input_conf=in_conf,
                        output_name=out_name,
                        defines_str=defs_raw,
                        defines=defs_dict,
                        cmake_file=rel_cmake,
                        category=sub_cat,
                    )
                    by_conf.setdefault(in_conf, []).append(var)

        # Also expand key benchmark targets from development/tplg-targets-bench.cmake
        bench_cmake = os.path.join(self.root_dir, "development", "tplg-targets-bench.cmake")
        if os.path.isfile(bench_cmake):
            bench_Pairs = [
                ("gain", "BENCH_GAIN_PARAMS=default"),
                ("eqiir", "BENCH_EQIIR_PARAMS=loudness"),
                ("eqfir", "BENCH_EQFIR_PARAMS=loudness"),
                ("drc", "BENCH_DRC_PARAMS=default_speaker_mic"),
                ("dcblock", "BENCH_DCBLOCK_PARAMS=default"),
                ("src", "BENCH_SRC_PARAMS=default"),
                ("asrc", "BENCH_ASRC_PARAMS=default"),
                ("tdfb", "BENCH_TDFB_PARAMS=default"),
            ]
            for comp, bparam in bench_Pairs:
                for sf in ("16", "24", "32"):
                    defs_str = f"PLATFORM=mtl,BENCH_MODULE_FORMAT=s{sf},BENCH_CONFIG={comp}{sf},{bparam}"
                    by_conf.setdefault("cavs-benchmark-hda", []).append(
                        CMakeTargetVariant(
                            input_conf="cavs-benchmark-hda",
                            output_name=f"sof-hda-benchmark-{comp}{sf}",
                            defines_str=defs_str,
                            defines=self._parse_defines_str(defs_str),
                            cmake_file="development/tplg-targets-bench.cmake",
                            category="development",
                        )
                    )

        return by_conf

    @staticmethod
    def _parse_defines_str(defs_str: str) -> Dict[str, str]:
        res: Dict[str, str] = {}
        if not defs_str:
            return res
        for part in defs_str.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                res[k.strip()] = strip_quotes(v.strip())
        return res

    # -------------------------------------------------------------------------
    # Graph Builders for the 4 Left-Pane Selection Modes
    # -------------------------------------------------------------------------

    def build_graph_for_base_class(self, full_key: str) -> GraphViewModel:
        """Build a graph representing a Base / Control / PCM / Dai Class (Pane 1)."""
        cdef = self.base_classes[full_key]
        vm = GraphViewModel(
            title=cdef.full_name,
            subtitle=f"Base Class ({cdef.rel_path})",
            view_kind="base_class",
            source_file=cdef.source_file,
            root_config=cdef.config_tree.clone(),
        )
        main_id = f"{cdef.category}.{cdef.name}"
        badges = []
        if cdef.constructor_attrs:
            badges.append("ctor: " + ".".join(cdef.constructor_attrs))
        badges.append(f"{len(cdef.attributes)} attrs")

        main_node = GraphNode(
            id=main_id,
            title=cdef.name,
            subtitle=f"Class.{cdef.category}",
            category=self._map_category(cdef.category, cdef.name),
            class_category=cdef.category,
            class_name=cdef.name,
            in_ports=[GraphPort("in0", "parent", "in", "control")],
            out_ports=[GraphPort("out0", "instance", "out", "control")],
            badges=badges,
            config=cdef.config_tree.clone(),
            class_def=cdef,
            source_file=cdef.source_file,
        )
        vm.nodes[main_id] = main_node

        # Create child nodes for any embedded sub-objects or attribute groups
        for idx, sub in enumerate(cdef.sub_objects):
            sub_id = f"{main_id}.sub.{idx}"
            sub_node = GraphNode(
                id=sub_id,
                title=sub.key,
                subtitle="Embedded Object",
                category="base",
                class_category="Base",
                class_name=sub.key,
                in_ports=[GraphPort("in0", "in", "in", "control")],
                out_ports=[],
                badges=[f"{len(sub.children)} fields"],
                config=sub.clone(),
                source_file=cdef.source_file,
            )
            vm.nodes[sub_id] = sub_node
            vm.edges.append(
                GraphEdge(
                    id=f"e_{idx}",
                    source_node=main_id,
                    sink_node=sub_id,
                )
            )
        return vm

    def build_graph_for_module(self, module_name: str, include_testbench_io: bool = False) -> GraphViewModel:
        """Build a graph representing a Processing Module Class.Widget.<name> (Pane 2)."""
        cdef = self.processing_modules[module_name]
        vm = GraphViewModel(
            title=f"Class.Widget.{cdef.name}",
            subtitle=f"Processing Module ({cdef.rel_path})",
            view_kind="module",
            source_file=cdef.source_file,
            root_config=cdef.config_tree.clone(),
        )
        raw_in = cdef.defaults.get("num_input_pins")
        raw_out = cdef.defaults.get("num_output_pins")
        num_in = (
            int(raw_in)
            if raw_in is not None and str(raw_in).isdigit()
            else (0 if cdef.name in ("siggen", "pipeline") else 1)
        )
        num_out = (
            int(raw_out)
            if raw_out is not None and str(raw_out).isdigit()
            else (0 if cdef.name in ("wov", "pipeline") else 1)
        )
        in_ports = [GraphPort(f"in{i}", f"in.{i}", "in", "audio") for i in range(num_in)]
        out_ports = [GraphPort(f"out{i}", f"out.{i}", "out", "audio") for i in range(num_out)]

        wtype = cdef.defaults.get("type", "effect")
        cpc = cdef.defaults.get("cpc", "")
        badges = [f"type: {wtype}"]
        if cpc:
            badges.append(f"cpc: {cpc}")
        if cdef.controls:
            badges.append(f"ctrls: {len(cdef.controls)}")

        node_id = f"{cdef.name}.1.1"
        node_cfg = cdef.config_tree.clone()
        main_node = GraphNode(
            id=node_id,
            title=cdef.name,
            subtitle=f"Class.Widget.{cdef.name} ({wtype})",
            category=self._map_category("Widget", cdef.name),
            class_category="Widget",
            class_name=cdef.name,
            instance_id="1",
            pipeline_id=1,
            in_ports=in_ports,
            out_ports=out_ports,
            in_fmt_badge=self.summarize_pin_audio_format(node_cfg, "in"),
            out_fmt_badge=self.summarize_pin_audio_format(node_cfg, "out"),
            badges=badges,
            config=node_cfg,
            class_def=cdef,
            source_file=cdef.source_file,
        )
        vm.nodes[node_id] = main_node
        self.attach_widget_kcontrol_nodes(vm, main_node)
        self.attach_module_pin_format_nodes(vm, main_node)
        if include_testbench_io:
            self.attach_host_copier_io_nodes(vm)
        return vm

    def build_graph_for_pipeline(
        self,
        pipeline_name: str,
        index: int = 1,
        include_testbench_io: bool = True,
    ) -> GraphViewModel:
        """Build a graph for a Pipeline Class.Pipeline.<name> (Pane 3)."""
        pdef = self.pipeline_classes[pipeline_name]
        vm = GraphViewModel(
            title=f"Class.Pipeline.{pdef.name}",
            subtitle=f"Pipeline Template ({pdef.rel_path})",
            view_kind="pipeline",
            source_file=pdef.source_file,
            root_config=pdef.config_tree.clone(),
        )
        defines = dict(self.global_defines)
        defines["index"] = str(index)

        default_dir = "capture" if "capture" in pdef.name else "playback"
        direction = str(pdef.defaults.get("direction", default_dir))
        is_capture = "capture" in direction.lower() or "capture" in pdef.name
        container = PipelineContainer(
            pipeline_id=index,
            class_name=pdef.name,
            direction=direction,
            config=pdef.config_tree.clone(),
        )
        vm.containers[index] = container

        ordered_widget_ids: List[str] = []
        for wspec in pdef.widgets:
            wclass = wspec["class_name"]
            winst = str(wspec["instance"])
            if wclass == "pipeline":
                # Merge pipeline.1 widget attributes into container config
                for st in wspec["stmts"]:
                    if st.stmt_type == "kv":
                        container.config.set_scalar(st.key, self._substitute_vars(str(st.value), defines), origin="pipeline")
                continue
            wnode = self._instantiate_widget_node(
                wclass=wclass,
                winst=winst,
                pipeline_id=index,
                pipeline_class=pdef.name,
                pipeline_direction=direction,
                override_stmts=wspec["stmts"],
                defines=defines,
                source_file=pdef.source_file,
            )
            vm.nodes[wnode.id] = wnode
            self.attach_widget_kcontrol_nodes(vm, wnode)
            ordered_widget_ids.append(wnode.id)

        # Add explicit routes from the pipeline class
        added_pairs: Set[Tuple[str, str]] = set()
        for ridx, rdict in enumerate(pdef.routes):
            src_raw = self._substitute_vars(rdict["source"], defines)
            snk_raw = self._substitute_vars(rdict["sink"], defines)
            src_id = self._match_widget_id(src_raw, vm.nodes, index)
            snk_id = self._match_widget_id(snk_raw, vm.nodes, index)
            if src_id and snk_id and (src_id, snk_id) not in added_pairs:
                added_pairs.add((src_id, snk_id))
                edge_cfg = ConfigNode("Object.Base.route", node_type="object")
                edge_cfg.set_scalar("source", src_id, origin="pipeline")
                edge_cfg.set_scalar("sink", snk_id, origin="pipeline")
                edge_cfg.set_scalar("index", str(index), origin="pipeline")
                vm.edges.append(
                    GraphEdge(
                        id=f"route_{index}_{ridx}",
                        source_node=src_id,
                        sink_node=snk_id,
                        pipeline_id=index,
                        config=edge_cfg,
                    )
                )

        # Connect endpoint copiers / mixers whose route is normally added at top-level in machine topologies
        def _has_audio_in(nid: str) -> bool:
            return any(
                e.sink_node == nid
                and e.source_node in vm.nodes
                and vm.nodes[e.source_node].class_category == "Widget"
                for e in vm.edges
            )

        def _has_audio_out(nid: str) -> bool:
            return any(
                e.source_node == nid
                and e.sink_node in vm.nodes
                and vm.nodes[e.sink_node].class_category == "Widget"
                for e in vm.edges
            )

        def _add_implicit_edge(u_id: str, v_id: str, tag: int) -> None:
            if u_id != v_id and (u_id, v_id) not in added_pairs:
                added_pairs.add((u_id, v_id))
                edge_cfg = ConfigNode("Object.Base.route (endpoint)", node_type="object")
                edge_cfg.set_scalar("source", u_id, origin="pipeline")
                edge_cfg.set_scalar("sink", v_id, origin="pipeline")
                vm.edges.append(
                    GraphEdge(
                        id=f"implicit_{index}_{tag}",
                        source_node=u_id,
                        sink_node=v_id,
                        pipeline_id=index,
                        config=edge_cfg,
                    )
                )

        if len(ordered_widget_ids) >= 2:
            imp_idx = 0
            for nid in ordered_widget_ids:
                if _has_audio_in(nid) or _has_audio_out(nid):
                    continue
                wn = vm.nodes[nid]
                wc = wn.class_name
                wt = str(wn.config.get_scalar("type", "")).lower()
                is_source_endpoint = (
                    (wc == "host-copier" and (not is_capture or wt == "aif_in"))
                    or (wc in ("dai-copier", "alh-copier") and (is_capture or wt == "dai_out"))
                    or wc == "mixout"
                )
                is_sink_endpoint = (
                    (wc == "host-copier" and (is_capture or wt == "aif_out"))
                    or (wc in ("dai-copier", "alh-copier") and not is_capture and wt != "dai_out")
                    or wc == "mixin"
                )
                if is_source_endpoint:
                    head_id = next(
                        (w for w in ordered_widget_ids if w != nid and not _has_audio_in(w)),
                        None,
                    )
                    if head_id:
                        _add_implicit_edge(nid, head_id, imp_idx)
                        imp_idx += 1
                elif is_sink_endpoint:
                    tail_id = next(
                        (w for w in reversed(ordered_widget_ids) if w != nid and not _has_audio_out(w)),
                        None,
                    )
                    if tail_id:
                        _add_implicit_edge(tail_id, nid, imp_idx)
                        imp_idx += 1

            # Connect any remaining completely isolated internal widgets to the chain head
            for nid in ordered_widget_ids:
                if not _has_audio_in(nid) and not _has_audio_out(nid):
                    head_id = next(
                        (w for w in ordered_widget_ids if w != nid and not _has_audio_in(w)),
                        None,
                    )
                    if head_id:
                        _add_implicit_edge(nid, head_id, imp_idx)
                        imp_idx += 1

        if include_testbench_io:
            self.attach_host_copier_io_nodes(vm)
        self.attach_pipeline_pin_format_nodes(vm, is_capture=is_capture)
        return vm

    def build_graph_for_topology(
        self,
        conf_path: str,
        variant: Optional[CMakeTargetVariant] = None,
    ) -> GraphViewModel:
        """Build a full multi-pipeline graph for a Machine Topology .conf (and optional CMake variant)."""
        rel_path = os.path.relpath(conf_path, self.root_dir)
        title = variant.output_name if variant else os.path.basename(conf_path)
        subtitle = f"{rel_path}  [{variant.defines_str}]" if (variant and variant.defines_str) else rel_path

        defines = dict(self.global_defines)
        if rel_path.startswith("platform" + os.sep) or "/platform/" in conf_path:
            defines.update(self.platform_defaults)
        if variant:
            defines.update(variant.defines)
        else:
            # If user selected the top-level .conf parent node directly, apply the canonical variant defines if available
            base_stem = os.path.splitext(os.path.basename(conf_path))[0]
            for entry in self.machine_topologies:
                if os.path.abspath(entry.conf_path) == os.path.abspath(conf_path) and entry.variants:
                    chosen = next((v for v in entry.variants if v.output_name == base_stem), entry.variants[0])
                    defines.update(chosen.defines)
                    variant = chosen
                    subtitle = f"{rel_path}  [default: {chosen.output_name}]"
                    break

        vm = GraphViewModel(
            title=title,
            subtitle=subtitle,
            view_kind="topology",
            source_file=conf_path,
            defines=defines,
            target_variant=variant,
            root_config=ConfigNode(key=title, node_type="object", category_info=subtitle),
        )

        cli_keys: Set[str] = set(variant.defines.keys()) if variant else set()
        search_dirs: List[str] = ["include", "include/common", "include/components", "include/dais", "include/pipelines", "include/pipelines/cavs", "platform", "platform/intel"]
        skipped_branches: List[Tuple[str, str, str, AstStmt, str]] = []
        expanded_stmts = self._evaluate_topology_file(
            conf_path,
            defines,
            search_dirs,
            cli_keys=cli_keys,
            skipped_branches=skipped_branches,
        )

        # Populate root_config with active Define variables
        def_node = ConfigNode(key="Define (Active Variables)", node_type="define", category_info=f"{len(defines)} vars")
        for k, v in sorted(defines.items()):
            if k not in self.global_defines or (variant and k in variant.defines):
                def_node.set_scalar(k, v, category_info="Define", origin="define")
        vm.root_config.children.append(def_node)

        raw_routes: List[Tuple[Dict[str, str], Optional[int]]] = []
        top_widgets: List[Tuple[str, str, List[AstStmt]]] = []

        # Process top-level Object.Pipeline, Object.Dai, Object.PCM, Object.Widget, Object.Base.route
        for st in expanded_stmts:
            if not st.key.startswith("Object."):
                continue
            parts = split_dotted_key(st.key)
            if len(parts) < 2:
                continue
            obj_cat = parts[1]

            if obj_cat == "Pipeline":
                self._process_topology_pipeline_stmt(st, parts, vm, defines, raw_routes, conf_path)
            elif obj_cat == "Widget":
                self._collect_topology_widget_stmt(st, parts, top_widgets)
            elif obj_cat == "Base":
                self._collect_topology_route_stmt(st, parts, defines, raw_routes)
            elif obj_cat == "Dai":
                self._process_topology_dai_stmt(st, parts, vm, defines, conf_path)
            elif obj_cat == "PCM":
                self._process_topology_pcm_stmt(st, parts, vm, defines, conf_path)

        # First pass over top-level Object.Widget: create/update PipelineContainers from Object.Widget.pipeline
        custom_pipe_ids: Set[int] = set()
        nested_pipe_widgets: List[Tuple[str, str, List[AstStmt]]] = []
        for wclass, winst, wstmts in top_widgets:
            if wclass != "pipeline":
                continue
            prov = next((s.provenance for s in wstmts if s.provenance), "")
            expanded_wstmts = self._expand_inner_include_by_key(wstmts, defines, search_dirs, conf_path)
            p_idx: Optional[int] = None
            for ch in expanded_wstmts:
                if ch.stmt_type == "kv" and ch.key == "index":
                    val_s = self._substitute_vars(str(ch.value), defines)
                    if val_s.lstrip("-").isdigit():
                        p_idx = int(val_s)
            if p_idx is None and winst.lstrip("-").isdigit():
                p_idx = int(winst)
            if p_idx is None:
                continue
            if p_idx not in vm.containers:
                pdef_base = self.processing_modules.get("pipeline")
                pcfg = pdef_base.config_tree.clone() if pdef_base else ConfigNode("Object.Widget.pipeline", node_type="object")
                pcfg.set_scalar("index", str(p_idx), origin="instance", is_constructor=True)
                en_by = prov or f"Defined in {rel_path}"
                pcfg.set_scalar("enabled_by", en_by, category_info="IncludeByKey Provenance", origin="default", is_immutable=True)
                vm.containers[p_idx] = PipelineContainer(
                    pipeline_id=p_idx,
                    class_name="io-gateway",
                    direction="playback",
                    enabled_by=en_by,
                    config=pcfg,
                )
                custom_pipe_ids.add(p_idx)
            pipe_attr_stmts: List[AstStmt] = []
            for ch in expanded_wstmts:
                if ch.key.startswith("Object.Widget"):
                    self._collect_topology_widget_stmt(
                        ch,
                        split_dotted_key(ch.key),
                        nested_pipe_widgets,
                        default_index=p_idx,
                    )
                else:
                    pipe_attr_stmts.append(ch)
            self._apply_override_stmts_to_config(vm.containers[p_idx].config, pipe_attr_stmts, defines)

        top_widgets.extend(nested_pipe_widgets)

        # Second pass: instantiate standalone/top-level Object.Widget nodes
        for wclass, winst, wstmts in top_widgets:
            if wclass in ("virtual", "pipeline"):
                continue  # Keep graph focused on active DSP pipelines/widgets/DAIs/PCMs
            # Find index in wstmts
            p_idx = None
            w_dir: Optional[str] = None
            w_type: Optional[str] = None
            for ch in wstmts:
                if ch.stmt_type == "kv":
                    if ch.key == "index":
                        val_s = self._substitute_vars(str(ch.value), defines)
                        if val_s.lstrip("-").isdigit():
                            p_idx = int(val_s)
                    elif ch.key == "direction":
                        w_dir = strip_quotes(self._substitute_vars(str(ch.value), defines)).lower()
                    elif ch.key == "type":
                        w_type = strip_quotes(self._substitute_vars(str(ch.value), defines)).lower()

            if p_idx is None and winst.lstrip("-").isdigit() and int(winst) in vm.containers:
                p_idx = int(winst)

            if p_idx is not None and p_idx not in vm.containers:
                prov = next((s.provenance for s in wstmts if s.provenance), "")
                en_by = prov or f"Defined in {rel_path}"
                pcfg = ConfigNode(f"Pipeline.{p_idx}", node_type="object")
                pcfg.set_scalar("index", str(p_idx), origin="instance", is_constructor=True)
                pcfg.set_scalar("enabled_by", en_by, category_info="IncludeByKey Provenance", origin="default", is_immutable=True)
                vm.containers[p_idx] = PipelineContainer(
                    pipeline_id=p_idx,
                    class_name="io-gateway",
                    direction="playback",
                    enabled_by=en_by,
                    config=pcfg,
                )
                custom_pipe_ids.add(p_idx)

            if p_idx in custom_pipe_ids:
                is_cap = (w_dir == "capture") or (w_type == "dai_out")
                if is_cap:
                    vm.containers[p_idx].direction = "capture"
                    vm.containers[p_idx].class_name = "io-gateway-capture"

            wnode = self._instantiate_widget_node(
                wclass=wclass,
                winst=winst,
                pipeline_id=p_idx,
                pipeline_class=vm.containers[p_idx].class_name if (p_idx in vm.containers) else "top-level",
                pipeline_direction=vm.containers[p_idx].direction if (p_idx in vm.containers) else "playback",
                override_stmts=wstmts,
                defines=defines,
                source_file=conf_path,
            )
            vm.nodes[wnode.id] = wnode
            self.attach_widget_kcontrol_nodes(vm, wnode)

        # Resolve all collected routes into GraphEdges
        seen_edges: Set[Tuple[str, str]] = set()
        for ridx, (rdict, p_idx) in enumerate(raw_routes):
            src_raw = self._substitute_vars(rdict.get("source", ""), defines)
            snk_raw = self._substitute_vars(rdict.get("sink", ""), defines)
            ctrl_raw = self._substitute_vars(rdict.get("control", ""), defines)
            src_id = self._match_widget_id(src_raw, vm.nodes, p_idx)
            snk_id = self._match_widget_id(snk_raw, vm.nodes, p_idx)
            if src_id and snk_id and (src_id, snk_id) not in seen_edges:
                seen_edges.add((src_id, snk_id))
                src_p = vm.nodes[src_id].pipeline_id
                snk_p = vm.nodes[snk_id].pipeline_id
                is_inter = (src_p is not None and snk_p is not None and src_p != snk_p)
                edge_cfg = ConfigNode("Object.Base.route", node_type="object")
                edge_cfg.set_scalar("source", src_id, origin="instance")
                edge_cfg.set_scalar("sink", snk_id, origin="instance")
                if ctrl_raw:
                    edge_cfg.set_scalar("control", ctrl_raw, origin="instance")
                if p_idx is not None:
                    edge_cfg.set_scalar("index", str(p_idx), origin="instance")
                vm.edges.append(
                    GraphEdge(
                        id=f"edge_{ridx}",
                        source_node=src_id,
                        sink_node=snk_id,
                        control=ctrl_raw,
                        pipeline_id=p_idx,
                        is_inter_pipeline=is_inter,
                        config=edge_cfg,
                    )
                )

        self._analyze_unconnected_pipelines(vm, conf_path, defines, search_dirs, skipped_branches)
        self.attach_host_copier_io_nodes(vm)
        return vm

    def _evaluate_topology_file(
        self,
        fpath: str,
        defines: Dict[str, str],
        search_dirs: List[str],
        visited: Optional[Set[str]] = None,
        cli_keys: Optional[Set[str]] = None,
        active_conditions: Tuple[str, ...] = (),
        skipped_branches: Optional[List[Tuple[str, str, str, AstStmt, str]]] = None,
    ) -> List[AstStmt]:
        if visited is None:
            visited = set()
        if cli_keys is None:
            cli_keys = set()
        abs_f = os.path.abspath(fpath)
        if abs_f in visited:
            return []
        visited.add(abs_f)

        stmts = self.get_ast(abs_f)
        return self._evaluate_stmt_list(
            stmts,
            defines,
            search_dirs,
            abs_f,
            visited,
            cli_keys,
            active_conditions=active_conditions,
            skipped_branches=skipped_branches,
        )

    def _evaluate_stmt_list(
        self,
        stmts: List[AstStmt],
        defines: Dict[str, str],
        search_dirs: List[str],
        current_file: str,
        visited: Set[str],
        cli_keys: Set[str],
        active_conditions: Tuple[str, ...] = (),
        skipped_branches: Optional[List[Tuple[str, str, str, AstStmt, str]]] = None,
    ) -> List[AstStmt]:
        out: List[AstStmt] = []
        for st in stmts:
            if st.stmt_type == "searchdir":
                if st.key not in search_dirs:
                    search_dirs.append(st.key)
            elif st.stmt_type == "include":
                inc_path = self.resolve_include(st.key, search_dirs, current_file)
                if inc_path and not self._is_pure_class_include(inc_path):
                    out.extend(
                        self._evaluate_topology_file(
                            inc_path,
                            defines,
                            search_dirs,
                            visited,
                            cli_keys,
                            active_conditions=active_conditions,
                            skipped_branches=skipped_branches,
                        )
                    )
            elif st.stmt_type == "block" and st.key == "Define":
                for ch in st.children:
                    if ch.stmt_type == "kv":
                        # CLI / CMake target defines take precedence over .conf Define blocks,
                        # while later .conf Define blocks override earlier default Define blocks.
                        if not self._is_target_override(ch.key, defines, cli_keys):
                            defines[ch.key] = self._substitute_vars(str(ch.value), defines)
                        else:
                            defines.setdefault(ch.key, self._substitute_vars(str(ch.value), defines))
            elif st.stmt_type == "block" and st.key.startswith("IncludeByKey."):
                parts = split_dotted_key(st.key)
                var_name = parts[1] if len(parts) > 1 else ""
                var_val = str(defines.get(var_name, ""))
                matched_ch: Optional[AstStmt] = None
                matched_pattern: str = ""
                for ch in st.children:
                    pattern = strip_quotes(ch.key)
                    if matched_ch is None and self._key_matches(pattern, var_val):
                        matched_ch = ch
                        matched_pattern = pattern
                    elif skipped_branches is not None:
                        skipped_branches.append((var_name, var_val, pattern, ch, current_file))
                if matched_ch is not None:
                    if matched_ch.stmt_type == "kv" and matched_ch.value:
                        inc_file = self._substitute_vars(str(matched_ch.value), defines)
                        inc_path = self.resolve_include(inc_file, search_dirs, current_file)
                        if inc_path:
                            cond = f'{var_name} = "{var_val}" → {inc_file}'
                            out.extend(
                                self._evaluate_topology_file(
                                    inc_path,
                                    defines,
                                    search_dirs,
                                    set(),
                                    cli_keys,
                                    active_conditions=active_conditions + (cond,),
                                    skipped_branches=skipped_branches,
                                )
                            )
                    elif matched_ch.stmt_type == "block":
                        cond = f'{var_name} = "{var_val}" (matches "{matched_pattern}")'
                        out.extend(
                            self._evaluate_stmt_list(
                                matched_ch.children,
                                defines,
                                search_dirs,
                                current_file,
                                visited,
                                cli_keys,
                                active_conditions=active_conditions + (cond,),
                                skipped_branches=skipped_branches,
                            )
                        )
            else:
                st.provenance = "  ▸  ".join(active_conditions)
                out.append(st)
        return out

    @staticmethod
    def _is_target_override(key: str, defines: Dict[str, str], cli_keys: Set[str]) -> bool:
        return key in cli_keys and defines.get(key, "") not in ("", "none")

    @staticmethod
    def _key_matches(pattern: str, val: str) -> bool:
        if pattern == val:
            return True
        try:
            return bool(re.fullmatch(pattern, val))
        except re.error:
            return False

    def _is_pure_class_include(self, inc_path: str) -> bool:
        rel = os.path.relpath(inc_path, self.root_dir)
        return rel.startswith("include/") and not rel.startswith("include/bench/")

    def _process_topology_pipeline_stmt(
        self,
        st: AstStmt,
        parts: List[str],
        vm: GraphViewModel,
        defines: Dict[str, str],
        raw_routes: List[Tuple[Dict[str, str], Optional[int]]],
        source_file: str,
    ) -> None:
        # Forms:
        # 1) Object.Pipeline { class_name [ { index 1 ... } ] }
        # 2) Object.Pipeline.class_name [ { index 1 ... } ]
        # 3) Object.Pipeline.class_name."1" { ... }
        prov = st.provenance
        if len(parts) == 2 and st.stmt_type == "block":
            for pch in st.children:
                pparts = split_dotted_key(pch.key)
                pclass = pparts[0]
                if pch.stmt_type == "array":
                    for item in pch.children:
                        if item.stmt_type == "block":
                            self._instantiate_pipeline_in_topology(pclass, None, item.children, vm, defines, raw_routes, source_file, provenance=prov)
                elif pch.stmt_type == "block":
                    pidx_str = pparts[1] if len(pparts) > 1 else None
                    self._instantiate_pipeline_in_topology(pclass, pidx_str, pch.children, vm, defines, raw_routes, source_file, provenance=prov)
        elif len(parts) == 3 and st.stmt_type == "array":
            pclass = parts[2]
            for item in st.children:
                if item.stmt_type == "block":
                    self._instantiate_pipeline_in_topology(pclass, None, item.children, vm, defines, raw_routes, source_file, provenance=prov)
        elif len(parts) >= 3 and st.stmt_type == "block":
            pclass = parts[2]
            pidx_str = parts[3] if len(parts) > 3 else None
            self._instantiate_pipeline_in_topology(pclass, pidx_str, st.children, vm, defines, raw_routes, source_file, provenance=prov)

    def _instantiate_pipeline_in_topology(
        self,
        pclass: str,
        explicit_idx: Optional[str],
        body_stmts: List[AstStmt],
        vm: GraphViewModel,
        defines: Dict[str, str],
        raw_routes: List[Tuple[Dict[str, str], Optional[int]]],
        source_file: str,
        provenance: str = "",
    ) -> None:
        # Determine pipeline index
        p_idx = 0
        if explicit_idx:
            sub_idx = self._substitute_vars(explicit_idx, defines)
            if sub_idx.lstrip("-").isdigit():
                p_idx = int(sub_idx)
        for ch in body_stmts:
            if ch.stmt_type == "kv" and ch.key == "index":
                sub_idx = self._substitute_vars(str(ch.value), defines)
                if sub_idx.lstrip("-").isdigit():
                    p_idx = int(sub_idx)

        pdef = self.pipeline_classes.get(pclass)
        local_defs = dict(defines)
        local_defs["index"] = str(p_idx)

        direction = str(pdef.defaults.get("direction", "playback")) if pdef else "playback"
        for ch in body_stmts:
            if ch.stmt_type == "kv" and ch.key == "direction":
                direction = self._substitute_vars(str(ch.value), local_defs)

        rel_src = os.path.relpath(source_file, self.root_dir)
        en_by = provenance or f"Defined in {rel_src}"
        container_cfg = pdef.config_tree.clone() if pdef else ConfigNode(f"Pipeline.{pclass}", node_type="object")
        container_cfg.set_scalar("index", str(p_idx), origin="instance", is_constructor=True)
        container_cfg.set_scalar("enabled_by", en_by, category_info="IncludeByKey Provenance", origin="default", is_immutable=True)

        # Collect instance-level widget overrides inside this Object.Pipeline block
        widget_overrides: Dict[Tuple[str, str], List[AstStmt]] = {}
        for ch in body_stmts:
            if ch.stmt_type == "kv":
                container_cfg.set_scalar(ch.key, self._substitute_vars(str(ch.value), local_defs), origin="instance")
            elif ch.key.startswith("Object.Widget"):
                wparts = split_dotted_key(ch.key)
                if len(wparts) == 2 and ch.stmt_type == "block":
                    for wch in ch.children:
                        wp = split_dotted_key(wch.key)
                        wc = wp[0]
                        wi = wp[1] if len(wp) > 1 else "1"
                        widget_overrides.setdefault((wc, wi), []).extend(wch.children)
                elif len(wparts) >= 3:
                    wc = wparts[2]
                    wi = wparts[3] if len(wparts) > 3 else "1"
                    widget_overrides.setdefault((wc, wi), []).extend(ch.children)

        container = PipelineContainer(
            pipeline_id=p_idx,
            class_name=pclass,
            direction=direction,
            enabled_by=en_by,
            config=container_cfg,
        )
        vm.containers[p_idx] = container

        if pdef:
            for wspec in pdef.widgets:
                wc = wspec["class_name"]
                wi = str(wspec["instance"])
                if wc == "pipeline":
                    continue
                combined_stmts = list(wspec["stmts"]) + widget_overrides.pop((wc, wi), [])
                wnode = self._instantiate_widget_node(
                    wclass=wc,
                    winst=wi,
                    pipeline_id=p_idx,
                    pipeline_class=pclass,
                    pipeline_direction=direction,
                    override_stmts=combined_stmts,
                    defines=local_defs,
                    source_file=source_file,
                )
                vm.nodes[wnode.id] = wnode
                self.attach_widget_kcontrol_nodes(vm, wnode)

            for rdict in pdef.routes:
                raw_routes.append(
                    (
                        {
                            "source": self._substitute_vars(rdict["source"], local_defs),
                            "sink": self._substitute_vars(rdict["sink"], local_defs),
                        },
                        p_idx,
                    )
                )

        # Instantiate any additional widgets declared only in the pipeline instance
        for (wc, wi), ov_stmts in widget_overrides.items():
            if wc == "pipeline":
                continue
            wnode = self._instantiate_widget_node(
                wclass=wc,
                winst=wi,
                pipeline_id=p_idx,
                pipeline_class=pclass,
                pipeline_direction=direction,
                override_stmts=ov_stmts,
                defines=local_defs,
                source_file=source_file,
            )
            vm.nodes[wnode.id] = wnode
            self.attach_widget_kcontrol_nodes(vm, wnode)

    def _expand_inner_include_by_key(
        self,
        stmts: List[AstStmt],
        defines: Dict[str, str],
        search_dirs: List[str],
        current_file: str,
    ) -> List[AstStmt]:
        out: List[AstStmt] = []
        for st in stmts:
            if st.stmt_type == "block" and st.key.startswith("IncludeByKey."):
                parts = split_dotted_key(st.key)
                var_name = parts[1] if len(parts) > 1 else ""
                var_val = str(defines.get(var_name, ""))
                for ch in st.children:
                    if self._key_matches(strip_quotes(ch.key), var_val):
                        if ch.stmt_type == "kv" and ch.value:
                            inc_file = self._substitute_vars(str(ch.value), defines)
                            inc_path = self.resolve_include(inc_file, search_dirs, current_file)
                            if inc_path:
                                out.extend(self._expand_inner_include_by_key(self.get_ast(inc_path), defines, search_dirs, inc_path))
                        elif ch.stmt_type == "block":
                            out.extend(self._expand_inner_include_by_key(ch.children, defines, search_dirs, current_file))
                        break
            elif st.stmt_type == "include":
                inc_path = self.resolve_include(st.key, search_dirs, current_file)
                if inc_path:
                    out.extend(self._expand_inner_include_by_key(self.get_ast(inc_path), defines, search_dirs, inc_path))
            else:
                out.append(st)
        return out

    def _collect_topology_widget_stmt(
        self,
        st: AstStmt,
        parts: List[str],
        top_widgets: List[Tuple[str, str, List[AstStmt]]],
        default_index: Optional[int] = None,
    ) -> None:
        prov = st.provenance

        def with_default_idx(children: List[AstStmt]) -> List[AstStmt]:
            res = list(children)
            if default_index is not None and not any(ch.stmt_type == "kv" and ch.key == "index" for ch in res):
                res = [AstStmt(stmt_type="kv", key="index", value=str(default_index))] + res
            if prov:
                for ch in res:
                    if not ch.provenance:
                        ch.provenance = prov
            return res

        if len(parts) == 2 and st.stmt_type == "block":
            for wch in st.children:
                wp = split_dotted_key(wch.key)
                wc = wp[0]
                if wch.stmt_type == "array":
                    for idx, item in enumerate(wch.children):
                        if item.stmt_type == "block":
                            top_widgets.append((wc, str(idx), with_default_idx(item.children)))
                elif wch.stmt_type == "block":
                    wi = wp[1] if len(wp) > 1 else "1"
                    top_widgets.append((wc, wi, with_default_idx(wch.children)))
        elif len(parts) >= 3:
            wc = parts[2]
            if st.stmt_type == "array":
                for idx, item in enumerate(st.children):
                    if item.stmt_type == "block":
                        top_widgets.append((wc, str(idx), with_default_idx(item.children)))
            elif st.stmt_type == "block":
                wi = parts[3] if len(parts) > 3 else "1"
                top_widgets.append((wc, wi, with_default_idx(st.children)))

    def _analyze_unconnected_pipelines(
        self,
        vm: GraphViewModel,
        conf_path: str,
        defines: Dict[str, str],
        search_dirs: List[str],
        skipped_branches: List[Tuple[str, str, str, AstStmt, str]],
    ) -> None:
        """Detect pipelines with missing routes and explain how they were enabled and how to connect them."""
        if not vm.containers:
            return

        # 1. Extract routes inside skipped IncludeByKey branches so we can tell the user how to enable them
        skipped_routes: List[Tuple[str, str, str, str, str, str]] = []
        for var_name, cur_val, req_pattern, ch, cur_file in skipped_branches:
            branch_routes: List[Tuple[Dict[str, str], Optional[int]]] = []
            inc_label = ""
            if ch.stmt_type == "kv" and ch.value:
                inc_file = self._substitute_vars(str(ch.value), defines)
                inc_label = inc_file
                inc_path = self.resolve_include(inc_file, search_dirs, cur_file)
                if inc_path:
                    for sst in self.get_ast(inc_path):
                        if sst.key.startswith("Object.Base"):
                            self._collect_topology_route_stmt(sst, split_dotted_key(sst.key), defines, branch_routes)
            elif ch.stmt_type == "block":
                for sst in ch.children:
                    if sst.key.startswith("Object.Base"):
                        self._collect_topology_route_stmt(sst, split_dotted_key(sst.key), defines, branch_routes)
            for rdict, _ in branch_routes:
                src_s = self._substitute_vars(rdict.get("source", ""), defines)
                snk_s = self._substitute_vars(rdict.get("sink", ""), defines)
                if src_s and snk_s:
                    skipped_routes.append((src_s, snk_s, var_name, cur_val, req_pattern, inc_label))

        # Find CMake variants for this topology so we can suggest matching targets
        topo_variants: List[CMakeTargetVariant] = []
        for entry in self.machine_topologies:
            if os.path.abspath(entry.conf_path) == os.path.abspath(conf_path):
                topo_variants = entry.variants
                break

        # 2. Check each pipeline container for widgets missing their upstream/downstream route
        for pid, cont in vm.containers.items():
            p_nodes = [
                n for n in vm.nodes.values()
                if n.pipeline_id == pid and n.class_category == "Widget"
            ]
            if not p_nodes:
                continue

            warnings: List[str] = []
            for n in p_nodes:
                real_in = [
                    e for e in vm.edges
                    if e.sink_node == n.id
                    and e.source_node in vm.nodes
                    and vm.nodes[e.source_node].class_category == "Widget"
                ]
                real_out = [
                    e for e in vm.edges
                    if e.source_node == n.id
                    and e.sink_node in vm.nodes
                    and vm.nodes[e.sink_node].class_category == "Widget"
                ]
                is_cap = (
                    "capture" in cont.direction.lower()
                    or "capture" in n.id.lower()
                    or str(n.config.get_scalar("direction", "")).lower() == "capture"
                    or str(n.config.get_scalar("type", "")).lower() == "dai_out"
                )

                missing_desc = ""
                if "host-copier" in n.class_name:
                    if is_cap and not real_in:
                        missing_desc = "has no upstream capture source route"
                    elif not is_cap and not real_out:
                        missing_desc = "has no downstream playback sink route"
                elif "dai-copier" in n.class_name or "alh-copier" in n.class_name:
                    if is_cap and not real_out:
                        missing_desc = "has no downstream capture route (left port unconnected)"
                    elif not is_cap and not real_in:
                        missing_desc = "has no upstream playback route (left port unconnected)"
                elif n.class_name not in ("kpb", "siggen"):
                    if not real_in and not real_out:
                        missing_desc = "has no input or output routes"
                    elif not real_in:
                        missing_desc = "has no input route"
                    elif not real_out:
                        missing_desc = "has no output route"

                if not missing_desc:
                    continue

                lines = [
                    f"⚠ Pipeline {pid} ({cont.class_name}): '{n.id}' {missing_desc}",
                    f"• Pipeline enabled by: {cont.enabled_by or 'Top-level topology definition'}",
                ]

                # Check if any skipped IncludeByKey branch connects this widget
                matched_hints: List[str] = []
                for src_s, snk_s, var_name, cur_val, req_pattern, inc_label in skipped_routes:
                    if src_s == n.id or snk_s == n.id:
                        inc_part = f" → {inc_label}" if inc_label else ""
                        matching_vars = [
                            v.output_name for v in topo_variants
                            if self._key_matches(req_pattern, str(v.defines.get(var_name, "")))
                        ]
                        var_hint = f" (e.g. target '{matching_vars[0]}')" if matching_vars else ""
                        matched_hints.append(
                            f"• Connected when: IncludeByKey.{var_name} matches \"{req_pattern}\" "
                            f"(currently \"{cur_val}\"){inc_part}\n"
                            f"  Routes: {src_s} → {snk_s}{var_hint}"
                        )
                if matched_hints:
                    lines.extend(matched_hints[:2])
                else:
                    lines.append(f"• No Object.Base.route in active Define configuration connects '{n.id}'.")
                warnings.append("\n".join(lines))

            if warnings:
                cont.warning_text = "\n\n".join(warnings)
                cont.config.set_scalar(
                    "warning",
                    cont.warning_text.replace("\n", "  |  "),
                    category_info="Unconnected Pipeline Diagnostic",
                    origin="default",
                    is_immutable=True,
                )

    def _collect_topology_route_stmt(
        self,
        st: AstStmt,
        parts: List[str],
        defines: Dict[str, str],
        raw_routes: List[Tuple[Dict[str, str], Optional[int]]],
    ) -> None:
        if len(parts) == 2 and st.stmt_type == "block":
            for bch in st.children:
                bparts = split_dotted_key(bch.key)
                if bparts and bparts[0].lstrip("!") == "route":
                    self._collect_topology_route_stmt(bch, ["Object", "Base", "route"], defines, raw_routes)
        elif len(parts) >= 3 and parts[2].lstrip("!") == "route":
            if st.stmt_type == "array":
                for item in st.children:
                    if item.stmt_type == "block":
                        rdict = {ch.key: self._substitute_vars(str(ch.value), defines) for ch in item.children if ch.stmt_type == "kv"}
                        if "source" in rdict and "sink" in rdict:
                            idx_val = int(rdict["index"]) if rdict.get("index", "").isdigit() else None
                            raw_routes.append((rdict, idx_val))
            elif st.stmt_type == "block":
                rdict = {ch.key: self._substitute_vars(str(ch.value), defines) for ch in st.children if ch.stmt_type == "kv"}
                if "source" in rdict and "sink" in rdict:
                    idx_val = int(rdict["index"]) if rdict.get("index", "").isdigit() else None
                    raw_routes.append((rdict, idx_val))

    def _process_topology_dai_stmt(
        self,
        st: AstStmt,
        parts: List[str],
        vm: GraphViewModel,
        defines: Dict[str, str],
        source_file: str,
    ) -> None:
        # e.g. Object.Dai.HDA [ { name ... dai_index 0 ... } ]
        if len(parts) >= 3 and st.stmt_type == "array":
            dai_class = parts[2]
            cdef = self.all_classes.get(f"Dai.{dai_class}")
            for idx, item in enumerate(st.children):
                if item.stmt_type != "block":
                    continue
                cfg = cdef.config_tree.clone() if cdef else ConfigNode(f"Object.Dai.{dai_class}", node_type="object")
                for ch in item.children:
                    if ch.stmt_type == "kv":
                        cfg.set_scalar(ch.key, self._substitute_vars(str(ch.value), defines), origin="instance")
                    else:
                        cfg.children.append(self._ast_to_config_node(ch, defines, origin="instance"))
                dname = str(cfg.get_scalar("name", cfg.get_scalar("dai_index", idx)))
                vm.root_config.children.append(cfg)

    def _process_topology_pcm_stmt(
        self,
        st: AstStmt,
        parts: List[str],
        vm: GraphViewModel,
        defines: Dict[str, str],
        source_file: str,
    ) -> None:
        if len(parts) >= 3 and st.stmt_type == "array":
            for item in st.children:
                if item.stmt_type != "block":
                    continue
                cfg = self._ast_to_config_node(item, defines, origin="instance")
                pcm_name = cfg.get_scalar("name", "PCM")
                pcm_id = cfg.get_scalar("id", "0")
                cfg.key = f"Object.PCM.pcm ({pcm_name} #{pcm_id})"
                vm.root_config.children.append(cfg)

    def _instantiate_widget_node(
        self,
        wclass: str,
        winst: str,
        pipeline_id: Optional[int],
        pipeline_class: Optional[str],
        pipeline_direction: str,
        override_stmts: List[AstStmt],
        defines: Dict[str, str],
        source_file: str,
    ) -> GraphNode:
        cdef = self.processing_modules.get(wclass)
        cfg = cdef.config_tree.clone() if cdef else ConfigNode(f"Class.Widget.{wclass}", node_type="object")

        if pipeline_id is not None:
            cfg.set_scalar("index", str(pipeline_id), origin="pipeline", is_constructor=True)
        cfg.set_scalar("instance", str(winst), origin="pipeline", is_constructor=True)

        # Infer default direction from pipeline_direction for host-copier and dai-copier
        if wclass in ("host-copier", "dai-copier", "alh-dai-copier"):
            cfg.set_scalar("direction", pipeline_direction, origin="pipeline", is_constructor=True)

        # Apply override statements recursively
        self._apply_override_stmts_to_config(cfg, override_stmts, defines)

        # Compute canonical widget name using constructor attributes (matching alsatplg pre-process-dapm.c)
        node_id = self._compute_widget_id(wclass, winst, pipeline_id, cfg, cdef)

        # Determine input & output audio format badges (bottom-left = input, bottom-right = output)
        in_fmt_badge = self.summarize_pin_audio_format(cfg, "in")
        out_fmt_badge = self.summarize_pin_audio_format(cfg, "out")

        wtype = str(cfg.get_scalar("type", wclass))
        subtitle = f"{wclass} ({wtype})" if wtype != wclass else f"Widget.{wclass}"
        if pipeline_id is not None:
            subtitle = f"Pipe {pipeline_id} • {subtitle}"
        pcm_id = cfg.get_scalar("pcm_id")
        if pcm_id is not None and str(pcm_id) != "":
            subtitle += f" • PCM {pcm_id}"
        stream_name = cfg.get_scalar("stream_name")
        if stream_name:
            subtitle += f" ({stream_name})"

        badges: List[str] = []
        ctrl_node = cfg.get_child("Object.Control")
        if ctrl_node and ctrl_node.children:
            badges.append(f"{len(ctrl_node.children)} ctl")

        raw_in = cfg.get_scalar("num_input_pins")
        raw_out = cfg.get_scalar("num_output_pins")
        num_in = (
            int(str(raw_in))
            if raw_in is not None and str(raw_in).isdigit()
            else (0 if wclass == "siggen" else 1)
        )
        num_out = int(str(raw_out or 1)) if raw_out is not None and str(raw_out).isdigit() else 1
        in_ports = [GraphPort(f"in{i}", f"in.{i}", "in", "audio") for i in range(0 if wclass == "siggen" else max(1, num_in))]
        out_ports = [GraphPort(f"out{i}", f"out.{i}", "out", "audio") for i in range(max(1, num_out))]

        return GraphNode(
            id=node_id,
            title=node_id,
            subtitle=subtitle,
            category=self._map_category("Widget", wclass),
            class_category="Widget",
            class_name=wclass,
            instance_id=str(winst),
            pipeline_id=pipeline_id,
            pipeline_class=pipeline_class,
            in_ports=in_ports,
            out_ports=out_ports,
            in_fmt_badge=in_fmt_badge,
            out_fmt_badge=out_fmt_badge,
            badges=badges,
            config=cfg,
            class_def=cdef,
            source_file=source_file,
        )

    def _apply_override_stmts_to_config(
        self,
        cfg: ConfigNode,
        stmts: List[AstStmt],
        defines: Dict[str, str],
    ) -> None:
        in_fmt_keys = {"Object.Base.input_audio_format", "CombineArrays.Object.Base.input_audio_format"}
        out_fmt_keys = {"Object.Base.output_audio_format", "CombineArrays.Object.Base.output_audio_format"}
        search_dirs = [
            "include",
            "include/common",
            "include/components",
            "include/controls",
            "include/pipelines",
            "include/pipelines/cavs",
            "platform",
            "platform/intel",
        ]
        for st in stmts:
            if st.stmt_type == "kv":
                val = self._substitute_vars(str(st.value), defines)
                cfg.set_scalar(st.key, val, origin="instance")
            elif st.stmt_type == "block" and st.key.startswith("IncludeByKey."):
                parts = split_dotted_key(st.key)
                var_name = parts[1] if len(parts) > 1 else ""
                var_val = str(defines.get(var_name, ""))
                for ch in st.children:
                    if self._key_matches(strip_quotes(ch.key), var_val):
                        if ch.stmt_type == "kv" and ch.value:
                            inc_str = self._substitute_vars(str(ch.value), defines)
                            cfg.set_scalar("included_param_file", inc_str, origin="instance")
                            inc_path = self.resolve_include(inc_str, search_dirs)
                            if inc_path:
                                self._apply_override_stmts_to_config(cfg, self.get_ast(inc_path), defines)
                        elif ch.stmt_type == "block":
                            self._apply_override_stmts_to_config(cfg, ch.children, defines)
                        break
            elif st.stmt_type == "include":
                inc_path = self.resolve_include(st.key, search_dirs)
                if inc_path:
                    if cfg.category_info == "Control.bytes" or "components/" in st.key:
                        cfg.set_scalar("included_param_file", st.key, origin="instance")
                    self._apply_override_stmts_to_config(cfg, self.get_ast(inc_path), defines)
                else:
                    cfg.set_scalar("included_conf", st.key, origin="instance")
            elif st.stmt_type in ("block", "array") and st.key.startswith("Object.Control"):
                parts = split_dotted_key(st.key)
                ctrl_group = cfg.get_child("Object.Control")
                if ctrl_group is None:
                    ctrl_group = ConfigNode(key="Object.Control", node_type="object", category_info="controls")
                    cfg.children.append(ctrl_group)
                if len(parts) == 2:
                    for cch in st.children:
                        cparts = split_dotted_key(cch.key)
                        ctype = cparts[0]
                        cinst = strip_quotes(cparts[1]) if len(cparts) > 1 else "1"
                        ckey = f"{ctype}.{cinst}"
                        target_ctrl = ctrl_group.get_child(ckey)
                        if target_ctrl is None:
                            target_ctrl = ConfigNode(key=ckey, node_type="object", category_info=f"Control.{ctype}", origin="instance")
                            ctrl_group.children.append(target_ctrl)
                        self._apply_override_stmts_to_config(target_ctrl, cch.children, defines)
                elif len(parts) >= 3:
                    ctype = parts[2]
                    cinst = strip_quotes(parts[3]) if len(parts) > 3 else "1"
                    ckey = f"{ctype}.{cinst}"
                    target_ctrl = ctrl_group.get_child(ckey)
                    if target_ctrl is None:
                        target_ctrl = ConfigNode(key=ckey, node_type="object", category_info=f"Control.{ctype}", origin="instance")
                        ctrl_group.children.append(target_ctrl)
                    self._apply_override_stmts_to_config(target_ctrl, st.children, defines)
            elif st.stmt_type in ("block", "array"):
                # Replace or add compound child (e.g. Object.Base.input_audio_format, Object.Base.tlv)
                conv = self._ast_to_config_node(st, defines, origin="instance")
                if conv.key in in_fmt_keys:
                    cfg.children = [c for c in cfg.children if c.key not in in_fmt_keys]
                    cfg.children.append(conv)
                elif conv.key in out_fmt_keys:
                    cfg.children = [c for c in cfg.children if c.key not in out_fmt_keys]
                    cfg.children.append(conv)
                else:
                    existing_idx = next((i for i, c in enumerate(cfg.children) if c.key == conv.key), None)
                    if existing_idx is not None:
                        cfg.children[existing_idx] = conv
                    else:
                        cfg.children.append(conv)

    @staticmethod
    def _extract_kcontrol_enum_options(ctrl_cfg: ConfigNode) -> List[str]:
        """Extract enum option strings from Object.Base -> text.* -> !values inside a Control.enum ConfigNode."""
        opts: List[str] = []

        def walk(node: ConfigNode) -> None:
            if node.key in ("!values", "values") and node.children:
                for ch in node.children:
                    v = str(ch.value or ch.key or "").strip().strip('"').strip("'")
                    if v and v not in opts:
                        opts.append(v)
            for ch in node.children:
                walk(ch)

        walk(ctrl_cfg)
        return opts if opts else ["0", "1"]

    def attach_widget_kcontrol_nodes(self, vm: GraphViewModel, parent_node: GraphNode) -> None:
        """Instantiate child kcontrol GraphNodes below parent_node for each control in Object.Control."""
        default_bytes_files = {
            "eqiir": "include/components/eqiir/bandpass.conf",
            "eqfir": "include/components/eqfir/loudness.conf",
            "drc": "include/components/drc/enabled.conf",
            "dcblock": "include/components/dcblock/default.conf",
            "tdfb": "include/components/tdfb/default.conf",
            "multiband_drc": "include/components/multiband_drc/default.conf",
            "crossover": "include/components/crossover/default.conf",
            "aria": "include/components/aria/param_2.conf",
            "micsel": "include/components/micsel/passthrough.conf",
        }

        ctrl_group = parent_node.config.get_child("Object.Control")
        if parent_node.class_name in default_bytes_files:
            if ctrl_group is None:
                ctrl_group = ConfigNode(key="Object.Control", node_type="object", category_info="controls")
                parent_node.config.children.append(ctrl_group)
            if not any(c.key.startswith("bytes") for c in ctrl_group.children):
                def_bfile = default_bytes_files[parent_node.class_name]
                b_cfg = ConfigNode(key="bytes.1", node_type="object", category_info="Control.bytes", origin="default")
                if os.path.isfile(os.path.join(self.root_dir, def_bfile)):
                    b_cfg.set_scalar("included_param_file", def_bfile, origin="default")
                ctrl_group.children.insert(0, b_cfg)

        if not ctrl_group or not ctrl_group.children:
            return

        ctrl_cat_map = {
            "mixer": "control_mixer",
            "bytes": "control_bytes",
            "enum": "control_enum",
        }

        has_any_named = any(
            bool(str(c.get_scalar("name", "")).strip())
            for c in ctrl_group.children
        )

        for idx, ctrl_cfg in enumerate(ctrl_group.children):
            cparts = split_dotted_key(ctrl_cfg.key)
            ctype = cparts[0] if cparts else "mixer"
            cinst = strip_quotes(cparts[1]) if len(cparts) > 1 else str(idx + 1)
            node_cat = ctrl_cat_map.get(ctype, "control")

            kname = str(ctrl_cfg.get_scalar("name", "")).strip()
            if has_any_named and not kname and vm.view_kind not in ("module", "base_class"):
                # Skip unused class template control slots that were not instantiated/named in this pipeline
                continue
            if not has_any_named and not kname:
                # For newly added or standalone widgets, skip secondary uninstantiated mixer/enum slots in bench harness
                if vm.view_kind not in ("module", "base_class") and cinst != "1":
                    continue
                w_up = parent_node.class_name.upper()
                inst_suffix = f" {parent_node.instance_id}" if str(parent_node.instance_id) not in ("", "1") else ""
                if ctype == "mixer":
                    is_sw = str(ctrl_cfg.get_scalar("max", "")).strip() == "1"
                    ctl_kind = "switch" if is_sw else "volume"
                    kname = f"Analog Playback {w_up}{inst_suffix} {ctl_kind}"
                elif ctype == "bytes":
                    kname = f"Analog Playback {w_up}{inst_suffix} bytes"
                elif ctype == "enum":
                    kname = f"Analog Playback {w_up}{inst_suffix} enum"
                else:
                    kname = f"Analog Playback {w_up}{inst_suffix} {ctype}"
                ctrl_cfg.set_scalar("name", kname, origin="default")

            title = f"{ctype}.{cinst}: {kname}" if kname else f"{ctype}.{cinst}"

            badges: List[str] = []
            if ctype == "mixer":
                max_val = str(ctrl_cfg.get_scalar("max", "")).strip()
                is_switch = max_val == "1"
                if is_switch:
                    node_cat = "control_switch"
                    if ctrl_cfg.get_scalar("value") is None:
                        ctrl_cfg.set_scalar("value", "1", category_info="Switch state (0=Off, 1=On)", origin="default")
                else:
                    max_int = int(max_val) if max_val.isdigit() else 32
                    if ctrl_cfg.get_scalar("value") is None:
                        default_v = int(round(max_int * 0.78))
                        ctrl_cfg.set_scalar(
                            "value",
                            f"{default_v},{default_v}",
                            category_info=f"Volume level (0..{max_int})",
                            origin="default",
                        )
                subtitle = f"Control.mixer ({'Switch' if is_switch else 'Volume'})"
                if max_val:
                    badges.append(f"max {max_val}")
                if any(c.key.startswith("Object.Base.tlv") for c in ctrl_cfg.children):
                    badges.append("TLV")
            elif ctype == "bytes":
                subtitle = "Control.bytes (TLV/Blob)"
                pfile = str(ctrl_cfg.get_scalar("included_param_file", "")).strip()
                if not pfile and parent_node.class_name in default_bytes_files:
                    cand_rel = default_bytes_files[parent_node.class_name]
                    if os.path.isfile(os.path.join(self.root_dir, cand_rel)):
                        pfile = cand_rel
                        ctrl_cfg.set_scalar("included_param_file", pfile, origin="default")
                max_val = str(ctrl_cfg.get_scalar("max", "")).strip()
                if pfile:
                    badges.append(os.path.basename(pfile))
                if max_val:
                    badges.append(f"max {max_val}")
            elif ctype == "enum":
                subtitle = "Control.enum (Enum)"
                enum_opts = self._extract_kcontrol_enum_options(ctrl_cfg)
                if ctrl_cfg.get_scalar("enum_options") is None:
                    ctrl_cfg.set_scalar(
                        "enum_options",
                        ",".join(enum_opts),
                        category_info="Available enum choices",
                        origin="default",
                        is_immutable=True,
                    )
                if ctrl_cfg.get_scalar("value") is None:
                    default_opt = "0" if "0" in enum_opts else enum_opts[0]
                    ctrl_cfg.set_scalar(
                        "value",
                        default_opt,
                        category_info="Selected enum option",
                        origin="default",
                        valid_values=enum_opts,
                    )
                badges.append("enum")
            else:
                subtitle = f"Control.{ctype}"
                badges.append(ctype)

            if not badges:
                badges.append(ctype)

            cid = f"{parent_node.id}.ctl.{ctype}.{cinst}"
            cnode = GraphNode(
                id=cid,
                title=title,
                subtitle=subtitle,
                category=node_cat,
                class_category="Control",
                class_name=ctype,
                instance_id=cinst,
                pipeline_id=parent_node.pipeline_id,
                pipeline_class=parent_node.pipeline_class,
                parent_node_id=parent_node.id,
                in_ports=[],
                out_ports=[GraphPort("ctl_out", "ctl", "out", "control")],
                badges=badges,
                config=ctrl_cfg,
                class_def=self.base_classes.get(f"Control.{ctype}"),
                source_file=parent_node.source_file,
            )
            vm.nodes[cid] = cnode
            vm.edges.append(
                GraphEdge(
                    id=f"ctl_edge_{parent_node.id}_{ctype}_{cinst}",
                    source_node=cid,
                    sink_node=parent_node.id,
                    source_port="ctl_out",
                    sink_port="ctl_in",
                    control=ctype,
                    pipeline_id=parent_node.pipeline_id,
                )
            )

    def attach_host_copier_io_nodes(self, vm: GraphViewModel) -> None:
        """Attach left-hand Testbench I/O nodes to host-copiers and right-hand Testbench I/O nodes to dai/alh-copiers."""
        from collections import deque

        hc_nodes = sorted(
            [
                n for n in list(vm.nodes.values())
                if "host-copier" in n.class_name and n.class_category == "Widget"
            ],
            key=lambda n: (n.pipeline_id if n.pipeline_id is not None else 9999, n.id),
        )
        dc_nodes = sorted(
            [
                n for n in list(vm.nodes.values())
                if ("dai-copier" in n.class_name or "alh-copier" in n.class_name) and n.class_category == "Widget"
            ],
            key=lambda n: (n.pipeline_id if n.pipeline_id is not None else 9999, n.id),
        )
        if not hc_nodes and not dc_nodes:
            return

        def _is_copier_capture(node: GraphNode) -> bool:
            wdir = str(node.config.get_scalar("direction", "")).lower()
            wtype = str(node.config.get_scalar("type", "")).lower()
            return (
                "capture" in node.id.lower()
                or wdir == "capture"
                or wtype == "dai_out"
                or (
                    node.pipeline_id in vm.containers
                    and "capture" in vm.containers[node.pipeline_id].direction.lower()
                )
            )

        def _trace_pipeline_chain(start_node: GraphNode, walk_upstream: bool) -> List[int]:
            visited_nids: Set[str] = {start_node.id}
            chain_pids: List[int] = []
            if start_node.pipeline_id is not None:
                chain_pids.append(start_node.pipeline_id)
            start_is_cap = _is_copier_capture(start_node)
            dq = deque([start_node.id])
            while dq:
                cur_id = dq.popleft()
                for e in vm.edges:
                    if walk_upstream:
                        if e.sink_node != cur_id:
                            continue
                        nxt_id = e.source_node
                    else:
                        if e.source_node != cur_id:
                            continue
                        nxt_id = e.sink_node
                    nxt_node = vm.nodes.get(nxt_id)
                    if not nxt_node or nxt_node.class_category in ("Control", "TestbenchIO"):
                        continue
                    if nxt_node.pipeline_id is not None and nxt_node.pipeline_id in vm.containers:
                        nxt_dir = vm.containers[nxt_node.pipeline_id].direction.lower()
                        if start_is_cap and "playback" in nxt_dir and "capture" not in nxt_dir:
                            continue
                        if not start_is_cap and "capture" in nxt_dir and "playback" not in nxt_dir:
                            continue
                    if nxt_id not in visited_nids:
                        visited_nids.add(nxt_id)
                        if nxt_node.pipeline_id is not None and nxt_node.pipeline_id not in chain_pids:
                            chain_pids.append(nxt_node.pipeline_id)
                        dq.append(nxt_id)
            if walk_upstream and len(chain_pids) > 1:
                chain_pids = list(reversed(chain_pids))
            return chain_pids

        has_left_enabled = any(
            n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == "left"
            and str(n.config.get_scalar("enabled", "false")).lower() == "true"
            for n in vm.nodes.values()
        )
        enabled_chain_pids: Set[int] = set()

        # 1. Left-hand Testbench I/O nodes for every host-copier
        for hc_node in hc_nodes:
            tb_id = f"tb_io.{hc_node.id}"
            is_cap = _is_copier_capture(hc_node)
            chain_pids = _trace_pipeline_chain(hc_node, walk_upstream=is_cap)
            pipes_str = " → ".join(str(p) for p in chain_pids) if chain_pids else str(hc_node.pipeline_id or 1)
            if tb_id in vm.nodes:
                existing_tb = vm.nodes[tb_id]
                existing_tb.config.set_scalar(
                    "target_pipelines",
                    pipes_str,
                    category_info="Downstream/upstream pipelines enabled for testbench",
                    origin="default",
                    is_immutable=True,
                )
                existing_tb.badges = [f"Pipes: {pipes_str}"]
                if str(existing_tb.config.get_scalar("enabled", "false")).lower() == "true":
                    enabled_chain_pids.update(chain_pids)
                continue

            pcm_id = str(hc_node.config.get_scalar("pcm_id", hc_node.instance_id) or "0")
            is_enabled = not has_left_enabled
            if is_enabled:
                has_left_enabled = True
                enabled_chain_pids.update(chain_pids)

            tb_cfg = ConfigNode(
                key=f"Testbench.HostIO ({hc_node.id})",
                node_type="object",
                category_info="Testbench Host Endpoint",
            )
            tb_cfg.set_scalar(
                "enabled",
                "true" if is_enabled else "false",
                category_info="Radio: enable downstream/upstream pipelines in next testbench run",
                origin="instance",
                valid_values=["true", "false"],
            )
            tb_cfg.set_scalar(
                "side",
                "left",
                category_info="Graph placement side",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "role",
                "Capture Sink" if is_cap else "Playback Source",
                category_info="Endpoint direction",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "host_copier",
                hc_node.id,
                category_info="Connected Host Copier",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "pcm_id",
                pcm_id,
                category_info="Host PCM ID",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "target_pipelines",
                pipes_str,
                category_info="Downstream/upstream pipelines enabled for testbench",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "io_type",
                "file" if is_cap else "sig_gen",
                category_info=(
                    "Select Audio File or ALSA PCM"
                    if is_cap
                    else "Select Audio File, ALSA PCM, or Signal Generator"
                ),
                origin="instance",
                valid_values=["file", "alsa_pcm"] if is_cap else ["file", "alsa_pcm", "sig_gen"],
            )
            if not is_cap:
                tb_cfg.set_scalar(
                    "signal_type",
                    "sine_997",
                    category_info="Signal Generator waveform type (when io_type=sig_gen)",
                    origin="instance",
                    valid_values=[
                        "sine_997",
                        "sine_1000",
                        "sine_440",
                        "chirp",
                        "multitone",
                        "square",
                        "sawtooth",
                        "noise",
                        "pink_noise",
                        "impulse",
                        "remote_tone",
                    ],
                )
            tb_cfg.set_scalar(
                "file_path",
                f"capture_pcm{pcm_id}.wav" if is_cap else f"playback_pcm{pcm_id}.wav",
                category_info="Source/Sink audio file path",
                origin="instance",
            )
            tb_cfg.set_scalar(
                "alsa_pcm",
                "default",
                category_info="Source/Sink ALSA PCM device",
                origin="instance",
                valid_values=["default", "hw:0,0", "plughw:0,0", "hw:1,0", "plughw:1,0"],
            )

            tb_node = GraphNode(
                id=tb_id,
                title=f"PCM {pcm_id} {'Sink' if is_cap else 'Source'}",
                subtitle=f"{'Sink ←' if is_cap else 'Source →'} {hc_node.id}",
                category="testbench_io",
                class_category="TestbenchIO",
                class_name="testbench-sink" if is_cap else "testbench-source",
                instance_id=pcm_id,
                pipeline_id=None,
                parent_node_id=hc_node.id,
                in_ports=[GraphPort("in0", "in.0", "in", "host")] if is_cap else [],
                out_ports=[] if is_cap else [GraphPort("out0", "out.0", "out", "host")],
                badges=[f"Pipes: {pipes_str}"],
                config=tb_cfg,
                source_file=hc_node.source_file,
            )
            vm.nodes[tb_id] = tb_node

            edge_cfg = ConfigNode("Testbench.HostRoute", node_type="object")
            edge_cfg.set_scalar("source", hc_node.id if is_cap else tb_id, origin="instance")
            edge_cfg.set_scalar("sink", tb_id if is_cap else hc_node.id, origin="instance")
            vm.edges.append(
                GraphEdge(
                    id=f"tb_edge_{hc_node.id}",
                    source_node=hc_node.id if is_cap else tb_id,
                    sink_node=tb_id if is_cap else hc_node.id,
                    source_port="out0",
                    sink_port="in0",
                    is_inter_pipeline=True,
                    config=edge_cfg,
                )
            )

        # 2. Right-hand Testbench I/O nodes for every dai-copier and alh-copier
        has_right_enabled = any(
            n.class_category == "TestbenchIO"
            and str(n.config.get_scalar("side", "left")).lower() == "right"
            and str(n.config.get_scalar("enabled", "false")).lower() == "true"
            for n in vm.nodes.values()
        )

        for dc_node in dc_nodes:
            tb_id = f"tb_io.{dc_node.id}"
            is_cap = _is_copier_capture(dc_node)
            # For DAI/ALH copiers on the right:
            # - Playback DAI/ALH is a Sink at the end of an upstream playback chain (walk_upstream=True)
            # - Capture DAI/ALH is a Source at the start of a downstream capture chain (walk_upstream=False)
            chain_pids = _trace_pipeline_chain(dc_node, walk_upstream=not is_cap)
            pipes_str = " → ".join(str(p) for p in chain_pids) if chain_pids else str(dc_node.pipeline_id or 1)
            if tb_id in vm.nodes:
                existing_tb = vm.nodes[tb_id]
                existing_tb.config.set_scalar(
                    "target_pipelines",
                    pipes_str,
                    category_info="Downstream/upstream pipelines enabled for testbench",
                    origin="default",
                    is_immutable=True,
                )
                existing_tb.badges = [f"Pipes: {pipes_str}"]
                continue

            parts = dc_node.id.split(".")
            if len(parts) >= 3 and parts[0] == "dai-copier":
                if parts[2].upper().startswith(parts[1].upper()):
                    dai_label = parts[2]
                else:
                    dai_label = f"{parts[1]}.{parts[2]}"
            elif len(parts) >= 2:
                dai_label = parts[1]
            else:
                dai_label = dc_node.instance_id or "DAI"
            safe_dai = re.sub(r"[^a-zA-Z0-9_-]+", "_", dai_label).strip("_").lower() or "dai"

            if not has_right_enabled:
                if enabled_chain_pids:
                    is_enabled = (dc_node.pipeline_id in enabled_chain_pids) or bool(set(chain_pids) & enabled_chain_pids)
                else:
                    is_enabled = True
                if is_enabled:
                    has_right_enabled = True
            else:
                is_enabled = False

            tb_cfg = ConfigNode(
                key=f"Testbench.DaiIO ({dc_node.id})",
                node_type="object",
                category_info="Testbench DAI/ALH Endpoint",
            )
            tb_cfg.set_scalar(
                "enabled",
                "true" if is_enabled else "false",
                category_info="Radio: enable downstream/upstream pipelines in next testbench run",
                origin="instance",
                valid_values=["true", "false"],
            )
            tb_cfg.set_scalar(
                "side",
                "right",
                category_info="Graph placement side",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "role",
                "Capture Source" if is_cap else "Playback Sink",
                category_info="Endpoint direction",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "dai_copier",
                dc_node.id,
                category_info="Connected DAI/ALH Copier",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "dai_name",
                dai_label,
                category_info="DAI/ALH Stream Identifier",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "target_pipelines",
                pipes_str,
                category_info="Downstream/upstream pipelines enabled for testbench",
                origin="default",
                is_immutable=True,
            )
            tb_cfg.set_scalar(
                "io_type",
                "sig_gen" if is_cap else "file",
                category_info=(
                    "Select Audio File, ALSA PCM, or Signal Generator"
                    if is_cap
                    else "Select Audio File or ALSA PCM"
                ),
                origin="instance",
                valid_values=["file", "alsa_pcm", "sig_gen"] if is_cap else ["file", "alsa_pcm"],
            )
            if is_cap:
                tb_cfg.set_scalar(
                    "signal_type",
                    "sine_997",
                    category_info="Signal Generator waveform type (when io_type=sig_gen)",
                    origin="instance",
                    valid_values=[
                        "sine_997",
                        "sine_1000",
                        "sine_440",
                        "chirp",
                        "multitone",
                        "square",
                        "sawtooth",
                        "noise",
                        "pink_noise",
                        "impulse",
                        "remote_tone",
                    ],
                )
            tb_cfg.set_scalar(
                "file_path",
                f"capture_{safe_dai}.wav" if is_cap else f"playback_{safe_dai}.wav",
                category_info="Source/Sink audio file path",
                origin="instance",
            )
            tb_cfg.set_scalar(
                "alsa_pcm",
                "default",
                category_info="Source/Sink ALSA PCM device",
                origin="instance",
                valid_values=["default", "hw:0,0", "plughw:0,0", "hw:1,0", "plughw:1,0"],
            )

            tb_node = GraphNode(
                id=tb_id,
                title=f"{dai_label} {'Source' if is_cap else 'Sink'}",
                subtitle=f"{'← Source' if is_cap else '→ Sink'} {dc_node.id}",
                category="testbench_io",
                class_category="TestbenchIO",
                class_name="testbench-source" if is_cap else "testbench-sink",
                instance_id=dai_label,
                pipeline_id=None,
                parent_node_id=dc_node.id,
                in_ports=[] if is_cap else [GraphPort("in0", "in.0", "in", "dai")],
                out_ports=[GraphPort("out0", "out.0", "out", "dai")] if is_cap else [],
                badges=[f"Pipes: {pipes_str}"],
                config=tb_cfg,
                source_file=dc_node.source_file,
            )
            vm.nodes[tb_id] = tb_node

            edge_cfg = ConfigNode("Testbench.DaiRoute", node_type="object")
            edge_cfg.set_scalar("source", tb_id if is_cap else dc_node.id, origin="instance")
            edge_cfg.set_scalar("sink", dc_node.id if is_cap else tb_id, origin="instance")
            vm.edges.append(
                GraphEdge(
                    id=f"tb_edge_{dc_node.id}",
                    source_node=tb_id if is_cap else dc_node.id,
                    sink_node=dc_node.id if is_cap else tb_id,
                    source_port="out0",
                    sink_port="in0",
                    is_inter_pipeline=True,
                    config=edge_cfg,
                )
            )

    def _compute_widget_id(
        self,
        wclass: str,
        winst: str,
        pipeline_id: Optional[int],
        cfg: ConfigNode,
        cdef: Optional[TplgClassDef],
    ) -> str:
        explicit_name = str(cfg.get_scalar("name", "")).strip()
        if explicit_name:
            return explicit_name
        inst_val = str(cfg.get_scalar("instance", winst)).strip() or str(winst)
        if cdef and cdef.constructor_attrs:
            parts = [wclass]
            used_pid = "index" in cdef.constructor_attrs
            for attr in cdef.constructor_attrs:
                if attr == "index" and pipeline_id is not None:
                    parts.append(str(pipeline_id))
                elif attr == "instance":
                    parts.append(inst_val)
                else:
                    val = cfg.get_scalar(attr, "")
                    if val != "":
                        parts.append(str(val))
                    elif not used_pid and pipeline_id is not None:
                        parts.append(str(pipeline_id))
                        used_pid = True
                    else:
                        parts.append(inst_val)
            return ".".join(parts)
        idx_s = str(pipeline_id) if pipeline_id is not None else "0"
        return f"{wclass}.{idx_s}.{inst_val}"

    @staticmethod
    def summarize_pin_audio_format(cfg: ConfigNode, direction: str) -> str:
        """Summarize min/max rates, channels, and sample format for 'in' or 'out' audio_format."""
        prefix = "in" if direction == "in" else "out"
        target_keys = (
            ("Object.Base.input_audio_format", "CombineArrays.Object.Base.input_audio_format")
            if direction == "in"
            else ("Object.Base.output_audio_format", "CombineArrays.Object.Base.output_audio_format")
        )
        fnodes = [c for c in cfg.children if c.key in target_keys and c.children]
        if not fnodes:
            return ""

        def extract_ints(entry: ConfigNode, field_name: str, default_val: int) -> List[int]:
            ch = entry.get_child(field_name)
            if ch is None:
                return [default_val]
            if ch.node_type == "scalar":
                s = str(ch.value).strip()
                return [int(s)] if s.isdigit() else [default_val]
            vals: List[int] = []
            for item in ch.children:
                cand = str(item.key if item.key else item.value).strip()
                if cand.isdigit():
                    vals.append(int(cand))
            return vals if vals else [default_val]

        def fmt_khz(hz: int) -> str:
            if hz % 1000 == 0:
                return f"{hz // 1000}k"
            return f"{hz / 1000:.1f}k".rstrip("0").rstrip(".")

        rates: List[int] = []
        chans: List[int] = []
        depths: List[Tuple[int, int, str]] = []

        for fnode in fnodes:
            for entry in fnode.children:
                rates.extend(extract_ints(entry, f"{prefix}_rate", 48000))
                chans.extend(extract_ints(entry, f"{prefix}_channels", 2))
                b_list = extract_ints(entry, f"{prefix}_bit_depth", 16)
                has_vb = entry.get_child(f"{prefix}_valid_bit_depth") is not None
                vb_list = extract_ints(entry, f"{prefix}_valid_bit_depth", b_list[0]) if has_vb else list(b_list)
                st_str = str(entry.get_scalar(f"{prefix}_sample_type", "")).upper()
                sp = "f" if "FLOAT" in st_str else ("u" if "UNSIGNED" in st_str else "s")
                for b in b_list:
                    for vb in vb_list:
                        depths.append((vb, b, sp))

        if not rates or not chans or not depths:
            return ""

        unique_depths = sorted(set(depths), key=lambda t: (t[0], t[1]))
        if len(unique_depths) == 1:
            vb, b, sp = unique_depths[0]
            fmt_str = f"{sp}{vb}/{b}" if vb != b else f"{sp}{vb}"
        else:
            min_vb, _, min_sp = unique_depths[0]
            max_vb, _, max_sp = unique_depths[-1]
            fmt_str = f"{min_sp}{min_vb}-{max_sp}{max_vb}" if (min_vb != max_vb or min_sp != max_sp) else f"{min_sp}{min_vb}"

        min_r, max_r = min(rates), max(rates)
        rate_str = fmt_khz(min_r) if min_r == max_r else f"{fmt_khz(min_r)}-{fmt_khz(max_r)}"

        min_c, max_c = min(chans), max(chans)
        ch_str = f"{min_c}ch" if min_c == max_c else f"{min_c}-{max_c}ch"

        return f"{fmt_str} {rate_str} {ch_str}"

    @staticmethod
    def extract_pin_format_details(
        cfg: ConfigNode,
        direction: str,
        pin_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Extract detailed formats, rates, channels, and interleaving style for a module input or output pin."""
        prefix = "in" if direction == "in" else "out"
        pin_key = "input_pin_index" if direction == "in" else "output_pin_index"
        target_keys = (
            ("Object.Base.input_audio_format", "CombineArrays.Object.Base.input_audio_format")
            if direction == "in"
            else ("Object.Base.output_audio_format", "CombineArrays.Object.Base.output_audio_format")
        )
        fnodes = [c for c in cfg.children if c.key in target_keys and c.children]
        all_entries: List[ConfigNode] = []
        for fnode in fnodes:
            for ch in fnode.children:
                all_entries.append(ch)

        matched_entries: List[ConfigNode] = []
        if pin_index is not None and all_entries:
            for e in all_entries:
                p_raw = str(e.get_scalar(pin_key, "0")).strip()
                if p_raw.isdigit() and int(p_raw) == pin_index:
                    matched_entries.append(e)
        if not matched_entries:
            matched_entries = list(all_entries)

        def extract_ints(entry: ConfigNode, field_name: str, default_val: int) -> List[int]:
            ch = entry.get_child(field_name)
            if ch is None:
                return [default_val]
            if ch.node_type == "scalar":
                s = str(ch.value).strip()
                return [int(s)] if s.isdigit() else [default_val]
            vals: List[int] = []
            for item in ch.children:
                cand = str(item.key if item.key else item.value).strip()
                if cand.isdigit():
                    vals.append(int(cand))
            return vals if vals else [default_val]

        def fmt_khz(hz: int) -> str:
            if hz % 1000 == 0:
                return f"{hz // 1000}k"
            return f"{hz / 1000:.1f}k".rstrip("0").rstrip(".")

        rates: List[int] = []
        chans: List[int] = []
        depths: List[Tuple[int, int, str]] = []
        styles: List[str] = []
        sample_types: List[str] = []

        for entry in matched_entries:
            rates.extend(extract_ints(entry, f"{prefix}_rate", 48000))
            chans.extend(extract_ints(entry, f"{prefix}_channels", 2))
            b_list = extract_ints(entry, f"{prefix}_bit_depth", 16)
            has_vb = entry.get_child(f"{prefix}_valid_bit_depth") is not None
            vb_list = extract_ints(entry, f"{prefix}_valid_bit_depth", b_list[0]) if has_vb else list(b_list)
            st_raw = str(entry.get_scalar(f"{prefix}_sample_type", "LSB_INTEGER")).strip()
            st_up = st_raw.upper()
            sp = "f" if "FLOAT" in st_up else ("u" if "UNSIGNED" in st_up else "s")
            for b in b_list:
                for vb in vb_list:
                    depths.append((vb, b, sp))
            il_raw = str(entry.get_scalar(f"{prefix}_interleaving_style", "interleaved")).strip().strip('"')
            if il_raw and il_raw not in styles:
                styles.append(il_raw)
            st_clean = st_raw.replace("$SAMPLE_TYPE_", "").replace("_INTEGER", "_INT").strip()
            if st_clean and st_clean not in sample_types:
                sample_types.append(st_clean)

        if not rates:
            rates = [48000]
        if not chans:
            chans = [2]
        if not depths:
            depths = [(16, 16, "s"), (24, 32, "s"), (32, 32, "s")]
        if not styles:
            styles = ["interleaved"]
        if not sample_types:
            sample_types = ["LSB_INT"]

        unique_depths = sorted(set(depths), key=lambda t: (t[0], t[1]))
        fmt_tokens = [
            f"{sp}{vb}/{b}" if vb != b else f"{sp}{vb}"
            for vb, b, sp in unique_depths
        ]
        if len(fmt_tokens) == 1:
            vb, b, _ = unique_depths[0]
            formats_line = f"{fmt_tokens[0]} ({vb}-bit)" if vb == b else f"{fmt_tokens[0]} ({vb}-in-{b}b)"
        elif len(fmt_tokens) <= 4:
            formats_line = ", ".join(fmt_tokens)
        else:
            formats_line = f"{fmt_tokens[0]} .. {fmt_tokens[-1]}"

        unique_rates = sorted(set(rates))
        if len(unique_rates) == 1:
            r0 = unique_rates[0]
            rates_line = f"{r0 // 1000} kHz" if r0 % 1000 == 0 else f"{fmt_khz(r0)}Hz"
        elif len(unique_rates) <= 3:
            rates_line = ", ".join(fmt_khz(r) for r in unique_rates) + "Hz"
        else:
            rates_line = f"{fmt_khz(unique_rates[0])} – {fmt_khz(unique_rates[-1])}Hz ({len(unique_rates)})"

        unique_chans = sorted(set(chans))
        if len(unique_chans) == 1:
            c0 = unique_chans[0]
            ch_part = f"{c0} ch"
        elif len(unique_chans) <= 3:
            ch_part = ", ".join(str(c) for c in unique_chans) + " ch"
        else:
            ch_part = f"{unique_chans[0]}–{unique_chans[-1]} ch"

        style_part = styles[0] if len(styles) == 1 else "/".join(styles)
        channels_line = f"{ch_part} • {style_part}"

        return {
            "formats_line": formats_line,
            "rates_line": rates_line,
            "channels_line": channels_line,
            "rates_all": ", ".join(f"{r} Hz" for r in unique_rates),
            "channels_all": ", ".join(str(c) for c in unique_chans),
            "formats_all": ", ".join(fmt_tokens),
            "interleaving": ", ".join(styles),
            "sample_types": ", ".join(sample_types),
            "entries": matched_entries,
        }

    def attach_module_pin_format_nodes(
        self,
        vm: GraphViewModel,
        main_node: GraphNode,
        attach_in: bool = True,
        attach_out: bool = True,
        is_capture: bool = False,
    ) -> None:
        """Attach Input Pin Format node(s) and Output Pin Format node(s) to main_node."""
        num_in = len(main_node.in_ports) if attach_in else 0
        num_out = len(main_node.out_ports) if attach_out else 0

        def build_pin_node(
            direction: str,
            pin_indices: List[int],
        ) -> GraphNode:
            is_in = direction == "in"
            if is_capture:
                side = "right" if is_in else "left"
            else:
                side = "left" if is_in else "right"
            arrow = "◂" if is_capture else "▸"
            p0 = pin_indices[0]
            details = self.extract_pin_format_details(
                main_node.config,
                direction,
                pin_index=p0 if len(pin_indices) == 1 else None,
            )
            if len(pin_indices) == 1:
                pin_tag = f"{'in' if is_in else 'out'}.{p0}"
                node_id = f"{main_node.id}.pin_{direction}.{p0}"
                title = f"{arrow} {pin_tag} {'accepts' if is_in else 'outputs'}:"
            else:
                p_last = pin_indices[-1]
                pin_tag = f"{'in' if is_in else 'out'}.{p0}..{p_last}"
                node_id = f"{main_node.id}.pin_{direction}.all"
                title = f"{arrow} {pin_tag} ({len(pin_indices)} pins):"

            cfg_key = (
                f"Object.Base.input_audio_format ({pin_tag})"
                if is_in
                else f"Object.Base.output_audio_format ({pin_tag})"
            )
            pin_cfg = ConfigNode(
                key=cfg_key,
                node_type="object",
                category_info="Input Pin Audio Format" if is_in else "Output Pin Audio Format",
                origin="default",
            )
            pin_cfg.set_scalar("side", side, category_info="Graph placement side", origin="default", is_immutable=True)
            pin_cfg.set_scalar(
                "direction",
                "input" if is_in else "output",
                category_info="Pin direction",
                origin="default",
                is_immutable=True,
            )
            pin_cfg.set_scalar(
                "pin_index",
                str(p0) if len(pin_indices) == 1 else f"{p0}..{pin_indices[-1]}",
                category_info="Pin index on module",
                origin="default",
                is_immutable=True,
            )
            pin_cfg.set_scalar(
                "formats_line",
                str(details["formats_line"]),
                category_info="Accepted sample formats / bit depths",
                origin="default",
            )
            pin_cfg.set_scalar(
                "rates_line",
                str(details["rates_line"]),
                category_info="Accepted sample rates",
                origin="default",
            )
            pin_cfg.set_scalar(
                "channels_line",
                str(details["channels_line"]),
                category_info="Accepted channel counts & interleaving",
                origin="default",
            )
            pin_cfg.set_scalar(
                "accepted_formats",
                str(details["formats_all"]),
                category_info="Supported PCM formats",
                origin="default",
            )
            pin_cfg.set_scalar(
                "accepted_rates",
                str(details["rates_all"]),
                category_info="Supported sampling rates (Hz)",
                origin="default",
            )
            pin_cfg.set_scalar(
                "accepted_channels",
                str(details["channels_all"]),
                category_info="Supported channel counts",
                origin="default",
            )
            pin_cfg.set_scalar(
                "interleaving_style",
                str(details["interleaving"]),
                category_info="Channel interleaving style",
                origin="default",
            )
            pin_cfg.set_scalar(
                "sample_type",
                str(details["sample_types"]),
                category_info="Sample encoding type",
                origin="default",
            )
            entries = details.get("entries") or []
            if entries:
                fmt_group = ConfigNode(
                    key="audio_format_configs",
                    node_type="array",
                    category_info=f"{len(entries)} format configs",
                    origin="default",
                )
                for idx, ent in enumerate(entries):
                    ec = ent.clone()
                    ec.key = f"[{idx}]"
                    fmt_group.children.append(ec)
                pin_cfg.children.append(fmt_group)

            if is_in:
                in_ports: List[GraphPort] = []
                out_ports = [
                    GraphPort(f"out{i}", f"in.{p}", "out", "audio")
                    for i, p in enumerate(pin_indices)
                ]
            else:
                in_ports = [
                    GraphPort(f"in{i}", f"out.{p}", "in", "audio")
                    for i, p in enumerate(pin_indices)
                ]
                out_ports = []

            return GraphNode(
                id=node_id,
                title=title,
                subtitle=f"Fmt: {details['formats_line']}",
                category="pin_fmt_in" if is_in else "pin_fmt_out",
                class_category="PinFormat",
                class_name="input_audio_format" if is_in else "output_audio_format",
                instance_id=str(p0),
                pipeline_id=None,
                parent_node_id=main_node.id,
                in_ports=in_ports,
                out_ports=out_ports,
                in_fmt_badge=f"Rate: {details['rates_line']}",
                out_fmt_badge=f"Ch: {details['channels_line']}",
                badges=[
                    f"Fmt: {details['formats_line']}",
                    f"Rate: {details['rates_line']}",
                    f"Ch: {details['channels_line']}",
                ],
                config=pin_cfg,
                class_def=self.base_classes.get(
                    "Base.input_audio_format" if is_in else "Base.output_audio_format"
                ),
                source_file=main_node.source_file,
            )

        # 1. Left-hand Input Pin Format node(s) connected to main_node's input pin(s)
        if num_in > 0:
            if num_in <= 2:
                for p_idx in range(num_in):
                    pnode = build_pin_node("in", [p_idx])
                    vm.nodes[pnode.id] = pnode
                    vm.edges.append(
                        GraphEdge(
                            id=f"pin_in_edge_{main_node.id}_{p_idx}",
                            source_node=pnode.id,
                            sink_node=main_node.id,
                            source_port="out0",
                            sink_port=f"in{p_idx}",
                        )
                    )
            else:
                pnode = build_pin_node("in", list(range(num_in)))
                vm.nodes[pnode.id] = pnode
                for p_idx in range(num_in):
                    vm.edges.append(
                        GraphEdge(
                            id=f"pin_in_edge_{main_node.id}_{p_idx}",
                            source_node=pnode.id,
                            sink_node=main_node.id,
                            source_port=f"out{p_idx}",
                            sink_port=f"in{p_idx}",
                        )
                    )

        # 2. Right-hand Output Pin Format node(s) connected from main_node's output pin(s)
        if num_out > 0:
            if num_out <= 2:
                for p_idx in range(num_out):
                    pnode = build_pin_node("out", [p_idx])
                    vm.nodes[pnode.id] = pnode
                    vm.edges.append(
                        GraphEdge(
                            id=f"pin_out_edge_{main_node.id}_{p_idx}",
                            source_node=main_node.id,
                            sink_node=pnode.id,
                            source_port=f"out{p_idx}",
                            sink_port="in0",
                        )
                    )
            else:
                pnode = build_pin_node("out", list(range(num_out)))
                vm.nodes[pnode.id] = pnode
                for p_idx in range(num_out):
                    vm.edges.append(
                        GraphEdge(
                            id=f"pin_out_edge_{main_node.id}_{p_idx}",
                            source_node=main_node.id,
                            sink_node=pnode.id,
                            source_port=f"out{p_idx}",
                            sink_port=f"in{p_idx}",
                        )
                    )

    def attach_pipeline_pin_format_nodes(self, vm: GraphViewModel, is_capture: bool = False) -> None:
        """Attach Input/Output Pin Capability Callouts to the head and tail endpoint widgets of a pipeline."""
        wnodes = [n for n in vm.nodes.values() if n.class_category == "Widget"]
        if not wnodes:
            return
        wids = {n.id for n in wnodes}
        in_deg: Dict[str, int] = {n.id: 0 for n in wnodes}
        out_deg: Dict[str, int] = {n.id: 0 for n in wnodes}
        for e in vm.edges:
            if e.source_node in wids and e.sink_node in wids and e.source_node != e.sink_node:
                out_deg[e.source_node] += 1
                in_deg[e.sink_node] += 1

        tb_sides_by_parent: Set[Tuple[str, str]] = set()
        for n in vm.nodes.values():
            if n.class_category == "TestbenchIO" and n.parent_node_id:
                s = str(n.config.get_scalar("side", "left")).lower()
                tb_sides_by_parent.add((n.parent_node_id, s))

        in_side = "right" if is_capture else "left"
        out_side = "left" if is_capture else "right"

        head_nodes = [
            n for n in wnodes
            if in_deg[n.id] == 0 and n.class_name != "siggen" and len(n.in_ports) > 0
        ]
        tail_nodes = [
            n for n in wnodes
            if out_deg[n.id] == 0 and n.class_name != "wov" and len(n.out_ports) > 0
        ]

        for hnode in head_nodes:
            if (hnode.id, in_side) not in tb_sides_by_parent:
                self.attach_module_pin_format_nodes(
                    vm,
                    hnode,
                    attach_in=True,
                    attach_out=False,
                    is_capture=is_capture,
                )

        for tnode in tail_nodes:
            if (tnode.id, out_side) not in tb_sides_by_parent:
                self.attach_module_pin_format_nodes(
                    vm,
                    tnode,
                    attach_in=False,
                    attach_out=True,
                    is_capture=is_capture,
                )

    @staticmethod
    def _match_widget_id(
        raw_ref: str,
        nodes: Dict[str, GraphNode],
        default_pipeline_id: Optional[int],
    ) -> Optional[str]:
        ref = strip_quotes(raw_ref.strip())
        if not ref:
            return None
        if ref in nodes:
            return ref
        # Handle double-dot shorthand like "gain..1" where pipeline index is omitted
        if ".." in ref and default_pipeline_id is not None:
            expanded = ref.replace("..", f".{default_pipeline_id}.", 1)
            if expanded in nodes:
                return expanded
        # Handle prefix / case-insensitive / partial matches for dai-copier or host-copier
        parts = ref.split(".")
        if len(parts) == 4 and parts[0].lower() == "copier":
            target_kind = parts[1].lower()
            target_pid = parts[2]
            target_inst = parts[3]
            for nid, n in nodes.items():
                if str(n.pipeline_id) == target_pid and n.instance_id == target_inst:
                    if target_kind == "host" and n.class_name == "host-copier":
                        return nid
                    if target_kind in ("dai", "alh") and n.class_name in ("dai-copier", "alh-copier"):
                        return nid
        if len(parts) >= 2:
            wclass = parts[0]
            # Try matching by class_name + pipeline_id + instance
            for nid, n in nodes.items():
                if n.class_name == wclass:
                    if nid.lower() == ref.lower():
                        return nid
                    if len(parts) == 3 and str(n.pipeline_id) == parts[1] and n.instance_id == parts[2]:
                        return nid
        return None

    @staticmethod
    def _map_category(class_cat: str, class_name: str) -> str:
        if class_cat == "PCM":
            return "pcm"
        if class_cat == "Dai":
            return "dai"
        if class_cat == "Control":
            return "control"
        if class_cat == "Base":
            return "base"
        if class_cat == "Pipeline":
            return "pipeline"
        # Widget subcategories for distinct color coding
        if "host-copier" in class_name or "host" in class_name:
            return "host"
        if "dai-copier" in class_name or "dai" in class_name:
            return "dai"
        if class_name in ("mixin", "mixout", "module-copier"):
            return "mix"
        return "widget"

    # -------------------------------------------------------------------------
    # Factory Builders for File -> New (Module / Feature / Function / Monolithic)
    # and File -> Open Arbitrary .conf File
    # -------------------------------------------------------------------------

    def create_new_module_graph(self, name: str = "custom-module") -> GraphViewModel:
        """Create a new Processing Module (Class.Widget.<name>) GraphViewModel."""
        if "gain" in self.processing_modules:
            vm = self.build_graph_for_module("gain", include_testbench_io=True)
        elif self.processing_modules:
            first_m = sorted(self.processing_modules.keys())[0]
            vm = self.build_graph_for_module(first_m, include_testbench_io=True)
        else:
            vm = GraphViewModel(title=f"Class.Widget.{name}", view_kind="module")
        vm.title = f"Class.Widget.{name}"
        vm.subtitle = "New Processing Module (include/components/)"
        vm.view_kind = "module"
        vm.source_file = ""
        vm.dirty = True
        renamed_nodes: Dict[str, GraphNode] = {}
        old_w_id = ""
        new_w_id = f"{name}.1.1"
        for nid, node in vm.nodes.items():
            if node.class_category == "Widget":
                old_w_id = nid
                node.id = new_w_id
                node.class_name = name
                node.title = name
                node.subtitle = f"Class.Widget.{name} (effect)"
                renamed_nodes[new_w_id] = node
            else:
                new_cid = nid.replace("gain.1.1", new_w_id)
                node.id = new_cid
                node.parent_node_id = new_w_id
                renamed_nodes[new_cid] = node
        if old_w_id:
            vm.nodes = renamed_nodes
            for edge in vm.edges:
                edge.source_node = edge.source_node.replace(old_w_id, new_w_id)
                edge.sink_node = edge.sink_node.replace(old_w_id, new_w_id)
        return vm

    def create_new_feature_topology_graph(self, name: str = "custom-feature-pipeline") -> GraphViewModel:
        """Create a new Feature Topology (Class.Pipeline.<name>) GraphViewModel."""
        vm = GraphViewModel(
            title=f"Class.Pipeline.{name}",
            subtitle="New Feature Topology Pipeline (include/pipelines/cavs/)",
            view_kind="pipeline",
            source_file="",
            dirty=True,
        )
        defines = dict(self.global_defines)
        c1 = PipelineContainer(
            pipeline_id=1,
            class_name=name,
            direction="playback",
        )
        c1.config.set_scalar("index", "1", origin="pipeline")
        c1.config.set_scalar("direction", "playback", origin="pipeline")
        vm.containers[1] = c1

        hc = self._instantiate_widget_node(
            wclass="host-copier",
            winst="0.playback",
            pipeline_id=1,
            pipeline_class=name,
            pipeline_direction="playback",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        gn = self._instantiate_widget_node(
            wclass="gain",
            winst="1",
            pipeline_id=1,
            pipeline_class=name,
            pipeline_direction="playback",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        mc = self._instantiate_widget_node(
            wclass="module-copier",
            winst="2",
            pipeline_id=1,
            pipeline_class=name,
            pipeline_direction="playback",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        for wnode in (hc, gn, mc):
            vm.nodes[wnode.id] = wnode
            self.attach_widget_kcontrol_nodes(vm, wnode)

        vm.edges.append(GraphEdge(id="edge_feat_0", source_node=hc.id, sink_node=gn.id, pipeline_id=1))
        vm.edges.append(GraphEdge(id="edge_feat_1", source_node=gn.id, sink_node=mc.id, pipeline_id=1))
        self.attach_host_copier_io_nodes(vm)
        return vm

    def create_new_function_topology_graph(self, name: str = "custom-function-topology") -> GraphViewModel:
        """Create a new Function Topology (reusable multi-pipeline FE -> BE function block) GraphViewModel."""
        vm = GraphViewModel(
            title=name,
            subtitle="New Function Topology (FE + BE Pipeline Block)",
            view_kind="function_topology",
            source_file="",
            dirty=True,
        )
        defines = dict(self.global_defines)
        # Pipeline 1: FE host-gateway-playback (host-copier -> gain -> module-copier)
        c1 = PipelineContainer(
            pipeline_id=1,
            class_name="volume-playback",
            direction="playback",
        )
        c1.config.set_scalar("index", "1", origin="pipeline")
        c1.config.set_scalar("direction", "playback", origin="pipeline")
        vm.containers[1] = c1

        # Pipeline 2: BE io-gateway (dai-copier)
        c2 = PipelineContainer(
            pipeline_id=2,
            class_name="io-gateway",
            direction="playback",
        )
        c2.config.set_scalar("index", "2", origin="pipeline")
        c2.config.set_scalar("direction", "playback", origin="pipeline")
        vm.containers[2] = c2

        hc = self._instantiate_widget_node(
            wclass="host-copier",
            winst="0.playback",
            pipeline_id=1,
            pipeline_class="volume-playback",
            pipeline_direction="playback",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        gn = self._instantiate_widget_node(
            wclass="gain",
            winst="1",
            pipeline_id=1,
            pipeline_class="volume-playback",
            pipeline_direction="playback",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        dc = self._instantiate_widget_node(
            wclass="dai-copier",
            winst="HDA.Analog.playback",
            pipeline_id=2,
            pipeline_class="io-gateway",
            pipeline_direction="playback",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        for wnode in (hc, gn, dc):
            vm.nodes[wnode.id] = wnode
            self.attach_widget_kcontrol_nodes(vm, wnode)

        vm.edges.append(GraphEdge(id="edge_fn_0", source_node=hc.id, sink_node=gn.id, pipeline_id=1))
        vm.edges.append(
            GraphEdge(
                id="edge_fn_1",
                source_node=gn.id,
                sink_node=dc.id,
                pipeline_id=1,
                is_inter_pipeline=True,
            )
        )
        self.attach_host_copier_io_nodes(vm)
        return vm

    def create_new_monolithic_topology_graph(self, name: str = "sof-custom-generic") -> GraphViewModel:
        """Create a new Monolithic Machine Topology (sof-*.conf) GraphViewModel."""
        vm = self.create_new_function_topology_graph(name=name)
        vm.title = name
        vm.subtitle = "New Monolithic Machine Topology (sof-*.conf)"
        vm.view_kind = "topology"
        vm.source_file = ""
        vm.dirty = True

        defines = dict(self.global_defines)
        # Add Capture pipelines 3 (FE) and 4 (BE) for a complete duplex machine topology
        c3 = PipelineContainer(
            pipeline_id=3,
            class_name="host-gateway-capture",
            direction="capture",
        )
        c3.config.set_scalar("index", "3", origin="pipeline")
        c3.config.set_scalar("direction", "capture", origin="pipeline")
        vm.containers[3] = c3

        c4 = PipelineContainer(
            pipeline_id=4,
            class_name="io-gateway-capture",
            direction="capture",
        )
        c4.config.set_scalar("index", "4", origin="pipeline")
        c4.config.set_scalar("direction", "capture", origin="pipeline")
        vm.containers[4] = c4

        dc_cap = self._instantiate_widget_node(
            wclass="dai-copier",
            winst="HDA.Analog.capture",
            pipeline_id=4,
            pipeline_class="io-gateway-capture",
            pipeline_direction="capture",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        gn_cap = self._instantiate_widget_node(
            wclass="gain",
            winst="1",
            pipeline_id=3,
            pipeline_class="host-gateway-capture",
            pipeline_direction="capture",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        hc_cap = self._instantiate_widget_node(
            wclass="host-copier",
            winst="0.capture",
            pipeline_id=3,
            pipeline_class="host-gateway-capture",
            pipeline_direction="capture",
            override_stmts=[],
            defines=defines,
            source_file="",
        )
        for wnode in (dc_cap, gn_cap, hc_cap):
            vm.nodes[wnode.id] = wnode
            self.attach_widget_kcontrol_nodes(vm, wnode)

        vm.edges.append(
            GraphEdge(
                id="edge_mono_2",
                source_node=dc_cap.id,
                sink_node=gn_cap.id,
                pipeline_id=4,
                is_inter_pipeline=True,
            )
        )
        vm.edges.append(
            GraphEdge(
                id="edge_mono_3",
                source_node=gn_cap.id,
                sink_node=hc_cap.id,
                pipeline_id=3,
            )
        )
        # Re-attach Testbench IO nodes so capture pipelines also have source/sink cards
        for nid in [k for k, v in vm.nodes.items() if v.class_category == "TestbenchIO"]:
            del vm.nodes[nid]
        vm.edges = [e for e in vm.edges if not e.id.startswith("tb_io_edge_")]
        self.attach_host_copier_io_nodes(vm)
        return vm

    def build_graph_for_arbitrary_conf_file(self, conf_path: str) -> GraphViewModel:
        """Open any .conf file and build the appropriate GraphViewModel."""
        abs_path = os.path.abspath(conf_path)
        for mname, mdef in self.processing_modules.items():
            if os.path.abspath(mdef.source_file) == abs_path:
                return self.build_graph_for_module(mname)
        for pname, pdef in self.pipeline_classes.items():
            if os.path.abspath(pdef.source_file) == abs_path:
                return self.build_graph_for_pipeline(pname)
        for bkey, bdef in self.base_classes.items():
            if os.path.abspath(bdef.source_file) == abs_path:
                return self.build_graph_for_base_class(bkey)
        for entry in self.machine_topologies:
            if os.path.abspath(entry.conf_path) == abs_path:
                variant = entry.variants[0] if entry.variants else None
                return self.build_graph_for_topology(abs_path, variant)
        return self.build_graph_for_topology(abs_path, None)

    def build_graph_from_conf_text(
        self,
        text: str,
        source_file: str = "",
        fallback_view_kind: str = "topology",
        fallback_title: str = "",
        target_variant: Optional[CMakeTargetVariant] = None,
    ) -> GraphViewModel:
        """Parse edited Topology2 (.conf) text directly and rebuild the GraphViewModel."""
        eval_file = (
            os.path.abspath(source_file)
            if source_file
            else os.path.join(self.root_dir, "__in_memory_editor__.conf")
        )
        stmts = parse_conf_text(text, source_file=eval_file)
        rel_path = os.path.relpath(eval_file, self.root_dir) if source_file else "(Edited Buffer)"

        # Check if the buffer defines a Class.<Category>.<Name>
        for stmt in stmts:
            if stmt.stmt_type == "block" and stmt.key.startswith("Class."):
                parts = split_dotted_key(stmt.key)
                if len(parts) >= 3:
                    cat = parts[1]
                    cname = parts[2]
                    cdef = self._build_class_def(cat, cname, stmt, eval_file, rel_path)
                    self.all_classes[f"{cat}.{cname}"] = cdef
                    if cat == "Widget":
                        self.processing_modules[cname] = cdef
                        vm = self.build_graph_for_module(cname)
                        vm.source_file = source_file
                        vm.dirty = True
                        return vm
                    if cat == "Pipeline":
                        self.pipeline_classes[cname] = cdef
                        self._resolve_pipeline_inheritance()
                        vm = self.build_graph_for_pipeline(cname)
                        vm.source_file = source_file
                        vm.dirty = True
                        return vm
                    self.base_classes[f"{cat}.{cname}"] = cdef
                    vm = self.build_graph_for_base_class(f"{cat}.{cname}")
                    vm.source_file = source_file
                    vm.dirty = True
                    return vm

        # Otherwise evaluate as a multi-pipeline Function Topology or Monolithic Machine Topology
        self._ast_cache[eval_file] = stmts
        vm = self.build_graph_for_topology(eval_file, target_variant)
        vm.source_file = source_file
        if fallback_view_kind in ("function_topology", "topology"):
            vm.view_kind = fallback_view_kind
        if fallback_title and not source_file:
            vm.title = fallback_title
        vm.dirty = True
        return vm
