# OFS

Local folder sync for Linux. Jobs are grouped by the first word of the profile name. The window is GTK 4.

## Download and run

The release is one file. It uses Python 3.11 or newer and GTK 4 from the machine.

1. Download `OFS-0.1.0-x86_64.AppImage` from the [releases](https://github.com/jitrc/open-flexible-sync/releases) page.
2. Mark it executable and start it:

```bash
chmod +x OFS-0.1.0-x86_64.AppImage
./OFS-0.1.0-x86_64.AppImage
```

Debian or Ubuntu, if the window does not open:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0
```

Fedora: `sudo dnf install python3-gobject gtk4`

The same download page has `OFS-0.1.0-linux-x86_64.tar.gz` for a machine without AppImage:

```bash
tar -xzf OFS-0.1.0-linux-x86_64.tar.gz
./OFS-0.1.0/ofs
```

`list` prints the profiles. `import` reads `~/.SuperFlexibleSynchronizer/SuperFlexibleSynchronizer.cfg` and does not change that file. Profiles are stored in `~/.config/sfs`.

## From a checkout

```bash
./launch.sh
./launch.sh list
python3 -m pytest -q
python3 scripts/build_release.py
```

The build writes `dist/OFS-0.1.0-x86_64.AppImage` and `dist/OFS-0.1.0-linux-x86_64.tar.gz`. A pushed tag `v0.1.0` runs the tests and publishes those files.

How to use the window is in [docs/usage.md](docs/usage.md). Requirements are in [docs/spec.md](docs/spec.md).
