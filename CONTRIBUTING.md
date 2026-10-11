# Contributing

Issues and pull requests are welcome. The installable package is the Python server and the Krita plugin. The native helper is built on the machine that runs Krita 6.0.4.

## Setup

```bash
git clone https://github.com/thuanlm215/krita-mcp.git
cd krita-mcp
python3 mcp_server.py install
```

On Linux with Krita 6.0.4, build the helper before installing when you need brush pressure, native Undo, warp, or exact frames:

```bash
python3 build_native.py
python3 mcp_server.py install
```

Do not commit `pykrita/krita_mcp/*.so`, `native_manifest.json`, `artifacts/`, or `native/build/`.

## Checks

Offline tests need PyQt6 and Pillow. They do not launch Krita.

```bash
python3 -m unittest tests.test_core tests.test_transport tests.test_shutdown tests.test_planner tests.test_feedback tests.test_isolated_log_gate
```

Live suites need a running Krita, or `python3 scripts/run_isolated_tests.py` on Linux with Xvfb. Run one UI suite at a time.

## Releases

Bump `version` in `pyproject.toml`, then publish a GitHub release whose tag is `v` plus that version. `.github/workflows/publish.yml` uploads the package to PyPI. A version that is already on PyPI cannot be uploaded again.
