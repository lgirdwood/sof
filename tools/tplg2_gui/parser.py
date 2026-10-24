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


def parse_conf_file(path: str) -> List[AstStmt]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    tokens = tokenize_alsaconf(text)
    stream = TokenStream(tokens, source_file=path)
    return parse_stmt_list(stream)


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

    def _build_class_def(
        self,
        category: str,
        name: str,
        class_stmt: AstStmt,
        fpath: str,
        rel_path: str,
    ) -> TplgClassDef:
        cdef = TplgClassDef(
            category=category,
            name=name,
            source_file=fpath,
            rel_path=rel_path,
            description=class_stmt.doxy,
        )
        # Flatten inline includes inside the class block (e.g. <include/components/widget-common.conf>)
        expanded_stmts = self._expand_class_body(class_stmt.children, fpath)
        self._populate_class_from_stmts(cdef, expanded_stmts)
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

    def build_graph_for_module(self, module_name: str) -> GraphViewModel:
        """Build a graph representing a Processing Module Class.Widget.<name> (Pane 2)."""
        cdef = self.processing_modules[module_name]
        vm = GraphViewModel(
            title=f"Class.Widget.{cdef.name}",
            subtitle=f"Processing Module ({cdef.rel_path})",
            view_kind="module",
            source_file=cdef.source_file,
            root_config=cdef.config_tree.clone(),
        )
        num_in = int(cdef.defaults.get("num_input_pins", 1) or 1)
        num_out = int(cdef.defaults.get("num_output_pins", 1) or 1)
        in_ports = [GraphPort(f"in{i}", f"in.{i}", "in", "audio") for i in range(max(1, num_in))]
        out_ports = [GraphPort(f"out{i}", f"out.{i}", "out", "audio") for i in range(max(1, num_out))]

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
        return vm

    def build_graph_for_pipeline(self, pipeline_name: str, index: int = 1) -> GraphViewModel:
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

        direction = str(pdef.defaults.get("direction", "playback"))
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

        # If a pipeline has host-copier or dai-copier whose route is normally added at top-level,
        # connect unconnected adjacent widgets in declaration order so the pipeline graph is complete
        if len(ordered_widget_ids) >= 2:
            for i in range(len(ordered_widget_ids) - 1):
                u = ordered_widget_ids[i]
                v = ordered_widget_ids[i + 1]
                # Check if u has any outgoing edge or v has any incoming edge
                u_has_out = any(e.source_node == u for e in vm.edges)
                v_has_in = any(e.sink_node == v for e in vm.edges)
                if not u_has_out and not v_has_in and (u, v) not in added_pairs:
                    added_pairs.add((u, v))
                    edge_cfg = ConfigNode("Object.Base.route (endpoint)", node_type="object")
                    edge_cfg.set_scalar("source", u, origin="pipeline")
                    edge_cfg.set_scalar("sink", v, origin="pipeline")
                    vm.edges.append(
                        GraphEdge(
                            id=f"implicit_{index}_{i}",
                            source_node=u,
                            sink_node=v,
                            pipeline_id=index,
                            config=edge_cfg,
                        )
                    )

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
        expanded_stmts = self._evaluate_topology_file(conf_path, defines, search_dirs, cli_keys=cli_keys)

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

        # Instantiate standalone/top-level Object.Widget overrides or virtual widgets
        for wclass, winst, wstmts in top_widgets:
            if wclass == "virtual":
                continue  # Keep graph focused on active DSP pipelines/widgets/DAIs/PCMs
            # Find index in wstmts
            p_idx: Optional[int] = None
            for ch in wstmts:
                if ch.stmt_type == "kv" and ch.key == "index":
                    val_s = self._substitute_vars(str(ch.value), defines)
                    if val_s.lstrip("-").isdigit():
                        p_idx = int(val_s)
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

        return vm

    def _evaluate_topology_file(
        self,
        fpath: str,
        defines: Dict[str, str],
        search_dirs: List[str],
        visited: Optional[Set[str]] = None,
        cli_keys: Optional[Set[str]] = None,
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
        return self._evaluate_stmt_list(stmts, defines, search_dirs, abs_f, visited, cli_keys)

    def _evaluate_stmt_list(
        self,
        stmts: List[AstStmt],
        defines: Dict[str, str],
        search_dirs: List[str],
        current_file: str,
        visited: Set[str],
        cli_keys: Set[str],
    ) -> List[AstStmt]:
        out: List[AstStmt] = []
        for st in stmts:
            if st.stmt_type == "searchdir":
                if st.key not in search_dirs:
                    search_dirs.append(st.key)
            elif st.stmt_type == "include":
                inc_path = self.resolve_include(st.key, search_dirs, current_file)
                if inc_path and not self._is_pure_class_include(inc_path):
                    out.extend(self._evaluate_topology_file(inc_path, defines, search_dirs, visited, cli_keys))
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
                for ch in st.children:
                    pattern = strip_quotes(ch.key)
                    if self._key_matches(pattern, var_val):
                        if ch.stmt_type == "kv" and ch.value:
                            inc_file = self._substitute_vars(str(ch.value), defines)
                            inc_path = self.resolve_include(inc_file, search_dirs, current_file)
                            if inc_path:
                                # Allow re-inclusion of parametric files across IncludeByKey blocks
                                out.extend(self._evaluate_topology_file(inc_path, defines, search_dirs, set(), cli_keys))
                        elif ch.stmt_type == "block":
                            out.extend(self._evaluate_stmt_list(ch.children, defines, search_dirs, current_file, visited, cli_keys))
                        break
            else:
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
        return rel.startswith("include/")

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
        if len(parts) == 2 and st.stmt_type == "block":
            for pch in st.children:
                pparts = split_dotted_key(pch.key)
                pclass = pparts[0]
                if pch.stmt_type == "array":
                    for item in pch.children:
                        if item.stmt_type == "block":
                            self._instantiate_pipeline_in_topology(pclass, None, item.children, vm, defines, raw_routes, source_file)
                elif pch.stmt_type == "block":
                    pidx_str = pparts[1] if len(pparts) > 1 else None
                    self._instantiate_pipeline_in_topology(pclass, pidx_str, pch.children, vm, defines, raw_routes, source_file)
        elif len(parts) == 3 and st.stmt_type == "array":
            pclass = parts[2]
            for item in st.children:
                if item.stmt_type == "block":
                    self._instantiate_pipeline_in_topology(pclass, None, item.children, vm, defines, raw_routes, source_file)
        elif len(parts) >= 3 and st.stmt_type == "block":
            pclass = parts[2]
            pidx_str = parts[3] if len(parts) > 3 else None
            self._instantiate_pipeline_in_topology(pclass, pidx_str, st.children, vm, defines, raw_routes, source_file)

    def _instantiate_pipeline_in_topology(
        self,
        pclass: str,
        explicit_idx: Optional[str],
        body_stmts: List[AstStmt],
        vm: GraphViewModel,
        defines: Dict[str, str],
        raw_routes: List[Tuple[Dict[str, str], Optional[int]]],
        source_file: str,
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

        container_cfg = pdef.config_tree.clone() if pdef else ConfigNode(f"Pipeline.{pclass}", node_type="object")
        container_cfg.set_scalar("index", str(p_idx), origin="instance", is_constructor=True)

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

    def _collect_topology_widget_stmt(
        self,
        st: AstStmt,
        parts: List[str],
        top_widgets: List[Tuple[str, str, List[AstStmt]]],
    ) -> None:
        if len(parts) == 2 and st.stmt_type == "block":
            for wch in st.children:
                wp = split_dotted_key(wch.key)
                wc = wp[0]
                if wch.stmt_type == "array":
                    for idx, item in enumerate(wch.children):
                        if item.stmt_type == "block":
                            top_widgets.append((wc, str(idx + 1), item.children))
                elif wch.stmt_type == "block":
                    wi = wp[1] if len(wp) > 1 else "1"
                    top_widgets.append((wc, wi, wch.children))
        elif len(parts) >= 3:
            wc = parts[2]
            if st.stmt_type == "array":
                for idx, item in enumerate(st.children):
                    if item.stmt_type == "block":
                        top_widgets.append((wc, str(idx + 1), item.children))
            elif st.stmt_type == "block":
                wi = parts[3] if len(parts) > 3 else "1"
                top_widgets.append((wc, wi, st.children))

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

        num_in = int(str(cfg.get_scalar("num_input_pins", 1) or 1))
        num_out = int(str(cfg.get_scalar("num_output_pins", 1) or 1))
        in_ports = [GraphPort(f"in{i}", f"in.{i}", "in", "audio") for i in range(max(1, num_in))]
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

    def attach_widget_kcontrol_nodes(self, vm: GraphViewModel, parent_node: GraphNode) -> None:
        """Instantiate child kcontrol GraphNodes below parent_node for each control in Object.Control."""
        ctrl_group = parent_node.config.get_child("Object.Control")
        if not ctrl_group or not ctrl_group.children:
            return

        ctrl_cat_map = {
            "mixer": "control_mixer",
            "bytes": "control_bytes",
            "enum": "control_enum",
        }

        for idx, ctrl_cfg in enumerate(ctrl_group.children):
            cparts = split_dotted_key(ctrl_cfg.key)
            ctype = cparts[0] if cparts else "mixer"
            cinst = strip_quotes(cparts[1]) if len(cparts) > 1 else str(idx + 1)
            node_cat = ctrl_cat_map.get(ctype, "control")

            kname = str(ctrl_cfg.get_scalar("name", "")).strip()
            title = f"{ctype}.{cinst}: {kname}" if kname else f"{ctype}.{cinst}"

            badges: List[str] = []
            if ctype == "mixer":
                max_val = str(ctrl_cfg.get_scalar("max", "")).strip()
                is_switch = max_val == "1"
                if is_switch:
                    node_cat = "control_switch"
                subtitle = f"Control.mixer ({'Switch' if is_switch else 'Volume'})"
                if max_val:
                    badges.append(f"max {max_val}")
                if any(c.key.startswith("Object.Base.tlv") for c in ctrl_cfg.children):
                    badges.append("TLV")
            elif ctype == "bytes":
                subtitle = "Control.bytes (TLV/Blob)"
                pfile = str(ctrl_cfg.get_scalar("included_param_file", "")).strip()
                max_val = str(ctrl_cfg.get_scalar("max", "")).strip()
                if pfile:
                    badges.append(os.path.basename(pfile))
                if max_val:
                    badges.append(f"max {max_val}")
            elif ctype == "enum":
                subtitle = "Control.enum (Enum)"
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

    def _compute_widget_id(
        self,
        wclass: str,
        winst: str,
        pipeline_id: Optional[int],
        cfg: ConfigNode,
        cdef: Optional[TplgClassDef],
    ) -> str:
        if cdef and cdef.constructor_attrs:
            parts = [wclass]
            for attr in cdef.constructor_attrs:
                if attr == "index" and pipeline_id is not None:
                    parts.append(str(pipeline_id))
                elif attr == "instance":
                    parts.append(str(winst))
                else:
                    val = cfg.get_scalar(attr, "")
                    parts.append(str(val) if val != "" else str(winst))
            return ".".join(parts)
        idx_s = str(pipeline_id) if pipeline_id is not None else "0"
        return f"{wclass}.{idx_s}.{winst}"

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
