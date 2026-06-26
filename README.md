# Hold Keys — Multi-Threshold Hold Shortcuts for Blender

> **One key. Multiple actions. Your hands never leave the keyboard.**

Hold Keys adds hold-shortcut logic to Blender: tap a key and the native shortcut fires. Hold it past a threshold and a second operator runs. Hold even longer and a third one takes over. No modes, no combos, no remapping — your existing keymap is fully preserved.

Compatible with **Blender 4.2 → 5.1** · Windows · macOS · Linux

---

## How it works

```
G  →  tap          → Grab  (native Blender)
G  →  hold 0.3 s   → Grab on X axis
G  →  hold 0.8 s   → Snap menu
```

Each binding you create is **non-destructive**: a short tap always falls back to the existing Blender shortcut automatically. Hold Keys only intercepts when you hold past a threshold you've set.

You can assign **as many thresholds as you like** per key — letters, numbers, function keys, mouse buttons, numpad. Works in every Blender mode: Object, Edit Mesh, Pose, Sculpt, UV, Grease Pencil, and more.

---

## Features

### Real-time HUD feedback
The moment you hold a configured key, a minimal overlay appears:
- A **radial arc** around your cursor fills as time elapses
- A **progress bar** in the bottom-left shows each threshold as a tick mark
- Colors confirm which action will fire: blue → building up · green → threshold hit · orange → final threshold
- Adapts to your Blender theme automatically
- Disappears instantly on release — zero visual noise during normal editing

### Smart operator search
Assigning an operator is fast. The built-in search finds any operator in Blender — native or from installed addons:
- Type in plain language: `extrude`, `bisect`, `merge`, `loop cut`…
- Works in **French and English** (alias table included)
- 12-level scoring with fuzzy token matching
- Filter by domain (Mesh, Object, Sculpt, UV…) or source (Blender built-in vs. addon)
- Auto-rescans when addons are installed or enabled — no manual refresh

### Clean setup
- Install as a standard Blender extension (`.zip` drag-and-drop or Extensions menu)
- All settings in **Preferences → Add-ons → Hold Keys**
- Capture a key with one click — an interactive prompt listens for your keypress
- Duplicate bindings to quickly create variations
- Every binding can be individually enabled/disabled without deleting it

---

## Installation

**Method 1 — Drag and drop**
Drag `hold_keys_v2_0_0f.zip` directly into the Blender window.

**Method 2 — Extensions menu**
`Edit → Preferences → Extensions → Install from Disk` → select the `.zip`.

The addon will appear under **Preferences → Add-ons → Hold Keys**.

---

## Quick start

1. Open **Preferences → Add-ons → Hold Keys**
2. Click **+** to add a new binding
3. Click the key capture button and press the key you want to configure
4. Add one or more thresholds, each with a hold duration and an operator
5. Use the operator search to find any Blender or addon operator by name

Short tap always falls back to the native Blender shortcut — no existing binding is overwritten.

---

## Compatibility

| | |
|---|---|
| **Blender** | 4.2 · 4.3 · 4.4 · 5.0 · 5.1 |
| **OS** | Windows · macOS · Linux |
| **Dependencies** | None — pure Python |
| **Permissions** | No network access, no file system access, no admin rights |
| **License** | GPL-3.0 |

---

## Changelog

### v2.0.0 — Complete rewrite
- **Multi-threshold system**: unlimited hold levels per key (previously single-threshold only)
- **HUD**: radial arc around cursor + bottom-left progress bar with per-threshold tick marks
- **Operator search v2**: 12-level scoring, fuzzy match, domain/source filters, auto-rescan
- Precise Edit Mesh select-mode aliases (Vertex / Edge / Face) — no more accidental toggles
- Menu and pie menu support: bind `wm.call_menu` / `wm.call_menu_pie` to any threshold
- Native fallback auto-detection across all Blender modes (Object, Edit, Pose, Sculpt…)
- Timer-based early fire: last threshold executes immediately without waiting for key release
- Full Blender 5.1 extension format (`blender_manifest.toml`)

---

## Contributing

Found a bug or have an idea? [Open an issue](https://github.com/ladroguecmal/hold_keys_blender/issues) — include your Blender version, OS, and steps to reproduce.

The full source is in the `.zip` (GPL-3.0). Pull requests are welcome: bug fixes, new aliases in the search table, new features. Feel free to fork and experiment.

---

## License

Hold Keys is released under the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html) — the same license used by Blender itself.

**What this means in practice:**
- ✅ Use for any project — personal, commercial, professional, client work
- ✅ Your artwork and renders are entirely yours — the GPL only covers the addon code
- ✅ Modify the source code for your own use
- ✅ Full source included in the `.zip`
- ❌ You may not resell this addon as a closed-source product
- ❌ If you redistribute a modified version, it must also be GPL-3.0 and include the source
