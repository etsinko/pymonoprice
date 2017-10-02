import re
import serial
from threading import RLock

ZONE_PATTERN = re.compile('#>(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)')

EOL = b'\r\n#'
LEN_EOL = len(EOL)


class ZoneStatus(object):
    def __init__(self,
                 zone: int,
                 pa:bool,
                 power:bool,
                 mute:bool,
                 do_not_disturb: bool,
                 volume:int,  # 0 - 38
                 treble:int,  # 0 -> -7,  14-> +7
                 bass:int,  # 0 -> -7,  14-> +7
                 balance:int,  # 00 - left, 10 - center, 20 right
                 source:int,
                 keypad:bool):
        self.zone = zone
        self.pa = bool(pa)
        self.power = bool(power)
        self.mute = bool(mute)
        self.do_not_disturb = bool(do_not_disturb)
        self.volume = volume
        self.treble = treble
        self.bass = bass
        self.balance = balance
        self.source = source
        self.keypad = bool(keypad)

    @classmethod
    def from_string(self, string: str):
        if not string:
            return None
        return ZoneStatus(*[int(m) for m in re.search(ZONE_PATTERN, string).groups()])


class Monoprice(object):
    def __init__(self, port_url):
        self._lock = RLock()
        self._port = serial.serial_for_url(port_url, do_not_open=True)
        self._port.baudrate = 9600
        self._port.stopbits = serial.STOPBITS_ONE
        self._port.bytesize = serial.EIGHTBITS
        self._port.parity = serial.PARITY_NONE
        self._port.timeout = 2
        self._port.write_timeout = 2
        self._port.open()

    def _process_request(self, request):
        with self._lock:
            # clear
            self._port.flush()
            self._port.reset_output_buffer()
            self._port.reset_input_buffer()
            # send
            self._port.write(request.encode())
            # receive
            result = bytearray()
            while True:
                c = self._port.read(1)
                if not c:
                    raise serial.SerialTimeoutException('Connection timed out! Last received bytes {}'.format([hex(a) for a in result]))
                result += c
                # Ignore first 3 bytes as they will identical to EOL!
                if len(result) > 3 and result[-LEN_EOL:] == EOL:
                    break
            return bytes(result).decode('ascii')

    def zone_status(self, zone: int):
        """
        Get the structure representing the status of the zone
        :param zone: zone 11..16, 21..26, 31..36
        :return: status of the zone or None
        """
        return ZoneStatus.from_string(self._process_request('?{}\r'.format(zone)))

    def set_power(self, zone: int, power: bool):
        """
        Turns zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param power: True to turn on, False to turn off
        """
        self._process_request('<{}PR{}\r'.format(zone, '01' if power else '00'))

    def set_mute(self, zone: int, mute: bool):
        """
        Mutes zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param mute: True to mute, False to unmute
        """
        self._process_request('<{}MU{}\r'.format(zone, '01' if mute else '00'))

    def set_volume(self, zone: int, volume: int):
        """
        Sets volume for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param volume: integer from 0 to 38 inclusive
        """
        volume = max(0, min(volume, 38))
        self._process_request('<{}VO{:02}\r'.format(zone, volume))

    def set_treble(self, zone: int, treble: int):
        """
        Sets treble for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param treble: integer from 0 to 14 inclusive, where 0 is -7 treble and 14 is +7
        """
        treble = max(0, min(treble, 14))
        self._process_request('<{}TR{:02}\r'.format(zone, treble))

    def set_bass(self, zone: int, bass: int):
        """
        Sets bass for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param bass: integer from 0 to 14 inclusive, where 0 is -7 bass and 14 is +7
        """
        bass = max(0, min(bass, 14))
        self._process_request('<{}BS{:02}\r'.format(zone, bass))

    def set_balance(self, zone: int, balance: int):
        """
        Sets balance for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param balance: integer from 0 to 20 inclusive, where 0 is -10(left), 0 is center and 20 is +10 (right)
        """
        balance = max(0, min(balance, 20))
        self._process_request('<{}BL{:02}\r'.format(zone, balance))

    def set_source(self, zone: int, source: int):
        """
        Sets source for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param source: integer from 0 to 6 inclusive
        """
        source = max(1, min(source, 6))
        self._process_request('<{}CH{:02}\r'.format(zone, source))
