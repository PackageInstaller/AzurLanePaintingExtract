"""Unity AssetBundle 立绘还原与表情贴图。"""
import os
import struct

import UnityPy
from PIL import Image



UNITY_FALLBACK = "2022.3.62f3"
HEADER64 = b"\x1bLJ\x02\x0a"
# Senkin / 游戏内置透明占位 sprite (touming_tex)
_TOUMING_SPRITES = frozenset((-1941817362335269276, -627025325541918145))
_SKIP_LAYER_GO = frozenset(
    {
        "touch",
        "shadow",
        "hx",
        "shop_hx",
        "shophx",
        "face",
        "face_sub",
        "layers",
        "drag",
    }
)
# 只作为 prefab 子层 / 和谐替换, 不单独导出
_SKIP_TEX_SUFFIXES = frozenset(
    {
        "rw",
        "n_rw",
        "front",
        "n_front",
        "middle",
        "bj",
        "shophx",
        "shop_hx",
    }
)
def _chan(c, key):
    return c[key] if isinstance(c, dict) else getattr(c, key)


def _decode_vertices(vd):
    """通用顶点解码, 支持多 stream 布局 (Mesh / Sprite.m_RD 通用)。"""
    channels = list(_chan(vd, "m_Channels") or [])
    n = _chan(vd, "m_VertexCount")
    actives = [c for c in channels if _chan(c, "dimension")]
    for c in actives:
        if _chan(c, "format") != 0:  # 仅支持 Float32
            raise RuntimeError(f"不支持的顶点格式 {_chan(c, 'format')}")

    # 每个 stream 独立连续存储: stream 块按顺序拼接在 m_DataSize 里
    by_stream = {}
    for c in actives:
        by_stream.setdefault(_chan(c, "stream"), []).append(c)
    stream_base = {}
    base = 0
    for s in sorted(by_stream):
        stride = max(_chan(c, "offset") + _chan(c, "dimension") * 4 for c in by_stream[s])
        stream_base[s] = (base, stride)
        base += stride * n

    data = bytes(_chan(vd, "m_DataSize"))
    pos_idx = next(
        (i for i, c in enumerate(channels) if _chan(c, "dimension") >= 3), None
    )
    uv_idx = next(
        (i for i, c in enumerate(channels) if i == 4 and _chan(c, "dimension") >= 2),
        None,
    )
    if pos_idx is None:
        raise RuntimeError("顶点数据缺少位置通道")

    def read(i, c):
        sbase, stride = stream_base[_chan(c, "stream")]
        off = sbase + i * stride + _chan(c, "offset")
        dim = _chan(c, "dimension")
        return struct.unpack_from(f"<{dim}f", data, off)

    verts = []
    for i in range(n):
        x, y, z = read(i, channels[pos_idx])[:3]
        if uv_idx is not None:
            u, v = read(i, channels[uv_idx])[:2]
        else:
            u = v = 0.0
        verts.append((x, y, z, u, v))
    return verts


def _decode_mesh(mesh):
    verts = _decode_vertices(mesh.m_VertexData)
    buf = list(mesh.m_IndexBuffer)
    if mesh.m_IndexFormat == 0:  # UInt16
        idx = [buf[i] | (buf[i + 1] << 8) for i in range(0, len(buf) - 1, 2)]
    else:  # UInt32
        idx = [
            buf[i] | (buf[i + 1] << 8) | (buf[i + 2] << 16) | (buf[i + 3] << 24)
            for i in range(0, len(buf) - 3, 4)
        ]
    return verts, idx


def _decode_sprite_mesh(sprite):
    """Sprite 自带网格 (SpriteRenderData.m_VertexData / m_IndexBuffer)。"""
    tt = sprite.read_typetree()
    rd = tt.get("m_RD") or {}
    verts = _decode_vertices(rd["m_VertexData"])
    buf = list(rd.get("m_IndexBuffer") or [])
    idx = [buf[i] | (buf[i + 1] << 8) for i in range(0, len(buf) - 1, 2)]
    return verts, idx


def _sprite_internal_vertex_count(sprite):
    try:
        rd = (sprite.read_typetree() or {}).get("m_RD") or {}
        return (rd.get("m_VertexData") or {}).get("m_VertexCount") or 0
    except Exception:
        return 0


def _stitch(mesh, tex_img, canvas_size=None):
    verts, idx = _decode_mesh(mesh)
    w, h = tex_img.size
    # 与原版 image_deal.py 一致:
    # v 顶点取整作为绘制坐标, 画布 y 翻转; vt 按像素取整作为裁剪坐标
    draw_raw = [(int(round(x)), int(round(y))) for x, y, z, u, v in verts]
    uv = [(round(u * w), round((1.0 - v) * h)) for x, y, z, u, v in verts]
    if canvas_size:
        x_pic, y_pic = int(canvas_size[0]), int(canvas_size[1])
    else:
        x_pic = max(x for x, y in draw_raw)
        y_pic = max(y for x, y in draw_raw)
    pic = Image.new("RGBA", (x_pic, y_pic), (255, 255, 255, 0))
    for i in range(0, len(idx) - 2, 3):
        a, b, c = idx[i], idx[i + 1], idx[i + 2]
        pts = [
            (draw_raw[a][0], y_pic - draw_raw[a][1]),
            (draw_raw[b][0], y_pic - draw_raw[b][1]),
            (draw_raw[c][0], y_pic - draw_raw[c][1]),
        ]
        cuts = [uv[a], uv[b], uv[c]]
        pa = (
            min(p[0] for p in pts),
            min(p[1] for p in pts),
        )
        cx = min(p[0] for p in cuts)
        cy = min(p[1] for p in cuts)
        ex = max(p[0] for p in cuts)
        ey = max(p[1] for p in cuts)
        if ex <= cx or ey <= cy:
            continue
        crop = tex_img.crop((cx, cy, ex, ey))
        pic.paste(crop, pa)
    return pic


def _crop_sprite(tex_img, sprite):
    if sprite is None:
        return tex_img
    try:
        rect = sprite.read_typetree().get("m_Rect") or {}
        x = round(rect.get("x", 0))
        y = round(rect.get("y", 0))
        w = round(rect.get("width", tex_img.width))
        h = round(rect.get("height", tex_img.height))
        if (x, y, w, h) == (0, 0, tex_img.width, tex_img.height):
            return tex_img
        top = tex_img.height - y - h
        return tex_img.crop((x, top, x + w, top + h))
    except Exception:
        return tex_img


def _tex_prefab_mesh_info(tex_path):
    """同名 prefab MeshImage: (uses_mesh True/False/None, 引用的 Mesh path_id)。"""
    base = os.path.basename(tex_path)
    if base.endswith("_tex"):
        base = base[: -len("_tex")]
    prefab = os.path.join(os.path.dirname(tex_path), base)
    if not os.path.isfile(prefab):
        return None, 0
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
        env = UnityPy.load(prefab)
        name = {}
        for obj in env.objects:
            if obj.type.name == "GameObject":
                try:
                    name[obj.path_id] = obj.read().m_Name
                except Exception:
                    pass
        found_mesh = False
        found_nomesh = False
        mesh_id = 0
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            tt = obj.read_typetree()
            go = tt.get("m_GameObject", {}).get("m_PathID")
            if (name.get(go) or "").lower() != base.lower():
                continue
            if "mMesh" not in tt:
                continue
            sp = (tt.get("m_Sprite") or {}).get("m_PathID") or 0
            if sp in _TOUMING_SPRITES:
                continue
            pid = (tt.get("mMesh") or {}).get("m_PathID") or 0
            if pid:
                found_mesh = True
                mesh_id = pid
            else:
                found_nomesh = True
        if found_mesh:
            return True, mesh_id
        if found_nomesh:
            return False, 0
    except Exception:
        pass
    return None, 0


def _tex_prefab_uses_mesh(tex_path):
    """同名 prefab 的 MeshImage 是否引用外部 Mesh。

    MeshImage.OnPopulateMesh: mMesh 为空时走 Image 默认四边形, 直接显示 Sprite。
    部分立绘的 _tex 包仍带 Mesh (如 ouruola_h), 按它拼图会把已排好的图集拆碎。
    返回 True / False / None (找不到对应 MeshImage, 保持旧行为)。
    """
    used, _ = _tex_prefab_mesh_info(tex_path)
    return used


def _bust_uses_mesh(out_root, key):
    """半身节点是否引用外部 Mesh: 由 painting/<key> prefab 决定。

    部分皮肤的半身层是 MeshImage + 外部 Mesh (如 huangjiacaifu),
    部分是纯 Sprite (如 aierbin_3, mMesh=0)。
    """
    prefab = os.path.join(out_root, "Assets", "painting", key)
    if not os.path.isfile(prefab):
        return False
    used = _tex_prefab_uses_mesh(os.path.join(os.path.dirname(prefab), key + "_tex"))
    return bool(used)


def _layer_raw_size(out_root, key):
    """prefab 中 key 层 MeshImage 的 mRawSpriteSize。"""
    prefab = os.path.join(out_root, "Assets", "painting", key)
    if not os.path.isfile(prefab):
        return None
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
        env = UnityPy.load(prefab)
        name = {}
        rt_by_go = {}
        for obj in env.objects:
            tt = obj.read_typetree()
            go = (tt.get("m_GameObject") or {}).get("m_PathID")
            if obj.type.name == "GameObject":
                try:
                    name[obj.path_id] = obj.read().m_Name
                except Exception:
                    pass
            elif obj.type.name == "RectTransform":
                rt_by_go[go] = tt
        root = _prefab_root_go(name, rt_by_go, key)
        if root is None:
            return None
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            tt = obj.read_typetree()
            if (tt.get("m_GameObject") or {}).get("m_PathID") != root:
                continue
            raw = tt.get("mRawSpriteSize")
            if raw and raw.get("x") and raw.get("y"):
                return (raw["x"], raw["y"])
    except Exception:
        return None
    return None


def _load_face_image(out_root, key, expr):
    """从 paintingface/<key> 取编号 expr 的表情图。"""
    path = os.path.join(out_root, "Assets", "paintingface", key)
    if not os.path.isfile(path):
        return None
    try:
        env = UnityPy.load(path)
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            d = obj.read()
            c = str(getattr(obj, "container", "") or "")
            if d.m_Name == expr or c.endswith(f"/{expr}.png"):
                return d.image
    except Exception:
        return None
    return None


def _list_face_exprs(out_root, key):
    """paintingface/<key> 里全部表情编号。"""
    path = os.path.join(out_root, "Assets", "paintingface", key)
    if not os.path.isfile(path):
        return []
    names = set()
    try:
        env = UnityPy.load(path)
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            d = obj.read()
            c = str(getattr(obj, "container", "") or "")
            if "/PaintingFace/" in c:
                names.add(c.rsplit("/", 1)[-1].removesuffix(".png"))
            elif d.m_Name:
                names.add(d.m_Name)
    except Exception:
        return []
    # paintingface 里 *_sub 是 face_sub 子层, 不是独立表情 (ShipExpressionHelper._UpdateExpression)
    names = {n for n in names if n and not str(n).endswith("_sub")}
    return sorted(names, key=lambda x: (len(x), x))


def _prefab_root_go(gname, rt_by_go, target):
    """名为 target 且 RectTransform 无父节点的根 GO (避开 layers 里的同名子层)。"""
    want = (target or "").lower()
    fallback = None
    for go, nm in gname.items():
        if (nm or "").lower() != want:
            continue
        tt = rt_by_go.get(go)
        if not tt:
            continue
        fallback = go
        if not (tt.get("m_Father") or {}).get("m_PathID"):
            return go
    return fallback


def _rt_info(tt):
    return {
        "pos": tt.get("m_AnchoredPosition") or {"x": 0.0, "y": 0.0},
        "size": tt.get("m_SizeDelta") or {"x": 0.0, "y": 0.0},
        "scale": tt.get("m_LocalScale") or {"x": 1.0, "y": 1.0, "z": 1.0},
        "pivot": tt.get("m_Pivot") or {"x": 0.5, "y": 0.5},
    }


def _key_local_to_raw(key_rt, raw, delta, lx, ly):
    """key 层局部坐标 -> MeshImage raw 画布坐标 (y 向上)。"""
    rx, ry = raw["x"], raw["y"]
    rect_w = key_rt["size"]["x"]
    rect_h = key_rt["size"]["y"]
    if not rx or not ry or not rect_w or not rect_h:
        return None
    val_x = rect_w / rx
    val_y = rect_h / ry
    dx, dy = delta
    num_x = -key_rt["pivot"]["x"] * rx + dx
    num_y = -key_rt["pivot"]["y"] * ry + dy
    return lx / val_x - num_x, ly / val_y - num_y


def _prefab_key_is_touming(out_root, prefab_key):
    """key 层 Sprite 是否为 touming 透明占位。"""
    prefab = os.path.join(out_root, "Assets", "painting", prefab_key)
    if not os.path.isfile(prefab):
        return False
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
        env = UnityPy.load(prefab)
        name = {}
        rt_by_go = {}
        for obj in env.objects:
            tt = obj.read_typetree()
            go = (tt.get("m_GameObject") or {}).get("m_PathID")
            if obj.type.name == "GameObject":
                try:
                    name[obj.path_id] = obj.read().m_Name
                except Exception:
                    pass
            elif obj.type.name == "RectTransform":
                rt_by_go[go] = tt
        root = _prefab_root_go(name, rt_by_go, prefab_key)
        if root is None:
            return False
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            tt = obj.read_typetree()
            if (tt.get("m_GameObject") or {}).get("m_PathID") != root:
                continue
            if "m_Sprite" not in tt:
                continue
            return ((tt.get("m_Sprite") or {}).get("m_PathID") or 0) in _TOUMING_SPRITES
    except Exception:
        return False
    return False


def _skip_layer_name(nm):
    low = (nm or "").lower()
    if low in _SKIP_LAYER_GO:
        return True
    if low.startswith("shop_hx") or low.startswith("shophx"):
        return True
    return False


_tex_index_cache = {}


def _painting_tex_index(painting_dir):
    """小写名 -> 实际 *_tex 路径, 避免 Linux 下 2B_rw / 2b_rw 对不上。"""
    try:
        stamp = os.stat(painting_dir).st_mtime
    except OSError:
        return {}
    cached = _tex_index_cache.get(painting_dir)
    if cached and cached[0] == stamp:
        return cached[1]
    idx = {}
    try:
        for fn in os.listdir(painting_dir):
            if fn.endswith("_tex"):
                idx[fn.lower()] = os.path.join(painting_dir, fn)
    except OSError:
        return {}
    _tex_index_cache[painting_dir] = (stamp, idx)
    return idx


def _find_tex(painting_dir, name):
    if not name:
        return None
    return _painting_tex_index(painting_dir).get((name + "_tex").lower())


def _prefab_key_tex_path(painting_dir, prefab_key):
    """key 层 *_tex。_hx prefab 常没有独立底图, 复用未和谐 key/_n。"""
    cands = [prefab_key]
    if prefab_key.endswith("_n_hx"):
        cands.append(prefab_key[: -len("_hx")])
    elif prefab_key.endswith("_hx"):
        cands.append(prefab_key[: -len("_hx")])
    seen = set()
    for n in cands:
        if n in seen:
            continue
        seen.add(n)
        path = _find_tex(painting_dir, n)
        if path:
            return path
    return None


def _layer_tex_path(painting_dir, layer_name, prefab_key):
    """子层对应的 *_tex。忽略大小写; _rw_n 节点常对应 *_rw。"""
    cands = []
    if prefab_key.endswith("_n"):
        parts = layer_name.split("_")
        if len(parts) >= 2:
            cands.append(prefab_key + "_" + parts[-1])
            cands.append("_".join(parts[:-1]) + "_n_" + parts[-1])
        if layer_name.endswith("_rw"):
            cands.append(prefab_key + "_rw")
    if layer_name.endswith("_rw_n"):
        cands.append(layer_name[: -len("_n")])
        cands.append(layer_name[: -len("_rw_n")] + "_n_rw")
    if prefab_key.endswith("_hx") and not layer_name.endswith("_hx"):
        cands.append(layer_name + "_hx")
    cands.append(layer_name)
    seen = set()
    for n in cands:
        if n in seen:
            continue
        seen.add(n)
        path = _find_tex(painting_dir, n)
        if path:
            return path
    return None


def _prefab_extra_layers(out_root, prefab_key):
    """prefab 根节点下的可绘制子层 (含 _rw / front / middle / bj)。

    游戏 setPaintingPrefab 直接实例化整棵 prefab, Unity UI 按兄弟顺序绘制:
    key 底图 -> 子节点 (layers 容器或直接挂的 _rw) -> face。
    `_n` prefab 的 key 常是 touming 透明占位; 部分皮肤没有 layers 容器,
    人物层直接挂在根节点下 (如 changmen_6)。
    """
    prefab = os.path.join(out_root, "Assets", "painting", prefab_key)
    if not os.path.isfile(prefab):
        return []
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
        env = UnityPy.load(prefab)
        gname = {}
        for obj in env.objects:
            if obj.type.name == "GameObject":
                try:
                    gname[obj.path_id] = obj.read().m_Name
                except Exception:
                    pass
        rt_by_go = {}
        mb_by_go = {}
        for obj in env.objects:
            tt = obj.read_typetree()
            go = (tt.get("m_GameObject") or {}).get("m_PathID")
            if obj.type.name == "RectTransform":
                rt_by_go[go] = tt
            elif obj.type.name == "MonoBehaviour":
                mb_by_go.setdefault(go, []).append(tt)
        key_go = _prefab_root_go(gname, rt_by_go, prefab_key)
        if key_go is None or key_go not in rt_by_go:
            return []
        key_mb = next(
            (mb for mb in mb_by_go.get(key_go, []) if "mRawSpriteSize" in mb),
            {},
        )
        key_info = {
            "rt": _rt_info(rt_by_go[key_go]),
            "raw": key_mb.get("mRawSpriteSize") or {},
            "delta": (
                key_mb.get("delta_offset_x", 0.0),
                key_mb.get("delta_offset_y", 0.0),
            ),
        }
        rt_obj = {}
        for obj in env.objects:
            if obj.type.name == "RectTransform":
                rt_obj[obj.path_id] = obj

        def append_go(go, tt, layers, seen):
            if go in seen or go == key_go:
                return
            nm = gname.get(go) or ""
            if _skip_layer_name(nm):
                return
            mb = next(
                (m for m in mb_by_go.get(go, []) if "m_Sprite" in m or "mRawSpriteSize" in m),
                None,
            )
            if mb is None:
                return
            sp = (mb.get("m_Sprite") or {}).get("m_PathID") or 0
            if not sp or sp in _TOUMING_SPRITES:
                return
            raw = mb.get("mRawSpriteSize") or {}
            seen.add(go)
            layers.append(
                {
                    "name": nm,
                    "rt": _rt_info(tt),
                    "raw": raw,
                    "delta": (
                        mb.get("delta_offset_x", 0.0),
                        mb.get("delta_offset_y", 0.0),
                    ),
                    "use_mesh": bool((mb.get("mMesh") or {}).get("m_PathID")),
                    "key": key_info,
                }
            )

        layers = []
        seen = set()
        for ptr in rt_by_go[key_go].get("m_Children") or []:
            robj = rt_obj.get(ptr.get("m_PathID"))
            if robj is None:
                continue
            tt = robj.read_typetree()
            go = (tt.get("m_GameObject") or {}).get("m_PathID")
            nm = (gname.get(go) or "").lower()
            if nm == "layers" and go in rt_by_go:
                for cptr in rt_by_go[go].get("m_Children") or []:
                    cobj = rt_obj.get(cptr.get("m_PathID"))
                    if cobj is None:
                        continue
                    ctt = cobj.read_typetree()
                    append_go(
                        (ctt.get("m_GameObject") or {}).get("m_PathID"),
                        ctt,
                        layers,
                        seen,
                    )
            else:
                append_go(go, tt, layers, seen)
        return layers
    except Exception:
        return []


def _layer_box_on_key(layer, canvas_size):
    """layers 子层在 key raw 画布上的 (x, y, w, h), y 向下。"""
    key_rt = layer["key"]["rt"]
    raw = layer["key"]["raw"]
    if not raw.get("x") or not raw.get("y"):
        return None
    src_cx = (0.5 - key_rt["pivot"]["x"]) * key_rt["size"]["x"]
    src_cy = (0.5 - key_rt["pivot"]["y"]) * key_rt["size"]["y"]
    lrt = layer["rt"]
    lw = lrt["size"]["x"] * lrt["scale"]["x"]
    lh = lrt["size"]["y"] * lrt["scale"]["y"]
    px = src_cx + lrt["pos"]["x"]
    py = src_cy + lrt["pos"]["y"]
    left = px - lrt["pivot"]["x"] * lw
    bottom = py - lrt["pivot"]["y"] * lh
    mapped = _key_local_to_raw(key_rt, raw, layer["key"]["delta"], left, bottom)
    if mapped is None:
        return None
    mx, my = mapped
    # 尺寸按 key 的 rect/raw 比例换到 raw 画布
    rect_w, rect_h = key_rt["size"]["x"], key_rt["size"]["y"]
    rw, rh = lw * raw["x"] / rect_w, lh * raw["y"] / rect_h
    x = mx
    y = canvas_size[1] - (my + rh)
    return (round(x), round(y), max(1, round(rw)), max(1, round(rh)))


def _composite_paint_layers(out_root, prefab_key, base_img):
    """把 prefab layers 子层叠到 key 底图上 (游戏实例化整棵 prefab 的效果)。"""
    layers = _prefab_extra_layers(out_root, prefab_key)
    if not layers:
        return base_img
    painting_dir = os.path.join(out_root, "Assets", "painting")
    out = base_img.convert("RGBA")
    for layer in layers:
        path = _layer_tex_path(painting_dir, layer["name"], prefab_key)
        if not path:
            continue
        raw = layer.get("raw") or {}
        raw_size = (raw["x"], raw["y"]) if raw.get("x") and raw.get("y") else None
        try:
            piece = restore_bundle(
                path,
                raw_size=raw_size,
                use_mesh=layer.get("use_mesh"),
            )
        except Exception:
            continue
        if piece is None:
            continue
        box = _layer_box_on_key(layer, out.size)
        if box is None:
            continue
        x, y, w, h = box
        if piece.size != (w, h):
            piece = piece.resize((w, h), Image.BILINEAR)
        # 1px 取整误差夹到现有画布, 避免无意义放大
        cx0, cy0 = max(0, x), max(0, y)
        cx1, cy1 = min(out.width, x + w), min(out.height, y + h)
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        src = piece.crop((cx0 - x, cy0 - y, cx1 - x, cy1 - y)).convert("RGBA")
        out.alpha_composite(src, (cx0, cy0))
    return out


def _face_placement(out_root, key, canvas_size, target_layer="key"):
    """由 prefab 计算 face 节点在指定主立绘画布上的位置与尺寸 (像素)。

    游戏把 face 作为 key 层的子节点; key 层按 MeshImage.OnPopulateMesh
    (meshVertex + (-pivot*rawSize + delta)) * (rect/rawSize) 反算,
    返回 rawSize 画布坐标。
    rw 层: 保留旧的世界坐标换算 (近似), 且多数 _rw 已含默认脸。
    """
    prefab = os.path.join(out_root, "Assets", "painting", key)
    if not os.path.isfile(prefab):
        return None
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
        env = UnityPy.load(prefab)
        gname = {}
        for obj in env.objects:
            if obj.type.name == "GameObject":
                try:
                    gname[obj.path_id] = obj.read().m_Name
                except Exception:
                    pass
        layer_rt = {}
        layer_raw = {}
        layer_delta = {}
        face_rt = None
        for obj in env.objects:
            tt = obj.read_typetree()
            go = tt.get("m_GameObject", {}).get("m_PathID")
            nm = gname.get(go)
            if obj.type.name == "RectTransform":
                if nm == key:
                    layer_rt["key"] = tt
                elif nm == f"{key}_rw":
                    layer_rt["rw"] = tt
                elif nm == "face":
                    face_rt = tt
            elif obj.type.name == "MonoBehaviour" and "mRawSpriteSize" in tt:
                if nm == key:
                    layer_raw["key"] = tt.get("mRawSpriteSize")
                    layer_delta["key"] = (
                        tt.get("delta_offset_x", 0.0),
                        tt.get("delta_offset_y", 0.0),
                    )
                elif nm == f"{key}_rw":
                    layer_raw["rw"] = tt.get("mRawSpriteSize")
                    layer_delta["rw"] = (
                        tt.get("delta_offset_x", 0.0),
                        tt.get("delta_offset_y", 0.0),
                    )
        if "key" not in layer_rt or face_rt is None:
            return None
        if target_layer == "rw" and "rw" not in layer_rt:
            target_layer = "key"

        def rt_info(tt):
            return {
                "pos": tt.get("m_AnchoredPosition"),
                "size": tt.get("m_SizeDelta"),
                "scale": tt.get("m_LocalScale"),
                "pivot": tt.get("m_Pivot"),
            }

        src = rt_info(layer_rt["key"])

        # face 相对 key 层 pivot 的局部坐标 (锚点 0.5)
        src_cx = (0.5 - src["pivot"]["x"]) * src["size"]["x"]
        src_cy = (0.5 - src["pivot"]["y"]) * src["size"]["y"]
        if target_layer == "key":
            raw = layer_raw.get("key")
            if not raw or not raw.get("x") or not raw.get("y"):
                return None
            rx, ry = raw["x"], raw["y"]
            # MeshImage.OnPopulateMesh:
            #   screenLocal = (meshVertex + (-pivot*raw + delta)) * (rect/raw)
            # => meshVertex = screenLocal / (rect/raw) - (-pivot*raw + delta)
            face_local = (
                src_cx + face_rt.get("m_AnchoredPosition")["x"],
                src_cy + face_rt.get("m_AnchoredPosition")["y"],
            )
            rect_w = src["size"]["x"]
            rect_h = src["size"]["y"]
            val_x = rect_w / rx
            val_y = rect_h / ry
            dx, dy = layer_delta.get("key", (0.0, 0.0))
            num_x = -src["pivot"]["x"] * rx + dx
            num_y = -src["pivot"]["y"] * ry + dy
            mx = face_local[0] / val_x - num_x
            my = face_local[1] / val_y - num_y
            py = canvas_size[1] - my  # canvas == rawSize
            fw = face_rt.get("m_SizeDelta")["x"] / val_x
            fh = face_rt.get("m_SizeDelta")["y"] / val_y
            return (round(mx - fw / 2), round(py - fh / 2)), (round(fw), round(fh))
        else:
            # rw: 近似世界坐标换算 (保留旧逻辑)
            tgt = rt_info(layer_rt["rw"])
            face_local = (
                src_cx + face_rt.get("m_AnchoredPosition")["x"],
                src_cy + face_rt.get("m_AnchoredPosition")["y"],
            )
            face_size = (
                face_rt.get("m_SizeDelta")["x"],
                face_rt.get("m_SizeDelta")["y"],
            )
            # 世界坐标: 根级 sibling, anchoredPosition 相对同一根中心
            world = (
                src["pos"]["x"] + face_local[0] * src["scale"]["x"],
                src["pos"]["y"] + face_local[1] * src["scale"]["y"],
            )
            tgt_local = (
                (world[0] - tgt["pos"]["x"]) / tgt["scale"]["x"],
                (world[1] - tgt["pos"]["y"]) / tgt["scale"]["y"],
            )
            tgt_scale = tgt["scale"]
            tgt_size = tgt["size"]
            tgt_pivot = tgt["pivot"]
            face_world_size = (
                face_size[0] * src["scale"]["x"],
                face_size[1] * src["scale"]["y"],
            )

            rect_min = (
                -tgt_pivot["x"] * tgt_size["x"],
                -tgt_pivot["y"] * tgt_size["y"],
            )
            sx = tgt_size["x"] / canvas_size[0]
            sy = tgt_size["y"] / canvas_size[1]
            px = (tgt_local[0] - rect_min[0]) / sx
            py_up = (tgt_local[1] - rect_min[1]) / sy
            py = canvas_size[1] - py_up
            fw = face_world_size[0] / tgt_scale["x"] / sx
            fh = face_world_size[1] / tgt_scale["y"] / sy
            return (round(px - fw / 2), round(py - fh / 2)), (round(fw), round(fh))
    except Exception:
        return None


def apply_prefab_faces(out_root, key, prefab_key, img, default_expr, dest_png):
    """按游戏 SetExpression 贴表情: 默认表情写入 dest, 其余写成 _表情N。

    PoolMgr.GetPainting + ShipExpressionHelper:
      DefaultFaceless = (ship_skin_expression.default ~= "")
      为真时显示 face 并贴 default; 为空则隐藏 face (默认脸已画在底图/_rw)。
    游戏把 paintingface 叠在 face 节点上, 不要求底图有矩形透明挖空:
      挖空可能已被 layers 填成背景 (daqinghuayu_idol), 或是白色占位
      (shanfeng_2), 或不规则网格洞 (shanfeng)。yunlong_3 的 face 甚至是
      全身水体贴 (1926x2048), 不是小脸补丁。
    """
    exprs = _list_face_exprs(out_root, key)
    if not exprs:
        exprs = _list_face_exprs(out_root, prefab_key)
    if not exprs:
        return img
    place = _face_placement(out_root, prefab_key, img.size, "key")
    if place is None and prefab_key != key:
        place = _face_placement(out_root, key, img.size, "key")
    if not place:
        return img
    pos, fsize = place
    # 仅当 lua default 非空且能对上 paintingface 时改主图 (与 DefaultFaceless 一致)
    main_expr = default_expr if default_expr and default_expr in exprs else None
    final = img
    for ex in exprs:
        face = _load_face_image(out_root, key, ex)
        if face is None:
            face = _load_face_image(out_root, prefab_key, ex)
        if face is None:
            continue
        fimg = face.resize(fsize) if face.size != fsize else face
        comp = img.copy()
        _paste_face(comp, fimg, pos)
        if main_expr is not None and ex == main_expr:
            comp.save(dest_png)
            final = comp
        else:
            comp.save(dest_png[: -len(".png")] + f"_表情{ex}.png")
    return final


def _paste_face(target, face, pos):
    """透明混合粘贴表情 (alpha blend)。"""
    try:
        import numpy
        from PIL import Image, ImageChops
    except ImportError:
        target.paste(face, (int(pos[0]), int(pos[1])), face)
        return target
    x, y = int(pos[0]), int(pos[1])
    w, h = face.size
    region = target.crop((x, y, x + w, y + h))
    alpha = face.getchannel("A")
    a_f = ImageChops.lighter(alpha, region.getchannel("A"))
    scale = numpy.array(alpha, dtype=float) / 255.0
    fa = numpy.array(face)
    ba = numpy.array(region)
    for i in range(3):
        ba[:, :, i] = ba[:, :, i] * (1 - scale)
        fa[:, :, i] = fa[:, :, i] * scale
    out = ba + fa
    out[:, :, 3] = numpy.array(a_f)
    target.paste(Image.fromarray(out), (x, y))
    return target


def restore_prefab_painting(out_root, prefab_key):
    """按游戏 setPaintingPrefab 还原: key 底图 + layers(含人物 _rw / _front)。"""
    painting_dir = os.path.join(out_root, "Assets", "painting")
    raw = _layer_raw_size(out_root, prefab_key)
    raw_size = (int(raw[0]), int(raw[1])) if raw else None
    layers = _prefab_extra_layers(out_root, prefab_key)
    tex_path = _prefab_key_tex_path(painting_dir, prefab_key)
    touming = _prefab_key_is_touming(out_root, prefab_key)
    img = None
    if touming:
        # key 为 touming: 画布按 rawSize, 内容全在 layers (部分背景 / 人物)
        if raw_size:
            img = Image.new("RGBA", raw_size, (0, 0, 0, 0))
    elif tex_path:
        img = restore_bundle(tex_path, raw_size=raw_size)
    if img is None and raw_size:
        img = Image.new("RGBA", raw_size, (0, 0, 0, 0))
    if img is None:
        return None
    if layers:
        img = _composite_paint_layers(out_root, prefab_key, img)
    return img


def restore_bundle(path, bust=False, raw_size=None, use_mesh=None):
    UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
    env = UnityPy.load(path)
    tex = sprite = None
    meshes = []
    for obj in env.objects:
        t = obj.type.name
        try:
            if t == "Texture2D" and tex is None:
                tex = obj.read().image
            elif t == "Mesh":
                meshes.append((obj.path_id, obj.read()))
            elif t == "Sprite" and sprite is None:
                sprite = obj.read()
        except Exception:
            continue
    mesh = None
    if meshes:
        want = 0
        if use_mesh is not False:
            _, want = _tex_prefab_mesh_info(path)
        if want:
            mesh = next((m for pid, m in meshes if pid == want), None)
        if mesh is None:
            def _vcount(item):
                try:
                    return item[1].m_VertexData.m_VertexCount
                except Exception:
                    return 0
            mesh = max(meshes, key=_vcount)[1]
    if tex is None and mesh is not None and not bust:
        # 只有 Mesh 的包: 从同名基础贴图包找 Texture2D 配对
        base = os.path.basename(path)
        if base.endswith("_tex"):
            base = base[: -len("_tex")]
        parts = base.split("_")
        for i in range(len(parts) - 1, 0, -1):
            cand = os.path.join(
                os.path.dirname(path), "_".join(parts[:i]) + "_tex"
            )
            if not os.path.isfile(cand):
                continue
            try:
                tenv = UnityPy.load(cand)
                for tobj in tenv.objects:
                    if tobj.type.name == "Texture2D":
                        tex = tobj.read().image
                        break
            except Exception:
                continue
            if tex is not None:
                break
    if tex is None:
        return None
    if bust:
        # 半身像: 直接用 Sprite 原图, 不使用包里的外部 Mesh
        # (外部 Mesh 是全屏/立绘层用的, 拼出来会碎)
        return _crop_sprite(tex, sprite)
    # prefab MeshImage.mMesh 为空: 游戏走 Image 默认四边形, 不要按包内 Mesh 拼
    prefab_mesh = (
        use_mesh if use_mesh is not None else _tex_prefab_uses_mesh(path)
    )
    if mesh is not None and prefab_mesh is not False:
        return _stitch(mesh, tex, canvas_size=raw_size)
    if (
        sprite is not None
        and _sprite_internal_vertex_count(sprite) > 4
        and prefab_mesh is not False
    ):
        return _stitch(*_decode_sprite_mesh(sprite), tex)
    return _crop_sprite(tex, sprite)


def _is_blank_image(img):
    try:
        extrema = img.getchannel("A").getextrema()
        return extrema is None or extrema[1] < 8
    except Exception:
        return False

