import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_GridPower"]

class InvMode_GridPower(InvModeBase):
	"""Set total power from/to the external grid."""
	_mode = 2
	_name = "gridsetpoint"

	@property
	def feed_in(self):
		return self.intf.op.get("feed_in", 0)

	@property
	def excess(self):
		return self.intf.op.get("excess", None)

	_doc = dict(
		feedin="Power to take from(+) / send to(-) the grid",
		excess="Additional power to feed to the grid if available / battery full. -1=unlimited",
                phase="Phase to (ab)use. Default: distribute per load.",
		_l="""\
This module strives to maintain a constant flow of power from/to the grid.

It tries to balance grid phases, but it will never charge from one phase and
feed from another. If the inverter on one phase maxes out, it will distribute
power to other phases.

If power is available, the battery is charged until the voltage is 0.5V below the
current max charge voltage, as reported by the BMS.
""",
	)

	async def run(self, task_status):
		intf = self.intf
		while True:
			ps = intf.calc_grid_p(self.feed_in, excess=self.excess)
			await self.set_inv_ps(ps)

			if task_status is not None:
				task_status.started()
				task_status = None


