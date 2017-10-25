import unittest

import serial

from pymonoprice import (get_monoprice, get_async_monoprice, ZoneStatus)
from tests import create_dummy_port
import asyncio


class TestZoneStatus(unittest.TestCase):

    def test_zone_status_broken(self):
        self.assertIsNone(ZoneStatus.from_string(None))
        self.assertIsNone(ZoneStatus.from_string('\r\n#>010001000010111210040\r\n#'))
        self.assertIsNone(ZoneStatus.from_string('\r\n#>a100010000101112100401\r\n#'))
        self.assertIsNone(ZoneStatus.from_string('\r\n#>a1000100dfsf112100401\r\n#'))
        self.assertIsNone(ZoneStatus.from_string('\r\n#>\r\n#'))

class TestMonoprice(unittest.TestCase):
    def setUp(self):
        self.responses = {}
        self.monoprice = get_monoprice(create_dummy_port(self.responses))

    def test_zone_status(self):
        self.responses[b'?1\r'] = b'\r\n#>0100010000131112100401\r\n#'
        status = self.monoprice.zone_status(1)
        self.assertEqual(1, status.zone)
        self.assertFalse(status.pa)
        self.assertTrue(status.power)
        self.assertFalse(status.mute)
        self.assertFalse(status.do_not_disturb)
        self.assertEqual(13, status.volume)
        self.assertEqual(11, status.treble)
        self.assertEqual(12, status.bass)
        self.assertEqual(10, status.balance)
        self.assertEqual(4, status.source)
        self.assertTrue(status.keypad)
        self.assertEqual(0, len(self.responses))

    def test_set_power(self):
        self.responses[b'<3PR01\r'] = b'\r\n#'
        self.monoprice.set_power(3, True)
        self.responses[b'<3PR01\r'] = b'\r\n#'
        self.monoprice.set_power(3, 'True')
        self.responses[b'<3PR01\r'] = b'\r\n#'
        self.monoprice.set_power(3, 1)
        self.responses[b'<3PR00\r'] = b'\r\n#'
        self.monoprice.set_power(3, False)
        self.responses[b'<3PR00\r'] = b'\r\n#'
        self.monoprice.set_power(3, None)
        self.responses[b'<3PR00\r'] = b'\r\n#'
        self.monoprice.set_power(3, 0)
        self.responses[b'<3PR00\r'] = b'\r\n#'
        self.monoprice.set_power(3, '')
        self.assertEqual(0, len(self.responses))
        
    def test_set_mute(self):
        self.responses[b'<3MU01\r'] = b'\r\n#'
        self.monoprice.set_mute(3, True)
        self.responses[b'<3MU01\r'] = b'\r\n#'
        self.monoprice.set_mute(3, 'True')
        self.responses[b'<3MU01\r'] = b'\r\n#'
        self.monoprice.set_mute(3, 1)
        self.responses[b'<3MU00\r'] = b'\r\n#'
        self.monoprice.set_mute(3, False)
        self.responses[b'<3MU00\r'] = b'\r\n#'
        self.monoprice.set_mute(3, None)
        self.responses[b'<3MU00\r'] = b'\r\n#'
        self.monoprice.set_mute(3, 0)
        self.responses[b'<3MU00\r'] = b'\r\n#'
        self.monoprice.set_mute(3, '')
        self.assertEqual(0, len(self.responses))

    def test_set_volume(self):
        self.responses[b'<3VO01\r'] = b'\r\n#'
        self.monoprice.set_volume(3, 1)
        self.responses[b'<3VO38\r'] = b'\r\n#'
        self.monoprice.set_volume(3, 100)
        self.responses[b'<3VO00\r'] = b'\r\n#'
        self.monoprice.set_volume(3, -100)
        self.responses[b'<3VO20\r'] = b'\r\n#'
        self.monoprice.set_volume(3, 20)
        self.assertEqual(0, len(self.responses))

    def test_set_treble(self):
        self.responses[b'<3TR01\r'] = b'\r\n#'
        self.monoprice.set_treble(3, 1)
        self.responses[b'<3TR14\r'] = b'\r\n#'
        self.monoprice.set_treble(3, 100)
        self.responses[b'<3TR00\r'] = b'\r\n#'
        self.monoprice.set_treble(3, -100)
        self.responses[b'<3TR13\r'] = b'\r\n#'
        self.monoprice.set_treble(3, 13)
        self.assertEqual(0, len(self.responses))

    def test_set_bass(self):
        self.responses[b'<3BS01\r'] = b'\r\n#'
        self.monoprice.set_bass(3, 1)
        self.responses[b'<3BS14\r'] = b'\r\n#'
        self.monoprice.set_bass(3, 100)
        self.responses[b'<3BS00\r'] = b'\r\n#'
        self.monoprice.set_bass(3, -100)
        self.responses[b'<3BS13\r'] = b'\r\n#'
        self.monoprice.set_bass(3, 13)
        self.assertEqual(0, len(self.responses))

    def test_set_balance(self):
        self.responses[b'<3BL01\r'] = b'\r\n#'
        self.monoprice.set_balance(3, 1)
        self.responses[b'<3BL20\r'] = b'\r\n#'
        self.monoprice.set_balance(3, 100)
        self.responses[b'<3BL00\r'] = b'\r\n#'
        self.monoprice.set_balance(3, -100)
        self.responses[b'<3BL13\r'] = b'\r\n#'
        self.monoprice.set_balance(3, 13)
        self.assertEqual(0, len(self.responses))

    def test_set_source(self):
        self.responses[b'<3CH01\r'] = b'\r\n#'
        self.monoprice.set_source(3, 1)
        self.responses[b'<3CH06\r'] = b'\r\n#'
        self.monoprice.set_source(3, 100)
        self.responses[b'<3CH01\r'] = b'\r\n#'
        self.monoprice.set_source(3, -100)
        self.responses[b'<3CH03\r'] = b'\r\n#'
        self.monoprice.set_source(3, 3)
        self.assertEqual(0, len(self.responses))

    def test_restore_zone(self):
        zone = ZoneStatus.from_string('\r\n#>0100010000131112100401\r\n#')
        self.responses[b'<1PR01\r'] = b'\r\n#'
        self.responses[b'<1MU00\r'] = b'\r\n#'
        self.responses[b'<1VO13\r'] = b'\r\n#'
        self.responses[b'<1TR11\r'] = b'\r\n#'
        self.responses[b'<1BS12\r'] = b'\r\n#'
        self.responses[b'<1BL10\r'] = b'\r\n#'
        self.responses[b'<1CH04\r'] = b'\r\n#'
        self.monoprice.restore_zone(zone)
        self.assertEqual(0, len(self.responses))

    def test_timeout(self):
        with self.assertRaises(serial.SerialTimeoutException):
            self.monoprice.set_source(3, 3)


class TestAsyncMonoprice(TestMonoprice):

    def setUp(self):
        self.responses = {}
        loop = asyncio.get_event_loop()
        monoprice = loop.run_until_complete(get_async_monoprice(create_dummy_port(self.responses), loop))

        # Dummy monoprice that converts async to sync
        class DummyMonoprice():
            def __getattribute__(self, item):
                def f(*args, **kwargs):
                    return loop.run_until_complete(monoprice.__getattribute__(item)(*args, **kwargs))
                return f
        self.monoprice = DummyMonoprice()

    def test_timeout(self):
        with self.assertRaises(asyncio.TimeoutError):
            self.monoprice.set_source(3, 3)

if __name__ == '__main__':
    unittest.main()