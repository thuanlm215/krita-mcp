# Development

Work from a checkout when you change the plugin, the native helper, or the tests. `uvx` runs the packaged server. It is not the edit loop.

## Layout

```
mcp_server.py              stdio server, CLI, and plugin installer
painter_schema.py          stroke, target, and frame schemas
pyproject.toml             package metadata for uvx / pip
install.sh / install.ps1   wrappers around `mcp_server.py install`
build_native.py            content-addressed Linux helper build
pykrita/krita_mcp/         plugin loaded by Krita
native/                    GPL helper and vendored headers
examples/                  posters, workflows, and review players
scripts/                   private Krita/Xvfb runners
tests/                     live and offline suites
```

`scripts/dev_reload.py` starts a fresh private installation. It does not copy into the desktop plugin and it does not execute arbitrary Python inside Krita. Deploy to the desktop by saving work, closing Krita, running the installer, and starting Krita again.

## Native helper

On Linux with Krita 6.0.4:

```bash
python3 build_native.py
python3 mcp_server.py install
```

The build writes a checksummed `.so` and `native_manifest.json` under `pykrita/krita_mcp/`. Those files are gitignored and are excluded from the Python wheel. Dependencies, the ABI pin, and the GPL/LGPL split are in [native/README.md](../native/README.md).

Do not overwrite a `.so` that a running Krita has mapped. Rebuild, then start a new Krita process.

## Tests

```bash
python3 mcp_server.py --selftest
python3 -m unittest tests.test_mcp
python3 -m unittest -v tests.test_core tests.test_shutdown tests.test_transport
python3 scripts/run_isolated_tests.py
python3 examples/example_poster.py
python3 examples/example_banner.py
```

`tests/test_mcp.py` speaks JSON-RPC to a real `mcp_server.py` process, so a pass covers the handshake and the basic operation set against live Krita. Workflow, advanced, painter, and repair suites cover their fixtures. They do not cover every preset, mask, or platform.

Isolated examples:

```bash
python3 scripts/run_isolated_tests.py --output artifacts/my-warp -- \
  python3 examples/example_deformation_workflow.py --output artifacts/my-warp
```

The runner uses a private home, Xvfb, and a copy of this checkout. It does not modify the desktop Krita installation. Do not run two UI suites at the same time.

Offline suites do not need Krita. `test_shutdown.py` needs system PyQt6. The painter demos in a full isolated run also expect Pillow and ffmpeg when they encode review media. The MCP server stays on the standard library.

## Package

The wheel contains `mcp_server.py`, `painter_schema.py`, and `pykrita/`. It does not contain the native helper.

```bash
uv build
uvx --from dist/krita_mcp-1.5.1-py3-none-any.whl krita-mcp --list-tools
```

## Release

Publishing uses PyPI trusted publishing. GitHub Actions requests a short-lived token. There is no API token in the repository.

The workflow is `.github/workflows/publish.yml`. It runs when a GitHub release is published, and only if the tag matches `version` in `pyproject.toml`. Version 1.5.1 is the current release. PyPI keeps every uploaded filename, including files from a deleted release, so the next release needs a higher version and a matching tag.

Trusted publishing is already registered for this repository: project `krita-mcp`, workflow `publish.yml`, environment `pypi`. Clients install with `uvx krita-mcp`. The plugin install remains `krita-mcp install`, because Krita loads Python from its own resource folder.
