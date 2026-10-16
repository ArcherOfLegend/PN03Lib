bl_info = {
    "name": "P.N.03 (GameCube) Importer",
    "author": "ArcherOfLegend",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "File > Import > P.N.03 Archive (.dat)",
    "description": "Imports P.N.03 BIN models, TPL textures and FCV animations (with the game's leg IK baked)",
    "category": "Import-Export",
}

import os, json, math
import bpy
from bpy.props import StringProperty, BoolProperty, FloatProperty, IntProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector, Quaternion

from . import pn03lib as L
import importlib
L = importlib.reload(L)

# game is Y-up; Blender is Z-up
AXIS = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))


def bname(bid):
    return "bone_%02d" % bid


def make_image(name, tex):
    w, h = tex['width'], tex['height']
    img = bpy.data.images.new(name, w, h, alpha=True)
    rgba = tex['rgba']
    px = [0.0] * (w * h * 4)
    # Blender rows go bottom-up
    for y in range(h):
        src = (h - 1 - y) * w * 4
        dst = y * w * 4
        for i in range(w * 4):
            px[dst + i] = rgba[src + i] / 255.0
    img.pixels.foreach_set(px)
    has_alpha = any(rgba[i] < 250 for i in range(3, len(rgba), 4))
    try:
        img.pack()
    except Exception:
        pass
    img["pn03_alpha"] = has_alpha
    return img


def merge_alpha(col, msk):
    w, h = col['width'], col['height']
    mw, mh = msk['width'], msk['height']
    out = bytearray(col['rgba'])
    mr = msk['rgba']
    for y in range(h):
        my = y * mh // h
        for x in range(w):
            mx = x * mw // w
            out[(y * w + x) * 4 + 3] = mr[(my * mw + mx) * 4]
    return dict(width=w, height=h, format=col['format'], rgba=out)


def make_material(name, img, mask=None, extra=None):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        return mat
    if 'Specular IOR Level' in bsdf.inputs:
        bsdf.inputs['Specular IOR Level'].default_value = 0.2
    elif 'Specular' in bsdf.inputs:
        bsdf.inputs['Specular'].default_value = 0.2
    bsdf.inputs['Roughness'].default_value = 0.6
    alpha_src = None
    if img is not None:
        tn = nt.nodes.new('ShaderNodeTexImage'); tn.image = img; tn.location = (-450, 250)
        nt.links.new(tn.outputs['Color'], bsdf.inputs['Base Color'])
        if img.get("pn03_alpha"):
            alpha_src = tn.outputs['Alpha']
    if mask is not None:
        mn = nt.nodes.new('ShaderNodeTexImage'); mn.image = mask; mn.location = (-450, -50)
        mn.label = "alpha mask"
        try:
            mn.image.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
        alpha_src = mn.outputs['Color']
    if extra is not None:
        en = nt.nodes.new('ShaderNodeTexImage'); en.image = extra; en.location = (-450, -350)
        en.label = "secondary map (unused)"
    if img is not None:
        nt.nodes.active = tn  # Solid-mode Texture colour shows the active image node
    if alpha_src is not None:
        nt.links.new(alpha_src, bsdf.inputs['Alpha'])
        if hasattr(mat, 'surface_render_method'):
            try: mat.surface_render_method = 'DITHERED'
            except Exception: pass
        if hasattr(mat, 'blend_method'):
            try: mat.blend_method = 'HASHED'
            except Exception: pass
    return mat


# ------------------------------------------------------------------ armature + mesh
def bone_tail_dir(skel, i):
    kids = [j for j, p in enumerate(skel.parent) if p == i]
    best, bl = None, 0.0
    for j in kids:
        o = Vector(skel.rest[j])
        if o.length > bl:
            best, bl = o, o.length
    if best is not None and bl > 1e-3:
        return best.normalized(), bl
    p = skel.parent[i]
    if p is not None:
        o = Vector(skel.rest[i])
        if o.length > 1e-3:
            return o.normalized(), max(o.length * 0.35, 1.0)
    return Vector((0, 1, 0)), 1.0


def build_armature(context, name, model, scale, collection):
    skel = L.Skeleton(model.bones)
    C = Matrix.Scale(scale, 4) @ AXIS
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    collection.objects.link(obj)
    context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = []
    for i, bid in enumerate(skel.ids):
        eb = arm.edit_bones.new(bname(bid))
        head = Vector(skel.rest_world[i])
        d, ln = bone_tail_dir(skel, i)
        ln = max(min(ln, 200.0), 8.0)
        eb.head = (C @ head.to_4d()).to_3d()
        eb.tail = (C @ (head + d * ln).to_4d()).to_3d()
        eb.roll = 0.0
        ebs.append(eb)
    for i, p in enumerate(skel.parent):
        if p is not None:
            ebs[i].parent = ebs[p]
            ebs[i].use_connect = False
    bpy.ops.object.mode_set(mode='OBJECT')
    obj["pn03_skeleton"] = json.dumps(model.bones)
    obj["pn03_scale"] = scale
    obj.show_in_front = False
    return obj, skel


def build_mesh(name, model, scale, materials_for_tex, collection, arm_obj):
    C = Matrix.Scale(scale, 4) @ AXIS
    verts = [(C @ Vector(p).to_4d()).to_3d() for p in model.positions]
    faces, loop_uv, loop_n, mat_idx = [], [], [], []
    seen = set()
    mats = []
    mat_slot = {}
    for g in model.groups:
        key = (g['kind'], tuple(g['tex']))
        if key not in mat_slot:
            mat_slot[key] = len(mats)
            mats.append(materials_for_tex(key))
        for tri in L.triangles(g['prims']):
            pi = tuple(v[0] for v in tri)
            if len(set(pi)) < 3:
                continue
            a, b, c = (Vector(model.positions[k]) for k in pi)
            fn = (b - a).cross(c - a)
            vn = Vector((0, 0, 0))
            for v in tri:
                if v[1] < len(model.normals):
                    vn += Vector(model.normals[v[1]])
            if fn.dot(vn) < 0:
                tri = (tri[0], tri[2], tri[1])
                pi = tuple(v[0] for v in tri)
            k = tuple(sorted(pi)) + (key,)
            if k in seen:
                continue
            seen.add(k)
            faces.append(pi)
            mat_idx.append(mat_slot[key])
            for v in tri:
                uv = model.uvs[v[3]] if v[3] < len(model.uvs) else (0, 0)
                loop_uv.append((uv[0], 1.0 - uv[1]))
                n = model.normals[v[1]] if v[1] < len(model.normals) else (0, 1, 0)
                loop_n.append((AXIS.to_3x3() @ Vector(n)).normalized())
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    for m in mats:
        me.materials.append(m)
    me.polygons.foreach_set('material_index', mat_idx)
    uvl = me.uv_layers.new(name='UVMap')
    flat = [c for uv in loop_uv for c in uv]
    uvl.data.foreach_set('uv', flat)
    me.polygons.foreach_set('use_smooth', [True] * len(faces))
    if hasattr(me, 'use_auto_smooth'):
        me.use_auto_smooth = True
    try:
        me.normals_split_custom_set(loop_n)
    except Exception:
        pass
    me.update()
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    # skin
    groups = {}
    for vi, wi in enumerate(model.pos_weight):
        if wi >= len(model.weights):
            continue
        for bid, w in model.weights[wi]:
            if w <= 0:
                continue
            nm = bname(bid)
            vg = groups.get(nm)
            if vg is None:
                vg = groups[nm] = obj.vertex_groups.new(name=nm)
            vg.add([vi], w, 'ADD')
    if arm_obj is not None:
        obj.parent = arm_obj
        mod = obj.modifiers.new('Armature', 'ARMATURE')
        mod.object = arm_obj
    return obj


# ------------------------------------------------------------------ animation
def _fcurve_container(action, arm_obj):
    if hasattr(action, 'slots') and hasattr(action, 'layers'):
        slot = action.slots.new(id_type='OBJECT', name=arm_obj.name)
        try:
            from bpy_extras.anim_utils import action_ensure_channelbag_for_slot
            cb = action_ensure_channelbag_for_slot(action, slot)
        except Exception:
            layer = action.layers.new("Layer") if not action.layers else action.layers[0]
            strip = layer.strips.new(type='KEYFRAME') if not layer.strips else layer.strips[0]
            cb = strip.channelbag(slot, ensure=True)
        groups = {}
        def new(path, index, group):
            fc = cb.fcurves.new(path, index=index)
            g = groups.get(group)
            if g is None:
                g = groups[group] = cb.groups.new(group)
            fc.group = g
            return fc
        return new, slot
    def new(path, index, group):
        return action.fcurves.new(path, index=index, action_group=group)
    return new, None


def _pose_basis_stream(arm_obj, skel, scale):
    C = Matrix.Scale(scale, 4) @ AXIS
    Ci = C.inverted()
    bones = [arm_obj.data.bones[bname(b)] for b in skel.ids]
    restR = [b.matrix_local.to_3x3().to_4x4() for b in bones]
    rest_rel_inv = []
    for i, b in enumerate(bones):
        p = skel.parent[i]
        rr = (bones[p].matrix_local.inverted() @ b.matrix_local) if p is not None else b.matrix_local.copy()
        rest_rel_inv.append(rr.inverted())
    def bases(W, which):
        M = {}
        def getM(i):
            if i not in M:
                w = W[i]
                gm = Matrix(((w[0][0], w[0][1], w[0][2], w[0][3]),
                             (w[1][0], w[1][1], w[1][2], w[1][3]),
                             (w[2][0], w[2][1], w[2][2], w[2][3]),
                             (0, 0, 0, 1)))
                M[i] = C @ gm @ Ci @ restR[i]
            return M[i]
        out = {}
        for i in which:
            p = skel.parent[i]
            out[i] = (rest_rel_inv[i] @ (getM(p).inverted() @ getM(i) if p is not None else getM(i))).decompose()
        return out
    return bases


def bake_segments(arm_obj, skel, segments, name, scale, fps_scale, root_motion):
    animated = set()
    for anim, _, _ in segments:
        animated |= L.animated_indices(skel, anim)
    animated = sorted(animated)
    if not animated:
        return None
    bases = _pose_basis_stream(arm_obj, skel, scale)
    data = {i: ([], [], [], []) for i in animated}   # times, loc, quat, scale
    prevq = {}
    end = 0
    for anim, start, nf in segments:
        nf = nf if nf is not None else max(anim['frames'], 1) + 1
        mine = sorted(L.animated_indices(skel, anim))
        for f in range(nf):
            W = L.evaluate(skel, anim, float(f), root_motion=root_motion)
            for i, (loc, q, sc) in bases(W, mine).items():
                pq = prevq.get(i)
                if pq is not None and pq.dot(q) < 0:
                    q.negate()
                prevq[i] = q
                d = data[i]
                d[0].append((start + f) * fps_scale); d[1].append(loc); d[2].append(q); d[3].append(sc)
        end = max(end, start + nf - 1)
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    new_fc, slot = _fcurve_container(act, arm_obj)
    pbs = arm_obj.pose.bones
    for i in animated:
        bn = bname(skel.ids[i])
        pbs[bn].rotation_mode = 'QUATERNION'
        ts, locs, quats, scls = data[i]
        if not ts:
            continue
        chans = [('location', 3, locs), ('rotation_quaternion', 4, quats)]
        if any(abs(sv[k] - 1.0) > 1e-4 for sv in scls for k in range(3)):
            chans.append(('scale', 3, scls))
        for prop, nc, vals in chans:
            path = 'pose.bones["%s"].%s' % (bn, prop)
            for k in range(nc):
                fc = new_fc(path, k, bn)
                fc.keyframe_points.add(len(ts))
                co = []
                for t, v in zip(ts, vals):
                    co += [t, v[k]]
                fc.keyframe_points.foreach_set('co', co)
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'
                fc.update()
    act["pn03_frames"] = end
    try:
        act.use_frame_range = True
        act.frame_start = 0
        act.frame_end = end * fps_scale
    except Exception:
        pass
    return act, slot


def bake_action(arm_obj, skel, anim, name, scale, fps_scale, root_motion):
    return bake_segments(arm_obj, skel, [(anim, 0, None)], name, scale, fps_scale, root_motion)


def assign_action(arm_obj, act, slot):
    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()
    arm_obj.animation_data.action = act
    if slot is not None and hasattr(arm_obj.animation_data, 'action_slot'):
        try:
            arm_obj.animation_data.action_slot = slot
        except Exception:
            pass


# ------------------------------------------------------------------ motion names (from main.dol call sites)
_NAMES = None
def motion_name(base, entry):
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = json.load(open(os.path.join(os.path.dirname(__file__), "motion_names.json")))
        except Exception:
            _NAMES = {}
    return _NAMES.get(base, {}).get(str(entry))


# ------------------------------------------------------------------ model loading (shared by both importers)
def _same_skeleton(a, b, tol=1.0):
    return len(a) == len(b) and all(x[0] == y[0] and x[1] == y[1] and
                                    max(abs(p - q) for p, q in zip(x[2], y[2])) <= tol for x, y in zip(a, b))


def load_models(context, ents, name_base, scale, coll, report, host=None, tpl_override=None, share_rig=False):
    models = []
    for i, (t, b) in enumerate(ents):
        if t == 'BIN' and len(b) > 0x40:
            j = i + 1
            while j < len(ents) and ents[j][0] == 'BIN':   # a run of BINs can share the TPL after it
                j += 1
            tpl = ents[j][1] if j < len(ents) and ents[j][0] == 'TPL' else None
            models.append([i, L.read_bin(b), tpl])
    for k, data in (tpl_override or {}).items():
        if k < len(models):
            models[k][2] = data
    rigs = []
    if host is not None:
        rigs.append(dict(obj=host, skel=L.Skeleton([tuple(x[:2]) + (tuple(x[2]),) for x in json.loads(host["pn03_skeleton"])]),
                         bones=None, meshes=[], host=True))
    for (i, m, tpl) in models:
        name = "%s_%d" % (name_base, i)
        imgs, raw = [], []
        if tpl:
            try:
                raw = L.read_tpl(tpl)
                for k, tx in enumerate(raw):
                    imgs.append(make_image("%s_tex%d" % (name, k), tx))
            except Exception as e:
                report({'WARNING'}, "TPL %d: %s" % (i, e))
        cache = {}
        def mat_for(key, imgs=imgs, raw=raw, name=name, cache=cache):
            if key not in cache:
                kind, tex = key
                get = lambda k: imgs[k] if k < len(imgs) else None
                base = get(tex[0])
                if kind == 5 and tex[0] < len(raw) and tex[2] < len(raw):
                    ck = ("alpha", tex[0], tex[2])
                    if ck not in cache:
                        cache[ck] = make_image("%s_tex%d_alpha%d" % (name, tex[0], tex[2]),
                                               merge_alpha(raw[tex[0]], raw[tex[2]]))
                    base = cache[ck]
                extra = get(tex[1]) if kind in (1, 3, 5) else None
                tag = "_".join("%d" % t for t in tex if t != 0xff) or "none"
                cache[key] = make_material("%s_k%d_t%s" % (name, kind, tag), base, None, extra)
            return cache[key]
        ids = {b[0] for b in m.bones}
        rig = None
        if host is not None and all(bname(b) in host.data.bones for b in ids):
            rig = rigs[0]
        elif m.bones and share_rig:
            rig = next((r for r in rigs if r.get('bones') and _same_skeleton(r['bones'], m.bones)), None)
        if rig is None and m.bones:
            arm_obj, skel = build_armature(context, name + "_rig", m, scale, coll)
            rig = dict(obj=arm_obj, skel=skel, bones=m.bones, meshes=[], host=False)
            rigs.append(rig)
        twin = None
        if rig is not None and share_rig:
            twin = next((mo for mo, mm in rig['meshes'] if len(mm.positions) == len(m.positions)
                         and mm.pos_weight == m.pos_weight), None)
        if twin is not None:
            C = Matrix.Scale(scale, 4) @ AXIS
            if twin.data.shape_keys is None:
                twin.shape_key_add(name="Basis", from_mix=False)
            sk = twin.shape_key_add(name="shape_%d" % i, from_mix=False)
            co = []
            for p in m.positions:
                v = (C @ Vector(p).to_4d()).to_3d(); co += [v.x, v.y, v.z]
            sk.data.foreach_set('co', co)
            continue
        mo = build_mesh(name, m, scale, mat_for, coll, rig['obj'] if rig else None)
        if rig is not None:
            alt = share_rig and any(len(mm.positions) == len(m.positions) for _, mm in rig['meshes'])
            if alt:
                # second face of a cutscene character: the facial-shape stream blends toward it in-game
                mo.name = name + "_alt_face"
                mo.hide_viewport = True; mo.hide_render = True
                mo["pn03_alt"] = True
            rig['meshes'].append((mo, m))
    return rigs


def _set_fps(context, fps, on):
    if on:
        context.scene.render.fps = fps
        context.scene.render.fps_base = 1.0
    return context.scene.render.fps / context.scene.render.fps_base / float(fps)


# ------------------------------------------------------------------ archive operator
class IMPORT_OT_pn03(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.pn03_dat"
    bl_label = "Import P.N.03 Archive"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".dat"
    filter_glob: StringProperty(default="*.dat;*.DAT", options={'HIDDEN'})

    scale: FloatProperty(name="Scale", default=0.001, min=1e-6, max=100.0,
                         description="Game units are millimetres; 0.001 gives metres")
    import_models: BoolProperty(name="Models", default=True)
    import_anims: BoolProperty(name="Animations", default=True)
    root_motion: BoolProperty(name="Root Motion", default=True,
                              description="Applies Root Bone movement")
    fps: IntProperty(name="Game FPS", default=30, min=1, max=240,
                     description="Rate the game's motion frames play at")
    set_scene_fps: BoolProperty(name="Set Scene FPS", default=True,
                                description="Set the scene frame rate to Game FPS so keys land on whole frames")
    suit: IntProperty(name="Suit", default=-1, min=-1, max=99,
                      description="pl00 only: load suit textures pl00N.tpl / pl000N.tpl from the same folder (-1 = built-in)")
    attach: BoolProperty(name="Attach to Active Armature", default=True,
                         description="If an imported P.N.03 armature is active and has the bones this file uses "
                                     "(e.g. weapons wepXX.dat on Vanessa), skin meshes and put animations on it")
    max_anims: IntProperty(name="Max Animations", default=0, min=0, description="0 = all")

    def execute(self, context):
        try:
            return self._run(context)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.report({'ERROR'}, "P.N.03 import failed: %s" % e)
            return {'CANCELLED'}

    def _run(self, context):
        path = self.filepath
        base = os.path.splitext(os.path.basename(path))[0]
        ents = L.read_dat(open(path, 'rb').read())
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        host = None
        ao = context.view_layer.objects.active
        if self.attach and ao is not None and ao.type == 'ARMATURE' and "pn03_skeleton" in ao:
            host = ao
        coll = bpy.data.collections.new(base)
        context.scene.collection.children.link(coll)
        over = {}
        if self.suit >= 0:
            folder = os.path.dirname(path)
            for k, fn in enumerate(["%s%x.tpl" % (base, self.suit), "%s0%x.tpl" % (base, self.suit)]):
                fp = os.path.join(folder, fn)
                if os.path.exists(fp):
                    over[k] = open(fp, 'rb').read()
        rigs = []
        if self.import_models:
            rigs = load_models(context, ents, base, self.scale, coll, self.report, host=host,
                               tpl_override=over, share_rig=base.lower().startswith('ev'))
        elif host is not None:
            rigs = load_models(context, [], base, self.scale, coll, self.report, host=host)
        n_mesh = sum(len(r['meshes']) for r in rigs)
        n_act = 0
        if self.import_anims and rigs:
            fps_scale = _set_fps(context, self.fps, self.set_scene_fps)
            first = {}
            fcvs = [(i, b) for i, (t, b) in enumerate(ents) if t == 'FCV']
            if self.max_anims:
                fcvs = fcvs[:self.max_anims]
            for i, b in fcvs:
                anim = L.read_fcv(b)
                if not anim:
                    continue
                need = L.anim_bone_ids(anim)
                if not need:
                    continue
                cands = sorted([(len(r['skel'].ids), k) for k, r in enumerate(rigs) if need <= set(r['skel'].ids)])
                if not cands:
                    continue
                r = rigs[cands[0][1]]
                nm = motion_name(base, i)
                aname = "%s_m%03d" % (base, i) + ("_" + nm if nm else "")
                res = bake_action(r['obj'], r['skel'], anim, aname, self.scale, fps_scale, self.root_motion)
                if res is None:
                    continue
                n_act += 1
                first.setdefault(r['obj'].name, res)
            for r in rigs:
                if r['obj'].name in first:
                    assign_action(r['obj'], *first[r['obj'].name])
            if first:
                a0 = next(iter(first.values()))[0]
                context.scene.frame_start = 0
                context.scene.frame_end = int(round(a0.get("pn03_frames", 60) * fps_scale))
        self.report({'INFO'}, "P.N.03: %d meshes, %d actions" % (n_mesh, n_act))
        return {'FINISHED'}

    def draw(self, context):
        lay = self.layout
        lay.prop(self, "scale")
        lay.prop(self, "import_models")
        lay.prop(self, "suit")
        lay.prop(self, "import_anims")
        col = lay.column()
        col.enabled = self.import_anims
        col.prop(self, "fps")
        col.prop(self, "set_scene_fps")
        col.prop(self, "root_motion")
        col.prop(self, "max_anims")
        lay.prop(self, "attach")


# ------------------------------------------------------------------ event (cutscene) operator
def _key_hidden(obj, frame, hidden):
    obj.hide_viewport = hidden; obj.hide_render = hidden
    obj.keyframe_insert('hide_viewport', frame=frame)
    obj.keyframe_insert('hide_render', frame=frame)
    if obj.animation_data and obj.animation_data.action:
        try:
            for fc in L_fcurves(obj.animation_data.action):
                for kp in fc.keyframe_points:
                    kp.interpolation = 'CONSTANT'
        except Exception:
            pass


def L_fcurves(action):
    if hasattr(action, 'fcurves') and not hasattr(action, 'layers'):
        return list(action.fcurves)
    out = []
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for cb in strip.channelbags:
                    out += list(cb.fcurves)
    except Exception:
        out = list(getattr(action, 'fcurves', []))
    return out


def build_event_camera(context, name, cam_tracks, scale, fps_scale, coll):
    C = Matrix.Scale(scale, 4) @ AXIS
    cd = bpy.data.cameras.new(name)
    cd.sensor_fit = 'VERTICAL'
    cd.clip_start = 0.05; cd.clip_end = 2000.0
    co = bpy.data.objects.new(name, cd)
    coll.objects.link(co)
    co.rotation_mode = 'QUATERNION'
    prevq = None
    for start, recs in cam_tracks:
        for f, (eye, tgt, roll, fov) in enumerate(recs):
            e = (C @ Vector(eye).to_4d()).to_3d(); t = (C @ Vector(tgt).to_4d()).to_3d()
            d = t - e
            if d.length < 1e-6:
                continue
            q = d.to_track_quat('-Z', 'Y') @ Quaternion((0, 0, 1), math.radians(roll))
            if prevq is not None and prevq.dot(q) < 0:
                q.negate()
            prevq = q
            fr = (start + f) * fps_scale
            co.location = e; co.rotation_quaternion = q
            cd.angle_y = math.radians(max(fov, 1.0))
            co.keyframe_insert('location', frame=fr)
            co.keyframe_insert('rotation_quaternion', frame=fr)
            cd.keyframe_insert('lens', frame=fr)
    for idb in (co, cd):
        if idb.animation_data and idb.animation_data.action:
            for fc in L_fcurves(idb.animation_data.action):
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'
    return co


class IMPORT_OT_pn03_event(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.pn03_event"
    bl_label = "Import P.N.03 Cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".lze"
    filter_glob: StringProperty(default="*.lze;*.LZE", options={'HIDDEN'})
    event: IntProperty(name="Event", default=0, min=0,
                       description="Which EVT block in the room (0 = first). The import report lists how many there are")
    em_folder: StringProperty(name="Character Folder", default="", subtype='DIR_PATH',
                              description="Folder with ev00.dat, em11.dat... (blank = ../em next to the room's stage folder)")
    scale: FloatProperty(name="Scale", default=0.001, min=1e-6, max=100.0)
    fps: IntProperty(name="Game FPS", default=30, min=1, max=240)
    set_scene_fps: BoolProperty(name="Set Scene FPS", default=True)
    camera: BoolProperty(name="Camera", default=True)
    suit: IntProperty(name="Suit", default=-1, min=-1, max=10,
                      description="ev00/ev01 only: cutscene suit textures ev00_0N01.tpl / ev00_0N03.tpl (-1 = built-in)")

    def execute(self, context):
        try:
            return self._run(context)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.report({'ERROR'}, "P.N.03 cutscene import failed: %s" % e)
            return {'CANCELLED'}

    def _run(self, context):
        path = self.filepath
        room = os.path.splitext(os.path.basename(path))[0]
        rd = L.read_dat(L.lzs_decode(open(path, 'rb').read()))
        evts = [b for t, b in rd if t == 'EVT']
        if not evts:
            self.report({'ERROR'}, "%s has no cutscene (EVT) data" % room)
            return {'CANCELLED'}
        if self.event >= len(evts):
            self.report({'ERROR'}, "%s has %d events (0..%d)" % (room, len(evts), len(evts) - 1))
            return {'CANCELLED'}
        ev = L.read_evt(evts[self.event])
        folder = bpy.path.abspath(self.em_folder) if self.em_folder else \
            os.path.join(os.path.dirname(os.path.dirname(path)), "em")
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        fps_scale = _set_fps(context, self.fps, self.set_scene_fps)
        top = bpy.data.collections.new("%s_event%d" % (room, self.event))
        context.scene.collection.children.link(top)
        # cut timeline
        starts, t = [], 0
        cuts = []
        for frames, off in ev['cuts']:
            starts.append(t)
            cuts.append(L.read_cut(evts[self.event], off))
            t += frames
        total = t
        n_models = 0
        for mname in ev['models']:
            if mname == 'null':
                continue
            arc = os.path.join(folder, mname[:4] + ".dat")
            arc_ents = None
            if os.path.exists(arc):
                arc_ents = L.read_dat(open(arc, 'rb').read())
            elif len(mname) >= 4 and mname[:3] == room[1:4] and mname[3].isdigit():
                # room-local prop: <room minus 'r'><n> = the n-th BIN in the room archive (ra11 -> a111, a112...)
                bins = [k for k, (t, _) in enumerate(rd) if t == 'BIN']
                n = int(mname[3])
                if 1 <= n <= len(bins):
                    k = bins[n - 1]
                    j = k + 1
                    while j < len(rd) and rd[j][0] == 'BIN':
                        j += 1
                    arc_ents = [rd[k]] + ([rd[j]] if j < len(rd) and rd[j][0] == 'TPL' else [])
            if arc_ents is None:
                self.report({'WARNING'}, "%s: no %s and no matching room prop" % (mname, arc))
                continue
            coll = bpy.data.collections.new(mname)
            top.children.link(coll)
            over = {}
            if self.suit >= 0 and mname[:4] in ('ev00', 'ev01'):
                for k, fn in ((1, "ev00_%02x01.tpl" % self.suit), (3, "ev00_%02x03.tpl" % self.suit)):
                    fp = os.path.join(folder, fn)
                    if os.path.exists(fp):
                        over[k] = open(fp, 'rb').read()
            rigs = load_models(context, arc_ents, mname, self.scale, coll, self.report,
                               tpl_override=over, share_rig=True)
            if not rigs:
                continue
            segs = []
            for c, (cut, st) in enumerate(zip(cuts, starts)):
                for nm, _, anim in cut['motions']:
                    if nm == mname and anim:
                        segs.append((c, anim, st, ev['cuts'][c][0]))
            for r in rigs:
                need_ok = [s for s in segs if L.anim_bone_ids(s[1]) <= set(r['skel'].ids)]
                if need_ok:
                    res = bake_segments(r['obj'], r['skel'], [(a, st, n) for _, a, st, n in need_ok],
                                        "%s_ev%d_%s" % (room, self.event, mname), self.scale, fps_scale, True)
                    if res:
                        assign_action(r['obj'], *res)
                shown = {c for c, _, _, _ in segs}
                for mo, _ in r['meshes']:
                    if mo.get("pn03_alt"):
                        continue
                    for c, st in enumerate(starts):
                        _key_hidden(mo, st * fps_scale, c not in shown)
                    _key_hidden(mo, total * fps_scale, True)
            n_models += 1
        if self.camera:
            build_event_camera(context, "%s_ev%d_camera" % (room, self.event),
                               [(st, cut['camera']) for cut, st in zip(cuts, starts)], self.scale, fps_scale, top)
            cam = bpy.data.objects.get("%s_ev%d_camera" % (room, self.event))
            if cam is not None:
                context.scene.camera = cam
        context.scene.frame_start = 0
        context.scene.frame_end = int(total * fps_scale)
        self.report({'INFO'}, "P.N.03 cutscene %s event %d/%d: %d characters, %d cuts, %d frames" %
                    (room, self.event, len(evts) - 1, n_models, len(cuts), total))
        return {'FINISHED'}


def menu_func(self, context):
    self.layout.operator(IMPORT_OT_pn03.bl_idname, text="P.N.03 Archive (.dat)")
    self.layout.operator(IMPORT_OT_pn03_event.bl_idname, text="P.N.03 Cutscene (.lze)")


def register():
    bpy.utils.register_class(IMPORT_OT_pn03)
    bpy.utils.register_class(IMPORT_OT_pn03_event)
    bpy.types.TOPBAR_MT_file_import.append(menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    bpy.utils.unregister_class(IMPORT_OT_pn03_event)
    bpy.utils.unregister_class(IMPORT_OT_pn03)
