from __future__ import annotations

import asyncio
import logging
import re
import socket
import serialx
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Awaitable, Callable, Concatenate, ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")
    _AsyncLockable = TypeVar("_AsyncLockable", "MonopriceAsync", "MonopriceProtocol")

_LOGGER = logging.getLogger(__name__)
ZONE_PATTERN = re.compile(
    r">(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)"
)

EOL = b"\r\n#"
LEN_EOL = len(EOL)
TIMEOUT = 2  # Number of seconds before serial operation timeout

# TCP keepalive tuning for socket:// bridges (e.g. ESP serial-TCP controllers).
# serialx does not enable keepalive on its socket transport, so a half-open
# connection (bridge rebooted, network blip) is otherwise never detected by the
# OS and writes silently succeed into a dead socket. With keepalive the kernel
# tears the connection down within KEEPALIVE_IDLE + KEEPALIVE_PROBES *
# KEEPALIVE_INTERVAL seconds, so the next command fails fast and triggers the
# reconnect path instead of hanging.
KEEPALIVE_IDLE_SEC = 30
KEEPALIVE_INTERVAL_SEC = 10
KEEPALIVE_PROBES = 3


class MonopriceConnectionError(serialx.SerialException):
    """Raised when a command failed and one reconnect attempt also failed.

    Subclasses serialx.SerialException so existing callers that catch
    SerialException keep working unchanged.
    """


def synchronized(
    func: Callable[Concatenate[Monoprice, _P], _T]
) -> Callable[Concatenate[Monoprice, _P], _T]:
    @wraps(func)
    def wrapper(self: Monoprice, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        with self._lock:
            return func(self, *args, **kwargs)

    return wrapper


def locked_coro(
    coro: Callable[Concatenate[_AsyncLockable, _P], Awaitable[_T]]
) -> Callable[Concatenate[_AsyncLockable, _P], Awaitable[_T]]:
    @wraps(coro)
    async def wrapper(self: _AsyncLockable, *args: _P.args, **kwargs: _P.kwargs) -> _T:  # type: ignore[misc]
        async with self._lock:
            return await coro(self, *args, **kwargs)

    return wrapper


def connected(
    coro: Callable[Concatenate[MonopriceProtocol, _P], Awaitable[_T]]
) -> Callable[Concatenate[MonopriceProtocol, _P], Awaitable[_T]]:
    @wraps(coro)
    async def wrapper(  # type: ignore[misc]
        self: MonopriceProtocol, *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        await self._connected.wait()
        return await coro(self, *args, **kwargs)

    return wrapper


@dataclass
class ZoneStatus:
    zone: int
    pa: bool
    power: bool
    mute: bool
    do_not_disturb: bool
    volume: int  # 0 - 38
    treble: int  # 0 -> -7,  14-> +7
    bass: int  # 0 -> -7,  14-> +7
    balance: int  # 00 - left, 10 - center, 20 right
    source: int
    keypad: bool

    @classmethod
    def from_strings(cls, strings: list[str]) -> list[ZoneStatus]:
        if not strings:
            return list()
        return [zone for zone in (ZoneStatus.from_string(s) for s in strings) if zone is not None]

    @classmethod
    def from_string(cls, string: str) -> ZoneStatus | None:
        if not string:
            return None
        match = re.search(ZONE_PATTERN, string)
        if not match:
            return None
        (
            zone,
            pa,
            power,
            mute,
            do_not_disturb,
            volume,
            treble,
            bass,
            balance,
            source,
            keypad,
        ) = map(int, match.groups())
        return ZoneStatus(
            zone,
            bool(pa),
            bool(power),
            bool(mute),
            bool(do_not_disturb),
            volume,
            treble,
            bass,
            balance,
            source,
            bool(keypad),
        )


class Monoprice:
    def __init__(self, port_url: str, lock: RLock) -> None:
        """
        Monoprice amplifier interface
        """
        self._lock = lock
        self._port_url = port_url
        self._port: serialx.BaseSerial | None = None
        self._open_port()

    def _open_port(self) -> None:
        """Create, configure and open the serial (or socket-bridged) port."""
        port = serialx.serial_for_url(
            self._port_url,
            baudrate=9600,
            stopbits=serialx.StopBits.ONE,
            byte_size=8,
            parity=serialx.Parity.NONE,
            read_timeout=TIMEOUT,
            write_timeout=TIMEOUT,
        )
        port.open()
        _enable_tcp_keepalive(port)
        self._port = port
        _LOGGER.debug("Opened connection to %s", self._port_url)

    def _close_port(self) -> None:
        """Close the port, swallowing errors — used before reconnect."""
        if self._port is not None:
            try:
                self._port.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup of a dead port
                pass
            self._port = None

    def _reconnect(self) -> None:
        self._close_port()
        self._open_port()

    def _send_request(self, request: bytes) -> None:
        """
        :param request: request that is sent to the monoprice
        """
        _LOGGER.debug('Sending "%s"', request)
        # clear
        self._port.reset_output_buffer()
        self._port.reset_input_buffer()
        # send
        self._port.write(request)
        self._port.flush()

    def _process_request(self, request: bytes, num_eols_to_read: int = 1) -> str:
        """
        Send a request, transparently reconnecting and retrying once if the
        *connection* has died (e.g. the serial-TCP bridge rebooted).

        A bare read timeout is deliberately NOT treated as a dead connection.
        serialx surfaces the two cases differently: a slow/quiet amp makes
        ``read()`` return no bytes, which we raise as ``SerialTimeoutException``
        while the socket stays healthy; a genuinely dropped peer makes the next
        read/write raise ``OSError``/``SerialException`` ("socket closed by
        peer", broken pipe, connection reset). Only the latter should reconnect.
        Reconnecting on every timeout churns the link, and against a
        multi-client serial-TCP bridge each extra overlapping connection causes
        cross-talk and can exhaust the bridge's sockets.

        :param request: request that is sent to the monoprice
        :param num_eols_to_read: number of EOL sequences to read. When last EOL is read, reading stops
        :return: ascii string returned by monoprice
        :raises serialx.SerialTimeoutException: the amp did not answer in time (connection is fine)
        :raises MonopriceConnectionError: the connection died and reconnect+retry also failed
        """
        try:
            if self._port is None:
                self._open_port()
            return self._process_request_once(request, num_eols_to_read)
        except serialx.SerialTimeoutException:
            # The amp did not answer within TIMEOUT, but the socket is alive.
            # Do not reconnect — surface the timeout unchanged and leave the
            # (healthy) port open for the next command.
            raise
        except (serialx.SerialException, OSError) as first_error:
            _LOGGER.warning(
                "Command %r to %s failed (%s); reconnecting and retrying once",
                request,
                self._port_url,
                first_error,
            )
            try:
                self._reconnect()
                result = self._process_request_once(request, num_eols_to_read)
            except serialx.SerialTimeoutException:
                # Reconnect succeeded (fresh, live socket) but the amp still did
                # not answer the retried command. That is a timeout, not a
                # connection failure: preserve the timeout contract and keep the
                # healthy port open for the next call.
                raise
            except (serialx.SerialException, OSError) as retry_error:
                # Reconnect or the retried command itself failed — the link is
                # genuinely dead. Leave the port closed so the NEXT call starts
                # with a fresh connection attempt.
                self._close_port()
                raise MonopriceConnectionError(
                    "Command {!r} to {} failed after reconnect: {}".format(
                        request, self._port_url, retry_error
                    )
                ) from retry_error
            _LOGGER.info("Reconnected to %s and retried successfully", self._port_url)
            return result

    def _process_request_once(self, request: bytes, num_eols_to_read: int) -> str:
        """Single attempt: send request and read response. No retry logic."""
        self._send_request(request)
        # receive
        result = bytearray()
        count = None
        while True:
            c = self._port.read(1)
            if not c:
                raise serialx.SerialTimeoutException(
                    "Connection timed out! Last received bytes {}".format(
                        [hex(a) for a in result]
                    )
                )
            result += c
            count = _subsequence_count(result, EOL, count)
            if count[1] >= num_eols_to_read:
                break
        ret = bytes(result)
        _LOGGER.debug('Received "%s"', ret)
        return ret.decode("ascii")

    @synchronized
    def zone_status(self, zone: int) -> ZoneStatus | None:
        """
        Get the structure representing the status of the zone
        :param zone: zone 11..16, 21..26, 31..36
        :return: status of the zone or None
        """
        # Reading two lines as the response is in the form \r\n#>110001000010111210040\r\n#
        return ZoneStatus.from_string(
            self._process_request(_format_zone_status_request(zone), num_eols_to_read=2)
        )

    @synchronized
    def all_zone_status(self, unit: int) -> list[ZoneStatus]:
        """
        Get the structure representing the status of all zones in a unit
        :param unit: 1, 2, 3
        :return: list of all statuses of the unit's zones or empty list if unit number is incorrect
        """
        if unit < 1 or unit > 3:
            return []
        # Reading 7 lines, since response starts with EOL and each zone's status is followed by EOL
        response = self._process_request(
            _format_all_zones_status_request(unit), num_eols_to_read=7
        )
        return ZoneStatus.from_strings(response.split(sep=EOL.decode('ascii')))

    @synchronized
    def set_power(self, zone: int, power: bool) -> None:
        """
        Turn zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param power: True to turn on, False to turn off
        """
        self._process_request(_format_set_power(zone, power))

    @synchronized
    def set_mute(self, zone: int, mute: bool) -> None:
        """
        Mute zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param mute: True to mute, False to unmute
        """
        self._process_request(_format_set_mute(zone, mute))

    @synchronized
    def set_volume(self, zone: int, volume: int) -> None:
        """
        Set volume for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param volume: integer from 0 to 38 inclusive
        """
        self._process_request(_format_set_volume(zone, volume))

    @synchronized
    def set_treble(self, zone: int, treble: int) -> None:
        """
        Set treble for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param treble: integer from 0 to 14 inclusive, where 0 is -7 treble and 14 is +7
        """
        self._process_request(_format_set_treble(zone, treble))

    @synchronized
    def set_bass(self, zone: int, bass: int) -> None:
        """
        Set bass for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param bass: integer from 0 to 14 inclusive, where 0 is -7 bass and 14 is +7
        """
        self._process_request(_format_set_bass(zone, bass))

    @synchronized
    def set_balance(self, zone: int, balance: int) -> None:
        """
        Set balance for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param balance: integer from 0 to 20 inclusive, where 0 is -10(left), 0 is center and 20 is +10 (right)
        """
        self._process_request(_format_set_balance(zone, balance))

    @synchronized
    def set_source(self, zone: int, source: int) -> None:
        """
        Set source for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param source: integer from 0 to 6 inclusive
        """
        self._process_request(_format_set_source(zone, source))

    @synchronized
    def restore_zone(self, status: ZoneStatus) -> None:
        """
        Restores zone to it's previous state
        :param status: zone state to restore
        """
        self.set_power(status.zone, status.power)
        self.set_mute(status.zone, status.mute)
        self.set_volume(status.zone, status.volume)
        self.set_treble(status.zone, status.treble)
        self.set_bass(status.zone, status.bass)
        self.set_balance(status.zone, status.balance)
        self.set_source(status.zone, status.source)


class MonopriceAsync:
    def __init__(
        self, monoprice_protocol: MonopriceProtocol, lock: asyncio.Lock
    ) -> None:
        """
        Async Monoprice amplifier interface
        """
        self._protocol = monoprice_protocol
        self._lock = lock

    @locked_coro
    async def zone_status(self, zone: int) -> ZoneStatus | None:
        """
        Get the structure representing the status of the zone
        :param zone: zone 11..16, 21..26, 31..36
        :return: status of the zone or None
        """
        # Reading two lines as the response is in the form \r\n#>110001000010111210040\r\n#
        string = await self._protocol.send(_format_zone_status_request(zone), num_eols_to_read=2)
        return ZoneStatus.from_string(string)

    @locked_coro
    async def all_zone_status(self, unit: int) -> list[ZoneStatus]:
        """
        Get the structure representing the status of all zones in a unit
        :param unit: 1, 2, 3
        :return: list of all statuses of the unit's zones or empty list if unit number is incorrect
        """
        if unit < 1 or unit > 3:
            return []
        # Reading 7 lines, since response starts with EOL and each zone's status is followed by EOL
        response = await self._protocol.send(
            _format_all_zones_status_request(unit), num_eols_to_read=7
        )
        return ZoneStatus.from_strings(response.split(sep=EOL.decode('ascii')))

    @locked_coro
    async def set_power(self, zone: int, power: bool) -> None:
        """
        Turn zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param power: True to turn on, False to turn off
        """
        await self._protocol.send(_format_set_power(zone, power))

    @locked_coro
    async def set_mute(self, zone: int, mute: bool) -> None:
        """
        Mute zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param mute: True to mute, False to unmute
        """
        await self._protocol.send(_format_set_mute(zone, mute))

    @locked_coro
    async def set_volume(self, zone: int, volume: int) -> None:
        """
        Set volume for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param volume: integer from 0 to 38 inclusive
        """
        await self._protocol.send(_format_set_volume(zone, volume))

    @locked_coro
    async def set_treble(self, zone: int, treble: int) -> None:
        """
        Set treble for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param treble: integer from 0 to 14 inclusive, where 0 is -7 treble and 14 is +7
        """
        await self._protocol.send(_format_set_treble(zone, treble))

    @locked_coro
    async def set_bass(self, zone: int, bass: int) -> None:
        """
        Set bass for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param bass: integer from 0 to 14 inclusive, where 0 is -7 bass and 14 is +7
        """
        await self._protocol.send(_format_set_bass(zone, bass))

    @locked_coro
    async def set_balance(self, zone: int, balance: int) -> None:
        """
        Set balance for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param balance: integer from 0 to 20 inclusive, where 0 is -10(left), 0 is center and 20 is +10 (right)
        """
        await self._protocol.send(_format_set_balance(zone, balance))

    @locked_coro
    async def set_source(self, zone: int, source: int) -> None:
        """
        Set source for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param source: integer from 0 to 6 inclusive
        """
        await self._protocol.send(_format_set_source(zone, source))

    @locked_coro
    async def restore_zone(self, status: ZoneStatus) -> None:
        """
        Restores zone to it's previous state
        :param status: zone state to restore
        """
        await self._protocol.send(_format_set_power(status.zone, status.power))
        await self._protocol.send(_format_set_mute(status.zone, status.mute))
        await self._protocol.send(_format_set_volume(status.zone, status.volume))
        await self._protocol.send(_format_set_treble(status.zone, status.treble))
        await self._protocol.send(_format_set_bass(status.zone, status.bass))
        await self._protocol.send(_format_set_balance(status.zone, status.balance))
        await self._protocol.send(_format_set_source(status.zone, status.source))


class MonopriceProtocol(asyncio.Protocol):
    def __init__(self) -> None:
        super().__init__()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._transport: serialx.SerialTransport | None = None
        self._connected = asyncio.Event()
        self.q: asyncio.Queue[bytes] = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, serialx.SerialTransport)
        self._transport = transport
        self._connected.set()
        _LOGGER.debug("port opened %s", self._transport)

    def data_received(self, data: bytes) -> None:
        task = asyncio.create_task(self.q.put(data))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @connected
    @locked_coro
    async def send(self, request: bytes, num_eols_to_read: int = 1) -> str:
        """
        :param request: request that is sent to the monoprice
        :param num_eols_to_read: number of EOL sequences to read. When last EOL is read, reading stops
        :return: ascii string returned by monoprice
        """
        result = bytearray()
        assert self._transport is not None
        assert self._transport.serial is not None
        self._transport.serial.reset_output_buffer()
        self._transport.serial.reset_input_buffer()
        while not self.q.empty():
            self.q.get_nowait()
        self._transport.write(request)
        count = None
        try:
            while True:
                result += await asyncio.wait_for(self.q.get(), TIMEOUT)
                count = _subsequence_count(result, EOL, count)
                if count[1] >= num_eols_to_read:
                    break
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Timeout during receiving response for command '%s', received='%s'",
                request,
                result,
            )
            raise
        ret = bytes(result)
        _LOGGER.debug('Received "%s"', ret)
        return ret.decode("ascii")

# Helpers


def _enable_tcp_keepalive(port: serialx.BaseSerial) -> None:
    """Enable TCP keepalive when the port is a socket:// bridge.

    serialx's socket transport does not enable keepalive, so a half-open
    connection (bridge rebooted, network blip) is never detected by the OS.
    With keepalive the kernel tears the dead connection down within
    KEEPALIVE_IDLE + KEEPALIVE_PROBES * KEEPALIVE_INTERVAL seconds, so the next
    command fails fast and triggers the reconnect path instead of hanging.
    Silently does nothing for real serial devices (no underlying socket).
    """
    sock = getattr(port, "_socket", None)
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Per-option guards: not all platforms expose all three constants.
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE_SEC)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL_SEC
            )
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_PROBES)
        _LOGGER.debug("TCP keepalive enabled on %s", port)
    except OSError as err:
        _LOGGER.debug("Could not enable TCP keepalive: %s", err)


def _subsequence_count(sequence: bytearray, sub: bytes, previous: tuple[int, int] | None = None) -> tuple[int, int]:
    """
    Counts number of subsequences in a sequence
    """
    start, count = (previous or (0, 0))
    while True:
        idx = sequence.find(sub, start)
        if idx < 0:
            return start, count
        start, count = idx + len(sub), count + 1


def _format_zone_status_request(zone: int) -> bytes:
    return "?{}\r".format(zone).encode()


def _format_all_zones_status_request(unit: int) -> bytes:
    return "?{}\r".format(unit * 10).encode()


def _format_set_power(zone: int, power: bool) -> bytes:
    return "<{}PR{}\r".format(zone, "01" if power else "00").encode()


def _format_set_mute(zone: int, mute: bool) -> bytes:
    return "<{}MU{}\r".format(zone, "01" if mute else "00").encode()


def _format_set_volume(zone: int, volume: int) -> bytes:
    volume = int(max(0, min(volume, 38)))
    return "<{}VO{:02}\r".format(zone, volume).encode()


def _format_set_treble(zone: int, treble: int) -> bytes:
    treble = int(max(0, min(treble, 14)))
    return "<{}TR{:02}\r".format(zone, treble).encode()


def _format_set_bass(zone: int, bass: int) -> bytes:
    bass = int(max(0, min(bass, 14)))
    return "<{}BS{:02}\r".format(zone, bass).encode()


def _format_set_balance(zone: int, balance: int) -> bytes:
    balance = max(0, min(balance, 20))
    return "<{}BL{:02}\r".format(zone, balance).encode()


def _format_set_source(zone: int, source: int) -> bytes:
    source = int(max(1, min(source, 6)))
    return "<{}CH{:02}\r".format(zone, source).encode()


def get_monoprice(port_url: str) -> Monoprice:
    """
    Return synchronous version of Monoprice interface
    :param port_url: serial port, i.e. '/dev/ttyUSB0'
    :return: synchronous implementation of Monoprice interface
    """

    lock = RLock()

    return Monoprice(port_url, lock)


async def get_async_monoprice(port_url: str) -> MonopriceAsync:
    """
    Return asynchronous version of Monoprice interface
    :param port_url: serial port, i.e. '/dev/ttyUSB0'
    :return: asynchronous implementation of Monoprice interface
    """

    lock = asyncio.Lock()

    loop = asyncio.get_running_loop()
    _, protocol = await serialx.create_serial_connection(
        loop, MonopriceProtocol, port_url, baudrate=9600
    )
    return MonopriceAsync(protocol, lock)  # type: ignore[arg-type]
