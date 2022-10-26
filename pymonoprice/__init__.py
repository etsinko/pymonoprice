from __future__ import annotations

import asyncio
import logging
import re
import serial
from dataclasses import dataclass
from functools import wraps
from serial_asyncio import create_serial_connection, SerialTransport
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
    async def wrapper(self: _AsyncLockable, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        async with self._lock:
            return await coro(self, *args, **kwargs)  # type: ignore[return-value]

    return wrapper


def connected(
    coro: Callable[Concatenate[MonopriceProtocol, _P], Awaitable[_T]]
) -> Callable[Concatenate[MonopriceProtocol, _P], Awaitable[_T]]:
    @wraps(coro)
    async def wrapper(
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
        self._port = serial.serial_for_url(port_url, do_not_open=True)
        self._port.baudrate = 9600
        self._port.stopbits = serial.STOPBITS_ONE
        self._port.bytesize = serial.EIGHTBITS
        self._port.parity = serial.PARITY_NONE
        self._port.timeout = TIMEOUT
        self._port.write_timeout = TIMEOUT
        self._port.open()

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
        :param request: request that is sent to the monoprice
        :param num_eols_to_read: number of EOL sequences to read. When last EOL is read, reading stops
        :return: ascii string returned by monoprice
        """
        self._send_request(request)
        # receive
        result = bytearray()
        count = None
        while True:
            c = self._port.read(1)
            if not c:
                raise serial.SerialTimeoutException(
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
        self._transport: SerialTransport = None
        self._connected = asyncio.Event()
        self.q: asyncio.Queue[bytes] = asyncio.Queue()

    def connection_made(self, transport: SerialTransport) -> None:
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
    _, protocol = await create_serial_connection(
        loop, MonopriceProtocol, port_url, baudrate=9600
    )
    return MonopriceAsync(protocol, lock)
