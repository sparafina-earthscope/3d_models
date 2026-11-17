"""Build 3D-printable terrain models (STL) from USGS 3DEP elevation data."""
import datetime, http.client, io, json, math, os, re, socket, ssl, struct, tempfile, threading, time, urllib.error, \
    urllib.parse, urllib.request

import numpy as np
import plotly.graph_objects as go
import tifffile
from PIL import Image
from pyproj import Transformer

# name: (center latitude, center longitude, width of the square area in km)
PLACES = {
    'Mount St Helens': (46.1912, -122.1944, 15),
    'Mt Rainier':      (46.8529, -121.7604, 30),
    'Mt Whitney':      (36.5785, -118.2700, 14),
    'Grand Tetons':    (43.7900, -110.8500, 30),
    'Mt Hood':         (45.3736, -121.6960, 20),
    'Mt Shasta':       (41.4092, -122.1949, 28),
    'Pikes Peak':      (38.8405, -105.0442, 20),
}

SIZE_MM = 152.4      # model width and depth (6 inches)
GRID = 500           # samples per side (~0.3 mm spacing)
BASE_MM = 3.0        # solid base below lowest point

TIMEOUT = 120        # seconds per network operation when downloading a DEM
DOWNLOAD_DEADLINE = 900   # seconds for one whole download attempt, so a trickling stream can't stall forever

# NDVI layers for the 3D view, from the earliest and latest Landsat imagery (via Microsoft Planetary Computer)
PC_URL = 'https://planetarycomputer.microsoft.com/api'
NDVI_MAX_CLOUD = 10      # largest Landsat scene cloud cover to use, in %
NDVI_SPAN_DAYS = 31      # scenes combined to cover an area are taken within this many days
NDVI_MIN_COVER = 0.99    # fraction of the area the scenes must cover
NDVI_MAX_SCENES = 12     # scenes to try before giving up
NDVI_TIMEOUT = 60        # seconds per request to Planetary Computer
NDVI_SEASON = ('07-01', '09-30')   # the latest NDVI comes from this time of year, the season of the earliest imagery
NDVI_LATEST_YEARS = 5    # years to look back for the latest NDVI
NDVI_LATEST_MAX_AGE = 30   # days before a saved latest NDVI is downloaded again, to pick up new imagery
# Places whose first NDVI layer comes from a chosen period instead of the earliest imagery:
# name: (first date, last date, note shown with the layer)
NDVI_EARLY_PERIODS = {
    'Mount St Helens': ('1979-07-01', '1979-09-30', 'before the 18 May 1980 eruption'),
}

# Data folders in the project folder (the parent of app/), shared with the notebook
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEM_DIR = os.path.join(ROOT, 'dem')            # downloaded DEMs
STL_DIR = os.path.join(ROOT, 'stl')            # STLs and zips
PREVIEW_DIR = os.path.join(ROOT, 'preview')    # hillshade previews
TIFF_DIR = os.path.join(ROOT, 'tiff')          # other GeoTIFFs

TERRAIN_COLORS = [[0.0, '#4a7a46'], [0.35, '#9a9a5a'], [0.6, '#8c6d4f'], [0.85, '#b8a898'], [1.0, '#ffffff']]
NDVI_RANGE = (-0.2, 0.9)
# (NDVI, colour): water and snow blue-grey, bare ground tan, then sparse to dense vegetation
NDVI_COLORS = [[(v - NDVI_RANGE[0]) / (NDVI_RANGE[1] - NDVI_RANGE[0]), c] for v, c in
               [(-0.2, '#4f6d8f'), (0.0, '#e4ded2'), (0.15, '#cdb67c'), (0.3, '#a9c36a'),
                (0.5, '#5f9f46'), (0.7, '#2f7d33'), (0.9, '#16501f')]]


def slugify(name):
    """A file-name-safe version of a place name: 'Mt. Hood' -> 'mt_hood'."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower().replace('.', '')).strip('_')


def model_name(place, exag):
    return f'{slugify(place)}_{exag:g}x'


def place_extent(place):
    """Square UTM extent centred on the place, snapped to the DEM resolution."""
    lat, lon, size_km = PLACES[place]
    size_m = size_km * 1000
    res = max(10.0, math.ceil(size_m / 2000))   # keep downloads near 2000 px per side
    epsg = (32600 if lat >= 0 else 32700) + int(lon // 6 + 31)
    cx, cy = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
    xmin = math.floor((cx - size_m / 2) / res) * res
    ymin = math.floor((cy - size_m / 2) / res) * res
    n = int(round(size_m / res))
    return epsg, res, xmin, ymin, n


_download_locks = {}
_download_locks_guard = threading.Lock()


def _atomic_write(path, write):
    """Write to a temp file next to path, then swap it in, so readers never see a partial file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.', prefix=os.path.basename(path) + '.', suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as f:
            write(f)
        os.chmod(tmp, 0o644)            # mkstemp creates files readable only by the owner
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _read_tiff(path, n):
    """The DEM's pixels, or None unless the file is a readable n x n single-band TIFF."""
    try:
        a = tifffile.imread(path)
    except Exception:   # a corrupt file can raise almost anything
        return None
    return a if a.shape == (n, n) else None


def _fetch_dem(url, path, n):
    """Download an n x n DEM to path, retrying server errors, dropped connections and non-TIFF replies."""
    def write(f):
        deadline = time.monotonic() + DOWNLOAD_DEADLINE
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            while chunk := r.read1(1 << 16):   # read1 returns after each socket read, so the deadline is checked
                if time.monotonic() > deadline:
                    raise TimeoutError('download took too long')
                f.write(chunk)
    for attempt in range(5):   # the 3DEP ImageServer sometimes returns 504s or drops the connection
        wait = 10 * (attempt + 1)
        try:
            _atomic_write(path, write)
            if _read_tiff(path, n) is not None:
                return
            os.remove(path)    # e.g. a JSON error message sent with HTTP 200
            problem = 'the server did not return an image'
        except urllib.error.HTTPError as e:
            e.close()          # release the connection
            problem = f'HTTP {e.code} {e.reason}'.strip()
            if e.code < 500 and e.code not in (408, 429):   # other 4xx errors won't fix themselves
                raise RuntimeError(f'USGS 3DEP refused the elevation request ({problem}).') from None
            retry_after = (e.headers.get('Retry-After') or '') if e.headers else ''
            if retry_after.isdigit():
                wait = min(int(retry_after), 120)
        except (urllib.error.URLError, http.client.HTTPException, ssl.SSLError, socket.timeout, TimeoutError,
                ConnectionError) as e:
            problem = str(getattr(e, 'reason', e))
        if attempt == 4:
            raise RuntimeError(f'Could not download elevation data from USGS 3DEP ({problem}). Try again later.')
        print(f'download failed ({problem}), retrying...')
        time.sleep(wait)


def download_dem(place, dem_dir=DEM_DIR):
    """Download the DEM from USGS 3DEP, or reuse a cached copy."""
    epsg, res, xmin, ymin, n = place_extent(place)
    lat, lon, size_km = PLACES[place]
    os.makedirs(dem_dir, exist_ok=True)
    dem_file = os.path.join(dem_dir, f'dem_{slugify(place)}_{lat:.4f}_{lon:.4f}_{size_km:g}km_{res:g}m.tif')
    with _download_locks_guard:
        lock = _download_locks.setdefault(os.path.abspath(dem_file), threading.Lock())
    with lock:   # one download per file; other builds wait, then reuse it
        dem = _read_tiff(dem_file, n) if os.path.exists(dem_file) else None
        if dem is None:
            if os.path.exists(dem_file):
                print(f'cached DEM {dem_file} is unreadable, downloading it again')
                os.remove(dem_file)
            xmax, ymax = xmin + n * res, ymin + n * res
            url = ('https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage'
                   f'?bbox={xmin:.0f},{ymin:.0f},{xmax:.0f},{ymax:.0f}&bboxSR={epsg}&imageSR={epsg}&size={n},{n}'
                   '&format=tiff&pixelType=F32&interpolation=RSP_BilinearInterpolation&f=image')
            print(f'Downloading {n} x {n} px DEM at {res:g} m...')
            _fetch_dem(url, dem_file, n)
            dem = _read_tiff(dem_file, n)
    dem = np.where(dem.astype(np.float64) < -1000, np.nan, dem.astype(np.float64))
    valid = np.isfinite(dem)
    if not valid.any() or dem[valid].max() - dem[valid].min() < 1:
        raise ValueError(f'No elevation data for {place}: the area may be outside USGS 3DEP coverage '
                         '(for example, over the ocean).')
    return np.where(valid, dem, dem[valid].min()), res


def _pc_request(url, body=None):
    """GET (or POST JSON to) a Planetary Computer URL and return the reply, retrying server errors."""
    for attempt in range(3):
        wait = 5 * (attempt + 1)
        try:
            req = urllib.request.Request(url, json.dumps(body).encode() if body else None,
                                         {'Content-Type': 'application/json'} if body else {})
            with urllib.request.urlopen(req, timeout=NDVI_TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            e.close()
            problem = f'HTTP {e.code} {e.reason}'.strip()
            if e.code < 500 and e.code not in (408, 429):
                raise RuntimeError(f'Planetary Computer refused the request ({problem}).') from None
            retry_after = (e.headers.get('Retry-After') or '') if e.headers else ''
            if retry_after.isdigit():
                wait = min(int(retry_after), 60)
        except (urllib.error.URLError, http.client.HTTPException, ssl.SSLError, socket.timeout, TimeoutError,
                ConnectionError) as e:
            problem = str(getattr(e, 'reason', e))
        if attempt == 2:
            raise RuntimeError(f'Could not reach Microsoft Planetary Computer ({problem}). Try again later.')
        print(f'Planetary Computer request failed ({problem}), retrying...')
        time.sleep(wait)


def _area_bounds(place):
    """The model's square area: its UTM EPSG code and bounds, and the same square as a lon/lat polygon ring."""
    epsg, res, xmin, ymin, n = place_extent(place)
    xmax, ymax = xmin + n * res, ymin + n * res
    t = Transformer.from_crs(epsg, 4326, always_xy=True)
    ring = [list(t.transform(x, y)) for x, y in [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]]
    return epsg, (xmin, ymin, xmax, ymax), ring


def landsat_scenes(place, collection, newest_first=False, dates=None, limit=50):
    """Low-cloud Landsat scenes over the place from a Planetary Computer collection, in date order.

    dates is an optional 'start/end' range of ISO times.
    """
    conditions = [{'op': '<=', 'args': [{'property': 'eo:cloud_cover'}, NDVI_MAX_CLOUD]}]
    if collection == 'landsat-c2-l1':   # terrain-corrected scenes only; the others can be kilometres out
        conditions.append({'op': '=', 'args': [{'property': 'landsat:correction'}, 'L1TP']})
    body = {'collections': [collection], 'intersects': {'type': 'Polygon', 'coordinates': [_area_bounds(place)[2]]},
            'filter-lang': 'cql2-json', 'filter': {'op': 'and', 'args': conditions},
            'sortby': [{'field': 'properties.datetime', 'direction': 'desc' if newest_first else 'asc'}],
            'limit': limit}
    if dates:
        body['datetime'] = dates
    return json.loads(_pc_request(f'{PC_URL}/stac/v1/search', body))['features']


def _scene_ndvi(item, place, token=None):
    """NDVI from one Landsat scene on the model's GRID x GRID grid (NaN outside the scene), and where it's clear.

    Level-1 scenes (Landsat 1-5 MSS) need a Planetary Computer token to read their metadata.
    """
    level2 = item['collection'] == 'landsat-c2-l2'
    # Landsat 1-5 MSS: NDVI was first defined with its band 5 (red) and band 7 (nir09)
    nir = 'nir09' if 'mss' in item['properties']['instruments'] else 'nir08'
    assets = ['red', nir] + (['qa_pixel'] if level2 else [])
    epsg, (xmin, ymin, xmax, ymax), _ = _area_bounds(place)
    q = [('collection', item['collection']), ('item', item['id'])] + [('assets', a) for a in assets] + [
         ('asset_as_band', 'true'), ('expression', ';'.join(assets)),
         ('coord_crs', f'epsg:{epsg}'), ('dst_crs', f'epsg:{epsg}'), ('resampling', 'nearest')]
    url = (f'{PC_URL}/data/v1/item/bbox/{xmin:.0f},{ymin:.0f},{xmax:.0f},{ymax:.0f}/{GRID}x{GRID}.tif?'
           + urllib.parse.urlencode(q))
    reply = _pc_request(url)
    try:
        a = tifffile.imread(io.BytesIO(reply)).astype(np.float64)
    except Exception:   # e.g. a JSON error message
        raise RuntimeError('Planetary Computer did not return a Landsat image.') from None
    red_dn, nir_dn = a[..., 0], a[..., 1]
    good = (red_dn > 0) & (nir_dn > 0)    # 0 is fill outside the scene
    if level2:
        # surface reflectance, using the scale and offset in the scene's metadata
        def reflectance(dn, asset):
            band = item['assets'][asset]['raster:bands'][0]
            return dn * band['scale'] + band['offset']
        # not dilated cloud (bit 1), cloud (bit 3) or cloud shadow (bit 4). In the mountains these also
        # mark bright rock, glacier edges and valley shadows, so they're a preference, not a mask.
        clear = (a[..., 2].astype(np.uint16) & 0b11010) == 0
    else:
        # top-of-atmosphere reflectance; the sun-angle term cancels in the NDVI ratio. (Landsat 1's
        # cloud flag marks glaciers as cloud, so it isn't used: scenes are already limited to low cloud.)
        mtl = json.loads(_pc_request(item['assets']['mtl.json']['href'] + '?' + token))
        k = mtl['LANDSAT_METADATA_FILE']['LEVEL1_RADIOMETRIC_RESCALING']
        def reflectance(dn, asset):
            band = re.search(r'(\d+)$', item['assets'][asset]['eo:bands'][0]['name']).group(1)
            return dn * float(k[f'REFLECTANCE_MULT_BAND_{band}']) + float(k[f'REFLECTANCE_ADD_BAND_{band}'])
    r, n = reflectance(red_dn, 'red'), reflectance(nir_dn, nir)
    good &= n + r > 0
    ndvi = np.full(red_dn.shape, np.nan)
    ndvi[good] = (n[good] - r[good]) / (n[good] + r[good])
    return ndvi, good & clear if level2 else good


def _ndvi_composite(place, scenes, token=None):
    """NDVI from the first of the scenes that together cover the place, and the scenes used, or (None, []).

    Each pixel comes from the first scene where it's clear or, failing that, the first scene with data there.
    Scenes are combined only if they were taken within NDVI_SPAN_DAYS of the first one.
    """
    def result():
        ndvi = np.where(np.isfinite(clear), clear, any_data)
        source = np.where(np.isfinite(clear), clear_from, any_from)
        if np.isfinite(ndvi).mean() < NDVI_MIN_COVER:
            return None, []
        return ndvi, [s for i, s in enumerate(window) if (source == i).any()]

    window, start = [], None
    for item in scenes[:NDVI_MAX_SCENES]:
        date = datetime.datetime.fromisoformat(item['properties']['datetime'].replace('Z', '+00:00'))
        if start is not None and abs((date - start).days) > NDVI_SPAN_DAYS:
            ndvi, used = result()   # too far apart to combine: use what we have, or start again
            if used:
                return ndvi, used
            window, start = [], None
        if start is None:
            start = date
            clear, any_data = np.full((GRID, GRID), np.nan), np.full((GRID, GRID), np.nan)
            clear_from, any_from = np.full((GRID, GRID), -1), np.full((GRID, GRID), -1)
        part, part_clear = _scene_ndvi(item, place, token)
        for best, best_from, take in ((clear, clear_from, np.isnan(clear) & part_clear),
                                      (any_data, any_from, np.isnan(any_data) & np.isfinite(part))):
            best[take] = part[take]
            best_from[take] = len(window)
        window.append(item)
        if np.isfinite(clear).mean() >= NDVI_MIN_COVER:
            break
    return result() if window else (None, [])


def _find_ndvi(place, which):
    """NDVI from the 'earliest' Landsat imagery of the place, or the 'latest' from the NDVI_SEASON part of a year.

    For a place in NDVI_EARLY_PERIODS, 'earliest' means the earliest imagery in its period.
    """
    if which == 'earliest':
        token = json.loads(_pc_request(f'{PC_URL}/sas/v1/token/landsat-c2-l1'))['token']   # to read the metadata
        period = NDVI_EARLY_PERIODS.get(place)
        dates = f'{period[0]}T00:00:00Z/{period[1]}T23:59:59Z' if period else None
        ndvi, used = _ndvi_composite(place, landsat_scenes(place, 'landsat-c2-l1', dates=dates), token)
    else:
        this_year = datetime.date.today().year
        for year in range(this_year, this_year - NDVI_LATEST_YEARS, -1):
            season = f'{year}-{NDVI_SEASON[0]}T00:00:00Z/{year}-{NDVI_SEASON[1]}T23:59:59Z'
            ndvi, used = _ndvi_composite(place, landsat_scenes(place, 'landsat-c2-l2', newest_first=True,
                                                                dates=season))
            if used:
                break
    if not used:
        raise RuntimeError(f'No low-cloud Landsat imagery covers {place}.')
    return ndvi, used


def _write_geotiff(path, a, epsg, bounds, description):
    """Write a float32 GeoTIFF of a north-up array covering bounds (xmin, ymin, xmax, ymax)."""
    xmin, ymin, xmax, ymax = bounds
    rows, cols = a.shape
    tags = [(33550, 'd', 3, ((xmax - xmin) / cols, (ymax - ymin) / rows, 0.0), True),    # ModelPixelScale
            (33922, 'd', 6, (0.0, 0.0, 0.0, xmin, ymax, 0.0), True),                    # ModelTiepoint
            (34735, 'H', 16, (1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, epsg), True),  # GeoKeys
            (42113, 's', 0, 'nan', True)]                                               # GDAL nodata
    buf = io.BytesIO()    # tifffile can't write to the file handle _atomic_write opens
    tifffile.imwrite(buf, a.astype(np.float32), description=description, metadata=None, extratags=tags)
    _atomic_write(path, lambda f: f.write(buf.getvalue()))


def ndvi_layer_name(place, which):
    """'Earliest NDVI' or 'Latest NDVI', or for a place in NDVI_EARLY_PERIODS, e.g. 'NDVI before the 18 May 1980 eruption'."""
    if which == 'earliest' and place in NDVI_EARLY_PERIODS:
        return f'NDVI {NDVI_EARLY_PERIODS[place][2]}'
    return f'{which.capitalize()} NDVI'


def ndvi_label(info):
    """'Landsat 1 MSS, 28 Jul 1972' or 'Landsat 8 and 9 OLI/TIRS, 9–26 Sep 2026' for an NDVI image's scenes."""
    satellites = {}    # instrument: Landsat numbers
    for platform, instruments in zip(info['platforms'], info['instruments']):
        satellites.setdefault('/'.join(instruments).upper(), set()).add(platform.replace('landsat-', ''))
    sensors = ', '.join(f"Landsat {' and '.join(sorted(numbers, key=int))} {instrument}"
                        for instrument, numbers in satellites.items())
    d0, d1 = (datetime.date.fromisoformat(d) for d in (min(info['dates']), max(info['dates'])))
    if d0 == d1:
        when = f'{d0.day} {d0:%b %Y}'
    elif (d0.year, d0.month) == (d1.year, d1.month):
        when = f'{d0.day}–{d1.day} {d1:%b %Y}'
    else:
        when = f'{d0.day} {d0:%b}{f" {d0.year}" if d0.year != d1.year else ""} – {d1.day} {d1:%b %Y}'
    return f'{sensors}, {when}'


def _read_ndvi(path):
    """A saved NDVI image and its description, or (None, None) if the file is missing or unreadable."""
    try:
        with tifffile.TiffFile(path) as tif:
            ndvi, info = tif.asarray(), json.loads(tif.pages[0].description)
        if ndvi.shape == (GRID, GRID) and {'scenes', 'dates', 'platforms', 'instruments', 'retrieved'} <= set(info):
            return ndvi.astype(np.float64), info
    except FileNotFoundError:
        return None, None
    except Exception:
        pass
    print(f'saved NDVI {path} is unreadable, downloading it again')
    return None, None


def download_ndvi(place, which='earliest', tiff_dir=TIFF_DIR):
    """NDVI of the place from the 'earliest' or 'latest' Landsat imagery, or a saved copy.

    Returns a GRID x GRID array on the model's grid (NaN where there's no data) and a dict describing the scenes.
    A saved latest NDVI is downloaded again after NDVI_LATEST_MAX_AGE days, to pick up new imagery.
    """
    lat, lon, size_km = PLACES[place]
    os.makedirs(tiff_dir, exist_ok=True)
    path = os.path.join(tiff_dir, f'ndvi_{which}_{slugify(place)}_{lat:.4f}_{lon:.4f}_{size_km:g}km_{GRID}px.tif')
    with _download_locks_guard:
        lock = _download_locks.setdefault(os.path.abspath(path), threading.Lock())
    period = NDVI_EARLY_PERIODS.get(place) if which == 'earliest' else None
    with lock:   # one download per file; other builds wait, then reuse it
        saved, info = _read_ndvi(path)
        # a saved copy from another period (e.g. before NDVI_EARLY_PERIODS was changed) doesn't count
        if saved is not None and info.get('period') != (list(period[:2]) if period else None):
            saved = None
        if saved is not None and (which == 'earliest' or (datetime.date.today() - datetime.date.fromisoformat(
                info['retrieved'])).days <= NDVI_LATEST_MAX_AGE):
            return saved, info
        print(f'{ndvi_layer_name(place, which)} for {place}: downloading from Landsat...')
        try:
            ndvi, used = _find_ndvi(place, which)
        except Exception as ex:
            if saved is None:
                raise
            print(f'Could not update the {which} NDVI ({ex}); using the saved copy')
            return saved, info
        level2 = used[0]['collection'] == 'landsat-c2-l2'
        info = {'scenes': [s['id'] for s in used],
                'dates': [s['properties']['datetime'][:10] for s in used],
                'platforms': [s['properties']['platform'] for s in used],
                'instruments': [s['properties']['instruments'] for s in used],
                'retrieved': datetime.date.today().isoformat(),
                'period': list(period[:2]) if period else None,
                'note': period[2] if period else None,
                'source': f'USGS Landsat Collection 2 Level-{2 if level2 else 1}, via Microsoft Planetary Computer',
                'method': f'NDVI from {"surface" if level2 else "top-of-atmosphere"} reflectance'}
        epsg, bounds, _ = _area_bounds(place)
        _write_geotiff(path, ndvi, epsg, bounds, json.dumps(info))
        return ndvi.astype(np.float32).astype(np.float64), info   # the same values as a saved copy


def wall(t_edge, b_edge, flip):
    t0, t1, b0, b1 = t_edge[:-1], t_edge[1:], b_edge[:-1], b_edge[1:]
    if flip:
        return np.concatenate([np.stack([t0, t1, b0], 1), np.stack([t1, b1, b0], 1)])
    return np.concatenate([np.stack([t0, b0, t1], 1), np.stack([t1, b0, b1], 1)])


def grid_heights(dem):
    """The DEM resampled to the model's GRID x GRID grid, in metres."""
    img = Image.fromarray(dem.astype(np.float32)).resize((GRID, GRID), Image.BILINEAR)
    return np.array(img, dtype=np.float64)


def grid_lonlat(place):
    """Longitude and latitude of the model's GRID x GRID grid points (row 0 north)."""
    epsg, (xmin, ymin, xmax, ymax), _ = _area_bounds(place)
    centres = (np.arange(GRID) + 0.5) * (xmax - xmin) / GRID    # the grid points are pixel centres
    east, north = np.meshgrid(xmin + centres, ymax - centres)
    return Transformer.from_crs(epsg, 4326, always_xy=True).transform(east, north)


def write_stl(dem, size_m, exag, path, title):
    """Turn a DEM into a watertight binary STL with a flat base. Returns the surface heights in mm."""
    z_m = grid_heights(dem)
    scale = SIZE_MM / size_m                   # mm per meter horizontally
    z = (z_m - z_m.min()) * scale * exag + BASE_MM
    xs = np.linspace(0, SIZE_MM, GRID)
    ys = np.linspace(SIZE_MM, 0, GRID)         # row 0 = north
    X, Y = np.meshgrid(xs, ys)
    top = np.dstack([X, Y, z])
    bot = np.dstack([X, Y, np.zeros_like(z)])

    tris = []
    # top surface (normal up): rows go north->south, so wind accordingly
    a = top[:-1, :-1]; b = top[:-1, 1:]; c = top[1:, :-1]; d = top[1:, 1:]
    tris.append(np.stack([a, c, b], -2).reshape(-1, 3, 3))
    tris.append(np.stack([b, c, d], -2).reshape(-1, 3, 3))
    # bottom (normal down)
    a = bot[:-1, :-1]; b = bot[:-1, 1:]; c = bot[1:, :-1]; d = bot[1:, 1:]
    tris.append(np.stack([a, b, c], -2).reshape(-1, 3, 3))
    tris.append(np.stack([b, d, c], -2).reshape(-1, 3, 3))
    # side walls
    tris.append(wall(top[0], bot[0], True))          # north edge (x increasing)
    tris.append(wall(top[-1], bot[-1], False))       # south edge
    tris.append(wall(top[:, 0], bot[:, 0], False))   # west edge (y decreasing)
    tris.append(wall(top[:, -1], bot[:, -1], True))  # east edge
    T = np.concatenate(tris).astype(np.float32)

    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    rec = np.zeros(len(T), dtype=[('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    rec['n'] = n; rec['v'] = T
    def write(f):
        f.write(title.encode('ascii', 'replace')[:80].ljust(80, b' '))
        f.write(struct.pack('<I', len(T)))
        f.write(rec.tobytes())
    _atomic_write(path, write)

    # sanity: signed volume positive => outward normals
    vol = np.einsum('ij,ij->i', T[:, 0], np.cross(T[:, 1], T[:, 2])).sum() / 6
    print(f'triangles={len(T)} size={SIZE_MM:.1f}x{SIZE_MM:.1f}mm height={z.max():.1f}mm volume={vol/1000:.1f}cm3')
    return z


def hillshade(dem, res, path):
    """Write a hillshade PNG of the DEM and return it as a PIL image."""
    gy, gx = np.gradient(dem, res)
    slope = np.arctan(np.hypot(gx, gy)); aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(315), np.radians(45)
    hs = np.sin(alt)*np.cos(slope) + np.cos(alt)*np.sin(slope)*np.cos(az - aspect)
    img = Image.fromarray((np.clip(hs, 0, 1)*255).astype(np.uint8))
    _atomic_write(path, lambda f: img.save(f, format='PNG'))
    return img


def surface_figure(heights, place, exag, title, step=2, ndvi_layers=()):
    """Interactive 3D view of the model surface at its printed proportions, in longitude, latitude and elevation.

    heights is the GRID x GRID elevation grid in metres (from grid_heights). ndvi_layers is a list of
    (NDVI array, scene info) pairs; each gets a button in the view that colours the surface by that NDVI
    instead of by elevation.
    """
    zs = np.round(heights[::step, ::step])
    lon, lat = (np.round(a[::step, ::step], 5) for a in grid_lonlat(place))   # 5 decimals is about 1 m
    hover = 'Elevation %{z:,.0f} m<br>Latitude %{y:.4f}°<br>Longitude %{x:.4f}°'
    layers = [('Elevation', 'Coloured by elevation',
               dict(colorscale=TERRAIN_COLORS, showscale=False, hovertemplate=hover + '<extra></extra>'))]
    for ndvi, info in ndvi_layers:
        c = ndvi[::step, ::step]
        c = np.round(np.where(np.isnan(c), np.nanmedian(c), c), 2)   # fill the rare pixel no scene covered
        note = f", {info['note']}" if info.get('note') else ''
        layers.append((f"NDVI {max(info['dates'])[:4]}", f'NDVI from {ndvi_label(info)}{note}',
                       dict(surfacecolor=c, colorscale=NDVI_COLORS, cmin=NDVI_RANGE[0], cmax=NDVI_RANGE[1],
                            colorbar=dict(title='NDVI', len=0.6, thickness=14),
                            hovertemplate=hover + '<br>NDVI %{surfacecolor:.2f}<extra></extra>')))
    # The box is square, and as tall as the printed model relative to its width, so the view has the
    # print's proportions. Scaled like Plotly's own 'data' aspect mode (x * y * z = 1), so the camera frames it the same.
    height = max(float(zs.max() - zs.min()), 1.0) * exag / (PLACES[place][2] * 1000)
    k = height ** (-1 / 3)
    def heading(subtitle):
        return f'{title}<br><sub>{subtitle}</sub>'
    # One surface per layer, and the buttons show one at a time. (Changing a single surface's colours
    # from a button leaves Plotly's hover without the NDVI in Panel.)
    fig = go.Figure([go.Surface(x=lon, y=lat, z=zs, **style, visible=i == 0, name=label,
                                lighting=dict(ambient=0.5, diffuse=0.8, roughness=0.9, specular=0.1))
                     for i, (label, _, style) in enumerate(layers)])
    fig.update_layout(title=heading(layers[0][1]), margin=dict(l=30, r=10, t=60, b=10),
                      scene=dict(aspectmode='manual', aspectratio=dict(x=k, y=k, z=k * height),
                                 xaxis=dict(title='Longitude', tickformat='.2f', ticksuffix='°'),
                                 yaxis=dict(title='Latitude', tickformat='.2f', ticksuffix='°'),
                                 zaxis=dict(title='Elevation (m)', tickformat=',.0f'),
                                 camera=dict(eye=dict(x=-1.25, y=-1.6, z=0.95), center=dict(x=0, y=0, z=-0.3))))
    if len(layers) > 1:
        # switching happens in the browser, so it's instant and keeps the camera where the user left it
        fig.update_layout(updatemenus=[dict(
            type='buttons', direction='right', active=0, showactive=True, x=0, xanchor='left', y=1.0, yanchor='top',
            buttons=[dict(label=label, method='update',
                          args=[{'visible': [j == i for j in range(len(layers))]}, {'title.text': heading(subtitle)}])
                     for i, (label, subtitle, _) in enumerate(layers)])])
    return fig


def make_model(place, exag, stl_dir=STL_DIR, preview_dir=PREVIEW_DIR, dem_dir=DEM_DIR):
    """Download the DEM for a place and write its STL and hillshade preview."""
    os.makedirs(stl_dir, exist_ok=True)
    os.makedirs(preview_dir, exist_ok=True)
    name = model_name(place, exag)
    dem, res = download_dem(place, dem_dir)
    print(f'{place}: {PLACES[place][2]:g} x {PLACES[place][2]:g} km, '
          f'elevation {dem.min():.0f}-{dem.max():.0f} m, exaggeration {exag:g}x')
    z = write_stl(dem, PLACES[place][2] * 1000, exag, os.path.join(stl_dir, f'{name}.stl'),
                  f'{place} {exag:g}x terrain - USGS 3DEP')
    hillshade(dem, res, os.path.join(preview_dir, f'preview_{name}.png'))
    print(f'Saved {name}.stl and preview_{name}.png')
    return name, z


def png_bytes(img, max_px=900):
    """A downscaled PNG copy of a PIL image, for display."""
    im = img.copy()
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()
