#!/usr/bin/env python3

import sys
import anyio
from victron.dbus import Dbus
import random

def mon(*a):
	print(a)

async def main():
	async with Dbus() as bus, bus.service("com.victronenergy.battery.test.c") as srv:
		print("Setting up")
		await srv.add_mandatory_paths(
			processname=__file__,
			processversion="0.1",
			connection='Test',
			deviceinstance=1,
			productid=1234,
			productname="Null BMS",
			firmwareversion="0.1",
			hardwareversion=None,
			connected=1,
		)

		V=26.8
		A=3.0

		vlo = await srv.add_path("/Info/BatteryLowVoltage", 0.9*V,
				   gettextcallback=lambda p, v: "{:0.2f}V".format(v))
		vhi = await srv.add_path("/Info/MaxChargeVoltage", 1.1*V,
				   gettextcallback=lambda p, v: "{:0.2f}V".format(v))
		ich = await srv.add_path("/Info/MaxChargeCurrent", A,
				   gettextcallback=lambda p, v: "{:0.2f}A".format(v))
		idis = await srv.add_path("/Info/MaxDischargeCurrent", A*1.1,
				   gettextcallback=lambda p, v: "{:0.2f}A".format(v))
		ncell = await srv.add_path("/System/NrOfCellsPerBattery",8)
		non = await srv.add_path("/System/NrOfModulesOnline",1)
		noff = await srv.add_path("/System/NrOfModulesOffline",0)
		nbc = await srv.add_path("/System/NrOfModulesBlockingCharge",None)
		nbd = await srv.add_path("/System/NrOfModulesBlockingDischarge",None)
		cap = await srv.add_path("/Capacity", 4.0)
		cap = await srv.add_path("/InstalledCapacity", 5.0)
		cap = await srv.add_path("/ConsumedAmphours", 12.3)

		soc = await srv.add_path('/Soc', 30)
		soh = await srv.add_path('/Soh', 90)
		v0 = await srv.add_path('/Dc/0/Voltage', V,
					gettextcallback=lambda p, v: "{:2.2f}V".format(v))
		c0 = await srv.add_path('/Dc/0/Current', 0.1,
					gettextcallback=lambda p, v: "{:2.2f}A".format(v))
		p0 = await srv.add_path('/Dc/0/Power', 0.2,
					gettextcallback=lambda p, v: "{:0.0f}W".format(v))
		t0 = await srv.add_path('/Dc/0/Temperature', 21.0)
		mv0 = await srv.add_path('/Dc/0/MidVoltage', V/8,
				   gettextcallback=lambda p, v: "{:0.2f}V".format(v))
		mvd0 = await srv.add_path('/Dc/0/MidVoltageDeviation', 10.0,
				   gettextcallback=lambda p, v: "{:0.1f}%".format(v))

		# battery extras
		minct = await srv.add_path('/System/MinCellTemperature', None)
		maxct = await srv.add_path('/System/MaxCellTemperature', None)
		maxcv = await srv.add_path('/System/MaxCellVoltage', None,
				   gettextcallback=lambda p, v: "{:0.3f}V".format(v))
		maxcvi = await srv.add_path('/System/MaxVoltageCellId', None)
		mincv = await srv.add_path('/System/MinCellVoltage', None,
				   gettextcallback=lambda p, v: "{:0.3f}V".format(v))
		mincvi = await srv.add_path('/System/MinVoltageCellId', None)
		hcycles = await srv.add_path('/History/ChargeCycles', None)
		htotalah = await srv.add_path('/History/TotalAhDrawn', None)
		bal = await srv.add_path('/Balancing', None)
		okch = await srv.add_path('/Io/AllowToCharge', 0)
		okdis = await srv.add_path('/Io/AllowToDischarge', 0)
		# xx = await srv.add_path('/SystemSwitch',1)

		# alarms
		allv = await srv.add_path('/Alarms/LowVoltage', None)
		alhv = await srv.add_path('/Alarms/HighVoltage', None)
		allc = await srv.add_path('/Alarms/LowCellVoltage', None)
		alhc = await srv.add_path('/Alarms/HighCellVoltage', None)
		allow = await srv.add_path('/Alarms/LowSoc', None)
		alhch = await srv.add_path('/Alarms/HighChargeCurrent', None)
		alhdis = await srv.add_path('/Alarms/HighDischargeCurrent', None)
		albal = await srv.add_path('/Alarms/CellImbalance', None)
		alfail = await srv.add_path('/Alarms/InternalFailure', None)
		alhct = await srv.add_path('/Alarms/HighChargeTemperature', None)
		allct = await srv.add_path('/Alarms/LowChargeTemperature', None)
		alht = await srv.add_path('/Alarms/HighTemperature', None)
		allt = await srv.add_path('/Alarms/LowTemperature', None)

		await srv.setup_done()

		print("Sending")
		n = 0
		while True:
			n += 1
			await anyio.sleep(n)
			await v0.local_set_value(V*(0.9+random.random()*0.2))
			await c0.local_set_value(A*(0.1+random.random()))
			await p0.local_set_value(v0.value*c0.value)


try:
    anyio.run(main, backend="trio")
except KeyboardInterrupt:
    print("Interrupted.", file=sys.stderr)
