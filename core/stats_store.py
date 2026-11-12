"""Atomic cumulative statistics with a last-good backup."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from contextlib import contextmanager


class StatsStore:
    @staticmethod
    def combine(key,*values):
        if key=='rph_history':
            from core.recent_rph import merge_history
            return merge_history(*values)
        if key=='fastest_50_ms':
            positive=[value for value in values if type(value) is int and value>0]
            return min(positive) if positive else 0
        return max(values)

    def __init__(self, path, log=None):
        self.path = Path(path).resolve()
        self.backup = self.path.with_suffix(self.path.suffix + '.bak')
        self.log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._last_good = None

    @staticmethod
    def json_default(obj):
        if hasattr(obj, 'tolist'):
            return obj.tolist()
        if hasattr(obj, 'item'):
            return obj.item()
        return str(obj)

    @staticmethod
    def json_dumps(payload, **kwargs):
        if 'default' not in kwargs:
            kwargs['default'] = StatsStore.json_default
        return json.dumps(payload, **kwargs)

    @staticmethod
    def encode(stats):
        return json.dumps({'version': 1, 'stats': stats}, ensure_ascii=False, sort_keys=True, indent=2, default=StatsStore.json_default).encode('utf-8')

    def load(self, defaults):
        recovered = []
        for path in (self.path, self.backup):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                values = payload['stats']
                if payload.get('version') != 1 or not isinstance(values, dict):
                    raise ValueError('định dạng không hợp lệ')
                stats = dict(defaults)
                for key in defaults:
                    value = values.get(key, defaults[key])
                    if key=='rph_history':
                        from core.recent_rph import valid_history
                        if not valid_history(value):
                            self.log('[Stats] Lịch sử RPH không hợp lệ; giữ các tổng và dùng lịch sử dự phòng nếu có.')
                            value=[]
                    elif type(value) is not int or value < 0:
                        raise ValueError(f'chỉ số {key} không hợp lệ')
                    stats[key] = value
                recovered.append(stats)
                if path == self.backup:
                    if len(recovered) == 1:
                        self.log('[Stats] Đã khôi phục thống kê từ bản dự phòng.')
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.log(f'[Stats] Không đọc được {path.name}: {exc}')
                if path == self.path:
                    try:
                        self._atomic_write(self.path.with_suffix('.corrupt.json'), path.read_bytes())
                    except OSError:
                        pass
        stats = {key: self.combine(key,*(item[key] for item in recovered)) for key in defaults} if recovered else dict(defaults)
        if recovered:
            self._last_good = self.encode(stats)
            self.log(f"[Stats] Đã nạp {stats.get('battles_completed', 0)} trận, {stats.get('bases_scanned', 0)} nhà, "
                     f"{stats.get('uptime_seconds', 0)} giây từ {self.path}")
        else:
            self.log(f'[Stats] Chưa có dữ liệu đã lưu; tệp thống kê: {self.path}')
        return stats

    @staticmethod
    def _atomic_write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def _disk_lock(self):
        """Serialize read/merge/replace across overlapping tool processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+b') as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            deadline = time.monotonic() + 2
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, stats, *, reset_fastest=False):
        with self._lock, self._disk_lock():
            # Totals only increase; a fastest-time record only decreases. A
            # stale process must preserve both kinds of newer checkpoints.
            stats = dict(stats)
            if reset_fastest:
                stats['fastest_50_ms']=0
            for path in (self.path, self.backup):
                try:
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    if payload.get('version') != 1:
                        continue
                    for key, value in payload['stats'].items():
                        if reset_fastest and key=='fastest_50_ms':
                            continue
                        if key=='rph_history' and key in stats:
                            stats[key]=self.combine(key,stats[key],value)
                        elif key in stats and type(value) is int and value >= 0:
                            stats[key] = self.combine(key,stats[key],value)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    pass
            data = self.encode(stats)
            if not reset_fastest and data == self._last_good and self.path.exists():
                # Repair a corrupt primary after a successful backup recovery.
                try:
                    if self.path.read_bytes() == data:
                        if not self.backup.exists():
                            self._atomic_write(self.backup, data)
                        return stats
                except OSError:
                    pass
            if reset_fastest:
                self._atomic_write(self.backup,data)
            elif self._last_good is not None:
                self._atomic_write(self.backup, self._last_good)
            self._atomic_write(self.path, data)
            if self._last_good is None:
                self._atomic_write(self.backup, data)
            self._last_good = data
            return stats
