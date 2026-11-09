"""Web app for building 3D-printable terrain models.

Run it with:  python app.py
It opens in a new browser tab. On GeoLab, open the URL it prints instead.
"""
import argparse, asyncio, errno, html, logging, os, re, sys

try:
    import panel as pn
    import mailer
    import terrain
except ModuleNotFoundError as e:
    mod = (e.name or '').split('.')[0]
    if mod in ('mailer', 'terrain'):
        sys.exit(f'{mod}.py is missing. Keep app.py, terrain.py and mailer.py in the same folder.')
    pkg = {'PIL': 'pillow'}.get(mod, mod)
    in_venv = sys.prefix != getattr(sys, 'base_prefix', sys.prefix)   # --user isn't allowed inside a venv
    py = f'"{sys.executable}"' if ' ' in sys.executable else (sys.executable or 'python')
    sys.exit(f"Missing Python package '{pkg}'. Install it with:\n"
             f"    {py} -m pip install{'' if in_venv else ' --user'} {pkg}\n"
             "then start the app again the same way.")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'output')    # STLs, zips and previews, served at files/
CACHE_DIR = os.path.join(HERE, 'cache')   # downloaded DEMs

# On JupyterHub, .../proxy/<port> without a trailing slash is proxied as '/', so the page's relative
# resource URLs resolve one level too high and the app stays blank. Reload with the slash added.
# (The '//' in the data URL keeps Panel from putting the relative path in front of it.)
ADD_SLASH_JS = ("data:text/javascript,/*add-slash*//*//*/if(!location.pathname.endsWith('/'))"
                "location.replace(location.pathname+'/'+location.search+location.hash)")

DOWNLOAD_STYLE = ('display:inline-block;padding:8px 14px;border-radius:4px;background:#2e7d32;'
                  'color:#fff;text-decoration:none;font-weight:600;')

pn.extension('plotly', notifications=True)


def md_text(text):
    """Escape text so a Markdown pane shows it literally (no links, emphasis, code or HTML)."""
    return re.sub(r'([\\`*_{}\[\]()#+\-.!|~<>&@:/=$^\'"%?])', r'\\\1', ' '.join(str(text).split()))


def create_app():
    place = pn.widgets.Select(label='Place', options=list(terrain.PLACES), value='Mount St Helens')
    exag = pn.widgets.FloatSlider(label='Vertical exaggeration', start=1.0, end=4.0, step=0.25, value=2.0)
    area = pn.pane.Markdown(margin=(0, 10))
    build = pn.widgets.Button(label='Build model', color='primary', icon='cube')
    download = pn.pane.HTML('', margin=(10, 10))
    status = pn.pane.Markdown('Choose a place and exaggeration, then click **Build model**.', margin=(0, 10))
    view3d = pn.pane.Plotly(None, height=650, sizing_mode='stretch_width')
    preview = pn.pane.PNG(None, sizing_mode='scale_width', max_width=900)

    email_to = pn.widgets.TextInput(label='Email address', placeholder='you@example.org')
    send = pn.widgets.Button(label='Email zipped STL', color='success', icon='mail')
    email_note = pn.pane.Markdown('Builds the model if needed, zips the STL (about 15 MB) and emails it.',
                                  margin=(0, 10), styles={'font-size': '0.9em'})
    try:
        smtp = mailer.smtp_settings()
        smtp_problem = None if smtp else "Email isn't set up on this server (see the README)."
    except ValueError as ex:
        smtp, smtp_problem = None, f'Email is misconfigured: {md_text(ex)}'
    if smtp_problem:
        email_to.disabled = send.disabled = True
        email_note.object = smtp_problem

    controls = (place, exag, build, email_to, send)
    busy = {'now': False}
    built = {'key': None, 'stl': None, 'summary': None}   # the model this session's views currently show

    def set_busy(on):
        busy['now'] = on
        if on:
            pn.state.notifications.clear()   # drop messages from earlier attempts
        for w in controls:
            w.disabled = on or (smtp is None and w in (email_to, send))

    def show_area(event=None):
        lat, lon, size_km = terrain.PLACES[place.value]
        area.object = f'{size_km:g} × {size_km:g} km area centred on {lat:.4f}, {lon:.4f}'

    def show_error(what, msg):
        status.object = f'**{what} failed:** {md_text(msg)}'
        pn.state.notifications.error(f'{what} failed: {html.escape(msg)}', duration=0)

    def current_selection():
        p, e = place.value, exag.value
        # The browser enforces these, but a websocket client can send any value.
        if p not in terrain.PLACES or not (exag.start <= e <= exag.end):
            raise ValueError('invalid place or exaggeration')
        return p, round(e / exag.step) * exag.step

    async def build_model(p, e):
        """Build the model for a place and exaggeration, update the views, and return the STL path."""
        name = terrain.model_name(p, e)
        stl = os.path.join(OUT_DIR, f'{name}.stl')
        png = os.path.join(OUT_DIR, f'preview_{name}.png')
        built['key'] = None        # views are cleared below; a failed build leaves nothing to reuse
        download.object = ''
        view3d.object = None
        preview.object = None
        status.object = f'Downloading elevation data for {md_text(p)}…'
        dem, res = await asyncio.to_thread(terrain.download_dem, p, CACHE_DIR)
        status.object = 'Building the STL…'
        os.makedirs(OUT_DIR, exist_ok=True)
        z = await asyncio.to_thread(terrain.write_stl, dem, terrain.PLACES[p][2] * 1000, e, stl,
                                    f'{p} {e:g}x terrain - USGS 3DEP')
        status.object = 'Rendering the previews…'
        img = await asyncio.to_thread(terrain.hillshade, dem, res, png)
        fig = await asyncio.to_thread(terrain.surface_figure, z, f'{p}, {e:g}x exaggeration')
        view3d.object = fig
        preview.object = await asyncio.to_thread(terrain.png_bytes, img)
        size_km = terrain.PLACES[p][2]
        mb = os.path.getsize(stl) / 1e6
        download.object = (f'<a href="files/{name}.stl" download="{name}.stl" style="{DOWNLOAD_STYLE}">'
                           f'Download {name}.stl ({mb:.0f} MB)</a>')
        summary = (f'**{md_text(p)}**, {e:g}× exaggeration\n\n'
                   f'- Area: {size_km:g} × {size_km:g} km\n'
                   f'- Elevation: {dem.min():,.0f}–{dem.max():,.0f} m\n'
                   f'- Model: {terrain.SIZE_MM:.0f} × {terrain.SIZE_MM:.0f} mm, {z.max():.1f} mm tall')
        status.object = summary
        built.update(key=(p, e), stl=stl, summary=summary)
        return stl, summary

    async def on_build(event):
        if busy['now']:            # clicks that reached the server before the button was disabled
            return
        set_busy(True)
        build.loading = True
        try:
            await build_model(*current_selection())
        except Exception as ex:
            show_error('Build', str(ex) or type(ex).__name__)
        finally:
            build.loading = False
            set_busy(False)

    async def on_send(event):
        if busy['now'] or smtp is None:
            return
        to = email_to.value.strip()
        pn.state.notifications.clear()
        if not mailer.valid_email(to):
            show_error('Email', f'{to!r} is not a valid email address.')
            return
        set_busy(True)
        send.loading = True
        try:
            p, e = current_selection()
            if built['key'] == (p, e) and os.path.exists(built['stl']):
                stl, summary = built['stl'], built['summary']      # already built and shown
            else:
                try:
                    stl, summary = await build_model(p, e)
                except Exception as ex:
                    show_error('Build', str(ex) or type(ex).__name__)
                    return
            status.object = summary + '\n\nZipping the STL…'
            zip_path = await asyncio.to_thread(mailer.zip_file, stl)
            status.object = summary + f'\n\nEmailing {os.path.basename(zip_path)} to {md_text(to)}…'
            body = (f'Attached is a 3D-printable terrain model of {p} at {e:g}x vertical exaggeration.\n\n'
                    f'Unzip {os.path.basename(zip_path)} to get {os.path.basename(stl)}, a '
                    f'{terrain.SIZE_MM:.0f} x {terrain.SIZE_MM:.0f} mm (6 x 6 in) binary STL. '
                    'Print it flat, base down, with no supports.\n\n'
                    'Elevation data: USGS 3D Elevation Program (3DEP).\n')
            await asyncio.to_thread(mailer.send_zip, to, zip_path, f'3D terrain model: {p} ({e:g}x)', body, smtp)
            status.object = summary + f'\n\nEmailed {os.path.basename(zip_path)} to {md_text(to)}.'
            pn.state.notifications.success(f'Emailed {os.path.basename(zip_path)} to {html.escape(to)}', duration=8000)
        except Exception as ex:
            show_error('Email', mailer.describe_error(ex, smtp))
        finally:
            send.loading = False
            set_busy(False)

    place.param.watch(show_area, 'value')
    build.on_click(on_build)
    send.on_click(on_send)
    show_area()

    template = pn.template.FastListTemplate(
        title='3D Terrain Model Builder',
        sidebar=[place, area, exag, build, download,
                 pn.layout.Divider(), pn.pane.Markdown('#### Email the model', margin=(0, 10)),
                 email_to, send, email_note,
                 pn.layout.Divider(), status],
        main=[pn.Tabs(('3D view', view3d), ('Hillshade', preview), dynamic=False)],
        sidebar_width=330,
    )
    template.config.js_files['add_slash'] = ADD_SLASH_JS
    return template


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=5006, help='port to serve on (default 5006)')
    parser.add_argument('--no-show', action='store_true', help="don't open a browser tab")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    hub_prefix = os.environ.get('JUPYTERHUB_SERVICE_PREFIX')   # set on GeoLab and other JupyterHubs
    if hub_prefix:
        # behind JupyterHub's proxy, connections come from the hub's address; '*' is intended here
        # (the hub authenticates users), so silence bokeh's warning about it
        origins = '*'
        logging.getLogger('bokeh.server.util').setLevel(logging.ERROR)
    else:
        # locally, only pages served by this app may connect
        origins = [f'localhost:{args.port}', f'127.0.0.1:{args.port}']
    asyncio.set_event_loop(asyncio.new_event_loop())   # avoids a DeprecationWarning from tornado on Python 3.12
    try:
        server = pn.serve(create_app, port=args.port, address='127.0.0.1', websocket_origin=origins,
                          static_dirs={'files': OUT_DIR}, show=not (args.no_show or hub_prefix),
                          title='3D Terrain Model Builder', verbose=False, start=False)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        sys.exit(f'\nPort {args.port} is already in use. If the app is already running in another terminal, '
                 f'use that one or stop it with Ctrl+C.\nOr start this one on another port:  '
                 f'python app.py --port {args.port + 1}\n')
    if hub_prefix:
        print(f'\nOpen the app in a new browser tab at:\n  <your GeoLab address>{hub_prefix}proxy/{args.port}/\n'
              f'For example: https://geolab.earthscope.cloud{hub_prefix}proxy/{args.port}/\n', flush=True)
    else:
        print(f'\nThe app is at http://localhost:{args.port}/  (stop it with Ctrl+C)\n', flush=True)
    server.start()
    server.io_loop.start()
