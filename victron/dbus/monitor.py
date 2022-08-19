## @package dbus.monitor
# This code takes care of the D-Bus interface (not all of below is implemented yet):
# - on startup it scans the dbus for services we know. For each known service found, it searches for
#   objects/paths we know. Everything we find is stored in items{}, and an event is registered: if a
#   value changes we'll be notified and can pass that on to our owner. For example the vrmLogger.
#
# - after startup, it continues to monitor the dbus:
#		1) when services are added we do the same check on that
#		2) when services are removed, we remove any items that we had that referred to that service
#		3) if an existing services adds paths we update ourselves as well: on init, we make a
#		   VeDbusItemImport for a non-, or not yet existing objectpaths as well1
#
# Code is used by the vrmLogger, and also the pubsub code. Both are other modules in the dbus_vrm repo.

from asyncdbus import MessageBus, BusType, MessageType, DBusError, Message
import logging
import pprint
import os
import anyio

from collections import defaultdict
from functools import partial
from contextlib import asynccontextmanager
from inspect import iscoroutine

# our own packages
from .utils import wrap_dbus_value, unwrap_dbus_value, CtxObj, call as _call

notfound = object() # For lookups where None is a valid result

ITEM_INTF = "com.victronenergy.BusItem"

logger = logging.getLogger(__name__)

class MonitoredValue:
	def __init__(self, value, text, options):
		super().__init__()
		self.value = value
		self.text = text
		self.options = options

	# For legacy code, allow treating this as a tuple/list
	def __iter__(self):
		return iter((self.value, self.text, self.options))


class Service:
	whentologoptions = {'configChange', 'onIntervalAlwaysAndOnEvent',
		'onIntervalOnlyWhenChanged', 'onIntervalAlways', 'never'}
	def __init__(self, id, serviceName, deviceInstance):
		super().__init__()
		self.id = id
		self.name = serviceName
		self.paths = {}
		self._seen = set()
		self.deviceInstance = deviceInstance

		# whentolog-accessed options
		self.configChange = []
		self.onIntervalAlwaysAndOnEvent = []
		self.onIntervalOnlyWhenChanged = []
		self.onIntervalAlways = []
		self.never = []

	# For legacy code, attributes can still be accessed as if keys from a
	# dictionary.
	def __setitem__(self, key, value):
		setattr(self, key, value)

	def __getitem__(self, key):
		try:
			return getattr(self, key)
		except AttributeError:
			raise KeyError(key) from None

	def set_seen(self, path):
		self._seen.add(path)

	def seen(self, path):
		return path in self._seen

	@property
	def service_class(self):
		return '.'.join(self.name.split('.')[:3])


class DbusMonitor(CtxObj):
	"""
	This is the main DBus monitoring class.
	Usage:
		dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
		monitorlist = {'com.victronenergy.solarcharger': {
				'/Connected': dummy,
				'/ProductName': dummy } }
		
		async with DbusMonitor(None, MonitorList) as mon:
			…
	"""

	def __init__(self, bus, dbusTree, valueChangedCallback=None, deviceAddedCallback=None,
					deviceRemovedCallback=None, vebusDeviceInstance0=False):
		# valueChangedCallback is the callback that we call when something has changed.
		# def value_changed_on_dbus(dbusServiceName, dbusPath, options, changes, deviceInstance):
		# in which `changes` is a tuple with GetText() and GetValue()
		#
		# `dbusTree` is a service>pathlist dict. The path list can be anything iterable
		# (list, set, dict (we ignore the values)).
		super().__init__()

		self.valueChangedCallback = valueChangedCallback
		self.deviceAddedCallback = deviceAddedCallback
		self.deviceRemovedCallback = deviceRemovedCallback
		self.dbusConn = bus
		self.dbusTree = dbusTree
		self.vebusDeviceInstance0 = vebusDeviceInstance0

		# Lists all tracked services. Stores name, id, device instance, value per path, and whenToLog info
		# indexed by service name (eg. com.victronenergy.settings).
		self.servicesByName = {}

		# Same values as self.servicesByName, but indexed by service id (eg. :1.30)
		self.servicesById = {}

		# Keep track of services by class to speed up calls to get_service_list
		self.servicesByClass = defaultdict(list)

		# Keep track of any additional watches placed on items
		self.serviceWatches = defaultdict(list)


	# called via CtxObj
	@asynccontextmanager
	async def _ctx(self):

		@asynccontextmanager
		async def _bus():
			if self.dbusConn is None:
				async with MessageBus(bus_type=BusType.DETECT).connect() as bus:
					try:
						self.dbusConn = bus
						yield bus
					finally:
						self.dbusConn = None
			else:
				yield self.dbusConn

		async def _list_names():
			reply = await bus.call(
				Message(
					destination='org.freedesktop.DBus',
					path='/org/freedesktop/DBus',
					interface='org.freedesktop.DBus',
					member='ListNames'))
			if reply.message_type == MessageType.ERROR:
				raise Exception(reply.body[0])

			return reply.body[0]


		async with _bus() as bus, anyio.create_task_group() as self._tg:
			# subscribe to NameOwnerChange for bus connect / disconnect events.
			bus.add_message_handler(self._dispatch)
			await bus._init_high_level_client()  # enables name change signals

			#obj = await self.dbusConn.get_proxy_object(serviceName, objectPath)
			#intf = await obj.get_interface(ITEM_INTF)
			#await intf.on_properties_changed(cb)

			try:
				logger.info('===== Search on dbus for services that we will monitor starting... =====')
				for serviceName in await _list_names():
					await self.scan_dbus_service(serviceName)

				logger.info('===== Search on dbus for services that we will monitor finished =====')

				yield self
			finally:
				self._tg = None
				bus.remove_message_handler(self._dispatch)

	async def call_bus(self, *a, args=None, **k):
		"""
		A simple dbus call wrapper
		"""
		if args is not None:
			k['args'] = [ wrap_dbus_value[v] for v in args ]
		res = await self.dbusConn.call(Message(*a, **k))
		return unwrap_dbus_value(res.body[0])

	async def _dispatch(self, msg):
		if msg._matches(
				sender='org.freedesktop.DBus',
				path='/org/freedesktop/DBus',
				interface='org.freedesktop.DBus',
				member='NameOwnerChanged'):
			return await _call(self.dbus_name_owner_changed, msg)
#
#		if msg._matches(
#				interface=ITEM_INTF,
#				member='PropertiesChanged'):
#			return await _call(self.handler_value_changes, msg, senderId=msg.sender.name, path=msg.path.str)
#
#		if msg._matches(
#				interface=ITEM_INTF,
#				member='ItemsChanged',
#				path='/'):
#			return await _call(self.handler_item_changes, msg, senderId=msg.sender.name)

	def dbus_name_owner_changed(self, msg):
		name, oldowner, newowner = msg.body
		if not name.startswith("com.victronenergy."):
			return

		self._tg.start_soon(self._process_name_owner_changed, name, oldowner, newowner)

	async def _process_name_owner_changed(self, name, oldowner, newowner):
		if newowner != '':
			# so we found some new service. Check if we can do something with it.
			newdeviceadded = await self.scan_dbus_service(name)
			if newdeviceadded:
				await call(self.deviceAddedCallback, name, self.get_device_instance(name))

		elif name in self.servicesByName:
			# it disappeared, we need to remove it.
			logger.info("%s disappeared from the dbus. Removing it from our lists", name)
			service = self.servicesByName[name]
			deviceInstance = service['deviceInstance']
			del self.servicesById[service.id]
			del self.servicesByName[name]
			for watch in self.serviceWatches[name]:
				await _call(watch)
			del self.serviceWatches[name]
			self.servicesByClass[service.service_class].remove(service)
			await _call(self.deviceRemovedCallback, name, deviceInstance)

	async def scan_dbus_service(self, serviceName):
		"""
		Scans the given dbus service to see if it contains anything interesting for us.
		If it does, add it to our list of monitored D-Bus services.
		"""

		paths = self.dbusTree.get('.'.join(serviceName.split('.')[0:3]), None)
		if paths is None:
			if serviceName[0] != ':':
				logger.debug("Ignoring service %s, not in the tree", serviceName)
			return False

		logger.info("Found: %s, scanning and storing items", serviceName)
		serviceId = await self.dbusConn.get_name_owner(serviceName)

		# we should never be notified to add a D-Bus service that we already have. If this assertion
		# raises, check process_name_owner_changed, and D-Bus workings.
		assert serviceName not in self.servicesByName
		assert serviceId not in self.servicesById

		# for vebus.ttyO1, this is workaround, since VRM Portal expects the main vebus
		# devices at instance 0. Not sure how to fix this yet.
		if serviceName == 'com.victronenergy.vebus.ttyO1' and self.vebusDeviceInstance0:
			di = 0
		elif serviceName == 'com.victronenergy.settings':
			di = 0
		elif serviceName.startswith('com.victronenergy.vecan.'):
			di = 0
		else:
			try:
				di = await self.call_bus(serviceName, '/DeviceInstance', None, 'GetValue')
			except DBusError:
				logger.info("	   %s was skipped because it has no device instance", serviceName)
				return False # Skip it

		logger.info("	   %s has device instance %s", serviceName, di)
		service = Service(serviceId, serviceName, di)

		# Hook up the signals
		obj = await self.dbusConn.get_proxy_object(serviceName, '/')
		intf = await obj.get_interface(ITEM_INTF)
		await intf.on_items_changed(partial(self.handler_item_changes, service))
		# await intf.on_properties_changed(partial(self.handler_value_changes, service))

		# Let's try to fetch everything in one go
		values = {}
		texts = {}

		values.update(await self.call_bus(serviceName, '/', None, 'GetValue'))
		try:
			texts.update(await self.call_bus(serviceName, '/', None, 'GetText'))
		except DBusError:
			pass

		for path, options in paths.items():
			# path will be the D-Bus path: '/Ac/ActiveIn/L1/V'
			# options will be a dictionary: {'code': 'V', 'whenToLog': 'onIntervalAlways'}
			# check that the whenToLog setting is set to something we expect
			assert options['whenToLog'] is None or options['whenToLog'] in Service.whentologoptions

			# Try to obtain the value we want from our bulk fetch. If we
			# cannot find it there, do an individual query.
			value = values.get(path[1:], notfound)
			if value != notfound:
				service.set_seen(path)
			text = texts.get(path[1:], notfound)
			if value is notfound or text is notfound:
				try:
					if value is notfound:
						value = (await self.call_bus(serviceName, path, None, 'GetValue'))
						service.set_seen(path)
					if text is notfound:
						text = (await self.call_bus(serviceName, path, None, 'GetText'))
				except DBusError as e:
					if e.reply.error_name in {
							'org.freedesktop.DBus.Error.ServiceUnknown',
							'org.freedesktop.DBus.Error.Disconnected',
							}:
						raise # This exception will be handled below

					# TODO org.freedesktop.DBus.Error.UnknownMethod really
					# shouldn't happen but sometimes does.
					logger.debug("%s %s does not exist (yet)", serviceName, path)
					value = None
					text = None

			service.paths[path] = MonitoredValue(value, text, options)

			if options['whenToLog']:
				service[options['whenToLog']].append(path)


		logger.debug("Finished scanning and storing items for %s", serviceName)

		# Adjust self at the end of the scan, so we don't have an incomplete set of
		# data if an exception occurs during the scan.
		self.servicesByName[serviceName] = service
		self.servicesById[serviceId] = service
		self.servicesByClass[service.service_class].append(service)

		return True

	def handler_item_changes(self, service, items):
		for path, changes in items.items():
			try:
				v = unwrap_dbus_value(changes['Value'])
			except (KeyError, TypeError):
				continue

			try:
				t = unwrap_dbus_value(changes['Text'])
			except KeyError:
				t = str(v)
			self._handler_value_changes(service, path, v, t)

#	def handler_value_changes(self, service, msg):
#		breakpoint()
#		pass # changes, path, senderId):
#		# If this properyChange does not involve a value, our work is done.
#		if 'Value' not in changes:
#			return
#
#		v = unwrap_dbus_value(changes['Value'])
#		# Some services don't send Text with their PropertiesChanged events.
#		try:
#			t = changes['Text']
#		except KeyError:
#			t = str(v)
#		self._handler_value_changes(service, path, v, t)

	def _handler_value_changes(self, service, path, value, text):
		try:
			a = service.paths[path]
		except KeyError:
			# path isn't there, which means it hasn't been scanned yet.
			return

		service.set_seen(path)

		# First update our store to the new value
		if a.value == value:
			return

		a.value = value
		a.text = text

		# And do the rest of the processing in on the mainloop
		if self.valueChangedCallback is not None:
			self._tg.start_soon(self._execute_value_changes, service.name, path, {
				'Value': value, 'Text': text}, a.options)

	async def _execute_value_changes(self, serviceName, objectPath, changes, options):
		# double check that the service still exists, as it might have
		# disappeared between scheduling-for and executing this function.
		if serviceName not in self.servicesByName:
			return

		await _call(self.valueChangedCallback, serviceName, objectPath,
			options, changes, self.get_device_instance(serviceName))

	# Gets the value for a certain servicename and path
	# The default_value is returned when:
	# 1. When the service doesn't exist.
	# 2. When the path asked for isn't being monitored.
	# 3. When the path exists, but has dbus-invalid, ie an empty byte array.
	# 4. When the path asked for is being monitored, but doesn't exist for that service.
	def get_value(self, serviceName, objectPath, default_value=None):
		service = self.servicesByName.get(serviceName, None)
		if service is None:
			return default_value

		value = service.paths.get(objectPath, None)
		if value is None or value.value is None:
			return default_value

		return value.value

	# returns if a dbus exists now, by doing a blocking dbus call.
	# Typically seen will be sufficient and doesn't need access to the dbus.
	async def exists(self, serviceName, objectPath):
		try:
			await self.call_bus(serviceName, objectPath, None, 'GetValue')
			return True
		except DBusError as e:
			return False

	# Returns if there ever was a successful GetValue or valueChanged event.
	# Unlike get_value this return True also if the actual value is invalid.
	#
	# Note: the path might no longer exists anymore, but that doesn't happen in
	# practice. If a service really wants to reconfigure itself typically it should
	# reconnect to the dbus which causes it to be rescanned and seen will be updated.
	# If it is really needed to know if a path still exists, use exists.
	def seen(self, serviceName, objectPath):
		try:
			return self.servicesByName[serviceName].seen(objectPath)
		except KeyError:
			return False

	# Sets the value for a certain servicename and path, returns the return value of the D-Bus SetValue
	# method. If the underlying item does not exist (the service does not exist, or the objectPath was not
	# registered) the function will return -1
	async def set_value(self, serviceName, objectPath, value):
		# Check if the D-Bus object referenced by serviceName and objectPath is registered. There is no
		# necessity to do this, but it is in line with previous implementations which kept VeDbusItemImport
		# objects for registers items only.
		service = self.servicesByName.get(serviceName, None)
		if service is None:
			return -1
		if objectPath not in service.paths:
			return -1
		# We do not catch D-Bus exceptions here, because the previous implementation did not do that either.
		return await self.call_bus(serviceName, objectPath,
				   dbus_interface=ITEM_INTF,
				   method='SetValue', signature=None,
				   args=[value])

	# returns a dictionary, keys are the servicenames, value the instances
	# optionally use the classfilter to get only a certain type of services, for
	# example com.victronenergy.battery.
	def get_service_list(self, classfilter=None):
		if classfilter is None:
			return { servicename: service.deviceInstance \
				for servicename, service in self.servicesByName.items() }

		if classfilter not in self.servicesByClass:
			return {}

		return { service.name: service.deviceInstance \
			for service in self.servicesByClass[classfilter] }

	def get_device_instance(self, serviceName):
		return self.servicesByName[serviceName].deviceInstance

	# Parameter categoryfilter is to be a list, containing the categories you want (configChange,
	# onIntervalAlways, etc).
	# Returns a dictionary, keys are codes + instance, in VRM querystring format. For example vvt[0]. And
	# values are the value.
	# used only in vrmlogger
#	def get_values(self, categoryfilter, converter=None):
#
#		result = {}
#
#		for serviceName in self.servicesByName:
#			result.update(self.get_values_for_service(categoryfilter, serviceName, converter))
#
#		return result
#
##	# same as get_values above, but then for one service only
#	def get_values_for_service(self, categoryfilter, servicename, converter=None):
#		deviceInstance = self.get_device_instance(servicename)
#		result = {}
#
#		service = self.servicesByName[servicename]
#
#		for category in categoryfilter:
#
#			for path in service[category]:
#
#				value, text, options = service.paths[path]
#
#				if value is not None:
#
#					value = value if converter is None else converter.convert(path, options['code'], value, text)
#
#					precision = options.get('precision')
#					if precision:
#						value = round(value, precision)
#
#					result[options['code'] + "[" + str(deviceInstance) + "]"] = value
#
#		return result
#
	async def track_value(self, serviceName, objectPath, callback, *args, **kwargs):
		"""
		A DbusMonitor can watch specific service/path combos for changes
		so that it is not fully reliant on the global handler_value_changes
		in this class. Additional watches are deleted automatically when
		the service disappears from dbus.
		"""
		cb = partial(callback, *args, **kwargs)

		def root_tracker(items):
			# Check if objectPath in dict
			try:
				v = items[objectPath]
				_v = unwrap_dbus_value(v['Value'])
			except (KeyError, TypeError):
				return # not in this dict

			try:
				t = unwrap_dbus_value(v['Text'])
			except KeyError:
				cb({'Value': _v })
			else:
				cb({'Value': _v, 'Text': t})

		# Track changes on the path, and also on root
		async def add_prop_receiver():
			try:
				obj = await self.dbusConn.get_proxy_object(serviceName, objectPath)
				intf = await obj.get_interface(ITEM_INTF)
				await intf.on_properties_changed(cb)
				return partial(intf.off_properties_changed, cb)
			except DbusError:
				return None

		async def add_root_receiver():
			obj = await self.dbusConn.get_proxy_object(serviceName, '/')
			intf = await obj.get_interface(ITEM_INTF)
			await intf.on_items_changed(root_tracker)
			return partial(intf.off_items_changed, root_tracker)

		self.serviceWatches[serviceName].extend((
			await add_prop_receiver(),
			await add_root_receiver(),
		))

