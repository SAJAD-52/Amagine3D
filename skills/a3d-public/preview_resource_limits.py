"""Install an OS-enforced per-process resource boundary for preview work."""
from __future__ import annotations

import ctypes
import importlib
import os
import subprocess


DEFAULT_PREVIEW_MEMORY_BUDGET_BYTES = 2 * 1024 * 1024 * 1024

# Import CAD/rendering libraries before measuring the baseline. Geometry loading
# and tessellation happen only after this fixed runtime footprint is bounded.
PREVIEW_BASELINE_MODULES = (
    "agent_preview",
    "display_step",
    "brep_measurements",
    "geometry_binding",
    "brep_tessellation",
    "export_audit",
)
_PROCESS_MEMORY_BOUNDARY: dict | None = None


class PreviewResourceLimitError(RuntimeError):
    """The preview process could not install its required OS resource limit."""


def _prepare_preview_runtime() -> None:
    for name in PREVIEW_BASELINE_MODULES:
        importlib.import_module(name)


def _posix_virtual_size_bytes() -> int:
    result = subprocess.run(
        ["ps", "-o", "vsz=", "-p", str(os.getpid())],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    value = int(result.stdout.strip().split()[0])
    if value <= 0:
        raise ValueError("ps returned an invalid virtual-size value")
    return value * 1024


def _posix_limit(memory_budget_bytes: int) -> dict:
    try:
        import resource
    except ImportError as error:
        raise PreviewResourceLimitError("OS address-space limits are unavailable") from error
    if not hasattr(resource, "RLIMIT_AS"):
        raise PreviewResourceLimitError("OS address-space limits are unavailable")
    try:
        baseline = _posix_virtual_size_bytes()
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        proposed = baseline + memory_budget_bytes
        effective_hard = proposed if hard == resource.RLIM_INFINITY else min(proposed, hard)
        effective_soft = proposed if soft == resource.RLIM_INFINITY else min(proposed, soft)
        effective_soft = min(effective_soft, effective_hard)
        if effective_soft <= baseline:
            raise PreviewResourceLimitError(
                "existing address-space limit leaves no room for bounded preview work"
            )
        resource.setrlimit(resource.RLIMIT_AS, (effective_soft, effective_hard))
        installed = resource.getrlimit(resource.RLIMIT_AS)
        if installed != (effective_soft, effective_hard):
            raise PreviewResourceLimitError("OS did not retain the preview address-space limit")
    except PreviewResourceLimitError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise PreviewResourceLimitError(
            f"could not install preview address-space limit: {error}"
        ) from error
    return {"kind": "address-space", "baselineBytes": baseline,
            "budgetBytes": memory_budget_bytes, "limitBytes": effective_soft,
            "availableBytes": max(0, effective_soft - baseline)}


def _windows_private_bytes() -> int:
    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_memory = psapi.GetProcessMemoryInfo
    get_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCountersEx), ctypes.c_ulong]
    get_memory.restype = ctypes.c_int
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_process = kernel32.GetCurrentProcess
    get_process.restype = ctypes.c_void_p
    if not get_memory(get_process(), ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return int(counters.PrivateUsage)


def _windows_limit(memory_budget_bytes: int) -> dict:
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    baseline = _windows_private_bytes()
    limit = baseline + memory_budget_bytes
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    information.ProcessMemoryLimit = limit
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    create_job.restype = ctypes.c_void_p
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    set_information.restype = ctypes.c_int
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    assign.restype = ctypes.c_int
    get_process = kernel32.GetCurrentProcess
    get_process.restype = ctypes.c_void_p
    close = kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    handle = create_job(None, None)
    if not handle:
        raise PreviewResourceLimitError("could not create a preview memory-limit job")
    try:
        if not set_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not assign(handle, get_process()):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    except OSError as error:
        raise PreviewResourceLimitError(f"could not install preview process-memory limit: {error}") from error
    finally:
        close(handle)
    return {"kind": "job-process-commit", "baselineBytes": baseline,
            "budgetBytes": memory_budget_bytes, "limitBytes": limit,
            # An enclosing Job Object may impose a stricter cap that this
            # process cannot query portably; do not claim the full budget is
            # available when nested under such a limit.
            "availableBytes": None}


def install_preview_memory_limit(memory_budget_bytes: int = DEFAULT_PREVIEW_MEMORY_BUDGET_BYTES) -> dict:
    """Bound memory growth after fixed CAD libraries load, before STEP loading.

    POSIX uses RLIMIT_AS, so the bound is virtual address space and is not a
    physical-RSS cap. Windows uses a Job Object per-process commit limit.
    Unsupported platforms fail closed rather than running unbounded previews.
    """
    if type(memory_budget_bytes) is not int or memory_budget_bytes < 1:
        raise ValueError("preview memory budget must be a positive integer")
    global _PROCESS_MEMORY_BOUNDARY
    _prepare_preview_runtime()
    if os.name == "nt":
        _PROCESS_MEMORY_BOUNDARY = _windows_limit(memory_budget_bytes)
    elif os.name == "posix":
        _PROCESS_MEMORY_BOUNDARY = _posix_limit(memory_budget_bytes)
    else:
        raise PreviewResourceLimitError("this platform has no supported preview memory boundary")
    return {**_PROCESS_MEMORY_BOUNDARY, "baselineModules": list(PREVIEW_BASELINE_MODULES)}


def process_memory_boundary() -> dict | None:
    return dict(_PROCESS_MEMORY_BOUNDARY) if _PROCESS_MEMORY_BOUNDARY else None
