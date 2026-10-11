# Native workflow helper

This helper is pinned to **Linux, Krita 6.0.4, Qt6, little-endian**. It links
against the installed Krita libraries. It is not a portable plugin binary.
Other Krita versions return `unsupported` before a helper-backed edit starts.
`list_capabilities(kind="workflow")` verifies the installed helper checksum,
dynamic load, symbols and ABI marker without activating a document. It reports
Python API presence separately from helper usability. `list_brushes` also
reports actual pressure-helper availability rather than only `paintLine` presence.

Build from the repository root:

```bash
python build_native.py
bash install.sh
```

Requires a C++17 compiler, CMake, Qt6 Core/Gui/Widgets/Xml development files,
KF6I18n development files, Boost and Eigen headers, and the installed
`kritacommand`, `kritaui`, `kritaimage`, `kritalibkis` libraries. For this Arch
environment the header packages are `boost` and `eigen`. The build also accepts
headers under `native/build/deps/usr/include`; it does not install packages.

`fetch_headers.py` downloads header dependencies from KDE/krita **v6.0.4**
and caches them in `native/build/krita_headers`. A first build needs network
access. The vendored Krita headers keep their copyrights and licenses.
Generated export/config headers match the tested release configuration.

`build_native.py` produces a content-addressed `.so` and a checksum manifest
inside `pykrita/krita_mcp`. They are local build outputs ignored by Git and
copied by the existing installer. The loader verifies the checksum and ABI
marker. Rebuild against the matching libraries when moving installations.
Loaded libraries stay alive for their history commands. Development uses fresh
isolated processes; never overwrite a library mapped by running Krita.

## What it does

- Accesses the real document KUndo2 stack through exported window/view getters.
- Uses `KisFigurePaintingToolHelper` throughout a pressure/tilt/time stroke.
- Stores pixel edits as native commands wrapped by Krita's post-execution
  adapter, so their Undo jobs share the image scheduler with brush commands.
  An ordinary GUI callback cannot safely interleave pixel restoration with
  those asynchronous jobs inside a macro.
- Publishes selection commands through the same adapter. Direct legacy
  selection Undo can overtake queued cut/paste Undo and leave a selection mask
  attached; grouped Redo then attempts to attach that mask twice.
- Provides a read-only source-image barrier for cloning full editable repair
  contexts, plus private read-only history diagnostics (no public MCP tool).
- Groups existing native commands, and discards a failed group's redo branch.
- Addresses exact raster keyframes, independently of timeline multiselection.
- Reads/toggles paint-layer onion skin and records native visibility changes.

The 1.4 painter uses unrecorded scheduler-backed timeline navigation and reads
logical UI time independently of temporary cache-rendering time. Pixel edits
participate in native `KisTransaction` mementos. Bounded correction stages
detached devices and publishes only accepted frozen bytes; exact frame commits
include restore-key topology and playback-setting side effects in one group.
Applied repair receipts are recorded at native publication, before projection
refresh. Recoverable completion errors retain the known applied outcome.
Programmatic range sync blocks only timeline-spinbox feedback, which otherwise
creates new animation-setting commands and truncates Redo during Undo.
GUI metadata pushes synchronize against image jobs. Python callers retain
owned libkis wrappers for the entire native call. Tool contracts and limits
are in the [workflow guide](../docs/workflows.md).

The 1.3 local warp resamples in Python/Qt, then inserts a pre-rendered paint
duplicate and hides the preserved source in this helper's native history group.
It does not add a native mesh engine. The existing continuous stroke helper
was verified with `k) Blender Smear`; no C++ changes were needed for that preset.

Pixel history has a 256 MiB budget per loaded helper. Destruction of a command
releases its budget. Property callbacks retained by Python are used only for
animation settings and onion skin, outside mixed image batches. They defer
projection refresh until the Undo barrier has been released.

## Sources and licensing

`bridge.cpp` is GPL-2.0-or-later. The Python server and the Krita plugin are
CC0-1.0. GPL and LGPL notices for the helper and the Krita headers are in
`LICENSES/`. The header notices remain in `vendor/`.

The implementation follows the pinned primary sources:

- [KUndo2Stack](https://github.com/KDE/krita/blob/v6.0.4/libs/command/kundo2stack.cpp)
- [PaintingResources](https://github.com/KDE/krita/blob/v6.0.4/libs/libkis/PaintingResources.cpp)
- [Saved image commands](https://github.com/KDE/krita/blob/v6.0.4/libs/image/commands_new/kis_saved_commands.cpp)
- [Keyframe channels](https://github.com/KDE/krita/blob/v6.0.4/libs/image/kis_keyframe_channel.cpp)

Run `python -m unittest tests.test_workflow tests.test_advanced` against a separate Krita instance
after changing the bridge. Compilation alone does not verify Undo ordering.
