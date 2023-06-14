#!/usr/bin/env python3

import sys
from pprint import pprint

from victron.inv import InvControl


class V:
	def __init__(self, v):
		self.v = v
	@property
	def value(self):
		return self.v
	def __repr__(self):
		return f"V({self.v})"

class FakeInv:
	# does NOT inherit from InvControl, fakes it all

	# Fake a large battery
	b_cap=1000

	# takedown margins for top and bottom voltage
	umax_diff=0.001
	umin_diff=0.001

	# Make those numbers somewhat recognizeable when testing / debugging
	u_max=V(200)
	u_min=V(50)
	u_dc=100

	# max current to/from the battery
	ib_min=-20
	ib_max=40

	# max AC power to/from the inverter
	pg_min=-1100
	pg_max=1100
	# max power per phase
	p_per_phase=1000

	# i_pv cannot be lower than i_pv_max*pv_margin
	# because the margin is auto-adjusted downwards
	i_pv=0
	i_pv_max=0
	pv_margin=0.5
	pv_delta=10

	last_p=0
	p_dampen=999999999

	_top_off = False

	# tests are really inefficient :-P
	inv_eff = 0.25
	def i_from_p(self, *a,**kw):
		return InvControl.i_from_p(self, *a, **kw)
	def p_from_i(self, *a,**kw):
		return InvControl.p_from_i(self, *a, **kw)

	def to_phases(self, *a,**kw):
		return InvControl.to_phases(self, *a, **kw)

	def __init__(self, n_phase=1, **kw):
		self.n_phase = n_phase

		self.p_cons_ = [V(0)]*n_phase

		for k,v in kw.items():
			try:
				if len(k)>4 and k[-2] == "_":
					p = int(k[-1])-1
					getattr(self,k[:-1])[p] = V(v)
					continue
			except ValueError:
				pass
			assert hasattr(self,k)
			setattr(self,k,v)
	
def run(p, r, _calc={}, **kw):
	f = FakeInv(**kw)
	x = InvControl.calc_inv_p(f, p, **_calc)
	if x != r:
		pprint((p, kw, _calc, r, x), stream=sys.stderr)
		breakpoint()
		InvControl.calc_inv_p(f, p, **_calc)


def test_basic():
	run(n_phase=1, p=0, r=[0])
	run(n_phase=1, p=100, r=[100])
	run(n_phase=1, p=-100, r=[-100])
	run(n_phase=1, p=1000, i_pv=55, r=[750])  # i_max
	run(n_phase=1, p=1000, ib_max=100, r=[1000])
	run(n_phase=1, p=2000, ib_max=100, r=[1100])  # pg_max
	run(n_phase=1, p=-1000, r=[-500])  # ib_min
	run(n_phase=1, p=-1200, ib_min=-100, r=[-1100])
	run(n_phase=1, p=1000, ib_max=100, i_pv=50, i_pv_max=50, r=[1000])
	run(n_phase=1, p=0, i_pv=50, i_pv_max=50, r=[125])  # pv margin
	run(n_phase=1, p=125, i_pv=50, i_pv_max=50, r=[125])  # pv margin
	run(n_phase=1, p=125, i_pv=50, i_pv_max=100, r=[125])  # pv margin

	# test code uses four phases because we get exact floating-point math that way
	run(n_phase=4, p=100, r=[25,25,25,25])
	run(n_phase=4, p=100, p_cons_1=100, r=[100,0,0,0])
	run(n_phase=4, p=100, p_cons_1=50, r=[62.5,12.5,12.5,12.5])
	run(n_phase=4, p=100, p_cons_1=50, p_per_phase=46, r=[46,18,18,18])

	
