#!/usr/bin/env python3

import sys
import os
import anyio
from contextlib import asynccontextmanager, contextmanager

from victron.dbus.utils import DbusInterface, CtxObj, DbusName
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
			for k,v in self.VARS_RO.items():
				for n,p in v.items():
					setattr(self,n, (await self._intf.importer(k,p, createsignal=False)).value)
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

		del self.ctrl

	@method()
	async def GetModes(self) -> 'a{s(is)}':
		"""
		Return a dict of available methods.
		name => (ident#, descr)
		"""
		res = {}
		for i,nm in InvControl.MODE.items():
			n,m = nm
			res[n] = (i, m.__doc__)
		return res

	@method()
	async def SetMode(self, mode: 'i') -> 'a{s(is)}':
		await self.ctrl.change_mode(mode)


class InvControl(BusVars):
	"""
	This is the main controller for an inverter.

	It can operate in various modes. Call `change_mode` to switch between them.
	There's a mandatory 30 second delay so that things can settle down somewhat.
	"""

	#
	# Conventions used in this code:
    #
	# Max and min values are unsigned.
	# 
	# All power values refer to AC. All current values refer to DC.
	#
	# The sum of all power/current values is zero by definition.
	# Positive == power/current goes to your home  / DC bus.

	# when our algorithms say "go from X to Y" we only go partways towards Y,
	# because otherwise fun nonlinear effects (solar output adapts, battery
	# voltage changes due to internal resistance, …) cause the system to oscillate.
	f_dampen = 0.35
	# but if the delta is smaller than this, just set it.
	# This is also used as the max "stable" change between values, i.e. assume that
	# the system has mostly settled down if the charger/inverter load changes
	# by less than this
	p_dampen = 100

	_top_off = False  # go to the battery voltage limit?
	umax_diff = 0.5  # distance to max voltage, when not topping off
	umin_diff = 0.5  # distance to min voltage

	pg_min = -12000  # watt we may send to the grid
	pg_max = 12000  # watt we may take from the grid
	inv_eff = 0.9  # inverter's min efficiency
	p_per_phase = 4000  # inverter's max load per phase
	# TODO collect long term deltas

	# protect battery against excessive discharge if PV current should suddenly
	# fall off due to clouds. We assume that it'll not drop more than 60% during
	# any five second interval.
	pv_margin = 0.4
	# try to keep the max current from the solar chargers this many amps above the 
	# current amperage so that if solar power increases the system can notice and adapt
	pv_delta = 30

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
	#

	MODE = {}
	@classmethod
	def register(cls, target):
		if target._mode in cls.MODE:
			raise RuntimeError(f"Mode {target._mode} already known: {cls.MODE[target._mode]}")
		cls.MODE[target._mode] = target
		return target

	MON = {
		'com.victronenergy.solarcharger': {
			'/Yield/Power': _dummy,
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
			_p_grid1 = '/Ac/Grid/L1/Power',
			_p_grid2 = '/Ac/Grid/L2/Power',
			_p_grid3 = '/Ac/Grid/L3/Power',
			s_battery = '/Dc/Battery/BatteryService',
		),
	}
	VARS_RO = {
		'com.victronenergy.system': dict(
			n_phase = '/Ac/ActiveIn/NumberOfPhases',
		),
	}

	def __init__(self, bus, cfg):
		super().__init__(bus)
		self.cfg = cfg
		self.op = cfg.get("op",{})

		for k,v in cfg.items():
			if k in vars(self):
				setattr(self,k,v)

		self._trigger = anyio.Event()

	@asynccontextmanager
	async def _ctx(self):
		async with super()._ctx():
			self.p_grid_ = []
			self.p_cons_ = []
			self.p_cur_ = []

			for i in range(self.n_phase):
				i += 1
				self.p_grid_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/Grid/L{i}/Power'))
				self.p_cons_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/Consumption/L{i}/Power'))
				self.p_cur_.append(await self.intf.importer('com.victronenergy.system', f'/Ac/ActiveIn/L{i}/Power'))
			self.load = [0] * self.n_phase

			await self.update_vars()
			yield self

	@property
	def u_dc(self):
		return self._u_dc.value

	@property
	def batt_soc(self):
		return self._batt_soc.value

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
		self.u_min = await self.intf.importer(self.s_battery.value, '/Info/BatteryLowVoltage')
		self.u_max = await self.intf.importer(self.s_battery.value, '/Info/MaxChargeVoltage')
		self._ib_chg = await self.intf.importer(self.s_battery.value, '/Info/MaxChargeCurrent')
		self._ib_dis = await self.intf.importer(self.s_battery.value, '/Info/MaxDischargeCurrent')
		self._ok_chg = await self.intf.importer(self.s_battery.value, '/Io/AllowToCharge')
		self._ok_dis = await self.intf.importer(self.s_battery.value, '/Io/AllowToDischarge')

		self.b_cap = (await self.intf.importer(self.s_battery.value, '/Capacity', createsignal=False)).value
		self.p_set_ = []
		self.p_run_ = []
		for i in range(self.n_phase):
			i += 1
			self.p_set_.append(await self.intf.importer(self.acc_vebus.value, f'/Hub4/L{i}/AcPowerSetpoint'))
			self.p_run_.append(await self.intf.importer(self.acc_vebus.value, f'/Ac/ActiveIn/L{i}/P'))
		self._p_inv = await self.intf.importer(self.acc_vebus.value, '/Ac/ActiveIn/P', eventCallback=self._trigger_step)

	def _trigger_step(self, _sender, _path, _values):
		self._trigger.set()
		self._trigger = anyio.Event()

	async def trigger(self, sleep=3):
		await anyio.sleep(sleep)
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

		self._mode = self.cfg.get("mode",0)
		await self.mode.set_value(self._mode)
		self._mode_task = None
		self._mode_task_stopped = anyio.Event()
		self.last_p = -self.p_grid-self.p_cons
		await evt.wait()

	async def _change_mode(self, path, value):
		await self.change_mode(value)

	async def change_mode(self, value):
		if self._change_mode_evt is None:
			raise DBusError("org.m_o_a_t.inv.too_early", "try again later")
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
				m = self.MODE[self._mode]
				cfg = self.cfg.get(m._name, {})
				await m(self, cfg).run(task_status)
		finally:
			self._mode_task = None
			self._mode_task_stopped.set()

	async def _start_mode_task(self):
		await self._tg.start(self._run_mode_task)

	@classmethod
	def _mode_name(cls, path, value):
		try:
			return cls.MODE[value]._name
		except KeyError:
			return f'?_{v}'

	async def _solar_log(self):
		async with DbusMonitor(self._bus, self.MON) as mon:
			power = 0
			t = anyio.current_time()
			mt = (min(time_until((n,"min")) for n in range(0,60,15)) - datetime.now()).seconds
			print(mt)
			while True:
				n = 0
				while n < mt: # 15min
					t += 1
					n += 1
					for chg in mon.get_service_list('com.victronenergy.solarcharger'):
						power += mon.get_value(chg, '/Yield/Power')
					await anyio.sleep_until(t)
				print(power)
				mt = 900

	async def run(self):
		self._change_mode_evt = anyio.Event()
		self._change_mode_evt.set()
		self._mode = 0
		self._mode_task = None

		name = "org.m-o-a-t.power.inverter"
		async with InvInterface(self) as self._ctrl, \
				   self.intf.service(name) as self._srv, \
				   anyio.create_task_group() as self._tg:
		   
			await self._init_srv()
			async with DbusName(self.bus, f"org.m_o_a_t.inv.main"):
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
		if rev:
			res /= self.inv_eff
		else:
			res *= self.inv_eff
		return res

	def p_from_i(self, i, rev=False):
		"""
		Calculate how much AC power to set for a given inverter DC current would generate.

		Set `rev` if you want to know the AC power you'd need for a given DC current.
		"""
		res = -i * self.u_dc
		if rev:
			res /= self.inv_eff
		else:
			res *= self.inv_eff
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

	@contextmanager
	def topping_off(self):
		"""
		Context manager that allows getting close to the battery's max voltage.
		"""
		self._top_off = True
		try:
			yield self
		finally:
			self._top_off = False

	def calc_inv_p(self, p, excess=None, phase=None):
		"""
		Calculate inverter/charger power
		"""
		logger.debug("WANT inv P: %.0f", p)
		op = p

		i_inv = self.i_from_p(p, rev=True)
		i_batt = -i_inv-self.i_pv

		# if the PV input is close to the maximum, increase power
		i_max = self.ib_max-i_inv
		if self.i_pv_max > self.pv_delta and i_max-i_batt < self.pv_delta:
			logger.debug("I_PVD: I %.1f + %.1f < %.1f", i_batt, i_max, self.pv_delta)
			i_batt = i_max-self.pv_delta
		else:
			logger.debug("-I_PVD: I %.1f + %.1f > %.1f", i_batt, i_max, self.pv_delta)

		# if we're close to the max voltage, slow down / stop early
		i_maxchg = self.b_cap/4 * (((0 if self._top_off else self.umax_diff)-(self.u_max.value-self.u_dc)) / self.umax_diff)
		if i_batt < i_maxchg:
			logger.debug("U_MAX: I %.1f < %.1f", i_batt,i_maxchg)
			i_batt = i_maxchg
		else:
			logger.debug("-U_MAX: I %.1f > %.1f", i_batt,i_maxchg)

		# On the other side, if we're close to the min voltage, limit discharge rate.
		i_maxdis = -self.b_cap/5 * (((self.umin_diff)-(self.u_dc-self.u_min.value)) / self.umin_diff)
		if i_batt > i_maxdis:
			logger.debug("U_MIN: I %.1f > %.1f", i_batt,i_maxdis)
			i_batt = i_maxdis
		else:
			logger.debug("-U_MIN: I %.1f < %.1f", i_batt,i_maxdis)

		# We need to be on the safe side WRT considering PV current. Consider this state:
		# * PV delivers 60 A
		# * battery discharge is limited to 60 A
		# * thus we think it's safe to take 120A
		# * now PV output drops to 20A due to an ugly black cloud
		# * 40A over the discharge limit is probably enough to trip the BMS
		# * … owch.
		# 
		i_pv_min = self.i_pv_max * self.pv_margin
		if i_batt+i_pv_min > self.ib_max:
			logger.debug("I_MIN: I %.1f < %.1f %.1f", i_batt,self.ib_max,i_pv_min)
			i_batt = self.ib_max-i_pv_min
		else:
			logger.debug("-I_MIN: I %.1f > %.1f %.1f", i_batt,self.ib_max,i_pv_min)

		# The reverse cannot happen because the system tells the solar chargers
		# how much they may deliver. However, we want to leave a margin for them
		# so that we can actually notice when PV output increases.
		#
		i_inv = -i_batt-self.i_pv
		i_pv_max = -self.ib_min-i_inv  # this is what Venus systemcalc sets the PV max to
		if i_pv_max-self.i_pv < self.pv_delta:
			logger.debug("I_MAX: I %.1f %.1f < %.1f %.1f %.1f", i_batt, i_pv_max, self.ib_min, self.i_pv, self.pv_delta)
			i_batt -= self.pv_delta-(i_pv_max-self.i_pv)
			i_inv = -i_batt-self.i_pv
		else:
			logger.debug("-I_MAX: I %.1f %.1f > %.1f %.1f %.1f", i_batt, i_pv_max, self.ib_min, self.i_pv, self.pv_delta)

		if i_batt < self.ib_min or i_batt > self.ib_max:
			logger.error("IB ERR %.1f %.1f %.1f", self.ib_min, i_batt, self.ib_max)
			i_batt = max(self.ib_min,min(self.ib_max,i_batt))
			#i_inv = -i_batt-self.i_pv

		# We'll assume that this works.
		p = self.p_from_i(i_inv)

		# All adaption needs to be gradual because the parameters
		# feed back on themselves. We don't want positive feedback loops.
		if abs(p-self.last_p) < self.p_dampen:
			# Small change. Implement directly.
			np = p
		else:
			pd = (p-self.last_p)*self.f_dampen
			# The original step was > p_dampen but the scaled-down step
			# might not be, which is not quite what's intended: any smaller
			# step should be taken last.
			if -self.p_dampen < pd < 0:
				pd = -self.p_dampen
			elif 0 < pd < self.f_dampen:
				pd = self.p_dampen
			np = self.last_p + pd
			logger.debug("P_GRAD: %.0f > %.0f = %.0f", self.last_p,p,np)
		self.last_p = np

		# Apply external limits.
		# WARNING: the changes below here must go towards zero, because "nothing happens"
		# always is a valid state and the BMS plus the Victron system make sure that
		# the inverter's drawing or feeding in less power than requested won't hurt.
		if excess is not None and np>0 and np > op+excess:
			logger.debug("P_EXC: nP %.0f, max %.0f+%.0f", np, op,excess)
			np = op+excess
		elif excess is not None:
			logger.debug("-P_EXC: nP %.0f, max %.0f+%.0f", np, op,excess)
		if np < self.pg_min:
			logger.info("P_MIN: %.0f < %.0f", np, self.pg_min)
			np = self.pg_min
		elif np > self.pg_max:
			logger.info("P_MAX: %.0f > %.0f", np, self.pg_max)
			np = self.pg_max

		if phase is None and self.n_phase > 1:
			return self.to_phases(np)
		ps = [0] * self.n_phase
		ps[0 if phase is None else phase-1] = np
		return ps


	def to_phases(self, p):
		"""
		Distribute a given power to the phases so that the result is balanced,
		or at least as balanced as possible given that to feed in from one phase
		while sending energy out to another phase is a waste of energy.

		This step does not change the total, assuming the per-phase limits
		are not exceeded.
		"""

		self.load = [ -b.value for b in self.p_cons_ ]
		load_avg = sum(self.load)/self.n_phase

		ps = [ p/self.n_phase - (g-load_avg) for g in self.load ]
		ps = balance(ps, min=-self.p_per_phase, max=self.p_per_phase)
		return ps


	async def set_inv_ps(self, ps):
		# OK, we're safe, implement
		if self.op.get("fake", False):
			if self.n_phase > 1:
				logger.error("NO-OP SET inverter %.0f ∑ %s", -sum(ps), " ".join(f"{-x :.0f}" for x in ps))
			else:
				logger.error("NO-OP SET inverter %.0f", -ps[0])
			return

		if self.n_phase > 1:
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
	def __init__(self, intf, cfg):
		self.intf = intf
		self.ps = None
		self.ps_min = [-999999999] * intf.n_phase
		self.ps_max = [999999999] * intf.n_phase
		self.running = False

		for k,v in cfg.items():
			if not hasattr(self,k):
				logger.error("The parameter %r is unknown.", k)
			setattr(self,k,v)


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

		# This convoluted step thinks about inverter output limits.
		# Specifically, if one of them is overloaded and another is not
		# we distribute the excess to others inverters.
		#
		# Yes this may result in one grid phase going in while others
		# go out, but that could already happen anyway and should not
		# materially increase grid load differences across phases.
		if self.running and intf.n_phase > 1:
			pd_min = pd_max = 0
			d_min = d_max = 0
			ops = ps

			# First pass: determine the invertes' current operational limits.
			for i in range(intf.n_phase):
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
			if abs(pp-p) < intf.p_dampen:
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
			if isinstance(c,type) and issubclass(c,cls) and hasattr(c,'_mode'):
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
