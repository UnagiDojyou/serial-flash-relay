#!/usr/bin/env python3
"""
rp_relay.py — serial relay + HTTP flash server for RP-series boards
(RP2040 / RP2350 family: Pico, Pico W, Pico 2, RP2040 + ESP8285 clones,
third-party RP2040/RP2350 boards, ...).

Two services in one process (pure stdlib, Linux only):

  1. SERIAL RELAY  TCP 127.0.0.1:4000  <->  /dev/ttyACM* (USB CDC)
     (the TCP/HTTP ports auto-increment by 1 when busy; paired with HTTP)
     Connect with any TCP client.  On connect an optional
     "ESPRELAY:<mode>\\n" prefix is absorbed; the mode is informational for
     this board (there is no ESP-style DTR/RTS boot strap circuit).

     The relay survives USB re-enumeration: after a flash / reset / replug
     the board usually comes back as a *different* ttyACM node, and the
     kernel may even reuse the same name for the new device.  The relay
     tracks the node by (st_dev, st_ino) — not by name — and reopens it,
     keeping the TCP session alive.  On the USBIP/vhci bus a dead fd may
     never become readable (select() just blocks, no EIO/EOF), so the node
     identity is also polled while the session runs.

  2. HTTP FLASH API  http://127.0.0.1:8000  (auto-increments; paired with TCP)
       GET  /health
       POST /flash  {"file": "/abs/path/firmware.uf2",
                     "method": "auto"}          # auto | picotool | storage

     "method":
       auto      picotool zero-touch (if available), then BOOTSEL storage
       picotool  force the picotool path (reset interface -> BOOTSEL)
       storage   copy the UF2 to the mounted BOOTSEL drive (RPI-RP2 / RP2350)

     The file path is read on the HOST.  No external flash script is used:
     picotool is located automatically and the BOOTSEL mass-storage
     fallback is implemented here.

Zero-touch flashing requires firmware that exposes the picotool reset
interface (pico-sdk USB stdio + uploadmethod=picotool):

  RP2040 (Pico / Pico W / RP-ESP):
    --fqbn rp2040:rp2040:rpipicow:uploadmethod=picotool
    --build-property "build.usbpid=-DUSBD_PID=0x000a"
    (the plain Pico already defaults to 0x000a; the Pico W defaults to
     0xf00a, so the override is required there)

  RP2350 (Pico 2 and other RP2350-family boards):
    --fqbn rp2040:rp2040:rpipico2:uploadmethod=picotool
    (the board default USB PID is already 0x000f; the explicit override
     -DUSBD_PID=0x000f is optional)

CircuitPython only exposes a CDC console: flash it once with BOOTSEL held,
then install an Arduino sketch built as above.

Known RP-series problems fixed in this version (see README for details):
  * dead/zombie tty fd (same name, new inode)  -> identity polling
  * fd leaks in the old rescan / reswap paths  -> old fd is always closed
  * a blocking 180 s rescan froze the bridge   -> non-blocking rescan loop
  * a partial ESPRELAY prefix leaked to UART   -> line-buffered prefix read
  * sys.exit() inside open_serial killed threads -> SerialError exceptions
  * wrong board picked when several share VID  -> match by USB serial;
                                                   never guess
  * hard-coded cflag baud / HUPCL resets       -> proper termios setup
  * external tools/flash_pico.sh dependency    -> flashing implemented here

Usage (host):
  python3 rp_relay.py --port /dev/ttyACM1
  python3 rp_relay.py                       # auto-detect by VID 2e8a
  python3 rp_relay.py --usb-serial 45474E621A04FA4A
  python3 rp_relay.py --no-serial           # flash API only

Only the Python standard library is required.  picotool is optional: if it
is missing, the BOOTSEL mass-storage fallback is used.
"""

import argparse
import glob
import getpass
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import termios
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

DEFAULT_TCP = 4000
DEFAULT_HTTP = 8000
DEFAULT_VID = "2e8a"          # Raspberry Pi (Pico, Pico 2, RP2040/RP2350)
DEFAULT_BAUD = 115200
DEFAULT_NAME = "rp-relay"
DEFAULT_IDENT_INTERVAL = 5.0  # s, poll the /dev node identity
DEFAULT_RESCAN_TIMEOUT = 180  # s, 0 = wait forever
HTTP_TIMEOUT = 30             # s, per HTTP connection
MAX_BODY = 1 << 20            # 1 MiB JSON limit
LOG_TAIL = 16000              # chars of subprocess output returned to clients
PICOTOOL_TIMEOUT = 30         # s, `picotool info`
PICOTOOL_LOAD_TIMEOUT = 300   # s, `picotool load`
PORT_TRIES = 100              # auto-port: number of +1 attempts

NAME = DEFAULT_NAME
VERBOSE = False


# ---------------------------------------------------------------------------
# logging / runtime state
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print("%s [%s] %s" % (time.strftime("%H:%M:%S"), NAME, msg), flush=True)


class Runtime:
    """Small thread-safe state shared with the HTTP health endpoint."""

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
    """Invalid HTTP request body (-> 400)."""


class SerialError(RuntimeError):
    """The serial device could not be opened/configured/used."""


class FlashError(RuntimeError):
    """A flash method failed (message already contains the output)."""


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


def _ensure_dev_node(path: str) -> bool:
    """Create the /dev node for a live tty when udev is absent.

    On a static /dev the node for a drifted tty (the kernel assigned a
    different minor after re-enumeration) never appears; recreate it from
    the devt in /sys.  Best effort: silently give up when /dev forbids
    mknod (nodev mount or missing capability).
    """
    name = os.path.basename(path)
    devfile = "/sys/class/tty/%s/dev" % name
    try:
        fields = dict(
            line.split("=", 1) for line in open(devfile).read().split() if "=" in line)
        devt = os.makedev(int(fields["MAJOR"]), int(fields["MINOR"]))
    except (OSError, ValueError, KeyError):
        return False
    if os.path.exists(path):
        return True
    try:
        os.mknod(path, 0o666 | stat.S_IFCHR, devt)
        log("created %s (no udev)" % path)
        return True
    except OSError as exc:
        log("mknod %s failed: %s" % (path, exc))
        return False


def open_serial(path: str, baud: int) -> int:
    """Open a tty in raw 8N1 mode, non-blocking.  Raises SerialError."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except FileNotFoundError:
        if not _ensure_dev_node(path):
            raise SerialError("%s: no such device" % path)
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except PermissionError:
        raise SerialError(
            "%s: permission denied — add your user to the 'dialout' group "
            "and re-login (check with `groups`)" % path
        )
    except OSError as exc:
        raise SerialError("%s: %s" % (path, exc))

    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                       # iflag: no IXON/IXOFF/ICRNL
        attrs[1] = 0                       # oflag: no OPOST
        cflag = termios.CREAD | termios.CLOCAL | termios.CS8 | _baud_constant(baud)
        if hasattr(termios, "HUPCL"):
            # Do not drop DTR when the relay closes the port (would reset
            # the board).  esptool/picotool manage resets themselves.
            cflag &= ~termios.HUPCL
        attrs[2] = cflag
        attrs[3] = 0                       # lflag: raw
        speed = _baud_constant(baud)
        attrs[4] = speed                   # ispeed
        attrs[5] = speed                   # ospeed
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
    """Identity of the tty's USB device, or None when it does not exist.

    The /dev node inode changes whenever the USB device (re)enumerates,
    but ONLY when udev manages /dev.  On a static /dev (no udev) the node
    persists across re-enumerations with the same inode — so also include
    the sysfs device-directory inode: that directory is removed and
    recreated on every (re)enumeration, which detects the replacement
    even when the tty keeps the same name AND the same minor.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    ident = (st.st_dev, st.st_ino)
    try:
        devdir = os.path.realpath(
            "/sys/class/tty/%s/device" % os.path.basename(path))
        ident = ident + (os.stat(devdir).st_ino,)
    except OSError:
        pass
    return ident


def _read_sys(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def list_tties(vid: str):
    """[(tty_path, usb_serial)] of LIVE USB CDC ttyACM nodes for `vid`.

    Scans /sys/bus/usb/devices/*:* rather than /sys/class/tty: entries
    disappear when the device detaches, so stale nodes are not returned.
    The tty directory hangs off the interface that owns it (usually the
    CDC data interface, class 0x0A), so no interface-class filter is used
    — a ttyACM child + the parent device's idVendor is the reliable test.
    """
    vid = (vid or "").lower()
    out = []
    try:
        interfaces = glob.glob("/sys/bus/usb/devices/*:*")
    except OSError:
        return []
    for ifpath in sorted(interfaces):
        ttydir = os.path.join(ifpath, "tty")
        if not os.path.isdir(ttydir):
            continue
        base = os.path.basename(ifpath)
        devdir = os.path.join(os.path.dirname(ifpath), base.split(":")[0])
        if _read_sys(os.path.join(devdir, "idVendor")).lower() != vid:
            continue
        usb_serial = _read_sys(os.path.join(devdir, "serial"))
        try:
            names = os.listdir(ttydir)
        except OSError:
            continue
        for name in sorted(names):
            if not name.startswith("ttyACM"):
                continue
            # do NOT require the /dev node to exist: on a static /dev
            # (no udev) a drifted tty has no node; open_serial recreates
            # it via mknod when possible
            out.append(("/dev/" + name, usb_serial))
    seen = set()
    unique = []
    for path, serial in out:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, serial))
    return unique


def serial_for_path(path: str, vid: str) -> str:
    real = os.path.realpath(path)
    for dev, serial in list_tties(vid):
        if os.path.realpath(dev) == real:
            return serial
    return ""


def uart_write(fd: int, data: bytes, timeout: float = 10.0) -> None:
    """Write all bytes to a non-blocking tty.  Raises SerialError."""
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
    """Write all bytes to a non-blocking socket.  Raises OSError."""
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
    """Read the optional "ESPRELAY:<mode>\\n" line from the client.

    Returns (mode, leftover):
      * (None, b"")        -> client sent nothing (timeout)
      * (None, None)       -> client closed the connection
      * (None, bytes)      -> ordinary data, not a prefix
      * ("mode", bytes)    -> prefix absorbed, leftover must be forwarded

    The old code did a single recv(64) and treated any chunk starting with
    "ESPRELAY:" as the whole line, leaking a split prefix ("ESPRELAY:con"
    + "sole\\n") into the UART.  Read until newline (bounded) instead.
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
            # Malformed/overlong prefix: forward it rather than dropping it.
            return None, buf
    if not buf.startswith(b"ESPRELAY:"):
        return None, buf
    line, _, rest = buf.partition(b"\n")
    mode = line[len(b"ESPRELAY:"):].strip().decode("ascii", "replace").lower()
    return mode, rest


# ---------------------------------------------------------------------------
# serial state (device tracking / reopening)
# ---------------------------------------------------------------------------

class SerialState:
    """Owns the live serial fd and knows how to reopen it after a loss."""

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

    # -- low level (call with self.lock held) -------------------------------
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
        """Open `path`, close the previous fd, update the identity."""
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

    # -- public ------------------------------------------------------------
    def find_replacement(self):
        """Path to reopen, or None.  Base class: the configured path."""
        return self.path if self.path and os.path.exists(self.path) else None

    def try_open(self) -> bool:
        with self.lock:
            path = self.find_replacement()
            if not path:
                return False
            return self._open_locked(path)

    def drop(self) -> None:
        with self.lock:
            self._close_locked()

    def ensure_ready(self) -> bool:
        """Make sure fd is a LIVE node; reopen it if it was replaced.

        Called when a client connects: a re-enumeration can happen while
        no client is attached (flash, replug) and leave state.fd on a dead
        node.  Writing to it would fail on the first client byte.
        """
        with self.lock:
            if self.fd >= 0:
                cur = tty_identity(self.path)
                if cur is not None and (self.ident is None or cur == self.ident):
                    return True
                log("serial node changed or disappeared — reopening")
                self._close_locked()
            return self.try_open_locked()

    def try_open_locked(self) -> bool:
        path = self.find_replacement()
        if not path:
            return False
        return self._open_locked(path)

    def refresh(self) -> bool:
        """Reopen if the node identity changed.  True if fd was replaced."""
        with self.lock:
            if self.fd < 0:
                return self.try_open_locked()
            cur = tty_identity(self.path)
            if cur is not None and self.ident is not None and cur == self.ident:
                return False
            log("device re-enumerated (%s) — swapping serial fd" % self.path)
            self._close_locked()
            return self.try_open_locked()


class RPState(SerialState):
    """RP-series state: re-detect the tty by USB VID + serial number."""

    def __init__(self, path: str, baud: int, *, vid: str = DEFAULT_VID,
                 usb_serial: str = "", rescan: bool = True,
                 rescan_timeout: float = DEFAULT_RESCAN_TIMEOUT):
        super().__init__(path, baud, rescan=rescan, rescan_timeout=rescan_timeout)
        self.vid = (vid or DEFAULT_VID).lower()
        self.usb_serial = usb_serial or ""
        self._scan_at = 0.0
        self._scan_cache = []
        self._last_multi_log = 0.0

    def scan(self):
        now = time.monotonic()
        if now - self._scan_at >= 1.0:
            self._scan_cache = list_tties(self.vid)
            self._scan_at = now
        return self._scan_cache

    def find_replacement(self):
        cands = self.scan()
        if self.usb_serial:
            for path, serial in cands:
                if serial and serial == self.usb_serial:
                    return path
            # The board we track is absent.  Never grab a different board
            # that happens to share the VID.  Only fall back to the
            # configured path when the USB scan cannot see *any* CDC device
            # (virtual ports, boards with another VID, ...).
            if not cands and self.path and os.path.exists(self.path):
                return self.path
            return None
        if len(cands) == 1:
            return cands[0][0]
        if len(cands) > 1:
            now = time.monotonic()
            if now - self._last_multi_log >= 30.0:
                log("several VID:%s CDC devices and no --usb-serial — refusing "
                    "to guess" % self.vid)
                self._last_multi_log = now
            return None
        # No USB CDC device found: fall back to the explicitly configured
        # path (virtual/pty ports and RP2040/RP2350 boards with another VID).
        if self.path and os.path.exists(self.path):
            return self.path
        return None

    def open_now(self) -> None:
        """Open the current path; raises SerialError."""
        with self.lock:
            fd = open_serial(self.path, self.baud)
            old = self.fd
            self.fd = fd
            self.ident = tty_identity(self.path)
            if not self.usb_serial:
                self.usb_serial = serial_for_path(self.path, self.vid)
            if old >= 0:
                try:
                    os.close(old)
                except OSError:
                    pass
        log("opened %s @ %d baud (auto-rescan %s, serial %s)" % (
            self.path, self.baud, "on" if self.rescan else "off",
            self.usb_serial or "n/a"))


# ---------------------------------------------------------------------------
# serial <-> TCP bridge
# ---------------------------------------------------------------------------

class Bridge:
    """One TCP client bridged to the serial port; survives re-enumeration."""

    def __init__(self, state: SerialState, *, ident_interval: float,
                 allow_rescan: bool = True, stop=None):
        self.state = state
        self.ident_interval = max(0.2, float(ident_interval))
        self.allow_rescan = allow_rescan
        self.stop = stop

    def _deadline(self):
        timeout = self.state.rescan_timeout
        if timeout is None or timeout <= 0:
            return None
        return time.monotonic() + timeout

    def serve(self, conn: socket.socket) -> None:
        st = self.state
        Runtime.set_serial(True)
        rfd = -1
        n_to_client = 0
        n_to_uart = 0
        try:
            # Force a real reopen on every session start.  The cached fd goes
            # stale whenever the USB device re-enumerates behind the same node
            # (a board reboot through the USBIP link is a soft reset: the /dev
            # node AND the sysfs device directory keep their identity, so
            # identity-based detection cannot see the replacement).  A fresh
            # open() always resolves the node's devt to the current cdev.
            st.drop()
            ready = st.ensure_ready()
            if not ready and not (self.allow_rescan and st.rescan):
                log("serial device unavailable — closing client")
                return
            if not ready:
                log("serial device unavailable — waiting up to %ss ..." %
                    (st.rescan_timeout if st.rescan_timeout > 0 else "∞"))

            mode, leftover = read_client_prefix(conn)
            if mode is None and leftover is None:
                log("client closed before the bridge started")
                return
            if mode:
                log("client mode prefix absorbed: %s (no action for this device)" % mode)

            conn.setblocking(False)
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
                now = time.monotonic()
                if rfd < 0:
                    # ---- device absent: wait for it, but keep listening ---
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

                # ---- device present: poll identity, then pump bytes -------
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
        except Exception as exc:  # noqa: BLE001 — never kill the daemon
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
# picotool / BOOTSEL mass storage
# ---------------------------------------------------------------------------

def run_command(cmd, timeout: float):
    """Run a command; returns (exit_code, combined output)."""
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


def _version_key(path: str):
    for part in reversed(os.path.dirname(path).split(os.sep)):
        m = re.fullmatch(r"(\d+(?:\.\d+)*)", part)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
    return ()


def resolve_picotool(explicit: str = "") -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("PICOTOOL", "")
    if env:
        candidates.append(env)
    which = shutil.which("picotool")
    if which:
        candidates.append(which)
    candidates.append(os.path.expanduser("~/.local/bin/picotool"))
    core = os.environ.get("ARDUINO_DATA_DIR", os.path.expanduser("~/.arduino15"))
    candidates.extend(sorted(
        glob.glob(os.path.join(core, "packages", "*", "tools",
                               "pqt-picotool", "*", "picotool")),
        key=_version_key, reverse=True))
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return ""


def picotool_kind(exe: str) -> str:
    """'rust' for pqt-picotool (v2.x), 'python' for the legacy Python tool."""
    rc, _ = run_command([exe, "version"], 10)
    return "rust" if rc == 0 else "python"


def picotool_load_cmd(exe: str, kind: str, firmware: str, force: bool):
    if kind == "rust":
        cmd = [exe, "load"]
        if force:
            cmd.append("--force")
        cmd.extend(["-v", "-x", firmware])
    else:
        cmd = [exe, "-v"]
        if force:
            cmd.append("--force")
        cmd.extend(["load", "--firmware", firmware])
    return cmd


def picotool_flash(exe: str, kind: str, firmware: str) -> str:
    """Flash with picotool (zero-touch if the firmware exposes reset iface)."""
    parts = []
    rc, info = run_command([exe, "info"], PICOTOOL_TIMEOUT)
    parts.append("$ %s\n%s" % (shlex.join([exe, "info"]), info.rstrip()))
    low = info.lower()
    if rc == 0:
        mode = "plain"
    elif "consider -f" in info:
        mode = "force"
    elif "unable to connect" in low:
        raise FlashError(
            "\n".join(parts) +
            "\nERROR: picotool cannot access the USB device (permissions). "
            "Add a udev rule for VID 2e8a or run as a user with USB access:\n"
            "  echo 'SUBSYSTEM==\"usb\", ATTR{idVendor}==\"2e8a\", "
            "MODE=\"0660\", GROUP=\"dialout\"' | sudo tee "
            "/etc/udev/rules.d/99-pico.rules\n"
            "  sudo udevadm control --reload && sudo udevadm trigger"
        )
    elif "no accessible" in low or "no rp-series" in low:
        raise FlashError(
            "\n".join(parts) +
            "\nERROR: no RP-series device accessible via picotool. "
            "Is the board connected? For zero-touch the running firmware "
            "must expose the picotool reset interface (uploadmethod=picotool)."
        )
    else:
        raise FlashError(
            "\n".join(parts) +
            "\nERROR: picotool info failed (exit %d)" % rc
        )

    attempts = [("plain", False)] if mode == "plain" else [("force", True)]
    if mode == "plain":
        attempts.append(("force", True))
    for label, force in attempts:
        cmd = picotool_load_cmd(exe, kind, firmware, force)
        rc, out = run_command(cmd, PICOTOOL_LOAD_TIMEOUT)
        parts.append("$ %s\n%s" % (shlex.join(cmd), out.rstrip()))
        if rc == 0:
            parts.append("picotool: flash OK (%s)" % label)
            return "\n".join(parts)
        if "unable to locate reset interface" in out.lower():
            parts.append(
                "HINT: the running firmware has no picotool reset interface "
                "(CircuitPython only exposes a CDC console). Hold BOOTSEL "
                "while plugging the board in, then retry with method=storage."
            )
            break
        if not force:
            parts.append("plain load failed; retrying with --force (zero-touch) ...")
    raise FlashError("\n".join(parts) + "\nERROR: picotool load failed")


def is_uf2(path: str) -> bool:
    try:
        if os.path.getsize(path) % 512 != 0:
            return False
        with open(path, "rb") as f:
            head = f.read(8)
        return head[:4] == b"UF2\n" and len(head) == 8
    except OSError:
        return False


def find_bootsel_mount() -> str:
    """Locate a mounted RP-series BOOTSEL drive.

    The volume label differs between families (RPI-RP2 on RP2040, RP2350
    on RP2350) and can be white-labelled via OTP, so instead of relying on
    the label we scan the usual auto-mount roots for a directory that
    contains INFO_UF2.TXT — the same heuristic the Arduino UF2 uploader
    uses.
    """
    env = os.environ.get("PICO_BOOTSEL_MOUNT", "")
    user = getpass.getuser()
    candidates = [
        env,
        "/media/%s/RPI-RP2" % user,
        "/run/media/%s/RPI-RP2" % user,
        "/media/%s/RP2350" % user,
        "/run/media/%s/RP2350" % user,
        "/media/RPI-RP2",
        "/run/media/RPI-RP2",
        "/media/RP2350",
        "/run/media/RP2350",
        "/mnt/RPI-RP2",
        "/mnt/RP2350",
        "/mnt/pico",
    ]
    for mnt in candidates:
        if mnt and os.path.isdir(mnt) and os.path.isfile(
                os.path.join(mnt, "INFO_UF2.TXT")):
            return mnt
    # Do not fall back to a directory that merely exists: /mnt/pico is often
    # a stale mount point, and copying there produced a confusing sudo error
    # instead of "no BOOTSEL drive".  Only a directory with INFO_UF2.TXT is
    # treated as a BOOTSEL drive.
    roots = [
        "/media/%s" % user, "/run/media/%s" % user,
        "/var/run/media/%s" % user, "/opt/media/%s" % user,
        "/media", "/run/media", "/var/run/media", "/opt/media",
    ]
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in sorted(entries):
            mnt = os.path.join(root, name)
            if os.path.isdir(mnt) and os.path.isfile(
                    os.path.join(mnt, "INFO_UF2.TXT")):
                return mnt
    return ""


def _find_removable_usb():
    rc, out = run_command(["lsblk", "-rnpo", "NAME,TYPE,RM,TRAN"], 10)
    if rc != 0:
        return ""
    disk = ""
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[1] == "disk" and cols[2] == "1" \
                and "usb" in cols[3].lower():
            disk = cols[0]
            break
    if not disk:
        return ""
    rc, out = run_command(["lsblk", "-rnpo", "NAME,TYPE", disk], 10)
    if rc != 0:
        return disk
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 2 and cols[1] == "part":
            return cols[0]
    return disk


def storage_flash(firmware: str) -> str:
    """Copy a UF2 to the mounted RP-series BOOTSEL drive (mounting if needed)."""
    if not is_uf2(firmware):
        raise FlashError(
            "storage flashing needs a .uf2 file (magic 'UF2\\n', size % 512 == 0)"
        )
    parts = []
    mnt = find_bootsel_mount()
    mounted_here = False
    if not mnt:
        dev = _find_removable_usb()
        if not dev:
            raise FlashError(
                "no BOOTSEL drive (RPI-RP2 / RP2350) found and no removable "
                "USB disk detected.\n"
                "Hold BOOTSEL while plugging the board in, or run:\n"
                "  lsblk\n"
                "and mount the BOOTSEL partition manually."
            )
        mnt = "/mnt/pico"
        rc, out = run_command(["sudo", "-n", "mkdir", "-p", mnt], 10)
        rc, out = run_command(["sudo", "-n", "mount", dev, mnt], 30)
        if rc != 0:
            raise FlashError(
                "cannot mount %s at %s (sudo -n failed).\n%s\n"
                "Mount it manually or run the relay as a user with mount "
                "permissions." % (dev, mnt, out.rstrip())
            )
        mounted_here = True
        parts.append("mounted %s at %s" % (dev, mnt))
        if not os.path.isfile(os.path.join(mnt, "INFO_UF2.TXT")):
            run_command(["sudo", "-n", "umount", mnt], 30)
            raise FlashError(
                "mounted %s but it is not an RP-series BOOTSEL drive "
                "(INFO_UF2.TXT is missing). Refusing to write to an unrelated "
                "USB disk." % dev
            )
    dst = os.path.join(mnt, os.path.basename(firmware))
    parts.append("BOOTSEL: %s" % mnt)
    try:
        shutil.copyfile(firmware, dst)
    except OSError:
        rc, out = run_command(["sudo", "-n", "cp", firmware, dst], 120)
        if rc != 0:
            if mounted_here:
                run_command(["sudo", "-n", "umount", mnt], 30)
            raise FlashError("copy to %s failed:\n%s" % (dst, out.rstrip()))
    try:
        os.sync()
    except (AttributeError, OSError):
        pass
    time.sleep(2)
    if mounted_here:
        run_command(["sudo", "-n", "umount", mnt], 30)
    parts.append("copied %s -> %s (%d bytes)" % (
        firmware, dst, os.path.getsize(firmware)))
    parts.append("BOOTSEL storage flash OK; the board reboots into the new firmware")
    return "\n".join(parts)


def flash_firmware(firmware: str, method: str, picotool: str, kind: str) -> dict:
    """Run the requested flash strategy.  Returns {ok, method, log}."""
    parts = []
    if method in ("auto", "picotool"):
        if not picotool:
            if method == "picotool":
                return {"ok": False, "method": "", "log":
                        "picotool not found (set PICOTOOL or pass --picotool)"}
            parts.append("[picotool] not found — using BOOTSEL mass storage")
        else:
            try:
                parts.append(picotool_flash(picotool, kind, firmware))
                return {"ok": True, "method": "picotool",
                        "log": "\n".join(parts)}
            except FlashError as exc:
                parts.append("[picotool] FAILED\n%s" % exc)
                if method == "picotool":
                    return {"ok": False, "method": "picotool",
                            "log": "\n".join(parts)}
                parts.append("[picotool] falling back to BOOTSEL mass storage")
    if method in ("auto", "storage"):
        try:
            parts.append(storage_flash(firmware))
            return {"ok": True, "method": "storage", "log": "\n".join(parts)}
        except FlashError as exc:
            parts.append("[storage] FAILED\n%s" % exc)
            return {"ok": False, "method": "storage", "log": "\n".join(parts)}
    return {"ok": False, "method": "", "log": "\n".join(parts)}


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class RPApp:
    role = "rp"

    def __init__(self, state: RPState, *, picotool: str, picotool_kind: str,
                 default_method: str, started: float):
        self.state = state
        self.picotool = picotool
        self.picotool_kind = picotool_kind
        self.default_method = default_method
        self.started = started
        self.flash_lock = threading.Lock()
        self.http_port = None
        self.tcp_port = None

    def health(self) -> dict:
        serial_active, flash_busy = Runtime.snapshot()
        with self.state.lock:
            port = self.state.path
            ready = self.state.fd >= 0
            usb_serial = self.state.usb_serial
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
            "usb_serial": usb_serial or None,
            "vid": self.state.vid,
            "baud": self.state.baud,
            "rescan": bool(self.state.rescan),
            "serial_active": serial_active,
            "flash_busy": flash_busy,
            "picotool": self.picotool or None,
            "picotool_kind": self.picotool_kind or None,
            "flash_method": self.default_method,
            "uptime_s": round(time.time() - self.started, 1),
        }

    def flash(self, req: dict):
        firmware = req.get("file")
        if not isinstance(firmware, str) or not firmware.strip():
            raise BadRequest("'file' must be a non-empty host path string")
        firmware = os.path.abspath(os.path.expanduser(firmware.strip()))
        if not os.path.isfile(firmware):
            raise BadRequest("file not found on host: %r" % firmware)

        method = str(req.get("method", self.default_method) or "auto").lower()
        if method not in ("auto", "picotool", "storage"):
            raise BadRequest("'method' must be auto, picotool or storage")
        if method == "storage" and not is_uf2(firmware):
            raise BadRequest("storage flashing requires a .uf2 file")

        if not self.flash_lock.acquire(blocking=False):
            return 409, {"ok": False, "error": "flash already running, retry later"}

        Runtime.set_flash(True)
        t0 = time.time()
        log("flash requested: %s (method=%s)" % (firmware, method))
        try:
            result = flash_firmware(firmware, method, self.picotool,
                                    self.picotool_kind)
        except Exception as exc:  # noqa: BLE001
            log("flash error: %r" % exc)
            if VERBOSE:
                traceback.print_exc()
            result = {"ok": False, "method": "", "log":
                      "unexpected flash error: %r" % exc}
        finally:
            Runtime.set_flash(False)
            self.flash_lock.release()

        elapsed = round(time.time() - t0, 1)
        log("flash done: ok=%s method=%s in %.1fs" % (
            result["ok"], result["method"] or "-", elapsed))
        return (200 if result["ok"] else 500), {
            "ok": bool(result["ok"]),
            "exit_code": 0 if result["ok"] else 1,
            "elapsed_s": elapsed,
            "method": result["method"] or None,
            "tool": self.picotool or None,
            "log": result["log"][-LOG_TAIL:],
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

    def log_message(self, fmt, *args):  # quiet unless --verbose
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
                "endpoints": {"GET /health": "status", "POST /flash": "flash a firmware"},
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/flash":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            code, obj = self.app.flash(self._read_json())
        except BadRequest as exc:
            code, obj = 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log("http handler error: %r" % exc)
            if VERBOSE:
                traceback.print_exc()
            code, obj = 500, {"ok": False, "error": "internal error: %s" % exc}
        self._json(code, obj)


def make_http_server(app: RPApp, bind: str, port: int) -> ThreadingHTTPServer:
    """Create an UNBOUND HTTP server (bind/activate in allocate_ports)."""
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


def allocate_ports(app: RPApp, bind: str, http_start: int, tcp_start: int,
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
# startup / main
# ---------------------------------------------------------------------------

def pick_initial(vid: str, usb_serial: str) -> str:
    cands = list_tties(vid)
    if usb_serial:
        for path, serial in cands:
            if serial and serial == usb_serial:
                return path
        return ""
    if len(cands) == 1:
        return cands[0][0]
    return ""


def resolve_startup_port(args, stop) -> str:
    """Return the tty to open, waiting for it when auto-rescan is on."""
    if args.wait_timeout > 0:
        deadline = time.monotonic() + args.wait_timeout
    elif args.no_rescan:
        # --no-rescan: do not wait forever at startup; give the kernel a
        # few seconds to finish enumerating, then fail fast.
        deadline = time.monotonic() + 3.0
    else:
        deadline = None
    last_note = time.monotonic()
    while not stop.is_set():
        if args.port and os.path.exists(args.port):
            return args.port
        found = pick_initial(args.vid, args.usb_serial)
        if found:
            log("device found: %s" % found)
            return found
        if deadline is not None and time.monotonic() >= deadline:
            if not args.no_rescan:
                log("no device after %ss — continuing without a serial port "
                    "(auto-rescan will keep trying)" % args.wait_timeout)
                return args.port
            sys.exit("ERROR: no RP-series serial device found (VID %s%s)" % (
                args.vid, ", serial %s" % args.usb_serial if args.usb_serial else ""))
        now = time.monotonic()
        if now - last_note >= 30.0:
            cands = list_tties(args.vid)
            if len(cands) > 1 and not args.usb_serial:
                log("%d devices share VID %s — pass --usb-serial (or --port) "
                    "to choose one" % (len(cands), args.vid))
            else:
                log("waiting for a VID:%s CDC device%s ..." % (
                    args.vid,
                    " (serial %s)" % args.usb_serial if args.usb_serial else ""))
            last_note = now
        time.sleep(2)
    return ""


def main(argv=None) -> None:
    global VERBOSE, NAME

    ap = argparse.ArgumentParser(
        description="RP-series (RP2040/RP2350) serial relay + HTTP flash "
                    "server (picotool/BOOTSEL).")
    ap.add_argument("--port", default="",
                    help="serial device, e.g. /dev/ttyACM1 "
                         "(default: auto-detect by --vid/--usb-serial)")
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
    ap.add_argument("--vid", default=DEFAULT_VID,
                    help="USB vendor id used for re-detection (default 2e8a = "
                         "Raspberry Pi; use --vid for third-party RP2350 boards)")
    ap.add_argument("--usb-serial", default="",
                    help="USB serial number of the board (needed when several "
                         "boards share the VID)")
    ap.add_argument("--no-rescan", action="store_true",
                    help="do not track/reopen the device after a re-enumeration")
    ap.add_argument("--rescan-timeout", type=int,
                    default=DEFAULT_RESCAN_TIMEOUT,
                    help="seconds to wait for the device to return (0 = forever)")
    ap.add_argument("--ident-interval", type=float,
                    default=DEFAULT_IDENT_INTERVAL,
                    help="seconds between /dev node identity checks (default 5)")
    ap.add_argument("--no-serial", action="store_true",
                    help="disable the serial relay (HTTP flash API only)")
    ap.add_argument("--picotool", default="",
                    help="picotool binary (default: auto-detect)")
    ap.add_argument("--flash-method", choices=("auto", "picotool", "storage"),
                    default="auto",
                    help="default flash method for POST /flash (default auto)")
    ap.add_argument("--wait-timeout", type=int, default=0,
                    help="seconds to wait for a device at startup (0 = forever)")
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

    # Install the handlers early so Ctrl-C during the startup wait exits
    # cleanly instead of printing a KeyboardInterrupt traceback.
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    picotool = resolve_picotool(args.picotool)
    kind = picotool_kind(picotool) if picotool else ""
    if picotool:
        log("picotool: %s (%s)" % (picotool, kind))
    else:
        log("picotool not found — POST /flash will use BOOTSEL mass storage")

    state = RPState(
        path=args.port,
        baud=args.baud,
        vid=args.vid,
        usb_serial=args.usb_serial,
        rescan=not args.no_rescan,
        rescan_timeout=args.rescan_timeout,
    )
    app = RPApp(state, picotool=picotool, picotool_kind=kind,
                  default_method=args.flash_method, started=started)

    # Start the HTTP API first: it must work while the board is in BOOTSEL
    # or no serial device is present yet.  HTTP and TCP are allocated as a
    # pair so their suffixes stay aligned (default 8000/4000; if 8000 is
    # busy, 8001/4001 -- never 8001/4000).
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
    log("flash API on http://%s:%d (name: %s, picotool: %s, method: %s)" % (
        args.bind, http_port, NAME, picotool or "NOT FOUND", args.flash_method))

    if not args.no_serial and not stop.is_set():
        port = resolve_startup_port(args, stop)
        if stop.is_set():
            log("stopping before the serial relay was started")
        elif port:
            with state.lock:
                state.path = port
            try:
                state.open_now()
            except SerialError as exc:
                if not state.rescan:
                    sys.exit("ERROR: %s" % exc)
                log("warning: %s — will retry when a client connects" % exc)
        else:
            log("no serial device yet — the relay will keep looking")
    elif args.no_serial:
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
                    allow_rescan=not args.no_rescan, stop=stop)
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
            try:
                if args.no_serial:
                    log("serial relay disabled; closing client")
                else:
                    bridge.serve(conn)
            except Exception as exc:  # noqa: BLE001
                log("client session crashed: %r" % exc)
                if VERBOSE:
                    traceback.print_exc()
            finally:
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
