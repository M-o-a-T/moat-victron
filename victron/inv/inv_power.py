import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_InvPower"]

class InvMode_InvPower(InvModeBase):
	"""Set total power from/to the inverter."""
	_mode = 4
	_name = "p_inv"

	@property
	def power(self):
		return self.intf.op.get("power", 0)

	@property
	def excess(self):
		return self.intf.op.get("excess", None)

	@property
	def phase(self):
		return self.intf.op.get("phase", None)

	_doc = dict(
		power="Power for the inverter to send to(+) / take from(-) AC",
		excess="Additional power to send if available / battery full. -1=unlimited",
		phase="Phase to (ab)use. Default: distribute per load.",
		_l="""\
This module strives to maintain a constant flow of power through the inverter.

If 'power' is negative, the battery will be charged until its voltage is at the
current max charge voltage, as reported by the BMS.

If 'phase' is set, only this phase will be used.
""",
	)

	async def run(self):
		intf = self.intf
		while True:
			ps = intf.calc_inv_p(self.power, excess=self.excess, phase=self.phase)
			await self.set_inv_ps(ps)

