"""All Win32 / NT native access, centralised behind a small typed API.

Every ctypes call in the project lives here so the higher-level modules
(detection, throttle, daemon) stay pure Python logic. Functions never raise on
ordinary failure -- they return None / False / [] and the caller decides what to
do. This keeps the daemon crash-resistant by construction.

64-bit safety: HANDLE/HWND restypes and argtypes are declared so pointers are
not silently truncated to 32 bits.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

from .models import ProcInfo

# ---------------------------------------------------------------------------
# DLL handles
# ---------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_INFORMATION = 0x0200
PROCESS_SUSPEND_RESUME = 0x0800

# Priority classes (winbase.h)
IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100

PRIORITY_NAMES = {
    IDLE_PRIORITY_CLASS: "IDLE",
    BELOW_NORMAL_PRIORITY_CLASS: "BELOW_NORMAL",
    NORMAL_PRIORITY_CLASS: "NORMAL",
    ABOVE_NORMAL_PRIORITY_CLASS: "ABOVE_NORMAL",
    HIGH_PRIORITY_CLASS: "HIGH",
    REALTIME_PRIORITY_CLASS: "REALTIME",
}

# SetProcessInformation: ProcessPowerThrottling
_ProcessPowerThrottling = 4
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1


class _PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("ControlMask", wintypes.DWORD),
        ("StateMask", wintypes.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


# ---------------------------------------------------------------------------
# Function prototypes (declare arg/restypes for 64-bit correctness)
# ---------------------------------------------------------------------------
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.SetPriorityClass.restype = wintypes.BOOL
_kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
_kernel32.GetPriorityClass.restype = wintypes.DWORD
_kernel32.SetProcessInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
_kernel32.SetProcessInformation.restype = wintypes.BOOL
# GetProcessInformation is best-effort (used only for verification read-back).
try:
    _kernel32.GetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.GetProcessInformation.restype = wintypes.BOOL
    _HAVE_GET_PROCESS_INFO = True
except AttributeError:  # not present on this OS
    _HAVE_GET_PROCESS_INFO = False

_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
_ntdll.NtSuspendProcess.restype = ctypes.c_long  # NTSTATUS
_ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
_ntdll.NtResumeProcess.restype = ctypes.c_long


# ---------------------------------------------------------------------------
# Process enumeration
# ---------------------------------------------------------------------------
def enum_processes() -> list[ProcInfo]:
    """Return every process currently visible to us as ProcInfo (name lower-cased)."""
    out: list[ProcInfo] = []
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(
                ProcInfo(
                    pid=int(entry.th32ProcessID),
                    name=str(entry.szExeFile).lower(),
                    ppid=int(entry.th32ParentProcessID),
                )
            )
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def get_process_name(pid: int) -> Optional[str]:
    """Look up a single pid's exe name (lower-cased), or None if not found."""
    if pid <= 0:
        return None
    for p in enum_processes():
        if p.pid == pid:
            return p.name
    return None


def get_process_tree(root_pid: int, procs: Optional[list[ProcInfo]] = None) -> set[int]:
    """Return root_pid plus all transitive descendants (best effort)."""
    if root_pid <= 0:
        return set()
    if procs is None:
        procs = enum_processes()
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)
    tree: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in tree:
            continue
        tree.add(pid)
        stack.extend(children.get(pid, []))
    return tree


# ---------------------------------------------------------------------------
# Foreground window
# ---------------------------------------------------------------------------
def get_foreground_pid() -> int:
    """Return the pid owning the current foreground window, or 0."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------
def get_priority_class(pid: int) -> Optional[int]:
    """Return the process priority class constant, or None on failure."""
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        val = _kernel32.GetPriorityClass(h)
        return int(val) if val else None
    finally:
        _kernel32.CloseHandle(h)


def set_priority_class(pid: int, priority_class: int) -> bool:
    """Set a process priority class. Returns True on success."""
    h = _kernel32.OpenProcess(
        PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return False
    try:
        return bool(_kernel32.SetPriorityClass(h, priority_class))
    finally:
        _kernel32.CloseHandle(h)


# ---------------------------------------------------------------------------
# EcoQoS (power throttling)
# ---------------------------------------------------------------------------
def _set_power_throttling(pid: int, enable: bool) -> bool:
    h = _kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    if not h:
        return False
    try:
        state = _PROCESS_POWER_THROTTLING_STATE()
        state.Version = _PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = _PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        # enable -> StateMask set (throttle); disable -> StateMask 0 (system-managed)
        state.StateMask = _PROCESS_POWER_THROTTLING_EXECUTION_SPEED if enable else 0
        ok = _kernel32.SetProcessInformation(
            h, _ProcessPowerThrottling, ctypes.byref(state), ctypes.sizeof(state)
        )
        return bool(ok)
    finally:
        _kernel32.CloseHandle(h)


def enable_ecoqos(pid: int) -> bool:
    """Enable EcoQoS (EXECUTION_SPEED throttling) for a process."""
    return _set_power_throttling(pid, True)


def disable_ecoqos(pid: int) -> bool:
    """Return a process to system-managed power throttling (undo EcoQoS)."""
    return _set_power_throttling(pid, False)


def query_ecoqos(pid: int) -> Optional[bool]:
    """Best-effort read-back of EcoQoS state (verification aid).

    Returns True if EXECUTION_SPEED throttling is currently enabled, False if
    not, or None if the OS does not support querying this (no documented
    read-back on all builds) so the caller can fall back to setter-success.
    """
    if not _HAVE_GET_PROCESS_INFO:
        return None
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        state = _PROCESS_POWER_THROTTLING_STATE()
        state.Version = _PROCESS_POWER_THROTTLING_CURRENT_VERSION
        ok = _kernel32.GetProcessInformation(
            h, _ProcessPowerThrottling, ctypes.byref(state), ctypes.sizeof(state)
        )
        if not ok:
            return None
        enabled = bool(state.ControlMask & _PROCESS_POWER_THROTTLING_EXECUTION_SPEED
                       and state.StateMask & _PROCESS_POWER_THROTTLING_EXECUTION_SPEED)
        return enabled
    finally:
        _kernel32.CloseHandle(h)


# ---------------------------------------------------------------------------
# Suspend / resume (HARD tier)
# ---------------------------------------------------------------------------
def suspend_process(pid: int) -> bool:
    """Suspend all threads of a process via NtSuspendProcess. True on success."""
    h = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return False
    try:
        status = _ntdll.NtSuspendProcess(h)
        return status == 0  # STATUS_SUCCESS
    finally:
        _kernel32.CloseHandle(h)


def resume_process(pid: int) -> bool:
    """Resume a suspended process via NtResumeProcess. True on success."""
    h = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return False
    try:
        status = _ntdll.NtResumeProcess(h)
        return status == 0
    finally:
        _kernel32.CloseHandle(h)


def process_alive(pid: int) -> bool:
    """Cheap liveness check: can we open the process at all?"""
    if pid <= 0:
        return False
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    _kernel32.CloseHandle(h)
    return True


# ---------------------------------------------------------------------------
# Performance counters (for the metrics sampler)
# ---------------------------------------------------------------------------
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


def _ft64(ft: "_FILETIME") -> int:
    """Combine a FILETIME (two DWORDs) into a single 64-bit count of 100ns ticks."""
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32.GetSystemTimes.argtypes = [
    ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
]
_kernel32.GetSystemTimes.restype = wintypes.BOOL
_kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
_kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
_kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
]
_kernel32.GetProcessTimes.restype = wintypes.BOOL
# K32GetProcessMemoryInfo lives in kernel32 on Windows 7+ (no psapi.dll needed).
_kernel32.K32GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD,
]
_kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL


def get_system_times() -> Optional[tuple[int, int, int]]:
    """Return (idle, kernel, user) cumulative system times in 100ns ticks.

    NOTE: per Win32, `kernel` INCLUDES `idle`, and all three are summed across
    every logical processor. System busy fraction over an interval is therefore
    (deltaKernel + deltaUser - deltaIdle) / (deltaKernel + deltaUser).
    """
    idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not _kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
    ):
        return None
    return _ft64(idle), _ft64(kern), _ft64(user)


def get_memory_status() -> Optional[dict]:
    """System memory snapshot in bytes (plus the OS's own 0-100 memory load)."""
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return {
        "memory_load_pct": int(m.dwMemoryLoad),
        "total_phys": int(m.ullTotalPhys),
        "avail_phys": int(m.ullAvailPhys),
        "total_pagefile": int(m.ullTotalPageFile),
        "avail_pagefile": int(m.ullAvailPageFile),
        # Commit charge currently in use = committable total - still-available.
        "committed": int(m.ullTotalPageFile) - int(m.ullAvailPageFile),
    }


def get_process_cpu_ticks(pid: int) -> Optional[int]:
    """Cumulative (kernel+user) CPU time for a process, in 100ns ticks; None on fail."""
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        creation, exit_, kern, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not _kernel32.GetProcessTimes(
            h, ctypes.byref(creation), ctypes.byref(exit_),
            ctypes.byref(kern), ctypes.byref(user)
        ):
            return None
        return _ft64(kern) + _ft64(user)
    finally:
        _kernel32.CloseHandle(h)


def get_process_working_set(pid: int) -> Optional[int]:
    """Current working-set size (resident memory) of a process in bytes; None on fail."""
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if not _kernel32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        return int(pmc.WorkingSetSize)
    finally:
        _kernel32.CloseHandle(h)
