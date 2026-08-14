"""sharecfg Lua 索引/表达式/namecode 解析。"""
import os
import re

from rich.console import Console

console = Console()


def _find_lua(out_root, name):
    """优先 arm64, 其次 normal。"""
    for arch in ("arm64", "normal"):
        p = os.path.join(
            out_root,
            "Lua",
            "assets",
            "luabuilds",
            "android",
            arch,
            "sharecfg",
            name,
        )
        if os.path.isfile(p):
            return p
    return None


def _find_bin(out_root, name):
    p = os.path.join(out_root, "Assets", "sharecfgdata", name)
    return p if os.path.isfile(p) else None



def parse_painting_groups(lua_path):
    """painting_filte_map.lua -> {key: [res,...]}。"""
    src = open(lua_path, encoding="utf-8").read()
    pat = re.compile(
        r'(?:\.([a-zA-Z0-9_]+)|\["([^"]+)"\]) = \{\s*key = "([^"]+)",\s*res_list = \{(.*?)\s*\}\s*\}',
        re.S,
    )
    groups = {}
    for m in pat.finditer(src):
        key = m.group(3)
        res = re.findall(r'"painting/([^"]+)"', m.group(4))
        groups[key] = res
    return groups


def parse_stream_index(lua_path, table_var="var_0"):
    """取表尾的 {id: (offset, length)} 索引。"""
    src = open(lua_path, encoding="utf-8").read()
    starts = [m.start() for m in re.finditer(rf"^\s*{table_var}\.\w+ = \{{", src, re.M)]
    if not starts:
        return {}
    seg = src[max(starts):]
    return {
        int(i): (int(o), int(l))
        for i, o, l in re.findall(r"\[(\d+)\] = \{\s*(\d+),\s*(\d+)\s*\}", seg)
    }


def _read_uleb(data, pos):
    v = 0
    sh = 0
    n = 0
    while True:
        c = data[pos]
        pos += 1
        n += 1
        v |= (c & 0x7F) << sh
        if c < 0x80:
            return v, n
        sh += 7


def _stream_record_id(rec):
    """解析一条流式记录主原型的第一个整数常量 (即该条目的 id)。"""
    pos = [0]

    def rd():
        r = 0
        sh = 0
        while True:
            c = rec[pos[0]]
            pos[0] += 1
            r |= (c & 0x7F) << sh
            if c < 0x80:
                return r
            sh += 7

    rd()  # 原型长度
    f0, f1, f2, f3 = rec[pos[0]:pos[0] + 4]
    pos[0] += 4
    nkn = rd()
    nkgc = rd()
    nbc = rd() + 1
    pos[0] += (nbc - 1) * 4  # 指令区
    nuv = f3 ^ f2 ^ (f1 ^ f0)
    pos[0] += nuv * 2

    for _ in range(nkn):
        b = rec[pos[0]]
        pos[0] += 1
        v = b >> 1
        if v >= 0x40:
            v &= 0x3F
            sh = -1
            while True:
                c = rec[pos[0]]
                pos[0] += 1
                sh += 7
                v |= (c & 0x7F) << sh
                if c < 0x80:
                    break
        if b & 1:  # number 常量, 取 low 32
            sh = 0
            while True:
                c = rec[pos[0]]
                pos[0] += 1
                if c < 0x80:
                    break
                sh += 7
        return v
    return None


def _build_stream_index(bin_path):
    """直接从 sharecfgdata 二进制扫描重建 {id: (offset, length)}。

    二进制是按记录顺序连续存放的流: 每条记录以 uleb(原型长度) 开头,
    记录字节码主原型的第一个整数常量就是该条目的 id。
    """
    data = open(bin_path, "rb").read()
    idx = {}
    off = 0
    while off < len(data):
        v, nb = _read_uleb(data, off)
        ln = v + nb
        if off + ln > len(data):
            break
        sid = _stream_record_id(data[off:off + ln])
        if sid is None:
            return {}
        idx[sid] = (off, ln)
        off += ln
    return idx


def _parse_name_code(out_root):
    """name_code.lua -> {id: 中文名}。"""
    p = _find_lua(out_root, "name_code.lua")
    if not p:
        return {}
    src = open(p, encoding="utf-8").read()
    result = {}
    for m in re.finditer(r"\[(\d+)\] = \{(.*?)\n\s*\}", src, re.S):
        body = m.group(2)
        nm = re.search(r'name = "([^"]*)"', body)
        cd = re.search(r'code = "([^"]*)"', body)
        if nm:
            result[int(m.group(1))] = nm.group(1) or (cd.group(1) if cd else "")
    return result


def _resolve_namecodes(text, name_map):
    if not text or "{namecode:" not in text:
        return text

    def rep(m):
        return name_map.get(int(m.group(1)), m.group(0))

    return re.sub(r"\{namecode:(\d+)\}", rep, text)


# --------------------------------------------------------------------------
# 流式字节码记录 -> 字段
# --------------------------------------------------------------------------


def _parse_expression_defaults(out_root):
    """ship_skin_expression.lua -> {painting: 默认表情编号}。

    只取 `default` 槽位: 这是静态展示用的表情; main_1/home 等是
    事件表情, 不合成进静态立绘。
    """
    p = _find_lua(out_root, "ship_skin_expression.lua")
    if not p:
        return {}
    src = open(p, encoding="utf-8").read()
    pat = re.compile(
        r'(?:\.([a-zA-Z0-9_]+)|\["([^"]+)"\]) = \{(.*?)\n\s*\}',
        re.S,
    )
    result = {}
    for m in pat.finditer(src):
        body = m.group(3)
        painting = re.search(r'painting = "([^"]*)"', body)
        default = re.search(r'default = "([^"]*)"', body)
        if not painting or not default or not default.group(1):
            continue
        result[painting.group(1)] = default.group(1)
    return result
