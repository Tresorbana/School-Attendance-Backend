# NBIS binaries

This folder needs two NIST Biometric Image Software (NBIS) binaries:

```
mindtct.exe     ← minutiae extractor
bozorth3.exe    ← fingerprint matcher
```

Drop both here and restart the backend. On startup you'll see:

```
[INFO] app: Matcher: NBIS (bozorth3) — biometric-grade
```

## Where to get them

NIST stopped hosting NBIS on its own site around 2018. The source code is in the
public domain. Three options, fastest first:

### 1. Pre-built Windows binaries (fastest)

Search GitHub for a recent NBIS Windows release. Look for repos with names like
`nbis-windows`, `nbis-bins`, `NBIS-5.0.0`. Pick one that:

- Was built within the last 2 years
- Has both `mindtct.exe` and `bozorth3.exe` in the release ZIP
- Shows reasonable star/fork count (suggests other users trust it)

Copy the two .exe files here.

### 2. WSL (Windows Subsystem for Linux) — also fast

If you have WSL installed:

```bash
sudo apt update
sudo apt install build-essential cmake
# Clone an NBIS mirror, e.g.
git clone https://github.com/lessandro/nbis.git
cd nbis
./setup.sh /usr/local
make config
make it
sudo make install LIBDIR=/usr/local/lib INCLUDEDIR=/usr/local/include
```

Then copy `/usr/local/bin/mindtct` and `/usr/local/bin/bozorth3` into the
Windows side of this folder, renaming to `.exe`.

Set `NBIS_PATH` in `backend/.env` to point at the Linux binaries directly if
you'd prefer running the backend under WSL.

### 3. Build from source on Windows

You'll need Visual Studio Build Tools + Perl + a few NBIS dependencies. NIST's
NBIS 5.0.0 source archive is the canonical starting point. This is real work —
budget half a day. Not recommended unless 1 and 2 fail.

## Sanity check

After placing the binaries:

```powershell
cd D:\NemaTechnologies\Attendance\backend
python -c "import sys; sys.path.insert(0,'.'); from pipeline.nbis import is_available; print('NBIS ready:', is_available())"
```

Expected output: `NBIS ready: True`.

Then restart the backend and watch for the `Matcher: NBIS (bozorth3) — biometric-grade`
log line.

## Override the search path

If you keep the binaries elsewhere on disk, set:

```
NBIS_PATH=C:\path\to\nbis
```

in `backend/.env`. The startup loader will look there first.
