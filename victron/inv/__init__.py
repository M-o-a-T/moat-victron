#!/usr/bin/env python3

import sys
import os
import anyio

from victron.dbus import Dbus
from victron.dbus.monitor import DbusMonitor
from victron.dbus.util import DbusInterface
from asyncdbus.service import method

import logging
logger = logging.getLogger(__name__)

from ._util import balance, async_init



class BusVars(async_init):
	"""
	A helper that creates attributes as per the class's VARS and VARS_RO arrays.

	Both are mappings:  dbusname => { attrname => path }

	The attributes registered via VARS are DbusItemImport objects.
	Attributes registered via VARS_RO are read from the bus, and set directly.
	"""
	VARS = {}
	VARS_RO = {}

	async def __init__(self, bus):
		self._bus = bus

		for k,v in self.VARS_RO.items():
			for n,p in v.items():
				setattr(self,n, (await self.bus.importer(k,p, createsignal=False)).value)
		for k,v in self.VARS.items():
			for n,p in v.items():
				setattr(self,n, await self.bus.importer(k,p))

	@property
	def bus(self):
		return self._bus


class InvInterface(DbusInterface):
	def __init__(self, ctrl):
		self.ctrl = ctrl
		super().__init__(ctrl.bus, "/Control", "inv")

		del self.ctrl
		super().done()

	@method()
	async def GetMethods(self) -> 'a{s(is)}':
		"""
		Return a dict of available methods.
		name => (ident#, descr)
		"""
		res = {}
		for i,nm in InvControl.MODE.items():
			n,m = nm
			res[n] = (i, m.__doc__)
		return res


class InvControl(BusVars):
	high_delta = 0.3  # min difference between current voltage and whatever the battery says is OK


	MODE = {}
	@classmethod
	def register(cls,num,name):
		def _reg(proc):
			cls.MODE[num] = (name, proc)
			return proc
		return _reg

	VARS = {
		'com.victronenergy.system': dict(
			acc_vebus = '/VebusService',
			i_pv = '/Dc/Pv/Current',
			p_pv = '/Dc/Pv/Power',
			u_bat = '/Dc/Battery/Voltage',
			i_bat = '/Dc/Battery/Current',
			p_bat = '/Dc/Battery/Power',
			i_inv = '/Dc/Vebus/Current',
			p_inv = '/Dc/Vebus/Power',
			p_ac1 = '/Ac/ActiveIn/L1/Power',
			p_ac2 = '/Ac/ActiveIn/L2/Power',
			p_ac3 = '/Ac/ActiveIn/L3/Power',
			p_grid1 = '/Ac/Grid/L1/Power',
			p_grid2 = '/Ac/Grid/L2/Power',
			p_grid3 = '/Ac/Grid/L3/Power',
			s_battery = '/Dc/Battery/BatteryService',
		),
	}
	VARS_RO = {
		'com.victronenergy.system': dict(
			n_phase = '/Ac/ActiveIn/NumberOfPhases',
		),
	}


	i_bat_avg = None

	async def _i_bat_task(self):
		b_last = [None,None,None,None]
		while True:
			b_last.pop(0)
			b_last.append(self.i_bat.value)
			try:
				self.i_bat_avg = sum(b_last) / len(b_last)
			except (TypeError,ValueError):
				self.i_bat_avg = 0
			await anyio.sleep(1)


	async def set_inv_power(self, p):
		"""
		TODO balance, per outgoing grid meter
		TODO return error if the delta between what we want and what's possible is too high
		"""
		for pg in self.p_grid:
			await pg.set_value(p / len(self.p_grid))


	async def incr_inv_power(self, p):
		return await set_inv_power(self.p_inv.value + p)

	async def pv_setpoint(self):
		"""
		Helper. If the distance between the current voltage and the high limit is <300mV,
		successively increase feed-out until it is.
		"""
		while True:
			if self.u_bat.value + self.high_delta <= self.u_max.value:
				return True
			# Increase load out if there's not much going out already
			if self.i_bat_avg > -self.i_min.value/20:
				if not await self.incr_inv_power(-self.u_bat.value * self.i_min.value/50):
					return False
			await anyio.sleep(15)

	async def update_vars(self):
		self.u_min = await self.bus.importer(self.s_battery.value, '/Info/BatteryLowVoltage')
		self.u_max = await self.bus.importer(self.s_battery.value, '/Info/MaxChargeVoltage')
		self.i_min = await self.bus.importer(self.s_battery.value, '/Info/MaxDischargeCurrent')
		self.i_max = await self.bus.importer(self.s_battery.value, '/Info/MaxChargeCurrent')
		self.p_set = []
			self.p_set.append(await self.bus.importer(self.acc_vebus.value, f'/Hub4/L{i}/AcPowerSetpoint'))

	async def __init__(self, bus, cfg):
		await super().__init__(bus)
		self.cfg = cfg

		self.p_ac = []
		self.p_grid = []
		for i in range(self.n_phase):
			i += 1
			self.p_ac.append(await self.bus.importer('com.victronenergy.system', f'/Ac/ActiveIn/L{i}/Power'))
			self.p_grid.append(await self.bus.importer('com.victronenergy.system', f'/Ac/Grid/L{i}/Power'))
		await self.update()

	async def _init_srv(self):
		"""setup exports"""
		srv = self.srv
		await srv.add_mandatory_paths(
			processname=__file__,
			processversion="0.1",
			connection='MoaT Inv '+self.acc_vebus.value.rsplit(".",1)[1],
			deviceinstance="1",
			serial="123457",
			productid=123211,
			productname="MoaT Inverter Controller",
			firmwareversion="0.1",
			hardwareversion=None,
			connected=1,
		)

		self.mode = await srv.add_path("/Mode", 0, description="Controller mode", writeable=True, onchangecallback=self._change_mode, gettextcallback=self._mode_name)
		self._mode = 0
		self._mode_task = None
		self._mode_task_stopped = anyio.Event()
		self._change_mode_evt = anyio.Event()


	async def _change_mode(self, value):
		if self._change_mode_evt is None:
			raise RuntimeError("try again later")
		self._mode = value
		if self._mode_task is not None:
			self._mode_task.cancel()
			await self._mode_task_stopped.wait()
		self._change_mode_evt.set()

	async def _run_mode_task(self, *, task_status=None):
		if self._mode_task is not None:
			raise RuntimeError("cannot run two tasks")
		try:
			with anyio.CancelScope() as self._mode_task:
				await self.MODE[self._mode][1](self).run(task_status)
		finally:
			self._mode_task = None
			self._mode_task_stopped.set()

	async def _start_mode_task(self):
		await self._tg.start(self._run_mode_task)

	@staticmethod
	def _mode_name(v):
		try:
			return self.MODE[v][0]
		except KeyError:
			return f'?_{v}'


	async def run(self):
		await self._init()
		self._change_mode_evt = anyio.Event()
		self._change_mode_evt.set()
		self._mode = -1
		self._mode_task = None

		name = "org.m-o-a-t.power.inverter."+self.cfg.name
		async with InvInterface(self) as self._intf, \
				   self.bus.service(name) as self._srv, \
				   anyio.create_task_group() as self._tg:
			await self._init_srv()
			while True:
				await self._change_mode_evt.wait()
				self._change_mode_evt = None
				await self._start_mode_task()
				await self._change_mode()
				await anyio.sleep(30)
				self._change_mode_evt = anyio.Event()


			
	@property
	def srv(self):
		return self._srv

	@property
	def intf(self):
		return self._intf

	@property
	def tg(self):
		return self._tg


class InvModeBase:
	def __init__(self, intf):
		self.intf = intf


@InvControl.register(-1,"off")
class InvMode_None(InvModeBase):
	"Set the AC output to zero, then do nothing."
	async def run(self, task_status):
		intf = self.intf

		for p in intf.p_set:
			await p.set_value(0)
		task_status.started()
		while True:
			await anyio.sleep(99999)


@InvControl.register(0,"idle")
class InvMode_Idle(InvModeBase):
	"Continuously set AC output to zero."
	async def run(self, task_status):
		intf = self.intf

		while True:
			for p in intf.p_set:
				await p.set_value(0)
			if task_status is not None:
				task_status.started()
				task_status = None
			await anyio.sleep(5)


@InvControl.register(1,"GridZero")
class InvMode_GridZero(InvModeBase):
	"""
	This controller attempts to minimize the power from/to the external grid.
	"""
	feed_in = 0
	async def run(self, task_status):
		while True:
			ps = []
			for ac,gr in zip(intf.p_ac, intf.p_grid):
				ps.append(ac.value-gr.value + intf.feed_in/intf.n_phase)
			pmax = max(ps)
			pmin = min(ps)
			if pmin<0 and pmax>0:
				# Ugh. Don't do that.
				dmin = sum(x if x<0 else 0 for x in ps)
				dmax = sum(x if x>0 else 0 for x in ps)

				ps = [ x-d if x>0 else x+d for x in ps]

			for p,v in zip(intf.p_set,ps):
				await p.set_value(v)

			if task_status is not None:
				task_status.started()
				task_status = None
			await anyio.sleep(5)


@InvControl.register(2,"SetSOC")
class InvMode_SetSOC(InvModeBase):
	"""
	This controller attempts to reach a given charge level.
	"""
	feed_in = 0
	async def run(self, task_status):
		while True:
			ps = []
			for ac,gr in zip(intf.p_ac, intf.p_grid):
				ps.append(ac.value-gr.value + intf.feed_in/intf.n_phase)
			pmax = max(ps)
			pmin = min(ps)
			if pmin<0 and pmax>0:
				# Ugh. Don't do that.
				dmin = sum(x if x<0 else 0 for x in ps)
				dmax = sum(x if x>0 else 0 for x in ps)

				ps = [ x-d if x>0 else x+d for x in ps]

			for p,v in zip(intf.p_set,ps):
				await p.set_value(v)

			if task_status is not None:
				task_status.started()
				task_status = None
			await anyio.sleep(5)

