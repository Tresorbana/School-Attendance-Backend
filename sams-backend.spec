# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the SAMS backend.
# Run:  pyinstaller sams-backend.spec
# Output: dist/sams-backend/sams-backend.exe  (one-folder bundle)
#
# The one-folder mode is intentional: it starts fast (no temp-extract step)
# and makes it easy to place .env and databases next to the executable.

import sys
from pathlib import Path

HERE = Path(SPECPATH)

a = Analysis(
    [str(HERE / 'app.py')],
    pathex=[str(HERE)],
    binaries=[
        # Include the NBIS binaries (mindtct + bozorth3) alongside the exe
        (str(HERE / 'bridge' / 'nbis' / 'mindtct.exe'),  'bridge/nbis'),
        (str(HERE / 'bridge' / 'nbis' / 'bozorth3.exe'), 'bridge/nbis'),
        # Bridge DLLs go next to FingerprintBridge.exe in bridge/
        (str(HERE / 'bridge' / 'FingerprintBridge.exe'), 'bridge'),
        (str(HERE / 'bridge' / 'DPFPDevNET.dll'),        'bridge'),
        (str(HERE / 'bridge' / 'DPFPShrNET.dll'),        'bridge'),
    ],
    datas=[
        # Pipeline configuration
        (str(HERE / 'pipeline' / 'pipeline_config.py'), 'pipeline'),
    ],
    hiddenimports=[
        # FastAPI / Uvicorn
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
        # SQLAlchemy dialects
        'sqlalchemy.dialects.sqlite',
        'sqlalchemy.dialects.postgresql',
        # PIL / Pillow
        'PIL._tkinter_finder',
        # psycopg2 (PostgreSQL)
        'psycopg2',
        # All routers (must be importable by app.py)
        'routers.auth',
        'routers.attendance',
        'routers.fingerprint',
        'routers.fingerprint_ws',
        'routers.holidays',
        'routers.people',
        'routers.reports',
        'routers.stations',
        # Services
        'services.attendance',
        'services.auth',
        'services.recognition',
        'services.reports',
        'services.template_cache',
        'services.rate_limit',
        # Models
        'models.person',
        'models.station',
        'models.attendance',
        'models.holiday',
        'models.fingerprint_template',
        # Pipeline
        'pipeline.enroll',
        'pipeline.match',
        'pipeline.nbis',
        'pipeline.minutiae',
        'pipeline.preprocess',
        'pipeline.quality',
        'pipeline.coverage',
    ],
    excludes=[
        # Exclude heavy ML deps — use NBIS matcher in production
        # If you need DINOv2/embedding matching, remove these excludes
        # and ensure torch is installed before running PyInstaller.
        'torch',
        'torchvision',
        'torchaudio',
        'transformers',
        'tensorflow',
        'keras',
        # Dev / test tools
        'pytest',
        'hypothesis',
        'IPython',
        'notebook',
        'matplotlib',
        'tkinter',
        '_tkinter',
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
    name='sams-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,           # Keep console for logging; NSSM captures stdout
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='sams-backend',    # → dist/sams-backend/
)
