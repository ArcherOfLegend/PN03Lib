import struct, math

# ---------------------------------------------------------------- archive
def read_dat(data):
    n = struct.unpack_from('>I', data, 0)[0]
    offs = struct.unpack_from('>%dI' % n, data, 4)
    tp = 4 + 4 * n
    types = [data[tp + 4 * i: tp + 4 * i + 4].rstrip(b'\0').decode('latin1') for i in range(n)]
    ends = list(offs[1:]) + [len(data)]
    return [(types[i], data[offs[i]:ends[i]]) for i in range(n)]

# ---------------------------------------------------------------- TPL
def _rgb565(c):
    r = (c >> 11) & 31; g = (c >> 5) & 63; b = c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))

def _rgb5a3(c):
    if c & 0x8000:
        r = (c >> 10) & 31; g = (c >> 5) & 31; b = c & 31
        return ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 255)
    a = (c >> 12) & 7; r = (c >> 8) & 15; g = (c >> 4) & 15; b = c & 15
    return (r * 17, g * 17, b * 17, (a << 5) | (a << 2) | (a >> 1))

_BLOCK = {0: (8, 8, 4), 1: (8, 4, 8), 2: (8, 4, 8), 3: (4, 4, 16), 4: (4, 4, 16), 5: (4, 4, 16),
          6: (4, 4, 32), 8: (8, 8, 4), 9: (8, 4, 8), 10: (4, 4, 16), 14: (8, 8, 4)}

def decode_texture(buf, off, w, h, fmt, pal=None):
    out = bytearray(w * h * 4)
    bw, bh, bpp = _BLOCK[fmt]
    p = off
    def put(x, y, rgba):
        if x < w and y < h:
            i = (y * w + x) * 4
            out[i:i + 4] = bytes(rgba)
    if fmt == 14:  # CMPR: 8x8 tiles of four 4x4 DXT1 blocks
        for ty in range(0, h, 8):
            for tx in range(0, w, 8):
                for sb in range(4):
                    c0, c1 = struct.unpack_from('>HH', buf, p)
                    bits = struct.unpack_from('>I', buf, p + 4)[0]
                    p += 8
                    a = _rgb565(c0) + (255,); b = _rgb565(c1) + (255,)
                    if c0 > c1:
                        c2 = tuple((2 * a[k] + b[k]) // 3 for k in range(3)) + (255,)
                        c3 = tuple((a[k] + 2 * b[k]) // 3 for k in range(3)) + (255,)
                    else:
                        c2 = tuple((a[k] + b[k]) // 2 for k in range(3)) + (255,)
                        c3 = (0, 0, 0, 0)
                    pal4 = (a, b, c2, c3)
                    ox = tx + (sb & 1) * 4; oy = ty + (sb >> 1) * 4
                    for py in range(4):
                        for px in range(4):
                            idx = (bits >> (30 - 2 * (py * 4 + px))) & 3
                            put(ox + px, oy + py, pal4[idx])
        return out
    if fmt == 6:  # RGBA8: AR plane then GB plane per 4x4 tile
        for ty in range(0, h, 4):
            for tx in range(0, w, 4):
                for i in range(16):
                    a_, r_ = buf[p + 2 * i], buf[p + 2 * i + 1]
                    g_, b_ = buf[p + 32 + 2 * i], buf[p + 33 + 2 * i]
                    put(tx + i % 4, ty + i // 4, (r_, g_, b_, a_))
                p += 64
        return out
    for ty in range(0, h, bh):
        for tx in range(0, w, bw):
            for py in range(bh):
                for px in range(bw):
                    x, y = tx + px, ty + py
                    if fmt == 0:  # I4
                        v = buf[p >> 1]; v = (v >> 4) if not (p & 1) else (v & 15); v *= 17
                        put(x, y, (v, v, v, 255)); p += 1
                    elif fmt == 1:  # I8
                        v = buf[p]; put(x, y, (v, v, v, 255)); p += 1
                    elif fmt == 2:  # IA4
                        v = buf[p]; i_ = (v & 15) * 17; a_ = (v >> 4) * 17
                        put(x, y, (i_, i_, i_, a_)); p += 1
                    elif fmt == 3:  # IA8
                        a_, i_ = buf[p], buf[p + 1]; put(x, y, (i_, i_, i_, a_)); p += 2
                    elif fmt == 4:
                        put(x, y, _rgb565(struct.unpack_from('>H', buf, p)[0]) + (255,)); p += 2
                    elif fmt == 5:
                        put(x, y, _rgb5a3(struct.unpack_from('>H', buf, p)[0])); p += 2
                    elif fmt == 8:
                        v = buf[p >> 1]; v = (v >> 4) if not (p & 1) else (v & 15)
                        put(x, y, pal[v] if pal else (v * 17,) * 3 + (255,)); p += 1
                    elif fmt == 9:
                        v = buf[p]; put(x, y, pal[v] if pal else (v, v, v, 255)); p += 1
                    elif fmt == 10:
                        v = struct.unpack_from('>H', buf, p)[0] & 0x3fff
                        put(x, y, pal[v] if pal else (0, 0, 0, 255)); p += 2
            if fmt in (0, 8):
                pass
    # I4 / CI4 advanced p in nibbles
    return out

def read_tpl(data):
    magic, n, toff = struct.unpack_from('>3I', data, 0)
    if magic != 0x0020AF30:
        raise ValueError('not a TPL')
    res = []
    for i in range(n):
        io, po = struct.unpack_from('>2I', data, toff + 8 * i)
        h, w, fmt, doff = struct.unpack_from('>HHII', data, io)
        pal = None
        if po:
            pn, _, pfmt, pdo = struct.unpack_from('>HBxII', data, po)
            cols = [struct.unpack_from('>H', data, pdo + 2 * k)[0] for k in range(pn)]
            if pfmt == 0:
                pal = [((c & 255),) * 3 + (c >> 8,) for c in cols]
            elif pfmt == 1:
                pal = [_rgb565(c) + (255,) for c in cols]
            else:
                pal = [_rgb5a3(c) for c in cols]
        if fmt in (0, 8):
            # nibble formats: walk with nibble pointer
            rgba = _decode_nibble(data, doff, w, h, fmt, pal)
        else:
            rgba = decode_texture(data, doff, w, h, fmt, pal)
        res.append(dict(width=w, height=h, format=fmt, rgba=rgba))
    return res

def _decode_nibble(buf, off, w, h, fmt, pal):
    out = bytearray(w * h * 4)
    n = off * 2
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for py in range(8):
                for px in range(8):
                    b = buf[n >> 1]
                    v = (b >> 4) if not (n & 1) else (b & 15)
                    n += 1
                    x, y = tx + px, ty + py
                    if x < w and y < h:
                        if fmt == 0:
                            c = (v * 17, v * 17, v * 17, 255)
                        else:
                            c = pal[v] if pal else (v * 17,) * 3 + (255,)
                        i = (y * w + x) * 4
                        out[i:i + 4] = bytes(c)
    return out

def write_png(path, w, h, rgba):
    import zlib
    raw = b''.join(b'\0' + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))
    def chunk(t, d):
        c = struct.pack('>I', len(d)) + t + d
        return c + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)

# ---------------------------------------------------------------- BIN model
NRM_SCALE = 1.0 / 16384.0
UV_SCALE = 1.0 / 256.0

class Model:
    pass

def read_bin(data):
    m = Model()
    hdr = struct.unpack_from('>16I', data, 0)
    bone_off, col_off, uv_off, w_off, draw_off = hdr[0], hdr[3], hdr[4], hdr[5], hdr[7]
    nweight, nbone, _, ngroup = data[0x18], data[0x19], data[0x1a], data[0x1b]
    m.tex_count = hdr[9]
    shift = data[0x28]
    m.pos_scale = 1.0 / (1 << shift)
    pos_off, nrm_off = hdr[12], hdr[13]
    npos, nnrm = struct.unpack_from('>HH', data, 0x38)
    m.bones = []  # (id, parent_id or None, (x,y,z))
    for i in range(nbone):
        o = bone_off + 16 * i
        bid, par = data[o], data[o + 1]
        x, y, z = struct.unpack_from('>3f', data, o + 4)
        m.bones.append((bid, None if par == 0xff else par, (x, y, z)))
    m.positions = []
    m.pos_weight = []
    sc = m.pos_scale
    for i in range(npos):
        x, y, z, wi = struct.unpack_from('>3hH', data, pos_off + 8 * i)
        m.positions.append((x * sc, y * sc, z * sc))
        m.pos_weight.append(wi)
    m.normals = [tuple(v * NRM_SCALE for v in struct.unpack_from('>3h', data, nrm_off + 8 * i)) for i in range(nnrm)]
    nuv = (w_off - uv_off) // 4
    m.uvs = [tuple(v * UV_SCALE for v in struct.unpack_from('>2H', data, uv_off + 4 * i)) for i in range(nuv)]
    ncol = (uv_off - col_off) // 4
    m.colors = [tuple(data[col_off + 4 * i: col_off + 4 * i + 4]) for i in range(ncol)]
    m.weights = []
    for k in range((draw_off - w_off) // 8):
        o = w_off + 8 * k
        b0, b1, b2, cnt, w0, w1, w2 = struct.unpack_from('>7B', data, o)
        bw = [(b0, w0), (b1, w1), (b2, w2)][:max(cnt, 1)]
        tot = float(sum(w for _, w in bw)) or 100.0
        sc = 1.0 / tot   # percentages are rounded (98, 101, even 200)
        m.weights.append([(b, w * sc) for b, w in bw])
    m.has_color = bool(hdr[8] & 0x80000000)
    stride = 8 if m.has_color else 6
    m.groups = []
    p = draw_off
    while p + 0x20 <= len(data) and len(m.groups) < (ngroup or 9999):
        g = data[p:p + 0x20]
        dl = struct.unpack_from('>I', g, 0x18)[0]
        if dl == 0 or p + 0x20 + dl > len(data):
            break
        tex = [t for t in g[0x0c:0x10]]
        prims = []
        q = p + 0x20; end = q + dl
        while q + 3 <= end and data[q]:
            op = data[q] & 0xf8
            cnt = struct.unpack_from('>H', data, q + 1)[0]
            if q + 3 + stride * cnt > end:
                break
            if m.has_color:
                verts = [struct.unpack_from('>4H', data, q + 3 + 8 * k) for k in range(cnt)]
            else:
                verts = [(lambda a, b, c: (a, b, 0, c))(*struct.unpack_from('>3H', data, q + 3 + 6 * k)) for k in range(cnt)]
            prims.append((op, verts))
            q += 3 + stride * cnt
        m.groups.append(dict(header=g, tex=tex, kind=g[0x0b], prims=prims))
        p = end
    return m

def triangles(prims):
    tris = []
    for op, v in prims:
        if op == 0x90:
            for i in range(0, len(v) - 2, 3):
                tris.append((v[i], v[i + 1], v[i + 2]))
        elif op == 0x98:
            for i in range(len(v) - 2):
                if i & 1:
                    tris.append((v[i + 1], v[i], v[i + 2]))
                else:
                    tris.append((v[i], v[i + 1], v[i + 2]))
        elif op == 0x80:  # GX_QUADS
            for i in range(0, len(v) - 3, 4):
                tris.append((v[i], v[i + 1], v[i + 2]))
                tris.append((v[i], v[i + 2], v[i + 3]))
        elif op == 0xa0:
            for i in range(1, len(v) - 1):
                tris.append((v[0], v[i], v[i + 1]))
    return tris

# ---------------------------------------------------------------- FCV animation
KEY_SIZE = {0: 12, 1: 8, 2: 6, 4: 10, 5: 6, 6: 4, 8: 9, 9: 5, 10: 3}
INT_SCALE = 1e-4

def _rd(fmt, data, o):
    if fmt == 0:
        return struct.unpack_from('>f', data, o)[0], 4
    if fmt == 1:
        return struct.unpack_from('>h', data, o)[0] * INT_SCALE, 2
    return struct.unpack_from('>b', data, o)[0] * INT_SCALE, 1

class Curve:
    __slots__ = ('frames', 'keys')
    def __init__(self, frames, keys):
        self.frames = frames; self.keys = keys  # keys: (value, tan_in, tan_out)
    def eval(self, f):
        fr = self.frames; n = len(fr)
        if n == 0:
            return 0.0
        if f <= fr[0]:
            return self.keys[0][0]
        if f >= fr[-1]:
            return self.keys[-1][0]
        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if fr[mid] <= f: lo = mid
            else: hi = mid
        f0, f1 = fr[lo], fr[hi]
        if f == f0:
            return self.keys[lo][0]
        s = (f - f0) / (f1 - f0)
        p0, _, m0 = self.keys[lo]
        p1, m1, _ = self.keys[hi]
        s2 = s * s; s3 = s2 * s
        h01 = 3 * s2 - 2 * s3
        return p0 * (1 - h01) + p1 * h01 + m0 * (s3 - 2 * s2 + s) + m1 * (s3 - s2)

class Track:
    pass

def read_fcv(data):
    if len(data) < 8:
        return None
    nframes, ntr = struct.unpack_from('>HB', data, 0)
    desc = [struct.unpack_from('>H', data, 3 + 2 * i)[0] for i in range(ntr)]
    bone = list(data[3 + 2 * ntr: 3 + 3 * ntr])
    p = 3 + 3 * ntr
    p = (p + 3) & ~3
    extra = struct.unpack_from('>I', data, p)[0]
    toff = p + 4
    tracks = []
    for i in range(ntr):
        o = struct.unpack_from('>I', data, toff + 4 * i)[0]
        t = Track()
        t.desc = desc[i]; t.type = desc[i] >> 12; t.pole = (desc[i] >> 8) & 15
        t.flags = desc[i] & 0xff; t.bone = bone[i]; t.curves = None
        if o and t.type in KEY_SIZE:
            vf, tf = t.type >> 2, t.type & 3
            curves = []
            q = o
            for ax in range(3):
                n = struct.unpack_from('>H', data, q)[0]; q += 2
                frames = list(struct.unpack_from('>%dH' % n, data, q)); q += 2 * n
                keys = []
                for k in range(n):
                    v, a = _rd(vf, data, q); q += a
                    ti, a = _rd(tf, data, q); q += a
                    to, a = _rd(tf, data, q); q += a
                    keys.append((v, ti, to))
                curves.append(Curve(frames, keys))
            t.curves = curves
        tracks.append(t)
    return dict(frames=nframes, tracks=tracks, extra=extra)

# ---------------------------------------------------------------- math helpers (3x4 as row lists)
def mat_rot_xyz(r):
    sx, sy, sz = math.sin(r[0]), math.sin(r[1]), math.sin(r[2])
    cx, cy, cz = math.cos(r[0]), math.cos(r[1]), math.cos(r[2])
    return [[cz * cy, cz * sy * sx - sz * cx, cz * cx * sy + sz * sx, 0.0],
            [sz * cy, sz * sx * sy + cz * cx, sz * cx * sy - cz * sx, 0.0],
            [-sy, cy * sx, cy * cx, 0.0]]

def mat_mul(a, b):
    r = []
    for i in range(3):
        row = []
        for j in range(4):
            v = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
            if j == 3:
                v += a[i][3]
            row.append(v)
        r.append(row)
    return r

def mat_vec(m, v):
    return [m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] + m[i][3] for i in range(3)]

def mat_vec3(m, v):
    return [m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3)]

def mat_inv(m):
    # rigid-ish inverse with general 3x3 inverse
    a = [[m[i][j] for j in range(3)] for i in range(3)]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    inv = [[(a[1][1] * a[2][2] - a[1][2] * a[2][1]) / det, (a[0][2] * a[2][1] - a[0][1] * a[2][2]) / det, (a[0][1] * a[1][2] - a[0][2] * a[1][1]) / det],
           [(a[1][2] * a[2][0] - a[1][0] * a[2][2]) / det, (a[0][0] * a[2][2] - a[0][2] * a[2][0]) / det, (a[0][2] * a[1][0] - a[0][0] * a[1][2]) / det],
           [(a[1][0] * a[2][1] - a[1][1] * a[2][0]) / det, (a[0][1] * a[2][0] - a[0][0] * a[2][1]) / det, (a[0][0] * a[1][1] - a[0][1] * a[1][0]) / det]]
    t = [m[0][3], m[1][3], m[2][3]]
    it = [-(inv[i][0] * t[0] + inv[i][1] * t[1] + inv[i][2] * t[2]) for i in range(3)]
    return [inv[i] + [it[i]] for i in range(3)]

def mat_from_cols(x, y, z, t=(0, 0, 0)):
    return [[x[0], y[0], z[0], t[0]], [x[1], y[1], z[1], t[1]], [x[2], y[2], z[2], t[2]]]

def mat_T(m):
    return [[m[0][0], m[1][0], m[2][0], 0.0], [m[0][1], m[1][1], m[2][1], 0.0], [m[0][2], m[1][2], m[2][2], 0.0]]

def rot_axis(axis, ang):
    x, y, z = axis; c, s = math.cos(ang), math.sin(ang); t = 1 - c
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0.0],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0.0],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0.0]]

def _norm(v):
    l = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return [v[0] / l, v[1] / l, v[2] / l] if l > 1e-12 else [0.0, 0.0, 0.0]

def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]

def _dist(a, b):
    d = _sub(a, b); return math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)

def _set_orientation_zy(dirv, pole):
    a = _norm(pole); d = _norm(dirv)
    x = _norm(_cross(a, d)); y = _norm(_cross(d, x))
    return mat_from_cols(x, y, d)

def _set_orientation_zx(dirv, axis):
    a = _norm(axis); d = _norm(dirv)
    y = _norm(_cross(d, a)); x = _norm(_cross(y, d))
    return mat_from_cols(x, y, d)

POLE_AXES = {0: (0, 1, 0), 1: (0, -1, 0), 2: (1, 0, 0), 3: (-1, 0, 0), 4: (0, 0, 1), 5: (0, 0, -1)}

# ---------------------------------------------------------------- pose solver
class Skeleton:
    def __init__(self, bones):
        self.ids = [b[0] for b in bones]
        self.index = {b[0]: i for i, b in enumerate(bones)}
        self.parent = [self.index.get(b[1]) if b[1] is not None else None for b in bones]
        self.rest = [list(b[2]) for b in bones]
        n = len(bones)
        self.order = []
        seen = set()
        def visit(i):
            if i in seen: return
            p = self.parent[i]
            if p is not None: visit(p)
            seen.add(i); self.order.append(i)
        for i in range(n): visit(i)
        self.rest_world = [None] * n
        for i in self.order:
            p = self.parent[i]; o = self.rest[i]
            self.rest_world[i] = list(o) if p is None else [self.rest_world[p][k] + o[k] for k in range(3)]
        self.length = [0.0] * n
        for i in range(n - 1):
            self.length[i] = _dist(self.rest_world[i], self.rest_world[i + 1])

    def is_desc(self, b, anc):
        while b is not None:
            if b == anc: return True
            b = self.parent[b]
        return False

def anim_bone_ids(anim):
    return {t.bone for t in anim['tracks'] if t.curves is not None}

def animated_indices(skel, anim):
    s = set()
    for t in anim['tracks']:
        if t.curves is None or t.bone not in skel.index: continue
        i = skel.index[t.bone]; s.add(i)
        if t.flags & 0x30:
            s.update(j for j in (i, i + 1, i + 2) if j < len(skel.ids))
    out = set(s)
    for j in range(len(skel.ids)):
        if any(skel.is_desc(j, a) for a in s):
            out.add(j)
    return out

def evaluate(skel, anim, frame, root_motion=False):
    n = len(skel.ids)
    rot = [[0.0, 0.0, 0.0] for _ in range(n)]
    trn = [list(r) for r in skel.rest]
    scl = [[1.0, 1.0, 1.0] for _ in range(n)]
    ik = []
    root_mv = [0.0, 0.0, 0.0]
    goal = {}
    for t in anim['tracks']:
        if t.curves is None or t.bone not in skel.index:
            continue
        b = skel.index[t.bone]
        v = [c.eval(frame) for c in t.curves]
        f = t.flags
        if f & 0x30:
            rot[b] = v; ik.append((b, t.pole, bool(f & 0x20)))
        elif f & 0x02:
            rot[b] = v
        elif f & 0x04:
            trn[b] = v; goal[b] = v
        elif f & 0x08:
            scl[b] = v
        elif f & 0x01:
            root_mv = v
    W = [None] * n
    def local(i):
        L = mat_rot_xyz(rot[i])
        for r in range(3):
            L[r][3] = trn[i][r]
            for c in range(3):
                L[r][c] *= scl[i][c]
        return L
    for i in skel.order:
        p = skel.parent[i]
        W[i] = local(i) if p is None else mat_mul(W[p], local(i))
    for thigh, pole, heel in ik:
        knee, foot, toe = thigh + 1, thigh + 2, thigh + 3
        if foot >= n:
            continue
        g = goal.get(foot, [W[foot][k][3] for k in range(3)])
        rest_dir = _sub(skel.rest_world[foot], skel.rest_world[thigh])
        O = _set_orientation_zy(rest_dir, POLE_AXES.get(pole, (0, 0, 1)))
        OT = mat_T(O)
        x0 = [O[0][0], O[1][0], O[2][0]]
        Rf = mat_rot_xyz(rot[foot])
        if heel and toe < n and skel.parent[toe] == foot:
            off = mat_vec3(Rf, trn[toe])
            fpos = [g[k] - off[k] for k in range(3)]
        else:
            fpos = list(g)
        P = [W[thigh][k][3] for k in range(3)]
        L1, L2 = skel.length[thigh], skel.length[knee]
        d = _dist(P, fpos)
        a1 = a2 = 0.0
        if 1e-6 < d < L1 + L2:
            c1 = max(-1.0, min(1.0, (L1 * L1 + d * d - L2 * L2) / (2 * L1 * d)))
            c2 = max(-1.0, min(1.0, (d * d + L2 * L2 - L1 * L1) / (2 * d * L2)))
            a1, a2 = math.acos(c1), math.acos(c2)
        axw = mat_vec3(W[thigh], x0)
        M = mat_mul(_set_orientation_zx(_sub(fpos, P), axw), OT)
        Wt = mat_mul(M, rot_axis(x0, -a1))
        for k in range(3): Wt[k][3] = P[k]
        Wk = mat_mul(M, rot_axis(x0, a2))
        kp = mat_vec(Wt, trn[knee])
        for k in range(3): Wk[k][3] = kp[k]
        fp = mat_vec(Wk, skel.rest[foot]) if heel else fpos
        Wf = [row[:] for row in Rf]
        for k in range(3): Wf[k][3] = fp[k]
        W[thigh], W[knee], W[foot] = Wt, Wk, Wf
        for i in skel.order:
            p = skel.parent[i]
            if p is not None and i not in (thigh, knee, foot) and skel.is_desc(p, thigh):
                W[i] = mat_mul(W[p], local(i))
    if root_motion and any(root_mv):
        for i in range(n):
            for k in range(3):
                W[i][k][3] += root_mv[k]
    return W


# ---------------------------------------------------------------- LZS
def lzs_decode(src):
    out_size, flag_len, _ = struct.unpack_from('>III', src, 0)
    fp = 0xC; dp = 0xC + flag_len
    out = bytearray()
    fbyte = src[fp]; fp += 1; bit = 7
    n = len(src)
    while True:
        # inline bit reader
        if bit:
            b = (fbyte >> bit) & 1; bit -= 1
        else:
            b = fbyte & 1; bit = 7; fbyte = src[fp] if fp < n else 0; fp += 1
        if b:
            out.append(src[dp]); dp += 1
            continue
        if bit:
            b = (fbyte >> bit) & 1; bit -= 1
        else:
            b = fbyte & 1; bit = 7; fbyte = src[fp] if fp < n else 0; fp += 1
        if b:
            b0, b1 = src[dp], src[dp + 1]; dp += 2
            v = b0 | (b1 << 8); off = v >> 3; ln = b0 & 7
            if ln:
                ln += 2
            else:
                ln = src[dp] + 9; dp += 1
                if off == 0:
                    break
        else:
            bits = 0
            for _ in range(2):
                if bit:
                    b = (fbyte >> bit) & 1; bit -= 1
                else:
                    b = fbyte & 1; bit = 7; fbyte = src[fp] if fp < n else 0; fp += 1
                bits = bits * 2 + b
            ln = bits + 2
            off = src[dp]; dp += 1
        s = len(out) - off
        if off >= ln:
            out += out[s:s + ln]
        else:
            for k in range(ln):
                out.append(out[s + k])
    return bytes(out)


# ---------------------------------------------------------------- EVT cutscenes (room archive entries)
def _cstr(b, o, n=12):
    return b[o:o + n].split(b'\0')[0].decode('latin1')

def read_evt(ev):
    no, mo, co = struct.unpack_from('>3I', ev, 0)
    models = []
    for k in range(struct.unpack_from('>I', ev, mo)[0]):
        slot = mo + 4 + 4 * k; p = slot + struct.unpack_from('>I', ev, slot)[0]
        q = p + 4 + struct.unpack_from('>I', ev, p + 4)[0]
        models.append(_cstr(ev, q + struct.unpack_from('>I', ev, q)[0]))
    cuts = []
    for c in range(struct.unpack_from('>I', ev, co)[0]):
        o = co + 4 + 12 * c; rel, fr, sz = struct.unpack_from('>3I', ev, o)
        cuts.append((fr, o + rel))
    return dict(no=no, models=models, cuts=cuts)

def read_cut(ev, off):
    d = lzs_decode(ev[off:])
    sec = struct.unpack_from('>5I', d, 0)
    n = struct.unpack_from('>I', d, sec[0])[0]
    cam = []
    for f in range(n):
        r = struct.unpack_from('>8f', d, sec[0] + 4 + 32 * f)
        cam.append(([v * 100 for v in r[0:3]], [v * 100 for v in r[3:6]], r[6], r[7]))
    lists = []
    for s in sec[2:5]:
        items = []
        for k in range(struct.unpack_from('>I', d, s)[0]):
            slot = s + 4 + 4 * k; p = slot + struct.unpack_from('>I', d, slot)[0]
            items.append((_cstr(d, p), struct.unpack_from('>I', d, p + 12)[0], p + 16))
        lists.append(items)
    motions = [(nm, v, read_fcv(d[p:])) for nm, v, p in lists[0]]
    return dict(raw=d, camera=cam, motions=motions, effects=lists[1], shapes=lists[2])
