"""Reconnect/self-heal scenario tests for the sync Monoprice client.

Runs a fake serial-over-TCP bridge (like an ESP serial-TCP audio controller)
and validates: transparent same-call reconnect after a bridge reboot, fast
clean failure while the bridge is down, automatic recovery when it returns,
and that a bare read timeout (slow amp) does NOT reconnect. See also
test_monoprice.py for protocol-level tests.

Unlike test_monoprice.py (which uses a pty and is therefore POSIX-only), this
exercises the socket:// transport, so it runs on any platform.
"""
import socket
import socketserver
import threading
import time

import serialx

import pymonoprice as pm


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
ZONE_STATUS = b"\r\n#>1100010000131107100601\r\n#"
ACK = b"\r\n#"


class AmpHandler(socketserver.BaseRequestHandler):
    def setup(self):
        self.server.conns.add(self.request)

    def finish(self):
        self.server.conns.discard(self.request)

    def handle(self):
        buf = b""
        while True:
            try:
                data = self.request.recv(1)
            except OSError:
                return
            if not data:
                return
            buf += data
            if buf.endswith(b"\r"):
                cmd = buf
                buf = b""
                # Simulate a slow/quiet amp: the socket stays open and healthy
                # but the amp never answers this command, so the client's read
                # times out. Used to prove a bare timeout does NOT reconnect.
                if getattr(self.server, "silent", False):
                    continue
                try:
                    if cmd.startswith(b"?"):
                        self.request.sendall(ZONE_STATUS)
                    else:
                        self.request.sendall(ACK)
                except OSError:
                    return


class AmpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, *a, **kw):
        self.conns = set()
        self.silent = False
        super().__init__(*a, **kw)


class Bridge:
    """Start/stoppable fake bridge, like an ESP controller that can reboot."""
    def __init__(self):
        self.server = None
        self.thread = None

    def start(self):
        self.server = AmpServer(("127.0.0.1", PORT), AmpHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.1)

    def stop(self):
        """Simulate an ESP reboot: kill listener AND all live connections."""
        if self.server:
            for s in list(self.server.conns):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                    s.close()
                except OSError:
                    pass
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            time.sleep(0.2)


def check(name, cond):
    assert cond, name


def test_reconnect_scenarios():
    bridge = Bridge()
    bridge.start()

    # --- 1. Baseline ---
    amp = pm.get_monoprice(f"socket://127.0.0.1:{PORT}")
    amp.set_power(11, True)
    st = amp.zone_status(11)
    check("baseline set_power + zone_status", st is not None and st.zone == 11 and st.power)

    # --- 5. Keepalive set ---
    sock = getattr(amp._port, "_socket", None)
    ka = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    check("TCP keepalive enabled on socket:// port", ka == 1)

    # --- 2. Bridge 'reboots': old TCP session dies, bridge comes back up ---
    bridge.stop()
    bridge.start()
    # Old socket is now dead (server closed it). One call must transparently
    # reconnect and succeed WITHOUT the caller seeing any error.
    try:
        amp.set_power(12, True)
        recovered_in_same_call = True
    except Exception as e:  # noqa: BLE001
        recovered_in_same_call = False
        print("   unexpected:", repr(e))
    check("bridge reboot -> same-call transparent reconnect+retry", recovered_in_same_call)

    # --- 3. Bridge fully down: clean single exception, no hang ---
    bridge.stop()
    t0 = time.monotonic()
    try:
        amp.set_power(13, True)
        clean_error = False
    except pm.MonopriceConnectionError:
        clean_error = True
    except Exception as e:  # noqa: BLE001
        clean_error = False
        print("   wrong exception type:", repr(e))
    elapsed = time.monotonic() - t0
    check("bridge down -> MonopriceConnectionError (SerialException subclass)", clean_error)
    check("bridge down -> fails fast (<10s), no hang", elapsed < 10)
    check("port left closed for fresh retry next call", amp._port is None)

    # --- 4. Bridge returns: next call self-heals, no restart of anything ---
    bridge.start()
    try:
        amp.set_power(14, True)
        st = amp.zone_status(11)
        healed = st is not None and st.zone == 11
    except Exception as e:  # noqa: BLE001
        healed = False
        print("   unexpected:", repr(e))
    check("bridge back -> next call self-heals automatically", healed)

    # Backward-compat: MonopriceConnectionError is catchable as SerialException
    check("MonopriceConnectionError subclasses serialx.SerialException",
          issubclass(pm.MonopriceConnectionError, serialx.SerialException))

    # --- 6. Quiet amp: a bare read timeout must NOT reconnect ---
    # The socket stays alive; only the reply is missing (slow/busy amp).
    # Reconnecting here would churn the link and, against a multi-client
    # serial-TCP bridge, create overlapping connections. The command must
    # raise SerialTimeoutException (not MonopriceConnectionError), the port
    # object must be unchanged (no reconnect), and it must stay open.
    port_before = amp._port
    check("precondition: connection is open before quiet-amp test", port_before is not None)
    bridge.server.silent = True
    try:
        amp.zone_status(11)
        outcome = "no_error"
    except pm.MonopriceConnectionError:
        outcome = "reconnect"  # WRONG — a timeout must not trigger reconnect
    except serialx.SerialTimeoutException:
        outcome = "timeout"
    finally:
        bridge.server.silent = False
    check("quiet amp -> SerialTimeoutException, not a reconnect", outcome == "timeout")
    check("quiet amp -> connection NOT reconnected (same port object)",
          amp._port is port_before)
    check("quiet amp -> port left open (still healthy)", amp._port is not None)
    # And the same live connection still works for the next command.
    st = amp.zone_status(11)
    check("quiet amp -> same connection recovers on next command",
          st is not None and st.zone == 11)

    bridge.stop()
    print("\nALL SCENARIOS PASSED")
