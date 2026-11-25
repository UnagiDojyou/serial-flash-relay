#!/usr/bin/env python3
"""
serial_relay.py — a plain serial <-> TCP relay.  No flashing, no resets.

This is the smallest tool in the repository: every byte that arrives on the
TCP socket is written to the serial port and vice versa, unchanged.  There
is no HTTP API, no esptool/picotool, no board-specific reset and no
"ESPRELAY:" prefix protocol.

Typical uses:
  * watch/control any USB or UART serial device from another process
  * expose a router / switch / GPS / Arduino console to a sandbox
  * a simple PTY-less alternative to socat when you want one host process
    to own the serial port

Behaviour:
  * one TCP client at a time; additional connections wait in the listen
    backlog until the current client disconnects
  * the serial port is opened at startup and kept open between sessions
  * on connect, the /dev node identity (st_dev, st_ino) is checked and the
    port is reopened if the device was replaced behind the same name
  * if the device disappears during a session, the client is disconnected;
    the next client tries to reopen the port (the process keeps running)
  * no DTR/RTS toggling; HUPCL is cleared so closing the relay does not
    reset the device

Linux / POSIX (termios only; no DTR/RTS control).  Pure standard library.

Usage (host):
  python3 serial_relay.py --port /dev/ttyUSB0
  python3 serial_relay.py --port /dev/ttyUSB0 --baud 9600 --tcp 4000
  python3 serial_relay.py --port /dev/ttyUSB0 --wait 30   # wait for the device

Then connect any TCP client to 127.0.0.1:4000 (default; the port
auto-increments to 4001, 4002, ... when busy).  Example:
  python3 - <<'EOF'
  import socket
  s = socket.create_connection(("127.0.0.1", 4000))
  s.sendall(b"AT\\r\\n")
  print(s.recv(4096))
  EOF

For ESP32/ESP8266/ESP8285 (DTR/RTS reset + HTTP flash) use esp_relay.py.
For RP2040/RP2350 (re-enumeration tracking + picotool/BOOTSEL) use
rp_relay.py.
"""

import argparse
import os
import select
import signal
import socket
import sys
import termios
import threading
import time
import traceback

VERSION = "1.0.0"

DEFAULT_TCP = 4000
DEFAULT_BAUD = 115200
DEFAULT_NAME = "serial-relay"
PORT_TRIES = 100              # auto-port: number of +1 attempts

NAME = DEFAULT_NAME
VERBOSE = False


# ---------------------------------------------------------------------------
# logging / errors
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print("%s [%s] %s" % (time.strftime("%H:%M:%S"), NAME, msg), flush=True)


class SerialError(RuntimeError):
    """The serial device could not be opened/configured/used."""


class PortError(RuntimeError):
    """No free TCP port could be bound."""


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
        attrs[0] = 0                       # raw input
        attrs[1] = 0                       # raw output
        cflag = termios.CREAD | termios.CLOCAL | termios.CS8 | _baud_constant(baud)
        if hasattr(termios, "HUPCL"):
            # Do not drop DTR when the relay closes the port; some boards
            # treat that as a reset.  This relay never toggles DTR/RTS.
            cflag &= ~termios.HUPCL
        attrs[2] = cflag
        attrs[3] = 0                       # raw local flags
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
            select.select([], [conn], [], remain)
            continue
        except (OSError, ValueError) as exc:
            raise OSError("client send failed: %s" % exc)
        if n <= 0:
            raise OSError("client send returned 0 (connection closed)")
        view = view[n:]


# ---------------------------------------------------------------------------
# serial port ownership
# ---------------------------------------------------------------------------

class SerialPort:
    """Keeps the tty open across client sessions and reopens it on demand."""

    def __init__(self, path: str, baud: int):
        self.lock = threading.RLock()
        self.path = path
        self.baud = baud
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

    def _log_error(self, exc) -> None:
        msg = str(exc)
        now = time.monotonic()
        if msg != self._last_error or now - self._last_error_at >= 10.0:
            log("serial open failed: %s" % msg)
            self._last_error = msg
            self._last_error_at = now

    def ensure(self) -> bool:
        """Return True if the port is ready, reopening it if necessary."""
        with self.lock:
            if self.fd >= 0:
                cur = tty_identity(self.path)
                if cur is not None and (self.ident is None or cur == self.ident):
                    return True
                log("serial node changed or disappeared — reopening")
                self._close_locked()
            try:
                fd = open_serial(self.path, self.baud)
            except SerialError as exc:
                self._log_error(exc)
                return False
            self.fd = fd
            self.ident = tty_identity(self.path)
            self._last_error = ""
            log("opened %s @ %d baud" % (self.path, self.baud))
            return True

    def drop(self) -> None:
        with self.lock:
            self._close_locked()


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, port: SerialPort, stop: threading.Event):
        self.port = port
        self.stop = stop

    def serve(self, conn: socket.socket) -> None:
        port = self.port
        # force a real reopen on every session start: when the USB device
        # re-enumerates (flash reset / replug) the node keeps the same
        # identity (inode), so a cached fd silently points at the dead
        # cdev; open() again always resolves to the current device
        port.drop()
        if not port.ensure():
            log("serial device unavailable — closing client")
            return
        conn.setblocking(False)
        rfd = -1
        n_to_client = 0
        n_to_uart = 0
        try:
            rfd = os.dup(port.fd)
            while not self.stop.is_set():
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
                            log("serial read error: %s" % exc)
                            data = b""
                        if not data:
                            log("serial device lost — closing client")
                            port.drop()
                            return
                        n_to_client += len(data)
                        try:
                            send_all(conn, data)
                        except OSError as exc:
                            log("client gone (%s)" % exc)
                            return
                    else:
                        try:
                            data = conn.recv(4096)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError:
                            data = b""
                        if not data:
                            log("client closed")
                            return
                        n_to_uart += len(data)
                        try:
                            uart_write(port.fd, data)
                        except SerialError as exc:
                            log("serial write failed: %s" % exc)
                            port.drop()
                            return
        except OSError as exc:
            log("session error: %s" % exc)
            if VERBOSE:
                traceback.print_exc()
        finally:
            if rfd >= 0:
                try:
                    os.close(rfd)
                except OSError:
                    pass
            # Keep the port open for the next client, but forget a dead fd
            # so the next connection performs a real reopen.
            if port.fd >= 0:
                cur = tty_identity(port.path)
                if cur is None or (port.ident is not None and cur != port.ident):
                    port.drop()
            log("session ended (bytes to_client=%d, to_uart=%d)" %
                (n_to_client, n_to_uart))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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
    """Bind+listen a TCP socket, or return None if the port is busy."""
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


def allocate_tcp(bind: str, start: int, auto: bool, tries: int = PORT_TRIES):
    """Bind the first free TCP port starting at `start` (default 4000)."""
    last = "no candidate"
    attempts = 0
    for n in range(tries + 1):
        attempts += 1
        port = start + n
        if port > 65535:
            last = "port number exceeds 65535"
            break
        sock = try_bind_tcp(bind, port)
        if sock is not None:
            return sock, port
        last = "TCP %s:%d is busy" % (bind, port)
        if not auto:
            raise PortError(last)
    raise PortError("no free TCP port after %d tries (%s)" % (attempts, last))


def main(argv=None) -> None:
    global VERBOSE, NAME

    ap = argparse.ArgumentParser(
        description="Plain serial <-> TCP relay (no flashing, no resets).")
    ap.add_argument("--port", required=True,
                    help="serial device, e.g. /dev/ttyUSB0 or /dev/ttyACM0")
    ap.add_argument("--name", default=DEFAULT_NAME, metavar="NAME",
                    help="name shown in logs (default %s)" % DEFAULT_NAME)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                    help="baud rate (default %d)" % DEFAULT_BAUD)
    ap.add_argument("--tcp", type=port_arg, default=None,
                    help="TCP start port (default %d; auto-increments when busy)"
                         % DEFAULT_TCP)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="TCP bind address (default 127.0.0.1)")
    ap.add_argument("--no-auto-port", action="store_true",
                    help="use the exact TCP start port and fail if it is busy")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="seconds to wait for the serial device at startup "
                         "(default 0 = fail immediately)")
    ap.add_argument("--verbose", action="store_true",
                    help="print tracebacks for session errors")
    ap.add_argument("--version", action="version",
                    version="%(prog)s " + VERSION)
    args = ap.parse_args(argv)
    VERBOSE = args.verbose
    try:
        NAME = validate_name(args.name, DEFAULT_NAME)
    except ValueError as exc:
        sys.exit("ERROR: %s" % exc)
    tcp_start = args.tcp if args.tcp is not None else DEFAULT_TCP

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log("signal %d received — stopping" % signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    port = SerialPort(args.port, args.baud)
    deadline = time.monotonic() + max(0.0, args.wait)
    last_note = 0.0
    while not stop.is_set():
        if port.ensure():
            break
        now = time.monotonic()
        if args.wait <= 0 or now >= deadline:
            sys.exit(
                "ERROR: cannot open %s. Is the device connected and is your "
                "user in the 'dialout' group? (--wait N waits for it)" % args.port
            )
        if now - last_note >= 10.0:
            log("waiting for %s (up to %.0fs) ..." % (args.port, args.wait))
            last_note = now
        time.sleep(0.5)

    if stop.is_set():
        port.drop()
        log("stopped")
        return

    try:
        srv, tcp_port = allocate_tcp(args.bind, tcp_start,
                                     not args.no_auto_port)
    except PortError as exc:
        port.drop()
        sys.exit("ERROR: %s" % exc)
    if tcp_port != tcp_start:
        log("requested TCP port %d is busy — using %d" % (tcp_start, tcp_port))
    log("relay ready: %s @ %d baud <-> tcp://%s:%d (Ctrl-C to stop)" % (
        args.port, args.baud, args.bind, tcp_port))

    bridge = Bridge(port, stop)
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
            srv.close()
        except OSError:
            pass
        port.drop()
        log("stopped")


if __name__ == "__main__":
    main()
