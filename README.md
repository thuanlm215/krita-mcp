# Krita MCP

Give an agent a real Krita file. It can build the layer stack, select a part, turn it around a pivot, lay down a pressure stroke, step through raster frames, and look at the PNG it just made.

![A desk still life: layered lantern studies, a dashed selection around a handle, a bristle stroke, four animation cells, and a cracked pane beside a repaired one](docs/capabilities.jpg)

Read the picture from the back of the desk forward. The three sheets are a layer stack: wash, line, color. The dashed loop and the pins are a selection swung around a pivot. The bristle mark is one native stroke that starts light and ends heavy. The four cells are raster frames. The split lens is a repair: the crack on one side, the same pane whole on the other.

[thuanlm215](https://github.com/thuanlm215) maintains this project. Version **1.4.0** is published as [`krita-mcp`](https://pypi.org/project/krita-mcp/). It exposes fifty-two tools and sixty-one bridge operations. The tool list is in [docs/usage.md](docs/usage.md). The call order for a pose, a warp, a frame, and a repair is in [docs/workflows.md](docs/workflows.md).

## What has to be installed

Krita 5.2 or newer covers documents, layers, geometric drawing, filters, and export. Brush pressure, native pixel Undo, warp, batches, and exact raster frames need **Linux, Krita 6.0.4, Qt6, little-endian**, plus the helper in [native/README.md](native/README.md). Anywhere else those calls return `unsupported`.

The server is Python 3.8 or newer and the standard library. Krita loads a plugin in its own process. The MCP client runs `krita-mcp` beside it. Install the plugin once.

Close Krita before the installer writes `kritarc`. Krita rewrites that file on exit.

```bash
uvx krita-mcp install
```

Linux with Krita 6.0.4 can build the native helper from a clone, then install that copy:

```bash
python3 build_native.py
python3 mcp_server.py install
```

`./install.sh` and `install.ps1` call the same installer. `--force` writes the enable flag while Krita is still open. By hand, copy `pykrita/krita_mcp/` and `pykrita/krita_mcp.desktop` into Krita's `pykrita` folder and enable **MCP Bridge** under *Settings → Configure Krita → Python Plugin Manager*.

Start Krita. *Tools → Scripts* should show **Start/Stop MCP Bridge** and **MCP Bridge Status…**.

```bash
uvx krita-mcp --selftest
```

Later releases are cut from a GitHub release. The steps are in [docs/development.md](docs/development.md#release).

## Point a client at it

Restart the client after editing its config. Krita has to be running before a tool can touch a document.

Codex, `~/.codex/config.toml`:

```toml
[mcp_servers.krita]
command = "uvx"
args = ["krita-mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 360
enabled = true
```

Claude Code:

```bash
claude mcp add krita -- uvx krita-mcp
```

Claude Desktop and any other JSON config:

```json
{
  "mcpServers": {
    "krita": {
      "command": "uvx",
      "args": ["krita-mcp"]
    }
  }
}
```

A checkout works the same way with `python3` and an absolute path to `mcp_server.py`.

## A session

Ask for the open layer tree and set the top layer to multiply at 60%. Ask to cut a forearm onto its own layer, rotate it around the elbow, and paint the seam with a dry bristle. Ask to duplicate three raster keys, export an atlas, then repair one cracked pane on the held frame and show the before and after.

Reconnect the client after a plugin update. An open chat keeps the schema it saw at startup.

`close_document` can still bring Krita down on some teardown paths. *File → Close* can hit the same fault. Leave the file open and close it yourself when the session matters. [docs/usage.md](docs/usage.md#closing-documents) has the rest: colour space, filter types, the recovery dialog, and what a lost response means.

Build and tests: [docs/development.md](docs/development.md). Reports and changes: [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md).

## License

The Python server and the Krita plugin are [CC0 1.0](LICENSE). `native/bridge.cpp` is GPL-2.0-or-later. Vendored Krita headers keep their own GPL and LGPL notices. See [native licensing](native/README.md#sources-and-licensing) and `native/LICENSES/`.

The wheel that `uvx` installs is the CC0 server and plugin. It does not contain the helper binary or the vendored headers.
