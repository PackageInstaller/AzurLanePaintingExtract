"""立绘输出命名。"""
import re

from .datatables import _resolve_namecodes



def _safe(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.rstrip(" .")
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
    ship = _resolve_namecodes(ship, name_map)
    skin_name = _resolve_namecodes(skin_name, name_map)
    if skin_name and skin_name != ship:
        fname = f"{ship}_{skin_name}"
    else:
        fname = ship
    return _safe(ship), _safe(fname)
