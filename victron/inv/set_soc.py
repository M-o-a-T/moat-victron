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
		return self.intf.op.get("dest_soc", 0)

	_doc = dict(
		dest_soc="The SoC level to aim towards",
		_l="""\
This module tries to charge/discharge the battery towards a given
state of charge (SoC) percentage.

Untested.
""",
	)

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

