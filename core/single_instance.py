"""Keep one CoCAttack process per Windows desktop session."""

import ctypes
import sys


class SingleInstance:
    """A process-owned mutex plus an event to recall a hidden GUI window."""

    ERROR_ALREADY_EXISTS = 183
    WAIT_OBJECT_0 = 0

    def __init__(self, name='CoCAttack.SingleInstance.v1'):
        self._kernel = None
        self._mutex = None
        self._event = None
        self.acquired = True
        if sys.platform != 'win32':
            return

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_bool,
                                        ctypes.c_bool, ctypes.c_wchar_p)
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool,
                                        ctypes.c_wchar_p)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.SetEvent.argtypes = (ctypes.c_void_p,)
        kernel.SetEvent.restype = ctypes.c_bool
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel.WaitForSingleObject.restype = ctypes.c_uint
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_bool

        self._kernel = kernel
        self._event = kernel.CreateEventW(None, False, False, f'Local\\{name}.Restore')
        if not self._event:
            raise ctypes.WinError(ctypes.get_last_error())
        self._mutex = kernel.CreateMutexW(None, True, f'Local\\{name}.Mutex')
        if not self._mutex:
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)
        self.acquired = ctypes.get_last_error() != self.ERROR_ALREADY_EXISTS
        if not self.acquired:
            kernel.SetEvent(self._event)
            self.close()

    def restore_requested(self):
        return bool(self.acquired and self._event and self._kernel
                    and self._kernel.WaitForSingleObject(self._event, 0) == self.WAIT_OBJECT_0)

    def close(self):
        if self._kernel:
            if self._mutex:
                self._kernel.CloseHandle(self._mutex)
                self._mutex = None
            if self._event:
                self._kernel.CloseHandle(self._event)
                self._event = None
