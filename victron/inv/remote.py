import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_Remote"]

class InvMode_Remote(InvModeBase):
	"""Remote controlled inverter controller."""
	_name = "remote"

	@property
	def power(self):
		return max(0, self.intf.op.get("power", 0))

	@property
	def low_grid(self):
		return bool(self.intf.op.get("low_grid", 1))


	@property
	def mode(self):
		return self.intf.op.get("mode", 3)

	@mode.setter
	def mode(self, val):
		self.intf.op["mode"] = val


	@property
	def soc_low_zero(self):
		return max(5, min(self.intf.op.get("soc_low_zero", 99), self.soc_low-2))

	@property
	def soc_low(self):
		return min(max(self.intf.op.get("soc_low", 20), 10), 80)

	@property
	def soc_low_ok(self):
		return max(self.intf.op.get("soc_low_ok", 0), self.soc_low+2)

	@property
	def soc_high(self):
		return max(min(self.intf.op.get("soc_high", 90), 97), self.soc_low+10)

	@property
	def soc_high_ok(self):
		return max(min(self.intf.op.get("soc_high_ok", 85), 95), self.soc_high-2)

	_doc = dict(
		power="Max power to send to the grid",
		low_grid="Do grid zero?",
		soc_low_zero="SoC lower? stop the inverter",
		soc_low="SoC lower? start grid-only mode",
		soc_low_ok="SoC higher? end grid-only mode",
		soc_high="SoC higher? start feed-out mode",
		soc_high_ok="SoC lower? end feed-out mode",
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
""",
	)

	async def run(self):
		intf = self.intf
		while True:
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

			if self.mode == 1 or self.mode == 2 and not low_grid:
				ip = 0
			elif self.mode == 2:
				ip = min(intf.solar_p, -intf.p_cons)
			elif self.mode == 3:
				p = max(intf.solar_p+intf.p_cons, self.power)
			else:
				p = self.power

			if ip is None:
				ps = intf.calc_grid_p(-p, excess=0)
			else:
				ps = intf.calc_inv_p(ip, excess=0)

			print("P:",p," - IP:",ip, " = ", ps)
			await self.set_inv_ps(ps)
			# already calls "intf.trigger", so we don't have to

