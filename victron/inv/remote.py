import anyio

from moat.util import attrdict

from . import InvModeBase

import logging
logger = logging.getLogger(__name__)

__all__ = ["InvMode_Remote"]

class InvMode_Remote(InvModeBase):
	"""Remote controlled inverter controller."""
	_name = "remote"

	@property
	def power(self):
		p = self.intf.op.get("power", 0)
		p = max(0, p)
		self.intf.op["power"] = p
		return p

	@property
	def power_ref(self):
		p = self.intf.op.get("power_ref", 0)
		p = max(0, p)
		self.intf.op["power_ref"] = p
		return p

	@property
	def power_override(self):
		ip = self.intf.op.get("power_override", None)
		if ip is not None and ip == -1:
			del self.intf.op["power_override"]
			ip = None
		return ip

	@property
	def power_low(self):
		p = self.intf.op.get("power_low", 0)
		p = max(0, p)
		self.intf.op["power_low"] = p
		return p

	@property
	def low_grid(self):
		p = self.intf.op.get("low_grid", 1)
		p = bool(p)
		self.intf.op["low_grid"] = int(p)
		return p

	@property
	def limit(self):
		p = self.intf.op.get("limit", 1)
		p = max(0,min(1,p))
		self.intf.op["limit"] = p
		return p

	@property
	def p_limit(self):
		p = self.intf.op.get("p_limit", 1)
		p = max(0,p)
		self.intf.op["p_limit"] = p
		return p

	@property
	def mode(self):
		return self.intf.op.get("mode", 3)

	@mode.setter
	def mode(self, val):
		self.intf.op["mode"] = val


	@property
	def soc_low_zero(self):
		p = self.intf.op.get("soc_low_zero", .99)
		p = max(.05, min(p, self.soc_low-.02))
		self.intf.op["soc_low_zero"] = p
		return p

	@property
	def soc_low(self):
		p = self.intf.op.get("soc_low", .20)
		p = min(max(p, .10), .90)
		self.intf.op["soc_low"] = p
		return p

	@property
	def soc_low_ok(self):
		p = self.intf.op.get("soc_low_ok", 0)
		p = min(max(p, self.soc_low+.02), self.soc_high-.02)
		self.intf.op["soc_low_ok"] = p
		return p

	@property
	def soc_high(self):
		p = self.intf.op.get("soc_high", .90)
		p = max(min(p, .97), self.soc_low+.05)
		self.intf.op["soc_high"] = p
		return p

	@property
	def soc_high_ok(self):
		p = self.intf.op.get("soc_high_ok", .85)
		p = max(min(p, .95, self.soc_high-.02), self.soc_low+.02)
		self.intf.op["soc_high_ok"] = p
		return p

	_doc = dict(
		power="Max power to send to the grid",
		power_low="Power for charging, if the battery is empty",
		power_ref="Reference power, reported to energy marketing provider",
		power_override="Inverter power. Set to -1 to disable.",
		limit="Limit factor. Must be [0…1].",
		p_limit="Absolute grid power limit. Must be >0.",
		low_grid="Do grid zero?",
		soc_low_zero="SoC lower? stop the inverter",
		soc_low="SoC lower? start grid-only mode",
		soc_low_ok="SoC higher? end grid-only mode",
		soc_high="SoC higher? start feed-out mode",
		soc_high_ok="SoC lower? end feed-out mode",
		level="Power feed-out factor, if on manual control",
		_l="""\
This module implements dynamic control.

When SoC is between @soc_low and @soc_high, the inverter supplies @power to the grid.
(@mode=0)

Above @soc_high it switches to the maximum of @power and solar output. (@mode=3)
It stops doing that when SoC drops below @soc_high_ok.

Below @soc_low the inverter switches to grid-zero if @low_grid is on, else zero.
(mode=2)
Below @soc_low_zero the grid is ignored until @soc_low is reached. (mode=1)
Normal operation is resumed when SoC is higher than @soc_low_ok.

SoC values must be between 0 and 1, though values outside the 0.10 … 0.95 range are
unlikely to work the way you want them to.

Grid power is constrained by the "limit" value, which must be between 0 and 1.
If DistKV is active, this value is the minimum of the available "limit" sub-entries.

Same for "p_limit": absolute power values >= 0, "power_limit" sub-entries.

If @power_override is set, its value controls the inverter power directly.
This is intended to shut down the system at night / in low-battery situations.
DO NOT use this setting to feed energy to the grid. In other words, the value
should not be positive. Set to -1 to delete.
""",
	)

	_limit = None

	async def run(self):
		self._limits = {}
		self._powers = {}
		intf = self.intf

		dkv = None
		try:
			async with anyio.create_task_group() as tg:
				evt_l = anyio.Event()
				evt_pl = anyio.Event()
				evt_p = anyio.Event()
				tg.start_soon(self._dkv_mon_l, evt_l)
				tg.start_soon(self._dkv_mon_pl, evt_pl)
				tg.start_soon(self._dkv_mon_p, evt_p)
				await evt_l.wait()
				await evt_pl.wait()
				await evt_p.wait()
				tg.start_soon(self._run)

				dkv = await intf.distkv
				if dkv:
					await dkv.set(intf.distkv_prefix/"solar"/"online", True, idem=True)
		finally:
			if dkv:
				await dkv.set(intf.distkv_prefix/"solar"/"online", False, idem=True)


	async def _dkv_mon_l(self, evt):
		lims = self._limits
		p_lims = self._p_limits
		intf = self.intf
		dkv = await intf.distkv
		if dkv is None:
			evt.set()
			return
		async with dkv.watch(intf.distkv_prefix / "solar" / "limit", fetch=True) as mon:
			async for msg in mon:
				if "state" in msg:
					if msg.state == "uptodate":
						evt.set()
						# got them all
						self._limit = min(lims.values(), default=1)
						self._p_limit = min(p_lims.values(), default=None)
				else:
					lim = msg.get("value", None)
					p = msg.path[-1]
					logger.info("LIM %s: %f", p, lim)
					if lim is None:
						lims.pop(p, None)
					else:
						lims[p] = lim
					if self._limit is not None:
						self._limit = min(lims.values(), default=1)
						logger.info("LIM: %f", self._limit)
					if self._p_limit is not None:
						self._p_limit = min(p_lims.values(), default=1)
						logger.info("LIM: %f", self._p_limit)

	async def _dkv_mon_pl(self, evt):
		lims = self._p_limits
		intf = self.intf
		dkv = await intf.distkv
		if dkv is None:
			evt.set()
			return
		async with dkv.watch(intf.distkv_prefix / "solar" / "power_limit", fetch=True) as mon:
			async for msg in mon:
				if "state" in msg:
					if msg.state == "uptodate":
						evt.set()
						# got them all
						self._p_limit = min(lims.values(), default=None)
				else:
					lim = msg.get("value", None)
					p = msg.path[-1]
					logger.info("P_LIM %s: %f", p, lim)
					if lim is None:
						lims.pop(p, None)
					else:
						lims[p] = lim
					if self._p_limit is not None:
						self._p_limit = min(lims.values(), default=1)
						logger.info("P_LIM: %f", self._p_limit)

	async def _dkv_mon_p(self, evt):
		pows = self._powers
		intf = self.intf
		dkv = await intf.distkv
		if dkv is None:
			evt.set()
			return
		async with dkv.watch(intf.distkv_prefix / "solar" / "power", fetch=True) as mon:
			async for msg in mon:
				if "state" in msg:
					if msg.state == "uptodate":
						evt.set()
						# got them all
						self._power = min(pows.values(), default=0)
				else:
					val = msg.get("value", None)
					p = msg.path[-1]
					logger.info("POW %s: %f", p, val)
					if val is None:
						pows.pop(p, None)
					else:
						pows[p] = val
					self._power = min(pows.values(), default=0)
					logger.info("POW: %f", self._power)

	async def _run(self):
		intf = self.intf
		dkv = await intf.distkv
		state = attrdict(mode=0, limits=self._limits, powers=self._powers)
		intf.set_state("remote", state)

		while True:
			if dkv is None:
				state.limits["manual"] = self._limit = self.limit
				state.p_limits["manual"] = self._p_limit = self.p_limit
				state.powers["manual"] = self._power = self.power
			else:
				if "limit" in self.intf.op:
					logger.warning("DistKV is active: manual limit is ignored!")
					del self.intf.op["limit"]
				if "p_limit" in self.intf.op:
					logger.warning("DistKV is active: manual power limit is ignored!")
					del self.intf.op["p_limit"]
				if "power" in self.intf.op:
					logger.warning("DistKV is active: power setting is ignored!")
					del self.intf.op["power"]

			p=ip=None
			soc = intf.batt_soc
			if soc <= self.soc_low_zero:
				self.mode = 1
			elif self.mode == 1 and soc >= self.soc_low:
				self.mode = 2

			if self.mode != 1 and soc <= self.soc_low:
				self.mode = 2
			elif self.mode in (1,2) and soc >= self.soc_low_ok:
				self.mode = 0

			if soc >= self.soc_high:
				self.mode = 3
			elif self.mode == 3 and soc <= self.soc_high_ok:
				self.mode = 0

			if self.mode == 1 or self.mode == 2 and not self.low_grid:
				ip = -self.power_low
			elif self.mode == 2:
				ip = min(intf.solar_p, -intf.p_cons)
			elif self.mode == 3:
				p = max(intf.solar_p+intf.p_cons, self._power)
			else:
				p = self._power
			state.mode = self.mode
			state.p_want = p
			state.ip = ip

			if dkv:
				await dkv.set(intf.distkv_prefix/"solar"/"cur", max(0,-intf.p_grid))
				await dkv.set(intf.distkv_prefix/"solar"/"ref", self.power_ref, idem=True)
			ipn = self.power_override
			if ipn is not None:
				ip = ipn
			if ip is None:
				if dkv:
					await dkv.set(intf.distkv_prefix/"solar"/"max", p, idem=True)
				exc = self.power_ref-p
				if self._limit is not None:
					p *= self._limit
					exc *= self._limit
				if self._p_limit is not None:
					if p > self._p_limit:
						p = self._p_limit
						exc = 0
					else:
						exc = self._p_limit - p

				state.p_real = p
				state.exc = exc
				try:
					del state.p_i
				except AttributeError:
					pass
				ps = intf.calc_grid_p(-p, excess=exc)
			else:
				state.p_i = ip
				try:
					del state.p_real
				except AttributeError:
					pass
				try:
					del state.exc
				except AttributeError:
					pass
				if dkv:
					await dkv.set(intf.distkv_prefix/"solar"/"max", 0, idem=True)
				ps = intf.calc_inv_p(ip, excess=0)

			logger.debug("P: %s - IP: %s = %s", p, ip, ps)
			await self.set_inv_ps(ps)
			# already calls "intf.trigger", so we don't have to

