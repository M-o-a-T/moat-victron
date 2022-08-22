from . import InvModeBase

__all__ = ["InvMode_None"]

class InvMode_None(InvModeBase):
	"Set the AC output to zero, then do nothing."
	_mode = 0
	_name = "off"

	async def run(self, task_status):
		intf = self.intf

		logger.info("SET inverter ZERO")
		for p in intf.p_set_:
			await p.set_value(0)
		task_status.started()
		while True:
			await anyio.sleep(99999)

