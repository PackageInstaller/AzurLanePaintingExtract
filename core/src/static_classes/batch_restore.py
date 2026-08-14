"""
碧蓝航线立绘批量还原编排入口。

拆分说明:
    datatables.py         sharecfg Lua 索引/表达式/namecode 解析
    skin_map.py           皮肤/舰船名映射构建 (sharecfgdata 流式字节码)
    painting_assets.py    Unity AssetBundle 还原与表情贴图
    naming.py             输出命名
"""
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
    _bust_uses_mesh,
    _face_placement,
    _layer_raw_size,
    _list_face_exprs,
    _load_face_image,
    _paste_face,
    UNITY_FALLBACK,
    restore_bundle,
)
from .skin_map import PAINTING_OUT, _build_skin_map

console = Console()

def run(out_root, jobs=8, limit=None, sync=True, full=False):
    """同步并还原立绘: 默认先检查更新, 只转换有更新的 *_tex。"""
    UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK

    updated = None
    if sync:
        try:
            from . import downloader

            updated = downloader.sync_assets(out_root, jobs=jobs)
        except Exception as e:
            console.print(f"[yellow]立绘下载失败({e}), 使用本地资产[/yellow]")
    if updated is not None and not full and not updated:
        console.print("[green]立绘无更新, 跳过转换[/green]")
        return {
            "total": 0,
            "ok": 0,
            "fail": 0,
            "skip": 0,
            "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "updated": [],
            "errors": {},
            "skipped": [],
        }

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
    if updated is not None and not full:
        up = set(updated)
        files = [f for f in files if f in up]
        console.print(f"[cyan]仅转换更新立绘 {len(files)} 个[/cyan]")
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
    if limit:
        files = files[:limit]

    # 每个 key 的主立绘: 有 _rw 时优先取 _rw (全身人物立绘)
    files_by_key = {}
    for f in files:
        key = res2key[f]
        files_by_key.setdefault(key, []).append(f[: -len("_tex")])
    main_src = {}
    for key, names in files_by_key.items():
        main_src[key] = f"{key}_rw" if f"{key}_rw" in names else key

    # 同一 船名+皮肤名 的主 key (最短/无替代后缀): 重名时保留无后缀名
    main_groups = {}
    for f in files:
        key = res2key[f]
        main = main_src.get(key, key)
        ship, name = output_name(key, main, skins, ship_names, name_map)
        main_groups.setdefault((ship, name), []).append(key)
    canonical_key = {
        grp: min(keys, key=lambda k: (len(k), k))
        for grp, keys in main_groups.items()
    }
    key_canonical = {}
    for grp, keys in main_groups.items():
        ck = canonical_key[grp]
        for k in keys:
            key_canonical[k] = ck

    # 预计算输出路径; 不同 key 解析出相同 船名+皮肤名 时追加 key 区分
    planned = {}
    dup_count = {}
    for f in files:
        base = f[: -len("_tex")]
        key = res2key[f]
        main = main_src.get(key, key)
        ship, name = output_name(key, base, skins, ship_names, name_map)
        if base == key and main != key:
            variant = "半身"
        elif base == main:
            variant = ""
        else:
            variant = (
                base[len(key):].lstrip("_")
                if base.lower().startswith(key.lower())
                else base
            )
        if variant:
            name = f"{name}_{variant}"
        rel = os.path.join(ship, f"碧蓝航线_{name}.png")
        planned[f] = rel
        dup_count[rel] = dup_count.get(rel, 0) + 1
    used = set()
    for f in files:
        rel = planned[f]
        if dup_count[rel] > 1:
            key = res2key[f]
            if key_canonical.get(key) != key:
                stem, ext = os.path.splitext(rel)
                cand = f"{stem}_{key}{ext}"
                n = 2
                while cand in used:
                    cand = f"{stem}_{key}_{n}{ext}"
                    n += 1
                planned[f] = cand
        used.add(planned[f])

    out_dir = os.path.join(out_root, PAINTING_OUT)
    os.makedirs(out_dir, exist_ok=True)
    console.print(f"[cyan]待还原 {len(files)} 个立绘 -> {out_dir}[/cyan]")
    expr_defaults = _parse_expression_defaults(out_root)

    ok = fail = skip = 0
    errors = {}
    skipped = []

    def one(fname):
        base = fname[: -len("_tex")]
        key = res2key[fname]
        ship, name = output_name(key, base, skins, ship_names, name_map)
        main = main_src.get(key, key)
        bust = False
        if base == key and main != key:
            # 主立绘用 _rw, 无后缀的基础包是半身像, 单独输出 _半身
            variant = "半身"
            bust = True
        elif base == main:
            variant = ""
        else:
            variant = (
                base[len(key):].lstrip("_")
                if base.lower().startswith(key.lower())
                else base
            )
        if variant:
            name = f"{name}_{variant}"
        rel = planned[fname]
        dst = os.path.join(out_dir, rel)
        try:
            img = restore_bundle(
                os.path.join(painting_dir, fname),
                bust=bust and not _bust_uses_mesh(out_root, key),
            )
            if img is None:
                return fname, None, "SKIP"
            if variant == "" and expr_defaults.get(key) and not main.endswith("_rw"):
                # key 层按 MeshImage 公式贴脸: 画布必须用 mRawSpriteSize,
                # 否则裁掉顶部留白会导致脸错位 (如 xipeier_idolns)
                raw = _layer_raw_size(out_root, key)
                if raw:
                    img = restore_bundle(
                        os.path.join(painting_dir, fname),
                        raw_size=raw,
                    )
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            img.save(dst)
            if variant == "":
                default = expr_defaults.get(key)
                if default:
                    exprs = _list_face_exprs(out_root, key)
                else:
                    exprs = []
                if exprs:
                    target_layer = "rw" if main.endswith("_rw") else "key"
                    place = _face_placement(out_root, key, img.size, target_layer)
                    if place:
                        pos, fsize = place
                        for ex in exprs:
                            face = _load_face_image(out_root, key, ex)
                            if face is None:
                                continue
                            fimg = face.resize(fsize) if face.size != fsize else face
                            comp = img.copy()
                            _paste_face(comp, fimg, pos)
                            if default and ex == default:
                                comp.save(dst)  # 主立绘合成默认表情
                            else:
                                comp.save(dst[: -len(".png")] + f"_表情{ex}.png")
            return fname, rel, None
        except Exception as e:
            return fname, None, f"{type(e).__name__}: {e}"

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("还原立绘", total=len(files))
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(one, f) for f in files]
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
