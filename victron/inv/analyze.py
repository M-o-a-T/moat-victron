import anyio
import logging
logger = logging.getLogger(__name__)

from . import InvModeBase

__all__ = ["InvMode_Analyze"]

class InvMode_Analyze(InvModeBase):
	"""Analyze your battery."""
	_mode = 5
	_name = "analyze"

	e_dis = None
	e_chg = None
	e_dis_c = 0
	e_chg_d = 0

	@property
	def p_chg(self):
		return self.intf.op.get("p_chg", 0)

	@property
	def p_dis(self):
		return self.intf.op.get("p_dis", 0)

	@property
	def excess(self):
		return self.intf.op.get("excess", None)

	@property
	def t_balance(self):
		return self.intf.op.get("balance", 0)

	@property
	def use_grid(self):
		return self.intf.op.get("use_grid", False)

	@property
	def skip(self):
		return self.intf.op.get("skip", 0)

	@property
	def e_dis_in(self):
		return self.intf.op.get("e_dis", 0)

	@property
	def e_chg_in(self):
		return self.intf.op.get("e_chg", 0)

	_doc = dict(
		p_chg="Power when charging.",
		p_dis="Power when discharging.",
		excess="Additional power to power to the grid if available / battery full. -1=unlimited",
		balance="Time to hold the battery in top balancing. -1=do not balance.",
		skip="Skip the first N processing steps.",
		e_dis="Discharge energy (Ws), if step 4 is skipped.",
		e_chg="Charge energy (Ws), if step 5 is skipped.",
		use_grid="power values refer to the grid, not the inverter.",
		_l="""\
This module analyzes your battery. This happens in several steps.

* Top balance the battery: go to cell.u.ext.max and wait until all cells are "there".

  This step is optional.

* Discharge until the top cell is at (2*u.lim.max-u.ext.max), i.e. somewhat below u.lim.max.

* Charge until the top cell is at u.lim.max, as configured. Clear energy counters.

* Discharge until the "bottom" cell is at u.lim.min. Save discharge energy measurement.

* Charge back to u.lim.max. Save charge energy measurement.

* Alert the nobel prize committee if the discharge value is larger than the charge value. ;-)
  Otherwise, configure the BMS so it can report a valid SoC.

Battery charging and discharging uses the p_chg/p_dis values as inverter setpoints
(unless `use_grid` is set, in which case they're grid setpoints).

As positive values always mean "pull this many Watt from there", 'p_dis' must be
negative when 'use_gid' is on, but positive when off.
""",
	)

	async def run(self):
		intf = self.intf

		if (self.p_dis < 0) != self.use_grid:
			info=dict(
				p_chg=self.p_chg, p_dis=self.p_dis,
				error="p_dis has the wrong sign",
			)
			intf.set_state("analyze", info)
			return

		skip = intf.op.get("skip",0)

		# Step 0: top-balance the cells
		if skip:
			skip -= 1
		else:
			if self.t_balance > -1:
				await self.balance()

		# Step 1: return to sufficiently-below-top
		if skip:
			skip -= 1
		else:
			await self.to_below_top()

		# Step 2: charge to "normal" max
		if skip:
			skip -= 1
		else:
			await self.to_top(again=False)

		# Step 3: discharge to "normal" min
		if skip:
			skip -= 1
			self.e_dis = self.e_dis_in
			if not skip:
				await intf.get_bms_work(poll=True, clear=True)
		else:
			await intf.get_bms_work(poll=True, clear=True)
			await self.to_bottom()
			e = (await intf.get_bms_work(poll=True, clear=True))[0]
			i = await intf.get_bms_config()
			self.e_dis = e["dis"] #  - e["chg"] * (1-i["batt"]["cap"]["loss"])
			self.e_chg_d = e["chg"]

		# Step 4: re-charge to "normal" max
		if skip:
			skip -= 1
			self.e_chg = self.e_chg_in
			if not skip:
				await intf.get_bms_work(poll=True, clear=True)
		else:
			await self.to_top(again=True)
			e = (await intf.get_bms_work(poll=True, clear=True))[0]
			i = await intf.get_bms_config()
			self.e_chg = e["chg"]
			self.e_dis_c = e["dis"]

		# step 5: calculate loss factor and save to config
		# 
		# Solving the equation 'loss = 1 - dis / chg'
		# with dis = dis_d - chg_d * (1-loss)   (*)
		#  and chg = chg_c - dis_c / (1-loss)
		# for 'loss' yields
		loss = 1-(self.e_dis+self.e_dis_c)/(self.e_chg+self.e_chg_d +1)  # avoid div/0

		# (*) We assume that charging and discharging doesn't go smoothly.
		# Maybe there's some heavy clouds during charging by PV but we needed
		# more power. Or vice versa, we're discharging at a constant rate during
		# a rainy day but suddenly the sun comes out.
		# Thus the actual charge and discharge sum needs to be corrected w/ the
		# very loss factor we're measuring.
		#
		# dis_c == "discharge during charging". Likewise for the others.

		inf=dict(
			chg=self.e_chg, dis=self.e_dis,
			chg_d=self.e_chg_d, dis_c=self.e_dis_c,
			loss=loss
		)
		if loss < 0:
			inf["test"]="chg>dis"
		elif skip:
			inf["done"]=True
			inf["error"]="Skipped"
			logger.warning("Capacity not set: %s", intf)
		else:
			inf["done"]=True
			await intf.set_bms_capacity(0, self.e_dis, loss, True)
		if skip:
			skip -= 1

		# Step 6: Done. Choose a long-term algorithm to continue
		if skip:
			await intf.change_mode("p_off")
		else:
			await intf.change_mode("p_grid" if self.use_grid else "p_inv", {"power":0, "excess":self.excess})

	
	async def balance(self):
		intf = self.intf
		intf.top_off = True

		try:
			n = 0
			while True:
				cfg = await intf.get_bms_config()
				cfg_u = cfg["cell"]["u"]
				cfg_bal = cfg["cell"]["balance"]
				umin = (cfg_u["lim"]["max"]+cfg_u["ext"]["max"])/2

				vt = (await intf.get_bms_voltages())[0]
				info = dict(step="balance", min=vt["min_cell"], max=vt["max_cell"],
						umax=cfg_u["ext"]["max"], umin=umin, dest_d=3*cfg_bal["d"])
				if vt["min_cell"] < umin:
					info["wait"]="min>umin"
					n = 0
				elif vt["max_cell"]-vt["min_cell"] < cfg_bal["d"]*3:
					info["wait"]="max-min>dest_d"
					n = 0
				else:
					n += 1

				info["power"] = power = self.p_chg
				intf.set_state("analyze", info)
				if n>3:
					break
				await self.set_p(power)

			t = anyio.current_time() + self.t_balance
			info["step"] = "balance_hold"
			info["wait"] = "timer"
			while True:
				info["t"] = t2 = t-anyio.current_time()
				intf.set_state("analyze", info)
				await self.set_p(power)
				if t2 < 0:
					break

		finally:
			intf.top_off = True


	async def to_below_top(self):
		"""
		Send power until the top cell is below umin.

		u.lim.max-umin = u.ext.max-u.lim.max, thus
		umin = 2*u.lim.max-u.ext.max, thus
		"""
		intf = self.intf

		n = 0
		while True:
			# TODO use a signal to capture this part
			cfg = await intf.get_bms_config()
			cfg_u = cfg["cell"]["u"]
			# as far below u.lim.max as u.lim.max is below u.ext.max
			umin = (2*cfg_u["lim"]["max"]-cfg_u["ext"]["max"])

			vt = (await intf.get_bms_voltages())[0]
			info = dict(step="below_top", min=vt["min_cell"], max=vt["max_cell"], umin=umin)
			if vt["max_cell"] < umin:
				info["done"] = "OK"
				n += 1
			elif vt["min_cell"] < cfg["cell"]["u"]["ext"]["min"]:
				info["done"] = "LOW"
				n += 1
			else:
				n = 0

			info["power"] = power = self.p_dis
			intf.set_state("analyze", info)
			if n > 3:
				break
			await self.set_p(power)

	async def get_e(self):
		intf = self.intf
		e = (await intf.get_bms_work(poll=True))[0]
		if self.e_dis is not None:
			e.dis_now = self.e_dis
		return e

	async def to_bottom(self):
		"""
		Send power until the bottom cell is sufficiently below u.lim.min.
		"""
		intf = self.intf

		n=0
		while True:
			# TODO use a signal to capture this part
			cfg = await intf.get_bms_config()
			cfg_u = cfg["cell"]["u"]
			# somewhat above u_lim_min
			umin = cfg_u["lim"]["min"]+(cfg_u["lim"]["min"]-cfg_u["ext"]["min"])/3

			vt = (await intf.get_bms_voltages())[0]
			e = await self.get_e()
			info = dict(
				step="discharge",
				min=vt["min_cell"], max=vt["max_cell"], e=e,
				umin=umin,
			)
			if vt["min_cell"] < umin:
				info["done"] = "OK"
				n += 1
			else:
				n = 0

			info["wait"] = "min<umin"
			info["power"] = power = self.p_dis
			intf.set_state("analyze", info)
			if n>3:
				break
			await self.set_p(power)


	async def to_top(self, again=False):
		"""
		Take power until the bottom cell is somewhat below u.lim.max.
		"""
		intf = self.intf

		n=0
		while True:
			# TODO use a signal to capture this part
			cfg = await intf.get_bms_config()
			cfg_u = cfg["cell"]["u"]
			# somewhat below u_lim_max
			umax = cfg_u["lim"]["max"]-(cfg_u["ext"]["max"]-cfg_u["lim"]["max"])/3

			vt = (await intf.get_bms_voltages())[0]
			e = await self.get_e()
			info = dict(
				step="recharge" if again else "charge",
				min=vt["min_cell"], max=vt["max_cell"], e=e,
				umax=umax,
			)
			if vt["min_cell"] > umax:
				info["done"] = "OK"
				n += 1
			else:
				n = 0

			info["wait"] = "min>umax"
			info["power"] = power = self.p_chg
			intf.set_state("analyze", info)
			if n>3:
				break
			await self.set_p(power)


	async def set_p(self, p):
		if self.use_grid:
			ps = self.intf.calc_grid_p(p, excess=self.excess)
		else:
			ps = self.intf.calc_inv_p(p, excess=self.excess)
		await self.set_inv_ps(ps)

