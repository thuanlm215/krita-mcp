# Krita MCP

[![License: CC0](https://img.shields.io/badge/License-CC0-blue)](LICENSE)
[![Works with Claude Desktop](https://img.shields.io/badge/Works%20with-Claude%20Desktop-purple)](#clients)
[![Krita 5.2+](https://img.shields.io/badge/Krita-5.2%2B-orange)](https://krita.org)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green)](https://modelcontextprotocol.io)
[![PyPI](https://img.shields.io/pypi/v/krita-mcp?color=blue)](https://pypi.org/project/krita-mcp/)

Drive a live Krita document from Claude Desktop, Codex, or any other MCP client.

[thuanlm215](https://github.com/thuanlm215) maintains version **1.4.1**, published as [`krita-mcp`](https://pypi.org/project/krita-mcp/). Fifty-two tools. The list is in [docs/usage.md](docs/usage.md). Pose, warp, frame, and repair order is in [docs/workflows.md](docs/workflows.md).

## Demo

![Layer studies, a selection swung around a pivot, a pressure stroke, four animation cells, and a cracked pane beside a repaired one](docs/capabilities.jpg)

The three sheets are a layer stack: wash, line, color. The dashed loop and the pins are a selection swung around a pivot. The bristle mark is one native stroke that starts light and ends heavy. The four cells are raster frames. The split lens is a repair: the crack on one side, the same pane whole on the other.

## Install

Krita 5.2 or newer covers documents, layers, geometric drawing, filters, and export. Brush pressure, native pixel Undo, warp, batches, and exact raster frames need **Linux, Krita 6.0.4, Qt6, little-endian**, plus the helper in [native/README.md](native/README.md). Anywhere else those calls return `unsupported`.

The server is Python 3.8 or newer and the standard library. Krita loads a plugin in its own process. The MCP client runs `krita-mcp` beside it. Close Krita before the installer writes `kritarc`.

```bash
uvx krita-mcp install
uvx krita-mcp --selftest
```

Start Krita first. *Tools → Scripts* should show **Start/Stop MCP Bridge** and **MCP Bridge Status…**.

Linux with Krita 6.0.4 can build the native helper from a clone, then install that copy:

```bash
python3 build_native.py
python3 mcp_server.py install
```

`./install.sh` and `install.ps1` call the same installer. `--force` writes the enable flag while Krita is still open. By hand, copy `pykrita/krita_mcp/` and `pykrita/krita_mcp.desktop` into Krita's `pykrita` folder and enable **MCP Bridge** under *Settings → Configure Krita → Python Plugin Manager*.

## Clients

Restart the client after editing its config. Krita has to be running before a tool can touch a document.

**Claude Desktop**

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

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.krita]
command = "uvx"
args = ["krita-mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 360
enabled = true
```

**Claude Code**

```bash
claude mcp add krita -- uvx krita-mcp
```

A checkout works the same way with `python3` and an absolute path to `mcp_server.py`.

## A session

Ask for the open layer tree and set the top layer to multiply at 60%. Ask to cut a forearm onto its own layer, rotate it around the elbow, and paint the seam with a dry bristle. Ask to duplicate three raster keys, export an atlas, then repair one cracked pane on the held frame and show the before and after.

Reconnect the client after a plugin update. An open chat keeps the schema it saw at startup.

`close_document` can still bring Krita down on some teardown paths. *File → Close* can hit the same fault. Leave the file open and close it yourself when the session matters. [docs/usage.md](docs/usage.md#closing-documents) has the rest.

Build and tests: [docs/development.md](docs/development.md). Reports and changes: [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md).

## License

The Python server and the Krita plugin are [CC0 1.0](LICENSE). `native/bridge.cpp` is GPL-2.0-or-later. Vendored Krita headers keep their own GPL and LGPL notices. See [native licensing](native/README.md#sources-and-licensing) and `native/LICENSES/`.

The wheel that `uvx` installs is the CC0 server and plugin. It does not contain the helper binary or the vendored headers.
