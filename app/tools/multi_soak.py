"""Two controllers at once: N threads in one process, or N processes? Measured.

`BridgeManager` has to own more than one bridge, and there are only two shapes
it can take. The choice is not stylistic and it cannot be reasoned out from
first principles, because the thing that decides it is the GIL:

  * each bridge already runs four threads plus an asyncio loop, holds a 1 ms
    isochronous service interval, and does Opus encode/decode and resampling;
  * STATUS.md 16.6 gotcha #9 measured a single 1 ms pure-Python pacing loop
    starving the Bluetooth reader thread badly enough to halve the microphone
    rate -- 99.8 payloads/s at a 4 ms tick, 52.7/s at 1 ms;
  * STATUS.md 17.8 trap 10 says `asyncio.to_thread` is not an escape hatch,
    because the GIL is held by the C call either way.

So doubling that load inside one interpreter may or may not hold the timing,
and the honest way to find out is to run both shapes on real hardware and read
the counters. That is this file.

    cd app
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\multi_soak.py ^
        --serials d42f4ba1485d,a0fa9c0dd8bb --seconds 120 --mode both ^
        --jsonl C:\\Temp\\ds5-multi.jsonl

What is compared
----------------
  (a) `--mode inproc`  two `BridgeService` objects in THIS process, on ports
      3241 and 3242. This is what `service.py`'s module docstring prefers on
      principle: the server runs in-process precisely so that STATUS.md 17.8
      traps 7 and 8 (a stale child whose command line cannot be read, and one
      that cannot be killed from a later shell) cannot happen at all.
  (b) `--mode child`   two child processes, one `BridgeService` each, held in a
      Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so that the
      children cannot outlive this harness even if it is hard-killed. That
      guard is the price of admission for the multi-process shape; it is
      implemented here so the shape is measured with its real cost included.

THE GAME LOAD IS A THIRD PROCESS IN BOTH CONFIGURATIONS. A 250 Hz reader on
each virtual HID device stands in for a game. Putting those readers in the
bridge process would have loaded configuration (a) with work configuration (b)
does not carry, and the comparison would have been rigged. They live in one
separate child either way.

WHY NO AUDIO BY DEFAULT. `app/tools/soak.py` explains it: the Phase 3c
full-duplex soak cost about 2.5 %/min of battery. Two controllers, two
configurations and 120 s each is 8 minutes of streaming, which the 10 % unit
cannot survive -- and a run that ends in a flat controller measures the
battery, not the architecture. `--audio` is there for a short full-duplex run
on two charged units. Everything the audio path counts (underruns, queue
drops, ring overflow, Opus/0x39 errors, mic decode errors) is recorded either
way, so a zero in those columns means "not exercised", not "clean". `--audio`
plays a tone into every virtual render endpoint from the reader ("game")
process, which is what drives the 1 ms isochronous OUT path, the Opus encode
and the 0x39 report writes -- the load the whole GIL question is about. Run it
on two charged units and keep it short.

ONE THING THE AUDIO COLUMNS CANNOT TELL YOU, measured 2026-08-25: Windows makes
a freshly attached virtual DualSense the default render endpoint and streams
whatever it feels like into it. So `audio_out_calls` and `reports_39` come back
non-zero on a run that drove no audio at all, the load is not the same twice,
and the underrun / queue-drop columns moved in BOTH directions between two
otherwise identical runs. Treat them as uncontrolled unless `--audio` is on.

Counters come from `BridgeBackend.stats` and `BridgeBackend.clock_seam()`, the
same ones `emulator/tools/e2e_soak.py` and `bridge_input_soak.py` read. Nothing
new is invented here.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parent
for _d in (str(APP_DIR), str(REPO / "emulator")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from ds5app import service as S            # noqa: E402
from ds5app.usbip import Usbip             # noqa: E402

#: usbipd-win owns 3240. Ours start here and count upward.
BASE_PORT = 3241


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_json(path: str, obj: dict) -> None:
    """Publish a stats snapshot without ever showing a reader half a file.

    A partially written JSON file read by the parent looks exactly like a dead
    child, and a soak that reports "child stopped answering" at second 61 when
    it did not is worse than no measurement.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def stop_path(stats_file: str) -> str:
    return stats_file + ".stop"


def ask_to_stop(stats_file: str) -> None:
    """The out-of-band "please shut down" channel a child bridge needs.

    There is no polite kill on Windows. `Popen.terminate()` is
    `TerminateProcess`, which runs no atexit hook and no console handler, so a
    bridge child killed that way leaves the device attached and the
    auto-re-attach armed (STATUS.md 18.3(1)) -- the exact state
    `ds5bridge cleanup` exists to rescue. A file the child polls is crude and
    completely reliable, which is what a teardown path has to be. Note this
    for the multi-process architecture: it needs a channel like this, and the
    in-process one needs nothing at all.
    """
    try:
        open(stop_path(stats_file), "w").close()
    except OSError:
        pass


def clear_stop(stats_file: str) -> None:
    """Remove BOTH the stop flag and any stats file left by an earlier run.

    Leaving the old stats file behind was a real bug in this harness and a
    quiet one: `wait_for_reader` saw the PREVIOUS configuration's report count,
    declared the reader warm before it had opened a single handle, and took its
    baseline from it. The second configuration then reported a NEGATIVE report
    rate. A stale file that parses is far more dangerous than one that does
    not.
    """
    for f in (stop_path(stats_file), stats_file):
        try:
            os.remove(f)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the job object -- configuration (b)'s orphan guard
# ---------------------------------------------------------------------------


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOB_BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOB_EXTENDED(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOB_BASIC),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


def create_kill_on_close_job():
    """A job handle whose closure kills everything in it. -> handle or None.

    This is the ONLY thing that makes a child-process architecture as safe as
    the in-process one. `atexit`, signal handlers and `SetConsoleCtrlHandler`
    all assume the parent gets to run code on its way out; a `taskkill /F` on
    the parent gives it none, and what is left is STATUS.md 17.8 trap 7 exactly
    -- a bridge still holding TCP 3241 whose command line the next shell cannot
    read. The kernel closes every handle of a killed process, so the job
    closes, so the children die. No cooperation required.
    """
    if sys.platform != "win32":
        return None
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    h = k32.CreateJobObjectW(None, None)
    if not h:
        return None
    info = _JOB_EXTENDED()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(ctypes.c_void_p(h),
                                     _JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        k32.CloseHandle(ctypes.c_void_p(h))
        return None
    return h


def assign_to_job(job, proc: subprocess.Popen) -> bool:
    """Put a freshly spawned child in the job.

    Immediately after `Popen` returns, deliberately: the child is still inside
    the ntdll loader and has not run a line of its own code, so it cannot have
    spawned a grandchild that would escape the job.
    """
    if job is None or sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(int(proc._handle))))
    except Exception:  # noqa: BLE001
        return False


def close_job(job) -> None:
    if job is not None and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# the "game": a 250 Hz reader per virtual device, in its own process
# ---------------------------------------------------------------------------


def usb_hid_paths() -> set[str]:
    """Every DualSense HID path that is NOT a Bluetooth one.

    The `00001124` fragment is the Bluetooth HID service GUID and is what tells
    the two physical controllers apart from the virtual devices. Note that the
    two VIRTUAL devices are indistinguishable from each other here: nothing in
    the emulated descriptor set carries the Bluetooth serial, so a path can
    only be attributed to a controller by starting the bridges one at a time
    and watching which path appears.
    """
    import hid
    out = set()
    for d in hid.enumerate(0x054C, 0x0CE6):
        p = d["path"].decode("latin1") if isinstance(d["path"], bytes) else d["path"]
        if "00001124" not in p:
            out.add(p)
    return out


class GameReader(threading.Thread):
    """One virtual device, read as a game reads it. Running statistics only.

    Bounded on purpose (STATUS.md 17.8 trap 10): a 120 s run is 30 000 reports
    per controller and a structure that grows and then gets summarised is the
    instrumentation trap that phase measured.
    """

    def __init__(self, serial: str, path: str):
        super().__init__(name=f"reader-{serial}", daemon=True)
        self.serial = serial
        self.path = path
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.reports = self.empty = self.errors = self.seq_breaks = 0
        self.fatal: str | None = None
        self.gaps: deque = deque(maxlen=20_000)
        self.gap_max = 0.0
        self._last_t: float | None = None
        self._last_seq: int | None = None

    def run(self) -> None:
        import hid
        from ds5bridge import protocol as P
        d = hid.device()
        try:
            d.open_path(self.path.encode("latin1"))
        except Exception as e:  # noqa: BLE001
            self.fatal = f"open failed: {e}"
            return
        try:
            while not self.stop_flag.is_set():
                try:
                    raw = bytes(d.read(64, timeout_ms=50))
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    if self.errors > 500:
                        self.fatal = f"read failed {self.errors}x: {e}"
                        return
                    continue
                if not raw:
                    self.empty += 1
                    continue
                now = time.perf_counter()
                seq = raw[1 + P.OFFSETS_USB.sequence_num]
                with self.lock:
                    self.reports += 1
                    if self._last_t is not None:
                        gap = now - self._last_t
                        self.gaps.append(gap)
                        self.gap_max = max(self.gap_max, gap)
                    if self._last_seq is not None and \
                            seq != (self._last_seq + 1) & 0xFF:
                        self.seq_breaks += 1
                    self._last_t = now
                    self._last_seq = seq
        finally:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass

    def take(self) -> dict:
        with self.lock:
            gaps = sorted(self.gaps)
            out = {"reports": self.reports, "empty": self.empty,
                   "errors": self.errors, "seq_breaks": self.seq_breaks,
                   "gap_ms_max": round(self.gap_max * 1e3, 3),
                   "fatal": self.fatal}
        if len(gaps) > 10:
            out["gap_ms_median"] = round(statistics.median(gaps) * 1e3, 3)
            out["gap_ms_p99"] = round(gaps[int(len(gaps) * 0.99)] * 1e3, 3)
        return out


def start_audio(n_expected: int) -> list:
    """Play a looped tone into every virtual DualSense render endpoint.

    Attribution is impossible here for the same reason as the HID paths -- the
    emulated descriptors carry no serial -- but it does not need to be: the
    point is that BOTH virtual devices have their isochronous OUT endpoint
    driven, and the underrun/queue-drop counters are per-backend anyway.

    A callback stream per device, not `sounddevice.play()`: `play` drives one
    global stream, so a second call would silently move the tone off the first
    controller and the run would measure one audio path, not two.

    Returns the open streams; an empty list means the endpoints were not found
    and the run is HID-only. That is reported, never quietly recorded as a
    clean audio result.
    """
    import numpy as np
    import sounddevice as sd
    want = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] >= 2 and "dualsense" in d["name"].lower():
            want.append(i)
    if len(want) < n_expected:
        say(f"  audio: found {len(want)} DualSense render endpoints, "
            f"wanted {n_expected} -- running HID-only")
        return []
    sr = 48000
    t = np.arange(sr, dtype=np.float32) / sr             # a 1 s loop
    tone = np.repeat((0.35 * np.sin(2 * np.pi * 1500.0 * t)).astype(np.float32)
                     .reshape(-1, 1), 2, axis=1)
    streams = []
    for i in want[:n_expected]:
        pos = [0]

        def cb(outdata, frames, tinfo, status, _pos=pos):  # noqa: ARG001
            n = len(tone)
            a = _pos[0]
            idx = (np.arange(frames) + a) % n
            outdata[:] = tone[idx]
            _pos[0] = (a + frames) % n

        st = sd.OutputStream(device=i, samplerate=sr, channels=2,
                             dtype="float32", latency="low", callback=cb)
        st.start()
        streams.append(st)
    say(f"  audio: driving {len(streams)} render endpoints with a 1.5 kHz tone")
    return streams


def stop_audio(streams: list) -> None:
    for st in streams:
        try:
            st.stop()
            st.close()
        except Exception:  # noqa: BLE001
            pass


def run_reader(job_file: str, stats_file: str) -> int:
    """`--reader` child: open every path in the job file and read until told."""
    job = read_json(job_file)
    paths: dict[str, str] = job.get("paths", {})
    seconds = float(job.get("seconds", 120.0))
    audio = []
    if job.get("audio"):
        try:
            audio = start_audio(len(paths))
        except Exception as e:  # noqa: BLE001
            say(f"  audio setup failed: {e}")
    readers = [GameReader(s, p) for s, p in paths.items()]
    for r in readers:
        r.start()
    # The +60 s is a dead-man cap for a parent that died, not the normal exit.
    end = time.perf_counter() + seconds + 60.0
    try:
        while time.perf_counter() < end and not os.path.exists(stop_path(stats_file)):
            write_json(stats_file, {
                "t": time.time(),
                "cpu_s": round(time.process_time(), 3),
                "controllers": {r.serial: r.take() for r in readers}})
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop_audio(audio)
        for r in readers:
            r.stop_flag.set()
        for r in readers:
            r.join(timeout=5.0)
        write_json(stats_file, {
            "t": time.time(), "done": True,
            "cpu_s": round(time.process_time(), 3),
            "controllers": {r.serial: r.take() for r in readers}})
    return 0


# ---------------------------------------------------------------------------
# one bridge, in a child process -- configuration (b)
# ---------------------------------------------------------------------------


def bridge_sample(svc, cpu_s: float) -> dict:
    be = svc._backend
    out = {"t": time.time(), "state": svc.state, "serial": svc.serial,
           "port": svc.port, "cpu_s": round(cpu_s, 3),
           "error": svc.error, "stats": {}, "seam": {}, "device": {}}
    if be is not None:
        out["stats"] = dict(be.stats)
        try:
            out["seam"] = be.clock_seam()
        except Exception:  # noqa: BLE001
            pass
        try:
            out["device"] = be.device_status()
        except Exception:  # noqa: BLE001
            pass
    return out


def run_bridge_child(serial: str, port: int, seconds: float,
                     stats_file: str) -> int:
    """`--bridge` child: one BridgeService, publishing its counters once a second."""
    svc = S.BridgeService(serial=serial, port=port,
                          on_event=lambda k, t: say(f"  [{serial}] {k}: {t}"))
    try:
        svc.start()
    except BaseException as e:  # noqa: BLE001
        write_json(stats_file, {"t": time.time(), "state": "error",
                                "serial": serial, "port": port, "error": str(e)})
        return 1
    # The +60 s is a dead-man cap for a parent that died, not the normal exit.
    end = time.perf_counter() + seconds + 60.0
    try:
        while time.perf_counter() < end and not os.path.exists(stop_path(stats_file)):
            write_json(stats_file, bridge_sample(svc, time.process_time()))
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        final = bridge_sample(svc, time.process_time())
        final["done"] = True
        write_json(stats_file, final)
        svc.stop()
    return 0


# ---------------------------------------------------------------------------
# the parent
# ---------------------------------------------------------------------------


def wait_for_new_path(before: set[str], timeout: float = 20.0) -> str | None:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        new = usb_hid_paths() - before
        if new:
            return sorted(new)[0]
        time.sleep(0.4)
    return None


def wait_for_reader(reader_stats: str, serials: list[str],
                    timeout: float = 40.0) -> None:
    """Block until the reader child is actually reading every device.

    Starting the clock when the reader is SPAWNED counts its interpreter
    start-up and two `hid.open_path` calls as dead time, and a 20 s run then
    reports 229 reports/s for a link that was doing 250 -- a measurement
    artefact indistinguishable from a real regression.
    """
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        c = read_json(reader_stats).get("controllers") or {}
        if all((c.get(s) or {}).get("reports", 0) > 50 for s in serials):
            return
        time.sleep(0.3)
    say("  WARNING: the reader did not reach every device; numbers may be low")


def cleanup_ports(ports: list[int]) -> None:
    for p in ports:
        try:
            u = Usbip(port=p)
            S.cleanup(p, u.exe, log_fn=lambda t: print("    " + t, flush=True))
        except Exception as e:  # noqa: BLE001
            print(f"    cleanup {p}: {e}", flush=True)


def _d(cur: dict, base: dict, key: str) -> int:
    """A counter's movement DURING the window, not since the bridge started.

    Every counter here is cumulative from `BridgeBackend.start()`, and the
    bridges are up for several seconds of attach and settle before the reader
    process has even opened its HID handles. Dividing a cumulative counter by
    the measured window makes the settle look like part of the run -- which is
    STATUS.md 16.6 gotcha #11 turned into a reporting bug (the BT rate reads
    low for the first seconds after connect). Baseline, then difference.
    """
    return int(cur.get(key, 0) or 0) - int(base.get(key, 0) or 0)


def summarise(name: str, serials: list[str], reader: dict, bridges: dict,
              base_reader: dict, base_bridges: dict, wall: float,
              cpu: dict, audio: bool) -> dict:
    """One configuration's verdict, per controller plus the aggregate."""
    out = {"config": name, "wall_s": round(wall, 1), "cpu": cpu,
           "audio_driven": audio, "controllers": {}, "aggregate": {}}
    for s in serials:
        r = (reader.get("controllers") or {}).get(s, {})
        rb = (base_reader.get("controllers") or {}).get(s, {})
        b = bridges.get(s, {})
        bb = base_bridges.get(s, {})
        st, stb = b.get("stats", {}), bb.get("stats", {})
        seam, seamb = b.get("seam", {}), bb.get("seam", {})
        dev = b.get("device", {})
        reports = _d(r, rb, "reports")
        out["controllers"][s] = {
            "reports_per_s": round(reports / max(wall, 1e-6), 2),
            "reports": reports,
            "empty_polls": _d(r, rb, "empty"),
            "read_errors": _d(r, rb, "errors"),
            "seq_breaks": _d(r, rb, "seq_breaks"),
            "gap_ms_median": r.get("gap_ms_median", 0.0),
            "gap_ms_p99": r.get("gap_ms_p99", 0.0),
            "gap_ms_max": r.get("gap_ms_max", 0.0),
            "bt_reports_per_s": round(_d(st, stb, "bt_reports") / max(wall, 1e-6), 1),
            "bt_read_errors": _d(st, stb, "bt_read_errors"),
            "input_repeated": _d(st, stb, "input_repeated"),
            "input_neutral": _d(st, stb, "input_neutral"),
            "disconnects": _d(st, stb, "disconnects"),
            "reconnects": _d(st, stb, "reconnects"),
            "watchdog_trips": _d(st, stb, "link_watchdog_trips"),
            "audio_out_calls": _d(st, stb, "audio_out_calls"),
            "reports_39": _d(seam, seamb, "reports_39"),
            "audio_underrun_frames": _d(seam, seamb, "audio_underrun_frames"),
            "audio_q_drop_frames": _d(seam, seamb, "audio_q_drop_frames"),
            "out_ring_overflow_bytes": _d(seam, seamb, "out_ring_overflow_bytes"),
            "mic_ring_overflow_bytes": _d(seam, seamb, "mic_ring_overflow_bytes"),
            "mic_payloads": _d(seam, seamb, "mic_payloads"),
            "mic_decode_errors": _d(seam, seamb, "mic_decode_errors"),
            "mic_underrun_calls": _d(seam, seamb, "mic_underrun_calls"),
            "report_39_errors": _d(seam, seamb, "report_39_errors"),
            "control_jobs_dropped": _d(st, stb, "control_jobs_dropped"),
            "battery_percent": dev.get("battery_percent"),
            "state": b.get("state", "?"),
        }
    c = out["controllers"]
    out["aggregate"] = {
        "reports_per_s_total": round(sum(v["reports_per_s"] for v in c.values()), 2),
        "reports_per_s_min": round(min(v["reports_per_s"] for v in c.values()), 2),
        "seq_breaks_total": sum(v["seq_breaks"] for v in c.values()),
        "gap_ms_p99_worst": max(v["gap_ms_p99"] for v in c.values()),
        "gap_ms_max_worst": max(v["gap_ms_max"] for v in c.values()),
        "bt_read_errors_total": sum(v["bt_read_errors"] for v in c.values()),
        "audio_underrun_frames_total":
            sum(v["audio_underrun_frames"] for v in c.values()),
    }
    return out


def print_summary(res: dict) -> None:
    print()
    print(f"=== {res['config']}  --  {res['wall_s']:.0f} s wall clock, "
          f"audio {'DRIVEN' if res.get('audio_driven') else 'NOT driven'} ===")
    cols = [("reports/s", "reports_per_s", "{:.2f}"),
            ("seq breaks", "seq_breaks", "{}"),
            ("empty polls", "empty_polls", "{}"),
            ("gap med ms", "gap_ms_median", "{:.3f}"),
            ("gap p99 ms", "gap_ms_p99", "{:.3f}"),
            ("gap max ms", "gap_ms_max", "{:.3f}"),
            ("bt reports/s", "bt_reports_per_s", "{:.1f}"),
            ("bt read errors", "bt_read_errors", "{}"),
            ("input repeated", "input_repeated", "{}"),
            ("input neutral", "input_neutral", "{}"),
            ("disconnects", "disconnects", "{}"),
            ("reconnects", "reconnects", "{}"),
            ("watchdog trips", "watchdog_trips", "{}"),
            ("audio out calls", "audio_out_calls", "{}"),
            ("0x39 reports", "reports_39", "{}"),
            ("audio underrun frames", "audio_underrun_frames", "{}"),
            ("audio q-drop frames", "audio_q_drop_frames", "{}"),
            ("out ring overflow B", "out_ring_overflow_bytes", "{}"),
            ("mic payloads", "mic_payloads", "{}"),
            ("mic decode errors", "mic_decode_errors", "{}"),
            ("mic ring overflow B", "mic_ring_overflow_bytes", "{}"),
            ("0x39 errors", "report_39_errors", "{}"),
            ("control jobs dropped", "control_jobs_dropped", "{}"),
            ("battery %", "battery_percent", "{}"),
            ("final state", "state", "{}")]
    serials = list(res["controllers"])
    head = "  " + "metric".ljust(24) + "".join(s.rjust(16) for s in serials)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for label, key, fmt in cols:
        cells = []
        for s in serials:
            v = res["controllers"][s].get(key)
            cells.append(("-" if v is None else fmt.format(v)).rjust(16))
        print("  " + label.ljust(24) + "".join(cells))
    print()
    for k, v in res["cpu"].items():
        print(f"  cpu  {k:<30}{v}")
    print()
    for k, v in res["aggregate"].items():
        print(f"  agg  {k:<30}{v}")


def run_inproc(serials: list[str], ports: list[int], seconds: float,
               every: float, reader_stats: str, reader_job: str,
               audio: bool = False) -> dict:
    """Configuration (a): every bridge on threads inside THIS interpreter."""
    services = []
    paths: dict[str, str] = {}
    reader_proc = None
    t0 = time.perf_counter()
    cpu0 = time.process_time()
    try:
        for serial, port in zip(serials, ports):
            before = usb_hid_paths()
            svc = S.BridgeService(serial=serial, port=port,
                                  on_event=lambda k, t, s=serial: say(f"  [{s}] {k}: {t}"))
            services.append(svc)
            svc.start()
            p = wait_for_new_path(before)
            if p is None:
                raise RuntimeError(f"no virtual HID device appeared for {serial}")
            paths[serial] = p
            say(f"  [{serial}] virtual HID at {p[:70]}")

        clear_stop(reader_stats)
        write_json(reader_job, {"paths": paths, "seconds": seconds,
                                "audio": audio})
        reader_proc = spawn_self(["--reader", "--job", reader_job,
                                  "--stats-json", reader_stats])
        wait_for_reader(reader_stats, serials)
        base_reader = read_json(reader_stats)
        base_bridges = {s: bridge_sample(svc, 0.0)
                        for s, svc in zip(serials, services)}
        t0 = time.perf_counter()
        cpu0 = time.process_time()
        tick(seconds, every, serials,
             lambda: {s: bridge_sample(svc, 0.0)
                      for s, svc in zip(serials, services)},
             reader_stats, t0, base_reader)
        wall = time.perf_counter() - t0
        used = time.process_time() - cpu0
        cpu = {"bridge process (both bridges)":
               f"{used:.1f} s = {100.0 * used / max(wall, 1e-6):.1f} % of one core"}
        reader = read_json(reader_stats)
        cpu["reader process"] = f"{reader.get('cpu_s', 0.0)} s"
        bridges = {s: bridge_sample(svc, 0.0) for s, svc in zip(serials, services)}
        return summarise("(a) two BridgeService objects in ONE process",
                         serials, reader, bridges, base_reader, base_bridges,
                         wall, cpu, audio)
    finally:
        if reader_proc is not None:
            ask_to_stop(reader_stats)
            stop_child(reader_proc)
        for svc in services:
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                say("  teardown failed")


def run_children(serials: list[str], ports: list[int], seconds: float,
                 every: float, reader_stats: str, reader_job: str,
                 stats_dir: Path, audio: bool = False) -> dict:
    """Configuration (b): one child process per bridge, inside a job object."""
    job = create_kill_on_close_job()
    if job is None:
        say("  WARNING: no job object; children could outlive a hard kill")
    procs: list[subprocess.Popen] = []
    files: dict[str, str] = {}
    paths: dict[str, str] = {}
    reader_proc = None
    t0 = time.perf_counter()
    try:
        for serial, port in zip(serials, ports):
            before = usb_hid_paths()
            f = str(stats_dir / f"bridge-{serial}.json")
            clear_stop(f)
            files[serial] = f
            p = spawn_self(["--bridge", "--serial", serial, "--port", str(port),
                            "--seconds", str(seconds + 30), "--stats-json", f],
                           job=job)
            procs.append(p)
            deadline = time.perf_counter() + 60.0
            while time.perf_counter() < deadline:
                d = read_json(f)
                if d.get("state") == S.RUNNING:
                    break
                if d.get("state") == "error":
                    raise RuntimeError(f"{serial}: {d.get('error')}")
                if p.poll() is not None:
                    raise RuntimeError(f"{serial}: child exited {p.returncode}")
                time.sleep(0.5)
            else:
                raise RuntimeError(f"{serial}: child did not come up in 60 s")
            newp = wait_for_new_path(before)
            if newp is None:
                raise RuntimeError(f"no virtual HID device appeared for {serial}")
            paths[serial] = newp
            say(f"  [{serial}] pid {p.pid}, virtual HID at {newp[:60]}")

        clear_stop(reader_stats)
        write_json(reader_job, {"paths": paths, "seconds": seconds,
                                "audio": audio})
        reader_proc = spawn_self(["--reader", "--job", reader_job,
                                  "--stats-json", reader_stats], job=job)
        wait_for_reader(reader_stats, serials)
        base_reader = read_json(reader_stats)
        base_bridges = {s: read_json(f) for s, f in files.items()}
        t0 = time.perf_counter()
        base = {s: v.get("cpu_s", 0.0) for s, v in base_bridges.items()}
        tick(seconds, every, serials,
             lambda: {s: read_json(f) for s, f in files.items()},
             reader_stats, t0, base_reader)
        wall = time.perf_counter() - t0
        bridges = {s: read_json(f) for s, f in files.items()}
        cpu = {}
        total = 0.0

        def core_pct(sec: float) -> str:
            return f"{sec:.1f} s = {100.0 * sec / max(wall, 1e-6):.1f} % of one core"

        for s in serials:
            d = bridges[s].get("cpu_s", 0.0) - base.get(s, 0.0)
            total += d
            cpu[f"bridge child {s}"] = core_pct(d)
        cpu["bridge children total"] = core_pct(total)
        reader = read_json(reader_stats)
        cpu["reader process"] = f"{reader.get('cpu_s', 0.0)} s"
        return summarise("(b) two child processes, one BridgeService each",
                         serials, reader, bridges, base_reader, base_bridges,
                         wall, cpu, audio)
    finally:
        if reader_proc is not None:
            ask_to_stop(reader_stats)
            stop_child(reader_proc)
        # Ask every bridge child to tear down FIRST, then wait. Serialising
        # (ask, wait, ask, wait) would make the second controller run 15 s
        # longer than the first and put the two teardowns in different
        # conditions.
        for f in files.values():
            ask_to_stop(f)
        for p in procs:
            stop_child(p)
        # Closing the job kills anything still in it. Belt and braces: the
        # children were asked politely first.
        close_job(job)


def tick(seconds: float, every: float, serials: list[str], get_bridges,
         reader_stats: str, t0: float, base_reader: dict) -> None:
    """Print one line per controller per interval, so a collapse is visible live."""
    bc = base_reader.get("controllers") or {}
    prev_reports = {s: (bc.get(s) or {}).get("reports", 0) for s in serials}
    prev_t = t0
    end = t0 + seconds
    while time.perf_counter() < end:
        time.sleep(min(every, max(0.0, end - time.perf_counter())))
        now = time.perf_counter()
        reader = read_json(reader_stats)
        bridges = get_bridges()
        parts = []
        for s in serials:
            r = (reader.get("controllers") or {}).get(s, {})
            n = r.get("reports", 0)
            rate = (n - prev_reports[s]) / max(1e-6, now - prev_t)
            prev_reports[s] = n
            st = (bridges.get(s) or {}).get("stats", {})
            parts.append(f"{s[:6]} {rate:6.1f}/s seq {r.get('seq_breaks', 0)} "
                         f"bterr {st.get('bt_read_errors', 0)} "
                         f"neut {st.get('input_neutral', 0)}")
        prev_t = now
        print(f"  t={now - t0:6.1f}s  " + " | ".join(parts), flush=True)


def spawn_self(args: list[str], job=None) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + args,
                         creationflags=0)
    if job is not None:
        if not assign_to_job(job, p):
            say(f"  WARNING: pid {p.pid} is NOT in the job object")
    return p


def stop_child(p: subprocess.Popen) -> None:
    """Ask, then insist. A bridge child needs its teardown to run.

    `terminate()` on Windows is `TerminateProcess`, which runs NO cleanup at
    all -- no atexit, no console handler -- so a bridge killed that way leaves
    the device attached and the auto-re-attach armed (STATUS.md 18.3(1)). The
    child is given its own `--seconds` deadline to exit on, and this is only
    the fallback for one that did not.
    """
    if p.poll() is not None:
        return
    try:
        p.wait(timeout=25.0)
    except subprocess.TimeoutExpired:
        say(f"  child {p.pid} did not exit; terminating")
        try:
            p.terminate()
            p.wait(timeout=10.0)
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="two-controller architecture soak")
    ap.add_argument("--serials", default="",
                    help="comma-separated Bluetooth serials, in port order")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--every", type=float, default=15.0)
    ap.add_argument("--base-port", type=int, default=BASE_PORT)
    ap.add_argument("--mode", choices=("inproc", "child", "both"), default="both")
    ap.add_argument("--audio", action="store_true",
                    help="also drive the isochronous OUT path with a tone. "
                         "Costs roughly 2.5 %% of battery per minute; see the "
                         "module docstring before running it long.")
    ap.add_argument("--jsonl", default=None, help="append each verdict here")
    ap.add_argument("--work-dir", default=None,
                    help="where the child stats files go (default: %TEMP%)")
    # child roles
    ap.add_argument("--bridge", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--reader", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--serial", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=BASE_PORT, help=argparse.SUPPRESS)
    ap.add_argument("--stats-json", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--job", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.bridge:
        return run_bridge_child(a.serial, a.port, a.seconds, a.stats_json)
    if a.reader:
        return run_reader(a.job, a.stats_json)

    serials = [s.strip().lower() for s in a.serials.split(",") if s.strip()]
    if len(serials) < 2:
        print("give at least two serials: --serials aaaa,bbbb")
        return 2
    ports = [a.base_port + i for i in range(len(serials))]
    work = Path(a.work_dir or os.environ.get("TEMP", ".")) / "ds5-multi-soak"
    work.mkdir(parents=True, exist_ok=True)
    reader_stats = str(work / "reader.json")
    reader_job = str(work / "reader-job.json")

    say(f"serials {serials} on ports {ports}")
    results = []
    modes = ["inproc", "child"] if a.mode == "both" else [a.mode]
    for m in modes:
        say(f"--- cleaning ports {ports} before configuration {m} ---")
        cleanup_ports(ports)
        say(f"--- configuration {m}: {a.seconds:g} s ---")
        if m == "inproc":
            res = run_inproc(serials, ports, a.seconds, a.every,
                             reader_stats, reader_job, a.audio)
        else:
            res = run_children(serials, ports, a.seconds, a.every,
                               reader_stats, reader_job, work, a.audio)
        results.append(res)
        print_summary(res)
        if a.jsonl:
            with open(a.jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(res) + "\n")
        say("--- cleaning up after the configuration ---")
        cleanup_ports(ports)
        if m != modes[-1]:
            # Let Windows finish removing the devnodes, and let the controllers'
            # Bluetooth links settle, before the next configuration opens them
            # again. STATUS.md 16.6 gotcha #11: the BT rate reads low for the
            # first seconds after connect, so a back-to-back run measures the
            # settle, not the architecture.
            say("  settling for 20 s")
            time.sleep(20.0)

    if len(results) == 2:
        print()
        print("=== verdict ===")
        for r in results:
            print(f"  {r['config']}")
            print(f"      min reports/s {r['aggregate']['reports_per_s_min']:.2f}  "
                  f"seq breaks {r['aggregate']['seq_breaks_total']}  "
                  f"worst p99 gap {r['aggregate']['gap_ms_p99_worst']:.3f} ms  "
                  f"worst max gap {r['aggregate']['gap_ms_max_worst']:.3f} ms  "
                  f"bt read errors {r['aggregate']['bt_read_errors_total']}")
        for s in serials:
            a_ = results[0]["controllers"][s]
            b_ = results[1]["controllers"][s]
            print(f"  {s}   inproc {a_['reports_per_s']:.2f}/s "
                  f"(seq {a_['seq_breaks']}, p99 gap {a_['gap_ms_p99']:.3f} ms)"
                  f"   vs child {b_['reports_per_s']:.2f}/s "
                  f"(seq {b_['seq_breaks']}, p99 gap {b_['gap_ms_p99']:.3f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
