def balance(a, min=None, max=None):
	"""
	Takes an array. If there are positive *and* negative values in it, add the
	negative to the positive values so that the deltas between values are constant.

	Then, if some values exceed the min or max, cap it and distribute the delta.

	See `test/test_balance` for examples.
	"""
	if not a:
		return a

	sl=sum(-x for x in a if x<0)
	sh=sum(x for x in a if x>0)

	rev = sl>sh
	if rev:
		a=[-x for x in a]
		d=sh
	else:
		d=sl

	# sort
	a = sorted(enumerate(a), key=lambda x:-x[1])

	# drop the negatives, if there are any
	if a[-1][1] >= 0:
		ra = a
		ra.reverse()
	else:
		ra = []
		while a:
			i,v = a.pop()
			if v<=0:
				ra.append((i,0))
				continue
			rd = d/(len(a)+1)
			if rd >= v:
				d -= v
				ra.append((i,0))
				continue
			ra.append((i,v-rd))
			d -= rd

	m = min if rev else max
	if m is None:
		a=ra
	else:
		if not isinstance(m, (tuple,list)):
			m = [m]*len(ra)
		a=[]
		d = 0
		while ra:
			i,v = ra.pop()
			rd = d/(len(ra)+1)
			mi = -m[i] if rev else m[i]
			if v+rd>mi:
				d += v-mi
				a.append((i,mi))
				continue
			a.append((i,v+rd))
			d -= rd

	a.sort(key=lambda x:x[0])
	if rev:
		return [-v for i,v in a]
	else:
		return [v for i,v in a]


class async_init:
	"""Inheriting this class allows you to define an async __init__.

	So you can create objects by doing something like `await MyClass(params)`
	"""
	async def __new__(cls, *a, **kw):
		instance = super().__new__(cls)
		await instance.__init__(*a, **kw)
		return instance

	async def __init__(self):
		pass
