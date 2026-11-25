#!/usr/bin/env python3
"""
esp_relay.py — serial relay + HTTP flash server for ESP32 / ESP8266 / ESP8285.

Two services in one process (pure stdlib, Linux only):

  1. SERIAL RELAY  TCP 127.0.0.1:4000  <->  /dev/ttyUSB0 (or any tty)
     (the TCP/HTTP ports auto-increment by 1 when busy; paired with HTTP)
     On connect the standard DTR/RTS reset dance is performed:

         ESPRELAY:download\\n   enter ROM download mode (esptool)
         ESPRELAY:run\\n        reboot into the application
         ESPRELAY:none\\n       no reset, just bridge
         (no prefix)           download (legacy behaviour)

     The prefix is absorbed and never reaches the UART.  After the reset
     the relay is a transparent byte bridge.  If the USB device disappears
     (replug, native-USB ESP32 re-enumeration) the relay waits for the
     same path to come back and keeps the TCP session alive.

  2. HTTP FLASH / RESET API  http://127.0.0.1:8000
     (auto-increments; paired with TCP)
       GET  /health
       POST /reset  {"mode": "run"}      # run | download
       POST /flash
         {
           "images": [{"address": "0x1000", "file": "/abs/bootloader.bin"},
                      {"address": "0x10000", "file": "/abs/app.bin"}],
           # or "merged": "/abs/merged.bin"  (single image at 0x0)
           "chip": "esp32",          # auto (default) | esp32 | esp8266 |
                                     # esp8285 (alias for esp8266) | ...
           "baud": 115200,
           "before": "default-reset",
           "after": "hard-reset",
           "no_stub": false,
           "no_compression": false,
           "erase_all": false,
           "timeout_s": 1800,
           "dry_run": false
         }

     esptool runs LOCALLY on the host.  esptool v4 (write_flash /
     default_reset) and v5 (write-flash / default-reset) are both
     supported and detected automatically.

     A connected console session is *preempted* (its TCP connection is
     closed) so the flash does not have to wait for it.  The client can
     reconnect afterwards.  `flash_wait` bounds the wait for the session
     to release the port.

Why a host-side relay at all?  On a USBIP/vhci setup the sandbox may not
see /dev, and flashing through a TCP->pty chain is unreliable.  This
process owns the host serial port; the flash API runs esptool on the host
and the relay exposes the console over TCP.

Usage (host):
  python3 esp_relay.py --port /dev/ttyUSB0
  python3 esp_relay.py --port /dev/ttyUSB1 --chip esp8266 --baud 115200
  python3 esp_relay.py --reset none --port /dev/ttyUSB0   # no DTR/RTS
  python3 esp_relay.py --no-serial                        # flash API only

Only the Python standard library is required; esptool must be installed
(Arduino core, pip, or distro package) or passed with --esptool.

Potential bugs fixed in this version (see README for details):
  * esptool v4 command/option spelling broke flashing -> v4/v5 support
  * the old default chip was esp32 (ESP8266/ESP8285 failed) -> auto
  * a split "ESPRELAY:" prefix leaked into the UART -> line-buffered read
  * a console session blocked flashing for up to 300 s -> preemption
  * no reconnect after a USB re-enumeration -> identity check + wait
  * invalid baud / non-dict image entries raised a 500 -> validation
  * "merged" + "images" silently overlapped -> 400
  * the HTTP API died when the serial port was missing -> starts first
  * sys.exit() in the serial open path -> SerialError exceptions
  * HUPCL could reset the board on close -> cleared
  * busy-loop on a slow TCP client -> writability select with timeout
"""

import argparse
import fcntl
import glob
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"

DEFAULT_TCP = 4000
DEFAULT_HTTP = 8000
DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
DEFAULT_CHIP = "auto"
DEFAULT_NAME = "esp-relay"
DEFAULT_FLASH_TIMEOUT = 1800
DEFAULT_FLASH_WAIT = 40       # s, wait for the console session to release
DEFAULT_RESCAN_TIMEOUT = 180  # s, 0 = wait forever
DEFAULT_IDENT_INTERVAL = 5.0  # s
HTTP_TIMEOUT = 30             # s
MAX_BODY = 1 << 20            # 1 MiB
LOG_TAIL = 16000
FLASH_ADDR_MAX = 0x1000000    # 16 MiB
PORT_TRIES = 100              # auto-port: number of +1 attempts

NAME = DEFAULT_NAME
VERBOSE = False


# ---------------------------------------------------------------------------
# logging / runtime state
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print("%s [%s] %s" % (time.strftime("%H:%M:%S"), NAME, msg), flush=True)


class Runtime:
    _lock = threading.Lock()
    _serial = False
    _flash = False

    @classmethod
    def set_serial(cls, value: bool) -> None:
        with cls._lock:
            cls._serial = bool(value)

    @classmethod
    def set_flash(cls, value: bool) -> None:
        with cls._lock:
            cls._flash = bool(value)

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return cls._serial, cls._flash


class BadRequest(ValueError):
    """Invalid HTTP request (-> 400)."""


class SerialError(RuntimeError):
    """The serial device could not be opened/configured/used."""


class PortError(RuntimeError):
    """No free HTTP/TCP port pair could be bound."""


# ---------------------------------------------------------------------------
# serial helpers
# ---------------------------------------------------------------------------

def _baud_constant(baud: int) -> int:
    if baud <= 0:
        raise SerialError("invalid baud rate: %d" % baud)
    const = getattr(termios, "B%d" % baud, None)
    if const is None:
        raise SerialError(
            "unsupported baud rate %d on this platform (no termios B%d)" % (baud, baud)
        )
    return const


def open_serial(path: str, baud: int) -> int:
    """Open a tty in raw 8N1 mode, non-blocking.  Raises SerialError."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise SerialError("%s: no such device" % path)
    except PermissionError:
        raise SerialError(
            "%s: permission denied — add your user to the 'dialout' group "
            "and re-login (check with `groups`)" % path
        )
    except OSError as exc:
        raise SerialError("%s: %s" % (path, exc))

    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        cflag = termios.CREAD | termios.CLOCAL | termios.CS8 | _baud_constant(baud)
        if hasattr(termios, "HUPCL"):
            cflag &= ~termios.HUPCL
        attrs[2] = cflag
        attrs[3] = 0
        speed = _baud_constant(baud)
        attrs[4] = speed
        attrs[5] = speed
        if attrs[6]:
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.set_blocking(fd, False)
    except (OSError, termios.error, ValueError) as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        raise SerialError("%s: cannot configure serial port: %s" % (path, exc))
    return fd


def tty_identity(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def uart_write(fd: int, data: bytes, timeout: float = 10.0) -> None:
    view = memoryview(data)
    deadline = time.monotonic() + timeout
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise SerialError("uart write timeout (device not reading)")
            try:
                select.select([], [fd], [], remain)
            except (OSError, ValueError) as exc:
                raise SerialError("uart write select failed: %s" % exc)
            continue
        except OSError as exc:
            raise SerialError("uart write failed: %s" % exc)
        if n <= 0:
            raise SerialError("uart write returned %d" % n)
        view = view[n:]


def send_all(conn: socket.socket, data: bytes, timeout: float = 30.0) -> None:
    view = memoryview(data)
    deadline = time.monotonic() + timeout
    while view:
        try:
            n = conn.send(view)
        except InterruptedError:
            continue
        except BlockingIOError:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise OSError("client write timeout (client not reading)")
            try:
                select.select([], [conn], [], remain)
            except (OSError, ValueError) as exc:
                raise OSError("client write select failed: %s" % exc)
            continue
        except (OSError, ValueError) as exc:
            raise OSError("client send failed: %s" % exc)
        if n <= 0:
            raise OSError("client send returned 0 (connection closed)")
        view = view[n:]


def read_client_prefix(conn: socket.socket, timeout: float = 3.0):
    """Read the optional "ESPRELAY:<mode>\\n" line.

    Returns (mode, leftover):
      (None, b"")     timeout, no data
      (None, None)    client closed
      (None, bytes)   ordinary data (not a prefix)
      (mode, bytes)   prefix absorbed; leftover must be forwarded

    The old single recv(64) could split the prefix and leak the rest
    ("ESPRELAY:con" + "sole\\n") into the UART.  Read a bounded line.
    """
    deadline = time.monotonic() + timeout
    buf = b""
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        try:
            conn.settimeout(remain)
            chunk = conn.recv(256)
        except socket.timeout:
            break
        except OSError:
            return None, None
        if not chunk:
            return None, None
        buf += chunk
        if b"\n" in buf:
            break
        if not buf.startswith(b"ESPRELAY:"):
            break
        if len(buf) > 256:
            return None, buf
    if not buf.startswith(b"ESPRELAY:"):
        return None, buf
    line, _, rest = buf.partition(b"\n")
    mode = line[len(b"ESPRELAY:"):].strip().decode("ascii", "replace").lower()
    return mode, rest


# ---------------------------------------------------------------------------
# DTR/RTS reset sequences
# ---------------------------------------------------------------------------

_dtr_warned = False


def set_dtr_rts(fd: int, dtr: bool, rts: bool) -> bool:
    """Set DTR/RTS.  Returns False when the adapter has no line control."""
    global _dtr_warned
    mask = 0
    if dtr:
        mask |= termios.TIOCM_DTR
    if rts:
        mask |= termios.TIOCM_RTS
    try:
        fcntl.ioctl(fd, termios.TIOCMSET, struct.pack("I", mask))
        return True
    except OSError as exc:
        if not _dtr_warned:
            log("warning: DTR/RTS control unavailable (%s) — reset the board "
                "manually, or use --reset none" % exc)
            _dtr_warned = True
        return False


def reset_download(fd: int) -> bool:
    """esptool UnixTightReset: enter the ROM download mode.

    Returns False when DTR/RTS control is unavailable.
    """
    log("reset: DOWNLOAD mode (UnixTightReset sequence)")
    if not set_dtr_rts(fd, False, False):
        return False
    time.sleep(0.02)
    if not set_dtr_rts(fd, True, True):
        return False
    time.sleep(0.02)
    if not set_dtr_rts(fd, False, True):   # IO0=HIGH, EN=LOW (chip in reset)
        return False
    time.sleep(0.10)
    if not set_dtr_rts(fd, True, False):   # IO0=LOW, EN=HIGH (boot from ROM)
        return False
    time.sleep(0.05)
    if not set_dtr_rts(fd, False, False):  # IO0=HIGH
        return False
    time.sleep(0.10)
    return True


def reset_run(fd: int) -> bool:
    """esptool HardReset: reboot into the application.

    Returns False when DTR/RTS control is unavailable.
    """
    log("reset: APP mode (HardReset sequence)")
    if not set_dtr_rts(fd, False, True):  # EN=LOW
        return False
    time.sleep(0.10)
    if not set_dtr_rts(fd, False, False):  # EN=HIGH
        return False
    time.sleep(0.20)
    return True


# ---------------------------------------------------------------------------
# serial state
# ---------------------------------------------------------------------------

class SerialState:
    def __init__(self, path: str, baud: int, *, rescan: bool = True,
                 rescan_timeout: float = DEFAULT_RESCAN_TIMEOUT):
        self.lock = threading.RLock()
        self.path = path or ""
        self.baud = baud
        self.rescan = rescan
        self.rescan_timeout = rescan_timeout
        self.fd = -1
        self.ident = None
        self._last_error = ""
        self._last_error_at = 0.0

    def _close_locked(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1
        self.ident = None

    def _log_open_error(self, exc) -> None:
        msg = str(exc)
        now = time.monotonic()
        if msg != self._last_error or now - self._last_error_at >= 10.0:
            log("serial open failed: %s" % msg)
            self._last_error = msg
            self._last_error_at = now

    def _open_locked(self, path: str) -> bool:
        try:
            fd = open_serial(path, self.baud)
        except SerialError as exc:
            self._log_open_error(exc)
            return False
        old = self.fd
        self.fd = fd
        self.path = path
        self.ident = tty_identity(path)
        self._last_error = ""
        if old >= 0:
            try:
                os.close(old)
            except OSError:
                pass
        log("opened %s @ %d baud" % (path, self.baud))
        return True

    def find_replacement(self):
        return self.path if self.path and os.path.exists(self.path) else None

    def try_open(self) -> bool:
        with self.lock:
            path = self.find_replacement()
            if not path:
                return False
            return self._open_locked(path)

    def try_open_locked(self) -> bool:
        path = self.find_replacement()
        if not path:
            return False
        return self._open_locked(path)

    def drop(self) -> None:
        with self.lock:
            self._close_locked()

    def ensure_ready(self) -> bool:
        with self.lock:
            if self.fd >= 0:
                cur = tty_identity(self.path)
                if cur is not None and (self.ident is None or cur == self.ident):
                    return True
                log("serial node changed or disappeared — reopening")
                self._close_locked()
            return self.try_open_locked()

    def refresh(self) -> bool:
        with self.lock:
            if self.fd < 0:
                return self.try_open_locked()
            cur = tty_identity(self.path)
            if cur is not None and self.ident is not None and cur == self.ident:
                return False
            log("device re-enumerated (%s) — swapping serial fd" % self.path)
            self._close_locked()
            return self.try_open_locked()


class ESPState(SerialState):
    """ESP state + the two locks used to preempt a console session."""

    def __init__(self, path: str, baud: int, *, rescan: bool = True,
                 rescan_timeout: float = DEFAULT_RESCAN_TIMEOUT):
        super().__init__(path, baud, rescan=rescan,
                         rescan_timeout=rescan_timeout)
        self.preempt = threading.Event()
        # Held for the whole lifetime of a TCP console session (and by the
        # flash handler, which is how it evicts that session).
        self.session_lock = threading.Lock()

    def open_now(self) -> None:
        with self.lock:
            fd = open_serial(self.path, self.baud)
            old = self.fd
            self.fd = fd
            self.ident = tty_identity(self.path)
            if old >= 0:
                try:
                    os.close(old)
                except OSError:
                    pass
        log("opened %s @ %d baud (auto-rescan %s)" % (
            self.path, self.baud, "on" if self.rescan else "off"))


# ---------------------------------------------------------------------------
# serial <-> TCP bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, state: ESPState, *, ident_interval: float,
                 allow_rescan: bool = True, reset_mode: str = "auto",
                 stop=None):
        self.state = state
        self.ident_interval = max(0.2, float(ident_interval))
        self.allow_rescan = allow_rescan
        self.reset_mode = reset_mode
        self.stop = stop

    def _deadline(self):
        timeout = self.state.rescan_timeout
        if timeout is None or timeout <= 0:
            return None
        return time.monotonic() + timeout

    def _apply_reset(self, mode: str) -> None:
        if self.reset_mode == "none":
            return
        if mode in ("run", "console", "app"):
            reset_run(self.state.fd)
        elif mode in ("none", "skip", "no-reset"):
            log("no reset requested (mode=%s)" % mode)
        else:
            if mode and mode not in ("download", "flash", "boot"):
                log("unknown reset mode %r — using download" % mode)
            reset_download(self.state.fd)

    def serve(self, conn: socket.socket) -> None:
        st = self.state
        Runtime.set_serial(True)
        rfd = -1
        n_to_client = 0
        n_to_uart = 0
        reset_done = False
        try:
            ready = st.ensure_ready()
            if not ready and not (self.allow_rescan and st.rescan):
                log("serial device unavailable — closing client")
                return

            mode, leftover = read_client_prefix(conn)
            if mode is None and leftover is None:
                log("client closed before the bridge started")
                return
            if mode is None:
                mode = "download"   # legacy: no prefix = download mode
            log("client reset mode: %s" % mode)

            conn.setblocking(False)
            if ready:
                self._apply_reset(mode)
                reset_done = True
            else:
                log("serial device unavailable — waiting up to %ss ..." %
                    (st.rescan_timeout if st.rescan_timeout > 0 else "∞"))

            if leftover:
                if st.fd >= 0:
                    try:
                        uart_write(st.fd, leftover)
                        n_to_uart += len(leftover)
                    except SerialError as exc:
                        log("initial uart write failed: %s" % exc)
                        st.drop()
                else:
                    log("dropping %d byte(s) sent before the device was ready" %
                        len(leftover))

            if st.fd >= 0:
                rfd = os.dup(st.fd)
            deadline = None if rfd >= 0 else self._deadline()
            next_open = 0.0
            next_ident = time.monotonic() + self.ident_interval
            warned_drop = False

            while True:
                if self.stop is not None and self.stop.is_set():
                    log("shutdown requested — closing session")
                    return
                if st.preempt.is_set():
                    log("console session preempted for flashing — closing")
                    return
                now = time.monotonic()
                if rfd < 0:
                    if not (self.allow_rescan and st.rescan):
                        return
                    if deadline is not None and now >= deadline:
                        log("giving up waiting for the serial device")
                        return
                    if now >= next_open:
                        if st.try_open():
                            rfd = os.dup(st.fd)
                            deadline = None
                            warned_drop = False
                            log("serial device is back (%s) — resuming session" % st.path)
                            if not reset_done:
                                self._apply_reset(mode)
                                reset_done = True
                        next_open = now + 1.0
                    try:
                        rlist, _, _ = select.select([conn], [], [], 0.5)
                    except (OSError, ValueError):
                        return
                    if not rlist:
                        continue
                    try:
                        data = conn.recv(4096)
                    except (BlockingIOError, socket.timeout):
                        continue
                    except OSError:
                        return
                    if not data:
                        log("client closed while waiting for the device")
                        return
                    if not warned_drop:
                        log("device absent: dropping client->device data until "
                            "it returns")
                        warned_drop = True
                    continue

                if now >= next_ident:
                    try:
                        changed = st.refresh()
                    except Exception as exc:  # noqa: BLE001
                        log("identity refresh failed: %r" % exc)
                        changed = False
                    if changed:
                        try:
                            os.close(rfd)
                        except OSError:
                            pass
                        rfd = -1
                        rfd = os.dup(st.fd)
                        log("serial fd swapped to %s" % st.path)
                    elif st.fd < 0:
                        try:
                            os.close(rfd)
                        except OSError:
                            pass
                        rfd = -1
                        deadline = self._deadline()
                        next_open = 0.0
                        continue
                    next_ident = now + self.ident_interval

                try:
                    rlist, _, _ = select.select([conn, rfd], [], [], 0.5)
                except (OSError, ValueError):
                    return
                if not rlist:
                    continue

                for r in rlist:
                    if r is rfd:
                        try:
                            data = os.read(rfd, 4096)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError as exc:
                            data = b""
                            log("serial read error: %s" % exc)
                        if not data:
                            log("serial device lost")
                            try:
                                os.close(rfd)
                            except OSError:
                                pass
                            rfd = -1
                            st.drop()
                            deadline = self._deadline()
                            next_open = 0.0
                            continue
                        n_to_client += len(data)
                        try:
                            send_all(conn, data)
                        except OSError as exc:
                            log("client gone (%s)" % exc)
                            return
                    else:
                        try:
                            data = conn.recv(4096)
                        except (BlockingIOError, socket.timeout):
                            continue
                        except OSError:
                            data = b""
                        if not data:
                            log("client closed")
                            return
                        n_to_uart += len(data)
                        if st.fd < 0:
                            continue
                        try:
                            uart_write(st.fd, data)
                        except SerialError as exc:
                            log("serial write failed: %s" % exc)
                            try:
                                os.close(rfd)
                            except OSError:
                                pass
                            rfd = -1
                            st.drop()
                            deadline = self._deadline()
                            next_open = 0.0
        except Exception as exc:  # noqa: BLE001
            log("session error: %r" % exc)
            if VERBOSE:
                traceback.print_exc()
        finally:
            if rfd >= 0:
                try:
                    os.close(rfd)
                except OSError:
                    pass
            Runtime.set_serial(False)
            log("session ended (bytes to_client=%d, to_uart=%d)" %
                (n_to_client, n_to_uart))


# ---------------------------------------------------------------------------
# esptool discovery / command building
# ---------------------------------------------------------------------------

BEFORE_INPUT = {
    "default": "default", "default-reset": "default", "default_reset": "default",
    "usb": "usb", "usb-reset": "usb", "usb_reset": "usb",
    "no": "no", "no-reset": "no", "no_reset": "no",
    "no-sync": "no-sync", "no-reset-no-sync": "no-sync",
    "no_reset_no_sync": "no-sync",
}
AFTER_INPUT = {
    "hard": "hard", "hard-reset": "hard", "hard_reset": "hard",
    "soft": "soft", "soft-reset": "soft", "soft_reset": "soft",
    "no": "no", "no-reset": "no", "no_reset": "no",
    "no-stub": "no-stub", "no-reset-stub": "no-stub", "no_reset_stub": "no-stub",
    "watchdog": "watchdog", "watchdog-reset": "watchdog",
    "watchdog_reset": "watchdog",
}
BEFORE_V5 = {"default": "default-reset", "usb": "usb-reset",
             "no": "no-reset", "no-sync": "no-reset-no-sync"}
BEFORE_V4 = {"default": "default_reset", "usb": "usb_reset",
             "no": "no_reset", "no-sync": "no_reset_no_sync"}
AFTER_V5 = {"hard": "hard-reset", "soft": "soft-reset", "no": "no-reset",
            "no-stub": "no-reset-stub", "watchdog": "watchdog-reset"}
AFTER_V4 = {"hard": "hard_reset", "soft": "soft_reset", "no": "no_reset",
            "no-stub": "no_reset_stub"}

CHIP_ALIASES = {"esp8285": "esp8266"}   # esptool has no esp8285 target


def _version_key(path: str):
    for part in reversed(os.path.dirname(path).split(os.sep)):
        m = re.fullmatch(r"(\d+(?:\.\d+)*)", part)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
    return ()


def resolve_esptool(explicit: str = "") -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("ESPTOOL", "")
    if env:
        candidates.append(env)
    for name in ("esptool", "esptool.py"):
        which = shutil.which(name)
        if which:
            candidates.append(which)
    candidates.append(os.path.expanduser("~/.local/bin/esptool"))
    core = os.environ.get("ARDUINO_DATA_DIR", os.path.expanduser("~/.arduino15"))
    candidates.extend(sorted(
        glob.glob(os.path.join(core, "packages", "*", "tools",
                               "esptool_py", "*", "esptool")),
        key=_version_key, reverse=True))
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return ""


def run_command(cmd, timeout: float):
    if VERBOSE:
        log("run: %s" % shlex.join([str(c) for c in cmd]))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except FileNotFoundError:
        return 127, "command not found: %s" % cmd[0]
    except PermissionError:
        return 126, "permission denied: %s" % cmd[0]
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, "TIMEOUT after %ss\n%s%s" % (timeout, out, err)
    except OSError as exc:
        return 125, "cannot run %s: %s" % (cmd[0], exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def esptool_version(exe: str):
    rc, out = run_command([exe, "version"], 15)
    if rc != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def normalize_chip(value) -> str:
    chip = str(value or DEFAULT_CHIP).strip().lower()
    chip = CHIP_ALIASES.get(chip, chip)
    if chip in ("", "auto"):
        return ""
    if not re.fullmatch(r"[a-z0-9]+", chip):
        raise BadRequest("invalid chip name: %r" % value)
    return chip


def normalize_before(value):
    key = str(value or "default-reset").strip().lower()
    canon = BEFORE_INPUT.get(key)
    if not canon:
        raise BadRequest("invalid 'before' value: %r (use default-reset, "
                         "usb-reset, no-reset or no-reset-no-sync)" % value)
    return canon


def normalize_after(value):
    key = str(value or "hard-reset").strip().lower()
    canon = AFTER_INPUT.get(key)
    if not canon:
        raise BadRequest("invalid 'after' value: %r (use hard-reset, "
                         "soft-reset, no-reset, no-reset-stub or "
                         "watchdog-reset)" % value)
    return canon


def build_esptool_command(exe: str, major: int, *, images, port: str,
                          chip: str, baud: int, before: str, after: str,
                          no_stub: bool, no_compression: bool,
                          erase_all: bool):
    """Pure command builder (also used by the tests)."""
    cmd = [exe]
    if chip:
        cmd.extend(["--chip", chip])
    before_map = BEFORE_V5 if major >= 5 else BEFORE_V4
    after_map = AFTER_V5 if major >= 5 else AFTER_V4
    if after not in after_map:
        raise BadRequest("'after=%s' is not supported by esptool v%d" %
                         (after, major))
    cmd.extend(["--port", port, "--baud", str(baud),
                "--before", before_map[before],
                "--after", after_map[after]])
    if no_stub:
        cmd.append("--no-stub")
    cmd.append("write-flash" if major >= 5 else "write_flash")
    if erase_all:
        cmd.append("--erase-all")
    if not no_stub:
        cmd.append("--no-compress" if no_compression else "--compress")
    for addr, path in images:
        cmd.extend([hex(addr), path])
    return cmd


def parse_address(value) -> int:
    if isinstance(value, bool):
        raise BadRequest("invalid flash address: %r" % value)
    if isinstance(value, int):
        addr = value
    else:
        text = str(value if value is not None else "").strip()
        if not text:
            raise BadRequest("missing 'address' for an image")
        try:
            addr = int(text, 0)
        except ValueError:
            raise BadRequest("invalid flash address: %r" % value)
    if not 0 <= addr < FLASH_ADDR_MAX:
        raise BadRequest("flash address out of range (0..0x%X): %r" %
                         (FLASH_ADDR_MAX - 1, value))
    return addr


def _looks_like_arg_error(output: str) -> bool:
    low = output.lower()
    return any(marker in low for marker in (
        "invalid choice", "unrecognized arguments", "unrecognized option",
        "no such option", "argument operation",
    ))


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class EspApp:
    role = "esp"

    def __init__(self, state: ESPState, *, esptool: str, version,
                 default_chip: str, flash_wait: float, started: float):
        self.state = state
        self.esptool = esptool
        self.version = version
        self.default_chip = default_chip
        self.flash_wait = flash_wait
        self.started = started
        self.flash_lock = threading.Lock()
        self.http_port = None
        self.tcp_port = None
        self.serial_enabled = True

    def health(self) -> dict:
        serial_active, flash_busy = Runtime.snapshot()
        with self.state.lock:
            port = self.state.path
            ready = self.state.fd >= 0
        return {
            "ok": True,
            "service": "serial-flash-relay",
            "name": NAME,
            "role": self.role,
            "version": VERSION,
            "http_port": self.http_port,
            "tcp_port": self.tcp_port,
            "port": port or None,
            "port_ready": ready,
            "baud": self.state.baud,
            "rescan": bool(self.state.rescan),
            "serial_active": serial_active,
            "flash_busy": flash_busy,
            "esptool": self.esptool or None,
            "esptool_version": ".".join(str(x) for x in self.version)
                               if self.version else None,
            "default_chip": self.default_chip,
            "flash_wait_s": self.flash_wait,
            "uptime_s": round(time.time() - self.started, 1),
        }

    # -- request parsing ---------------------------------------------------
    def _parse_request(self, req: dict):
        has_merged = "merged" in req
        has_images = "images" in req
        if has_merged and has_images:
            raise BadRequest("use either 'merged' or 'images', not both")
        images = []
        if has_merged:
            path = req.get("merged")
            if not isinstance(path, str) or not path.strip():
                raise BadRequest("'merged' must be a host path string")
            images.append((0, self._host_file(path)))
        if has_images:
            entries = req.get("images")
            if not isinstance(entries, list) or not entries:
                raise BadRequest("'images' must be a non-empty list")
            seen = {}
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise BadRequest("images[%d] must be an object" % idx)
                addr = parse_address(entry.get("address"))
                if addr in seen:
                    raise BadRequest("images[%d] duplicates address %s" %
                                     (idx, hex(addr)))
                path = self._host_file(entry.get("file"))
                seen[addr] = path
                images.append((addr, path))
        if not images:
            raise BadRequest("need 'images' (list) or 'merged' (path)")

        port = req.get("port") or self.state.path
        if not isinstance(port, str) or not port.strip():
            raise BadRequest("no serial port: pass --port or 'port' in the request")
        port = port.strip()

        chip = normalize_chip(req.get("chip", self.default_chip))
        try:
            baud = int(req.get("baud", self.state.baud))
        except (TypeError, ValueError):
            raise BadRequest("'baud' must be an integer")
        if not 9600 <= baud <= 2000000:
            raise BadRequest("'baud' out of range (9600..2000000)")
        before = normalize_before(req.get("before", "default-reset"))
        after = normalize_after(req.get("after", "hard-reset"))
        no_stub = bool(req.get("no_stub", False))
        no_compression = bool(req.get("no_compression", False))
        erase_all = bool(req.get("erase_all", False))
        dry_run = bool(req.get("dry_run", False))
        timeout = DEFAULT_FLASH_TIMEOUT
        if "timeout_s" in req:
            try:
                timeout = int(req["timeout_s"])
            except (TypeError, ValueError):
                raise BadRequest("'timeout_s' must be an integer")
            if not 10 <= timeout <= 3600:
                raise BadRequest("'timeout_s' out of range (10..3600)")
        return {
            "images": images, "port": port, "chip": chip, "baud": baud,
            "before": before, "after": after, "no_stub": no_stub,
            "no_compression": no_compression, "erase_all": erase_all,
            "dry_run": dry_run, "timeout": timeout,
        }

    def _host_file(self, value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise BadRequest("each image needs a 'file' host path")
        path = os.path.abspath(os.path.expanduser(value.strip()))
        if not os.path.isfile(path):
            raise BadRequest("file not found on host: %r" % path)
        return path

    # -- flash -------------------------------------------------------------
    def flash(self, req: dict):
        if not self.esptool:
            return 500, {"ok": False, "error":
                         "esptool not found (install it or pass --esptool)"}
        opts = self._parse_request(req)
        major = self.version[0] if self.version else 5
        cmd = build_esptool_command(
            self.esptool, major, images=opts["images"], port=opts["port"],
            chip=opts["chip"], baud=opts["baud"], before=opts["before"],
            after=opts["after"], no_stub=opts["no_stub"],
            no_compression=opts["no_compression"], erase_all=opts["erase_all"])
        if opts["dry_run"]:
            return 200, {"ok": True, "dry_run": True, "cmd": cmd}

        if not self.flash_lock.acquire(blocking=False):
            return 409, {"ok": False, "error": "flash already running, retry later"}
        Runtime.set_flash(True)
        t0 = time.time()
        log("flash requested: %s" % " ".join(cmd[1:]))
        # Preempt a console session, then take the session lock.  A new
        # client is rejected by main while the flash holds it.
        self.state.preempt.set()
        acquired = self.state.session_lock.acquire(timeout=self.flash_wait)
        self.state.preempt.clear()
        if not acquired:
            Runtime.set_flash(False)
            self.flash_lock.release()
            return 409, {"ok": False,
                         "error": "console session did not release the port "
                                  "within %ss; close it and retry" % self.flash_wait}
        try:
            rc, out = run_command(cmd, opts["timeout"])
            # Unknown esptool version: retry once with the v4 spelling if
            # the failure is an argument-parsing error (nothing was flashed).
            if rc != 0 and self.version is None and _looks_like_arg_error(out):
                log("esptool rejected the v5 syntax — retrying with v4 syntax")
                cmd = build_esptool_command(
                    self.esptool, 4, images=opts["images"], port=opts["port"],
                    chip=opts["chip"], baud=opts["baud"],
                    before=opts["before"], after=opts["after"],
                    no_stub=opts["no_stub"],
                    no_compression=opts["no_compression"],
                    erase_all=opts["erase_all"])
                rc, out = run_command(cmd, opts["timeout"])
            if rc != 0 and _looks_like_arg_error(out):
                # Very old esptool without --compress/--no-compress.
                if "--compress" in cmd or "--no-compress" in cmd:
                    log("esptool does not know the compression flags — retrying")
                    cmd = [c for c in cmd
                           if c not in ("--compress", "--no-compress")]
                    rc, out = run_command(cmd, opts["timeout"])
        except Exception as exc:  # noqa: BLE001
            rc, out = 1, "unexpected error: %r" % exc
            if VERBOSE:
                traceback.print_exc()
        finally:
            self.state.session_lock.release()
            Runtime.set_flash(False)
            self.flash_lock.release()

        elapsed = round(time.time() - t0, 1)
        log("flash done: exit=%d in %.1fs" % (rc, elapsed))
        return (200 if rc == 0 else 500), {
            "ok": rc == 0,
            "exit_code": rc,
            "elapsed_s": elapsed,
            "chip": opts["chip"] or "auto",
            "port": opts["port"],
            "cmd": cmd,
            "log": out[-LOG_TAIL:],
        }

    # -- reset -------------------------------------------------------------
    def reset(self, req: dict):
        """POST /reset: send the DTR/RTS reset sequence.

        The console session is NOT preempted: a connected client keeps the
        session and sees the boot output.  A concurrent flash is refused
        (the reset would fight esptool).
        """
        mode = str(req.get("mode", "run") or "run").strip().lower()
        if mode in ("run", "app", "application", "console", "reset"):
            action = "run"
        elif mode in ("download", "flash", "boot", "bootloader"):
            action = "download"
        else:
            raise BadRequest("'mode' must be run (application) or download")

        if not self.serial_enabled:
            return 503, {"ok": False,
                         "error": "serial relay disabled (--no-serial)"}
        if not self.flash_lock.acquire(blocking=False):
            return 409, {"ok": False, "error": "flash already running, retry later"}
        try:
            serial_active, _ = Runtime.snapshot()
            with self.state.lock:
                # With a live console session, do not close/reopen the node
                # behind the bridge's dup'd fd; only open it if it is absent.
                if serial_active:
                    if self.state.fd < 0 and not self.state.try_open_locked():
                        return 503, {"ok": False,
                                     "error": "serial port not available"}
                elif not self.state.ensure_ready():
                    return 503, {"ok": False, "error": "serial port not available"}
                fd = self.state.fd
                port = self.state.path
                ok = reset_run(fd) if action == "run" else reset_download(fd)
        finally:
            self.flash_lock.release()

        if not ok:
            return 500, {"ok": False,
                         "error": "DTR/RTS control unavailable; reset the board "
                                  "manually or use an adapter with line control"}
        log("reset requested via HTTP: %s (%s)" % (action, port))
        return 200, {
            "ok": True,
            "mode": action,
            "port": port,
            "message": "reset sequence sent (%s)" % action,
        }


class RelayHTTPHandler(BaseHTTPRequestHandler):
    app = None
    server_version = "SerialFlashRelay/" + VERSION
    sys_version = ""
    timeout = HTTP_TIMEOUT

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        if VERBOSE:
            log("http: " + (fmt % args))

    def _read_json(self) -> dict:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            body = b"{}"
        else:
            try:
                length = int(raw_len)
            except ValueError:
                raise BadRequest("invalid Content-Length")
            if length < 0 or length > MAX_BODY:
                raise BadRequest("request body too large (max %d bytes)" % MAX_BODY)
            body = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("bad JSON: %s" % exc)
        if not isinstance(req, dict):
            raise BadRequest("body must be a JSON object")
        return req

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz"):
            self._json(200, self.app.health())
        elif path in ("/", "/index.html"):
            self._json(200, {
                "service": "serial-flash-relay",
                "name": NAME,
                "role": self.app.role,
                "version": VERSION,
                "endpoints": {
                    "GET /health": "status",
                    "POST /flash": "flash a firmware",
                    "POST /reset": "reset the board (mode: run|download)",
                },
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/flash":
            handler = self.app.flash
        elif path == "/reset":
            handler = self.app.reset
        else:
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            code, obj = handler(self._read_json())
        except BadRequest as exc:
            code, obj = 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log("http handler error: %r" % exc)
            if VERBOSE:
                traceback.print_exc()
            code, obj = 500, {"ok": False, "error": "internal error: %s" % exc}
        self._json(code, obj)


def make_http_server(app: EspApp, bind: str, port: int) -> ThreadingHTTPServer:
    """Create an UNBOUND HTTP server (call server_bind/activate later).

    Keeping the bind separate lets allocate_ports() try an HTTP/TCP port
    pair and roll both back if either one is busy.
    """
    handler = type("BoundRelayHandler", (RelayHTTPHandler,), {"app": app})
    httpd = ThreadingHTTPServer((bind, port), handler, bind_and_activate=False)
    httpd.daemon_threads = True
    return httpd


def validate_name(value, default: str) -> str:
    name = (value if value is not None else default).strip()
    if not name:
        name = default
    if len(name) > 64:
        raise ValueError("--name must be at most 64 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("--name must not contain control characters")
    return name


def port_arg(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("invalid port: %r" % value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def try_bind_tcp(bind: str, port: int):
    """Bind+listen a TCP relay socket, or return None if the port is busy."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, port))
        sock.listen(1)
        sock.settimeout(0.5)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def allocate_ports(app: EspApp, bind: str, http_start: int, tcp_start: int,
                   auto: bool, tries: int = PORT_TRIES):
    """Bind a free HTTP/TCP port PAIR with matching offsets.

    Defaults are HTTP 8000 / TCP 4000.  If 8000 is busy, the next candidate
    is 8001/4001 -- never 8001/4000.  With auto=False the exact starting
    ports must be free.
    """
    last = "no candidate"
    attempts = 0
    for n in range(tries + 1):
        attempts += 1
        http_port = http_start + n
        tcp_port = tcp_start + n
        if http_port > 65535 or tcp_port > 65535:
            last = "port number exceeds 65535"
            break
        tcp_sock = try_bind_tcp(bind, tcp_port)
        if tcp_sock is None:
            last = "TCP %s:%d is busy" % (bind, tcp_port)
            if not auto:
                raise PortError(last)
            continue
        httpd = make_http_server(app, bind, http_port)
        try:
            httpd.server_bind()
            httpd.server_activate()
        except OSError as exc:
            httpd.server_close()
            tcp_sock.close()
            last = "HTTP %s:%d is busy (%s)" % (bind, http_port, exc)
            if not auto:
                raise PortError(last)
            continue
        return httpd, tcp_sock, http_port, tcp_port
    raise PortError("no free HTTP/TCP port pair after %d tries (%s)" %
                    (attempts, last))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    global VERBOSE, NAME

    ap = argparse.ArgumentParser(
        description="ESP32/ESP8266/ESP8285 serial relay + HTTP flash server "
                    "(local esptool).")
    ap.add_argument("--port", default=DEFAULT_PORT,
                    help="serial device (default %s)" % DEFAULT_PORT)
    ap.add_argument("--name", default=DEFAULT_NAME, metavar="NAME",
                    help="name shown in logs and /health (default %s)" % DEFAULT_NAME)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--tcp", type=port_arg, default=None,
                    help="console relay TCP start port (default %d; "
                         "auto-increments together with --http)" % DEFAULT_TCP)
    ap.add_argument("--http", type=port_arg, default=None,
                    help="flash API HTTP start port (default %d; "
                         "auto-increments together with --tcp)" % DEFAULT_HTTP)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="bind address for both services (default 127.0.0.1)")
    ap.add_argument("--no-auto-port", action="store_true",
                    help="use the exact starting ports and fail if they are busy")
    ap.add_argument("--esptool", default="",
                    help="esptool binary (default: auto-detect)")
    ap.add_argument("--chip", default=DEFAULT_CHIP,
                    help="default chip for /flash (default auto = let esptool "
                         "detect; esp8285 is an alias for esp8266)")
    ap.add_argument("--reset", choices=("auto", "none"), default="auto",
                    help="DTR/RTS reset on connect (default auto; use none "
                         "for adapters without line control)")
    ap.add_argument("--no-rescan", action="store_true",
                    help="do not wait for/reopen the device after a loss")
    ap.add_argument("--rescan-timeout", type=int,
                    default=DEFAULT_RESCAN_TIMEOUT,
                    help="seconds to wait for the device to return (0 = forever)")
    ap.add_argument("--ident-interval", type=float,
                    default=DEFAULT_IDENT_INTERVAL,
                    help="seconds between /dev node identity checks (default 5)")
    ap.add_argument("--flash-wait", type=float, default=DEFAULT_FLASH_WAIT,
                    help="seconds to wait for a console session to release the "
                         "port before flashing (default %d)" % DEFAULT_FLASH_WAIT)
    ap.add_argument("--no-serial", action="store_true",
                    help="disable the serial relay (HTTP flash API only)")
    ap.add_argument("--verbose", action="store_true",
                    help="log subprocess commands and HTTP requests")
    ap.add_argument("--version", action="version",
                    version="%(prog)s " + VERSION)
    args = ap.parse_args(argv)
    VERBOSE = args.verbose
    try:
        NAME = validate_name(args.name, DEFAULT_NAME)
    except ValueError as exc:
        sys.exit("ERROR: %s" % exc)
    http_start = args.http if args.http is not None else DEFAULT_HTTP
    tcp_start = args.tcp if args.tcp is not None else DEFAULT_TCP

    started = time.time()
    stop = threading.Event()

    def _on_signal(signum, _frame):
        log("signal %d received — stopping" % signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        default_chip = normalize_chip(args.chip)
    except BadRequest as exc:
        sys.exit("ERROR: %s" % exc)
    if not default_chip:
        default_chip = "auto"
    try:
        esptool = resolve_esptool(args.esptool)
        version = esptool_version(esptool) if esptool else None
    except BadRequest as exc:
        sys.exit("ERROR: %s" % exc)
    if esptool:
        log("esptool: %s (version %s)" % (
            esptool,
            ".".join(str(x) for x in version) if version else "unknown"))
    else:
        log("warning: esptool not found — POST /flash will fail "
            "(install esptool or pass --esptool)")

    state = ESPState(
        path=args.port,
        baud=args.baud,
        rescan=not args.no_rescan,
        rescan_timeout=args.rescan_timeout,
    )
    app = EspApp(state, esptool=esptool, version=version,
                 default_chip=default_chip, flash_wait=args.flash_wait,
                 started=started)
    app.serial_enabled = not args.no_serial

    # Start the HTTP API first: a missing serial device must not prevent
    # flashing (esptool opens the port itself).  The TCP relay socket and
    # the HTTP server are allocated as a pair so their suffixes stay aligned
    # (default 8000/4000; if 8000 is busy, 8001/4001 -- never 8001/4000).
    try:
        httpd, srv, http_port, tcp_port = allocate_ports(
            app, args.bind, http_start, tcp_start, not args.no_auto_port)
    except PortError as exc:
        sys.exit("ERROR: %s" % exc)
    app.http_port = http_port
    app.tcp_port = tcp_port
    if (http_port, tcp_port) != (http_start, tcp_start):
        log("requested port pair HTTP %d / TCP %d is unavailable — using %d / %d" % (
            http_start, tcp_start, http_port, tcp_port))
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log("flash API on http://%s:%d (name: %s, esptool: %s)" % (
        args.bind, http_port, NAME, esptool or "NOT FOUND"))

    if not args.no_serial:
        try:
            state.open_now()
        except SerialError as exc:
            if not state.rescan:
                sys.exit("ERROR: %s" % exc)
            log("warning: %s — will retry when a client connects" % exc)
    else:
        log("--no-serial: serial relay disabled (flash API only)")

    if stop.is_set():
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.close()
        except OSError:
            pass
        state.drop()
        log("stopped")
        return

    log("serial relay on tcp://%s:%d (Ctrl-C to stop)" % (args.bind, tcp_port))

    bridge = Bridge(state, ident_interval=args.ident_interval,
                    allow_rescan=not args.no_rescan, reset_mode=args.reset,
                    stop=stop)
    try:
        while not stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if stop.is_set():
                    break
                log("accept failed: %s" % exc)
                time.sleep(0.2)
                continue
            log("client connected from %s" % (addr,))
            if args.no_serial:
                log("serial relay disabled; closing client")
                conn.close()
                continue
            if not state.session_lock.acquire(blocking=False):
                log("client rejected: port busy (flash in progress)")
                conn.close()
                continue
            try:
                bridge.serve(conn)
            except Exception as exc:  # noqa: BLE001
                log("client session crashed: %r" % exc)
                if VERBOSE:
                    traceback.print_exc()
            finally:
                state.session_lock.release()
                try:
                    conn.close()
                except OSError:
                    pass
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.close()
        except OSError:
            pass
        state.drop()
        log("stopped")


if __name__ == "__main__":
    main()
