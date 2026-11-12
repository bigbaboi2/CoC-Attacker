"""Read-only, low-frequency CPU and game presentation-rate samples."""
import os
import re
import shlex
import time

import psutil


class PerformanceMonitor:
    PACKAGE = 'com.supercell.clashofclans'
    MUMU_NAMES = {'mumunxdevice.exe', 'mumunxmain.exe', 'nemuplayer.exe'}

    def __init__(self, adb):
        self.adb = adb
        self.tool = psutil.Process(os.getpid())
        self.tool.cpu_percent(None)
        self.processes = {}
        self._process_scan_at = -100.0
        self._layer = None
        self._serial = None
        self._last_present = None
        self._last_progress_at = None
        self._layer_scan_at = -100.0

    @staticmethod
    def layer_names(output):
        names = []
        for line in output.splitlines():
            if PerformanceMonitor.PACKAGE not in line or 'SurfaceView' not in line or 'Background' in line:
                continue
            name = line.strip()
            if name.startswith('RequestedLayerState{'):
                name = name[len('RequestedLayerState{'):].split(' parentId=')[0].rstrip('}')
            names.append(name)
        return sorted(names, key=lambda name: 'BLAST' not in name)

    @staticmethod
    def presentation_times(output):
        # FrameTracker dumps desiredPresentTime, actualPresentTime, frameReadyTime.
        present = set()
        for line in output.splitlines():
            parts = line.split()
            if len(parts) != 3 or not all(re.fullmatch(r'\d+', item) for item in parts):
                continue
            desired, actual, ready = map(int, parts)
            if 0 < actual < 9223372036854775807 and desired > 0 and ready > 0:
                present.add(actual)
        return sorted(present)

    @staticmethod
    def fps_from_times(present):
        if len(present) < 2 or present[-1] <= present[0]:
            return None
        return (len(present) - 1) * 1e9 / (present[-1] - present[0])

    def _fps(self, now):
        serial = self.adb.device_serial
        if serial != self._serial:
            self._serial, self._layer = serial, None
            self._last_present = self._last_progress_at = None
            self._layer_scan_at = -100.0
        if not serial:
            return None
        if self._layer is None:
            if now - self._layer_scan_at < 10:
                return None
            self._layer_scan_at = now
            names = self.layer_names(self.adb.shell('dumpsys SurfaceFlinger --list'))
        else:
            names = [self._layer]
        for name in names:
            output = self.adb.shell('dumpsys SurfaceFlinger --latency ' + shlex.quote(name))
            present = self.presentation_times(output)
            fps = self.fps_from_times(present)
            if fps is None:
                continue
            self._layer = name
            if self._last_present is None or present[-1] > self._last_present:
                self._last_present = present[-1]
                self._last_progress_at = now
                return fps
            # An old ring buffer must not keep reporting 60 FPS after rendering stops.
            if now - self._last_progress_at >= 4:
                self._layer = None
                return None
            return None
        self._layer = None
        return None

    def sample(self):
        now = time.monotonic()
        count = psutil.cpu_count() or 1
        if now - self._process_scan_at >= 10:
            self._process_scan_at = now
            found = {}
            for proc in psutil.process_iter(['pid', 'name']):
                if (proc.info.get('name') or '').lower() in self.MUMU_NAMES:
                    old = self.processes.get(proc.pid)
                    if old is None:
                        try:
                            proc.cpu_percent(None)
                        except psutil.Error:
                            continue
                    found[proc.pid] = old or proc
            self.processes = found
        mumu_cpu = 0.0 if self.processes else None
        for proc in self.processes.values():
            try:
                mumu_cpu += proc.cpu_percent(None) / count
            except psutil.Error:
                pass
        tool_cpu = self.tool.cpu_percent(None) / count
        try:
            fps = self._fps(now)
        except (OSError, ValueError, RuntimeError):
            self._layer = None
            fps = None
        return dict(tool_cpu=min(100., max(0., tool_cpu)),
                    mumu_cpu=None if mumu_cpu is None else min(100., max(0., mumu_cpu)),
                    fps=fps)
