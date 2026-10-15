# Workflows

Helper-backed steps below require the Linux / Krita 6.0.4 native helper described in [native/README.md](../native/README.md). Build it, install the plugin from that checkout, and restart Krita. `list_capabilities(kind="workflow")` reports Python API presence separately from a verified helper. A missing helper returns `unsupported` before the edit starts.

Call `inspect_document` and pass an explicit document and layer. Session ids belong to the open document and change when it closes. Prefer `"uuid:…"` when two layers share a name.

Painter and repair targets in this version are unlocked, alpha-unlocked, unmasked RGBA/U8 paint layers using `sRGB-elle-V2-srgbtrc.icc`, with no transformed or locked ancestors. `plan_stroke` and `paint_stroke` accept canvases up to 16 megapixels. Repairs and colour feedback keep a canvas of at most 1 megapixel. Static clone, patch, and warp tools keep their own animated-layer guards.

## Extract, pose, and clean up

1. `select_polygon` with a closed point list. A lasso is a polygon with more samples. `combine` is `replace`, `add`, `subtract`, or `intersect`. `feather` is in pixels.
2. `selection_to_layer` with `mode` `copy` or `cut`. Native paste keeps document coordinates. The selection clears unless `clear_selection` is false.
3. `transform_layer` with `mode: "preview"`, then the same arguments with `mode: "apply"`.
4. `get_image` to read the composite.
5. `create_layer` named `Cleanup` at the document root, then `draw_brush`.
6. `undo` / `redo` using the returned `undo_steps` when you need to step history.
7. `save_document` to a new `.kra` and `export_document` to PNG.

Transform parameters are absolute against the original layer pixels. Another angle replaces the `MCP Transform` mask. It does not stack a second resampling. Zero rotation, unit scale, and zero translation reset that mask to identity. The original layer UUID and pixel bytes stay intact. Keep the mask's name.

Preview does not change source pixels or history. It applies the mask on a detached clone and returns the Krita composite, including surrounding layers and blend modes. Targets may be static paint or group layers and transparency masks. Negative scale flips. `shear_x` and `shear_y` are supported. The transform covers the whole layer. Extract a part first when only some pixels should move.

```json
{
  "layer": "Arm",
  "mode": "preview",
  "pivot": {"x": 130, "y": 110},
  "degrees": -25,
  "dx": 8,
  "dy": 0,
  "scale_x": 1,
  "scale_y": 1
}
```

Brush example:

```json
{
  "layer": "Cleanup",
  "preset": "g) Dry Bristles",
  "size": 12,
  "opacity": 0.6,
  "color": "#39312b",
  "points": [
    {"x": 125, "y": 112, "pressure": 0.25},
    {"x": 150, "y": 119, "pressure": 0.8}
  ]
}
```

`list_brushes` returns the installed preset names. `erase: true` uses the native eraser. Colour, preset, size, opacity, and eraser UI settings are restored after the call. Presets depend on the Krita installation. `b) Basic-5 Size` is the calibrated default for planned strokes. `g) Dry Bristles` is the additional calibrated textured preset. `k) Blender Smear` was verified through `draw_brush` with a feathered selection and one Undo/Redo.

`edit_batch` runs several of these tools as one native Undo group. On an operation error it undoes the group and discards that group's redo entry. It rejects file I/O, history actions, and nested batches. A batch accepts up to 64 edits. Starting a batch discards an existing redo branch.

```json
{
  "label": "Pose selected forearm",
  "edits": [
    {"tool": "select_polygon", "arguments": {"points": [[100,100],[160,100],[170,220],[110,220]]}},
    {"tool": "selection_to_layer", "arguments": {"layer": "Working", "mode": "cut", "name": "Forearm"}},
    {"tool": "transform_layer", "arguments": {"layer": "Forearm", "mode": "apply", "pivot": {"x": 130, "y": 110}, "degrees": -25}}
  ]
}
```

## Local warp

`warp_layer` pulls one circular region on a static, visible, unlocked, unmasked RGBA/U8 paint layer. Apply inserts a duplicate, hides the source, and records both in one Undo group. Source pixels, including off-canvas content, stay unchanged. The result is raster. A later warp resamples the working copy. This is not a mesh or liquify mask.

`center` maps to `center + offset`. Influence is a disk of radius 2–256 around that destination. Displacement length is at most `radius / 4`, and both controls must lie inside the canvas. Sampling is bilinear in premultiplied RGBA. Pixels outside the disk and zero-selection pixels stay byte-exact. Soft selection weights the difference at the destination.

Preview matches apply for the same unchanged source and selection, and it does not touch history. Apply returns `undo_steps: 1`. A no-op pull returns zero Undo steps and creates no layer. `edit_batch` rejects warp during preflight. Sampling is limited to 1 megapixel. Composite preview is limited to 16 megapixels. `max_size` affects only the returned PNG.

## Undo steps

| Operation | Entries that revert it |
| --- | --- |
| `select_polygon`, rect selection, grow, shrink, feather | 1 |
| `draw_brush`, pressure or continuous mode | 1 stroke for every sample |
| `draw_brush`, segments mode with N points | N − 1 line calls |
| `draw_brush`, path mode at pressure 1 | 1 connected path |
| `selection_to_layer`, copy, selection cleared | 2 |
| `selection_to_layer`, cut, selection cleared | 3 |
| `selection_to_layer` with `clear_selection: false` | copy 1, cut 2 |
| `transform_layer` apply | 1. The first application also creates an earlier identity mask |
| `draw`, regional filter, patch, clone, mask edit | 1. An unchanged pixel edit returns 0 |
| `bake_layer` | 1. The source stays preserved and hidden |
| `warp_layer` apply | 1. A no-op returns 0 |
| `edit_batch` | 1 for the whole batch |
| Frame edit, FPS or range, onion skin | 1 |

`undo` and `redo` share history with manual edits. These counts were checked on Krita 6.0.4.

## Generated image to game sprite

1. `create_document` at the source size, then `draw` an `image` command with the generated file's `path` onto a paint layer.
2. `remove_background`, with `holes` to clear backdrop between limbs. Check edges against a contrasting backdrop with `get_image`; raise `tolerance` a little for a noisy backdrop, `softness` for a softer matte, or `shrink: 1` for a stubborn halo.
3. For a sheet, `slice_sheet` with `columns` set to the frames per row, and `output: "layers"` to repair cells in place, or `output: "files"` with `uniform` and `align: "bottom"` for engine frames. `import_animation` takes those files back as a timeline.
4. `quantize_layer` with the game's `palette` to pull stray colours onto it, or `mode: "swap"` for colour variants.
5. `outline_layer` for a consistent sprite outline, then `export_layers`.

Wrap steps 2 and 4 in one `edit_batch` to undo them together.

## Texture, masks, and review

- `texture_patch` takes `data` (base64 PNG), `data_path` (an absolute image path), or `source_layer`, an optional `source_region`, a destination `region`, `opacity`, and optional `mask_data` or `mask_path`. An explicit mask matches the destination size. `mask_channel` is `alpha` or `luminance`.
- `clone_stamp` takes a source anchor, destination `points`, `size`, `softness`, and `opacity`. Pressure weights opacity. The source is frozen before the stroke. This is sampled compositing. Use `draw_brush` when the preset itself must paint.
- `mask_layer` builds a transparency mask from the selection, a `fill`, or a canvas-sized image.
- `bake_layer` renders the full layer projection, including off-canvas pixels, into a new paint layer and hides the source.
- `preview_changes` stores a checkpoint, then `compare` returns before, after, difference, and changed-pixel bounds. `discard` releases the snapshot. Checkpoints are session-local.

Draw, patch, clone, and regional filters respect selection, including soft alpha. Brush work and direct raster edits reject masked paint layers and transformed ancestors. Create Cleanup at the document root, above the highest transformed ancestor. Cleanup inside that group would be transformed again. To edit the rendered result, `bake_layer` the ancestor and target the returned copy. Native projection can drop RGB under zero alpha, so a baked transform may normalize invisible bytes while keeping the visible premultiplied result.

## Raster animation

`animation_frame` modes:

| Mode | Behaviour |
| --- | --- |
| `info` | Keyframe times, current time, FPS, clip range, onion skin |
| `blank` | Blank key at `time`. `replace: true` overwrites |
| `duplicate` | Copy the exact `source_time` key. The copy is independent |
| `delete` | Remove the key at `time`. Frame 0 stays |
| `scrub` | View `time`. This is not an Undo edit |
| `settings` | Set `fps`, `start`, and `end` as one Undo |
| `onion` | Set `enabled` as one Undo |

The first animation step uses Krita's native action and keeps the static pixels as frame 0. Blanking that frame needs `replace: true`. Native brush work can target the current animation frame. Geometric `draw` and patches require static layers. Undoing the first added frame can leave animation enabled, matching Krita.

`export_animation` writes sampled composite frames, `atlas.png`, and `manifest.json` into a new directory. Pass `start`, `end`, `step`, `columns`, and an `{x, y}` anchor in document pixels. The tool restores the previous view time and does not bake onion-skin overlays into the export. Limits are 128 samples and a 16 megapixel atlas.

`import_animation` checks an ordered PNG list before creating a document: first time 0, increasing times, equal dimensions, explicit FPS, and a canvas anchor. Pixels are not recentered, cropped, or rescaled. Limits are 64 frames, 64 MiB decoded, 6 MiB per PNG, and 1 megapixel per frame. Registration hashes are stored in a KRA annotation.

```bash
python examples/sequence_player.py output/manifest.json --speed .25
python examples/sequence_player.py after/manifest.json --compare before/manifest.json
```

## Planned strokes

`plan_stroke` returns geometry and does not paint. `paint_stroke` renders one continuous native stroke of 2–128 samples and restores brush UI state. Oversized paths fail. They are not split. Nonzero tilt is rejected by this specification.

Deterministic geometry does not promise pixel-exact replay of a randomized brush engine. Preset names are exact strings. The binding does not hash the preset file, so a same-named resource change is not detected.

`get_image` can return a full-resolution ROI (`max_size=0`), a thumbnail, a layer, or merged pixels at an explicit animation time. Clean readback excludes onion skins and restores timeline state. Timeline position is native `currentUITime`.

`sample_colors` ignores fully transparent pixels and returns an alpha-weighted linear-light sRGB mean, plus a deterministic 4-bit palette. `compare_images` returns aligned before, after, difference, and protected panels. Mask alpha zero forbids changes to every stored byte. IoU and coverage use alpha >= 128 by default. These metrics describe the stated geometry. They are not an art-quality score.

Merged exports can differ in RGB where alpha is zero after a cache rebuild. Use a raw layer readback when the invisible bytes matter.

## Bounded repair

1. State the defect. `prepare_repair` takes an explicit target, ROI, an exact-size allowed-mask PNG, and an optional target mask.
2. `render_repair` builds at most three alternatives from the same immutable baseline. Kinds are a native `stroke`, a static `clone`, `patch`, or `warp` on an owned document, or an explicit RGBA `replacement`. Candidate documents close after capture. The source layer is not baked implicitly.
3. `inspect_repair` returns one PNG content block and small JSON metadata. Encoded PNGs are not embedded in the text payload. `view=composite` is the frozen full-document rendering. `view=raw_layer` is the raw-layer ROI. `image=before` or `image=after` returns one frozen full-resolution image. `max_size` applies to panels only.
4. `commit_repair` accepts only the frozen `transaction` and `candidate` ids. It rechecks pixels, selection, layer structure, profile, view settings, logical time, keyframes, timing, history, composite contributors, and the document background. A dependency change returns `stale_input`.
5. `repair_status` returns the recorded outcome after an ambiguous response. Repeating the same commit does not paint again, including after Undo. `discard_repair` releases the prepared buffers.

A `completion_failure` can mean the pixels and history were applied and the receipt or refresh failed. Query status before assuming a rollback.

Replacement blends premultiplied RGB/alpha by soft mask weight. It can remove alpha. Zero weights retain every RGBA byte. Candidates do not change source history. The composite preview edits a read-locked clone and closes it without opening a view.

Budgets: 8 active and 32 retained tokens, 64 MiB of copied snapshots, a separate 64 MiB composite-context ceiling, a 600-second TTL, 1 megapixel source extent, 8 MiB encoded PNG, and 3 candidates. There is no crash-durable store. An ambiguous send or read is never resent automatically.

Frame scope uses `{"kind":"frame","time":4,"exposure_policy":"one_sample"}`. `existing_keyframe` updates the held key and reports its exposure. `one_sample` isolates that integer time and restores the old held image at the next time. Keys at 0 and 10 repaired at time 4 become 0, 4, 5, and 10. `prepare_repair.region_mode` is `roi` or `whole_frame`. Whole-frame mode requires frame scope and a ROI that is exactly the canvas. Off-canvas pixels stay preserved. Direct `paint_stroke` on an animation requires an existing key and `existing_keyframe`.

`animation_review` returns clean previous, current, and next panels with shared coordinates.

## Limits

- Pressure and continuous mode accept floating coordinates, pressure, tilt, and increasing `timestamp_ms` samples. One native helper covers the stroke. Path mode uses pressure 1. The helper does not synthesize operating-system tablet events.
- Part extraction requires an unlocked static paint layer without masks. Brush work also supports animated paint layers without masks.
- Static transforms target RGBA/U8 paint or group layers. Other transform masks beside `MCP Transform`, and animated descendants, are rejected.
- Selection masks, preview buffers, and transformed bounds are limited to 16 megapixels. Strokes accept 2–128 points and brush sizes up to 2048. Polygons accept 3–2048 points.
- Clone processing is limited to 8192 dabs and 16 megapixels of dab work. Native pixel history is limited to 256 MiB per loaded helper. Preview checkpoints are limited to 8 snapshots and 128 MiB.
- Animated transform masks and skeletal rigs are outside this raster workflow.

Reference scripts live in `examples/`: `example_artist_workflow.py`, `example_animation_workflow.py`, `example_deformation_workflow.py`, and `painter_demo.py`. Run them through `scripts/run_isolated_tests.py` when the desktop documents must stay untouched. See [development.md](development.md).
