#!/usr/bin/env python3

from victron.dbus import Dbus
import anyio

def mon(*a):
    print(a)

N = "test.victron.sender"
V = "/Some/Value"

async def main():
    async with Dbus() as bus:
        v = await bus.importer(N, V, eventCallback=mon)

        print("Initial value:",v.value)
        await anyio.sleep(999)
anyio.run(main, backend="trio")
