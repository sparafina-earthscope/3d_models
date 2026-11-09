"""Build 3D-printable terrain models (STL) from USGS 3DEP elevation data."""
import http.client, io, math, os, re, socket, ssl, struct, tempfile, threading, time, urllib.error, urllib.request

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

# Data folders in the project folder (the parent of app/), shared with the notebook
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEM_DIR = os.path.join(ROOT, 'dem')            # downloaded DEMs
STL_DIR = os.path.join(ROOT, 'stl')            # STLs and zips
PREVIEW_DIR = os.path.join(ROOT, 'preview')    # hillshade previews

TERRAIN_COLORS = [[0.0, '#4a7a46'], [0.35, '#9a9a5a'], [0.6, '#8c6d4f'], [0.85, '#b8a898'], [1.0, '#ffffff']]


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


def wall(t_edge, b_edge, flip):
    t0, t1, b0, b1 = t_edge[:-1], t_edge[1:], b_edge[:-1], b_edge[1:]
    if flip:
        return np.concatenate([np.stack([t0, t1, b0], 1), np.stack([t1, b1, b0], 1)])
    return np.concatenate([np.stack([t0, b0, t1], 1), np.stack([t1, b0, b1], 1)])


def write_stl(dem, size_m, exag, path, title):
    """Turn a DEM into a watertight binary STL with a flat base. Returns the surface heights in mm."""
    img = Image.fromarray(dem.astype(np.float32)).resize((GRID, GRID), Image.BILINEAR)
    z_m = np.array(img, dtype=np.float64)
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


def surface_figure(z, title, step=2):
    """Interactive 3D view of the model surface, in mm at printed proportions."""
    zs = z[::step, ::step]
    xs = np.linspace(0, SIZE_MM, zs.shape[1])
    ys = np.linspace(SIZE_MM, 0, zs.shape[0])
    fig = go.Figure(go.Surface(x=xs, y=ys, z=zs, colorscale=TERRAIN_COLORS, showscale=False,
                               lighting=dict(ambient=0.5, diffuse=0.8, roughness=0.9, specular=0.1),
                               hovertemplate='x %{x:.0f} mm<br>y %{y:.0f} mm<br>z %{z:.1f} mm<extra></extra>'))
    fig.update_layout(title=title, margin=dict(l=30, r=10, t=40, b=10),
                      scene=dict(aspectmode='data', xaxis_title='x (mm)', yaxis_title='y (mm)',
                                 zaxis_title='z (mm)', camera=dict(eye=dict(x=-1.25, y=-1.6, z=0.95), center=dict(x=0, y=0, z=-0.3))))
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
