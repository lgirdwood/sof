# SPDX-License-Identifier: BSD-3-Clause
"""ALSATPLG Topology2 compiler integration."""

from __future__ import annotations
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import GraphViewModel
from .serializer import generate_compilable_conf


DEFAULT_ABI_CONF = """Object.Base.manifest.1 {
\tname "sof_manifest"
\tObject.Base.data.1 {
\t\tname "SOF ABI"
\t\tbytes\t"0x03,0x00,0x1d,0x00,0x01,0x00"
\t}
}
"""


@dataclass
class CompileDiagnostic:
    """Structured diagnostic parsed from alsatplg compilation output."""

    severity: str  # "error" or "warning"
    raw_line: str
    message: str
    conf_file: Optional[str] = None
    line_no: Optional[int] = None
    col_no: Optional[int] = None
    symbol_or_attr: Optional[str] = None
    target_kind: str = "root"  # "node", "container", "edge", "define", "root"
    target_id: Optional[str] = None


@dataclass
class CompileResult:
    success: bool
    tplg_path: str
    conf_path: str
    pipeline_ids: List[int]
    command: str
    stdout: str
    stderr: str
    size_bytes: int = 0
    diagnostics: List[CompileDiagnostic] = field(default_factory=list)


_ALSA_PREFIX_RE = re.compile(
    r"^ALSA\s+lib\s+[^:]+:\d+:\([^)]*\)\s*(?:\[[^\]]*\]\s*)?",
    re.IGNORECASE,
)
_FILE_LOC_RE = re.compile(
    r"(?P<file>_toplevel_|[A-Za-z0-9_./\-]+\.conf):(?P<line>\d+)(?::(?P<col>\d+))?:?\s*(?P<reason>.*)"
)
_NOT_A_TYPE_RE = re.compile(
    r"['\"]?(?P<sym>[A-Za-z0-9_.\-]+)['\"]?\s+is\s+not\s+a\s+(?:string|integer|real|compound|boolean|number)",
    re.IGNORECASE,
)
_MANDATORY_ATTR_RE = re.compile(
    r"Mandatory\s+attribute\s+['\"]?(?P<attr>[A-Za-z0-9_.\-]+)['\"]?\s+not\s+set(?:\s+for\s+class\s+['\"]?(?P<cls>[A-Za-z0-9_.\-]+)['\"]?)?",
    re.IGNORECASE,
)
_INVALID_ATTR_RE = re.compile(
    r"(?:Unknown|Invalid)\s+(?:field|attribute|key|compound|type|value(?:\s+for)?)\s+['\"]?(?P<attr>[A-Za-z0-9_.\-]+)['\"]?",
    re.IGNORECASE,
)
_OBJECT_REF_RE = re.compile(
    r"(?:Failed\s+to\s+update\s+attributes\s+for\s+object|error\s+getting\s+widget\s+name\s+for|Error\s+building\s+object|No\s+Class\s+definition\s+found\s+for)\s+['\"]?(?P<obj>[A-Za-z0-9_.\-]+)['\"]?",
    re.IGNORECASE,
)
_ERROR_LINE_RE = re.compile(
    r"\[error[^\]]*\]|\berror[:\s]|\bUnable\s+(?:to|not)\b|\bFailed\s+to\b|\bError\s+building\b|\bInvalid\b|\bUnknown\b|\bMandatory\s+attribute\b|\bNo\s+Class\s+definition\b|\bUnexpected\s+char\b",
    re.IGNORECASE,
)
_WARN_LINE_RE = re.compile(
    r"\[warn[^\]]*\]|\bwarning[:\s]|\bwarn[:\s]",
    re.IGNORECASE,
)


def classify_log_line(line: str) -> Optional[str]:
    """Return 'error', 'warning', or None for a single compiler/console line."""
    s = line.strip()
    if not s or s.startswith("$ "):
        return None
    if _ERROR_LINE_RE.search(s):
        return "error"
    if _WARN_LINE_RE.search(s):
        return "warning"
    return None


def _extract_key_from_conf_line(line_text: str) -> Optional[str]:
    """Extract the leading configuration key from a .conf line if present."""
    s = line_text.split("#", 1)[0].strip()
    if not s or s in ("{", "}", "[", "]") or s.startswith("<"):
        return None
    m = re.match(r"^([A-Za-z0-9_.\-]+)", s)
    return m.group(1) if m else None


def _match_object_hint_to_vm(
    obj_hint: str,
    vm: Optional[GraphViewModel],
) -> Tuple[str, Optional[str]]:
    """Map an alsatplg object identifier (e.g. 'Widget.gain.99', 'gain.1.1', 'Base.route.0') to (target_kind, target_id)."""
    if not vm or not obj_hint:
        return ("root", None)
    clean = obj_hint.strip("'\"")
    if clean.startswith("Object."):
        clean = clean[len("Object."):]

    # 1. Exact node ID match
    if clean in vm.nodes:
        return ("node", clean)

    # 2. Widget.<class>.<inst> or <class>.<pid>.<inst>
    w_clean = clean[len("Widget."):] if clean.startswith("Widget.") else clean
    if w_clean in vm.nodes:
        return ("node", w_clean)
    parts = w_clean.split(".")
    if len(parts) >= 2:
        wclass = parts[0]
        winst = parts[-1]
        for nid, n in vm.nodes.items():
            if n.class_name == wclass and (n.instance_id == winst or nid.endswith(f".{winst}")):
                return ("node", nid)
        for nid, n in vm.nodes.items():
            if n.class_name == wclass:
                return ("node", nid)

    # 3. Class.Widget.<class>
    if clean.startswith("Class.Widget."):
        wclass = clean[len("Class.Widget."):]
        for nid, n in vm.nodes.items():
            if n.class_name == wclass:
                return ("node", nid)

    # 4. Pipeline.<class>.<pid> or pipeline.<pid>
    if clean.lower().startswith("pipeline."):
        pparts = clean.split(".")
        if pparts[-1].isdigit() and int(pparts[-1]) in vm.containers:
            return ("container", pparts[-1])

    # 5. Base.route.<idx>
    if clean.startswith("Base.route"):
        rparts = clean.split(".")
        if rparts[-1].isdigit():
            idx = int(rparts[-1])
            if 0 <= idx < len(vm.edges):
                return ("edge", vm.edges[idx].id)
        if vm.edges:
            return ("edge", vm.edges[-1].id)

    # 6. Define variable match
    def_node = vm.root_config.get_child("Define (Active Variables)")
    if def_node and def_node.get_child(clean) is not None:
        return ("define", clean)

    return ("root", None)


def _inspect_conf_line_context(
    conf_file: str,
    line_no: int,
    vm: Optional[GraphViewModel],
    hint_symbol: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Inspect `conf_file` around 1-based `line_no` to determine (target_kind, target_id, symbol_or_attr)."""
    if not conf_file or not os.path.isfile(conf_file) or line_no <= 0:
        return ("root", None, hint_symbol)
    try:
        with open(conf_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.splitlines()
    except Exception:
        return ("root", None, hint_symbol)

    idx = min(len(lines) - 1, max(0, line_no - 1))
    # ALSA's _snd_config_load_with_include often reports the line immediately after a failed parse_value
    key_curr = _extract_key_from_conf_line(lines[idx])
    key_prev = _extract_key_from_conf_line(lines[idx - 1]) if idx > 0 else None

    chosen_idx = idx
    chosen_key = hint_symbol or key_curr or key_prev
    if hint_symbol and idx > 0 and key_prev == hint_symbol:
        chosen_idx = idx - 1
        chosen_key = key_prev
    elif key_curr is None and key_prev is not None:
        chosen_idx = idx - 1
        chosen_key = key_prev

    # Walk upward from chosen_idx to find the enclosing top-level block header (indent 0)
    enclosing_header: Optional[str] = None
    block_start_idx = chosen_idx
    for i in range(chosen_idx, -1, -1):
        raw = lines[i]
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not raw[0].isspace() and ("{" in stripped or "[" in stripped):
            enclosing_header = stripped.split("{", 1)[0].split("[", 1)[0].strip()
            block_start_idx = i
            break

    # If no hint_symbol was given and we're in a Define block where ALSA reported line N+1,
    # check if line N-1 has a type mismatch (e.g. integer Define quoted as string)
    if enclosing_header == "Define":
        if not hint_symbol and idx > 0 and key_prev:
            prev_text = lines[idx - 1].strip()
            # If previous line in Define has a quoted number like '52', prefer key_prev
            if re.search(r"['\"]\d+['\"]", prev_text):
                chosen_key = key_prev
        return ("define", chosen_key, chosen_key)

    if enclosing_header and enclosing_header.startswith("Object.Widget."):
        # Check for `index <pid>` inside this block
        block_end = min(len(lines), block_start_idx + 40)
        pid_val: Optional[int] = None
        for j in range(block_start_idx, block_end):
            m_idx = re.match(r"^\s*index\s+(\d+)", lines[j])
            if m_idx:
                pid_val = int(m_idx.group(1))
                break
            if j > block_start_idx and lines[j].strip().startswith("}"):
                break
        w_tail = enclosing_header[len("Object.Widget."):]
        w_parts = w_tail.split(".")
        wclass = w_parts[0]
        winst = w_parts[1] if len(w_parts) > 1 else "1"
        if vm and pid_val is not None:
            cand_id = f"{wclass}.{pid_val}.{winst}"
            if cand_id in vm.nodes:
                return ("node", cand_id, chosen_key)
        tk, tid = _match_object_hint_to_vm(enclosing_header, vm)
        return (tk, tid, chosen_key)

    if enclosing_header and enclosing_header.startswith("Object.Pipeline."):
        tk, tid = _match_object_hint_to_vm(enclosing_header, vm)
        return (tk, tid, chosen_key)

    if enclosing_header and enclosing_header.startswith("Object.Base.route"):
        # Look around chosen_idx for source / sink
        src_val: Optional[str] = None
        snk_val: Optional[str] = None
        for j in range(max(block_start_idx, chosen_idx - 5), min(len(lines), chosen_idx + 6)):
            m_src = re.match(r"^\s*source\s+['\"]?([^'\"]+)['\"]?", lines[j].strip())
            if m_src:
                src_val = m_src.group(1).strip()
            m_snk = re.match(r"^\s*sink\s+['\"]?([^'\"]+)['\"]?", lines[j].strip())
            if m_snk:
                snk_val = m_snk.group(1).strip()
        if vm:
            for e in vm.edges:
                if (src_val and e.source_node == src_val) or (snk_val and e.sink_node == snk_val):
                    return ("edge", e.id, chosen_key or "sink")
            if src_val and src_val in vm.nodes:
                return ("node", src_val, chosen_key)
            if snk_val and snk_val in vm.nodes:
                return ("node", snk_val, chosen_key)
        return ("edge", vm.edges[-1].id if (vm and vm.edges) else None, chosen_key)

    if chosen_key and vm:
        tk, tid = _match_object_hint_to_vm(chosen_key, vm)
        if tk != "root":
            return (tk, tid, chosen_key)

    return ("root", None, chosen_key)


def parse_compile_diagnostics(
    stdout: str,
    stderr: str,
    conf_path: str,
    vm: Optional[GraphViewModel] = None,
    topology2_root: Optional[str] = None,
) -> List[CompileDiagnostic]:
    """Parse alsatplg stdout/stderr into structured CompileDiagnostic objects mapped to GraphViewModel."""
    combined_lines = []
    for chunk in (stdout, stderr):
        if chunk:
            combined_lines.extend(chunk.splitlines())

    diags: List[CompileDiagnostic] = []
    for raw in combined_lines:
        sev = classify_log_line(raw)
        if not sev:
            continue
        clean_msg = _ALSA_PREFIX_RE.sub("", raw.strip()).strip()
        conf_file: Optional[str] = None
        line_no: Optional[int] = None
        col_no: Optional[int] = None
        symbol_or_attr: Optional[str] = None
        target_kind = "root"
        target_id: Optional[str] = None

        # 1. File:Line:Col pattern
        m_loc = _FILE_LOC_RE.search(clean_msg)
        if m_loc:
            f_tok = m_loc.group("file")
            if f_tok == "_toplevel_":
                conf_file = conf_path
            elif topology2_root and os.path.isfile(os.path.join(topology2_root, f_tok)):
                conf_file = os.path.join(topology2_root, f_tok)
            else:
                conf_file = f_tok
            line_no = int(m_loc.group("line"))
            if m_loc.group("col"):
                col_no = int(m_loc.group("col"))

        # 2. Semantic symbol/attribute/object patterns
        m_type = _NOT_A_TYPE_RE.search(clean_msg)
        if m_type:
            symbol_or_attr = m_type.group("sym")

        m_mand = _MANDATORY_ATTR_RE.search(clean_msg)
        if m_mand:
            symbol_or_attr = m_mand.group("attr")
            if m_mand.group("cls"):
                target_kind, target_id = _match_object_hint_to_vm(
                    f"Class.Widget.{m_mand.group('cls')}", vm
                )

        m_inv = _INVALID_ATTR_RE.search(clean_msg)
        if m_inv and not symbol_or_attr:
            symbol_or_attr = m_inv.group("attr")

        m_obj = _OBJECT_REF_RE.search(clean_msg)
        if m_obj:
            obj_tok = m_obj.group("obj")
            tk, tid = _match_object_hint_to_vm(obj_tok, vm)
            if tk != "root":
                target_kind, target_id = tk, tid
            elif not symbol_or_attr:
                symbol_or_attr = obj_tok

        # 3. Direct symbol match against VM Define variables or node IDs
        if symbol_or_attr and target_kind == "root":
            tk, tid = _match_object_hint_to_vm(symbol_or_attr, vm)
            if tk != "root":
                target_kind, target_id = tk, tid

        # 4. If we have a .conf file and line number, inspect the .conf context
        if conf_file and line_no is not None:
            # Check if previous diagnostic already extracted a symbol_or_attr
            prev_sym = symbol_or_attr or (diags[-1].symbol_or_attr if diags else None)
            tk, tid, sym = _inspect_conf_line_context(conf_file, line_no, vm, hint_symbol=prev_sym)
            if target_kind == "root" and tk != "root":
                target_kind, target_id = tk, tid
            if not symbol_or_attr and sym:
                symbol_or_attr = sym

        diags.append(
            CompileDiagnostic(
                severity=sev,
                raw_line=raw.strip(),
                message=clean_msg,
                conf_file=conf_file,
                line_no=line_no,
                col_no=col_no,
                symbol_or_attr=symbol_or_attr,
                target_kind=target_kind,
                target_id=target_id,
            )
        )

    # 5. Propagate shared context across multi-line error cascades
    if len(diags) > 1:
        best_kind = next((d.target_kind for d in reversed(diags) if d.target_kind != "root"), "root")
        best_id = next((d.target_id for d in reversed(diags) if d.target_id is not None), None)
        best_sym = next((d.symbol_or_attr for d in diags if d.symbol_or_attr is not None), None)
        best_file = next((d.conf_file for d in diags if d.conf_file is not None), None)
        best_line = next((d.line_no for d in diags if d.line_no is not None), None)
        best_col = next((d.col_no for d in diags if d.col_no is not None), None)

        for d in diags:
            if best_kind != "root":
                d.target_kind = best_kind
                if best_id is not None:
                    d.target_id = best_id
            if not d.symbol_or_attr and best_sym:
                d.symbol_or_attr = best_sym
            if not d.conf_file and best_file:
                d.conf_file = best_file
            if d.line_no is None and best_line is not None:
                d.line_no = best_line
                d.col_no = best_col

    return diags


class TopologyCompiler:
    """Compiles a GraphViewModel into a binary .tplg using alsatplg -p."""

    def __init__(self, topology2_root: str, build_dir: Optional[str] = None):
        self.topology2_root = os.path.abspath(topology2_root)
        self.build_dir = build_dir or os.path.join(tempfile.gettempdir(), "sof_tplg2_gui_build")
        os.makedirs(self.build_dir, exist_ok=True)
        self.alsatplg_bin = self._find_alsatplg()
        self.target_platform: str = "mtl"
        self.target_harness: str = "hda_analog"
        self.extra_platform_conf: str = ""

    def _find_alsatplg(self) -> str:
        # Check workspace tools/bin/alsatplg first
        sof_root = os.path.abspath(os.path.join(self.topology2_root, "..", "..", ".."))
        workspace_root = os.path.abspath(os.path.join(sof_root, ".."))
        candidates = [
            os.path.join(workspace_root, "tools", "bin", "alsatplg"),
            os.path.join(workspace_root, "alsa-utils", "topology", "alsatplg"),
            "/home/lrg/work/sof-arl/tools/bin/alsatplg",
            shutil.which("alsatplg") or "",
        ]
        for c in candidates:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return "alsatplg"

    def _ensure_abi_conf(self) -> str:
        abi_path = os.path.join(self.build_dir, "abi.conf")
        if os.path.isfile(abi_path):
            return abi_path

        get_abi = os.path.join(self.topology2_root, "get_abi.sh")
        sof_root = os.path.abspath(os.path.join(self.topology2_root, "..", "..", ".."))
        if os.path.isfile(get_abi) and os.path.isdir(os.path.join(sof_root, "src")):
            try:
                res = subprocess.run(
                    [get_abi, sof_root, "ipc4"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                if res.stdout.strip():
                    with open(abi_path, "w", encoding="utf-8") as f:
                        f.write(res.stdout)
                    return abi_path
            except Exception:
                pass

        with open(abi_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_ABI_CONF)
        return abi_path

    def compile_graph(
        self,
        vm: GraphViewModel,
        extra_defines: str = "",
    ) -> CompileResult:
        """Serialize vm to .conf, prepend abi.conf, and compile to .tplg with alsatplg -p."""
        os.makedirs(self.build_dir, exist_ok=True)
        abi_path = self._ensure_abi_conf()
        with open(abi_path, "r", encoding="utf-8") as f:
            abi_text = f.read()

        body_text, defines_str, tb_pipes = generate_compilable_conf(
            vm,
            self.topology2_root,
            target_platform=self.target_platform,
            target_harness=self.target_harness,
            extra_platform_conf=self.extra_platform_conf,
        )
        if extra_defines.strip():
            defines_str = f"{defines_str},{extra_defines.strip()}" if defines_str else extra_defines.strip()

        safe_name = re.sub(r"[^A-Za-z0-9_\-]", "_", vm.title) or "custom_topology"
        conf_path = os.path.join(self.build_dir, f"{safe_name}.conf")
        tplg_path = os.path.join(self.build_dir, f"{safe_name}.tplg")

        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(abi_text + "\n" + body_text)

        cmd = [
            self.alsatplg_bin,
            "-I",
            self.topology2_root,
        ]
        if defines_str:
            cmd.extend(["-D", defines_str])
        cmd.extend(["-p", "-c", conf_path, "-o", tplg_path])

        env = os.environ.copy()
        env["ALSA_CONFIG_DIR"] = self.topology2_root

        cmd_str = f"ALSA_CONFIG_DIR={self.topology2_root} " + " ".join(
            f'"{a}"' if (" " in a or "," in a) else a for a in cmd
        )

        try:
            proc = subprocess.run(
                cmd,
                env=env,
                cwd=self.topology2_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            ok = proc.returncode == 0 and os.path.isfile(tplg_path)
            sz = os.path.getsize(tplg_path) if ok else 0
            diags = parse_compile_diagnostics(
                proc.stdout,
                proc.stderr,
                conf_path,
                vm=vm,
                topology2_root=self.topology2_root,
            )
            return CompileResult(
                success=ok,
                tplg_path=tplg_path,
                conf_path=conf_path,
                pipeline_ids=tb_pipes,
                command=cmd_str,
                stdout=proc.stdout,
                stderr=proc.stderr,
                size_bytes=sz,
                diagnostics=diags,
            )
        except Exception as exc:
            err_s = str(exc)
            diags = parse_compile_diagnostics(
                "",
                err_s,
                conf_path,
                vm=vm,
                topology2_root=self.topology2_root,
            )
            return CompileResult(
                success=False,
                tplg_path=tplg_path,
                conf_path=conf_path,
                pipeline_ids=tb_pipes,
                command=cmd_str,
                stdout="",
                stderr=err_s,
                size_bytes=0,
                diagnostics=diags,
            )
