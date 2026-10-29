"""Support for Xiaomi vacuums."""
import json
import logging
import asyncio
from datetime import timedelta

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.vacuum import (  # noqa: F401
    DOMAIN as ENTITY_DOMAIN,
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,  # v2022.5
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:
    from homeassistant.components.vacuum import Segment  # v2026.3
except ImportError:
    Segment = None

from . import (
    DOMAIN,
    CONF_MODEL,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    HassEntry,
    MiotEntity,
    MiotCloud,
    BaseSubEntity,
    MIOT_LOCAL_MODELS,
    async_setup_config_entry,
    bind_services_to_entries,
)
from .core.utils import DeviceException
from .core import json_map
from .core.miot_spec import (
    MiotSpec,
    MiotService,
)

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'
SCAN_INTERVAL = timedelta(seconds=60)

SERVICE_TO_METHOD = {}


async def async_setup_entry(hass, config_entry, async_add_entities):
    HassEntry.init(hass, config_entry).new_adder(ENTITY_DOMAIN, async_add_entities)
    await async_setup_config_entry(hass, config_entry, async_setup_platform, async_add_entities, ENTITY_DOMAIN)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})
    hass.data[DOMAIN]['add_entities'][ENTITY_DOMAIN] = async_add_entities
    config['hass'] = hass
    model = str(config.get(CONF_MODEL) or '')
    spec = hass.data[DOMAIN]['miot_specs'].get(model)
    entities = []
    if isinstance(spec, MiotSpec):
        for srv in spec.get_services(ENTITY_DOMAIN, 'mopping_machine'):
            if not srv.get_property('status'):
                continue
            if model in MIOT_LOCAL_MODELS:
                entities.append(MiotVacuumEntity(config, srv))
            elif 'roborock.' in model or 'rockrobo.' in model:
                entities.append(MiotRoborockVacuumEntity(config, srv))
            elif 'viomi.' in model:
                entities.append(MiotViomiVacuumEntity(config, srv))
            else:
                entities.append(MiotVacuumEntity(config, srv))
    for entity in entities:
        hass.data[DOMAIN]['entities'][entity.unique_id] = entity
    async_add_entities(entities, update_before_add=True)
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class MiotVacuumEntity(MiotEntity, StateVacuumEntity):
    _attr_activity = None

    def __init__(self, config: dict, miot_service: MiotService):
        super().__init__(miot_service, config=config, logger=_LOGGER)

        self._prop_power = miot_service.get_property('on', 'power')
        self._prop_status = miot_service.get_property('status')
        self._act_start = miot_service.get_action('start_sweep', 'start_mop')
        self._act_pause = miot_service.get_action('pause_sweeping', 'pause')
        self._act_stop = miot_service.get_action('stop_sweeping')
        self._act_locate = miot_service.get_action('find_device', 'position')
        self._prop_mode = miot_service.get_property('mode', 'clean_mode')
        self._prop_fan = self._prop_mode
        for srv in [*miot_service.spec.get_services('sweep', 'clean'), miot_service]:
            if prop := srv.get_property('fan_level', 'speed_level', 'suction_state', 'fan_mode', 'mode'):
                self._prop_fan = prop
                break
        self._prop_battery = miot_service.get_property('battery_level')
        self._srv_battery = miot_service.spec.get_service('battery')
        if self._srv_battery:
            self._prop_battery = self._srv_battery.get_property('battery_level')
        self._srv_audio = miot_service.spec.get_service('audio', 'voice')
        if self._srv_audio and not self._act_locate:
            self._act_locate = self._srv_audio.get_action('find_device', 'position')
        self._act_charge = None
        for srv in [*miot_service.spec.get_services('battery', 'go_charging'), miot_service]:
            act = srv.get_action('start_charge', 'start_charging')
            if act:
                self._act_charge = act
                break

        if self._prop_power:
            self._supported_features |= VacuumEntityFeature.TURN_ON
            self._supported_features |= VacuumEntityFeature.TURN_OFF
        if self._act_start:
            self._supported_features |= VacuumEntityFeature.START
        if self._act_pause:
            self._supported_features |= VacuumEntityFeature.PAUSE
        if self._act_stop:
            self._supported_features |= VacuumEntityFeature.STOP
        if self._act_charge:
            self._supported_features |= VacuumEntityFeature.RETURN_HOME
        if self._prop_fan:
            self._supported_features |= VacuumEntityFeature.FAN_SPEED
        if self._prop_status:
            self._supported_features |= VacuumEntityFeature.STATUS
            self._supported_features |= VacuumEntityFeature.STATE
        if self._act_locate:
            self._supported_features |= VacuumEntityFeature.LOCATE
        self._supported_features |= VacuumEntityFeature.SEND_COMMAND

        # Room cleaning through HA areas (vacuum.clean_area), for models exposing
        # room-information + start-vacuum-room-sweep (e.g. xiaomi.vacuum.d109gl)
        self._prop_room_info = miot_service.get_property('room_information')
        self._act_room_sweep = miot_service.get_action('start_vacuum_room_sweep')
        self._map_uid = None
        self._segment_ids = None
        clean_area = getattr(VacuumEntityFeature, 'CLEAN_AREA', None)
        if clean_area and Segment and self._prop_room_info and self._act_room_sweep:
            self._supported_features |= clean_area

        # Cloud JSON map (xiaomi.vacuum.* models with a vacuum-map service)
        self._prop_map_obj = None
        if str(self.model).startswith('xiaomi.vacuum.'):
            if srv := miot_service.spec.get_service('vacuum_map'):
                self._prop_map_obj = srv.get_property('map_obj_name')
        self._map_key = None
        self._map_object = None
        self._map_image = None
        self._map_rooms = {}
        self._map_attrs = {}

    async def async_update(self):
        await super().async_update()
        if not self._available:
            return
        if self._prop_room_info and self._act_room_sweep:
            self._update_room_info()
        if self._prop_map_obj:
            await self._async_update_map()
        if self._prop_status:
            self._prop_status.description_to_dict(self._state_attrs)
            val = self._prop_status.from_device(self.device)
            if val is None:
                pass
            elif val in self._prop_status.list_search(
                'Cleaning', 'Sweeping', 'Mopping', 'Sweeping And Mopping', 'Washing', 'Go Washing',
                'Part Sweeping', 'Zone Sweeping', 'Select Sweeping', 'Spot Sweeping', 'Goto Target',
                'Starting', 'Working', 'Busy', 'DustCollecting'
            ):
                self._attr_activity = VacuumActivity.CLEANING
            elif val in self._prop_status.list_search('Idle', 'Sleep'):
                self._attr_activity = VacuumActivity.IDLE
            elif val in self._prop_status.list_search(
                'Charging', 'Charging Completed', 'Fullcharge', 'Charge Done', 'Charged', 'Drying',
                'MultiTaskStationWorking', 'StationWorking', 'MultiTaskRecharge', 'WashBreak',
            ):
                self._attr_activity = VacuumActivity.DOCKED
            elif val in self._prop_status.list_search('Go Charging', 'GoWash', 'Go Wash'):
                self._attr_activity = VacuumActivity.RETURNING
            elif val in self._prop_status.list_search('Paused'):
                self._attr_activity = VacuumActivity.PAUSED
            elif val in self._prop_status.list_search('Error', 'Charging Problem'):
                self._attr_activity = VacuumActivity.ERROR
            else:
                self._attr_activity = VacuumActivity.IDLE

    async def async_turn_on(self, **kwargs):
        if self._prop_power:
            await self.async_set_property(self._prop_power, True)
        return await self.async_start()

    async def async_turn_off(self, **kwargs):
        return await self.async_stop()

    async def async_start(self):
        if self._act_start:
            return await self.async_call_action(self._act_start)
        return False

    async def async_stop(self, **kwargs):
        if self._act_stop:
            return await self.async_call_action(self._act_stop)
        return False

    async def async_pause(self):
        if self._act_pause:
            return await self.async_call_action(self._act_pause)
        return await self.async_stop()

    async def async_return_to_base(self, **kwargs):
        if self._act_charge:
            return await self.async_call_action(self._act_charge)
        return self.stop()

    async def async_locate(self, **kwargs):
        if self._act_locate:
            return await self.async_call_action(self._act_locate)
        return False

    def clean_spot(self, **kwargs):
        raise NotImplementedError()

    def _room_info(self):
        val = self._prop_room_info.from_device(self.device)
        if not isinstance(val, str) or not val:
            return {}
        try:
            info = json.loads(val)
        except ValueError:
            return {}
        return info if isinstance(info, dict) else {}

    def _update_room_info(self):
        info = self._room_info()
        if info.get('map_uid') is not None:
            self._map_uid = info['map_uid']
        rooms = info.get('rooms')
        if not isinstance(rooms, list) or not Segment or not self.registry_entry:
            return
        ids = sorted(str(r.get('id')) for r in rooms if isinstance(r, dict))
        if ids == self._segment_ids:
            return
        self._segment_ids = ids
        last_seen = self.last_seen_segments
        if last_seen is not None and sorted(s.id for s in last_seen) != ids:
            self.async_create_segments_issue()

    async def async_get_segments(self):
        rooms = self._room_info().get('rooms') or []
        segments = []
        for r in rooms:
            if not isinstance(r, dict) or r.get('id') is None:
                continue
            rid = str(r['id'])
            name = r.get('name') or (self._map_rooms.get(int(rid)) if rid.isdigit() else None)
            segments.append(Segment(id=rid, name=name or f'Room {rid}'))
        return segments

    @property
    def map_image(self):
        return self._map_image

    @property
    def map_attributes(self):
        return self._map_attrs

    async def _async_update_map(self):
        cloud = self.xiaomi_cloud
        did = self.miot_did
        if not isinstance(cloud, MiotCloud) or not did:
            return
        if add_cameras := self.device.entry.adders.get('camera'):
            if 'map' not in self._subs:
                self._subs['map'] = VacuumMapCameraEntity(self)
                add_cameras([self._subs['map']], update_before_add=False)
        try:
            rst = await cloud.async_get_props([{
                'did': str(did),
                'siid': self._prop_map_obj.service.iid,
                'piid': self._prop_map_obj.iid,
            }]) or []
            obj = rst[0].get('value') if rst and rst[0].get('code', 0) == 0 else None
            if not obj or not isinstance(obj, str) or obj == self._map_object:
                return
            name = json_map.object_file_name(obj)
            url = None
            for api in ['v2/home/get_interim_file_url_pro', 'v2/home/get_interim_file_url']:
                rdt = await cloud.async_request_api(api, {'obj_name': name}) or {}
                url = (rdt.get('result') or {}).get('url')
                if url:
                    break
            if not url:
                self.logger.debug('%s: No map file url for %s', self.name_model, name)
                return
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    self.logger.debug('%s: Map download failed: %s', self.name_model, resp.status)
                    return
                raw = await resp.read()
            if self._map_key is None:
                self._map_key = json_map.derive_key(self.model, did)
            decoded = await self.hass.async_add_executor_job(json_map.decrypt_map, raw, self._map_key)
            if not decoded:
                return
            image = await self.hass.async_add_executor_job(json_map.render_map, decoded)
            if not image:
                return
        except Exception as exc:  # a map failure must not break the vacuum update
            self.logger.debug('%s: Map update failed: %s', self.name_model, exc)
            return
        self._map_object = obj
        self._map_image = image
        self._map_rooms = json_map.room_names(decoded)
        room_id, room_name = json_map.current_room(decoded)
        self._map_attrs = {
            'map_name': json_map.map_label(decoded),
            'map_id': decoded.get('map_id'),
            'rooms': {str(k): v for k, v in self._map_rooms.items()},
            'current_room': room_name,
            'current_room_id': room_id,
        }
        self._state_attrs.update({
            'current_room': room_name,
            'current_room_id': room_id,
        })
        if sub := self._subs.get('map'):
            sub.update_from_parent()

    async def async_clean_segments(self, segment_ids, **kwargs):
        if not self._act_room_sweep:
            raise NotImplementedError()
        info = self._room_info()
        if info.get('map_uid') is not None:
            self._map_uid = info['map_uid']
        rooms = [int(i) if str(i).isdigit() else i for i in segment_ids]
        payload = {'rooms': rooms}
        if self._map_uid is not None:
            payload['map_uid'] = self._map_uid
        return await self.async_call_action(
            self._act_room_sweep, [json.dumps(payload, separators=(',', ':'))],
        )

    @property
    def fan_speed(self):
        if self._prop_fan:
            val = self._prop_fan.from_device(self.device)
            try:
                val = int(val)
            except (TypeError, ValueError):
                val = None
            if val is not None:
                return self._prop_fan.list_description(val)
        return None

    @property
    def fan_speed_list(self):
        if self._prop_fan:
            return self._prop_fan.list_description(None) or []
        return None

    async def async_set_fan_speed(self, fan_speed, **kwargs):
        if self._prop_fan:
            val = self._prop_fan.list_value(fan_speed)
            return self.async_set_property(self._prop_fan, val)
        return False

    async def async_send_command(self, command, params=None, **kwargs):
        """Send a command to a vacuum cleaner.
        This method must be run in the event loop.
        """
        return await self.async_miio_command(command, params)


class MiotRoborockVacuumEntity(MiotVacuumEntity):
    def __init__(self, config: dict, miot_service: MiotService):
        super().__init__(config, miot_service)
        self._supported_features |= VacuumEntityFeature.PAUSE
        self._supported_features |= VacuumEntityFeature.LOCATE
        self._supported_features |= VacuumEntityFeature.SEND_COMMAND

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        rooms = await self.get_room_mapping() or []

        if add_buttons := self.device.entry.adders.get('button'):
            from .button import ButtonSubEntity
            for r in rooms:
                if len(r) < 3:
                    continue
                rid = r[0]
                sub = f'segment_{rid}'
                self._subs[sub] = ButtonSubEntity(self, sub, option={
                    'name': f'{self.device_name} {r[2]}',
                    'async_press_action': self.async_start_clean_segment,
                    'press_kwargs': {'segment': rid},
                    'state_attrs': {'room_id': r[1]},
                })
                add_buttons([self._subs[sub]], update_before_add=False)
        self.logger.info('Room buttons: %s', [rooms, add_buttons])

    async def async_update(self):
        await super().async_update()
        if not self._available:
            return
        if self._miio2miot:
            self._state_attrs['props'] = self._miio2miot.miio_props_values
        props = self.miio_props
        adt = {}
        if 'clean_area' in props:
            adt['clean_area'] = round(props['clean_area'] / 1000000, 1)
        if 'clean_time' in props:
            adt['clean_time'] = round(props['clean_time'] / 60, 1)
        if adt:
            await self.async_update_attrs(adt)
            self.device.dispatch(self.device.decode_attrs({'props': props}))

    async def get_room_mapping(self):
        if not self.miot_device:
            return None
        try:
            rooms = await self.miot_device.async_send('get_room_mapping')
            if rooms and rooms != 'unknown_method':
                homes = await self.xiaomi_cloud.async_get_homerooms() if self.xiaomi_cloud else []
                cloud_rooms = {}
                for home in homes:
                    for room in home.get('roomlist', []):
                        cloud_rooms[room['id']] = room
                for r in rooms:
                    room = cloud_rooms.get(r[1])
                    name = room['name'] if room else r[0]
                    if len(r) < 3:
                        r.append(name)
                    else:
                        r[2] = name
                self._state_attrs['room_mapping'] = rooms
                self.logger.info('Vacuum rooms: %s', rooms)
                return rooms
            self.logger.info('Vacuum rooms: %s', rooms)
        except (DeviceException, Exception):
            pass
        return None

    @property
    def miio_props(self):
        return self._state_attrs.get('props') or {}

    async def async_pause(self):
        """Pause the cleaning task."""
        if not self._act_pause:
            return await self.async_miio_command('app_pause')
        return await super().async_pause()

    async def async_return_to_base(self, **kwargs):
        if self.model in ['rockrobo.vacuum.v1']:
            await self.async_stop()
        return await super().async_return_to_base()

    async def async_clean_spot(self, **kwargs):
        """Perform a spot clean-up."""
        if self._miio2miot:
            return await self.async_miio_command('app_spot')
        return await super().async_clean_spot()

    async def async_locate(self, **kwargs):
        """Locate the vacuum cleaner."""
        if not self._act_locate:
            return await self.async_miio_command('find_me', [''])
        return await super().async_locate()

    async def async_send_command(self, command, params=None, **kwargs):
        """Send a command to a vacuum cleaner."""
        dvc = self.miot_device
        if not dvc:
            raise NotImplementedError()
        return await self.async_miio_command(command, params)

    async def async_start_clean_segment(self, segment, repeat=1, **kwargs):
        segments = []
        for r in self._state_attrs.get('room_mapping', []):
            if segment in r:
                segments.append(r[0])
                break
        if not segments:
            await self.async_return_to_base()
            return False
        if self.state == VacuumActivity.CLEANING:
            await self.async_pause()
            await asyncio.sleep(1)
        if self.model in ['roborock.vacuum.m1s']:
            return await self.async_miio_command('app_segment_clean', segments)
        return await self.async_miio_command('app_segment_clean', [{'segments': segments, 'repeat': repeat}])


class MiotViomiVacuumEntity(MiotVacuumEntity):
    def __init__(self, config: dict, miot_service: MiotService):
        super().__init__(config, miot_service)
        self._supported_features |= VacuumEntityFeature.LOCATE
        self._supported_features |= VacuumEntityFeature.SEND_COMMAND
        self._miio_props = [
            'run_state', 'mode', 'err_state', 'battary_life', 'box_type', 'mop_type', 's_time', 's_area',
            'suction_grade', 'water_grade', 'remember_map', 'has_map', 'is_mop', 'has_newmap',
        ]

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self._miio2miot:
            self._miio2miot.extend_miio_props(self._miio_props)

    async def async_update(self):
        await super().async_update()
        if not self._available:
            return
        props = self.device.props or {}
        adt = {}
        if 'miio.s_area' in props:
            adt['clean_area'] = props['miio.s_area']
        if 'miio.s_time' in props:
            adt['clean_time'] = props['miio.s_time']
        if adt:
            await self.async_update_attrs(adt)
            self.device.dispatch(self.device.decode_attrs(adt))

    async def async_locate(self, **kwargs):
        """Locate the vacuum cleaner."""
        if not self._act_locate:
            return await self.async_miio_command('set_resetpos', [1])
        return await super().async_locate()

    async def async_send_command(self, command, params=None, **kwargs):
        """Send a command to a vacuum cleaner."""
        dvc = self.miot_device
        if not dvc:
            raise NotImplementedError()
        _LOGGER.debug('%s: Send command: %s %s', self.name_model, command, params)
        if command == 'app_zoned_clean':
            # params: [[x1, y2, x2, y1, repeats]]
            rpt = 1
            lst = []
            for z in params or []:
                rpt = z.pop(-1)
                lst.append(z)
            return await self.async_clean_zones(lst, rpt)
        elif command == 'app_goto_target':
            return await self.async_clean_point(params)
        return await self.async_miio_command(command, params)

    async def async_clean_zones(self, zones, repeats=1):
        result = []
        i = 0
        for z in zones:
            x1, y2, x2, y1 = z
            res = '_'.join(str(x) for x in [i, 0, x1, y1, x1, y2, x2, y2, x2, y1])
            for _ in range(repeats):
                result.append(res)
                i += 1
        result = [i, *result]
        await self.async_miio_command('set_uploadmap', [1])
        await self.async_miio_command('set_zone', result)
        return await self.async_miio_command('set_mode', [3, 1])

    async def async_clean_point(self, point):
        await self.async_miio_command('set_uploadmap', [0])
        return await self.async_miio_command('set_pointclean', [1, *point])


class VacuumMapCameraEntity(BaseSubEntity, Camera):
    """The cloud map of a vacuum, rendered by its parent vacuum entity."""

    def __init__(self, parent, option=None):
        option = {
            'name': 'Map',
            'icon': 'mdi:map',
            **(option or {}),
        }
        super().__init__(parent, 'map', option, domain='camera')
        Camera.__init__(self)
        self._attr_content_type = 'image/png'
        self._available = False

    @property
    def supported_features(self):
        return CameraEntityFeature(0)

    @property
    def state(self):
        return self._parent.map_attributes.get('map_name') or super().state

    def update(self, data=None):
        self._available = self._parent.map_image is not None
        self._state_attrs.update(self._parent.map_attributes)

    async def async_camera_image(self, width=None, height=None):
        return self._parent.map_image
