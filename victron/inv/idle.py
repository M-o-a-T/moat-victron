from . import InvModeBase

__all__ = ["InvMode_Idle"]

class InvMode_Idle(InvModeBase):
	"Continuously set AC output to zero."
	_mode = 1
	_name = "idle"

	async def run(self, task_status):
		intf = self.intf

		logger.info("SET inverter IDLE")
		while True:
			for p in intf.p_set_:
				await p.set_value(0)
			if task_status is not None:
				task_status.started()
				task_status = None
			await anyio.sleep(20)

