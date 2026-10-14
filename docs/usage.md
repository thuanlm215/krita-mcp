# Using Krita MCP

Version 1.4 exposes 54 tools and 63 bridge operations. Reconnect the MCP client after a plugin update. A running chat keeps the schema it discovered at startup.

## Tools

| Tool | What it does |
| --- | --- |
| `status` | Krita version and every open document |
| `dismiss_recovery_dialog` | Cancel the native Recover Files dialog and keep autosaves. Other modals stay open |
| `inspect_document` | Canvas, colour space, layer tree, selection |
| `create_document` / `open_document` | Make or load a document |
| `save_document` / `export_document` | Save in place or save-as; export PNG, JPEG, and other formats Krita supports |
| `close_document` | Close a document. See [Closing documents](#closing-documents) |
| `transform_image` | Resize canvas, scale, rotate, crop, flatten |
| `create_layer` / `set_layer` / `delete_layer` | Layer stack, including file layers linked to an image |
| `duplicate_layer` / `move_layer` / `merge_layer_down` | Restack and merge |
| `get_image` | Render the canvas and return a PNG |
| `get_pixel` | Colour at one pixel |
| `draw` | Shapes, text, gradients, and bitmaps on a paint layer |
| `list_brushes` / `draw_brush` | Native presets and undoable brush or eraser strokes |
| `plan_stroke` / `paint_stroke` | Versioned geometry and one native pressure stroke |
| `sample_colors` / `compare_images` | Linear-light palette samples and aligned geometric diagnostics |
| `prepare_repair` / `render_repair` / `inspect_repair` | Frozen baseline and bounded candidates |
| `commit_repair` / `repair_status` / `discard_repair` | Idempotent commit, outcome lookup, snapshot release |
| `select_polygon` / `selection_to_layer` | Polygon or lasso; copy or cut the selection to a layer |
| `transform_layer` | Preview or apply a native transform mask around a pivot |
| `warp_layer` | Preview or apply one local raster pull on a preserved-source duplicate |
| `select_content` | Alpha, colour, contiguous region, and imported masks |
| `texture_patch` / `clone_stamp` | Masked texture placement and sampled cloning |
| `mask_layer` / `bake_layer` | Transparency masks and baked paint layers |
| `outline_layer` / `export_layers` | Antialiased sprite outlines; one trimmed PNG per layer with a manifest |
| `edit_batch` / `preview_changes` | One Undo group with rollback, and before/after/difference |
| `animation_frame` / `export_animation` | Raster keyframes, onion skin, FPS, PNG sequence, atlas, manifest |
| `import_animation` / `animation_review` | Registered PNG import and previous/current/next panels |
| `undo` / `redo` | Native history for the requested document |
| `apply_filter` | A Krita filter on a layer or a region |
| `set_selection` | rect, all, none, invert, grow, shrink, feather |
| `list_capabilities` | Filter parameters, blend-mode ids, and real helper support |
| `trigger_action` | Allowlisted native history and selection actions |
| `self_test` | End-to-end health check |

Sequences for extraction, brushes, warp, animation, and repair are in [workflows.md](workflows.md).

## Drawing

`draw` paints a list of commands, in order, onto an RGBA/8-bit paint layer:

`fill_rect`, `rect` (optional corner radius), `ellipse`, `circle`, `line`, `polyline`, `polygon`, `ink`, `text`, `linear_gradient`, `radial_gradient`, `image`, `svg`, and `clear`.

The active selection constrains these commands, including feathered alpha. Colours accept `#rrggbb`, `#rrggbbaa`, SVG names, or `[r, g, b, a]`.

Geometry uses `w` and `h`. Outline thickness is `stroke_width`. Only the union of affected pixels is read, painted, and written back.

**Gradients.** Both take `stops`, such as `[[0, "#fff"], [1, "#fff0"]]`. `linear_gradient` fills its `x, y, w, h` box along `direction` (`horizontal`, `vertical`, `diagonal`), or along explicit `x1, y1, x2, y2`. `radial_gradient` given `cx, cy` and `radius` (or `rx` and `ry`, with `angle` in degrees clockwise) fills only that ellipse, and the last stop lands on its edge. Given a box instead, it fills the ellipse inscribed in the box, so a wide box gives a soft oval, not a cut-off circle.

**Ink.** `ink` fills a line whose width varies along it. Each point is `[x, y]` or `[x, y, width]`; a point without a width uses `stroke_width`. Taper the ends by giving them small widths. `smooth` (default `true`) passes a Catmull-Rom curve through the points. Overlaps fill once, so a translucent colour has no darker joints.

**Bitmaps.** `image` takes base64 PNG `data` or an absolute `path` to a PNG, JPEG, WebP, or BMP file of up to 48 MiB. With neither `w` nor `h` the image keeps its natural size; with one of them it keeps its aspect ratio. `svg` takes `svg` markup or an absolute `.svg` `path` (up to 8 MiB) and rasterises it the same way. It needs Qt's SVG module, which Krita builds normally ship; without it the command returns `unsupported`.

**Alpha lock.** `create_layer` and `set_layer` take `alpha_locked` and `inherit_alpha`. On an alpha-locked layer, `draw` and `draw_brush` change colour only where pixels already exist, and `draw` skips `clear` commands. Layer listings show both flags only when they are set.

**Native strokes.** `draw_brush` rejects a point outside the canvas and names it. Pass `clamp: true` to pull such points onto the nearest edge. Pass `strokes` instead of `points` to paint up to 64 strokes in one call and one Undo entry. Each stroke may set its own `color`, `size`, `opacity`, and `erase`; unset values come from the top level. One preset and mode apply to every stroke.

**File layers.** `create_layer` with `type: "filelayer"` and an absolute `path` links an image file. Krita reloads the layer when the file changes. `scaling` is `none`, `to_image_size`, or `to_ppi`. The result reports whether the file finished loading. `get_pixel` reads what a file, fill, clone, vector, or group layer renders.

## Game assets

`outline_layer` traces the rendered alpha of a layer onto a new paint layer. `width` is 0.5 to 32 pixels. `position` `outside` (the default) puts the outline under the source and fills beneath it, so antialiased source edges leave no seam. `inside` and `center` put it above the source. `antialias: false` gives a hard pixel-art outline from alpha 128 and up, four-connected at width 1. The source is untouched, and the new layer is one Undo entry.

`export_layers` writes one PNG per layer and a `manifest.json` into a new directory. It exports the visible top-level layers unless `layers` names others. `trim` (default `true`) crops each PNG to its exact alpha bounds. `padding` adds a transparent margin. The manifest gives each file's canvas `x` and `y`, which can be negative after padding, so an engine can place the parts back where they were drawn. The document is not changed.

## Files instead of inline data

Every tool takes `params_file`: an absolute path to a `.json` file holding an object of that tool's arguments. Inline arguments override the file. Use it for long command lists, stroke points, or batch edits, so they do not travel through the conversation. Required arguments may come from either place, so tool schemas list none; the server checks them after merging.

Tools that return a picture (`get_image`, `transform_layer`, `warp_layer`, `preview_changes`, `compare_images`, `inspect_repair`, `animation_review`) take `save_path`, an absolute `.png` path. The image is written there and the reply carries only the metadata and `saved_to`.

Image inputs take a file path beside their base64 form: `texture_patch` `data_path` and `mask_path`, and `select_content` (mask mode) and `mask_layer` (image mode) `data_path`. The client and Krita share a filesystem, because the bridge listens on loopback only. Repair candidates still take bounded base64 data.

## Addressing layers

Listings use docker order, top first. Name a layer by its name (`"Sky"`), a path through groups (`"Background/Sky"`), an index path (`"#0/#1"`), or `"uuid:…"`. Omit the layer to use the active one. Listings return all of these forms.

## How it works

```
MCP client  --stdio JSON-RPC-->  krita-mcp  --HTTP-->  Krita plugin  -->  libkis
                                  (stdlib)   loopback   (GUI thread)
```

Krita runs Python only inside itself, and an MCP server is a separate process. The two halves find each other through a small file in Krita's data folder. That file holds the port and a random per-session token. The bridge binds loopback only and requires the token.

libkis is not thread-safe. The HTTP server accepts connections on worker threads and marshals every Krita call onto the GUI thread, with a deadline. A modal dialog or a long filter returns `krita_busy` at the deadline. Requests do not wait forever.

## Behaviour that affects edits

**Native Undo.** On the tested Linux/Krita 6.0.4 helper, drawing and regional filters enter Krita's image command scheduler. A batch can mix those edits with brush, selection, extraction, and transform commands, then undo the group once. A new edit discards the existing redo branch, as a manual edit does. Rollback cannot restore that earlier redo branch.

**Blending modes.** Krita 5.3 has no API that enumerates composite ops, and it accepts any string. Unknown ids render as Normal. `set_layer` canonicalises the ids it knows and warns on anything else.

**Colour space.** `draw`, `get_pixel`, and per-layer `get_image` require RGBA/8-bit. The merged `get_image` works in other colour spaces.

**Filter parameters.** Call `list_capabilities(kind="filters", include_parameters=true)` and use each entry's `parameters` and `parameter_info`. QColor is `{"type":"QColor","rgba":[1,0,0,1]}` with normalized components. QByteArray is `{"type":"QByteArray","base64":"...","byte_length":123}`. Primitives keep their types. Unknown Qt types are read-only. A malformed setting leaves pixels and history intact.

A serialization fault returns HTTP 500 with `response_serialization_error`, the operation, and the exception type. The bridge keeps serving. A send/read timeout, reset, or truncated response returns `incomplete_response` with `execution_outcome: "unknown"` and is not retried, because the edit may already have completed. The client rediscovers the bridge only when TCP fails before any HTTP bytes are sent, or when it receives HTTP 401 before dispatch.

**Startup recovery.** `dismiss_recovery_dialog` with `{}` cancels the native `KisAutoSaveRecoveryDialog` and keeps the autosaves. It matches the dialog class, not a translated label. Other modals stay open. With no recovery dialog, the call is a no-op. It does not recover or discard files.

**Masked layers.** Native brush work, geometric draw, extraction, patches, clone, and warp reject masks and transformed ancestors before editing. Put a Cleanup layer at the document root above the masked ancestor, or `bake_layer` that ancestor and edit the unmasked copy. Bake preserves and hides the source. See [workflows.md](workflows.md).

## Closing documents

`close_document` can take Krita down. A fresh or lightly edited document usually closes. A long session fails more often. The fault is in Krita's C++ teardown after `Document.close()` returns, and *File → Close* can hit the same path.

The 1.2 helper drains pending `KisNodeModel` updates before view teardown on Krita 6.0.4. That covers the layer-model timer crash observed here. Other teardown faults remain possible.

Prefer to leave documents open and close them in Krita. If Krita exits, the next call returns a clear "could not reach Krita" error.

## Configuration

Krita reads these keys from `kritarc` under `[krita_mcp]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `autostart` | `true` | Start the bridge when Krita starts |
| `port` | `9797` | First port to try. The bridge scans upward if it is taken |

| Variable | Meaning |
| --- | --- |
| `KRITA_MCP_STATE_DIR` | Override the discovery-file directory |
| `KRITA_MCP_TRACE` | Set to `1` before starting Krita to log every operation |
| `KRITA_MCP_PORT` / `KRITA_MCP_TOKEN` | Skip discovery and connect directly |

Trace lines go to `krita_mcp_trace.log` in Krita's data folder, flushed per line. Krita has no console. An entry with no matching completion names the operation that stopped the process.

## Troubleshooting

**Could not reach the Krita MCP bridge.** Krita may be stopped, or the plugin may be disabled. Check *Tools → Scripts → MCP Bridge Status…*. If the menu entries are missing, enable MCP Bridge in the Python Plugin Manager and restart Krita.

**`response_serialization_error` or `incomplete_response`.** Inspect `status` and the document before repeating an edit. These errors do not mean Krita has exited. Filter metadata requires plugin 1.3 or newer. HTTP handler changes require a Krita restart.

**Tools are missing in the client.** Restart the client after editing its config. `uvx` must be on `PATH`. A checkout config must use an absolute path to `mcp_server.py`.

**Port already in use.** The bridge scans upward from 9797 and writes the chosen port into the discovery file. Set another start port with `port=` under `[krita_mcp]` in `kritarc`.

## Diagnostics

```bash
krita-mcp --list-tools
krita-mcp --call status
krita-mcp --call draw --params-file commands.json
```

From a checkout, `python3 mcp_server.py` accepts the same flags. With no flag, the process speaks MCP on stdin/stdout.
