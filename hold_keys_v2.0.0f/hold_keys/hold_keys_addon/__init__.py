"""
Hold Keys — Blender 5.1 Extension
===================================
Principe : pour chaque binding configuré, un opérateur proxy est inséré
en tête (head=True) des keymaps natifs correspondants.

Flux d'un appui :
  PRESS  → le proxy démarre un modal + timer 16ms
  TIMER  → si elapsed >= dernier_seuil : exécute l'action immédiatement, quitte
  RELEASE avant dernier seuil → résout le seuil le plus haut atteint :
      • seuil atteint → son opérateur
      • aucun seuil atteint → tap court :
          - native_operator renseigné → l'appelle directement
          - vide → _find_native_kmi() détecte le mode actif (Edit Mesh,
            Object, Pose…) et appelle l'opérateur Blender natif correspondant

Modes de sélection mesh précis :
  Les alias "vertex", "edge", "face" pointent vers des opérateurs virtuels
  mesh.select_mode;type=VERT/EDGE/FACE injectés dans le cache, afin d'éviter
  le toggle aléatoire de l'op générique mesh.select_mode sans kwarg.

Scans automatiques : t+0.5s, t+2s, puis toutes les 5s pendant ~100s,
+ handler load_post pour couvrir les addons chargés tardivement.
"""

import bpy
import time
import math
import addon_utils
from bpy.types import Operator, AddonPreferences
from bpy.props import (
    StringProperty, FloatProperty, CollectionProperty,
    IntProperty, BoolProperty, EnumProperty
)
import gpu
import blf
from gpu_extras.batch import batch_for_shader

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_prefs(context=None):
    ctx = context or bpy.context
    return ctx.preferences.addons[__package__].preferences

# ─────────────────────────────────────────────────────────────────────────────
# Moteur de recherche d'opérateurs v2
# – Cache enrichi : domaine + source + haystack pré-calculé
# – Alias table fixe + génération automatique depuis labels/idnames
# – Scoring 12 niveaux avec bonus multi-token
# – Fuzzy : préfixe par token (≥3 chars)
# – Filtres : source (Blender / Addon / addon spécifique) + domaine
# ─────────────────────────────────────────────────────────────────────────────

_NATIVE_MODULES = {
    'action','anim','armature','asset','boid','brush','buttons','cachefile',
    'camera','clip','cloth','collection','console','constraint','curve','curves',
    'dpaint','ed','export_anim','export_scene','file','fluid','font','geometry',
    'gpencil','grease_pencil','graph','image','import_anim','import_curve',
    'import_scene','info','lattice','marker','mask','material','mball','mesh',
    'nla','node','object','outliner','paint','paintcurve','palette','particle',
    'pointcloud','pose','poselib','preferences','ptcache','render','rigidbody',
    'scene','screen','sculpt','sculpt_curves','sequencer','sound','surface',
    'text','texture','transform','ui','uv','view2d','view3d','wm','workspace',
    'world','spreadsheet','extensions','script',
}

# Domaines : module → catégorie lisible
_DOMAIN_MODULES: dict[str, set] = {
    'Maillage':       {'mesh'},
    'Objet':          {'object'},
    'Courbe/Texte':   {'curve','surface','font','mball'},
    'Armature/Pose':  {'armature','pose','poselib'},
    'Sculpture':      {'sculpt','sculpt_curves'},
    'Peinture':       {'paint','paintcurve','palette'},
    'UV':             {'uv'},
    'Nodes':          {'node'},
    'Vue 3D':         {'view3d'},
    'Rendu':          {'render'},
    'Image':          {'image'},
    'Séquenceur':     {'sequencer'},
    'Animation':      {'anim','action','marker','graph','nla'},
    'Grease Pencil':  {'gpencil','grease_pencil'},
    'Particules/Sim': {'particle','rigidbody','fluid','cloth','dpaint','boid','ptcache'},
    'Scène/World':    {'scene','world','collection','outliner'},
    'Import/Export':  {'import_scene','export_scene','import_anim','export_anim',
                       'import_curve'},
    'Transform':      {'transform'},
    'Interface':      {'screen','workspace','preferences','ui','view2d','ed',
                       'info','asset','wm'},
    'Matériaux':      {'material','texture'},
    'Géométrie':      {'geometry','curves','pointcloud','lattice'},
    'Divers':         {'sound','camera','clip','mask','constraint','cachefile',
                       'buttons','console','text','font','brush'},
}

def _get_domain(module: str) -> str:
    for domain, mods in _DOMAIN_MODULES.items():
        if module in mods:
            return domain
    return 'Autres'

# ── Table d'alias fixes (FR + EN + termes courants) ─────────────────────────
# Format : mot-clé → liste de sous-chaînes présentes dans full_id ou label
_ALIAS_TABLE: dict[str, list[str]] = {
    # Maillage – opérations fondamentales
    'join':          ['mesh.merge', 'object.join'],
    'fusionner':     ['mesh.merge', 'object.join'],
    'merge':         ['mesh.merge'],
    'weld':          ['mesh.merge'],
    'split':         ['mesh.split', 'mesh.separate', 'mesh.edge_split'],
    'séparer':       ['mesh.split', 'mesh.separate'],
    'separate':      ['mesh.separate'],
    'delete':        ['mesh.delete', 'object.delete'],
    'supprimer':     ['mesh.delete', 'object.delete'],
    'dissolve':      ['mesh.dissolve'],
    'dissoudre':     ['mesh.dissolve'],
    'extrude':       ['mesh.extrude'],
    'extruder':      ['mesh.extrude'],
    'inset':         ['mesh.inset'],
    'bevel':         ['mesh.bevel'],
    'biseau':        ['mesh.bevel'],
    'loop cut':      ['mesh.loopcut'],
    'loopcut':       ['mesh.loopcut'],
    'cut':           ['mesh.loopcut', 'mesh.bisect', 'mesh.knife'],
    'couper':        ['mesh.loopcut', 'mesh.bisect', 'mesh.knife'],
    'knife':         ['mesh.knife'],
    'bisect':        ['mesh.bisect'],
    'bridge':        ['mesh.bridge_edge_loops'],
    'fill':          ['mesh.fill', 'mesh.fill_holes', 'mesh.grid_fill'],
    'remplir':       ['mesh.fill'],
    'smooth':        ['mesh.vertices_smooth', 'mesh.smooth_normals'],
    'lisse':         ['mesh.vertices_smooth'],
    'subdivide':     ['mesh.subdivide', 'object.subdivision'],
    'subdiv':        ['mesh.subdivide'],
    'triangulate':   ['mesh.quads_convert_to_tris'],
    'tris':          ['mesh.quads_convert_to_tris'],
    'quads':         ['mesh.tris_convert_to_quads'],
    'trifan':        ['mesh.poke'],
    'poke':          ['mesh.poke'],
    'flip':          ['mesh.flip_normals', 'mesh.edge_flip'],
    'normal':        ['mesh.normals', 'mesh.flip_normals'],
    'recalc':        ['mesh.normals_make_consistent'],
    'mark seam':     ['mesh.mark_seam'],
    'seam':          ['mesh.mark_seam'],
    'crease':        ['mesh.mark_crease', 'transform.edge_crease'],
    'shrink':        ['transform.shrink_fatten'],
    'fatten':        ['transform.shrink_fatten'],
    'solidify':      ['mesh.solidify', 'object.modifier_add'],
    'spin':          ['mesh.spin'],
    'screw':         ['mesh.screw'],
    'rip':           ['mesh.rip', 'mesh.rip_edge'],
    'separate verts':['mesh.rip'],
    'select all':    ['mesh.select_all;action=SELECT',   'object.select_all;action=SELECT',
                       'mesh.select_all;action=TOGGLE',   'object.select_all;action=TOGGLE'],
    'tout sélect':   ['mesh.select_all;action=SELECT',   'object.select_all;action=SELECT'],
    'deselect all':  ['mesh.select_all;action=DESELECT', 'object.select_all;action=DESELECT'],
    'désélect tout': ['mesh.select_all;action=DESELECT', 'object.select_all;action=DESELECT'],
    'invert sel':    ['mesh.select_all;action=INVERT',   'object.select_all;action=INVERT'],
    'inverser sel':  ['mesh.select_all;action=INVERT',   'object.select_all;action=INVERT'],
    'toggle sel':    ['mesh.select_all;action=TOGGLE',   'object.select_all;action=TOGGLE'],
    'loop select':   ['mesh.loop_select', 'mesh.edgeloop_select'],
    'ring':          ['mesh.loop_to_region', 'mesh.select_similar'],
    'linked':        ['mesh.select_linked'],
    'hide':          ['mesh.hide', 'object.hide_view_set'],
    'cacher':        ['mesh.hide', 'object.hide_view_set'],
    'unhide':        ['mesh.reveal', 'object.hide_view_clear'],
    'reveal':        ['mesh.reveal'],
    'duplicate':     ['mesh.duplicate', 'object.duplicate'],
    'dupliquer':     ['mesh.duplicate', 'object.duplicate'],
    # Objets
    'origin':        ['object.origin_set'],
    'parent':        ['object.parent_set', 'object.parent_clear',
                       'wm.call_menu;name=VIEW3D_MT_object_parent'],
    'parent menu':   ['wm.call_menu;name=VIEW3D_MT_object_parent'],
    'add menu':      ['wm.call_menu;name=VIEW3D_MT_add'],
    'add':           ['wm.call_menu;name=VIEW3D_MT_add'],
    'ajouter':       ['wm.call_menu;name=VIEW3D_MT_add'],
    'snap':          ['wm.call_menu;name=VIEW3D_MT_snap'],
    'snap menu':     ['wm.call_menu;name=VIEW3D_MT_snap'],
    'aimanter':      ['wm.call_menu;name=VIEW3D_MT_snap'],
    'clear':         ['wm.call_menu;name=VIEW3D_MT_object_clear'],
    'clear menu':    ['wm.call_menu;name=VIEW3D_MT_object_clear'],
    'specials':      ['wm.call_menu;name=VIEW3D_MT_object_specials'],
    'context menu':  ['wm.call_menu;name=VIEW3D_MT_object_context_menu',
                       'wm.call_menu;name=VIEW3D_MT_edit_mesh_context_menu'],
    'make links':    ['wm.call_menu;name=VIEW3D_MT_make_links'],
    'links':         ['wm.call_menu;name=VIEW3D_MT_make_links'],
    'collection menu': ['wm.call_menu;name=VIEW3D_MT_object_collection'],
    'delete menu':   ['wm.call_menu;name=VIEW3D_MT_edit_mesh_delete'],
    'extrude menu':  ['wm.call_menu;name=VIEW3D_MT_edit_mesh_extrude'],
    'normals menu':  ['wm.call_menu;name=VIEW3D_MT_edit_mesh_normals'],
    'vertex group':  ['wm.call_menu;name=VIEW3D_MT_vertex_group'],
    'clean up':      ['wm.call_menu;name=VIEW3D_MT_edit_mesh_clean'],
    'add modifier':  ['wm.call_menu;name=OBJECT_MT_modifier_add'],
    'mode pie':      ['wm.call_menu_pie;name=VIEW3D_MT_object_mode_pie'],
    'snap pie':      ['wm.call_menu_pie;name=VIEW3D_MT_snap_pie'],
    'mirror':        ['object.mirror', 'mesh.mirror'],
    'symmetrize':    ['mesh.symmetrize'],
    'array':         ['object.modifier_add'],
    'modifier':      ['object.modifier_add', 'object.modifier_remove'],
    # Transform
    'grab':          ['transform.translate'],
    'move':          ['transform.translate'],
    'déplacer':      ['transform.translate'],
    'rotate':        ['transform.rotate'],
    'tourner':       ['transform.rotate'],
    'scale':         ['transform.resize'],
    'resize':        ['transform.resize'],
    # Vue / navigation
    'zoom':          ['view3d.zoom', 'view2d.zoom'],
    'focus':         ['view3d.view_selected', 'view3d.localview'],
    'numpad':        ['view3d.numpad'],
    'camera view':   ['view3d.view_camera'],
    # Sculpt
    'inflate':       ['sculpt.inflate'],
    'clay':          ['sculpt.clay', 'sculpt.clay_strips'],
    'snake hook':    ['sculpt.snake_hook'],
    'flatten':       ['sculpt.flatten'],
    'mask':          ['sculpt.mask_flood_fill', 'paint.mask_flood_fill'],
    # Général
    'undo':          ['ed.undo'],
    'redo':          ['ed.redo'],
    'menu':          ['wm.call_menu'],
    'panel':         ['wm.call_panel'],
    'pie':           ['wm.call_menu_pie'],
    'search':        ['wm.search_menu', 'wm.operator_search'],

    # ── Modes de sélection Edit Mesh ─────────────────────────────────────────
    # mesh.select_mode;type=VERT/EDGE/FACE sont des opérateurs virtuels
    # injectés dans le cache après _build_ops_cache(). Chaque alias pointe
    # vers la variante précise plutôt que vers l'op générique (qui togglerait
    # le mode courant de façon aléatoire).
    'vertex':        ['mesh.select_mode;type=VERT'],
    'vertices':      ['mesh.select_mode;type=VERT'],
    'vertex mode':   ['mesh.select_mode;type=VERT'],
    'select vertex': ['mesh.select_mode;type=VERT'],
    'vert mode':     ['mesh.select_mode;type=VERT'],
    'edge':          ['mesh.select_mode;type=EDGE'],
    'edges':         ['mesh.select_mode;type=EDGE'],
    'edge mode':     ['mesh.select_mode;type=EDGE'],
    'select edge':   ['mesh.select_mode;type=EDGE'],
    'face':          ['mesh.select_mode;type=FACE'],
    'faces':         ['mesh.select_mode;type=FACE'],
    'face mode':     ['mesh.select_mode;type=FACE'],
    'select face':   ['mesh.select_mode;type=FACE'],
    'sommet':        ['mesh.select_mode;type=VERT'],
    'arrête':        ['mesh.select_mode;type=EDGE'],

    # ── Modes de sélection avec action ─────────────────────────────────────
    # SET (par défaut, sans action= → même comportement)
    'set vertex':    ['mesh.select_mode;type=VERT'],
    'set edge':      ['mesh.select_mode;type=EDGE'],
    'set face':      ['mesh.select_mode;type=FACE'],
    # EXTEND
    'extend vertex': ['mesh.select_mode;type=VERT;action=EXTEND'],
    'extend edge':   ['mesh.select_mode;type=EDGE;action=EXTEND'],
    'extend face':   ['mesh.select_mode;type=FACE;action=EXTEND'],
    'extend':        ['mesh.select_mode;type=VERT;action=EXTEND',
                      'mesh.select_mode;type=EDGE;action=EXTEND',
                      'mesh.select_mode;type=FACE;action=EXTEND'],
    # SUBTRACT
    'subtract vertex':['mesh.select_mode;type=VERT;action=SUBTRACT'],
    'subtract edge':  ['mesh.select_mode;type=EDGE;action=SUBTRACT'],
    'subtract face':  ['mesh.select_mode;type=FACE;action=SUBTRACT'],
    'subtract':       ['mesh.select_mode;type=VERT;action=SUBTRACT',
                       'mesh.select_mode;type=EDGE;action=SUBTRACT',
                       'mesh.select_mode;type=FACE;action=SUBTRACT'],
    # TOGGLE mode
    'toggle vertex': ['mesh.select_mode;type=VERT;action=TOGGLE'],
    'toggle edge':   ['mesh.select_mode;type=EDGE;action=TOGGLE'],
    'toggle face':   ['mesh.select_mode;type=FACE;action=TOGGLE'],
}

# ── Génération automatique d'alias depuis labels/idnames ─────────────────────
# Construite une fois au moment du _build_ops_cache, enrichit les alias
_auto_alias: dict[str, list[str]] = {}   # mot → [full_id, ...]

def _build_auto_alias(cache_entries: list) -> dict[str, list[str]]:
    """
    Pour chaque opérateur, tokenise label + op_name et crée des entrées
    dans le dict alias mot→full_id.
    Permet de trouver 'merge' même en tapant 'fusionner' via la table fixe,
    et aussi de trouver les variantes de nommage automatiquement.
    """
    import re
    result: dict[str, list[str]] = {}
    seen: set[tuple] = set()

    for entry in cache_entries:
        full_id, label, desc, module, is_addon, source, domain = entry
        # Tokenise le label (mots significatifs ≥4 chars)
        words = re.findall(r'[a-z]{4,}', label.lower())
        # Tokenise l'op_name (ex: "loopcut_slide" → ["loopcut","slide"])
        op_part = full_id.split('.', 1)[-1] if '.' in full_id else full_id
        words += re.findall(r'[a-z]{4,}', op_part.replace('_', ' '))
        # ── Tokenise aussi desc (enrichi avec les enum RNA au build) ─────────
        # Permet à "vert", "edge", "face" (issus de VERT/EDGE/FACE enum) d'être
        # des alias automatiques sans passer par la table manuelle.
        words += re.findall(r'[a-z]{4,}', desc.lower())
        for w in words:
            key = (w, full_id)
            if key not in seen:
                seen.add(key)
                result.setdefault(w, []).append(full_id)

    return result

# ── Cache principal ──────────────────────────────────────────────────────────
# Chaque entrée : (full_id, label, desc, module, is_addon, source, domain)
_ops_cache:        list = []
_ops_cache_built:  bool = False
_addon_module_map: dict = {}

def _build_ops_cache():
    global _ops_cache, _ops_cache_built, _addon_module_map, _auto_alias

    # ── Détection addons actifs ──────────────────────────────────────────
    _addon_module_map = {}
    for mod in addon_utils.modules():
        try:
            enabled, _ = addon_utils.check(mod.__name__)
            if enabled:
                info = addon_utils.module_bl_info(mod)
                name = info.get('name', mod.__name__)
                pkg  = mod.__name__.split('.')[-1]
                _addon_module_map[pkg]          = name
                _addon_module_map[mod.__name__] = name
        except Exception:
            pass

    # ── Parcours de bpy.ops ──────────────────────────────────────────────
    _ops_cache = []
    skip = {'gizmogroup','uilist','make','menu','remove','unlink',
            'collision_layers_list','collision_masks_list','data',
            'default_collision_layers_list','default_collision_masks_list',
            'default_group_list','default_render_layers_list',
            'groups_list','render_layers_list'}
    for mod_name in sorted(dir(bpy.ops)):
        if mod_name.startswith('_') or mod_name in skip:
            continue
        if mod_name == 'holdkeys':
            continue
        mod      = getattr(bpy.ops, mod_name)
        is_addon = mod_name not in _NATIVE_MODULES
        source   = _addon_module_map.get(mod_name, mod_name) if is_addon else 'Blender'
        domain   = _get_domain(mod_name) if not is_addon else 'Addon'
        try:
            op_names = sorted(dir(mod))
        except Exception:
            continue
        for op_name in op_names:
            if op_name.startswith('_'):
                continue
            full_id = f"{mod_name}.{op_name}"
            try:
                rna   = getattr(mod, op_name).get_rna_type()
                label = rna.name or ''
                desc  = rna.description or ''
                # ── Enrichissement : ajouter les valeurs d'enum RNA ──────────
                # Ex : mesh.select_mode a type VERT/EDGE/FACE → leurs noms
                # ("Vertices", "Edges", "Faces") et identifiants sont indexés
                # dans desc pour que "vert", "edge", "face" soient trouvables.
                try:
                    extra = []
                    for prop in rna.properties:
                        if prop.identifier == 'rna_type':
                            continue
                        if hasattr(prop, 'enum_items'):
                            for item in prop.enum_items:
                                if item.identifier:
                                    extra.append(item.identifier.lower())
                                if item.name:
                                    extra.append(item.name.lower())
                    if extra:
                        desc = (desc + ' ' + ' '.join(extra)).strip()
                except Exception:
                    pass
                if label in {'(undocumented operator)', ''}:
                    label = op_name.replace('_', ' ').title()
            except Exception:
                label = op_name.replace('_', ' ').title()
                desc  = ''
            _ops_cache.append((full_id, label, desc, mod_name, is_addon, source, domain))

    # ── Auto-alias depuis le cache ───────────────────────────────────────
    _auto_alias = _build_auto_alias(_ops_cache)

    # ── Entrées virtuelles : variantes précises de mesh.select_mode ─────
    # L'opérateur générique mesh.select_mode sans kwarg togglerait le mode
    # courant de façon imprévisible. On injecte trois entrées virtuelles
    # (avec le kwarg type= dans l'idname) pour que la recherche et les alias
    # pointent vers des actions précises et déterministes.
    # On injecte aussi des variantes action= pour les modes SET / EXTEND / SUBTRACT /
    # TOGGLE, ainsi que mesh.select_all et object.select_all avec action= explicite.
    _VIRTUAL_SELECT_MODES = [
        # ── Modes de sélection standard (SET seul = comportement natif) ──────
        ('mesh.select_mode;type=VERT', 'Select Vertex Mode',
         'Switch to vertex selection mode set vertices vert',     'mesh'),
        ('mesh.select_mode;type=EDGE', 'Select Edge Mode',
         'Switch to edge selection mode set edges edge',          'mesh'),
        ('mesh.select_mode;type=FACE', 'Select Face Mode',
         'Switch to face selection mode set faces face',          'mesh'),

        # ── EXTEND : ajoute le type au mode courant (Shift+clic dans Blender) ─
        ('mesh.select_mode;type=VERT;action=EXTEND', 'Extend Vertex Mode',
         'Add vertex to selection mode extend vertices vert',     'mesh'),
        ('mesh.select_mode;type=EDGE;action=EXTEND', 'Extend Edge Mode',
         'Add edge to selection mode extend edges edge',          'mesh'),
        ('mesh.select_mode;type=FACE;action=EXTEND', 'Extend Face Mode',
         'Add face to selection mode extend faces face',          'mesh'),

        # ── SUBTRACT : retire le type du mode courant ─────────────────────────
        ('mesh.select_mode;type=VERT;action=SUBTRACT', 'Subtract Vertex Mode',
         'Remove vertex from selection mode subtract vert',       'mesh'),
        ('mesh.select_mode;type=EDGE;action=SUBTRACT', 'Subtract Edge Mode',
         'Remove edge from selection mode subtract edge',         'mesh'),
        ('mesh.select_mode;type=FACE;action=SUBTRACT', 'Subtract Face Mode',
         'Remove face from selection mode subtract face',         'mesh'),

        # ── TOGGLE : bascule le type individuellement ─────────────────────────
        ('mesh.select_mode;type=VERT;action=TOGGLE', 'Toggle Vertex Mode',
         'Toggle vertex selection mode toggle vert',              'mesh'),
        ('mesh.select_mode;type=EDGE;action=TOGGLE', 'Toggle Edge Mode',
         'Toggle edge selection mode toggle edge',                'mesh'),
        ('mesh.select_mode;type=FACE;action=TOGGLE', 'Toggle Face Mode',
         'Toggle face selection mode toggle face',                'mesh'),

        # ── mesh.select_all variants ──────────────────────────────────────────
        ('mesh.select_all;action=SELECT',   'Select All (Mesh)',
         'Select all mesh elements select tout',                  'mesh'),
        ('mesh.select_all;action=DESELECT', 'Deselect All (Mesh)',
         'Deselect all mesh elements deselect tout désélectionner','mesh'),
        ('mesh.select_all;action=INVERT',   'Invert Selection (Mesh)',
         'Invert mesh selection inverser sélection',              'mesh'),
        ('mesh.select_all;action=TOGGLE',   'Toggle Selection (Mesh)',
         'Toggle all mesh selection A basculer tout',             'mesh'),

        # ── object.select_all variants ────────────────────────────────────────
        ('object.select_all;action=SELECT',   'Select All Objects',
         'Select all objects select tout objet',                  'object'),
        ('object.select_all;action=DESELECT', 'Deselect All Objects',
         'Deselect all objects deselect tout désélectionner',     'object'),
        ('object.select_all;action=INVERT',   'Invert Selection (Objects)',
         'Invert objects selection inverser sélection',           'object'),
        ('object.select_all;action=TOGGLE',   'Toggle Selection (Objects)',
         'Toggle all objects selection A basculer tout',          'object'),
    ]
    for full_id, label, desc, module in _VIRTUAL_SELECT_MODES:
        _ops_cache.append((full_id, label, desc, module,
                           False, 'Blender', 'Maillage'))

    # ── Entrées virtuelles : menus Blender courants (Apply, Add, …) ───────
    # Ces menus ne sont pas des opérateurs bpy.ops mais des appels
    # wm.call_menu;name=XXX_MT_yyy. On les injecte comme entrées virtuelles
    # pour qu'ils soient trouvables et assignables comme un opérateur normal
    # (recherche, alias "apply"/"add"/…), sans devoir taper l'idname à la main.
    _VIRTUAL_MENUS = [
        # ── Objet ──────────────────────────────────────────────────────
        ('wm.call_menu;name=VIEW3D_MT_object_apply', 'Apply Menu',
         'apply transform all rotation scale location apply menu appliquer',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_add', 'Add Menu',
         'add mesh curve object ajouter menu shift a',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_object_parent', 'Parent Menu',
         'parent set clear menu parenter',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_object_context_menu', 'Object Context Menu',
         'object context menu right click',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_snap', 'Snap Menu',
         'snap menu cursor selection aimanter',
         'view3d', 'Vue 3D'),
        ('wm.call_menu;name=VIEW3D_MT_object_clear', 'Clear Menu',
         'clear location rotation scale origin reset menu effacer',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_object_specials', 'Object Special Menu',
         'object special menu quick favorites',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_make_links', 'Make Links Menu',
         'make links menu objects data link',
         'object', 'Objet'),
        ('wm.call_menu;name=VIEW3D_MT_object_collection', 'Collection Menu',
         'collection menu link move',
         'object', 'Objet'),
        # ── Mesh / Edit Mode ───────────────────────────────────────────
        ('wm.call_menu;name=VIEW3D_MT_edit_mesh_delete', 'Mesh Delete Menu',
         'delete menu vertices edges faces supprimer menu',
         'mesh', 'Maillage'),
        ('wm.call_menu;name=VIEW3D_MT_edit_mesh_extrude', 'Extrude Menu',
         'extrude menu extruder',
         'mesh', 'Maillage'),
        ('wm.call_menu;name=VIEW3D_MT_edit_mesh_context_menu', 'Mesh Context Menu',
         'mesh context menu right click',
         'mesh', 'Maillage'),
        ('wm.call_menu;name=VIEW3D_MT_edit_mesh_normals', 'Normals Menu',
         'normals menu flip recalculate',
         'mesh', 'Maillage'),
        ('wm.call_menu;name=VIEW3D_MT_vertex_group', 'Vertex Group Menu',
         'vertex group menu assign remove',
         'mesh', 'Maillage'),
        ('wm.call_menu;name=VIEW3D_MT_edit_mesh_clean', 'Mesh Clean Up Menu',
         'clean up menu mesh degenerate',
         'mesh', 'Maillage'),
        # ── Modifiers / Mirror ─────────────────────────────────────────
        ('wm.call_menu;name=OBJECT_MT_modifier_add', 'Add Modifier Menu',
         'add modifier menu ajouter modificateur',
         'object', 'Objet'),
        # ── Pie menus utiles ─────────────────────────────────────────────
        ('wm.call_menu_pie;name=VIEW3D_MT_object_mode_pie', 'Mode Pie Menu',
         'mode pie menu switch object edit sculpt',
         'object', 'Objet'),
        ('wm.call_menu_pie;name=VIEW3D_MT_snap_pie', 'Snap Pie Menu',
         'snap pie menu aimanter',
         'view3d', 'Vue 3D'),
        # ── Fichier / Édition ───────────────────────────────────────────
        ('wm.call_menu;name=TOPBAR_MT_file', 'File Menu',
         'file menu open save fichier',
         'wm', 'Interface'),
        ('wm.call_menu;name=TOPBAR_MT_edit', 'Edit Menu',
         'edit menu undo redo édition',
         'wm', 'Interface'),
    ]
    for full_id, label, desc, module, domain in _VIRTUAL_MENUS:
        _ops_cache.append((full_id, label, desc, module,
                           False, 'Blender', domain))

    # Reconstruire l'auto-alias pour inclure les virtuels
    _auto_alias = _build_auto_alias(_ops_cache)

    _ops_cache_built = True
    print(f"[HoldKeys] Cache: {len(_ops_cache)} opérateurs "
          f"| {len(_auto_alias)} mots auto-alias")

def _invalidate_ops_cache():
    global _ops_cache_built
    _ops_cache_built = False

def _get_cache():
    if not _ops_cache_built:
        _build_ops_cache()
    return _ops_cache

# ── Résolution d'alias ───────────────────────────────────────────────────────

def _resolve_aliases(tokens: list[str]) -> set[str]:
    """
    Pour chaque token (et paires de tokens consécutifs), retourne l'ensemble
    des full_id boostés par les alias fixes + auto-alias.
    """
    boosted: set[str] = set()
    # Alias fixes (table + paires)
    for i, t in enumerate(tokens):
        for ids in _ALIAS_TABLE.get(t, []):
            boosted.add(ids)
        # Paire "token token+1"
        if i < len(tokens) - 1:
            pair = f"{t} {tokens[i+1]}"
            for ids in _ALIAS_TABLE.get(pair, []):
                boosted.add(ids)
    # Auto-alias (mots du label/op_name)
    for t in tokens:
        for full_id in _auto_alias.get(t, []):
            boosted.add(full_id)
    return boosted

# ── Fuzzy : match préfixe par token (≥3 chars) ──────────────────────────────

def _fuzzy_match(tokens: list[str], haystack: str) -> bool:
    """
    Chaque token de ≥3 chars doit apparaître comme préfixe d'un mot
    dans le haystack (ex: 'dis' matche 'dissolve').
    Tokens <3 chars doivent être présents exactement.
    """
    for t in tokens:
        if len(t) < 3:
            if t not in haystack:
                return False
        else:
            # Préfixe : le haystack doit contenir un mot commençant par t
            import re
            if not re.search(r'\b' + re.escape(t), haystack):
                return False
    return True

# ── Scoring 12 niveaux ───────────────────────────────────────────────────────

def _score_entry(full_id: str, label: str, desc: str,
                 q: str, tokens: list[str],
                 boosted_ids: set[str]) -> int | None:
    """
    Retourne un score (plus bas = plus pertinent) ou None si aucun match.

    Niveaux :
      0  idname exact
      1  idname commence par q
      2  label exact
      3  label commence par token[0]
      4  idname contient q (substring)
      5  label contient q (substring)
      6  tous les tokens dans id+label (exact)
      7  alias boost (table ou auto)
      8  description contient q
      9  fuzzy préfixe sur id+label
     10  fuzzy préfixe sur description
     11  match partiel (au moins 1 token)
    """
    if not tokens:
        return 9   # sans query : tout passe, score neutre

    id_l    = full_id.lower()
    label_l = label.lower()
    desc_l  = desc.lower()

    # Niveaux exacts (0-5)
    if id_l == q:                                    return 0
    if id_l.startswith(q):                           return 1
    if label_l == q:                                 return 2
    if label_l.startswith(tokens[0]):                return 3
    if q in id_l:                                    return 4
    if q in label_l:                                 return 5

    # Niveau 6 : tous tokens présents exactement dans id+label
    haystack_exact = f"{id_l} {label_l}"
    if all(t in haystack_exact for t in tokens):     return 6

    # Niveau 7 : alias boost
    if full_id in boosted_ids:                       return 7

    # Niveau 8 : description substring
    if q in desc_l:                                  return 8

    # Niveau 9 : fuzzy préfixe id+label
    if _fuzzy_match(tokens, haystack_exact):         return 9

    # Niveau 10 : fuzzy préfixe description
    if _fuzzy_match(tokens, desc_l):                 return 10

    # Niveau 11 : au moins un token dans id+label
    if any(t in haystack_exact for t in tokens):     return 11

    return None   # aucun match

# ── Fonction principale de recherche ─────────────────────────────────────────

def _search_ops(query:         str  = '',
                source_filter: str  = 'ALL',
                domain_filter: str  = 'ALL',
                limit:         int  = 200) -> list[tuple]:
    """
    Recherche dans le cache avec filtres source/domaine.

    source_filter : 'ALL' | 'BLENDER' | 'ADDON' | nom_addon_exact
    domain_filter : 'ALL' | nom_domaine (clé de _DOMAIN_MODULES) | 'Addon'
    Retourne une liste de tuples (full_id, label, desc, module, is_addon, source, domain).
    """
    cache = _get_cache()
    q     = query.strip().lower()
    tokens = q.split() if q else []

    boosted = _resolve_aliases(tokens) if tokens else set()

    candidates: list[tuple[int, str, tuple]] = []

    for entry in cache:
        full_id, label, desc, module, is_addon, source, domain = entry

        # ── Filtres ──────────────────────────────────────────────────────
        if source_filter == 'BLENDER' and is_addon:
            continue
        if source_filter == 'ADDON' and not is_addon:
            continue
        if source_filter not in ('ALL', 'BLENDER', 'ADDON') and source != source_filter:
            continue
        if domain_filter != 'ALL' and domain != domain_filter:
            continue

        # ── Scoring ──────────────────────────────────────────────────────
        score = _score_entry(full_id, label, desc, q, tokens, boosted)
        if score is None:
            continue

        candidates.append((score, full_id, entry))

    # Tri : score d'abord, puis full_id alphabétique pour la stabilité
    candidates.sort(key=lambda x: (x[0], x[1]))
    return [e for _, _, e in candidates[:limit]]

# ── Helpers UI : listes dynamiques pour les filtres ─────────────────────────

def _source_filter_items(self, context):
    """EnumProperty items : sources disponibles dans le cache."""
    if not _ops_cache_built:
        return [('ALL', 'Toutes les sources', '', 'WORLD', 0)]
    items = [
        ('ALL',     'Toutes les sources', '', 'WORLD',  0),
        ('BLENDER', 'Blender natif',      '', 'BLENDER',1),
        ('ADDON',   'Tous les addons',    '', 'PLUGIN', 2),
    ]
    seen: set[str] = set()
    for entry in _ops_cache:
        _, _, _, _, is_addon, source, _ = entry
        if is_addon and source not in seen:
            seen.add(source)
            items.append((source, source, '', 'SCRIPTPLUGINS', len(items)))
    return items

def _domain_filter_items(self, context):
    """EnumProperty items : domaines disponibles selon le filtre source actif."""
    wm = getattr(context, 'window_manager', None)
    if wm is None or not _ops_cache_built:
        return [('ALL', 'Tous les domaines', '', 'FILTER', 0)]

    sf    = getattr(wm, 'holdkeys_source_filter', 'ALL')
    items = [('ALL', 'Tous les domaines', '', 'FILTER', 0)]

    if sf in ('ALL', 'BLENDER'):
        for i, domain in enumerate(sorted(_DOMAIN_MODULES.keys()), 1):
            items.append((domain, domain, '', 'DOT', i))
    if sf in ('ALL', 'ADDON'):
        items.append(('Addon', 'Addons (tous)', '', 'PLUGIN', len(items)))

    return items

# ── wm properties pour la recherche (debounce 150ms) ─────────────────────────

_search_timer = None

def _flush_search():
    global _search_timer
    _search_timer = None
    wm = bpy.context.window_manager
    pending = getattr(wm, 'holdkeys_search_pending', '')
    if getattr(wm, 'holdkeys_search_query', '') != pending:
        wm.holdkeys_search_query = pending
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == 'PREFERENCES':
                    area.tag_redraw()
    return None

def _on_search_update(self, context):
    global _search_timer
    if bpy.app.timers.is_registered(_flush_search):
        bpy.app.timers.unregister(_flush_search)
    _search_timer = True
    bpy.app.timers.register(_flush_search, first_interval=0.15)

# ─────────────────────────────────────────────────────────────────────────────
# Capture de touche
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_OT_capture_key(Operator):
    """Appuyez sur la touche à assigner (Échap pour annuler)."""
    bl_idname = "holdkeys.capture_key"
    bl_label  = "Capturer une touche"

    binding_index: IntProperty(default=-1)

    _IGNORE = {
        'MOUSEMOVE','INBETWEEN_MOUSEMOVE','MOUSE_X','MOUSE_Y',
        'WINDOW_DEACTIVATE','TIMER','TIMER0','TIMER1','TIMER2',
        'LEFT_CTRL','RIGHT_CTRL','LEFT_SHIFT','RIGHT_SHIFT',
        'LEFT_ALT','RIGHT_ALT','OSKEY','NONE',
    }

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in self._IGNORE or event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        if event.type == 'ESC':
            return {'CANCELLED'}

        prefs = _get_prefs(context)
        idx   = self.binding_index
        if not (0 <= idx < len(prefs.bindings)):
            return {'CANCELLED'}

        b = prefs.bindings[idx]
        b.use_ctrl  = event.ctrl
        b.use_shift = event.shift
        b.use_alt   = event.alt
        b.use_oskey = event.oskey

        # Convertir le type Blender en notre KEY_TYPE_ITEMS
        # Les lettres/chiffres sont déjà au bon format
        b.key_type = event.type

        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'PREFERENCES':
                    area.tag_redraw()
        return {'FINISHED'}

# ─────────────────────────────────────────────────────────────────────────────
# Assign operator depuis la recherche
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_OT_set_key(Operator):
    """Assigne directement une valeur de touche au binding (contourne la capture pour les touches interceptées par Blender comme TAB)."""
    bl_idname = "holdkeys.set_key"
    bl_label  = "Assigner touche directement"

    binding_index: IntProperty(default=-1)
    key_value:     StringProperty(default="TAB")

    def execute(self, context):
        prefs = _get_prefs(context)
        idx   = self.binding_index
        if not (0 <= idx < len(prefs.bindings)):
            return {'CANCELLED'}
        prefs.bindings[idx].key_type = self.key_value
        return {'FINISHED'}


class HOLDKEYS_OT_assign_op(Operator):
    """Assigne l'opérateur sélectionné au binding actif."""
    bl_idname = "holdkeys.assign_op"
    bl_label  = "Assigner"

    op_idname:     StringProperty()
    binding_index: IntProperty(default=-1)
    # target : "native" ou "threshold_N" (N = index du seuil)
    target:        StringProperty(default="threshold_0")

    def execute(self, context):
        prefs = _get_prefs(context)
        idx   = self.binding_index
        if not (0 <= idx < len(prefs.bindings)):
            return {'CANCELLED'}
        b = prefs.bindings[idx]
        if self.target == "native":
            b.native_operator = self.op_idname
        elif self.target.startswith("threshold_"):
            try:
                t_i = int(self.target.split("_", 1)[1])
            except (ValueError, IndexError):
                return {'CANCELLED'}
            if 0 <= t_i < len(b.thresholds):
                b.thresholds[t_i].operator = self.op_idname
                _rebuild_keymaps()
        _rebuild_keymaps()
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'PREFERENCES':
                    area.tag_redraw()
        return {'FINISHED'}

class HOLDKEYS_OT_rebuild_cache(Operator):
    """Reconstruit le cache des opérateurs (après activation d'un addon)."""
    bl_idname = "holdkeys.rebuild_cache"
    bl_label  = "Rafraîchir le cache"
    def execute(self, context):
        _invalidate_ops_cache()
        _build_ops_cache()
        self.report({'INFO'}, f"{len(_ops_cache)} opérateurs indexés")
        return {'FINISHED'}

def _call_op(op_string: str, ctx_window=None, ctx_area=None, ctx_region=None):
    """
    Appelle un opérateur depuis son idname.
    Syntaxe kwargs : "wm.call_menu;name=VIEW3D_MT_edit_mesh_delete"
    ctx_window/area/region : contexte sauvegardé au PRESS pour temp_override.
    """
    if not op_string:
        return
    kwargs = {}
    if ";" in op_string:
        parts = op_string.split(";")
        op_string = parts[0].strip()
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                kwargs[k.strip()] = v.strip()
    mod, fn = (op_string.split(".", 1) + [""])[:2]
    if not fn:
        print(f"[HoldKeys] idname invalide: {op_string!r}")
        return
    func = getattr(getattr(bpy.ops, mod, None), fn, None)
    if func is None:
        print(f"[HoldKeys] op introuvable: {op_string!r}")
        return
    override = {k: v for k, v in (('window', ctx_window),
                                   ('area',   ctx_area),
                                   ('region', ctx_region)) if v is not None}
    try:
        if override:
            with bpy.context.temp_override(**override):
                func('INVOKE_DEFAULT', **kwargs)
        else:
            func('INVOKE_DEFAULT', **kwargs)
    except Exception as e:
        print(f"[HoldKeys] erreur {op_string}: {e}")

def _context_keymap_priority(ctx_area=None, ctx_region=None) -> list:
    """
    Retourne la liste ordonnée de noms de keymaps Blender à consulter
    pour retrouver le kmi natif d'une touche.

    Ordre : mode le plus spécifique → space → generics → globaux.
    Couvre tous les modes 3D View, les éditeurs 2D, et les espaces
    spéciaux de Blender 5.x (Grease Pencil, Curves, Spreadsheet…).
    """
    space_type  = ctx_area.type    if ctx_area   else None
    region_type = ctx_region.type  if ctx_region else None

    # ── Détection du mode actif ──────────────────────────────────────────
    # On part de l'active_object, ou du mode du contexte courant.
    mode = None
    obj  = getattr(bpy.context, 'active_object', None)
    if obj:
        m = obj.mode
        if m == 'EDIT':
            # Affiner selon le type d'objet en édition
            obj_to_edit_mode = {
                'MESH':     'EDIT_MESH',
                'CURVE':    'EDIT_CURVE',
                'SURFACE':  'EDIT_SURFACE',
                'META':     'EDIT_METABALL',
                'FONT':     'EDIT_FONT',
                'ARMATURE': 'EDIT_ARMATURE',
                'LATTICE':  'EDIT_LATTICE',
                'GPENCIL':  'EDIT_GPENCIL',
                'CURVES':   'EDIT_CURVES',
            }
            mode = obj_to_edit_mode.get(obj.type, 'EDIT_MESH')
        else:
            mode = m

    # ── Keymaps par mode (du plus spécifique au plus général) ───────────
    # Chaque entrée = liste de noms de keymaps Blender dans l'ordre de
    # priorité pour ce mode. Les sub-keymaps (Vertex/Edge/Face) viennent
    # en premier quand la sélection est connue.
    MODE_TO_KEYMAPS: dict[str, list[str]] = {
        # ── 3D View : modes objet ──
        'OBJECT':        ['Object Mode', 'Object Non-modal'],
        'POSE':          ['Pose'],
        'SCULPT':        ['Sculpt'],
        'PAINT_WEIGHT':  ['Weight Paint'],
        'PAINT_VERTEX':  ['Vertex Paint'],
        'PAINT_TEXTURE': ['Image Paint'],
        'PARTICLE_EDIT': ['Particle'],

        # ── 3D View : modes édition ──
        # Mesh : les sub-keymaps Vertex/Edge/Face contiennent les
        # raccourcis liés au type de sélection actif, puis le keymap
        # global 'Mesh' couvre les commandes communes.
        'EDIT_MESH':     ['Mesh Vertex', 'Mesh Edge', 'Mesh Face',
                           'Mesh', 'Object Non-modal'],
        'EDIT_CURVE':    ['Curve'],
        'EDIT_SURFACE':  ['Surface'],
        'EDIT_METABALL': ['Metaball'],
        'EDIT_FONT':     ['Font'],
        'EDIT_ARMATURE': ['Armature'],
        'EDIT_LATTICE':  ['Lattice'],

        # ── Grease Pencil (Blender 3.x legacy + 4.x/5.x) ──
        'EDIT_GPENCIL':   ['Grease Pencil Stroke Edit Mode',
                            'Grease Pencil'],
        'SCULPT_GPENCIL': ['Grease Pencil Stroke Sculpt Mode',
                            'Grease Pencil Stroke Sculpt (common)',
                            'Grease Pencil'],
        'PAINT_GPENCIL':  ['Grease Pencil Stroke Paint (Draw)',
                            'Grease Pencil Stroke Paint Mode',
                            'Grease Pencil'],
        'WEIGHT_GPENCIL': ['Grease Pencil Stroke Weight Mode',
                            'Grease Pencil'],
        'VERTEX_GPENCIL': ['Grease Pencil Stroke Vertex Mode',
                            'Grease Pencil'],

        # ── Curves (géométrie — Blender 3.3+) ──
        'EDIT_CURVES':   ['Curves'],
        'SCULPT_CURVES': ['Sculpt Curves'],
    }

    priority: list[str] = []

    # 1. Keymaps du mode actif
    if mode and mode in MODE_TO_KEYMAPS:
        priority.extend(MODE_TO_KEYMAPS[mode])

    # 2. Keymaps liés au space_type (éditeurs 2D et spéciaux)
    SPACE_TO_KEYMAPS: dict[str, list[str]] = {
        'VIEW_3D':          ['3D View', 'View3D Generic'],
        'IMAGE_EDITOR':     ['Image', 'Image Paint', 'UV Editor'],
        'NODE_EDITOR':      ['Node Editor', 'Node Generic'],
        'GRAPH_EDITOR':     ['Graph Editor', 'Graph Editor Generic'],
        'DOPESHEET_EDITOR': ['Dopesheet', 'Dopesheet Generic'],
        'NLA_EDITOR':       ['NLA Editor', 'NLA Generic'],
        'OUTLINER':         ['Outliner'],
        'SEQUENCE_EDITOR':  ['Sequencer', 'SequencerCommon',
                               'Sequencer Preview'],
        'CLIP_EDITOR':      ['Clip', 'Clip Editor',
                               'Clip Graph Editor', 'Clip Dopesheet Editor'],
        'TEXT_EDITOR':      ['Text', 'Text Generic'],
        'FILE_BROWSER':     ['File Browser', 'File Browser Main',
                               'File Browser Buttons'],
        'CONSOLE':          ['Console'],
        'INFO':             ['Info'],
        'PROPERTIES':       ['Property Editor'],
        'SPREADSHEET':      ['Spreadsheet Generic'],
        'PREFERENCES':      ['Preferences'],
    }
    if space_type and space_type in SPACE_TO_KEYMAPS:
        for km_name in SPACE_TO_KEYMAPS[space_type]:
            if km_name not in priority:
                priority.append(km_name)

    # 3. Animation / timeline (communs à plusieurs éditeurs)
    if space_type in {'GRAPH_EDITOR', 'DOPESHEET_EDITOR',
                      'NLA_EDITOR', 'SEQUENCE_EDITOR'}:
        if 'Frames' not in priority:
            priority.append('Frames')

    # 4. Globaux (toujours en dernier)
    for km_name in ['Window', 'Screen', 'Screen Editing']:
        if km_name not in priority:
            priority.append(km_name)

    return priority

def _find_native_kmi(key_type: str, use_shift: bool, use_ctrl: bool,
                     use_alt: bool, use_oskey: bool,
                     ctx_area=None, ctx_region=None):
    """
    Retourne le kmi natif (hors holdkeys) le plus adapté au contexte.
    Priorité : keymaps mode-actif → space_type → globaux → fallback.
    Exclut keyconfigs.addon pour ne pas trouver le proxy lui-même.
    """
    wm = bpy.context.window_manager

    def _matches(kmi):
        return (kmi.idname != "holdkeys.proxy"
                and kmi.type        == key_type
                and bool(kmi.shift) == use_shift
                and bool(kmi.ctrl)  == use_ctrl
                and bool(kmi.alt)   == use_alt
                and bool(kmi.oskey) == use_oskey
                and kmi.active
                and kmi.value in {"PRESS", "CLICK", "ANY"})

    # Exclure addon : le proxy y est enregistré, on veut le natif pur
    kc_order = [kc for kc in (wm.keyconfigs.default,
                               wm.keyconfigs.active,
                               wm.keyconfigs.user)
                if kc is not None]

    priority_names = _context_keymap_priority(ctx_area, ctx_region)

    # Passe 1 : keymaps prioritaires dans l'ordre du contexte détecté
    for km_name in priority_names:
        for kc in kc_order:
            for km in kc.keymaps:
                if km.name == km_name:
                    for kmi in km.keymap_items:
                        if _matches(kmi):
                            return kmi

    # Passe 2 : fallback exhaustif sur tous les keymaps restants
    for kc in kc_order:
        for km in kc.keymaps:
            for kmi in km.keymap_items:
                if _matches(kmi):
                    return kmi

    return None

def _call_native(key_type: str, use_shift: bool, use_ctrl: bool,
                 use_alt: bool, use_oskey: bool,
                 ctx_window=None, ctx_area=None, ctx_region=None):
    """
    Trouve et exécute l'opérateur natif Blender lié à cette touche,
    en tenant compte du mode et du contexte actifs.
    """
    kmi = _find_native_kmi(key_type, use_shift, use_ctrl, use_alt, use_oskey,
                           ctx_area=ctx_area, ctx_region=ctx_region)
    if kmi is None:
        return
    parts = kmi.idname.split(".", 1)
    if len(parts) != 2:
        return
    func = getattr(getattr(bpy.ops, parts[0], None), parts[1], None)
    if func is None:
        print(f"[HoldKeys] Op natif introuvable: {kmi.idname}")
        return
    try:
        props = {p.identifier: getattr(kmi.properties, p.identifier)
                 for p in kmi.properties.bl_rna.properties
                 if p.identifier != "rna_type"}
    except Exception:
        props = {}
    override = {k: v for k, v in (('window', ctx_window),
                                   ('area',   ctx_area),
                                   ('region', ctx_region)) if v is not None}
    try:
        if override:
            with bpy.context.temp_override(**override):
                func("INVOKE_DEFAULT", **props)
        else:
            func("INVOKE_DEFAULT", **props)
    except Exception as e:
        print(f"[HoldKeys] Erreur natif {kmi.idname}: {e}")

def _scan_keymaps(key_type: str, use_shift: bool, use_ctrl: bool,
                  use_alt: bool, use_oskey: bool) -> list[tuple[str, str, str]]:
    """
    Scanne TOUS les keymaps (natifs + addons installés) pour trouver
    où key_type+modifiers est défini.
    Retourne une liste dédupliquée de (keymap_name, space_type, region_type).
    """
    wm = bpy.context.window_manager
    seen: set[tuple] = set()
    results: list[tuple[str, str, str]] = []

    # On scanne les 4 sources dans l'ordre de priorité Blender
    for kc in (wm.keyconfigs.addon,
               wm.keyconfigs.user,
               wm.keyconfigs.active,
               wm.keyconfigs.default):
        if kc is None:
            continue
        for km in kc.keymaps:
            for kmi in km.keymap_items:
                if (kmi.type   == key_type
                        and bool(kmi.shift)  == use_shift
                        and bool(kmi.ctrl)   == use_ctrl
                        and bool(kmi.alt)    == use_alt
                        and bool(kmi.oskey)  == use_oskey
                        and kmi.active):
                    loc = (km.name, km.space_type, km.region_type)
                    if loc not in seen:
                        seen.add(loc)
                        results.append(loc)
    return results

# ─────────────────────────────────────────────────────────────────────────────
# HUD — jauge d'énergie en bas-gauche (feedback visuel du hold)
# ─────────────────────────────────────────────────────────────────────────────

# Palette HUD
_HUD_BG_COLOR        = (0.08, 0.08, 0.08, 0.82)   # fond arrondi
_HUD_BAR_EMPTY_COLOR = (0.18, 0.18, 0.18, 0.90)   # piste vide
_HUD_BAR_FILL_IDLE   = (0.25, 0.55, 1.00, 1.00)   # bleu — entre deux seuils
_HUD_BAR_FILL_HIT    = (0.20, 0.90, 0.45, 1.00)   # vert — seuil atteint
_HUD_BAR_FILL_MAX    = (1.00, 0.55, 0.10, 1.00)   # orange — dernier seuil
_HUD_TICK_COLOR      = (1.00, 1.00, 1.00, 0.75)   # graduations
_HUD_TICK_HIT_COLOR  = (1.00, 0.90, 0.20, 1.00)   # graduation atteinte
_HUD_TEXT_COLOR      = (1.00, 1.00, 1.00, 0.95)   # texte principal
_HUD_SUB_COLOR       = (0.70, 0.70, 0.70, 0.85)   # texte secondaire
_HUD_BORDER_COLOR    = (0.40, 0.40, 0.40, 0.50)   # bordure

# Géométrie HUD
_HUD_X          = 24     # marge gauche
_HUD_Y          = 32     # marge bas
_HUD_W          = 260    # largeur totale
_HUD_BAR_H      = 14     # hauteur de la piste
_HUD_PAD        = 12     # padding interne (horizontal)
_HUD_PAD_V      = 9      # padding vertical
_HUD_FONT_SIZE  = 11     # titre
_HUD_SFONT_SIZE = 10     # sous-texte

# Arc radial HUD (autour du curseur)
_HUD_RADIUS     = 18     # rayon de l'anneau en pixels
_HUD_THICKNESS  = 4      # épaisseur de l'anneau en pixels
_HUD_SEGS       = 64     # segments pour le rendu de l'arc

# Cache thème : recalculé une seule fois par hold (invalidé à chaque PRESS)
_hud_theme_cache: tuple | None = None

def _invalidate_hud_theme_cache():
    global _hud_theme_cache
    _hud_theme_cache = None

def _theme_colors():
    """Retourne (col_idle, col_hit, col_max, col_track, col_text) depuis le thème Blender.
    Résultat mis en cache pour la durée du hold — invalider avant chaque PRESS.
    """
    global _hud_theme_cache
    if _hud_theme_cache is not None:
        return _hud_theme_cache
    theme = bpy.context.preferences.themes[0]
    ui    = theme.user_interface

    # Accentuation (idle) — wcol_regular.item ou fallback bleu
    try:
        r, g, b = ui.wcol_regular.item[:3]
        # S'assurer que la couleur est assez lumineuse
        lum = 0.2126*r + 0.7152*g + 0.0722*b
        if lum < 0.15:
            r, g, b = 0.25, 0.55, 1.0
        col_idle = (min(r * 1.3, 1.0), min(g * 1.3, 1.0), min(b * 1.3, 1.0), 1.0)
    except Exception:
        col_idle = (0.25, 0.55, 1.0, 1.0)

    # Succès (hit) — vertex_select du viewport → teinte verte
    try:
        r, g, b = theme.view_3d.vertex_select[:3]
        lum = 0.2126*r + 0.7152*g + 0.0722*b
        if lum < 0.15:
            r, g, b = 0.20, 0.88, 0.45
        col_hit = (min(r * 1.2, 1.0), min(g * 1.2, 1.0), min(b * 1.2, 1.0), 1.0)
    except Exception:
        col_hit = (0.20, 0.88, 0.45, 1.0)

    # Max (warn) — orange fixe, lisible sur tout thème
    col_max = (1.0, 0.55, 0.10, 1.0)

    # Piste (fond anneau) — transparent sombre
    col_track = (0.12, 0.12, 0.12, 0.60)

    # Texte — wcol_regular.text ou blanc
    try:
        r, g, b = ui.wcol_regular.text[:3]
        col_text = (r, g, b, 0.92)
    except Exception:
        col_text = (1.0, 1.0, 1.0, 0.92)

    _hud_theme_cache = col_idle, col_hit, col_max, col_track, col_text
    return _hud_theme_cache


# Shader caché pour éviter de le recréer à chaque frame
_hud_shader = None

def _get_hud_shader():
    global _hud_shader
    if _hud_shader is None:
        _hud_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    return _hud_shader


# Cache des batches d'arc : évite de reconstruire vertices/indices/batch
# à chaque frame (gros gain de fluidité, surtout avec plusieurs seuils).
# Clé = paramètres quantisés (rayon/épaisseur entiers, angle arrondi).
_arc_batch_cache: dict[tuple, object] = {}
_ARC_CACHE_MAX = 256

def _draw_arc(shader, cx, cy, radius, thickness, a_start, a_end, color, segs=80):
    """
    Anneau épais entre a_start et a_end (radians).
    Sens horaire = a_end < a_start.
    Implémenté comme bande de triangles.
    Géométrie mise en cache par (radius, thickness, span, segs) — seule la
    position (cx, cy) varie d'une frame à l'autre pour un rayon/span donnés,
    donc on retranslate les vertices d'un batch existant plutôt que de
    recréer indices + batch_for_shader à chaque appel.
    """
    span = a_end - a_start
    if abs(span) < 0.001:
        return
    r_out = radius + thickness * 0.5
    r_in  = max(0.0, radius - thickness * 0.5)
    steps = max(4, int(segs * abs(span) / (2.0 * math.pi)))

    # Quantisation : angle de départ arrondi à ~0.2° près suffit visuellement
    # et multiplie les hits de cache pendant l'anim (sinon chaque frame a un
    # a_start légèrement différent → cache toujours raté).
    key = (round(radius, 2), round(thickness, 2), round(a_start, 3),
           round(span, 3), steps)

    cached = _arc_batch_cache.get(key)
    if cached is None:
        verts   = []
        indices = []
        for i in range(steps + 1):
            a  = a_start + span * i / steps
            ca = math.cos(a)
            sa = math.sin(a)
            verts.append((r_out * ca, r_out * sa))
            verts.append((r_in  * ca, r_in  * sa))
        for i in range(steps):
            b = i * 2
            indices += [(b, b + 1, b + 2), (b + 1, b + 3, b + 2)]
        if len(_arc_batch_cache) >= _ARC_CACHE_MAX:
            _arc_batch_cache.clear()
        cached = (verts, indices)
        _arc_batch_cache[key] = cached

    verts_local, indices = cached
    # Translation vers la position courante du curseur (cx, cy) — bien moins
    # coûteux que de régénérer cos/sin pour chaque vertex à chaque frame.
    verts = [(cx + vx, cy + vy) for vx, vy in verts_local]
    batch = batch_for_shader(shader, 'TRIS', {'pos': verts}, indices=indices)
    shader.uniform_float('color', color)
    batch.draw(shader)


def _lerp_color(a, b, t):
    """Interpolation linéaire entre deux couleurs RGBA."""
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


def _hud_draw_callback(self, context):
    """
    Callback GPU : arc radial unique autour du curseur.

    Logique « un seul anneau, un tour par seuil » :
      - Les seuils découpent la timeline en segments : [0, T0], [T0, T1], [T1, T2]…
      - Chaque segment correspond à un tour complet (2π) sur le même anneau
        (rayon fixe _HUD_RADIUS) — pas d'anneau empilé par seuil.
      - Au passage d'un seuil, l'anneau se vide et repart de 0 pour le tour
        suivant (couleur mise à jour selon le seuil atteint).
      - Le tour courant progresse en temps réel ; les tours déjà bouclés ne
        laissent qu'un flash bref (pulse) plutôt qu'un anneau supplémentaire.

    Redraw piloté par le timer modal (pas depuis ici) → pas de tag_redraw ici.
    """

    elapsed = time.monotonic() - self._press_time

    prefs      = _get_prefs(context)
    binding    = prefs.bindings[self._binding_idx]
    # Utiliser les seuils pré-triés mis en cache au PRESS (évite sorted() à chaque frame)
    thresholds = self._sorted_thresholds

    if not thresholds:
        return

    # Position souris
    mx = getattr(self, '_mouse_x', None)
    my = getattr(self, '_mouse_y', None)
    if mx is None or my is None:
        mx_win = getattr(self, '_mouse_x_win', None)
        my_win = getattr(self, '_mouse_y_win', None)
        if context.region and mx_win is not None and my_win is not None:
            mx = mx_win - context.region.x
            my = my_win - context.region.y
        elif context.region:
            mx = context.region.width  // 2
            my = context.region.height // 2
        else:
            return

    # ── Segments : [0, T0], [T0, T1], … [T(n-1), Tn] ────────────────────
    # Chaque segment = un tour complet de l'anneau (rayon fixe).
    segment_starts = [0.0] + [t.hold_time for t in thresholds[:-1]]
    segment_ends   = [t.hold_time for t in thresholds]
    n_segs         = len(thresholds)

    # Segment actif et ratio dans ce segment
    active_seg = 0
    seg_ratio  = 0.0
    for i in range(n_segs):
        t_start = segment_starts[i]
        t_end   = segment_ends[i]
        dur     = max(t_end - t_start, 1e-6)
        if elapsed <= t_end:
            active_seg = i
            seg_ratio  = max(0.0, min(1.0, (elapsed - t_start) / dur))
            break
        else:
            active_seg = i  # dernier segment dépassé
            seg_ratio  = 1.0

    # Seuil courant atteint
    current_idx = -1
    for i, t in enumerate(thresholds):
        if elapsed >= t.hold_time:
            current_idx = i

    # ── Couleurs thème ────────────────────────────────────────────────────
    col_idle, col_hit, col_max, col_track, col_text = _theme_colors()

    # Couleur de l'arc en cours
    if current_idx >= n_segs - 1:
        # Dernier seuil dépassé : pulse orange → rouge selon dépassement
        overshoot = min((elapsed - thresholds[-1].hold_time) / 0.3, 1.0)
        fill_color = _lerp_color(col_max, (1.0, 0.15, 0.05, 1.0), overshoot)
    elif current_idx >= 0:
        fill_color = col_hit
    else:
        fill_color = col_idle

    R   = _HUD_RADIUS
    THK = _HUD_THICKNESS
    A0  = math.pi / 2      # 12h
    TAU = 2.0 * math.pi

    gpu.state.blend_set('ALPHA')
    shader = _get_hud_shader()
    shader.bind()

    # ── Piste de fond — un seul anneau, rayon fixe ────────────────────────
    _draw_arc(shader, mx, my, R, THK, A0, A0 - TAU, col_track, segs=_HUD_SEGS)

    # ── Flash bref au moment où un tour vient de se boucler ──────────────
    # (le tour précédent ne laisse pas un anneau permanent en plus : il
    # disparaît dès que le tour suivant démarre, seul l'anneau courant
    # est dessiné en dessous).
    if active_seg > 0 and seg_ratio < 0.12:
        flash_t = 1.0 - (seg_ratio / 0.12)
        flash_col = (col_hit[0], col_hit[1], col_hit[2], col_hit[3] * flash_t * 0.5)
        _draw_arc(shader, mx, my, R, THK, A0, A0 - TAU, flash_col, segs=_HUD_SEGS)

    # ── Arc du tour courant (progression en temps réel) sur l'anneau unique ─
    if seg_ratio > 0.001:
        _draw_arc(shader, mx, my, R, THK,
                  A0, A0 - TAU * seg_ratio, fill_color, segs=_HUD_SEGS)

    # ── Tick à 12h (point de départ/fin de chaque tour) ───────────────────
    tick_col = col_hit if current_idx >= 0 else (1.0, 1.0, 1.0, 0.35)
    _draw_arc(shader, mx, my, R, THK + 4,
              A0 + 0.06, A0 - 0.06, tick_col, segs=4)

    gpu.state.blend_set('NONE')

    # ── Texte : nom de l'action centré sous le curseur (pas de chrono) ────
    text_y = my - R - THK - _HUD_SFONT_SIZE - 6

    if current_idx >= 0:
        t_hit = thresholds[current_idx]
        label = t_hit.label if t_hit.label else f"Seuil {current_idx + 1}"
        blf.size(0, _HUD_SFONT_SIZE)
        blf.color(0, fill_color[0], fill_color[1], fill_color[2], 0.95)
        lw = blf.dimensions(0, label)[0]
        blf.position(0, mx - lw * 0.5, text_y, 0)
        blf.draw(0, label)
    else:
        # Avant le 1er seuil : afficher le label du prochain seuil en grisé
        next_label = thresholds[0].label if thresholds[0].label else f"Seuil 1"
        blf.size(0, _HUD_SFONT_SIZE)
        blf.color(0, 0.55, 0.55, 0.55, 0.70)
        lw = blf.dimensions(0, next_label)[0]
        blf.position(0, mx - lw * 0.5, text_y, 0)
        blf.draw(0, next_label)

    # NB : pas de tag_redraw ici — c'est le timer TIMER du modal qui pilote le redraw

# ─────────────────────────────────────────────────────────────────────────────
# Proxy Operator — un seul operator, instancié N fois dans des keymaps
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_OT_proxy(Operator):
    """
    Opérateur proxy inséré en tête des keymaps natifs (head=True).

    PRESS   → entre en modal, note l'heure, démarre un timer 16ms
    TIMER   → surveille elapsed :
                • si elapsed >= dernier seuil → fire immédiatement et quitte
                • sinon → idle
    RELEASE → si pas encore fire via TIMER :
                • opérateur du seuil le plus haut atteint
                • ou natif Blender si aucun seuil atteint
              si déjà fire via TIMER → no-op (guard anti-double)
    REPEAT  → avalé silencieusement
    ESC/RMB → annulation propre
    """
    bl_idname  = "holdkeys.proxy"
    bl_label   = "Hold Keys Proxy"
    bl_options = {'INTERNAL'}

    binding_index: IntProperty(default=-1)

    def invoke(self, context, event):
        prefs = _get_prefs(context)
        idx   = self.binding_index
        if idx < 0 or idx >= len(prefs.bindings):
            return {'PASS_THROUGH'}
        binding = prefs.bindings[idx]
        if not binding.enabled:
            return {'PASS_THROUGH'}

        self._binding_idx = idx
        self._press_time  = time.monotonic()
        self._fired       = False   # True dès qu'on a fire via TIMER
        # Seuils triés mis en cache une fois au PRESS — lus à chaque frame
        # par _hud_draw_callback. Sans ça l'attribut n'existe pas et le
        # callback GPU lève une AttributeError (silencieuse côté Blender) :
        # le cercle ne s'affiche jamais.
        self._sorted_thresholds = binding._sorted_thresholds()
        # Thème HUD : invalider le cache pour reprendre les couleurs à jour
        # à chaque nouveau hold (sinon couleurs figées au 1er appui).
        _invalidate_hud_theme_cache()
        # Position souris — coordonnées région relatives à la VIEW_3D
        # On stocke aussi les coordonnées fenêtre absolues comme fallback
        self._mouse_x = event.mouse_region_x
        self._mouse_y = event.mouse_region_y
        self._mouse_x_win = event.mouse_x
        self._mouse_y_win = event.mouse_y

        # Contexte exact au moment du PRESS — réutilisé pour l'appel natif
        self._ctx_window = context.window
        self._ctx_area   = context.area
        self._ctx_region = context.region
        self._window     = context.window

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.016, window=self._window)

        # ── HUD : enregistrement du callback de dessin ────────────────────
        # Chercher une zone VIEW_3D disponible même si le focus est ailleurs
        self._hud_handler = None
        hud_area = None
        if context.area and context.area.type == 'VIEW_3D':
            hud_area = context.area
        else:
            for win in context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type == 'VIEW_3D':
                        hud_area = area
                        break
                if hud_area:
                    break

        if hud_area:
            self._hud_handler = bpy.types.SpaceView3D.draw_handler_add(
                _hud_draw_callback, (self, context), 'WINDOW', 'POST_PIXEL'
            )

        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        prefs   = _get_prefs(context)
        binding = prefs.bindings[self._binding_idx]

        # ── Suivi souris pour l'arc HUD ────────────────────────────────────
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._mouse_x = event.mouse_region_x
            self._mouse_y = event.mouse_region_y
            self._mouse_x_win = event.mouse_x
            self._mouse_y_win = event.mouse_y
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── Timer : détecter si le dernier seuil est dépassé ─────────────
        if event.type == 'TIMER':
            if not self._fired:
                elapsed = time.monotonic() - self._press_time
                last_time, last_op = binding.last_threshold_op()
                if last_time is not None and elapsed >= last_time:
                    # Dernier seuil dépassé pendant le hold → fire immédiat
                    self._fired = True
                    self._cleanup(context)
                    _call_op(last_op,
                             ctx_window=self._ctx_window,
                             ctx_area=self._ctx_area,
                             ctx_region=self._ctx_region)
                    return {'FINISHED'}
                # Redraw piloté par le timer : nécessaire pour faire progresser
                # l'arc même si la souris ne bouge pas (sinon le cercle reste
                # figé entre deux MOUSEMOVE).
                if self._hud_handler is not None and self._ctx_area:
                    self._ctx_area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── Events de la touche ───────────────────────────────────────────
        if event.type == binding.key_type:
            if event.value in {'RELEASE', 'CLICK', 'DOUBLE_CLICK'}:
                elapsed = time.monotonic() - self._press_time
                self._cleanup(context)

                # Déjà fire via TIMER → ne pas re-déclencher
                if self._fired:
                    return {'FINISHED'}

                # Résoudre : seuil le plus haut atteint, ou tap court
                action = binding.resolve_action(elapsed)
                if action:
                    _call_op(action,
                             ctx_window=self._ctx_window,
                             ctx_area=self._ctx_area,
                             ctx_region=self._ctx_region)
                elif binding.native_operator:
                    _call_op(binding.native_operator,
                             ctx_window=self._ctx_window,
                             ctx_area=self._ctx_area,
                             ctx_region=self._ctx_region)
                else:
                    _call_native(
                        binding.key_type,
                        binding.use_shift, binding.use_ctrl,
                        binding.use_alt,   binding.use_oskey,
                        ctx_window=self._ctx_window,
                        ctx_area=self._ctx_area,
                        ctx_region=self._ctx_region,
                    )
                return {'FINISHED'}
            if event.value == 'REPEAT':
                # Maintenu mais pas encore au dernier seuil → avaler les repeats
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ── Annulation Esc/RMB ────────────────────────────────────────────
        if event.value == 'PRESS' and event.type in {'ESC', 'RIGHTMOUSE'}:
            self._cleanup(context)
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

    def _cleanup(self, context):
        if getattr(self, '_timer', None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        # ── Retirer le handler HUD ────────────────────────────────────────
        if getattr(self, '_hud_handler', None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._hud_handler, 'WINDOW')
            self._hud_handler = None
            if context.area:
                context.area.tag_redraw()
        self._window = None

    def cancel(self, context):
        self._cleanup(context)

# ─────────────────────────────────────────────────────────────────────────────
# Data : un binding
# ─────────────────────────────────────────────────────────────────────────────

KEY_TYPE_ITEMS = [
    # ── Lettres ──────────────────────────────────────────────────────────────
    ('A','A',''),('B','B',''),('C','C',''),('D','D',''),('E','E',''),
    ('F','F',''),('G','G',''),('H','H',''),('I','I',''),('J','J',''),
    ('K','K',''),('L','L',''),('M','M',''),('N','N',''),('O','O',''),
    ('P','P',''),('Q','Q',''),('R','R',''),('S','S',''),('T','T',''),
    ('U','U',''),('V','V',''),('W','W',''),('X','X',''),('Y','Y',''),
    ('Z','Z',''),
    # ── Chiffres rangée principale ───────────────────────────────────────────
    ('ZERO','0',''),('ONE','1',''),('TWO','2',''),('THREE','3',''),
    ('FOUR','4',''),('FIVE','5',''),('SIX','6',''),('SEVEN','7',''),
    ('EIGHT','8',''),('NINE','9',''),
    # ── Touches de fonction ──────────────────────────────────────────────────
    ('F1','F1',''),('F2','F2',''),('F3','F3',''),('F4','F4',''),
    ('F5','F5',''),('F6','F6',''),('F7','F7',''),('F8','F8',''),
    ('F9','F9',''),('F10','F10',''),('F11','F11',''),('F12','F12',''),
    ('F13','F13',''),('F14','F14',''),('F15','F15',''),('F16','F16',''),
    ('F17','F17',''),('F18','F18',''),('F19','F19',''),
    # ── Souris — boutons ─────────────────────────────────────────────────────
    ('LEFTMOUSE',  'LMB (Gauche)',  ''),
    ('RIGHTMOUSE', 'RMB (Droit)',   ''),
    ('MIDDLEMOUSE','MMB (Milieu)',  ''),
    ('BUTTON4MOUSE','Mouse 4',      ''),
    ('BUTTON5MOUSE','Mouse 5',      ''),
    ('BUTTON6MOUSE','Mouse 6',      ''),
    ('BUTTON7MOUSE','Mouse 7',      ''),
    # ── Souris — molette ─────────────────────────────────────────────────────
    ('WHEELUPMOUSE',  'Wheel ↑',''),
    ('WHEELDOWNMOUSE','Wheel ↓',''),
    ('WHEELINMOUSE',  'Wheel In', ''),
    ('WHEELOUTMOUSE', 'Wheel Out',''),
    # ── Touches de navigation ────────────────────────────────────────────────
    ('LEFT_ARROW', '← Gauche',  ''),
    ('RIGHT_ARROW','→ Droite',  ''),
    ('UP_ARROW',   '↑ Haut',    ''),
    ('DOWN_ARROW', '↓ Bas',     ''),
    ('HOME',      'Home',       ''),
    ('END',       'End',        ''),
    ('PAGE_UP',   'Page Up',    ''),
    ('PAGE_DOWN', 'Page Down',  ''),
    ('INSERT',    'Insert',     ''),
    # ── Touches spéciales ────────────────────────────────────────────────────
    ('SPACE',     'Space',      ''),
    ('TAB',       'Tab',        ''),
    ('RET',       'Enter',      ''),
    ('NUMPAD_ENTER','Num Enter',''),
    ('DEL',       'Delete',     ''),
    ('BACK_SPACE','Backspace',  ''),
    ('ESC',       'Escape',     ''),
    ('SEMI_COLON',';  (;)',     ''),
    ('PERIOD',    '.  (Point)', ''),
    ('COMMA',     ',  (Virgule)',''),
    ('QUOTE',     "'  (Apostrophe)",''),
    ('ACCENT_GRAVE','`  (Grave)',''),
    ('MINUS',     '-  (Tiret)', ''),
    ('PLUS',      '=  (Égal)',  ''),
    ('SLASH',     '/  (Slash)', ''),
    ('BACK_SLASH','\\  (Antislash)',''),
    ('LEFT_BRACKET', '[  ([)',  ''),
    ('RIGHT_BRACKET',']  (])',  ''),
    ('GRLESS',    '<  (<)',     ''),
    # ── Pavé numérique ───────────────────────────────────────────────────────
    ('NUMPAD_0','Num 0',''),('NUMPAD_1','Num 1',''),('NUMPAD_2','Num 2',''),
    ('NUMPAD_3','Num 3',''),('NUMPAD_4','Num 4',''),('NUMPAD_5','Num 5',''),
    ('NUMPAD_6','Num 6',''),('NUMPAD_7','Num 7',''),('NUMPAD_8','Num 8',''),
    ('NUMPAD_9','Num 9',''),
    ('NUMPAD_PERIOD', 'Num .',  ''),
    ('NUMPAD_PLUS',   'Num +',  ''),
    ('NUMPAD_MINUS',  'Num -',  ''),
    ('NUMPAD_ASTERIX','Num *',  ''),
    ('NUMPAD_SLASH',  'Num /',  ''),
]

def _rebuild_keymaps(self=None, context=None):
    _unregister_keymaps()
    _register_keymaps()


class HoldThreshold(bpy.types.PropertyGroup):
    """
    Un seuil de durée + opérateur associé.
    Plusieurs seuils par binding : tap < s1 < s2 < …
    Au RELEASE, l'action du seuil le plus haut atteint est déclenchée.
    """
    hold_time: FloatProperty(
        name="Seuil (s)",
        description="Durée minimale de pression pour déclencher cette action",
        default=0.3, min=0.05, max=5.0, step=1, precision=2,
        subtype='TIME_ABSOLUTE'
    )
    operator: StringProperty(
        name="Opérateur",
        description="Opérateur exécuté quand ce seuil est le plus haut atteint au RELEASE "
                    "(ex: mesh.dissolve_faces  ou  wm.call_menu;name=MY_MT_menu)",
        default=""
    )
    label: StringProperty(
        name="Label",
        description="Nom court affiché dans l'interface",
        default=""
    )


class HoldBinding(bpy.types.PropertyGroup):
    """
    Un binding = touche + modificateurs + liste de seuils (HoldThreshold).

    Logique au RELEASE :
      elapsed < seuil[0]  → native_operator (ou natif Blender auto-détecté)
      seuil[0] <= elapsed < seuil[1]  → seuil[0].operator
      seuil[1] <= elapsed  → seuil[1].operator
      … etc. (toujours le seuil le plus haut atteint)
    """
    enabled:   BoolProperty(name="Enabled", default=True,   update=_rebuild_keymaps)
    name:      StringProperty(name="Label", default="Nouveau binding")
    key_type:  EnumProperty(name="Key", items=KEY_TYPE_ITEMS, default='G',
                            update=_rebuild_keymaps)
    use_shift: BoolProperty(name="Shift", default=False, update=_rebuild_keymaps)
    use_ctrl:  BoolProperty(name="Ctrl",  default=False, update=_rebuild_keymaps)
    use_alt:   BoolProperty(name="Alt",   default=False, update=_rebuild_keymaps)
    use_oskey: BoolProperty(name="OSKey", default=False, update=_rebuild_keymaps)

    # Liste de seuils ordonnés (triés par hold_time au moment du RELEASE)
    thresholds:             CollectionProperty(type=HoldThreshold)
    active_threshold_index: IntProperty(default=0)

    # Tap court : vide = auto-détection natif Blender
    native_operator: StringProperty(
        name="Tap court",
        description="Opérateur sur tap court (sous le 1er seuil). "
                    "Vide = natif Blender auto-détecté "
                    "(ex: wm.call_menu;name=VIEW3D_MT_edit_mesh_delete)",
        default=""
    )

    # ── Champs legacy conservés pour migration (non affichés) ────────────
    hold_time: FloatProperty(name="[legacy]", default=0.4,
                             min=0.05, max=3.0, options={'HIDDEN'})
    hold_operator: StringProperty(name="[legacy]", default="",
                                  options={'HIDDEN'})

    def _sorted_thresholds(self):
        """Retourne les thresholds triés par hold_time croissant."""
        return sorted(self.thresholds, key=lambda t: t.hold_time)

    def resolve_action(self, elapsed: float):
        """
        Retourne l'opérateur à exécuter pour un elapsed donné.
        None → tap court (native_operator ou natif Blender).
        Retourne toujours l'opérateur du seuil le plus haut atteint
        (même si elapsed dépasse tous les seuils).
        """
        best_op = None
        for t in self._sorted_thresholds():
            if elapsed >= t.hold_time and t.operator:
                best_op = t.operator
        return best_op

    def last_threshold_op(self):
        """
        Retourne (hold_time, operator) du dernier seuil configuré,
        ou (None, None) si aucun seuil n'a d'opérateur.
        Utilisé par le TIMER pour déclencher l'action dès que le dernier
        seuil est dépassé sans attendre le RELEASE.
        """
        sorted_ts = [t for t in self._sorted_thresholds() if t.operator]
        if not sorted_ts:
            return None, None
        last = sorted_ts[-1]
        return last.hold_time, last.operator

    def has_any_operator(self):
        """True si au moins un seuil a un opérateur configuré."""
        return any(t.operator for t in self.thresholds)

# ─────────────────────────────────────────────────────────────────────────────
# Gestion des keymaps proxy
# ─────────────────────────────────────────────────────────────────────────────

_addon_keymaps: list[tuple] = []   # (km, kmi)

# Snapshot du dernier scan pour détecter les nouveaux keymaps
_last_scan_signature: str = ""

def _keymaps_signature() -> str:
    """
    Signature légère de l'état courant des keymaps (noms+nbre de kmi).
    Utilisée pour détecter si de nouveaux keymaps ont été ajoutés
    (ex: addon activé après Hold Keys).
    """
    wm = bpy.context.window_manager
    parts = []
    for kc in (wm.keyconfigs.addon, wm.keyconfigs.user,
               wm.keyconfigs.active, wm.keyconfigs.default):
        if kc is None:
            continue
        for km in kc.keymaps:
            parts.append(f"{km.name}:{len(km.keymap_items)}")
    return "|".join(parts)

def _register_keymaps():
    """
    Pour chaque binding actif :
    1. Scanne les keymaps natifs+addons qui utilisent cette touche
    2. Insère notre proxy head=True dans les mêmes keymaps
    3. Fallback sur 'Window' si aucun natif trouvé
    """
    global _last_scan_signature
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return

    prefs = _get_prefs()
    for idx, binding in enumerate(prefs.bindings):
        if not binding.enabled or not binding.has_any_operator():
            continue

        locations = _scan_keymaps(
            binding.key_type,
            binding.use_shift, binding.use_ctrl,
            binding.use_alt,   binding.use_oskey,
        )
        if not locations:
            locations = [("Window", 'EMPTY', 'WINDOW')]

        for km_name, space_type, region_type in locations:
            try:
                km = kc.keymaps.new(
                    name=km_name,
                    space_type=space_type,
                    region_type=region_type,
                )
            except Exception as e:
                print(f"[HoldKeys] keymap introuvable '{km_name}': {e}")
                continue

            kmi = km.keymap_items.new(
                idname = "holdkeys.proxy",
                type   = binding.key_type,
                value  = 'PRESS',
                shift  = binding.use_shift,
                ctrl   = binding.use_ctrl,
                alt    = binding.use_alt,
                oskey  = binding.use_oskey,
                head   = True,
            )
            kmi.properties.binding_index = idx
            _addon_keymaps.append((km, kmi))
            print(f"[HoldKeys] proxy [{binding.key_type}] "
                  f"dans '{km_name}' (binding #{idx})")

    _last_scan_signature = _keymaps_signature()

def _unregister_keymaps():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()

def _auto_rescan():
    """
    Timer périodique : re-scan si la signature des keymaps a changé
    (nouvel addon activé). S'arrête après MAX_RESCANS cycles.
    """
    global _rescan_count, _last_scan_signature

    # Arrêter si addon désactivé
    addon = bpy.context.preferences.addons.get(__package__)
    if addon is None or not addon.preferences.enabled:
        return None

    _rescan_count += 1
    if _rescan_count > _MAX_RESCANS:
        return None  # ne plus répéter

    sig = _keymaps_signature()
    if sig != _last_scan_signature:
        print(f"[HoldKeys] Changement keymaps détecté — re-scan #{_rescan_count}")
        _rebuild_keymaps()

    return _RESCAN_INTERVAL

_rescan_count   = 0
_MAX_RESCANS    = 20          # surveille pendant ~20×5s = ~100s après démarrage
_RESCAN_INTERVAL = 5.0        # toutes les 5 secondes

# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────

def _on_load_post(*_):
    """
    Après chargement d'une scène : les addons peuvent avoir changé.
    Re-scan complet + relance de la surveillance.
    """
    global _rescan_count
    _rescan_count = 0
    _rebuild_keymaps()
    # Relance le timer de surveillance si plus actif
    if not bpy.app.timers.is_registered(_auto_rescan):
        bpy.app.timers.register(_auto_rescan, first_interval=_RESCAN_INTERVAL)

# ─────────────────────────────────────────────────────────────────────────────
# Operators UI
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_OT_add_binding(Operator):
    bl_idname = "holdkeys.add_binding"
    bl_label  = "Ajouter"
    def execute(self, context):
        prefs = _get_prefs(context)
        b = prefs.bindings.add()
        b.name = "Nouveau binding"
        # Ajouter un seuil par défaut
        t = b.thresholds.add()
        t.hold_time = 0.4
        t.label     = "Hold"
        prefs.active_binding_index = len(prefs.bindings) - 1
        return {'FINISHED'}

class HOLDKEYS_OT_remove_binding(Operator):
    bl_idname = "holdkeys.remove_binding"
    bl_label  = "Supprimer"
    def execute(self, context):
        prefs = _get_prefs(context)
        idx = prefs.active_binding_index
        if 0 <= idx < len(prefs.bindings):
            prefs.bindings.remove(idx)
            prefs.active_binding_index = max(0, idx - 1)
            _rebuild_keymaps()
        return {'FINISHED'}

class HOLDKEYS_OT_duplicate_binding(Operator):
    bl_idname = "holdkeys.duplicate_binding"
    bl_label  = "Dupliquer"
    def execute(self, context):
        prefs = _get_prefs(context)
        idx = prefs.active_binding_index
        if 0 <= idx < len(prefs.bindings):
            s = prefs.bindings[idx]
            n = prefs.bindings.add()
            for attr in ('name', 'key_type', 'use_shift', 'use_ctrl', 'use_alt',
                         'use_oskey', 'native_operator'):
                setattr(n, attr, getattr(s, attr))
            n.name += " (copie)"
            # Dupliquer les seuils
            for t_src in s.thresholds:
                t_dst = n.thresholds.add()
                t_dst.hold_time = t_src.hold_time
                t_dst.operator  = t_src.operator
                t_dst.label     = t_src.label
            prefs.active_binding_index = len(prefs.bindings) - 1
            _rebuild_keymaps()
        return {'FINISHED'}

class HOLDKEYS_OT_add_threshold(Operator):
    """Ajoute un seuil au binding actif."""
    bl_idname = "holdkeys.add_threshold"
    bl_label  = "Ajouter un seuil"

    binding_index: IntProperty(default=-1)

    def execute(self, context):
        prefs = _get_prefs(context)
        idx   = self.binding_index
        if not (0 <= idx < len(prefs.bindings)):
            return {'CANCELLED'}
        b = prefs.bindings[idx]
        t = b.thresholds.add()
        # Seuil suivant = dernier + 0.2s, ou 0.3s si vide
        if len(b.thresholds) > 1:
            last_time = max(x.hold_time for x in b.thresholds
                            if x != t)
            t.hold_time = round(last_time + 0.2, 2)
        else:
            t.hold_time = 0.3
        t.label = f"Hold {len(b.thresholds)}"
        b.active_threshold_index = len(b.thresholds) - 1
        _rebuild_keymaps()
        return {'FINISHED'}

class HOLDKEYS_OT_remove_threshold(Operator):
    """Supprime le seuil actif du binding."""
    bl_idname = "holdkeys.remove_threshold"
    bl_label  = "Supprimer le seuil"

    binding_index: IntProperty(default=-1)

    def execute(self, context):
        prefs = _get_prefs(context)
        idx   = self.binding_index
        if not (0 <= idx < len(prefs.bindings)):
            return {'CANCELLED'}
        b   = prefs.bindings[idx]
        t_i = b.active_threshold_index
        if 0 <= t_i < len(b.thresholds):
            b.thresholds.remove(t_i)
            b.active_threshold_index = max(0, t_i - 1)
            _rebuild_keymaps()
        return {'FINISHED'}

class HOLDKEYS_OT_scan_keymaps(Operator):
    """Scanne les keymaps natifs et affiche où la touche est définie."""
    bl_idname = "holdkeys.scan_keymaps"
    bl_label  = "Scanner"
    def execute(self, context):
        prefs = _get_prefs(context)
        idx = prefs.active_binding_index
        if not (0 <= idx < len(prefs.bindings)):
            self.report({'WARNING'}, "Aucun binding sélectionné")
            return {'CANCELLED'}
        b = prefs.bindings[idx]
        locations = _scan_keymaps(b.key_type, b.use_shift, b.use_ctrl,
                                   b.use_alt, b.use_oskey)
        if locations:
            msg = " | ".join(f"{n} ({s}/{r})" for n, s, r in locations)
            self.report({'INFO'}, f"Trouvé dans : {msg}")
            print(f"[HoldKeys] Scan [{b.key_type}] → {msg}")
        else:
            self.report({'WARNING'}, "Aucun keymap natif trouvé")
        return {'FINISHED'}

class HOLDKEYS_OT_rebuild_keymaps(Operator):
    """Force un re-scan et recharge tous les proxies."""
    bl_idname = "holdkeys.rebuild_keymaps"
    bl_label  = "Recharger"
    def execute(self, context):
        global _rescan_count
        _rescan_count = 0
        _rebuild_keymaps()
        self.report({'INFO'}, f"{len(_addon_keymaps)} proxy(s) enregistré(s)")
        return {'FINISHED'}

# ─────────────────────────────────────────────────────────────────────────────
# Préférences
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_preferences(AddonPreferences):
    bl_idname = __package__

    enabled: BoolProperty(
        name="Hold Keys actif", default=True,
        update=lambda s, c: (_unregister_keymaps() if not s.enabled
                              else _rebuild_keymaps())
    )
    bindings: CollectionProperty(type=HoldBinding)
    active_binding_index: IntProperty(default=0)

    def draw(self, context):
        layout = self.layout

        # ── Header : toggle + statut + reload ────────────────────────────
        row = layout.row(align=True)
        row.prop(self, "enabled",
                 icon='HIDE_OFF' if self.enabled else 'HIDE_ON',
                 toggle=True)
        row.label(text=(f"● ACTIF — {len(_addon_keymaps)} proxy(s) "
                        f"| re-scan dans {max(0, _MAX_RESCANS - _rescan_count)}×")
                  if self.enabled else "○ INACTIF")
        row.operator("holdkeys.rebuild_keymaps", icon='FILE_REFRESH', text="")

        layout.separator()

        # ── Liste bindings ────────────────────────────────────────────────
        row = layout.row()
        col = row.column()
        col.template_list("HOLDKEYS_UL_bindings", "",
                          self, "bindings",
                          self, "active_binding_index", rows=4)
        ops = row.column(align=True)
        ops.operator("holdkeys.add_binding",       icon='ADD',       text="")
        ops.operator("holdkeys.remove_binding",    icon='REMOVE',    text="")
        ops.separator()
        ops.operator("holdkeys.duplicate_binding", icon='DUPLICATE', text="")

        # ── Éditeur ───────────────────────────────────────────────────────
        idx = self.active_binding_index
        if not (0 <= idx < len(self.bindings)):
            return
        b = self.bindings[idx]
        box = layout.box()
        box.use_property_split = True
        box.use_property_decorate = False

        box.prop(b, "name")
        box.prop(b, "enabled")
        box.separator()

        # ── Capture de touche ──────────────────────────────────────────────
        chord = (("Ctrl+"  if b.use_ctrl  else "") +
                 ("Shift+" if b.use_shift else "") +
                 ("Alt+"   if b.use_alt   else "") +
                 ("OS+"    if b.use_oskey else "") +
                 b.key_type)
        cap_row = box.row(align=True)
        cap_row.scale_y = 1.4
        cap = cap_row.operator("holdkeys.capture_key",
                               text=f"⌨  {chord}" if b.key_type != 'G' else "⌨  Cliquer pour capturer…",
                               icon='REC')
        cap.binding_index = idx
        # Bouton dédié TAB (Blender intercepte TAB avant le modal de capture)
        tab_btn = cap_row.operator("holdkeys.set_key", text="TAB", icon='EVENT_TAB')
        tab_btn.binding_index = idx
        tab_btn.key_value     = "TAB"
        box.separator()

        # ── Liste des seuils (multi-threshold) ────────────────────────────
        thresh_box = box.box()
        hdr = thresh_box.row(align=True)
        hdr.label(text="Seuils HOLD (au RELEASE, le plus haut atteint) :", icon='MODIFIER')
        add_t = hdr.operator("holdkeys.add_threshold", text="", icon='ADD')
        add_t.binding_index = idx

        wm = context.window_manager
        sq = getattr(wm, 'holdkeys_search_query', '')

        # Seuils triés par temps pour l'affichage
        sorted_ts = sorted(enumerate(b.thresholds), key=lambda x: x[1].hold_time)

        for t_i, t in sorted_ts:
            t_row = thresh_box.row(align=True)

            # Temps seuil
            t_row.prop(t, "hold_time", text="")

            # Opérateur assigné ou placeholder
            if t.operator:
                t_row.label(text=t.operator, icon='CHECKMARK')
                clr = t_row.operator("holdkeys.assign_op", text="", icon='X')
                clr.op_idname     = ""
                clr.binding_index = idx
                clr.target        = f"threshold_{t_i}"
            else:
                t_row.label(text="— opérateur non assigné —", icon='ERROR')

            # Bouton sélectionner (focus sur ce seuil pour la recherche)
            sel = t_row.operator("holdkeys.select_threshold", text="",
                                 icon='VIEWZOOM')
            sel.binding_index     = idx
            sel.threshold_index   = t_i

            # Bouton supprimer
            rm = t_row.operator("holdkeys.remove_threshold", text="", icon='REMOVE')
            rm.binding_index = idx

        if not b.thresholds:
            thresh_box.label(text="Aucun seuil — cliquer + pour en ajouter", icon='INFO')

        box.separator()

        # ── Tap court — affichage + effacement ────────────────────────────
        nat_box = box.box()
        nat_row = nat_box.row(align=True)
        nat_row.label(text="Tap COURT :", icon='FORWARD')
        if b.native_operator:
            nat_row.label(text=b.native_operator, icon='CHECKMARK')
            clr = nat_row.operator("holdkeys.assign_op", text="", icon='X')
            clr.op_idname = ""; clr.binding_index = idx; clr.target = "native"
        else:
            nat_row.label(text="Blender natif (automatique)", icon='INFO')

        box.separator()

        # ── Recherche opérateur — panneau unifié ──────────────────────────
        srch_box = box.box()
        srch_hdr = srch_box.row(align=True)
        srch_hdr.label(text="Recherche d'opérateur", icon='VIEWZOOM')
        srch_hdr.operator("holdkeys.rebuild_cache", text="", icon='FILE_REFRESH')

        # Filtres source + domaine sur une ligne
        filter_row = srch_box.row(align=True)
        filter_row.prop(wm, "holdkeys_source_filter", text="")
        filter_row.prop(wm, "holdkeys_domain_filter", text="")

        # Champ texte + clear
        sq_row = srch_box.row(align=True)
        sq_row.prop(wm, "holdkeys_search_pending", text="", icon='VIEWZOOM',
                    placeholder="Rechercher : join, extrude, dis…")
        if getattr(wm, 'holdkeys_search_pending', ''):
            sq_row.operator("holdkeys.clear_search", text="", icon='X')

        # Résultats via template_list scrollable
        sf = getattr(wm, 'holdkeys_source_filter', 'ALL')
        df = getattr(wm, 'holdkeys_domain_filter', 'ALL')
        results = _search_ops(sq, source_filter=sf, domain_filter=df, limit=200)
        _store_search_results(results, wm)

        count_row = srch_box.row()
        count_row.label(
            text=f"{len(results)} opérateur(s)"
                 + (" (limité à 200)" if len(results) == 200 else ""),
            icon='INFO',
        )

        srch_box.template_list(
            "HOLDKEYS_UL_OpResults", "",
            wm, "holdkeys_op_results",
            wm, "holdkeys_op_results_index",
            rows=8,
        )

        # Cible active (seuil ou tap court)
        active_t_i = b.active_threshold_index
        has_seuil  = 0 <= active_t_i < len(b.thresholds)

        # Ligne de sélection cible
        tgt_row = srch_box.row(align=True)
        tgt_row.label(text="Assigner à :", icon='FORWARD')
        if has_seuil:
            t_name = b.thresholds[active_t_i].label or f"Seuil {active_t_i + 1}"
            tgt_row.label(text=t_name, icon='MODIFIER')
        else:
            tgt_row.label(text="Tap court", icon='MOUSE_LMB')

        # Bouton Assigner
        res_idx = getattr(wm, 'holdkeys_op_results_index', 0)
        if results and 0 <= res_idx < len(results):
            sel_full_id, sel_label = results[res_idx][0], results[res_idx][1]
            assign_row = srch_box.row()
            assign_row.scale_y = 1.3
            tgt_str = f"threshold_{active_t_i}" if has_seuil else "native"
            op_btn               = assign_row.operator("holdkeys.assign_op",
                                                        text=f"→  {sel_label}",
                                                        icon='CHECKMARK')
            op_btn.op_idname     = sel_full_id
            op_btn.binding_index = idx
            op_btn.target        = tgt_str

        # ── Proxies actifs ─────────────────────────────────────────────────
        active_proxies = [(km, kmi) for km, kmi in _addon_keymaps
                          if getattr(kmi.properties, 'binding_index', -1) == idx]
        if active_proxies:
            sub = box.column(align=True)
            sub.label(text=f"Proxy dans {len(active_proxies)} keymap(s) :", icon='CHECKMARK')
            for km, kmi in active_proxies:
                sub.label(text=f"  → {km.name}", icon='DOT')
        elif b.enabled and b.has_any_operator():
            box.label(text="⚠ Aucun proxy — cliquer Recharger", icon='ERROR')

# ─────────────────────────────────────────────────────────────────────────────
# UIList
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_UL_bindings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="", emboss=False,
                 icon='CHECKBOX_HLT' if item.enabled else 'CHECKBOX_DEHLT')
        chord = (("Ctrl+"  if item.use_ctrl  else "") +
                 ("Shift+" if item.use_shift else "") +
                 ("Alt+"   if item.use_alt   else "") +
                 ("OS+"    if item.use_oskey else "") +
                 item.key_type)
        # Résumé seuils : "0.15s/0.40s" ou "?" si vide
        if item.thresholds:
            ts_sorted = sorted(item.thresholds, key=lambda t: t.hold_time)
            ts_str    = "/".join(f"{t.hold_time:.2f}s" for t in ts_sorted)
        else:
            ts_str = "?"
        row.label(text=f"{item.name}  [{chord}]  {ts_str}")

# ─────────────────────────────────────────────────────────────────────────────
# Stockage temporaire des résultats de recherche dans le WindowManager
# (alimente le template_list scrollable)
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_OpResultItem(bpy.types.PropertyGroup):
    """Un résultat de recherche sérialisé dans le WindowManager."""
    op_idname: StringProperty()
    op_label:  StringProperty()
    op_desc:   StringProperty()
    op_source: StringProperty()
    op_domain: StringProperty()
    is_addon:  BoolProperty()

_last_results_key: tuple = ()

def _store_search_results(results: list, wm=None):
    """
    Synchronise wm.holdkeys_op_results avec les résultats de _search_ops.
    Evite de reconstruire si la liste n'a pas changé (clé = tuple des idnames).
    """
    global _last_results_key
    if wm is None:
        wm = bpy.context.window_manager

    key = tuple(r[0] for r in results)
    if key == _last_results_key:
        return
    _last_results_key = key

    wm.holdkeys_op_results.clear()
    for full_id, label, desc, module, is_addon, source, domain in results:
        item           = wm.holdkeys_op_results.add()
        item.op_idname = full_id
        item.op_label  = label
        item.op_desc   = desc[:120]
        item.op_source = source
        item.op_domain = domain
        item.is_addon  = is_addon

    # Clamp l'index
    if getattr(wm, 'holdkeys_op_results_index', 0) >= len(results):
        wm.holdkeys_op_results_index = max(0, len(results) - 1)


# ─────────────────────────────────────────────────────────────────────────────
# UIList résultats de recherche
# ─────────────────────────────────────────────────────────────────────────────

class HOLDKEYS_UL_OpResults(bpy.types.UIList):
    """Liste scrollable des résultats de recherche d'opérateurs."""

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        row = layout.row(align=True)
        # Icône : SETTINGS pour les ops virtuels (idname contient ;),
        # SCRIPTPLUGINS pour les addons, BLENDER pour le natif standard.
        if ';' in item.op_idname:
            ico = 'SETTINGS'
        elif item.is_addon:
            ico = 'SCRIPTPLUGINS'
        else:
            ico = 'BLENDER'
        row.label(text=item.op_label, icon=ico)
        sub = row.row()
        sub.alignment = 'RIGHT'
        sub.scale_x   = 0.7
        sub.label(text=item.op_idname)

    def draw_filter(self, context, layout):
        pass   # filtres gérés au-dessus dans draw()

    def filter_items(self, context, data, propname):
        return [], []   # déjà filtré par _search_ops



class HOLDKEYS_OT_clear_search(Operator):
    bl_idname = "holdkeys.clear_search"
    bl_label  = "Effacer la recherche"
    def execute(self, context):
        context.window_manager.holdkeys_search_query   = ""
        context.window_manager.holdkeys_search_pending = ""
        return {'FINISHED'}

class HOLDKEYS_OT_select_threshold(Operator):
    """Sélectionne un seuil pour la recherche d'opérateur."""
    bl_idname = "holdkeys.select_threshold"
    bl_label  = "Sélectionner ce seuil"

    binding_index:   IntProperty(default=-1)
    threshold_index: IntProperty(default=0)

    def execute(self, context):
        prefs = _get_prefs(context)
        idx   = self.binding_index
        if 0 <= idx < len(prefs.bindings):
            prefs.bindings[idx].active_threshold_index = self.threshold_index
        return {'FINISHED'}

_classes = (
    HoldThreshold,
    HoldBinding,
    HOLDKEYS_OpResultItem,
    HOLDKEYS_preferences,
    HOLDKEYS_UL_bindings,
    HOLDKEYS_UL_OpResults,
    HOLDKEYS_OT_proxy,
    HOLDKEYS_OT_capture_key,
    HOLDKEYS_OT_set_key,
    HOLDKEYS_OT_assign_op,
    HOLDKEYS_OT_rebuild_cache,
    HOLDKEYS_OT_clear_search,
    HOLDKEYS_OT_select_threshold,
    HOLDKEYS_OT_add_binding,
    HOLDKEYS_OT_remove_binding,
    HOLDKEYS_OT_duplicate_binding,
    HOLDKEYS_OT_add_threshold,
    HOLDKEYS_OT_remove_threshold,
    HOLDKEYS_OT_scan_keymaps,
    HOLDKEYS_OT_rebuild_keymaps,
)

def _first_setup():
    """Timer t+0.5s : binding démo (2 seuils) + premier scan."""
    prefs = _get_prefs()
    if len(prefs.bindings) == 0:
        b = prefs.bindings.add()
        b.name            = "[Démo] X — Delete Menu / Dissolve"
        b.key_type        = 'X'
        b.native_operator = "wm.call_menu;name=VIEW3D_MT_edit_mesh_delete"
        b.enabled         = True
        # Seuil 1 : hold 0.15s → dissolve verts
        t1           = b.thresholds.add()
        t1.hold_time = 0.15
        t1.label     = "Hold court"
        t1.operator  = "mesh.dissolve_verts"
        # Seuil 2 : hold 0.40s → dissolve faces
        t2           = b.thresholds.add()
        t2.hold_time = 0.40
        t2.label     = "Hold long"
        t2.operator  = "mesh.dissolve_faces"
    _register_keymaps()
    return None

def _deferred_rescan():
    """Timer t+2s : second scan pour attraper les addons lents."""
    _rebuild_keymaps()
    return None

def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    # Handlers persistants
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)

    # wm properties pour la recherche
    bpy.types.WindowManager.holdkeys_search_pending = StringProperty(
        name="Recherche", default="", update=_on_search_update
    )
    bpy.types.WindowManager.holdkeys_search_query = StringProperty(
        name="Recherche active", default=""
    )
    bpy.types.WindowManager.holdkeys_source_filter = EnumProperty(
        name="Source",
        items=_source_filter_items,
        default=0,
    )
    bpy.types.WindowManager.holdkeys_domain_filter = EnumProperty(
        name="Domaine",
        items=_domain_filter_items,
        default=0,
    )
    bpy.types.WindowManager.holdkeys_op_results = CollectionProperty(
        type=HOLDKEYS_OpResultItem
    )
    bpy.types.WindowManager.holdkeys_op_results_index = IntProperty(
        name="Résultat sélectionné", default=0
    )

    # Scans différés : t+0.5s (premier), t+2s (addons lents)
    bpy.app.timers.register(_first_setup,      first_interval=0.5)
    bpy.app.timers.register(_deferred_rescan,  first_interval=2.0)

    # Surveillance continue : toutes les 5s pendant ~100s
    bpy.app.timers.register(_auto_rescan, first_interval=_RESCAN_INTERVAL)

def unregister():
    global _arc_batch_cache, _hud_shader
    # Timers
    for fn in (_auto_rescan, _first_setup, _deferred_rescan):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)

    # Handler
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)

    _unregister_keymaps()

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

    for prop in ('holdkeys_search_pending', 'holdkeys_search_query',
                 'holdkeys_source_filter', 'holdkeys_domain_filter',
                 'holdkeys_op_results', 'holdkeys_op_results_index'):
        if hasattr(bpy.types.WindowManager, prop):
            delattr(bpy.types.WindowManager, prop)

    _arc_batch_cache.clear()
    _hud_shader = None
    _invalidate_hud_theme_cache()
