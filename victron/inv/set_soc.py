import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_SetSOC"]

class InvMode_SetSOC(InvModeBase):
	"""Reach a given charge level."""
	_mode = 3
	_name = "soc"

	@property
	def dest_soc(self):
		return self.intf.op.get("dest_soc", .50)

	@property
	def power_in(self):
		return self.intf.op.get("power_in", 0)

	@property
	def power_out(self):
		return self.intf.op.get("power_out", 0)

	@property
	def excess(self):
		return self.intf.op.get("excess", None)

	@property
	def top_off(self):
		return self.intf.op.get("top_off", False)

	@property
	def use_grid(self):
		return self.intf.op.get("use_grid", False)

	_doc = dict(
		power_in="Power to take from the grid when charging. Positive unless relying on solar.",
		power_out="Power to send to the grid when discharging. Must be negative.",
		dest_soc="The SoC level to aim towards",
		_l="""\
This module tries to charge/discharge the battery towards a given
state of charge (SoC) percentage.

Untested.
""",
	)

	async def run(self):
		intf = self.intf
		eq = 0
		while True:
			ps = intf.calc_grid_p(self.power_in, excess=self.excess)
			await self.set_inv_ps(ps)

			soc = intf.batt_soc
			info = {"now":soc, "dest":self.dest_soc, "delta":soc-self.dest_soc}
			if abs(soc-self.dest_soc)<0.02:
				info["delta"] = 0
				ps = intf.calc_batt_i(0)
			elif self.dest_soc > soc:  # want power
				info["power"] = self.power_in
				ps = intf.calc_grid_p(self.power_in, excess=self.excess)
			else:  # send power
				info["power"] = self.power_out
				ps = intf.calc_grid_p(self.power_out, excess=self.excess)
			intf.set_state("to_soc", info)
			await self.set_inv_ps(ps)
			await intf.trigger()

