#!/usr/bin/env python3

import sys
import os
import anyio
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from victron.dbus.utils import DbusInterface, CtxObj, DbusName, wrap_dbus_dict, unwrap_dbus_value, unwrap_dbus_dict
from victron.dbus import Dbus
from victron.dbus.monitor import DbusMonitor
from asyncdbus.service import method
from asyncdbus import DBusError
from datetime import datetime
from moat.util import attrdict
from moat.util.times import time_until

import logging
logger = logging.getLogger(__name__)

from ._util import balance

_dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}

class BusVars(CtxObj):
	"""
	A wrapper that creates attributes as per the class's VARS and VARS_RO arrays.

	Both are mappings:  dbusname => { attrname => path }

	The attributes registered via VARS are DbusItemImport objects.
	Attributes registered via VARS_RO are read from the bus, and set directly.
	"""
	VARS = {}
	VARS_RO = {}

	def __init__(self, bus):
		self._bus = bus

	@asynccontextmanager
	async def _ctx(self):
		async with Dbus(self._bus) as self._intf:
#			for k,v in self.VARS_RO.items():
#				for n,p in v.items():
#					setattr(self,n, (await self._intf.importer(k,p, createsignal=False)).value)
			for k,v in self.VARS.items():
				for n,p in v.items():
					setattr(self,n, await self._intf.importer(k,p))
			yield self

	@property
	def bus(self):
		return self._bus

	@property
	def intf(self):
		return self._intf


class InvInterface(DbusInterface):
	def __init__(self, ctrl):
		self.ctrl = ctrl
		super().__init__(ctrl.bus, "/Control", "inv")

	@method()
	async def GetModes(self) -> 'as':
		"""
		Return a list of available methods.
		name => (ident#, descr)
		"""
		return [ m._name for m in InvControl.MODES.values() ]

	@method()
	async def GetModeInfo(self, mode: 's') -> 'a{ss}':
		m = InvControl.MODES[mode]
		return m._doc

	@method()
	async def SetMode(self, mode: 's', args: 'a{sv}') -> 'b':
		return await self.ctrl.change_mode(mode, unwrap_dbus_dict(args))

	@method()
	async def SetModeParam(self, param: 's', value: 'v') -> 'b':
		return await self.ctrl.change_mode_param(param, unwrap_dbus_value(value))

	@method()
	async def GetState(self) -> 'a{sv}':
		return wrap_dbus_dict(self.ctrl.get_state())


class InvControl(BusVars):
	"""
	This is the main controller for an inverter.

	It can operate in various modes. Call `change_mode` to switch between them.
	There's a mandatory 30 second delay so that things can settle down somewhat.

	Configurable parameters ("system" group in `inv*.cfg`):
	"""

	#
	# Conventions used in this code:
    #
	# Max and min values are unsigned.
	# 
	# All power values (except for solar input) refer to AC. All current values refer to DC.
	#
	# The sum of all power/current values is zero by definition.
	# Positive == power/current goes to your home  / DC bus.

	# when our algorithms say "go from X to Y" we only go partways towards Y,
	# because otherwise fun nonlinear effects (solar output adapts, battery
	# voltage changes due to internal resistance, …) cause the system to oscillate.
	f_step = 0.35
	# but if the delta is smaller than this, just set it.
	# This is also used as the max "stable" change between values, i.e. assume that
	# the system has mostly settled down if the charger/inverter load changes
	# by less than this
	p_step = 100
	# if the SoC is more than f_delta away from 0/1, don't bother either
	f_delta = 0.2

	solar_p = 0  # current solar power yield

	top_off = False  # go to the battery voltage limit?
	umax_diff = 0.5  # distance to max voltage, when not topping off
	umin_diff = 0.5  # distance to min voltage

	pg_min = -12000  # watt we may send to the grid
	pg_max = 12000  # watt we may take from the grid
	inv_eff = 0.9  # inverter's typical efficiency
	p_per_phase = 4500  # inverter's max load per phase
	# TODO collect long term deltas

	# protect battery against excessive discharge if PV current should suddenly
	# fall off due to clouds. We assume that it'll not drop more than 60%.
	pv_margin = 0.4
	# try to keep the max current from the solar chargers this many amps above the 
	# current amperage so that if solar power increases the system can notice and adapt
	pv_delta = 30

	# 
	cap_scale = 4
	#
	# Approximate internal resistance of the battery pack.
	# TODO should be autodetectable (dU/dI)
	r_int=0.01
	# Per-phase variables
	# p_set_
	#   Multi, /Hub4/L{i}/AcPowerSetpoint
	#   the power we want this Multi to get/emit. Positive.
	# p_cur_
	#   System, /Ac/ActiveIn/L{i}/Power
	#   The power actually going to/from the grid. Positive.
	# p_run_
	#   Multi, /Ac/ActiveIn/L{i}/P
	#   The power the Multi is feeding us. Negative.
	# p_cons_
	#   System, /Ac/Consumption/L{i}/Power
	#   The power other consumers are taking from the bus. Negative.
	# p_crit_
	#   System, /Ac/ConsumptionOnOutput/L{i}/Power
	#   The power critical consumers are taking from the Multiplus. Negative.
	#

	# distkv
	_dkv = None
	_dkv_evt = None

	_mode = None
	MODES = {}
	@classmethod
	def register(cls, target):
		if target._name in cls.MODES:
			raise RuntimeError(f"Mode {target._mode} already known: {cls.MODES[target._mode]}")
		cls.MODES[target._name] = target
		return target

	MON = {
		'com.victronenergy.solarcharger': {
			'/Yield/Power': _dummy,
			'/CustomName': _dummy,
		},
	}

	VARS = {
		'com.victronenergy.system': dict(
			acc_vebus = '/VebusService',
			_i_pv = '/Dc/Pv/Current',
			_u_dc = '/Dc/Battery/Voltage',
			_i_batt = '/Dc/Battery/Current',
			_i_inv = '/Dc/Vebus/Current',
			_batt_soc = '/Dc/Battery/Soc',
			_p_cons1 = '/Ac/Consumption/L1/Power',
			_p_cons2 = '/Ac/Consumption/L2/Power',
			_p_cons3 = '/Ac/Consumption/L3/Power',
			_p_crit1 = '/Ac/ConsumptionOnOutput/L1/Power',
			_p_crit2 = '/Ac/ConsumptionOnOutput/L2/Power',
			_p_crit3 = '/Ac/ConsumptionOnOutput/L3/Power',
			_p_grid1 = '/Ac/Grid/L1/Power',
			_p_grid2 = '/Ac/Grid/L2/Power',
			_p_grid3 = '/Ac/Grid/L3/Power',
			s_battery = '/Dc/Battery/BatteryService',
			n_phase = '/Ac/ActiveIn/NumberOfPhases',
		),
	}

	def __init__(self, bus, cfg):
		super().__init__(bus)
		self.cfg = cfg
		self.op = cfg.get("op",{})

		for k,v in cfg.get("system",{}).items():
			try:
				vv = getattr(self,k)
				if not isinstance(vv,(type(None),int,float,str)):
					raise RuntimeError
			except AttributeError:
				logger.error("System param unknown: %r", k)
			except RuntimeError:
				logger.error("Not a system param: %r", k)
			else:
				setattr(self,k,v)

		self._trigger = anyio.Event()
		self._dkv_evt = anyio.Event()
		self.clear_state()
	
	def clear_state(self):
		self._state = {}

	def get_state(self):
		return self._state

	def set_state(self, k,v):
		self._state[k] = v

	@asynccontextmanager
	async def _ctx(self):
		async with super()._ctx():
			self.p_grid_ = []
			self.p_cons_ = []
			self.p_crit_ = []
			self.p_cur_ = []

			n_phase = self.n_phase.value or 0
			for i in range(n_phase):
				i += 1
				self.p_grid_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/Grid/L{i}/Power'))
				self.p_cons_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/Consumption/L{i}/Power'))
				self.p_crit_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/ConsumptionOnOutput/L{i}/Power'))
				self.p_cur_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/ActiveIn/L{i}/Power'))
			self.load = [0] * n_phase

			await self.update_vars()
			yield self

	@property
	def u_dc(self):
		# consider internal resistance
		return self._u_dc.value+self.i_batt*self.r_int

	@property
	def batt_soc(self):
		return self._batt_soc.value / 100

	@property
	def i_batt(self):
		return -self._i_batt.value

	@property
	def i_pv(self):
		return self._i_pv.value

	@property
	def i_inv(self):
		return self._i_inv.value

	@property
	def p_inv(self):
		"""
		AC power from the inverter.
		"""
		return -self.p_cons-self.p_grid

	@property
	def p_cons(self):
		"""
		Power from other AC consumers, between this inverter and the home meter.
		"""
		return -sum(x.value for x in self.p_cons_)

	@property
	def p_crit(self):
		"""
		Power from other AC consumers, between this inverter and the home meter.
		"""
		return -sum(x.value for x in self.p_crit_)

	@property
	def p_grid(self):
		"""
		Power as measured by the grid meter.
		"""
		return sum(x.value for x in self.p_grid_)

	@property
	def ib_max(self):
		"""
		Max battery current, discharging.
		"""
		# Remember that currents are measured from the PoV of the bus bar.
		# Thus this is the discharge current, current goes from the battery to the bus.
		if not self._ok_dis:
			return 0
		return self._ib_dis.value

	@property
	def ib_min(self):
		"""
		Max battery current, charging. The charging current is negative
		so this is the lower limit.
		"""
		# Remember that currents are measured from the PoV of the bus bar.
		# Thus the max charge current is negative, current goes into the battery.
		if not self._ok_chg:
			return 0
		return -self._ib_chg.value


	async def update_vars(self):
		if not self.acc_vebus.value:
			logger.warning("NO vebus")
			return
		self.u_min = await self.intf.importer(self.s_battery.value, '/Info/BatteryLowVoltage')
		self.u_max = await self.intf.importer(self.s_battery.value, '/Info/MaxChargeVoltage')
		self._ib_chg = await self.intf.importer(self.s_battery.value, '/Info/MaxChargeCurrent')
		self._ib_dis = await self.intf.importer(self.s_battery.value, '/Info/MaxDischargeCurrent')
		self._ok_chg = await self.intf.importer(self.s_battery.value, '/Io/AllowToCharge')
		self._ok_dis = await self.intf.importer(self.s_battery.value, '/Io/AllowToDischarge')

		self.b_cap = (await self.intf.importer(self.s_battery.value, '/Capacity', createsignal=False)).value
		self.p_set_ = []
		self.p_run_ = []
		n_phase = self.n_phase.value or 0
		for i in range(n_phase):
			i += 1
			self.p_set_.append(await self.intf.importer(self.acc_vebus.value, f'/Hub4/L{i}/AcPowerSetpoint'))
			self.p_run_.append(await self.intf.importer(self.acc_vebus.value, f'/Ac/ActiveIn/L{i}/P'))
		self._p_inv = await self.intf.importer(self.acc_vebus.value, '/Ac/ActiveIn/P', eventCallback=self._trigger_step)

	def _trigger_step(self, _sender, _path, _values):
		self._trigger.set()
		self._trigger = anyio.Event()

	async def trigger(self, sleep=3):
		await anyio.sleep(sleep)
		with anyio.move_on_after(5):
			await self._trigger.wait()

	i_batt_avg = None
	async def _i_batt_task(self, evt):
		"""
		Calculate a running average of the last four battery current values.
		"""
		b_last = [None,None,None,None]
		while True:
			b_last.pop(0)
			b_last.append(self.i_batt)
			try:
				self.i_batt_avg = sum(b_last) / len(b_last)
			except (TypeError,ValueError):
				self.i_batt_avg = 0
			else:
				if evt is not None:
					evt.set()
					evt = None
			await anyio.sleep(1.1)

	i_pv_max = 0
	async def _avg_task(self):
		"""
		Calculate exponential averages and decaying maxima.
		"""
		# well that's a lie, currently we only track i_pv_max.
		while True:
			i = self.i_pv
			if i is None:
				continue
			if self.i_pv_max < self.i_pv:
				self.i_pv_max = self.i_pv
			elif self.i_pv_max>1000 and self.i_pv < self.i_pv_max * self.pv_margin:
				# Owch, that was too fast
				pvm = self.i_pv/self.i_pv_max
				logger.error("PV went down too fast: margin factor set from %.2f to %.2f", self.pv_margin, pvm)
				self.pv_margin = pvm
			else:
				self.i_pv_max += (self.i_pv-self.i_pv_max)/20
			await anyio.sleep(0.9)

	async def _init_srv(self):
		"""setup exports"""
		srv = self.srv
		evt = anyio.Event()
		self._tg.start_soon(self._i_batt_task, evt)
		self._tg.start_soon(self._avg_task)
		self._tg.start_soon(self._distkv_main)

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

		# self.mode = await srv.add_path("/Mode", 0, description="Controller mode", writeable=True, onchangecallback=self._change_mode, gettextcallback=self._mode_name)

		# await self.mode.set_value(self._mode)
		self._mode_task = None
		self._mode_task_stopped = anyio.Event()
		self.dest_p = self.last_p = -self.p_grid-self.p_cons
		await evt.wait()

	async def _change_mode(self, path, value):
		await self.change_mode(value)

	async def change_mode(self, mode:str, data={}):
		if self._mode is None or self._mode != mode:
			if self._change_mode_evt is None:
				raise DBusError("org.m_o_a_t.inv.too_early", "try again later")
			if mode not in self.MODES:
				raise DBusError("org.m_o_a_t.inv.unknown", "unknown mode")
			self._mode = mode
			# TODO verify the new mode's parameters
			self._change_mode_evt.set()

		# TODO verify parameters for current mode
		self.op.update(data)
		for k,v in self.cfg["modes"].get(self._mode, {}).items():
			self.op.setdefault(k,v)
		return True

	async def change_mode_param(self, param: str, value: Any) -> bool:
		if not param or param[0]=='_' or param not in self.MODES[self._mode]._doc:
			raise DBusError("org.m_o_a_t.inv.unknown", f"unknown parameter for {self._mode}")
		self.op[param] = value
		return True

	async def _run_mode_task(self):
		if self._mode_task is not None:
			raise RuntimeError("cannot run two tasks")
		try:
			with anyio.CancelScope() as self._mode_task:
				m = self.MODES[self._mode]
				self.clear_state()
				self.set_state("mode", [m._name, self.op])
				self.op.update(self.cfg["modes"].get(self._mode, {}))
				await m(self).run()
		finally:
			logger.debug("MODE STOP %s", m._name)
			self._mode_task = None
			self._mode_task_stopped.set()

	async def _start_mode_task(self):
		if self._mode_task is not None:
			self._mode_task.cancel()
			await self._mode_task_stopped.wait()
			self._mode_task_stopped = anyio.Event()
		self._tg.start_soon(self._run_mode_task)

	async def _solar_log(self):
		async with DbusMonitor(self._bus, self.MON) as mon:
			power = 0
			dkv = await self.distkv
			if dkv:
				val = await dkv.get(self.distkv_prefix / "solar" / "energy")
				if val and "value" in val:
					power = val.value
			t = anyio.current_time()
			t_sol = t+5
			mt = (min(time_until((n,"min")) for n in range(0,60,15)) - datetime.now()).seconds
			print(mt)
			while True:
				n = 0
				while n < mt: # 15min
					t += 1
					n += 1
					cur_p = 0
					for chg in mon.get_service_list('com.victronenergy.solarcharger'):
						cur_p += (mon.get_value(chg, '/Yield/Power') or 0)
					power += cur_p
					self.solar_p = cur_p
					if t >= t_sol:
						if dkv:
							for chg in mon.get_service_list('com.victronenergy.solarcharger'):
								name = mon.get_value(chg, '/CustomName')
								ni = name.find(" : ")
								if ni >= 0:
									name = name[ni+3:]
								pp = (mon.get_value(chg, '/Yield/Power') or 0)
								await dkv.set(self.distkv_prefix / "solar" / "p" / name, pp, idem=True)
							await dkv.set(self.distkv_prefix / "solar" / "p", cur_p, idem=True)
							await dkv.set(self.distkv_prefix / "solar" / "batt_pct", self.batt_soc, idem=True)
							await dkv.set(self.distkv_prefix / "solar" / "grid", self.p_grid, idem=True)
						t_sol=t+10
					await anyio.sleep_until(t)
				print(power)
				if dkv:
					await dkv.set(self.distkv_prefix / "solar" / "energy", power, idem=True)

				mt = 900

	async def _distkv_main(self):
		if "distkv" not in self.cfg:
			self._dkv_evt.set()
			self._dkv_evt = None
			return

		# TODO use a service scope instead
		from distkv.client import open_client as distkv_client
		try:
			async with distkv_client(**self.cfg["distkv"]) as dkv:
				self._dkv = dkv
				self.distkv_prefix = self.cfg["distkv"]["root"]
				self._dkv_evt.set()
				while True:
					await anyio.sleep(99999)
		finally:
			self._dkv = None

	@property
	async def distkv(self):
		if self._dkv_evt is None:
			return False
		await self._dkv_evt.wait()
		return self._dkv

	async def _init_intf(self):
		proxy = await self._bus.get_proxy_object(self.s_battery.value, "/bms")
		self._bms_intf = await proxy.get_interface("org.m_o_a_t.bms")

		self._batt_intf = []
		for i in range(await self._bms_intf.call_get_n_batteries()):
			proxy = await self._bus.get_proxy_object(self.s_battery.value, f"/bms/{i}")
			self._batt_intf.append(await proxy.get_interface("org.m_o_a_t.bms"))
		# TODO multiple batteries
	
	async def get_bms_work(self, poll:bool = False, clear:bool = False):
		return await self._bms_intf.call_get_work(poll, clear)

	async def get_bms_voltages(self):
		return [ unwrap_dbus_dict(x) for x in await self._bms_intf.call_get_voltages() ]

	async def get_bms_currents(self):
		return await self._bms_intf.call_get_currents()

	async def get_bms_config(self):
		return unwrap_dbus_dict(await self._bms_intf.call_get_config())

	async def set_bms_capacity(self, n:int, cap:float, loss:float, top:bool=False):
		if n != 0:
			raise NotImplementedError(n)
		return await self._batt_intf[n].call_set_capacity(cap, loss, top)

	async def run(self, mode=None):
		self._change_mode_evt = anyio.Event()
		self._change_mode_evt.set()
		self._mode = mode or self.cfg["modes"]["default"]
		self._mode_task = None

		name = "org.m-o-a-t.power.inverter"

		async with InvInterface(self) as self._ctrl, \
				   self.intf.service(name) as self._srv, \
				   anyio.create_task_group() as self._tg:
		   
			self._tg.start_soon(self._init_intf)
			if not self.acc_vebus.value:
				logger.warning("VEBUS not known")
				await self.acc_vebus.refresh()
				if not self.acc_vebus.value:
					raise RuntimeError("VEBUS not known")
			await self._init_srv()
			if not self.op.get("fake", False):
				self._tg.start_soon(self._solar_log)
			async with DbusName(self.bus, f"org.m_o_a_t.inv.{self.cfg.get('name', 'fake' if self.op.get('fake', False) else 'main')}"):
				while True:
					await self._change_mode_evt.wait()
					self._change_mode_evt = None
					await self._start_mode_task()
					await anyio.sleep(30)
					self._change_mode_evt = anyio.Event()

	def i_from_p(self, p, rev=False):
		"""
		Calculate how much DC current a given inverter output would generate.

		Set `rev` if you want to know the DC current you'd need for a given AC power.
		"""
		res = -p / self.u_dc
		if rev == (res<0):
			res /= self.inv_eff
		else:
			res *= self.inv_eff
		# logger.debug("I %.1f from P %.0f %s", res, p, "R" if rev else "")
		return res

	def p_from_i(self, i, rev=False):
		"""
		Calculate how much AC power to set for a given inverter DC current would generate.

		Set `rev` if you want to know the AC power you'd need for a given DC current.
		"""
		res = -i * self.u_dc
		if rev == (res>0):
			res /= self.inv_eff
		else:
			res *= self.inv_eff
		# logger.debug("P %.0f from I %.1f %s", res, i, "R" if rev else "")
		return res


	def calc_batt_i(self, i):
		logger.debug("Want bat I: %.1f", i)
		ii = max(self.ib_min, min(i, self.ib_max))
		if ii != i:
			logger.debug("Adj: bat I: %f", ii)
		# i_pv+i_batt+i_inv == zero
		return self.calc_inv_i(-ii - self.i_pv)


	def calc_inv_i(self, i):
		logger.debug("Want inv I: %.1f", i)
		return self.calc_inv_p(self.p_from_i(i))


	def calc_grid_p(self, p, excess=None):
		"""
		Set power from/to the grid. Positive = take power.

		p_cons+p_grid+p_inv == zero by definition.
		"""
		logger.debug("Want grid P: %.0f", p)

		p = -self.p_cons-p
		return self.calc_inv_p(p, excess=excess)

	def calc_inv_p(self, p, excess=None, phase=None):
		"""
		Calculate inverter/charger power
		"""
		n_phase = self.n_phase.value or 0
		if not n_phase:
			# no input
			return []

		lims = []
		no_lims = []
		p_info = dict(
			limits=lims,
			# non_limits=no_lims,
			init=p,
		)

		logger.debug("WANT inv P: %.0f", p)
		op = p

		i_inv = self.i_from_p(p, rev=True)
		i_batt = -i_inv-self.i_pv

		# if the PV input is close to the maximum, increase power
		i_max = self.ib_max-i_inv
		lim = dict(
			rule="I_PVD",
			pvmax=self.i_pv_max, pvdelta=self.pv_delta,
			imax=i_max, ib=i_batt,
			lim="pvmax>pvdelta, imax-ib<pvdelta",
		)
		if self.i_pv_max > self.pv_delta and i_max-i_batt < self.pv_delta:
			lim["fix"] = "ib=imax-pv_delta"
			lim["res"] = i_batt = i_max-self.pv_delta
			lims.append(lim)
		else:
			no_lims.append(lim)

		# if we're close to the max voltage, slow down / stop early
		i_maxchg = self.b_cap/self.cap_scale * ((0 if self.top_off else self.umax_diff)-(self.u_max.value-self.u_dc)) / self.umax_diff
		lim=dict(
			rule="U_MAX",
			max=i_maxchg, cap_lim=self.b_cap/self.cap_scale, 
			range=(0 if self.top_off else self.umax_diff, self.u_max.value-self.u_dc, self.umax_diff),
			umax=self.u_max.value, udc=self.u_dc, ib=i_batt,
			lim="ib<max",
		)
		if i_batt < i_maxchg:
			lim["fix"] = "ib=max"
			i_batt = i_maxchg
			i_inv = -i_batt-self.i_pv
			lims.append(lim)
			lim["res"] = {"batt": i_batt, "inv": i_inv}
		else:
			no_lims.append(lim)

		# On the other side, if we're close to the min voltage, limit discharge rate.
		i_maxdis = -self.b_cap/self.cap_scale * (self.umin_diff-(self.u_dc-self.u_min.value)) / self.umin_diff
		lim=dict(
			rule="U_MIN",
			min=i_maxdis, cap_lim=self.b_cap/self.cap_scale, 
			range=(self.umin_diff, self.u_dc-self.u_min.value),
			umin=self.u_min.value, udc=self.u_dc, ib=i_batt,
			lim="ib<min",
		)
		if i_batt > i_maxdis:
			lim["fix"] = "ib=min"
			i_batt = i_maxdis
			i_inv = -i_batt-self.i_pv
			lim["res"] = {"batt": i_batt, "inv": i_inv}
			lims.append(lim)
		else:
			no_lims.append(lim)

		# The system tells the solar chargers how much they may deliver.
		# However, we want to leave a margin for them
		# so that we can actually notice when PV output increases.
		#
#             'limits': [{'d': 17.970052750227836,
#                         'fix': 'ib-=d',
#                         'ibmin': -83.2819387025245,
#                         'inv': -56.74800979149463,
#                         'ipv': 128.00000124424696,
#                         'lim': 'max-ipv<pvdelta',
#                         'max': 140.02994849401912,
#                         'pvdelta': 30,
#                         'res': {'batt': -89.22204420298016,
#                                 'inv': -38.7779570412668},
#                         'rule': 'I_MAX'}],

		i_pv_max = -self.ib_min-i_inv  # this is what Venus systemcalc sets the PV max to
		lim=dict(
			rule="I_MAX",
			max=i_pv_max, ipv=self.i_pv, pvdelta=self.pv_delta,
			ibmin=self.ib_min, inv=i_inv,
			lim="max-ipv<pvdelta",
		)
		if i_pv_max-self.i_pv < self.pv_delta:
			lim["d"] = d = self.pv_delta-(i_pv_max-self.i_pv)
			lim["fix"] = "ib-=d"
			i_batt -= d
			i_inv = -i_batt-self.i_pv
			lim["res"] = {"batt": i_batt, "inv": i_inv}
			lims.append(lim)
		else:
			no_lims.append(lim)

		# Now check some AC limits.
		p = self.p_from_i(i_inv)

		# Don't overload the charger.
		lim = dict(
			rule="P_MIN",
			p=p, min=self.pg_min,
			lim="p<min",
		)
		if p < self.pg_min:
			lim["fix"] = "p=min"
			lim["res"] = p = self.pg_min
			lims.append(lim)
		else:
			no_lims.append(lim)

		# Don't overload the inverter.
		lim = dict(
			rule="P_MAX",
			p=p, max=self.pg_max,
			lim="p>max",
		)
		if p > self.pg_max:
			lim["fix"] = "p=max"
			lim["res"] = p = self.pg_max
			lims.append(lim)
		else:
			no_lims.append(lim)


		# We want to be on the safe side with varying PV input. Consider this state:
		# * PV delivers 60 A
		# * battery discharge maximum is 60 A
		# * thus we think it's safe for the inverter to take 120A
		# * now PV output drops to 20A due to an ugly black cloud
		# * 40A over the discharge limit is probably enough to trip the BMS
		# * … owch.
		# 
		i_inv = self.i_from_p(p, rev=True)
		i_pv_min = self.i_pv_max * self.pv_margin
		lim = dict(
			rule="I_MIN",
			inv=i_inv, pvmin=i_pv_min, ibmax=self.ib_max,
			lim="-inv-pvmin>ibmax",
			max = self.i_pv_max, margin=self.pv_margin
		)
		if -i_inv-i_pv_min > self.ib_max:
			i_inv = -i_pv_min-self.ib_max
			i_batt = -i_inv-self.i_pv
			lim["fix"] = "inv=-pvmin-ibmax"
			lim["res"] = {"batt": i_batt, "inv": i_inv}
			lims.append(lim)
		else:
			no_lims.append(lim)

		# Don't push more into the battery than allowed.
		lim = dict(
			rule="IB_ERR_L",
			batt=i_batt, min=self.ib_min,
			lim="batt<min",
		)
		if i_batt < self.ib_min:
			lim["fix"] = "batt=min",
			i_batt = self.ib_min
			i_inv = -i_batt-self.i_pv
			lim["res"] = {"batt": i_batt, "inv": i_inv}
		# no add to no_lims because it's too obvious

		# Don't pull more from the battery than allowed.
		lim = dict(
			rule="IB_ERR_H",
			batt=i_batt, max=self.ib_max,
			lim="batt>max",
		)
		if i_batt > self.ib_max:
			lim["fix"] = "batt=max",
			i_batt = self.ib_max
			i_inv = -i_batt-self.i_pv
			lim["res"] = {"batt": i_batt, "inv": i_inv}
		# no add to no_lims because it's too obvious

		# back to the AC side
		p = self.p_from_i(i_inv)

		# Don't exceed what we're allowed to feed to the grid.
		if excess is None:
			no_lims.append({"rule":"P_EXC", "exc":"-"})
		else:
			lim=dict(
				rule="P_EXC",
				lim="p>op+exc, p>0",
				p=p,op=op,exc=excess,
			)
			if p > 0 and p > op+excess:
				lim["fix"] = "p=op+exc"
				lim["res"] = p = op+excess
				lims.append(lim)
			else:
				no_lims.append(lim)

		# This part ensures that we don't take too-huge steps towards
		# the new value, which would cause instabilities.
		if self.f_delta <= self.batt_soc <= 1-self.f_delta or self.small_p_step(self.last_p, p):
			# Small(ish) change from last target, or far enough away from SoC bounds not to care.
			# Implement directly.
			np = p
			self.step = 1
		else:
			lim = dict(rule="P_GRAD")

			if self.small_p_step(self.dest_p, p):
				# the goal is roughly the same as last time
				self.step += 1
			else:
				# reset the step counter if the goal has changed significantly
				self.step = 2

			# The first step goes 1/3rd towards the destination (if f_step
			# is 1/3). The second step, ~ halfway. This tries to strike a
			# hopefully-reasonable balance between slow exponential decay
			# and going too fast.
			pd = (p-self.last_p) * self.f_step**(2/self.step)

			# If the scaled-off step is < p_step, use that instead:
			# smaller steps should be taken last.
			if abs(pd) < self.p_step:
				pd = self.p_step * (-1 if pd<0 else 1)
			np = self.last_p + pd
			lim["step"] = (pd,self.last_p,p,np)
			logger.debug("P_GRAD: %.0f > %.0f = %.0f", self.last_p,p,np)

			lims.append(lim)

		p_info["dest"] = self.dest_p = p
		self.last_p = np

		p_info["setpoint"] = np

		if phase is None and n_phase > 1:
			ps = self.to_phases(np)
		else:
			ps = [0] * n_phase
			ps[phase-1 if phase else 0] = np
		p_info["inv_phases"] = ps

		# The return value is the inverter output, which needs
		# to be adjusted for loads connected to it
		p_info["phases"] = phases = [ a-b.value for a,b in zip(ps, self.p_crit_) ]

		self.set_state("inverter", p_info)
		return phases

	def small_p_step(self, p, q):
		if abs(p-q) < self.p_step:
			return True
		if (p>0) != (q>0):
			return False
		if 10/12 < ((self.p_step+abs(p))/(self.p_step+abs(q))) < 12/10:
			return True
		return False

	def to_phases(self, p):
		"""
		Distribute a given power to the phases so that the result is balanced,
		or at least as balanced as possible given that to feed in from one phase
		while sending energy out to another phase is a waste of energy.

		This step does not change the total, assuming the per-phase limits
		are not exceeded.
		"""

		n_phase = self.n_phase.value or 0
		if not n_phase:
			return []

		self.load = [ -b.value for b in self.p_cons_ ]
		load_avg = sum(self.load)/n_phase

		ps = [ p/n_phase - (g-load_avg) for g in self.load ]
		ps = balance(ps, min=-self.p_per_phase, max=self.p_per_phase)
		return ps


	async def set_inv_ps(self, ps):
		# OK, we're safe, implement
		n_phase = self.n_phase.value or 0
		if not n_phase:
			return

		if self.op.get("fake", False):
			if n_phase > 1:
				logger.error("NO-OP SET inverter %.0f ∑ %s", -sum(ps), " ".join(f"{-x :.0f}" for x in ps))
			else:
				logger.error("NO-OP SET inverter %.0f", -ps[0])
			return

		if n_phase > 1:
			logger.info("SET inverter %.0f ∑ %s", -sum(ps), " ".join(f"{-x :.0f}" for x in ps))
		else:
			logger.info("SET inverter %.0f", -ps[0])

		for p,v in zip(self.p_set_, ps):
			await p.set_value(-v)
			# Victron Multiplus: negative=inverting: positive=charging
			# This code: negative=takes from AC, positive=feeds to AC power
			# thus this is inverted

			
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
		self.ps = None
		self.ps_min = None
		self.ps_max = None
		self.running = False

	# p_set_
	#   the power we want this Multi to get/emit. Negative.
	# p_cur_
	#   The power actually going to/from the grid. Positive.
	# p_run_
	#   The power the Multi is feeding us. Negative.
	# p_cons_
	#   The power other consumers are taking from the bus. Negative.
	async def set_inv_ps(self, ps):
		intf = self.intf
		n_phase = intf.n_phase.value or 0
		if not n_phase:
			self.ps_min = None
			self.ps_max = None
			return

		if self.ps_min is None:
			self.ps_min = [-999999999] * n_phase
			self.ps_max = [999999999] * n_phase

		# This convoluted step thinks about inverter output limits.
		# Specifically, if one of them is overloaded and another is not
		# we distribute the excess to others inverters.
		#
		# Yes this may result in one grid phase going in while others
		# go out, but that could already happen anyway and should not
		# materially increase grid load differences across phases.
		if self.running and n_phase > 1:
			pd_min = pd_max = 0
			d_min = d_max = 0
			ops = ps

			# First pass: determine the invertes' current operational limits.
			for i in range(n_phase):
				p = ps[i]
				p_set = intf.p_set_[i].value
				p_cur = intf.p_cur_[i].value
				p_run = intf.p_run_[i].value
				p_cons = -intf.p_cons_[i].value
				p_min = self.ps_min[i]
				p_max = self.ps_max[i]
				# logger.debug("%.0f %.0f %.0f %.0f %.0f %.0f", p_set,p_cur,p_run,p_cons,p_min,p_max)

				if p_set < 0:
					if p_set < p_run-20:
						# power out seems to be limited
						self.ps_min[i] = p_min = p_run
						if p < p_min:
							pd_min += p_min-p-50
					elif p_min == -999999999:
						pass
					elif p_min >= p_run-10:
						self.ps_min[i] = -999999999
						# unknown, we don't ask for it
					elif p<0 and p<p_min-50:
						# this will go below the limit
						pd_min += p_min-p-50

				elif p_set > 0: # same in reverse for p_max
					if p_set > p_run+20:
						self.ps_max[i] = p_max = p_run
						if p > p_max:
							pd_max += p-p_max+50
					elif p_max == 999999999:
						pass
					elif p_max <= p_run-10:
						self.ps_max[i] = 999999999
					elif p>0 and p>p_max-50:
						pd_max += p_max-p+50

			# Second pass. Distribute difference to lower-powered phases.
			pa = [(i,x) for i,x in enumerate(ps)]
			if pd_min > 0:
				# We sort by "worst-hit device last". Going through the
				# list from the end, the delta is accumulated in d_min and
				# then distributed to the remaining devices, if any.
				#
				# We add a fudge of 50W so that we can discover (in the next
				# round's first pass) whether the limit has been lifted.
				# 50W is too high for small batteries, but if you really
				# do run a multiphase system on a 12V 20A battery, you're
				# going to have worse problems than this. :-P
				pa.sort(key=lambda x: -ps[x[0]] + self.ps_min[x[0]])
				# logger.debug("MIN Pre %s", pa)
				pb = []
				d_min = 0
				while pa:
					i,v = pa.pop()
					p_run = intf.p_run_[i].value
					p_min = self.ps_min[i]
					if v < p_min:  # over the limit
						d_min += p_min-v
						v = p_min-50
					else:
						pp = d_min/(len(pa)+1)
						if v-pp < p_min: # limited by min
							d_min -= v-p_min
							v = p_min-50
						else: # can use pp
							d_min -= pp+50
							v -= pp+50
					pb.append((i,v))
				pa = pb

			if pd_max > 0:
				# same as above for taking power from the grid.
				# Not yet tested because it's summer.
				pa.sort(key=lambda x: ps[x[0]] - self.ps_max[x[0]])
				# logger.debug("MAX Pre %s", pa)
				pb = []
				d_max = 0
				while pa:
					i,v = pa.pop()
					p_run = intf.p_run_[i].value
					p_max = self.ps_max[i]
					if v > p_max:  # over the limit
						v = p_max+50
					else:
						pp = d_max/(len(pa)+1)
						if v+pp > p_max: # limited by max
							d_max -= p_max-v
							v = p_max+50
						else: # can use pp
							d_max -= pp+50
							v += pp+50
					pb.append((i,v))
				pa = pb
			ps = [ x[1] for x in sorted(pa, key=lambda x:x[0]) ]

			if ops != ps and (d_min > 0 or d_max > 0):
				# logger.debug("START %s",ops)
				if d_min > 0:
					logger.debug("PD_MIN: P %.0f, want %.0f", sum(x[1] for x in pa), d_min)
				if d_max > 0:
					logger.debug("PD_MAX: P %.0f, want %.0f", sum(x[1] for x in pa), d_max)
				# togger.debug("END %s",ps)

		await intf.set_inv_ps(ps)

		p = intf.p_inv
		await intf.trigger()
		n=0
		nt=False
		while n<10:
			await intf.trigger()
			pp = intf.p_inv
			logger.debug("now %.0f", pp)
			if abs(pp-p) < intf.p_step:
				if nt:
					break
				nt=True
			else:
				nt=False
			p=pp
			n += 1
		self.running = True


#from ._utils import _loader
def _loader(path, cls, reg):
	from pathlib import Path
	from importlib import import_module

	def _imp(name):
		m = import_module("."+name, package=_loader.__module__)
		for n in m.__all__:
			c = getattr(m,n)
			if isinstance(c,type) and issubclass(c,cls) and hasattr(c,'_name'):
				reg(c)

	path = Path(__path__[0])
	for filename in path.glob("*"):
		if filename.name[0] in "._":
			continue
		if filename.name.endswith(".py"):
			_imp(filename.name[:-3])
		elif (path / filename / "__init__.py").is_file():
			_imp(filename.name)

_loader(__path__[0], InvModeBase, InvControl.register)
del _loader
