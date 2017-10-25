## Status
[![Build Status](https://travis-ci.org/etsinko/pymonoprice.svg?branch=master)](https://travis-ci.org/etsinko/pymonoprice)[![Coverage Status](https://coveralls.io/repos/github/etsinko/pymonoprice/badge.svg)](https://coveralls.io/github/etsinko/pymonoprice)

# pymonoprice
Python3 interface implementation for Monoprice 6 zone amplifier

## Notes
This is for use with [Home-Assistant](http://home-assistant.io)

## Usage
```python
from pymonoprice import get_monoprice

monoprice = get_monoprice('/dev/ttyUSB0')
# Valid zones are 11-16 for main monoprice amplifier
zone_status = monoprice.zone_status(11)

# Print zone status
print('Zone Number = {}'.format(zone_status.zone))
print('Power is {}'.format('On' if zone_status.power else 'Off'))
print('Mute is {}'.format('On' if zone_status.mute else 'Off'))
print('Public Anouncement Mode is {}'.format('On' if zone_status.pa else 'Off'))
print('Do Not Disturb Mode is {}'.format('On' if zone_status.do_not_disturb else 'Off'))
print('Volume = {}'.format(zone_status.volume))
print('Treble = {}'.format(zone_status.treble))
print('Bass = {}'.format(zone_status.bass))
print('Balance = {}'.format(zone_status.balance))
print('Source = {}'.format(zone_status.source))
print('Keypad is {}'.format('connected' if zone_status.keypad else 'disconnected'))

# Turn off zone #11
monoprice.set_power(11, False)

# Mute zone #12
monoprice.set_mute(12, True)

# Set volume for zone #13
monoprice.set_volume(13, 15)

# Set source 1 for zone #14 
monoprice.set_source(14, 1)

# Set treble for zone #15
monoprice.set_treble(15, 10)

# Set bass for zone #16
monoprice.set_bass(16, 7)

# Set balance for zone #11
monoprice.set_balance(11, 3)

# Restore zone #11 to it's original state
monoprice.restore_zone(zone_status)
```

## Usage with asyncio

With `asyncio` flavor all methods of Monoprice object are coroutines.

```python
import asyncio
from pymonoprice import get_async_monoprice

async def main(loop):
    monoprice = await get_async_monoprice('/dev/ttyUSB0', loop)
    zone_status = await monoprice.zone_status(1)
    if zone_status.power:
        await monoprice.set_power(1, False)

loop = asyncio.get_event_loop()
main(loop)

```