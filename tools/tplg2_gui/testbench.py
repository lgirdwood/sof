# SPDX-License-Identifier: BSD-3-Clause
"""SOF Testbench (sof-testbench4) runner, audio signal generator, and Gtk.Snapshot waveform viewer."""

from __future__ import annotations
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango


@dataclass
class TestbenchRunResult:
    success: bool
    command: str
    in_wav_path: str
    out_wav_path: str
    in_samples: List[float] = field(default_factory=list)
    out_samples: List[float] = field(default_factory=list)
    in_peak_db: float = -120.0
    out_peak_db: float = -120.0
    in_rms_db: float = -120.0
    out_rms_db: float = -120.0
    stdout: str = ""
    stderr: str = ""
    summary_text: str = ""


class TestbenchRunner:
    """Executes sof-testbench4 on a compiled .tplg file with synthesized or WAV test audio."""

    def __init__(self, topology2_root: str, work_dir: Optional[str] = None):
        self.topology2_root = os.path.abspath(topology2_root)
        self.work_dir = work_dir or os.path.join(tempfile.gettempdir(), "sof_tplg2_gui_build")
        os.makedirs(self.work_dir, exist_ok=True)
        self.testbench_bin = self._find_testbench_bin()

    def _find_testbench_bin(self) -> str:
        sof_root = os.path.abspath(os.path.join(self.topology2_root, "..", "..", ".."))
        workspace_root = os.path.abspath(os.path.join(sof_root, ".."))
        candidates = [
            os.path.join(sof_root, "tools", "testbench", "build_testbench", "install", "bin", "sof-testbench4"),
            os.path.join(sof_root, "tools", "testbench", "build_testbench", "sof-testbench4"),
            os.path.join(workspace_root, "tools", "bin", "sof-testbench4"),
            "/home/lrg/work/work-extra/sof-host/build_testbench/sof-testbench4",
            shutil.which("sof-testbench4") or "",
        ]
        for c in candidates:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return "sof-testbench4"

    def generate_input_signal(
        self,
        signal_kind: str,
        rate: int,
        channels: int,
        bits: int,
        duration_sec: float = 0.15,
        wav_source: str = "",
    ) -> Tuple[str, str, List[float]]:
        """Create input .raw and .wav files and return (raw_path, wav_path, normalized_mono_samples)."""
        in_raw = os.path.join(self.work_dir, "tb_input.raw")
        in_wav = os.path.join(self.work_dir, "tb_input.wav")
        n_frames = max(256, int(rate * duration_sec))

        frames_ch0: List[float] = []
        if signal_kind == "wav_file" and wav_source and os.path.isfile(wav_source):
            if shutil.which("sox"):
                subprocess.run(
                    [
                        "sox",
                        "--encoding",
                        "signed-integer",
                        wav_source,
                        "-L",
                        "-r",
                        str(rate),
                        "-c",
                        str(channels),
                        "-b",
                        "32" if bits == 24 else str(bits),
                        in_raw,
                        "trim",
                        "0",
                        f"{duration_sec:.2f}",
                    ],
                    check=False,
                    capture_output=True,
                )
                frames_ch0 = self._read_raw_normalized(in_raw, channels, bits)
                self._write_wav_from_normalized(in_wav, frames_ch0, rate)
                return (in_raw, in_wav, frames_ch0)

        # Synthesize clean mathematical test waveform
        rng = random.Random(42)
        for i in range(n_frames):
            t = i / float(rate)
            if signal_kind == "chirp":
                f0, f1 = 100.0, min(12000.0, rate * 0.4)
                k = (f1 - f0) / max(duration_sec, 0.01)
                phase = 2.0 * math.pi * (f0 * t + 0.5 * k * t * t)
                val = 0.50 * math.sin(phase)
            elif signal_kind == "multitone":
                val = (
                    0.25 * math.sin(2.0 * math.pi * 250.0 * t)
                    + 0.25 * math.sin(2.0 * math.pi * 997.0 * t)
                    + 0.20 * math.sin(2.0 * math.pi * 4000.0 * t)
                )
            elif signal_kind == "noise":
                val = 0.35 * (rng.uniform(-1.0, 1.0))
            else:
                # Default: 997 Hz Sine at -6 dBFS
                val = 0.50 * math.sin(2.0 * math.pi * 997.0 * t)
            frames_ch0.append(val)

        self._write_raw_from_normalized(in_raw, frames_ch0, channels, bits)
        self._write_wav_from_normalized(in_wav, frames_ch0, rate)
        return (in_raw, in_wav, frames_ch0)

    def run_testbench(
        self,
        tplg_path: str,
        pipeline_ids: List[int],
        signal_kind: str = "sine_997",
        rate: int = 48000,
        channels: int = 2,
        bits: int = 32,
        duration_sec: float = 0.15,
        wav_source: str = "",
        control_script: str = "",
    ) -> TestbenchRunResult:
        in_raw, in_wav, in_norm = self.generate_input_signal(
            signal_kind=signal_kind,
            rate=rate,
            channels=channels,
            bits=bits,
            duration_sec=duration_sec,
            wav_source=wav_source,
        )
        out_raw = os.path.join(self.work_dir, "tb_output.raw")
        out_wav = os.path.join(self.work_dir, "tb_output.wav")
        if os.path.exists(out_raw):
            os.remove(out_raw)

        pipe_arg = ",".join(str(p) for p in pipeline_ids) if pipeline_ids else "1,2"
        fmt_arg = f"S{bits}_LE"

        cmd = [
            self.testbench_bin,
            "-r",
            str(rate),
            "-R",
            str(rate),
            "-c",
            str(channels),
            "-n",
            str(channels),
            "-b",
            fmt_arg,
            "-p",
            pipe_arg,
            "-t",
            tplg_path,
            "-i",
            in_raw,
            "-o",
            out_raw,
        ]
        if control_script and os.path.isfile(control_script):
            cmd.extend(["-s", control_script])

        cmd_str = " ".join(f'"{a}"' if " " in a else a for a in cmd)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            ok = proc.returncode == 0 and os.path.isfile(out_raw) and os.path.getsize(out_raw) > 0
            out_norm = self._read_raw_normalized(out_raw, channels, bits) if ok else []
            if ok and out_norm:
                self._write_wav_from_normalized(out_wav, out_norm, rate)

            in_peak, in_rms = self._compute_metrics(in_norm)
            out_peak, out_rms = self._compute_metrics(out_norm)

            summary = (
                f"Status: {'PASS' if ok else 'FAIL'}  |  Pipelines: -p {pipe_arg}  |  "
                f"In: {len(in_norm)} frames (Peak {in_peak:.1f} dBFS, RMS {in_rms:.1f} dBFS)  →  "
                f"Out: {len(out_norm)} frames (Peak {out_peak:.1f} dBFS, RMS {out_rms:.1f} dBFS)"
            )
            return TestbenchRunResult(
                success=ok,
                command=cmd_str,
                in_wav_path=in_wav,
                out_wav_path=out_wav if ok else "",
                in_samples=in_norm,
                out_samples=out_norm,
                in_peak_db=in_peak,
                out_peak_db=out_peak,
                in_rms_db=in_rms,
                out_rms_db=out_rms,
                stdout=proc.stdout,
                stderr=proc.stderr,
                summary_text=summary,
            )
        except Exception as exc:
            return TestbenchRunResult(
                success=False,
                command=cmd_str,
                in_wav_path=in_wav,
                out_wav_path="",
                in_samples=in_norm,
                out_samples=[],
                stdout="",
                stderr=str(exc),
                summary_text=f"Error executing testbench: {exc}",
            )

    @staticmethod
    def _write_raw_from_normalized(raw_path: str, samples: List[float], channels: int, bits: int) -> None:
        with open(raw_path, "wb") as f:
            if bits == 16:
                scale = 32767.0
                for s in samples:
                    v = max(-32768, min(32767, int(s * scale)))
                    f.write(struct.pack("<" + "h" * channels, *([v] * channels)))
            elif bits == 24:
                scale = 8388607.0
                for s in samples:
                    v = max(-8388608, min(8388607, int(s * scale)))
                    f.write(struct.pack("<" + "i" * channels, *([v] * channels)))
            else:
                scale = 2147483647.0
                for s in samples:
                    v = max(-2147483648, min(2147483647, int(s * scale)))
                    f.write(struct.pack("<" + "i" * channels, *([v] * channels)))

    @staticmethod
    def _read_raw_normalized(raw_path: str, channels: int, bits: int) -> List[float]:
        if not os.path.isfile(raw_path):
            return []
        data = open(raw_path, "rb").read()
        ch = max(1, channels)
        out: List[float] = []
        if bits == 16:
            frame_sz = 2 * ch
            n = len(data) // frame_sz
            for i in range(n):
                vals = struct.unpack_from("<" + "h" * ch, data, i * frame_sz)
                out.append(vals[0] / 32768.0)
        elif bits == 24:
            frame_sz = 4 * ch
            n = len(data) // frame_sz
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(max(-1.0, min(1.0, vals[0] / 8388608.0)))
        else:
            frame_sz = 4 * ch
            n = len(data) // frame_sz
            for i in range(n):
                vals = struct.unpack_from("<" + "i" * ch, data, i * frame_sz)
                out.append(vals[0] / 2147483648.0)
        return out

    @staticmethod
    def _write_wav_from_normalized(wav_path: str, samples: List[float], rate: int) -> None:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            buf = bytearray()
            for s in samples:
                v = max(-32768, min(32767, int(s * 32767.0)))
                buf.extend(struct.pack("<h", v))
            wf.writeframes(bytes(buf))

    @staticmethod
    def _compute_metrics(samples: List[float]) -> Tuple[float, float]:
        if not samples:
            return (-120.0, -120.0)
        peak = max(abs(x) for x in samples)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        peak_db = 20.0 * math.log10(peak) if peak > 1e-9 else -120.0
        rms_db = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
        return (peak_db, rms_db)


class WaveformSnapshotWidget(Gtk.Widget):
    """Hardware-accelerated Gtk.Snapshot widget comparing Input vs Output testbench waveforms."""

    __gtype_name__ = "WaveformSnapshotWidget"

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_size_request(-1, 110)
        self.in_samples: List[float] = []
        self.out_samples: List[float] = []
        self.summary_label: str = "Run Testbench to inspect Input vs. Output audio waveforms"

    def set_waveforms(self, in_samples: List[float], out_samples: List[float], summary: str = "") -> None:
        self.in_samples = in_samples
        self.out_samples = out_samples
        if summary:
            self.summary_label = summary
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w = float(self.get_width())
        h = float(self.get_height())
        if w <= 0 or h <= 0:
            return

        bg = Gdk.RGBA()
        bg.red, bg.green, bg.blue, bg.alpha = (0.07, 0.08, 0.11, 1.0)
        bounds = Graphene.Rect.alloc()
        bounds.init(0.0, 0.0, w, h)
        snapshot.append_color(bg, bounds)

        mid_in_y = h * 0.32
        mid_out_y = h * 0.74
        amp = h * 0.18

        # Draw center zero-lines
        zero_col = Gdk.RGBA()
        zero_col.red, zero_col.green, zero_col.blue, zero_col.alpha = (0.22, 0.26, 0.34, 1.0)
        zpb = Gsk.PathBuilder.new()
        zpb.move_to(0.0, mid_in_y)
        zpb.line_to(w, mid_in_y)
        zpb.move_to(0.0, mid_out_y)
        zpb.line_to(w, mid_out_y)
        snapshot.append_stroke(zpb.to_path(), Gsk.Stroke.new(1.0), zero_col)

        # Draw Input Waveform (Cyan)
        if self.in_samples:
            in_col = Gdk.RGBA()
            in_col.red, in_col.green, in_col.blue, in_col.alpha = (0.28, 0.78, 0.96, 0.95)
            self._draw_trace(snapshot, self.in_samples, w, mid_in_y, amp, in_col)

        # Draw Output Waveform (Emerald Green)
        if self.out_samples:
            out_col = Gdk.RGBA()
            out_col.red, out_col.green, out_col.blue, out_col.alpha = (0.24, 0.92, 0.58, 0.95)
            self._draw_trace(snapshot, self.out_samples, w, mid_out_y, amp, out_col)

        # Overlay labels
        txt_col = Gdk.RGBA()
        txt_col.red, txt_col.green, txt_col.blue, txt_col.alpha = (0.85, 0.90, 0.96, 1.0)
        layout = self.create_pango_layout(self.summary_label)
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
        snapshot.save()
        pt = Graphene.Point.alloc()
        pt.init(10.0, 4.0)
        snapshot.translate(pt)
        snapshot.append_layout(layout, txt_col)
        snapshot.restore()

    @staticmethod
    def _draw_trace(
        snapshot: Gtk.Snapshot,
        samples: List[float],
        w: float,
        center_y: float,
        amp: float,
        color: Gdk.RGBA,
    ) -> None:
        # Plot first ~600 samples so individual sine/chirp cycles are clearly visible
        view_n = min(len(samples), 600)
        if view_n < 2:
            return
        pb = Gsk.PathBuilder.new()
        for i in range(view_n):
            x = (i / float(view_n - 1)) * w
            y = center_y - max(-1.0, min(1.0, samples[i])) * amp
            if i == 0:
                pb.move_to(x, y)
            else:
                pb.line_to(x, y)
        snapshot.append_stroke(pb.to_path(), Gsk.Stroke.new(1.6), color)
