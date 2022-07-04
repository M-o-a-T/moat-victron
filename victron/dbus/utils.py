#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
from traceback import print_exc
from os import _exit as os_exit
from os import statvfs
from subprocess import check_output, CalledProcessError
import logging
import inspect
import asyncdbus.signature as dbus
logger = logging.getLogger(__name__)

VEDBUS_INVALID = dbus.Variant('ai', [])

class NoVrmPortalIdError(Exception):
	pass

__vrm_portal_id = None
def get_vrm_portal_id():
	# The original definition of the VRM Portal ID is that it is the mac
	# address of the onboard- ethernet port (eth0), stripped from its colons
	# (:) and lower case. This may however differ between platforms. On Venus
	# the task is therefore deferred to /sbin/get-unique-id so that a
	# platform specific method can be easily defined.
	#
	# If /sbin/get-unique-id does not exist, then use the ethernet address
	# of eth0. This also handles the case where velib_python is used as a
	# package install on a Raspberry Pi.
	#
	# On a Linux host where the network interface may not be eth0, you can set
	# the VRM_IFACE environment variable to the correct name.

	global __vrm_portal_id

	if __vrm_portal_id:
		return __vrm_portal_id

	portal_id = None

	# First try the method that works if we don't have a data partition. This
	# will fail when the current user is not root.
	try:
		portal_id = check_output("/sbin/get-unique-id").decode("utf-8", "ignore").strip()
		if not portal_id:
			raise NoVrmPortalIdError("get-unique-id returned blank")
		__vrm_portal_id = portal_id
		return portal_id
	except CalledProcessError:
		# get-unique-id returned non-zero
		raise NoVrmPortalIdError("get-unique-id returned non-zero")
	except OSError:
		# File doesn't exist, use fallback
		pass

	# Fall back to getting our id using a syscall. Assume we are on linux.
	# Allow the user to override what interface is used using an environment
	# variable.
	import fcntl, socket, struct, os

	iface = os.environ.get('VRM_IFACE', 'eth0').encode('ascii')
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		info = fcntl.ioctl(s.fileno(), 0x8927,  struct.pack('256s', iface[:15]))
	except IOError:
		raise NoVrmPortalIdError("ioctl failed for eth0")

	__vrm_portal_id = info[18:24].hex()
	return __vrm_portal_id


# See VE.Can registers - public.docx for definition of this conversion
#def convert_vreg_version_to_readable(version):
#	def str_to_arr(x, length):
#		a = []
#		for i in range(0, len(x), length):
#			a.append(x[i:i+length])
#		return a
#
#	x = "%x" % version
#	x = x.upper()
#
#	if len(x) == 5 or len(x) == 3 or len(x) == 1:
#		x = '0' + x
#
#	a = str_to_arr(x, 2);
#
#	# remove the first 00 if there are three bytes and it is 00
#	if len(a) == 3 and a[0] == '00':
#		a.remove(0);
#
#	# if we have two or three bytes now, and the first character is a 0, remove it
#	if len(a) >= 2 and a[0][0:1] == '0':
#		a[0] = a[0][1];
#
#	result = ''
#	for item in a:
#		result += ('.' if result != '' else '') + item
#
#
#	result = 'v' + result
#
#	return result


def get_free_space(path):
	result = -1

	try:
		s = statvfs(path)
		result = s.f_frsize * s.f_bavail     # Number of free bytes that ordinary users
	except Exception as ex:
		logger.info("Error while retrieving free space for path %s: %s" % (path, ex))

	return result


#def get_load_averages():
#	c = read_file('/proc/loadavg')
#	return c.split(' ')[:3]
#

def _get_sysfs_machine_name():
	try:
		with open('/sys/firmware/devicetree/base/model', 'r') as f:
			return f.read().rstrip('\x00')
	except IOError:
		pass

	return None

# Returns None if it cannot find a machine name. Otherwise returns the string
# containing the name
def get_machine_name():
	# First try calling the venus utility script
	try:
		return check_output("/usr/bin/product-name").strip().decode('UTF-8')
	except (CalledProcessError, OSError):
		pass

	# Fall back to sysfs
	name = _get_sysfs_machine_name()
	if name is not None:
		return name

	# Fall back to venus build machine name
	try:
		with open('/etc/venus/machine', 'r', encoding='UTF-8') as f:
			return f.read().strip()
	except IOError:
		pass

	return None


def get_product_id():
	""" Find the machine ID and return it. """

	# First try calling the venus utility script
	try:
		return check_output("/usr/bin/product-id").strip()
	except (CalledProcessError, OSError):
		pass

	# Fall back machine name mechanism
	name = _get_sysfs_machine_name()
	return {
		'Color Control GX': 'C001',
		'Venus GX': 'C002',
		'Octo GX': 'C006',
		'EasySolar-II': 'C007',
		'MultiPlus-II': 'C008'
	}.get(name, 'C003') # C003 is Generic


# Returns False if it cannot open the file. Otherwise returns its rstripped contents
#def read_file(path):
#	content = False
#
#	try:
#		with open(path, 'r') as f:
#			content = f.read().rstrip()
#	except Exception as ex:
#		logger.debug("Error while reading %s: %s" % (path, ex))
#
#	return content
#

def wrap_dbus_value(value):
	if value is None:
		return VEDBUS_INVALID
	if isinstance(value, dbus.Variant):
            # already wrapped. No, we won't dual-wrap it.
            return value
	if isinstance(value, float):
		return dbus.Variant('d', value)
	if isinstance(value, bool):
		return dbus.Variant('b', value)
	if isinstance(value, int):
		if 0 <= value < 2**8:
			return dbus.Variant('y', value)
		if -2**15 <= value < 2**15:
			return dbus.Variant('n', value)
		if 0 <= value < 2**16:
			return dbus.Variant('q', value)
		if -2**31 <= value < 2**31:
			return dbus.Variant('i', value)
		if 0 <= value < 2**32:
			return dbus.Variant('u', value)
		if -2**63 <= value < 2**63:
			return dbus.Variant('x', value)
		if 0 <= value < 2**64:
			return dbus.Variant('t', value)

		raise OverflowError(value)

	if isinstance(value, str):
		return dbus.Variant('s', value)
	if isinstance(value, (bytes,bytearray)):
		return dbus.Variant('ay', value)
	if isinstance(value, list):
		if len(value) == 0:
			# If the list is empty we cannot infer the type of the contents. So assume unsigned integer.
			# A (signed) integer is dangerous, because an empty list of signed integers is used to encode
			# an invalid value.
			return dbus.Variant('au', [])
		return dbus.Variant('av', [wrap_dbus_value(x) for x in value])
	if isinstance(value, dict):
		# Wrapping the keys of the dictionary causes D-Bus errors like:
		# 'arguments to dbus_message_iter_open_container() were incorrect,
		# assertion "(type == DBUS_TYPE_ARRAY && contained_signature &&
		# *contained_signature == DBUS_DICT_ENTRY_BEGIN_CHAR) || (contained_signature == NULL ||
		# _dbus_check_is_valid_signature (contained_signature))" failed in file ...'
		return dbus.Variant('a{sv}', {k: wrap_dbus_value(v) for k, v in value.items()})
	raise ValueError("No idea how to encode %r (%s)" % (value,type(value).__name__))


dbus_int_types = (dbus.Int32, dbus.UInt32, dbus.Byte, dbus.Int16, dbus.UInt16, dbus.UInt32, dbus.Int64, dbus.UInt64)


def unwrap_dbus_value(val):
	"""Converts D-Bus values back to the original type. For example if val is of type DBus.Double,
	a float will be returned."""
	breakpoint()
	if isinstance(val, dbus_int_types):
		return int(val)
	if isinstance(val, dbus.Double):
		return float(val)
	if isinstance(val, dbus.Array):
		v = [unwrap_dbus_value(x) for x in val]
		return None if len(v) == 0 else v
	if isinstance(val, (dbus.Signature, dbus.String)):
		return str(val)
	# Python has no byte type, so we convert to an integer.
	if isinstance(val, dbus.Byte):
		return int(val)
	if isinstance(val, dbus.ByteArray):
		return bytes(val)
	if isinstance(val, (list, tuple)):
		return [unwrap_dbus_value(x) for x in val]
	if isinstance(val, (dbus.Dictionary, dict)):
		# Do not unwrap the keys, see comment in wrap_dbus_value
		return dict([(x, unwrap_dbus_value(y)) for x, y in val.items()])
	if isinstance(val, dbus.Boolean):
		return bool(val)
	return val


class CtxObj:
	"""
	Add an async context manager that calls `_ctx` to run the context.

	Usage::
		class Foo(CtxObj):
			@asynccontextmanager
			async def _ctx(self):
				yield self # or whatever

		async with Foo() as self_or_whatever:
			pass
	"""

	async def __aenter__(self):
		self.__ctx = ctx = self._ctx()  # pylint: disable=E1101,W0201
		return await ctx.__aenter__()

	def __aexit__(self, *tb):
		return self.__ctx.__aexit__(*tb)


async def call(p, *a, **k):
	"""\
		Call a possibly-null, possibly-async procedure with the given arguments.
		
		If the procedure is None, return None.
		Otherwise if the result is a coroutine, resolve it.
		Then return the result.
		"""
	if p is None:
		return None
	res = p(*a, **k)
	if inspect.iscoroutine(res):
		res = await res
	return res
