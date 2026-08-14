"""皮肤/舰船名映射构建 (sharecfgdata 流式字节码)。"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console

from .azl2std import convert
from .datatables import (
    _build_stream_index,
    _find_bin,
    _find_lua,
    _parse_name_code,
    _resolve_namecodes,
    parse_stream_index,
)

console = Console()

PAINTING_OUT = "Painting"
CACHE_FILE = "painting_skin_map.json"
STATE_VERSION = 6
HEADER64 = b"\x1bLJ\x02\x0a"
FOOTER = bytes(
    [24, 3, 0, 3, 0, 0, 1, 4, 75, 255, 0, 0, 44, 254, 0, 1, 37, 254, 1, 3, 50, 255, 1, 3, 0, 0]
)


def _decompile_record(rec, dec, work_dir, tag):
    """记录 -> (转换+反编译) -> 源码文本。失败返回 None。"""
    try:
        std = convert(HEADER64 + rec + FOOTER)
    except Exception:
        return None
    p = os.path.join(work_dir, f"{tag}.luac")
    with open(p, "wb") as f:
        f.write(std)
    r = subprocess.run(
        [dec, p, "-o", work_dir, "-f", "-s"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    op = os.path.join(work_dir, f"{tag}.luac.lua")
    if not os.path.exists(op):
        return None
    with open(op, encoding="utf-8", errors="replace") as f:
        return f.read()


def _field(txt, name, cast=None):
    m = re.search(rf"^\s*{name} = \"([^\"]*)\"", txt, re.M)
    if m:
        return m.group(1)
    m = re.search(rf"^\s*{name} = (-?\d+)", txt, re.M)
    if m:
        v = int(m.group(1))
        return v
    return None


def _build_skin_map(out_root, jobs=8, cache_path=None):
    """从本地 sharecfgdata 构建 skin_by_painting 与 ship_names_by_group。"""
    skin_lua = _find_lua(out_root, "ship_skin_template.lua")
    stats_lua = _find_lua(out_root, "ship_data_statistics.lua")
    pfm_lua = _find_lua(out_root, "painting_filte_map.lua")
    nc_lua = _find_lua(out_root, "name_code.lua")
    skin_bin = _find_bin(out_root, "ship_skin_template")
    stats_bin = _find_bin(out_root, "ship_data_statistics")
    missing = [
        p
        for p in (pfm_lua, nc_lua, skin_bin, stats_bin)
        if not p
    ]
    if missing:
        raise RuntimeError(f"缺少数据表: {missing}; 请先运行 batch 下载数据表")

    def md5(p):
        with open(p, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    # 二进制是按记录顺序连续存放的流, 可直接扫描重建完整索引;
    # 本地 Lua 索引可能不完整 (ship_skin_template 只有 2130 条, 二进制 2827 条)
    skin_idx = _build_stream_index(skin_bin)
    stats_idx = _build_stream_index(stats_bin)
    skin_lua_used = skin_lua if len(skin_idx) < 2500 else None
    stats_lua_used = stats_lua if len(stats_idx) < 3500 else None
    skin_idx_mode = "stream" if len(skin_idx) >= 2500 else "lua"
    stats_idx_mode = "stream" if len(stats_idx) >= 3500 else "lua"
    if len(skin_idx) < 2500:
        skin_idx = parse_stream_index(skin_lua) if skin_lua else {}
        skin_idx_mode = "lua"
        console.print("[yellow]二进制流扫描失败, 退回本地 Lua 索引[/yellow]")
        if len(skin_idx) < 2500:
            raise RuntimeError("ship_skin_template 索引不完整, 无法构建映射")
    if len(stats_idx) < 3500:
        stats_idx = parse_stream_index(stats_lua) if stats_lua else {}
        stats_idx_mode = "lua"
        console.print("[yellow]二进制流扫描失败, 退回本地 Lua 索引[/yellow]")
        if len(stats_idx) < 3500:
            raise RuntimeError("ship_data_statistics 索引不完整, 无法构建映射")

    src = {
        "skin_lua": md5(skin_lua_used) if skin_lua_used else None,
        "stats_lua": md5(stats_lua_used) if stats_lua_used else None,
        "pfm_lua": md5(pfm_lua),
        "nc_lua": md5(nc_lua) if nc_lua else None,
        "skin_bin": md5(skin_bin),
        "stats_bin": md5(stats_bin),
        "skin_idx_mode": skin_idx_mode,
        "stats_idx_mode": stats_idx_mode,
    }
    name_map = _parse_name_code(out_root)

    cache_path = cache_path or os.path.join(out_root, PAINTING_OUT, CACHE_FILE)
    if os.path.isfile(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
            if cache.get("version") == STATE_VERSION and cache.get("source") == src:
                return (
                    cache["skins"],
                    cache["ship_names"],
                    cache.get("name_map", name_map),
                )
        except Exception:
            pass

    dec = shutil.which("luajit-decompiler")
    if not dec:
        raise RuntimeError("未找到 luajit-decompiler 命令, 请通过 AUR 包安装")

    work = tempfile.mkdtemp(prefix="azl_paint_map_")

    def process(index, bin_path, tag, fields):
        data = open(bin_path, "rb").read()
        out = {}

        def one(item):
            sid, (off, ln) = item
            rec = data[off:off + ln]
            txt = _decompile_record(rec, dec, work, f"{tag}{sid}")
            if not txt:
                return sid, None
            row = {}
            for name, cast in fields.items():
                v = _field(txt, name)
                if v is not None and cast:
                    v = cast(v)
                row[name] = v
            row["id"] = sid
            return sid, row

        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for sid, row in ex.map(one, index.items()):
                if row:
                    out[sid] = row
        return out

    skins = process(
        skin_idx,
        skin_bin,
        "s",
        {
            "name": str,
            "painting": str,
            "ship_group": int,
            "group_index": int,
            "skin_type": int,
        },
    )
    stats = process(
        stats_idx,
        stats_bin,
        "d",
        {"name": str, "skin_id": int},
    )
    shutil.rmtree(work, ignore_errors=True)

    skin_by_painting = {}
    for v in skins.values():
        if v.get("painting"):
            v["name"] = _resolve_namecodes(v.get("name"), name_map)
            skin_by_painting.setdefault(v["painting"].lower(), v)

    # 舰船名: 优先 ship_data_statistics 的明文 name, 按 skin_id 关联
    name_by_skin_id = {}
    for v in stats.values():
        if v.get("skin_id") is not None and v.get("name"):
            name_by_skin_id.setdefault(v["skin_id"], v["name"])

    ship_names = {}
    for v in skins.values():
        g = v.get("ship_group")
        if g is None:
            continue
        name = name_by_skin_id.get(v["id"]) or v.get("name")
        name = _resolve_namecodes(name, name_map)
        if name:
            old = ship_names.get(str(g))
            if old is None or v.get("group_index", 0) < old[0]:
                ship_names[str(g)] = (v.get("group_index", 0), name)
    ship_names = {g: name for g, (_, name) in ship_names.items()}

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    cache = {
        "version": STATE_VERSION,
        "source": src,
        "skins": skin_by_painting,
        "ship_names": ship_names,
        "name_map": name_map,
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    return skin_by_painting, ship_names, name_map
