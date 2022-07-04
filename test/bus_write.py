#!/usr/bin/env python3

import sys
import anyio
from victron.dbus import Dbus

def mon(*a):
	print(a)

async def main():
	async with Dbus() as bus, bus.service("test.victron.sender") as srv:
		v = await srv.add_path("/Some/Value", None, description="Test", onchangecallback=mon)
		await srv.add_mandatory_paths(
			processname=__file__,
			processversion="0.1",
			connection='test',
			deviceinstance=0,
			productid=None,
			productname=None,
			firmwareversion=None,
			hardwareversion=None,
			connected=1,
		)

		print("Sending")
		n = 0
		while True:
			n += 1
			await anyio.sleep(n)
			await v.local_set_value(n)

try:
    anyio.run(main, backend="trio")
except KeyboardInterrupt:
    print("Interrupted.", file=sys.stderr)
