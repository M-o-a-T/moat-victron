import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_GridPower"]

class InvMode_GridPower(InvModeBase):
	"""Set total power from/to the external grid."""
	_name = "p_grid"

	@property
	def power(self):
		return self.intf.op.get("power", 0)

	@property
	def excess(self):
		return self.intf.op.get("excess", None)

	_doc = dict(
		power="Power to take from(+) / send to(-) the grid",
		excess="Max PV power to the grid, if the battery is limited / full. -1=unlimited",
		phase="Phase to (ab)use. Default: distribute per load.",
		_l="""\
This module strives to maintain a constant flow of power from/to the grid.

It tries to balance grid phases, but it will never charge from one phase and
discharge from another. If the inverter on one phase maxes out, remaining
power gets distributed to other phases.

If power is available, the battery is charged until the voltage is 0.5V below the
current max charge voltage, as reported by the BMS.
""",
	)

	async def run(self):
		intf = self.intf
		while True:
			ps = intf.calc_grid_p(self.power, excess=self.excess)
			await self.set_inv_ps(ps)
			# already calls "intf.trigger", so we don't have to

