"""从 scripts64 中只提取需要的 sharecfg Lua 表 (arm64)。

不需要全量反编译 4 万多个脚本, 只要:
    painting_filte_map.lua     立绘 key -> 资源组
    name_code.lua              {namecode:N} 中文名
    ship_skin_expression.lua   表情差分表
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile

from rich.console import Console

import UnityPy

from .azl2std import convert
from .scipio import scipio_www


console = Console()

NEEDED_LUA = {
    "painting_filte_map.lua",
    "name_code.lua",
    "ship_skin_expression.lua",
}
STATE_FILE = "version.json"
SCRIPTS_NAME = "scripts64"
# GGET/GSET 与 tools 2026-08-18 对齐; 改转换器时递增以强制重抽
LUA_CONVERT_VER = 2


def _scripts64_path(out_root):
    return os.path.join(out_root, "Assets", SCRIPTS_NAME)


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _lua_dest(out_root, name):
    return os.path.join(
        out_root,
        "Lua",
        "assets",
        "luabuilds",
        "android",
        "arm64",
        "sharecfg",
        name,
    )


def _extract_one(script, name, out_root, work, dec):
    std = convert(script)
    stem = name.replace(".", "_")
    luac = os.path.join(work, f"{stem}.luac")
    with open(luac, "wb") as f:
        f.write(std)
    r = subprocess.run(
        [dec, luac, "-o", work, "-f", "-s"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    out_lua = os.path.join(work, f"{stem}.luac.lua")
    if not os.path.isfile(out_lua):
        raise RuntimeError(f"反编译失败 {name}: {(r.stdout + r.stderr)[-200:]}")
    dst = _lua_dest(out_root, name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(out_lua, dst)


def ensure_needed_lua(out_root, jobs=8):
    """需要时从 scripts64 解密并提取 3 个 Lua 表, 返回是否就绪。"""
    scripts = _scripts64_path(out_root)
    if not os.path.isfile(scripts):
        return False
    cur_md5 = _md5(scripts)
    state_path = os.path.join(out_root, STATE_FILE)
    state = {}
    if os.path.isfile(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            state = {}
    if (
        state.get("scripts64_md5") == cur_md5
        and state.get("lua_convert_ver") == LUA_CONVERT_VER
        and all(os.path.isfile(_lua_dest(out_root, n)) for n in NEEDED_LUA)
    ):
        return True

    dec = shutil.which("luajit-decompiler")
    if not dec:
        raise RuntimeError("未找到 luajit-decompiler 命令, 请通过 AUR 包安装")

    console.print("[cyan]解密 scripts64 并提取所需 Lua 数据表 ...[/cyan]")
    decrypted = scipio_www(open(scripts, "rb").read())
    tmp_ys = os.path.join(out_root, "Assets", SCRIPTS_NAME + ".dec_tmp")
    with open(tmp_ys, "wb") as f:
        f.write(decrypted)
    try:
        env = UnityPy.load(tmp_ys)
        found = {}
        for obj in env.objects:
            if obj.type.name != "TextAsset":
                continue
            d = obj.read()
            nm = str(getattr(d, "m_Name", "") or "")
            container = str(getattr(obj, "container", "") or "")
            if nm not in NEEDED_LUA:
                continue
            if "/arm64/sharecfg/" not in container.replace("\\", "/"):
                continue
            script = d.m_Script
            if isinstance(script, str):
                script = script.encode("utf-8", "surrogateescape")
            found[nm] = script
        if not found:
            raise RuntimeError("scripts64 中未找到所需 Lua 表")
        work = tempfile.mkdtemp(prefix="azl_lua_")
        try:
            for name in sorted(NEEDED_LUA):
                if name in found:
                    _extract_one(found[name], name, out_root, work, dec)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        try:
            os.remove(tmp_ys)
        except OSError:
            pass

    state["scripts64_md5"] = cur_md5
    state["lua_convert_ver"] = LUA_CONVERT_VER
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    console.print("[green]Lua 数据表就绪[/green]")
    return True
