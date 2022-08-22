from . import InvModeBase

__all__ = ["InvMode_Idle"]

class InvMode_Idle(InvModeBase):
	"Continuously set AC output to zero."
	_mode = 1
	_name = "idle"

	power = 0

	_doc = dict(
		_l="""\
This module continually resets the inverter power to a specific value,
defaulting to zero.

This module does not care about battery limits! Specifically, it may
discharge the battery below the boundary set by the BMS.

The power level is from the point of view of the AC side, i.e.
positive = inverter, negaive = charger.
""",
	)

	async def run(self, task_status):
		intf = self.intf

		logger.info("SET inverter IDLE %.0f", self.power)
		while True:
			for p in intf.p_set_:
				await p.set_value(-self.power/intf.n_phase)
			if task_status is not None:
				task_status.started()
				task_status = None
			await anyio.sleep(20)

