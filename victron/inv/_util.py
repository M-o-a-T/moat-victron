def balance(a):
	"""
	Takes an array. If there are positive and negative values in it, subtract the
	lower sum from the higher part so that the delta stays the same if possible.

	>>> assert balance(f(50, 10, 100)) == (50, 10, 100)
	>>> assert balance(f(50, -10, 100)) == [45.0, 0, 95.0]
	>>> assert balance(f(-50, -10, 100)) == [0, 0, 40.0]
	>>> assert balance(f(50, -10, -100)) == [0, 0, -60.0]
	>>> assert balance(f(-50, -10, -100)) == (-50, -10, -100)

	"""
	sl=sum(-x for x in a if x<0)
	sh=sum(x for x in a if x>0)
	if sl==0 or sh==0:
		return a

	rev = sl>sh
	if rev:
		a=[-x for x in a]
		d=sh
	else:
		d=sl

	# sort
	a = sorted(enumerate(a), key=lambda x:-x[1])

	# drop the negatives
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
		v -= rd
		ra.append((i,v))

	ra.sort(key=lambda x:x[0])
	if rev:
		return [-v for i,v in ra]
	else:
		return [v for i,v in ra]


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
