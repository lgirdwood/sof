# SPDX-License-Identifier: BSD-3-Clause
"""ALSATPLG Topology2 compiler integration."""

from __future__ import annotations
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

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
class CompileResult:
    success: bool
    tplg_path: str
    conf_path: str
    pipeline_ids: List[int]
    command: str
    stdout: str
    stderr: str
    size_bytes: int = 0


class TopologyCompiler:
    """Compiles a GraphViewModel into a binary .tplg using alsatplg -p."""

    def __init__(self, topology2_root: str, build_dir: Optional[str] = None):
        self.topology2_root = os.path.abspath(topology2_root)
        self.build_dir = build_dir or os.path.join(tempfile.gettempdir(), "sof_tplg2_gui_build")
        os.makedirs(self.build_dir, exist_ok=True)
        self.alsatplg_bin = self._find_alsatplg()

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

        body_text, defines_str, tb_pipes = generate_compilable_conf(vm, self.topology2_root)
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
            return CompileResult(
                success=ok,
                tplg_path=tplg_path,
                conf_path=conf_path,
                pipeline_ids=tb_pipes,
                command=cmd_str,
                stdout=proc.stdout,
                stderr=proc.stderr,
                size_bytes=sz,
            )
        except Exception as exc:
            return CompileResult(
                success=False,
                tplg_path=tplg_path,
                conf_path=conf_path,
                pipeline_ids=tb_pipes,
                command=cmd_str,
                stdout="",
                stderr=str(exc),
                size_bytes=0,
            )
