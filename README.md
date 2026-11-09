# 3D-Printable Terrain Models from USGS 3DEP

Build 6 × 6 inch, 3D-printable terrain models of well-known US mountains from USGS 3DEP elevation data. You pick a place and a vertical exaggeration, and you get a solid, watertight STL with a flat base, a hillshade preview, and an interactive 3D view of the model.

There are two ways to use it:

- **The web app** (in `app/`) opens in its own browser tab, with the controls in a sidebar and the results beside them. You can download the STL or email it, zipped, to any address.
- **The notebook** (`make_stl.ipynb`) does the same builds from a form inside Jupyter.

Both produce identical STL files.

## Contents

| File | Description |
|---|---|
| `app/app.py` | The web app. Run `python app/app.py` from the project folder. |
| `app/terrain.py` | Model-building code used by the app: places, DEM download, STL, hillshade, 3D view. |
| `app/mailer.py` | Zips an STL and emails it. Used by the app. |
| `make_stl.ipynb` | The notebook version. |
| `environment.yml` | Conda environment for GeoLab (or any conda install). |
| `requirements.txt` | The same packages for `pip`. |
| `dem/` | Elevation data (DEMs) downloaded from USGS 3DEP, reused by later builds. |
| `stl/` | Built STL files, and zipped copies made for email. |
| `preview/` | Hillshade previews of the built models. |
| `tiff/` | Other GeoTIFF files. The app and the notebook don't write to it. |
| `emails.json` | A record of the emails the app has tried to send, created with the first one (see [Email log](#email-log)). |

The app and the notebook create `dem/`, `stl/`, `preview/` and `tiff/` when they start, if they don't exist. Git doesn't keep empty folders, so a fresh clone doesn't have them.

## Running the app

### On your computer

Create the environment once, then start the app:

```bash
conda env create -f environment.yml
conda activate terrain-3d-model
python app/app.py
```

The app opens in a new browser tab at `http://localhost:5006/`, and the terminal prints that address too. To use another port, run `python app/app.py --port 5010`. To stop it, press **Ctrl+C** in the terminal. If it says the port is already in use, the app is probably still running in another terminal: use that one, or start this one on another port.

### On GeoLab

1. Log in to GeoLab and launch a server with the default **GeoLab** image. The smallest server size is enough: a model uses about 0.5 GB of memory and builds in seconds, plus the time to download the DEM.
2. Open a terminal (**File → New → Terminal**) and clone the repository into your home directory:

   ```bash
   cd ~
   git clone https://github.com/sparafina-earthscope/notebooks.git
   ```

   To get later updates, run `git pull` inside `~/notebooks`.
3. Install `tifffile`, the one package the GeoLab image doesn't include. This only needs to be done once, because `--user` installs into your home directory. The second command should print the installed version:

   ```bash
   python -m pip install --user tifffile
   python -c "import tifffile; print(tifffile.__version__)"
   ```

   If a GeoLab image update changes the Python version, run the install again.

4. Start the app:

   ```bash
   cd ~/notebooks/3d_models
   python app/app.py
   ```

   It prints the address to open, for example:

   ```
   Open the app in a new browser tab at:
     <your GeoLab address>/user/<your-username>/proxy/5006/
   For example: https://geolab.earthscope.cloud/user/<your-username>/proxy/5006/
   ```

5. Open that address in a new browser tab. You must be logged in to GeoLab in the same browser. Use the `/proxy/5006/` form shown; the `/proxy/absolute/5006/` form doesn't work with this app.

The app runs as long as the terminal command is running. Stop it with **Ctrl+C**. When you're done, stop your server (**File → Hub Control Panel → Stop My Server**) so it doesn't use up your compute quota. GeoLab also stops servers after 60 minutes of inactivity.

### Using the app

1. Choose a **Place** and set the **Vertical exaggeration** in the sidebar.
2. Click **Build model**. The sidebar shows a summary, and the main area shows the model:
   - **3D view** draws the model at its printed proportions, so it shows how the exaggeration will look on the print. Drag to rotate, scroll to zoom, and right-drag to pan.
   - **Hillshade** shows a shaded-relief map of the area.
3. Click **Download** to save the STL (about 50 MB), or email it (see below).

Built STLs are saved in `stl/`, previews in `preview/`, and downloaded elevation data in `dem/`, all in the project folder (next to `app/`). Each model takes about 50 MB in `stl/`, plus 15 MB for the zip if you email it, so delete old ones when you no longer need them.

## Emailing models

Under **Email the model**, type an address and click **Email zipped STL**. The app builds the model for the current place and exaggeration if needed, zips the STL (about 15 MB), and emails it as an attachment.

### Setting up email

The app sends mail through an SMTP server you provide. Until one is configured, the email section is greyed out with the note "Email isn't set up on this server".

Put the settings in `~/.config/terrain-app/smtp.env`, outside the repository so the password is never committed:

```bash
mkdir -p ~/.config/terrain-app
nano ~/.config/terrain-app/smtp.env
chmod 600 ~/.config/terrain-app/smtp.env
```

For example, to send from a Gmail account:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-app-password
```

Gmail needs an [app password](https://support.google.com/accounts/answer/185833), not your regular password. That requires 2-Step Verification on the account. For an organization's mail server, ask your IT group for the host, port and security setting.

| Setting | Default | Description |
|---|---|---|
| `SMTP_HOST` | (required) | Mail server, e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` (`465` with `ssl`) | Mail server port |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl`, or `none` |
| `SMTP_USER` | | Login name, if the server needs one |
| `SMTP_PASSWORD` | | Login password |
| `SMTP_FROM` | `SMTP_USER` | Sender address |
| `SMTP_MAX_MB` | `25` | Largest email the app will send, in MB |

The same names also work as environment variables, which take precedence over the file. Restart the app after changing the settings.

### Attachment size

A zipped model is about 15 MB, which becomes about 20 MB once attached. That's within Gmail's 25 MB limit. Some mail systems allow only 20 MB or less. If yours does, set `SMTP_MAX_MB` to its limit: the app then reports that the model is too large, instead of sending an email that bounces.

### Email log

The app records every email it tries to send in `emails.json` in the project folder (next to `app/`), as a JSON list:

```json
[
  {
    "time": "2026-11-05T14:02:31-08:00",
    "to": "someone@example.org",
    "place": "Mt Hood",
    "exaggeration": 2.0,
    "file": "mt_hood_2x.zip",
    "status": "sent"
  }
]
```

`status` is `sent` or `failed`. A failed email also has an `error` field with the message the app showed. Emails that are never attempted, because the address is invalid or the model fails to build, aren't recorded.

The file holds people's email addresses, so it's readable only by you. Don't commit it to the repository. To start a new log, delete or rename the file. The app creates a new one with the next email.

## Using the notebook

### Setting up the environment on GeoLab

Clone the repository as in [On GeoLab](#on-geolab) above. The default GeoLab image already includes everything the notebook needs except `tifffile`. You have two options.

**Option A: persistent environment (recommended).** Build it once and it stays available across sessions, because it is stored in your home directory. In the terminal, run:

```bash
cd ~/notebooks/3d_models
conda env create -f environment.yml
conda activate terrain-3d-model
python -m ipykernel install --user --name terrain-3d-model --display-name "Python (terrain-3d-model)"
```

The build takes a few minutes. Then open the notebook, go to **Kernel → Change Kernel…**, and select **Python (terrain-3d-model)**. You need to choose the kernel once for each notebook.

**Option B: quick install.** Run `python -m pip install --user tifffile` in a terminal once, then restart the notebook's kernel.

### Building a model

1. Open `make_stl.ipynb` and choose **Kernel → Restart Kernel and Run All Cells**.
2. A form appears at the bottom of the notebook. Choose a **Place**, set the **Exaggeration**, and click **Build model**.
3. When the build finishes, the form shows a summary, the hillshade preview, and the 3D view. The STL is saved in `stl/` and the preview in `preview/`.

To build another model, change the form and click **Build model** again. To download the STL, open the `stl` folder in the File Browser, right-click the file and choose **Download**.

## Choosing a place and exaggeration

### Places

Each place is a square area centred on the feature and sized to include the whole mountain or range. Every model is 6 × 6 in (152.4 mm), so larger areas are shown at a smaller scale.

| Place | Area |
|---|---|
| Mount St Helens | 15 × 15 km |
| Mt Rainier | 30 × 30 km |
| Mt Whitney | 14 × 14 km |
| Grand Tetons | 30 × 30 km |
| Mt Hood | 20 × 20 km |
| Mt Shasta | 28 × 28 km |
| Pikes Peak | 20 × 20 km |

### Exaggeration

Exaggeration multiplies the heights. `1.0` is true scale, and the slider goes from `1.0` to `4.0`. Most terrain looks flat when printed at true scale, so `1.5`–`3` is typical:

- **Steep places in small areas** (Mt Whitney, Mount St Helens) already have tall relief. Use `1.5`–`2`. Mt Whitney at `2` is about 63 mm tall.
- **Large areas** (Mt Rainier, Grand Tetons, Mt Shasta) look flatter because they are printed at a smaller scale. Use `2.5`–`3.5`.

Output files are named after the place and exaggeration, for example `stl/mt_rainier_2.5x.stl` and `preview/preview_mt_rainier_2.5x.png`, so earlier models aren't overwritten.

### Adding a place

Each entry in the places list is the centre latitude and longitude and the width of the square area in km. For the app, edit `PLACES` in `app/terrain.py` and restart the app. For the notebook, edit the **Places** cell and re-run the cells.

```python
PLACES = {
    'Mount St Helens': (46.1912, -122.1944, 15),
    ...
    'Mt Baker':        (48.7768, -121.8145, 20),   # added
}
```

3DEP covers the United States and its territories. A place outside that coverage, such as one over the ocean, fails with "No elevation data". To check the framing, build a model and look at the hillshade: if the feature runs off an edge, increase the width or move the centre.

### Model parameters

These are at the top of `app/terrain.py` for the app, and in the **Model parameters** cell for the notebook. The defaults work well for a typical FDM printer.

| Parameter | Default | Description |
|---|---|---|
| `SIZE_MM` | `152.4` | Width and depth of the model, in mm (152.4 mm = 6 in). |
| `GRID` | `500` | Number of samples per side. Higher values give more detail but larger files: 500 gives about 1 million triangles and a 50 MB STL. Detail finer than about 0.3 mm is lost on a 0.4 mm nozzle. |
| `BASE_MM` | `3.0` | Thickness of the solid base below the lowest point, in mm. |

### How the DEM download works

The DEM is downloaded from the [USGS 3DEP ImageServer](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer) in the UTM zone of the place:

- **Resolution:** 10 m for areas up to 20 km wide. Larger areas use a coarser resolution, such as 15 m for 30 km, to keep downloads near 2000 × 2000 px. That is still finer than the model's grid can show.
- **Caching:** the DEM is saved in `dem/` under a name built from the place, centre, and width, such as `dem_mt_rainier_46.8529_-121.7604_30km_15m.tif`. The app and the notebook share these files. Rebuilding the same place with a different exaggeration reuses it. Editing a place's centre or width downloads a new one.
- **Download failures:** the server sometimes returns errors or stalls. The app and the notebook each try the download up to 5 times. If a build still fails, wait a few minutes and click **Build model** again.

## Printing tips

- **Setup:** print flat, base down, with no supports. The terrain has no overhangs.
- **Layers:** use 0.12 mm layers, or variable layer height if your slicer has it, to reduce visible steps on gentle slopes.
- **Shell and infill:** 3 walls, 5–6 top layers, and 10–15% infill.
- **Adhesion:** a brim helps stop the corners of a large, flat base from lifting.
- **Filament:** matte grey, white, or tan PLA shows relief best.
