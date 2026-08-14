"""Unity AssetBundle 立绘还原与表情贴图。"""
import os
import struct

import UnityPy
from PIL import Image



UNITY_FALLBACK = "2022.3.62f3"
HEADER64 = b"\x1bLJ\x02\x0a"
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


def _bust_uses_mesh(out_root, key):
    """半身节点是否引用外部 Mesh: 由 painting/<key> prefab 决定。

    部分皮肤的半身层是 MeshImage + 外部 Mesh (如 huangjiacaifu),
    部分是纯 Sprite (如 aierbin_3, mMesh=0)。
    """
    prefab = os.path.join(out_root, "Assets", "painting", key)
    if not os.path.isfile(prefab):
        return False
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
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            tt = obj.read_typetree()
            go = tt.get("m_GameObject", {}).get("m_PathID")
            if (name.get(go) or "").lower() != key.lower():
                continue
            mesh = tt.get("mMesh") or {}
            if mesh.get("m_PathID") and mesh.get("m_FileID"):
                return True
    except Exception:
        pass
    return False


def _layer_raw_size(out_root, key):
    """prefab 中 key 层 MeshImage 的 mRawSpriteSize。"""
    prefab = os.path.join(out_root, "Assets", "painting", key)
    if not os.path.isfile(prefab):
        return None
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
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            tt = obj.read_typetree()
            go = tt.get("m_GameObject", {}).get("m_PathID")
            if (name.get(go) or "").lower() != key.lower():
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
    return sorted(names, key=lambda x: (len(x), x))


def _face_placement(out_root, key, canvas_size, target_layer="key"):
    """由 prefab 计算 face 节点在指定主立绘画布上的位置与尺寸 (像素)。

    key 层: 按 MeshImage.OnPopulateMesh 的精确公式
    (meshVertex + (-pivot*rawSize + delta)) * (rect/rawSize) 反算,
    返回 rawSize 画布坐标。
    rw 层: 保留旧的世界坐标换算 (近似)。
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


def restore_bundle(path, bust=False, raw_size=None):
    UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK
    env = UnityPy.load(path)
    tex = mesh = sprite = None
    for obj in env.objects:
        t = obj.type.name
        try:
            if t == "Texture2D" and tex is None:
                tex = obj.read().image
            elif t == "Mesh" and mesh is None:
                mesh = obj.read()
            elif t == "Sprite" and sprite is None:
                sprite = obj.read()
        except Exception:
            continue
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
    if mesh is not None:
        return _stitch(mesh, tex, canvas_size=raw_size)
    if sprite is not None and _sprite_internal_vertex_count(sprite) > 4:
        return _stitch(*_decode_sprite_mesh(sprite), tex)
    return _crop_sprite(tex, sprite)


# --------------------------------------------------------------------------
# 命名
# --------------------------------------------------------------------------


