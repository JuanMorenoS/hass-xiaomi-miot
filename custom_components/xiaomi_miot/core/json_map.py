"""Map support for Xiaomi-branded vacuums that publish a JSON map.

These models (e.g. xiaomi.vacuum.d109gl) expose the name of the current map
object in the `vacuum-map / map-obj-name` property. The object is downloaded
from the Xiaomi file service and looks like:

    {"version": 2, "data": "<base64 AES-CBC ciphertext>"}

which decrypts to a zlib-compressed JSON document describing the whole map.

The AES key is not a secret: the device's own Mi Home plugin derives it from
the model string and the device id, so it can be reproduced for any device of
a supported model:

    MODEL_KEY   = model[-16:]
    IV          = "ABCDEF1234123412"
    ENC_KEY     = AES-CBC(MODEL_KEY + did, key=MODEL_KEY, iv=IV, Pkcs7)
    DECRYPT_KEY = MD5(ENC_KEY)

Decoding and rendering are adapted from voska/xiaomi-vacuum (MIT License,
Copyright (c) 2022 Tasshack), a fork of Tasshack/dreame-vacuum.
"""
import base64
import hashlib
import json
import logging
import zlib
from io import BytesIO

_LOGGER = logging.getLogger(__name__)

MAP_IV = b'ABCDEF1234123412'

# Occupancy grid markers; every other non-zero value is a room id.
GRID_EMPTY = 0
GRID_WALL = 255

WALL_COLOR = (70, 80, 95)
FLOOR_COLOR = (226, 232, 240)
ROOM_COLORS = [
    (122, 183, 240), (160, 215, 145), (245, 205, 120), (230, 150, 165),
    (180, 165, 220), (140, 210, 205), (240, 175, 130), (200, 200, 160),
    (170, 190, 230), (215, 180, 150), (150, 195, 175), (225, 195, 215),
]
ROUTE_COLOR = (255, 255, 255, 235)
ROUTE_WIDTH = 3
SCALE = 4


def object_file_name(object_name):
    """The file-url API only accepts the inner obj_name of a wrapped value."""
    if isinstance(object_name, str) and object_name.startswith('{'):
        try:
            return json.loads(object_name).get('obj_name') or object_name
        except ValueError:
            pass
    return object_name


def derive_key(model, did):
    """Reproduce the Mi Home plugin's map key for this model and device."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    model_key = model[-16:].encode()
    original = model_key + str(did).encode()
    padding = 16 - (len(original) % 16)
    padded = original + bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(model_key), modes.CBC(MAP_IV)).encryptor()
    enc_key = encryptor.update(padded) + encryptor.finalize()
    return hashlib.md5(enc_key).digest()


def decrypt_map(raw, key):
    """Turn a downloaded map object into the decoded map document."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    try:
        envelope = json.loads(raw)
        data = envelope['data'].replace('_', '/').replace('-', '+')
        blob = base64.b64decode(data)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(MAP_IV)).decryptor()
        plain = decryptor.update(blob) + decryptor.finalize()
        if plain and 0 < plain[-1] <= 16:
            plain = plain[:-plain[-1]]
        return json.loads(zlib.decompress(plain))
    except Exception as exc:  # a bad map object must never break the poll
        _LOGGER.debug('JSON map decode failed: %s', exc)
        return None


def room_names(map_data):
    """Room id -> name, as named in the Mi Home app (empty names skipped)."""
    names = {}
    for room in map_data.get('room_attrs') or []:
        rid = room.get('id')
        name = (room.get('room_name') or '').strip()
        if rid is not None and name:
            names[int(rid)] = name
    return names


def map_label(map_data):
    """A stable name for the map, used as the camera state."""
    name = (map_data.get('map_name') or '').strip()
    if name:
        return name
    map_id = map_data.get('map_id')
    return f'Map {map_id}' if map_id is not None else 'Map'


def _grid(map_data):
    width = int(map_data['width'])
    height = int(map_data['height'])
    grid = zlib.decompress(base64.b64decode(map_data['map_data']))
    if len(grid) < width * height:
        raise ValueError(f'map grid too short: {len(grid)} < {width * height}')
    return width, height, grid


def _to_grid(map_data, point):
    resolution = int(map_data.get('resolution') or 0)
    if not resolution:
        return None
    return (
        (int(point['x']) - int(map_data['origin_x'])) / resolution,
        (int(point['y']) - int(map_data['origin_y'])) / resolution,
    )


def current_room(map_data):
    """(room id, name) of the cell the robot stands on, or (None, None).

    A docked robot reports no live position; it sits on the pile, so the pile's
    cell is used instead.
    """
    try:
        width, height, grid = _grid(map_data)
        position = map_data.get('position') or {}
        cell = _to_grid(map_data, position) if position else None
        if cell is None and map_data.get('have_pile'):
            cell = (map_data.get('pile_x', -1), map_data.get('pile_y', -1))
        if cell is None:
            return None, None
        gx, gy = int(cell[0]), int(cell[1])
        if not (0 <= gx < width and 0 <= gy < height):
            return None, None
        value = grid[gy * width + gx]
        if value in (GRID_EMPTY, GRID_WALL):
            return None, None
        return value, room_names(map_data).get(value, f'Room {value}')
    except Exception as exc:
        _LOGGER.debug('JSON map current room lookup failed: %s', exc)
        return None, None


def render_map(map_data):
    """Render the map document to PNG bytes."""
    from PIL import Image, ImageDraw

    try:
        width, height, grid = _grid(map_data)
        room_ids = sorted({int(r['room_id']) for r in map_data.get('map_room_info') or []})
        palette = [FLOOR_COLOR] * 256
        palette[GRID_EMPTY] = (0, 0, 0)
        palette[GRID_WALL] = WALL_COLOR
        for i, rid in enumerate(room_ids):
            if 0 < rid < 255:
                palette[rid] = ROOM_COLORS[i % len(ROOM_COLORS)]
        image = Image.frombytes('P', (width, height), bytes(grid[:width * height]))
        image.putpalette([c for rgb in palette for c in rgb])
        image.info['transparency'] = GRID_EMPTY
        image = image.convert('RGBA').resize((width * SCALE, height * SCALE), Image.NEAREST)
        draw = ImageDraw.Draw(image)

        if map_data.get('have_pile'):
            px = int(map_data.get('pile_x', 0)) * SCALE
            py = int(map_data.get('pile_y', 0)) * SCALE
            draw.ellipse([px - 11, py - 11, px + 11, py + 11], fill=(35, 45, 60, 255))
            draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill=(250, 250, 250, 255))

        # Cleaning route the robot actually drove, in world mm like position.
        points = (map_data.get('paths') or {}).get('points') or []
        route = []
        for pt in points:
            if 'x' in pt and 'y' in pt:
                cell = _to_grid(map_data, pt)
                if cell:
                    route.append((cell[0] * SCALE, cell[1] * SCALE))
        if len(route) > 1:
            draw.line(route, fill=ROUTE_COLOR, width=ROUTE_WIDTH, joint='curve')

        position = map_data.get('position') or {}
        cell = _to_grid(map_data, position) if position else None
        if cell:
            rx, ry = cell[0] * SCALE, cell[1] * SCALE
            draw.ellipse([rx - 13, ry - 13, rx + 13, ry + 13], fill=(255, 255, 255, 255))
            draw.ellipse([rx - 9, ry - 9, rx + 9, ry + 9], fill=(52, 120, 246, 255))

        # The device grid origin is bottom-left; images are drawn top-left.
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        return buffer.getvalue()
    except Exception as exc:
        _LOGGER.debug('JSON map render failed: %s', exc)
        return None
