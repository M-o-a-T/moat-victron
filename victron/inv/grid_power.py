from . import InvModeBase

__all__ = ["InvMode_GridPower"]

class InvMode_GridPower(InvModeBase):
	"""Set total power from/to the external grid."""

	_mode = 2
	_name = "gridsetpoint"

	feed_in = -30
	excess = None

	async def run(self, task_status):
		intf = self.intf
		while True:
			ps = intf.calc_grid_p(self.feed_in, excess=self.excess)
			await self.set_inv_ps(ps)

			if task_status is not None:
				task_status.started()
				task_status = None


