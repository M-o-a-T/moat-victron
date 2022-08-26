import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_InvPower"]

class InvMode_InvPower(InvModeBase):
	"""Set total power from/to the inverter."""
	_mode = 4
	_name = "invsetpoint"

	feed_in = 0
	excess = None
	phase = None

	_doc = dict(
		feed_in="Power for the inverter to take from(+) / send to(-) AC",
		excess="Additional power to send if available / battery full. -1=unlimited",
                phase="Phase to (ab)use. Default: distribute per load.",
		_l="""\
This module strives to maintain a constant flow of power through the inverter.

If the feed is positive, the battery is charged until the voltage is 0.5V below the
current max charge voltage, as reported by the BMS.

If 'phase' is set, only this phase will be used.
""",
	)

	async def run(self, task_status):
		intf = self.intf
		while True:
			ps = intf.calc_inv_p(-self.feed_in, excess=self.excess, phase=self.phase)
			await self.set_inv_ps(ps)

			if task_status is not None:
				task_status.started()
				task_status = None


