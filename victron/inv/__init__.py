#!/usr/bin/env python3

import sys
import os
import anyio
from contextlib import asynccontextmanager, contextmanager

from victron.dbus.utils import DbusInterface, CtxObj, DbusName
from victron.dbus import Dbus
from asyncdbus.service import method
from asyncdbus import DBusError

import logging
logger = logging.getLogger(__name__)

from ._util import balance


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

	high_delta = 0.3  # min difference between current voltage and whatever the battery says is OK
	_top_off = False  # go to the battery voltage limit?
	top_delta = 0.5  # distance to max voltage when not topping off

	pg_min = -10000  # watt we may send to the grid
	pg_max = 10000  # watt we may take from the grid
	inv_eff = 0.85  # inverter's min efficiency
	p_per_phase = 3000  # inverter's max load per phase
	# TODO collect long term deltas

	pv_margin = 0.4
	# protect battery against excessive discharge if PV current should suddenly
	# fall off due to clouds. We assume that it'll not drop more than 60% during
	# any five second interval.

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
	def register(cls,num,name):
		def _reg(proc):
			cls.MODE[num] = (name, proc)
			return proc
		return _reg

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
		return -sum(x.value for x in self.p_grid_)

	@property
	def ib_max(self):
		"""
		Max battery current.
		"""
		# Remember that currents are measured from the PoV of the bus bar.
		# Thus this is the discharge current, current goes from the battery to the bus.
		return self._ib_min.value

	@property
	def ib_min(self):
		"""
		Max battery current.
		"""
		# Remember that currents are measured from the PoV of the bus bar.
		# Thus the max charge current is negative, current goes into the battery.
		return -self._ib_max.value


	async def update_vars(self):
		self.u_min = await self.intf.importer(self.s_battery.value, '/Info/BatteryLowVoltage')
		self.u_max = await self.intf.importer(self.s_battery.value, '/Info/MaxChargeVoltage')
		self._ib_max = await self.intf.importer(self.s_battery.value, '/Info/MaxChargeCurrent')
		self._ib_min = await self.intf.importer(self.s_battery.value, '/Info/MaxDischargeCurrent')
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
			else:
				self.i_pv_max += (self.i_pv-self.i_pv_max)/100
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
				await self.MODE[self._mode][1](self).run(task_status)
		finally:
			self._mode_task = None
			self._mode_task_stopped.set()

	async def _start_mode_task(self):
		await self._tg.start(self._run_mode_task)

	@classmethod
	def _mode_name(cls, path, value):
		try:
			return cls.MODE[value][0]
		except KeyError:
			return f'?_{v}'


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
		logger.debug("Want bat I: %f", i)
		ii = max(self.ib_min, min(i, self.ib_max))
		if ii != i:
			logger.debug("Adj: bat I: %f", ii)
		# i_pv+i_batt+i_inv == zero
		return self.set_inv_i(-ii - self.i_pv)


	def calc_inv_i(self, i):
		logger.debug("Want inv I: %f", i)
		return self.calc_inv_p(self.p_from_i(i))


	def calc_grid_p(self, p, excess=None):
		"""
		Set power from/to the grid. Positive = take power.

		p_cons+p_grid+p_inv == zero by definition.
		"""
		logger.debug("Want grid P: %f", p)

		p += self.p_cons
		return self.calc_inv_p(-p, excess=None)

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

	def calc_inv_p(self, p, excess=None):
		"""
		Calculate inverter/charger power
		"""
		logger.debug("WANT inv P: %f", p)
		op = p
		inv_i = self.i_from_p(p, rev=True)

		# if we're close to the top, slow down / stop early
		if not self._top_off:
			# current we'd usually pull with given P

			# This varies min battery current from +C/20 to -C/20 depending on how close we are to the top
			i_dis = self.b_cap/20 * ((self.top_delta-(self.u_max.value-self.u_dc)) / self.top_delta)
			i_max = -min(self.ib_max, i_dis+self.i_pv)

			# if we're trying to take less from the battery than required, pull more
			if inv_i > i_max:
				p = self.p_from_i(i_max)

		# The grid may impose power limits
		p = max(self.pg_min, min(p, self.pg_max))

		# Check against max charge/discharge.
		# For discharging, consider the min PV value when clouds obscure the sun.
		i_pv = min(self.i_pv_max * self.pv_margin, self.i_pv)
		i_inv = self.i_from_p(p, rev=True)
		i_batt = -i_inv - i_pv
		if i_batt < self.ib_min:
			# charge
			breakpoint()
			p = self.p_from_i(-self.ib_min - i_pv, rev=True)
		elif -i_inv-self.i_pv_max > self.ib_max:
			# discharge
			p = self.p_from_i(-self.i_max-self.i_pv_max, rev=True)
			breakpoint()

		if excess is not None and p-op > excess:
			p = op+excess
		# Apply consumption offsets. The goal is to never feed in from one phase
		# while feeding out from another phase.
		# This step must not change the total.

		self.load = [ b.value for b in self.p_cons_ ]
		load_avg = sum(self.load)/self.n_phase

		ps = [ -p/self.n_phase - (g-load_avg) for g in self.load ]
		ps = balance(ps, min=-self.p_per_phase, max=self.p_per_phase)
		# TODO consider operational limits of the inverters:
		# the result may be too large for us to handle
		return ps


	async def set_inv_ps(self, ps):
		# OK, we're safe, implement
		logger.info("SET inverter %s", " ".join(f"{x :.0f}" for x in ps))
		for p,v in zip(self.p_set_, ps):
			await p.set_value(v)

			
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
		self.ps_min = [-999999999] * intf.n_phase
		self.ps_max = [999999999] * intf.n_phase
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

		# This convoluted step thinks about inverter output limits.
		# Specifically, if one of them is overloaded and another is not
		# we distribute the excess to others inverters.
		#
		# Yes this may result in one grid phase going in while others
		# go out, but that could already happen anyway and should not
		# materially increase grid load differences across phases.
		if not self.running and intf.n_phase > 1:
			pd_min = 0
			pd_max = 0
			logger.debug("START %s",ps)
			for i in range(intf.n_phase):
				p = ps[i]
				p_set = intf.p_set_[i].value
				p_cur = intf.p_cur_[i].value
				p_run = intf.p_run_[i].value
				p_cons = -intf.p_cons_[i].value
				p_min = self.ps_min[i]
				p_max = self.ps_max[i]
				logger.debug("%.0f %.0f %.0f %.0f %.0f %.0f", p_set,p_cur,p_run,p_cons,p_min,p_max)

				if p_set < 0 and p_run < 0:
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

				elif p_set > 0 and p_run > 0: # same in reverse for p_max
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

			# Second pass. Distribute pd_min/pd_max to lower-powered phases.
			pa = [(i,x) for i,x in enumerate(ps)]
			if pd_min > 0:
				pa.sort(key=lambda x: -ps[x[0]] + self.ps_min[x[0]])
				logger.debug("MIN Pre %s", pa)
				pb = []
				d = 0
				while pa:
					i,v = pa.pop()
					p_run = intf.p_run_[i].value
					p_min = self.ps_min[i]
					if v < p_min:  # over the limit
						d += p_min-v
						v = p_min-50
					else:
						pp = d/(len(pa)+1)
						if v-pp < p_min: # goes to min
							d -= v-p_min
							v = p_min-50
						else: # can use pp
							d -= pp+50
							v -= pp+50
					pb.append((i,v))
				pa = pb

			if pd_max > 0:
				breakpoint()
				pa.sort(key=lambda x: ps[x[0]] - self.ps_max[x[0]])
				logger.debug("MAX Pre %s", pa)
				pb = []
				d = 0
				while pa:
					i,v = pa.pop()
					p_run = intf.p_run_[i].value
					p_max = self.ps_max[i]
					if v > p_max:  # over the limit
						v = p_max+50
					else:
						pp = d/(len(pa)+1)
						if v+pp > p_max: # goes to max
							d -= p_max-v
							v = p_max+50
						else: # can use pp
							d -= pp+50
							v += pp+50
					pb.append((i,v))
				pa = pb
				breakpoint()
			ps = [ x[1] for x in sorted(pa, key=lambda x:x[0]) ]
			logger.debug("END %s",ps)

		await intf.set_inv_ps(ps)

		p = intf.p_inv
		await intf.trigger()
		n=0
		while n<10:
			await intf.trigger()
			pp = intf.p_inv
			logger.debug("now %s", pp)
			if abs(pp-p) < 50:
				break
			p=pp
			n += 1
		self.running = True

@InvControl.register(0,"off")
class InvMode_None(InvModeBase):
	"Set the AC output to zero, then do nothing."
	async def run(self, task_status):
		intf = self.intf

		logger.info("SET inverter ZERO")
		for p in intf.p_set_:
			await p.set_value(0)
		task_status.started()
		while True:
			await anyio.sleep(99999)


@InvControl.register(1,"idle")
class InvMode_Idle(InvModeBase):
	"Continuously set AC output to zero."
	async def run(self, task_status):
		intf = self.intf

		logger.info("SET inverter IDLE")
		while True:
			for p in intf.p_set_:
				await p.set_value(0)
			if task_status is not None:
				task_status.started()
				task_status = None
			await anyio.sleep(20)


@InvControl.register(2,"GridSetpoint")
class InvMode_GridPower(InvModeBase):
	"""Set total power from/to the external grid."""

	feed_in = 0
	excess = None
	async def run(self, task_status):
		intf = self.intf
		d = self.feed_in
		while True:
			grid = intf.p_grid
			logger.debug("old %s",d)
			# d += (grid-self.feed_in)/3
			# print("new",d)
			ps = intf.calc_grid_p(d, excess=self.excess)
			await self.set_inv_ps(ps)
			if task_status is not None:
				task_status.started()
				task_status = None



@InvControl.register(3,"SetSOC")
class InvMode_SetSOC(InvModeBase):
	"""Reach a given charge level."""
	dest_soc = 90

	async def run(self, task_status):
		intf = self.intf
		eq = 0
		while True:
			soc = await intf.batt_soc
			if self.dest_soc < soc:
				await intf.set_batt_i(intf.ib_min * (soc-self.dest_soc) /3)
			elif self.dest_soc > soc:
				await intf.set_batt_i(intf.ib_max * (self.dest_soc-soc) /3)
			else:
				await intf.set_batt_i(0)

			if task_status is not None:
				task_status.started()
				task_status = None
			await intf.trigger()

