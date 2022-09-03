import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_BattCurrent"]

class InvMode_BattCurrent(InvModeBase):
	"""Set total current from/to the battery."""
	_name = "i_batt"

	@property
	def current(self):
		return self.intf.op.get("current", 0)

	_doc = dict(
		current="Current to take from(+) / send to(-) the battery",
		_l="""\
This module strives to hold the battery current constant.

It basically sets AC output to the difference between PV input
and your intended battery current.

TODO: The Victron controller is not told about your current cap.
Thus if the solar array supplies more than the inverter can (or is
allowed to) feed to the AC side, the battery will get more
than you specify.
""",
	)

	async def run(self):
		intf = self.intf
		while True:
			ps = intf.calc_batt_i(self.current)
			await self.set_inv_ps(ps)
			# already calls "intf.trigger", so we don't have to

