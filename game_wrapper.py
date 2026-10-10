"""
GameWrapper - add this exe to Steam as a non-Steam game instead of the game itself.
It starts the launcher (exe 1), finds the game (exe 2) by path, keeps the Steam
shortcut "running" for exactly as long as the game lives, then exits.

Config: <this exe's name>.json next to the exe (log: <name>.log, overwritten each run).
Settings window opens when: no config, "ready": false, an exe is missing, or --config.
Windows 8.1+. No third-party dependencies.

Build (onedir, single process, no temp extraction):
    py -m venv venv
    venv\\Scripts\\python -m pip install -U pyinstaller
    venv\\Scripts\\pyinstaller --noconfirm --onedir --windowed --name GameWrapper game_wrapper.py
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import time

APP = "GameWrapper"
POLL_SEC = 1.0              # one cheap process snapshot per second
KILL_WAIT_MS = 10000        # wait for a killed process to actually exit
RELAUNCH_LOOP_SEC = 60      # game reappearing outside the chain this soon after a relaunch = loop
MAX_LOOP_ERRORS = 30

DEFAULTS = {
    "ready": False,
    "launcher_path": "",
    "launcher_args": "",
    "game_path": "",
    "grace_minutes": 2.0,
    "kill_launcher_on_start": False,
    "kill_launcher_on_exit": False,
    "relaunch_game_as_child": False,
}

log = logging.getLogger(APP)

# --------------------------------------------------------------------------- Win32

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
u32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
JOB_INFO_EXTENDED_LIMITS = 9
JOB_LIMIT_BREAKAWAY_OK = 0x0800
JOB_LIMIT_SILENT_BREAKAWAY_OK = 0x1000   # children never join our jobs -> Stop kills only exe1/exe2
JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
ERROR_ALREADY_EXISTS = 183
ERROR_ELEVATION_REQUIRED = 740
PROCESS_COMMAND_LINE_INFO = 60
MB_ICONERROR, MB_ICONINFO = 0x10, 0x40
MB_TOPMOST, MB_SETFOREGROUND = 0x40000, 0x10000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps",
                                                "ReadBytes", "WriteBytes", "OtherBytes")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]


def _sig(fn, args, res):
    fn.argtypes, fn.restype = args, res


_sig(k32.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
_sig(k32.CloseHandle, [wt.HANDLE], wt.BOOL)
_sig(k32.WaitForSingleObject, [wt.HANDLE, wt.DWORD], wt.DWORD)
_sig(k32.TerminateProcess, [wt.HANDLE, wt.UINT], wt.BOOL)
_sig(k32.QueryFullProcessImageNameW, [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL)
_sig(k32.CreateToolhelp32Snapshot, [wt.DWORD, wt.DWORD], wt.HANDLE)
_sig(k32.Process32FirstW, [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wt.BOOL)
_sig(k32.Process32NextW, [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wt.BOOL)
_sig(k32.CreateJobObjectW, [ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE)
_sig(k32.SetInformationJobObject, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL)
_sig(k32.AssignProcessToJobObject, [wt.HANDLE, wt.HANDLE], wt.BOOL)
_sig(k32.CreateMutexW, [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR], wt.HANDLE)
_sig(k32.GetModuleHandleW, [wt.LPCWSTR], wt.HMODULE)
_sig(ntdll.NtQueryInformationProcess,
     [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)], ctypes.c_long)
_sig(u32.MessageBoxW, [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT], ctypes.c_int)


def msgbox(text, icon=MB_ICONERROR):
    u32.MessageBoxW(None, text, APP, icon | MB_TOPMOST | MB_SETFOREGROUND)


def snapshot():
    """[(pid, exe_name_lower)] for every running process. One syscall, no per-process opens."""
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, INVALID_HANDLE_VALUE):
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.szExeFile.lower()))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def open_proc(pid):
    """(handle, can_control). Falls back to watch-only access for elevated/protected processes."""
    base = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    h = k32.OpenProcess(base | PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
    if h:
        return h, True
    return k32.OpenProcess(base, False, pid), False


def image_path(h):
    buf = ctypes.create_unicode_buffer(32768)
    size = wt.DWORD(len(buf))
    return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else None


def get_cmdline(h):
    size = wt.ULONG(0)
    ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFO, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = (ctypes.c_byte * size.value)()
    if ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFO, buf, size.value, ctypes.byref(size)) != 0:
        return None
    us = UNICODE_STRING.from_buffer(buf)
    return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length else None


def set_job_kill(job, kill):
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (JOB_LIMIT_BREAKAWAY_OK | JOB_LIMIT_SILENT_BREAKAWAY_OK
                                             | (JOB_LIMIT_KILL_ON_JOB_CLOSE if kill else 0))
    return bool(k32.SetInformationJobObject(job, JOB_INFO_EXTENDED_LIMITS,
                                            ctypes.byref(info), ctypes.sizeof(info)))


def overlay_module():
    for name in ("GameOverlayRenderer64.dll", "GameOverlayRenderer.dll"):
        if k32.GetModuleHandleW(name):
            return name
    return None


def norm(p):
    try:
        return os.path.normcase(os.path.realpath(p))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(p))


# --------------------------------------------------------------------------- config

def base_paths():
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    folder = os.path.dirname(exe)
    stem = os.path.splitext(os.path.basename(exe))[0]
    return folder, stem, os.path.join(folder, stem + ".json"), os.path.join(folder, stem + ".log")


def _coerce(val, default):
    if isinstance(default, bool):
        return val if isinstance(val, bool) else default
    if isinstance(default, float):
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
            return float(val)
        return default
    return val if isinstance(val, str) else default


def load_config(path):
    """(cfg, error_text, needs_rewrite). Never modifies the file."""
    cfg = dict(DEFAULTS)
    if not os.path.isfile(path):
        return cfg, None, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        return cfg, f"Config unreadable ({e}). Saving will overwrite it.", False
    for k, d in DEFAULTS.items():
        if k in data:
            cfg[k] = _coerce(data[k], d)
    needs_rewrite = any(k not in data for k in DEFAULTS)
    return cfg, None, needs_rewrite


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: cfg[k] for k in DEFAULTS}, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- tracking

class Proc:
    __slots__ = ("pid", "kind", "handle", "control", "own", "checked")

    def __init__(self, pid, kind, handle, control, own):
        self.pid, self.kind, self.handle, self.control, self.own = pid, kind, handle, control, own
        self.checked = False


class Tracker:
    """Finds exe1/exe2 by full path. Each found process gets its own kill-on-close job,
    so if this wrapper is killed (Steam Stop) Windows kills exactly those processes."""

    def __init__(self, launcher_path, game_path):
        self.paths = {"launcher": launcher_path, "game": game_path}
        self.targets = {k: norm(p) for k, p in self.paths.items()}
        self.names = {k: {os.path.basename(p).lower(), os.path.basename(self.targets[k]).lower()}
                      for k, p in self.paths.items()}
        self.procs = {}       # pid -> Proc
        self.rejected = {}    # pid -> exe name: same name but other path / no access; skip until pid dies
        self.jobs = []        # kept until exit: closing a job early would kill its process
        self.popens = []

    def kind_for(self, npath):
        if npath == self.targets["game"]:   # game wins if both paths are the same exe
            return "game"
        if npath == self.targets["launcher"]:
            return "launcher"
        return None

    def _add(self, pid, kind, h, control, own):
        job = None
        if control:
            job = k32.CreateJobObjectW(None, None)
            if job and not (set_job_kill(job, True) and k32.AssignProcessToJobObject(job, h)):
                log.warning("PID %d: job assign failed (err %d) - won't be auto-killed on Stop",
                            pid, ctypes.get_last_error())
                k32.CloseHandle(job)
                job = None
        if job:
            self.jobs.append(job)
        p = Proc(pid, kind, h, control, own)
        self.procs[pid] = p
        log.info("Tracking %s PID %d%s%s", kind, pid, " (started by us)" if own else "",
                 "" if job else " (watch only)")
        return p

    def adopt(self, pid, kind, own):
        p = self.procs.get(pid)
        if p:
            p.own = p.own or own
            return p
        h, control = open_proc(pid)
        if not h:
            log.warning("Can't open PID %d", pid)
            return None
        return self._add(pid, kind, h, control, own)

    def drop(self, p):
        self.procs.pop(p.pid, None)
        k32.CloseHandle(p.handle)

    def scan(self):
        for p in list(self.procs.values()):
            if k32.WaitForSingleObject(p.handle, 0) == WAIT_OBJECT_0:
                log.info("%s PID %d exited", p.kind, p.pid)
                self.drop(p)
        seen = set()
        all_names = self.names["launcher"] | self.names["game"]
        for pid, name in snapshot():
            seen.add(pid)
            if pid in self.procs or name not in all_names or self.rejected.get(pid) == name:
                continue
            h, control = open_proc(pid)
            if not h:
                log.warning("PID %d (%s): access denied, ignoring", pid, name)
                self.rejected[pid] = name
                continue
            path = image_path(h)
            kind = self.kind_for(norm(path)) if path else None
            if kind is None:
                k32.CloseHandle(h)
                self.rejected[pid] = name
                continue
            self._add(pid, kind, h, control, own=False)
        for pid in [p for p in self.rejected if p not in seen]:
            del self.rejected[pid]

    def alive(self, kind):
        return [p for p in self.procs.values()
                if p.kind == kind and k32.WaitForSingleObject(p.handle, 0) != WAIT_OBJECT_0]

    def terminate(self, p):
        if not p.control:
            log.warning("Can't terminate %s PID %d (no access - elevated?)", p.kind, p.pid)
            return False
        k32.TerminateProcess(p.handle, 1)
        k32.WaitForSingleObject(p.handle, KILL_WAIT_MS)
        log.info("Killed %s PID %d", p.kind, p.pid)
        self.drop(p)
        return True

    def start(self, kind, cmd):
        """Start exe as our child so Steam's hook follows it. Returns True if running."""
        path = self.paths[kind]
        cwd = os.path.dirname(path)
        p = subprocess.Popen(cmd, executable=path, cwd=cwd)
        self.popens.append(p)
        self.adopt(p.pid, self.kind_for(self.targets[kind]) or kind, own=True)
        return True

    def relaunch(self, p):
        if not p.control:
            log.warning("Game PID %d: no access, not relaunching", p.pid)
            return False
        cmd = get_cmdline(p.handle)
        if not cmd:
            log.warning("Game PID %d: can't read command line, not relaunching", p.pid)
            return False
        log.info("Relaunching game PID %d as our child (cmdline %d chars, not logged)", p.pid, len(cmd))
        self.terminate(p)
        try:
            return self.start("game", cmd)
        except OSError as e:
            log.error("Relaunch failed: %s", e)
            return False

    def finish(self, kill_launcher):
        for job in self.jobs:          # disarm: from here on only explicit kills
            set_job_kill(job, False)
        if kill_launcher:
            for p in self.alive("launcher"):
                self.terminate(p)
        for p in list(self.procs.values()):
            self.drop(p)
        for job in self.jobs:
            k32.CloseHandle(job)
        self.jobs.clear()


# --------------------------------------------------------------------------- run mode

def start_launcher(cfg, tr):
    path, args = cfg["launcher_path"], cfg["launcher_args"].strip()
    cmd = f'"{path}" {args}'.rstrip()
    try:
        tr.start("launcher", cmd)
        log.info("Started launcher")
        return True
    except OSError as e:
        if getattr(e, "winerror", None) == ERROR_ELEVATION_REQUIRED:
            log.warning("Launcher requires admin: starting via UAC (it will be outside Steam's chain)")
            try:
                os.startfile(path, arguments=args, cwd=os.path.dirname(path))
                return True
            except OSError as e2:
                e = e2
        log.error("Can't start launcher: %s", e)
        msgbox(f"Can't start launcher:\n{path}\n\n{e}")
        return False


def watch(cfg, tr):
    grace = cfg["grace_minutes"] * 60.0
    relaunch_on = cfg["relaunch_game_as_child"]
    game_seen = False
    launcher_last = game_last = time.monotonic()
    last_relaunch = None
    prev = None
    errors = 0
    while True:
        try:
            tr.scan()
            now = time.monotonic()

            if relaunch_on:
                for p in tr.alive("game"):
                    if p.own or p.checked:
                        continue
                    p.checked = True
                    if last_relaunch is not None and now - last_relaunch < RELAUNCH_LOOP_SEC:
                        log.warning("Game came back outside the chain right after a relaunch - "
                                    "relaunch loop, disabled for this session")
                        relaunch_on = False
                        break
                    if tr.relaunch(p):
                        last_relaunch = now

            games, launchers = len(tr.alive("game")), len(tr.alive("launcher"))
            if (games, launchers) != prev:
                log.info("State: game=%d launcher=%d", games, launchers)
                prev = (games, launchers)

            if games:
                if not game_seen:
                    log.info("Game detected - from now on only the game decides when to exit")
                    game_seen = True
                game_last = now
            elif not game_seen:
                if launchers:
                    launcher_last = now
                elif now - launcher_last >= grace:
                    log.info("No launcher and no game for %.1f min - exiting", grace / 60)
                    return
            elif now - game_last >= grace:
                log.info("Game gone for %.1f min - exiting", grace / 60)
                return
            errors = 0
        except Exception:
            errors += 1
            log.exception("Loop error %d/%d", errors, MAX_LOOP_ERRORS)
            if errors >= MAX_LOOP_ERRORS:
                raise
        time.sleep(POLL_SEC)


def run(cfg):
    machine = platform.machine()
    log.info("Arch: %s | Steam overlay in wrapper: %s", machine,
             overlay_module() or "NO (not started from Steam, or overlay disabled)")
    if machine.upper() == "ARM64":
        log.warning("Native ARM64 build: x64 Steam can't inject into it - use the x64 build")
    tr = Tracker(cfg["launcher_path"], cfg["game_path"])
    normal = False
    try:
        if cfg["kill_launcher_on_start"]:
            tr.scan()
            for p in tr.alive("launcher"):
                log.info("Kill on start: launcher PID %d", p.pid)
                tr.terminate(p)
        if start_launcher(cfg, tr):
            watch(cfg, tr)
            normal = True
    except Exception:
        log.exception("Fatal error - leaving launcher and game running")
    finally:
        # Any error path disarms the jobs, so a bug here never takes the game down with it.
        tr.finish(kill_launcher=normal and cfg["kill_launcher_on_exit"])
        log.info("Bye")


# --------------------------------------------------------------------------- settings GUI

def run_gui(cfg_path, stem, cfg, note):
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except Exception:
        pass
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title(f"{APP} - {stem}")
    root.resizable(True, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)

    v_launcher = tk.StringVar(value=cfg["launcher_path"])
    v_args = tk.StringVar(value=cfg["launcher_args"])
    v_game = tk.StringVar(value=cfg["game_path"])
    v_grace = tk.StringVar(value=f'{cfg["grace_minutes"]:g}')
    v_kstart = tk.BooleanVar(value=cfg["kill_launcher_on_start"])
    v_kexit = tk.BooleanVar(value=cfg["kill_launcher_on_exit"])
    v_relaunch = tk.BooleanVar(value=cfg["relaunch_game_as_child"])
    exe_dir = os.path.dirname(cfg_path)

    def browse(var):
        cur = os.path.dirname(var.get().strip())
        p = filedialog.askopenfilename(parent=root, initialdir=cur if os.path.isdir(cur) else exe_dir,
                                       filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if p:
            var.set(os.path.normpath(p))

    def path_row(row, label, var):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm, textvariable=var, width=70).grid(row=row, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(frm, text="Browse...", command=lambda: browse(var)).grid(row=row, column=2, padx=(6, 0), pady=(6, 0))
        status = ttk.Label(frm, foreground="#c0392b")
        status.grid(row=row + 1, column=1, sticky="w")

        def update(*_):
            p = var.get().strip()
            status.config(text="" if not p or os.path.isfile(p) else "Not found")
        var.trace_add("write", update)
        update()

    path_row(0, "Launcher (exe 1)", v_launcher)
    ttk.Label(frm, text="Launcher arguments").grid(row=2, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(frm, textvariable=v_args).grid(row=2, column=1, sticky="ew", pady=(6, 0))
    path_row(3, "Game (exe 2)", v_game)

    ttk.Label(frm, text="Grace period (minutes)").grid(row=5, column=0, sticky="w", pady=(10, 0))
    ttk.Spinbox(frm, from_=0, to=1440, increment=0.5, textvariable=v_grace, width=8).grid(
        row=5, column=1, sticky="w", pady=(10, 0))

    checks = [
        (v_kstart, "Kill launcher on start (forces a fresh instance inside Steam's chain)"),
        (v_kexit, "Kill launcher on exit"),
        (v_relaunch, "Relaunch game as child if it starts outside the chain (breaks anti-cheat games)"),
    ]
    for i, (var, text) in enumerate(checks):
        ttk.Checkbutton(frm, text=text, variable=var).grid(row=6 + i, column=0, columnspan=3, sticky="w",
                                                            pady=(6 if i == 0 else 2, 0))

    hint = note or "Add this exe to Steam as a non-Steam game. Settings reopen if an exe goes missing."
    ttk.Label(frm, text=hint, foreground="#c0392b" if note else "#666", wraplength=560).grid(
        row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def save():
        try:
            grace = float(v_grace.get().strip().replace(",", "."))
            if grace < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP, "Grace period must be a number >= 0.", parent=root)
            return
        lp, gp = v_launcher.get().strip(), v_game.get().strip()
        new = {
            "launcher_path": os.path.normpath(lp) if lp else "",
            "launcher_args": v_args.get().strip(),
            "game_path": os.path.normpath(gp) if gp else "",
            "grace_minutes": grace,
            "kill_launcher_on_start": bool(v_kstart.get()),
            "kill_launcher_on_exit": bool(v_kexit.get()),
            "relaunch_game_as_child": bool(v_relaunch.get()),
        }
        new["ready"] = bool(new["launcher_path"] and new["game_path"])
        try:
            save_config(cfg_path, new)
        except OSError as e:
            messagebox.showerror(APP, f"Can't write {cfg_path}\n\n{e}\n\nPut the wrapper in a folder "
                                      "you can write to.", parent=root)
            return
        if not new["ready"]:
            messagebox.showinfo(APP, "Saved. Both paths are required - settings will open again "
                                     "on next launch.", parent=root)
        root.destroy()

    ttk.Button(frm, text="Save", command=save).grid(row=10, column=2, sticky="e", pady=(12, 0))
    root.bind("<Return>", lambda _e: save())
    root.mainloop()


# --------------------------------------------------------------------------- main

def setup_logging(log_path):
    try:
        h = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    except OSError:
        h = logging.NullHandler()
    log.addHandler(h)
    log.setLevel(logging.INFO)


def main():
    _folder, stem, cfg_path, log_path = base_paths()
    cfg, err, needs_rewrite = load_config(cfg_path)

    missing = [p for p in (cfg["launcher_path"], cfg["game_path"]) if p and not os.path.isfile(p)]
    want_gui = "--config" in sys.argv[1:] or err or not cfg["ready"] or missing

    # One instance per config. Named after the config path, so different games never collide.
    ctypes.set_last_error(0)
    mutex = k32.CreateMutexW(None, False, "Local\\GameWrapper_" +
                             hashlib.sha1(os.path.normcase(cfg_path).encode()).hexdigest()[:16])
    if mutex and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if want_gui:
            msgbox(f"{stem} is already running.", MB_ICONINFO)
        return

    if needs_rewrite and not err:
        try:
            save_config(cfg_path, cfg)   # fill in missing keys, keep every user value
        except OSError:
            pass

    if want_gui:
        note = err or ("Not found: " + " | ".join(missing) if missing else None)
        run_gui(cfg_path, stem, cfg, note)
        return

    setup_logging(log_path)
    log.info("Config: %s", cfg_path)
    run(cfg)


if __name__ == "__main__":
    main()
