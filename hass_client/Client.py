# -*- coding: utf-8 -*-

import json
import threading

# pylint: disable=no-name-in-module,import-error
from base import \
	Application, \
	Plugin, \
	Settings, \
	configuration, \
	ConfigurationManager, \
	ConfigurationNumber, \
	ConfigurationString, \
	implements, \
	ISignalObserver, \
	slot
import paho.mqtt.client as mqtt
import logging
from board import Board
from telldus import DeviceManager, Device
import netifaces
# pylint: enable=no-name-in-module,import-error

__name__ = 'HASSMQTT'  # pylint: disable=W0622

ScaleConverter = {
	Device.WATT: {
		1: 'kVAh', #Device.SCALE_POWER_KVAH
		Device.SCALE_POWER_KWH: 'kWh',
		Device.SCALE_POWER_WATT: 'W',
		4: 'V', #Device.SCALE_POWER_VOLT
		5: 'A', #Device.SCALE_POWER_AMPERE
		6: 'PF' #Device.SCALE_POWER_POWERFACTOR
	},
	Device.TEMPERATURE: {
		Device.SCALE_TEMPERATURE_CELCIUS: u'°C',
		Device.SCALE_TEMPERATURE_FAHRENHEIT: u'°F'
	},
	Device.HUMIDITY: {
		Device.SCALE_HUMIDITY_PERCENT: '%'
	},
	Device.RAINRATE: {
		Device.SCALE_RAINRATE_MMH: 'mm/h'
	},
	Device.RAINTOTAL: {
		Device.SCALE_RAINTOTAL_MM: 'mm'
	},
	Device.WINDDIRECTION: {
		0: ''
	},
	Device.WINDAVERAGE: {
		Device.SCALE_WIND_VELOCITY_MS: 'm/s'
	},
	Device.WINDGUST: {
		Device.SCALE_WIND_VELOCITY_MS: 'm/s'
	},
	Device.LUMINANCE: {
		Device.SCALE_LUMINANCE_PERCENT: '%',
		Device.SCALE_LUMINANCE_LUX: 'lux'
	},
	Device.BAROMETRIC_PRESSURE: {
		Device.SCALE_BAROMETRIC_PRESSURE_KPA: 'kPa'
	}
}

ClassConverter = {
	Device.TEMPERATURE: 'temperature',
	Device.HUMIDITY: 'humidity',
	Device.BAROMETRIC_PRESSURE: 'pressure',
	Device.LUMINANCE: 'illuminance'
}

def getMacAddr(compact = True):
	addrs = netifaces.ifaddresses(Board.networkInterface())
	try:
		mac = addrs[netifaces.AF_LINK][0]['addr']
	except (IndexError, KeyError):
		return ''
	return mac.upper().replace(':', '') if compact else mac.upper()

@configuration(
	username = ConfigurationString(
		defaultValue='',
		title='MQTT Username',
		description='Username'
	),
	password=ConfigurationString(
		defaultValue='',
		title='MQTT Password',
		description='Password'
	),
	hostname=ConfigurationString(
		defaultValue='',
		title='MQTT Hostname',
		description='Hostname'
	),
	port=ConfigurationNumber(
		defaultValue=1883,
		title='MQTT Port',
		description='Port'
	),
	discovery_topic=ConfigurationString(
		defaultValue='homeassistant',
		title='Autodiscovery topic',
		description='Homeassistants autodiscovery topic'
	),
	device_name=ConfigurationString(
		defaultValue='znet',
		title='Device name',
		description='Name of this device'
	),
	base_topic=ConfigurationString(
		defaultValue='telldus',
		title='Base topic',
		description='Base topic for this device'
	),
	devices_configured=ConfigurationString(
		defaultValue='',
		hidden=True,
		title='Internal, do not change',
		description='Internal, do not change. Used to store what devices has been published.'
	)
)
class Client(Plugin):
	implements(ISignalObserver)

	def __init__(self):
		self._ready = False
		self._running = True
		self._knownDevices = None
		Application().registerShutdown(self.onShutdown)
		self.client = mqtt.Client(userdata = self)
		self.client.on_disconnect = self.onDisconnect
		self.client.on_connect = self.onConnect
		self.client.on_message = self.onMessage
		if self.config('hostname'):
			Application().queue(self.connect)

	def onShutdown(self):
		self._running = False 
		self.disconnect()

	def updateConfig(self):
		self.debug('Updating config.devices_configured to : %s' % self._knownDevices)
		try:
			self.setConfig('devices_configured', self._knownDevices)
		except Exception as e:
			self.debug('updateConfig error %s' % str(e))

	def getKnownDevices(self):
		if not self._knownDevices:
			if self.config('devices_configured'):
				self._knownDevices = [tuple(x) for x in json.loads(self.config('devices_configured'))]
			else:
				self._knownDevices = []
		return self._knownDevices
	
	def setKnownDevices(self, devices):
		self._knownDevices = devices
		self.updateConfig()

	def isKnownDevice(self, type, devId, deviceId):
		devices = self.getKnownDevices()
		return (type, str(devId), str(deviceId)) in devices

	def addKnownDevice(self, type, devId, deviceId):
		devices = self.getKnownDevices()
		devices.append((type, str(devId), str(deviceId)))
		self.setKnownDevices(devices)

	def configWasUpdated(self, key, value):
		if not key in ['devices_configured']:
			self.disconnect()
			Application().queue(self.connect)

	def tearDown(self):
		try:
			for type, _, fullId in self.getKnownDevices():
				deviceTopic = self.getDeviceTopic(type, fullId)
				self.client.publish('%s/config' % deviceTopic, '', retain = True)
				self.client.publish('%s/state' % deviceTopic, '', retain = True)
		except Exception as e:
			self.debug('tearDown %s' % str(e))
		self._knownDevices = []
		self.updateConfig()
		self.disconnect()

	def disconnect(self):
		#self.client.disconnect()
		self.client.loop_stop()
		self._running = False
		self._ready = False

	def connect(self):
		username = self.config('username')
		password = self.config('password')
		base_topic = self.config('base_topic')
		device_name = self.config('device_name')
		hostname = self.config('hostname')
		port = self.config('port')

		if username != '':
			self.client.username_pw_set(username, password)
		self.client.will_set(
			'%s/%s/available' % (base_topic, device_name) if base_topic \
			else '%s/available' % device_name, 
			'offline', 
			0, 
			True
		)
		self.client.connect_async(hostname, port, keepalive=10)
		self.client.loop_start()

	def debug(self, msg):
		logging.info('HASS DBG: %s', msg)
		base_topic = self.config('base_topic')
		device_name = self.config('device_name')
		debugTopic = (
			'%s/%s/debug' % (base_topic, device_name) if base_topic \
			else '%s/debug' % device_name
		)
		self.client.publish(debugTopic, 'Thread: %s, %s' % (threading.current_thread().ident, msg))

	def getDeviceType(self, device):
		capabilities = device.methods()
		if capabilities & Device.DIM:
			return 'light'
		elif capabilities & Device.TURNON:
			return 'switch'
		elif capabilities & Device.UP:
			return 'cover'
		elif capabilities & Device.BELL:
			return 'switch'
		else:
			return 'binary_sensor'

	def getDeviceTopic(self, type, id):
		discoverTopic = self.config('discovery_topic')
		telldusName = self.config('device_name') or 'telldus'
		return '%s/%s/%s/%s' % (discoverTopic, type, telldusName, id)

	def getSensorId(self, deviceId, valueType, scale):
		return '%s_%s_%s' % (deviceId, valueType, scale)
	
	def getBatteryId(self, device):
		return '%s_%s_battery' % (getMacAddr(), device.id())

	def formatBattery(self, battery):
		if battery == Device.BATTERY_LOW:
			return 1
		elif battery == Device.BATTERY_UNKNOWN:
			return None
		elif battery == Device.BATTERY_OK:
			return 100
		else:
			return int(battery)

	def formatScale(self, type, scale):
		return ScaleConverter.get(type, {}).get(scale, '')

	def deviceState(self, device):
		try:
			state, stateValue = device.state()

			deviceType = self.getDeviceType(device)
			if not deviceType:
				return

			self.debug('deviceState %s, state: %s, value: %s' % (device.id(), state, stateValue))

			stateTopic = '%s/state' % self.getDeviceTopic(deviceType, device.id())
			payload = ''

			if deviceType in ['light']:
				if state == Device.DIM:
					payload = json.dumps({
						'state': 'ON' if stateValue and int(stateValue) > 0 else 'OFF',
						'brightness': int(stateValue) if stateValue else 0
					})
				else:
					payload = json.dumps({
						'state': 'ON' if state == Device.TURNON else 'OFF',
						'brightness': (int(stateValue) if stateValue else 255) if state == Device.TURNON else 0
					})
			elif deviceType in ['switch']:
				payload = 'ON' if state in [Device.TURNON, Device.BELL] else 'OFF' 
			elif deviceType in ['binary_sensor']:
				payload = 'ON' if state in [Device.TURNON] else 'OFF' 
			elif deviceType in ['cover']:
				payload = 'OPEN' if state == Device.UP else 'CLOSED' if state == Device.DOWN else 'STOP'

			self.client.publish(stateTopic, payload, retain = True)
			if state == Device.BELL:
				self.client.publish(stateTopic, 'OFF', retain = True)
		except Exception as e:
			self.debug('deviceState exception %s' % str(e))

	def sensorState(self, device, valueType, scale):
		try:
			sensorId = self.getSensorId(device.id(), valueType, scale)
			for sensor in device.sensorValues()[valueType]:
				if sensor['scale'] == scale:
					self.debug('sensorState %s' % sensor)
					payload = { 
						'value': sensor['value'],
						'lastUpdated': sensor.get('lastUpdated')
					}
					self.client.publish(
						'%s/state' % self.getDeviceTopic('sensor', sensorId), 
						json.dumps(payload),
						retain = True
					)
		except Exception as e:
			self.debug('sensorState exception %s' % str(e))
	
	def batteryState(self, device):
		try:
			self.client.publish(
				'%s/state' % self.getDeviceTopic('sensor', self.getBatteryId(device)),
				self.formatBattery(device.battery()),
				retain = True
			)
		except Exception as e:
			self.debug('batteryState exception %s' % str(e))

	def publish_discovery(self, device, type, deviceId, config):
		base_topic = self.config('base_topic')
		device_name = self.config('device_name')
		config.update({ 
			'unique_id': '%s_%s' % (getMacAddr(), deviceId),
			'state_topic': '%s/state' % self.getDeviceTopic(type, deviceId),
			'availability_topic': (
				'%s/%s/available' % (base_topic, device_name) if base_topic \
				else '%s/available' % device_name
			),
			'device': {
				'identifiers': getMacAddr(),
				'connections': [['mac', getMacAddr(False)]],
				'manufacturer': 'Telldus Technologies',
				'model': Board.product(),
				'name': 'telldus',
				'sw_version': Board.firmwareVersion()
			} 
		})
		self.client.publish(
			'%s/config' % self.getDeviceTopic(type, deviceId), 
			json.dumps(config),
			retain = True
		)
		return (type, str(device.id()), str(deviceId))

	def remove_discovery(self, type, devId, fullId):
		deviceTopic = self.getDeviceTopic(type, fullId)
		self.debug('remove discovered device %s,%s,%s : %s' % (type, devId, fullId, deviceTopic))
		self.client.publish('%s/config' % deviceTopic, '', retain = True)
		self.client.publish('%s/state' % deviceTopic, '', retain = True)

	def discoverBattery(self, device):
		try:
			sensorConfig = {
				'name': '%s - Battery' % device.name(),
				'unit_of_measurement': '%',
				'device_class': 'battery'
			}
			return self.publish_discovery(device, 'sensor', self.getBatteryId(device), sensorConfig)
		except Exception as e:
			self.debug('discoverBattery %s' % str(e))

	def discoverSensor(self, device, valueType, scale):
		sensorId = self.getSensorId(device.id(), valueType, scale)
		try:
			sensorConfig = {
				'name': '%s %s - %s' % (
					device.name(), 
					Device.sensorTypeIntToStr(valueType), 
					self.formatScale(valueType, scale)
				),
				'value_template': '{{ value_json.value }}',
				'json_attributes_topic': '%s/state' % self.getDeviceTopic("sensor", sensorId),
				'unit_of_measurement': self.formatScale(valueType, scale),
			}
			if ClassConverter.get(valueType, None):
				sensorConfig.update({
					'device_class': ClassConverter.get(valueType, None)
				})

			sensorId = self.getSensorId(device.id(), valueType, scale)
			return self.publish_discovery(device, 'sensor', sensorId, sensorConfig)
		except Exception as e:
			self.debug('discoverSensor %s' % str(e))

	def discoverDevice(self, device):
		try:
			deviceType = self.getDeviceType(device)
			if not deviceType:
				return None	

			deviceTopic = self.getDeviceTopic(deviceType, device.id())
			deviceConfig = { 'name': device.name() }

			if deviceType in ['switch', 'light', 'cover']:
				deviceConfig.update({
					'command_topic': '%s/set' % deviceTopic
				})
			if deviceType == 'light':
				deviceConfig.update({
					'schema': 'json',
					'brightness': True
				})
			if deviceType == 'switch' and (device.methods() & Device.BELL):
				deviceConfig.update({
					'payload_on': 'BELL'
				})

			self.debug('device is device: %s' % json.dumps({
				'deviceType': deviceType,
				'deviceTopic': deviceTopic,
				'deviceConfig': deviceConfig
			}))

			return self.publish_discovery(device, deviceType, device.id(), deviceConfig)
		except Exception as e:
			self.debug('discoverDevice %s' % str(e))

	def discovery(self, device):
		result = []
		try:
			if device.battery() and device.battery() != Device.BATTERY_UNKNOWN:
				self.debug('device %s has battery' % device.id())
				self.discoverBattery(device)
				result.append(self.batteryState(device))

			if device.isSensor():
				self.debug('device %s has sensors' % device.id())
				for type, sensors in device.sensorValues().items():
					self.debug('sensortype %s has %s' % (type, sensors))
					for sensor in sensors:
						result.append(self.discoverSensor(device, type, sensor['scale']))
						self.sensorState(device, type, sensor['scale'])

			if device.isDevice():
				self.debug('device %s is a device' % device.id())
				result.append(self.discoverDevice(device))
				self.deviceState(device)
		except Exception as e:
			self.debug('discovery %s' % str(e))
		return [x for x in result if x]

	def run_discovery(self):
		self.debug('discover devices')
		try:
			# publish devices
			publishedDevices = []
			deviceManager = DeviceManager(self.context)
			devices = deviceManager.retrieveDevices()
			for device in devices:
				try:
					self.debug(json.dumps({
						'deviceId': device.id(),
						'type': self.getDeviceType(device),
						'name': device.name(),
						'isDevice': device.isDevice(),
						'isSensor': device.isSensor(),
						'methods': device.methods(),
						'battery': device.battery(),
						'parameters': device.allParameters() if hasattr(device, 'allParameters') else device.parameters(),
						'typeStr': device.typeString(),
						'sensors': device.sensorValues(),
						'state': device.state()
					}))
					publishedDevices.extend(self.discovery(device))
				except Exception as e:
					self.debug('run_discovery device exception %s' % str(e))

			for type, devId, fullId in list(set(self.getKnownDevices()) - set(publishedDevices)):
				self.remove_discovery(type, devId, fullId)

			self.setKnownDevices(publishedDevices)
		except Exception as e:
			self.debug('run_discovery exception %s' % str(e))

	def onConnect(self, client, userdata, flags, result):
		base_topic = userdata.config('base_topic')
		device_name = userdata.config('device_name')
		client.publish(
			'%s/%s/available' % (base_topic, device_name) if base_topic \
			else '%s/available' % device_name, 
			'online', 
			0, 
			True
		)
		try:
			userdata.run_discovery()
			#subscribe to commands
			userdata.debug('subscribing')
			client.subscribe('%s/+/%s/+/set' % (userdata.config('discovery_topic'), device_name))
			userdata._ready = True
		except Exception as e:
			userdata.debug('OnConnect error %s' % str(e))

	def onDisconnect(self, client, userdata, rc):
		self.debug("Mqtt disconnected")
		userdata._ready = False

	@slot('deviceAdded')
	def onDeviceAdded(self, device):
		if not self._running:
			return
		try:
			self.debug('Device added %s' % device.id())
			devices = self.getKnownDevices()
			devices.extend(self.discovery(device))
			self.setKnownDevices(devices)
		except Exception as e:
			self.debug('onDeviceAdded error %s' % str(e))

	@slot('deviceRemoved')
	def onDeviceRemoved(self, deviceId):
		if not self._running:
			return
		try:
			self.debug('Device removed %s' % deviceId)
			devices = self.getKnownDevices()
			for type, devId, fullId in devices:
				if devId == str(deviceId):
					self.remove_discovery(type, devId, fullId)
			devices = [x for x in devices if x[1] != str(deviceId)]
			self.setKnownDevices(devices)
		except Exception as e:
			self.debug('onDeviceRemoved error %s' % str(e))

	@slot('deviceUpdated')
	def onDeviceUpdated(self, device):
		if not self._running:
			return
		try:
			self.debug('Device updated %s' % device.id())
			devices = self.getKnownDevices()
			for type, devId, fullId in devices:
				if devId == str(device.id()):
					self.remove_discovery(type, devId, fullId)
			devices = [x for x in devices if x[1] != str(device.id())]
			devices.extend(self.discovery(device))
			self.setKnownDevices(devices)
		except Exception as e:
			self.debug('onDeviceUpdated error %s' % str(e))

	@slot('rf433RawData')
	def onRawData(self, data,*__args, **__kwargs):
		if not self._running:
			return
		self.debug(json.dumps(data))

	@slot('sensorValueUpdated')
	def onSensorValueUpdated(self, device, valueType, value, scale):
		if not self._ready or not self._running:
			return
		self.debug(json.dumps({
			'type': 'sensorValueUpdated',
			'deviceId': device.id(),
			'valueType': valueType,
			'value': value,
			'scale': scale,
			'battery': device.battery()
		}))
		sensorId = self.getSensorId(device.id(), valueType, scale)
		if not self.isKnownDevice('sensor', device.id(), sensorId):
			self.debug('A wild sensor appeared! deviceId: %s, sensorId: %s' % (device.id(), sensorId))
			type, devId, deviceId = self.discoverSensor(device, valueType, scale)
			self.addKnownDevice(type, devId, deviceId)
		self.sensorState(device, valueType, scale)
		if device.battery() and device.battery() != Device.BATTERY_UNKNOWN:
			self.batteryState(device)

	@slot('deviceStateChanged')
	def onDeviceStateChanged(self, device, state, stateValue, origin=None):
		if not self._ready or not self._running:
			return
		self.debug(json.dumps({
			'type': 'deviceStateChanged',
			'deviceId': device.id(),
			'state': state,
			'stateValue': stateValue,
			'origin': origin
		}))
		deviceType = self.getDeviceType(device)
		if not deviceType:
			return
		if not self.isKnownDevice(deviceType, device.id(), device.id()):
			self.debug('A wild device appeared! type: %s, deviceId: %s' % (deviceType, device.id()))
			type, devId, deviceId = self.discoverDevice(device)
			self.addKnownDevice(type, devId, deviceId)
		self.deviceState(device)
		if device.battery():
			self.batteryState(device)

	def onMessage(self, client, userdata, msg):
		try:
			#topic = msg.topic
			payload = msg.payload

			#topicType = topic.split('/')[-1]
			deviceManager = DeviceManager(userdata.context)
			
			device_id = int(msg.topic.split('/')[-2])
			device = deviceManager.device(device_id)
			deviceType = userdata.getDeviceType(device)
			if not deviceType:
				return

			userdata.debug(json.dumps({
				'type': 'command',
				'device_id': device_id,
				'device_type': deviceType,
				'command': payload
			}))

			if deviceType == 'light':
				payload = json.loads(payload)
				if 'brightness' in payload:
					if int(payload['brightness']) == 0:
						device.command(
							Device.TURNOFF, 
							origin = 'mqtt_hass'
						)
					else:
						device.command(
							Device.DIM, 
							value = int(payload['brightness']), 
							origin = 'mqtt_hass'
						)
				else:
					device.command(
						Device.TURNON if payload['state'].upper() == 'ON' \
						else Device.TURNOFF, 
						value = 255, 
						origin = 'mqtt_hass'
					)

			elif deviceType == 'switch':
				device.command(
					Device.TURNON if payload.upper() == 'ON' \
					else Device.BELL if payload.upper() == 'BELL' \
					else Device.TURNOFF, 
					origin = 'mqtt_hass'
				)

			elif deviceType == 'cover':
				device.command(
					Device.UP if payload.upper() == 'OPEN' \
					else Device.DOWN if payload.upper() == 'CLOSE' else \
					Device.STOP, 
					origin = 'mqtt_hass'
				)
		except Exception as e:
			userdata.debug('onMessage exception %s' % str(e))

	
