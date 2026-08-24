"""
碧蓝航线立绘批量还原编排入口。

拆分说明:
    datatables.py         sharecfg Lua 索引/表达式/namecode 解析
    skin_map.py           皮肤/舰船名映射构建 (sharecfgdata 流式字节码)
    painting_assets.py    Unity AssetBundle 还原与表情贴图
    naming.py             输出命名
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import UnityPy
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .datatables import _find_lua, _parse_expression_defaults, parse_painting_groups
from .lua_extract import ensure_needed_lua
from .naming import output_name
from .painting_assets import (
    _SKIP_TEX_SUFFIXES,
    _layer_tex_path,
    _is_blank_image,
    apply_prefab_faces,
    _prefab_extra_layers,
    _prefab_key_tex_path,
    UNITY_FALLBACK,
    restore_prefab_painting,
)
from .skin_map import PAINTING_OUT, _build_skin_map

console = Console()

def _name_tokens(name):
    return name.split("_")


def _is_shophx_name(name):
    tokens = _name_tokens(name)
    return "shophx" in tokens or ("shop" in tokens and "hx" in tokens)


def _is_hx_name(name):
    """资源名是否含和谐后缀 (_hx / _rw_hx / _n_hx 等), 不含 shop_hx。"""
    if _is_shophx_name(name):
        return False
    return "hx" in _name_tokens(name)


def _is_layer_only_name(name):
    """front/rw/middle 等只作为 prefab 子层, 不单独出图。"""
    tokens = _name_tokens(name)
    for suf in _SKIP_TEX_SUFFIXES:
        parts = suf.split("_")
        if tokens[-len(parts) :] == parts:
            return True
    return False


def _job_sources(out_root, prefab_key):
    """该 prefab 还原会读到的 *_tex, 用于增量判断。"""
    painting_dir = os.path.join(out_root, "Assets", "painting")
    out = []
    key_tex = _prefab_key_tex_path(painting_dir, prefab_key)
    if key_tex:
        out.append(os.path.basename(key_tex))
    for layer in _prefab_extra_layers(out_root, prefab_key):
        path = _layer_tex_path(painting_dir, layer["name"], prefab_key)
        if path:
            name = os.path.basename(path)
            if name not in out:
                out.append(name)
    return out


def run(out_root, jobs=8, limit=None, sync=True, full=False, include_hx=False):
    """同步并还原立绘: 默认先检查更新, 只转换有更新的立绘。

    include_hx: 为 True 时额外导出 _hx 和谐立绘; 默认跳过。
    """
    UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK

    updated = None
    if sync:
        try:
            from . import downloader

            updated = downloader.sync_assets(out_root, jobs=jobs)
        except Exception as e:
            console.print(f"[yellow]立绘下载失败({e}), 使用本地资产[/yellow]")
    if not ensure_needed_lua(out_root, jobs=jobs):
        raise RuntimeError("缺少 scripts64, 请先运行 batch 下载数据表")
    pfm_lua = _find_lua(out_root, "painting_filte_map.lua")
    if not pfm_lua:
        raise RuntimeError("缺少 painting_filte_map.lua, 请先下载并反编译数据表")
    groups = parse_painting_groups(pfm_lua)
    res2key = {}
    for key, res in groups.items():
        for r in res:
            if r.endswith("_tex"):
                res2key[r] = key

    console.print("[cyan]构建皮肤/舰船名映射 ...[/cyan]")
    skins, ship_names, name_map = _build_skin_map(out_root, jobs=jobs)
    matched = sum(1 for k in groups if k in skins)
    console.print(
        f"[green]映射完成: {len(skins)} 个皮肤 key, 覆盖 {matched}/{len(groups)} 个立绘组[/green]"
    )
    if matched < len(groups) * 0.5:
        console.print(
            "[yellow]覆盖偏低, 本地数据表可能过期; 建议先完整更新 Assets 与 Lua[/yellow]"
        )

    painting_dir = os.path.join(out_root, "Assets", "painting")
    if not os.path.isdir(painting_dir):
        raise RuntimeError(f"缺少立绘目录: {painting_dir}")
    files = sorted(
        f
        for f in os.listdir(painting_dir)
        if f.endswith("_tex") and f in res2key and "shadow" not in f
    )
    if not files:
        console.print("[green]没有需要转换的立绘[/green]")
        return {
            "total": 0,
            "ok": 0,
            "fail": 0,
            "skip": 0,
            "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "updated": list(updated or []),
            "errors": {},
            "skipped": [],
        }
    # 按皮肤 key 出图: 全背景一张, 有 _n prefab 再出部分背景一张
    skin_keys = []
    seen_key = set()
    for f in files:
        key = res2key[f]
        if key in seen_key:
            continue
        if (
            _is_layer_only_name(key)
            or _is_shophx_name(key)
            or (_is_hx_name(key) and not include_hx)
        ):
            continue
        seen_key.add(key)
        skin_keys.append(key)

    paint_jobs = []
    for key in skin_keys:
        paint_jobs.append((key, key, ""))
        if os.path.isfile(os.path.join(painting_dir, key + "_n")):
            paint_jobs.append((key, key + "_n", "n"))
        if include_hx:
            if os.path.isfile(os.path.join(painting_dir, key + "_hx")):
                paint_jobs.append((key, key + "_hx", "hx"))
            if os.path.isfile(os.path.join(painting_dir, key + "_n_hx")):
                paint_jobs.append((key, key + "_n_hx", "n_hx"))

    main_groups = {}
    for key, prefab_key, variant in paint_jobs:
        ship, name = output_name(key, key, skins, ship_names, name_map)
        main_groups.setdefault((ship, name, variant), []).append(key)
    canonical_key = {
        grp: min(keys, key=lambda k: (len(k), k))
        for grp, keys in main_groups.items()
    }

    planned = {}
    used = set()
    for key, prefab_key, variant in paint_jobs:
        ship, name = output_name(key, key, skins, ship_names, name_map)
        if variant:
            name = f"{name}_{variant}"
        rel = os.path.join(ship, f"碧蓝航线_{name}.png")
        grp = (
            output_name(key, key, skins, ship_names, name_map)[0],
            output_name(key, key, skins, ship_names, name_map)[1],
            variant,
        )
        if canonical_key.get(grp) != key:
            stem, ext = os.path.splitext(rel)
            cand = f"{stem}_{key}{ext}"
            n = 2
            while cand in used:
                cand = f"{stem}_{key}_{n}{ext}"
                n += 1
            rel = cand
        planned[(key, prefab_key, variant)] = rel
        used.add(rel)

    out_dir = os.path.join(out_root, PAINTING_OUT)
    os.makedirs(out_dir, exist_ok=True)
    expr_defaults = _parse_expression_defaults(out_root)

    state_path = os.path.join(out_dir, "painting_state.json")
    state = {}
    if os.path.isfile(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            state = {}
    up = set(updated or [])
    if not full:
        need = []
        for job in paint_jobs:
            key, prefab_key, variant = job
            dst = os.path.join(out_dir, planned[job])
            srcs = _job_sources(out_root, prefab_key)
            if (
                prefab_key not in state
                or not os.path.exists(dst)
                or any(s in up for s in srcs)
            ):
                need.append(job)
        paint_jobs = need
        console.print(
            f"[cyan]待转换 {len(paint_jobs)} 张立绘 (含资产更新/未转换/输出缺失)[/cyan]"
        )
    if limit:
        paint_jobs = paint_jobs[:limit]
    if not paint_jobs:
        console.print("[green]立绘无更新, 跳过转换[/green]")
        return {
            "total": 0,
            "ok": 0,
            "fail": 0,
            "skip": 0,
            "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "updated": list(updated or []),
            "errors": {},
            "skipped": [],
        }
    console.print(f"[cyan]待还原 {len(paint_jobs)} 张立绘 -> {out_dir}[/cyan]")

    ok = fail = skip = 0
    errors = {}
    skipped = []

    def one(job):
        key, prefab_key, variant = job
        rel = planned[job]
        dst = os.path.join(out_dir, rel)
        try:
            img = restore_prefab_painting(out_root, prefab_key)
            if img is None or _is_blank_image(img):
                return prefab_key, None, "SKIP"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            img.save(dst)
            default = expr_defaults.get(key) or expr_defaults.get(prefab_key)
            apply_prefab_faces(out_root, key, prefab_key, img, default, dst)
            return prefab_key, rel, None
        except Exception as e:
            return prefab_key, None, f"{type(e).__name__}: {e}"

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("还原立绘", total=len(paint_jobs))
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(one, job) for job in paint_jobs]
            for fut in as_completed(futs):
                fname, rel, err = fut.result()
                progress.advance(task)
                if err:
                    if err == "SKIP":
                        skip += 1
                        skipped.append(fname)
                    else:
                        fail += 1
                        errors[fname] = err
                        console.print(f"[red]失败 {fname}: {err}[/red]")
                else:
                    ok += 1
                    state[fname] = True

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    summary = {
        "total": len(files),
        "ok": ok,
        "fail": fail,
        "skip": skip,
        "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "errors": errors,
        "skipped": skipped,
    }
    with open(os.path.join(out_dir, "painting_result.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    console.print(f"[green]立绘还原完成: 成功 {ok}, 跳过 {skip}, 失败 {fail}[/green]")
    if skipped:
        console.print(f"[yellow]跳过 {len(skipped)} 个无贴图/动画包:[/yellow]")
        for fname in skipped[:20]:
            console.print(f"[yellow]  {fname}[/yellow]")
    if errors:
        for fname in list(errors)[:10]:
            console.print(f"[red]  {fname}: {errors[fname]}[/red]")
    return summary
