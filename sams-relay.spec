# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the SAMS relay (VPS split edition).
# Bundles only relay.py + its dependencies — no fingerprint pipeline,
# no database models, no heavy ML imports.
#
# Run:  pyinstaller sams-relay.spec
# Output: dist/sams-relay/sams-relay.exe  (one-folder bundle)

from pathlib import Path

HERE = Path(SPECPATH)

a = Analysis(
    [str(HERE / 'relay.py')],
    pathex=[str(HERE)],
    binaries=[
        (str(HERE / 'bridge' / 'FingerprintBridge.exe'), 'bridge'),
        (str(HERE / 'bridge' / 'DPFPDevNET.dll'),        'bridge'),
        (str(HERE / 'bridge' / 'DPFPShrNET.dll'),        'bridge'),
    ],
    datas=[],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'PIL._tkinter_finder',
        'scanner',
        'scanner.bridge_client',
    ],
    excludes=[
        'torch', 'torchvision', 'torchaudio', 'transformers',
        'tensorflow', 'keras', 'cv2', 'numpy', 'sklearn',
        'sqlalchemy', 'psycopg2', 'alembic',
        'pytest', 'IPython', 'notebook', 'matplotlib',
        'tkinter', '_tkinter',
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='sams-relay',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='sams-relay',
)
