"""立绘输出命名。"""
import re

from .datatables import _resolve_namecodes



def _safe(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip(" ._")
    return name or "_"


def output_name(key, basename, skins, ship_names, name_map=None):
    v = skins.get(key.lower())
    if v:
        ship = ship_names.get(str(v.get("ship_group")))
        if not ship:
            ship = v.get("name") if v.get("group_index", 0) == 0 else key
        skin_name = v.get("name")
    else:
        ship = key
        skin_name = None
    ship = (_resolve_namecodes(ship, name_map) or "").strip()
    skin_name = (_resolve_namecodes(skin_name, name_map) or "").strip() or None
    if skin_name and skin_name != ship:
        fname = f"{ship}_{skin_name}"
    else:
        fname = ship
    kl = (key or "").lower()
    # 剧情 NPC / memory 在表里常与本舰同名, 必须带资源 key 以免盖掉正式立绘
    if kl.startswith("npc") or kl.endswith("_memory"):
        fname = f"{fname}_{key}"
    return _safe(ship), _safe(fname)
