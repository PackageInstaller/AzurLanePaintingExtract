"""
碧蓝航线魔改 LuaJIT 字节码 -> 标准 LuaJIT 2.1 dump 转换器

供 luajit-decompiler 直接反编译。转换内容:
  - 头: XOR 编码 -> 明文 (flags/params/framesize/sizeuv)
  - 计数: nkn/nkgc 交换为标准 nkgc/nkn 顺序
  - 指令: 还原 a=255-a, b=i^b, 并将游戏操作码重映射为标准编号
  - 字符串常量: enc[i]=~raw[i]^i -> 明文
  - 表常量: nhash/narray 交换为标准 narray/nhash 顺序

注意:
  - CALL/CALLM 的字段与标准一致: C(byte2)=参数数+1, B(byte3)=结果数+1
    (B=0 为多返回值)。运行时 VM 忽略这两个字段, 仅编译期元数据。

用法:
    python3 azl2std.py <game.lua> <out.luac>
    python3 azl2std.py <game.lua>            # 输出同名 .luac
"""

import sys

from .azl_bytecode import parse

# 游戏操作码 -> 标准 LuaJIT 2.1 (beta3) 编号
# (标准编号按 luajit-decompiler 的 bytecode/instructions.h 枚举)
OPMAP = {
    0: 12,    # ISTC
    1: 13,    # ISFC
    2: 14,    # IST
    3: 15,    # ISF
    4: 4,     # ISEQV
    5: 5,     # ISNEV
    6: 39,    # KSTR
    7: 40,    # KCDATA
    8: 41,    # KSHORT
    9: 42,    # KNUM
    10: 43,   # KPRI
    11: 44,   # KNIL
    12: 77,   # FORI
    13: 77,   # JFORI -> FORI (反编译器不支持 JIT 变体)
    14: 79,   # FORL
    15: 79,   # IFORL -> FORL
    16: 79,   # JFORL -> FORL
    17: 82,   # ITERL
    18: 82,   # IITERL -> ITERL
    19: 82,   # JITERL -> ITERL
    20: 85,   # ILOOP -> LOOP
    21: 85,   # JLOOP -> LOOP
    22: 4,    # ISEQV (带元方法, 罕见)
    23: 88,   # 跳转载体 (紧随条件指令, 携带跳转目标; 相当于标准 JMP)
    24: 0,    # ISLT
    25: 1,    # ISGE
    26: 2,    # ISLE
    27: 3,    # ISGT
    28: 4,    # ISEQV (数值快速路径)
    29: 5,    # ISNEV (数值快速路径)
    30: 6,    # ISEQS (字符串常量)
    31: 7,    # ISNES (字符串常量)
    32: 8,    # ISEQN (knum 常量)
    33: 9,    # ISNEN (knum 常量)
    34: 10,   # ISEQP (原始常量)
    35: 11,   # ISNEP (原始常量)
    36: 65,   # CALLM
    37: 66,   # CALL
    38: 67,   # CALLMT
    39: 68,   # CALLT
    40: 69,   # ITERC
    41: 71,   # VARG
    42: 71,   # VARG (变参拷贝, 含 multres 形式)
    43: 72,   # ISNEXT
    44: 18,   # MOV
    45: 19,   # NOT
    46: 20,   # UNM
    47: 21,   # LEN
    48: 75,   # RET0
    49: 74,   # RET (返回 A..A+D-2, 与标准 RET 的 D-1 个一致)
    50: 74,   # 游戏 RETM 语义为 D-1 个返回值+补 nil, 等价标准 RET
    51: 76,   # RET1
    52: 22,   # ADDVN
    53: 23,   # SUBVN
    54: 24,   # MULVN
    55: 25,   # DIVVN
    56: 26,   # MODVN
    57: 27,   # ADDNV
    58: 28,   # SUBNV
    59: 29,   # MULNV
    60: 30,   # DIVNV
    61: 31,   # MODNV
    62: 32,   # ADDVV
    63: 33,   # SUBVV
    64: 34,   # MULVV
    65: 35,   # DIVVV
    66: 36,   # MODVV
    67: 37,   # POW
    68: 38,   # CAT
    69: 45,   # UGET
    70: 46,   # USETV
    71: 47,   # USETS
    72: 48,   # USETN
    73: 49,   # USETP
    74: 50,   # UCLO
    75: 51,   # FNEW
    76: 52,   # TNEW
    77: 53,   # TDUP
    78: 55,   # GSET
    79: 54,   # GGET
    80: 56,   # TGETV
    81: 57,   # TGETS
    82: 58,   # TGETB
    83: 59,   # TGETR
    84: 59,   # TGETR (未用)
    85: 60,   # TSETV
    86: 61,   # TSETS
    87: 62,   # TSETB
    88: 63,   # TSETM
    89: 64,   # TSETR (未用)
    90: 85,   # LOOP
    91: 86,   # ILOOP
    92: 87,   # JLOOP
    93: 96,   # FUNCF
    94: 97,   # IFUNCF
    95: 99,   # FUNCV
    96: 100,  # IFUNCV
    97: 102,  # FUNCC
    98: 103,  # FUNCCW
}

# 标准 ABC 格式指令 (与 decompiler 的 is_op_abc_format 一致)
ABC_OPS = {
    22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38,
    56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 69, 70,
}


def uleb(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def uleb33(v):
    """33 位有符号整数 -> uleb128_33 (最低位为 0 的 int)。"""
    out = bytearray()
    b = (2 * v) & 0xFF
    if b >= 0x40:
        b = (b & 0x7F) | 0x80
        out.append(b)
        v >>= 6
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)
    return bytes([b])


def ktabk(v):
    t = v[0]
    if t == "str":
        s = v[1].encode("utf-8", "surrogateescape")
        return uleb(5 + len(s)) + s
    if t == "int":
        return uleb(3) + uleb(v[1] & 0xFFFFFFFF)
    if t == "num":
        return uleb(4) + uleb(v[1]) + uleb(v[2])
    return uleb(v[1])  # pri


def convert(data):
    protos = parse(data)
    order = protos  # 子原型先于父原型, 与标准格式一致
    out = bytearray()
    # version 2; STRIP 始终设置, 源为 FR2 (64 位) 时保留该标志
    src_flags = data[4]
    out_flags = 0x02 | (src_flags & 0x08)
    out += b"\x1bLJ\x02" + bytes([out_flags])
    for p in order:
        body = bytearray()
        body += bytes([p.flags & 7, p.params, p.framesize, p.sizeuv])
        body += uleb(p.nkgc)
        body += uleb(p.nkn)
        body += uleb(len(p.ins))
        for op, a, c, b in p.ins:
            sop = OPMAP.get(op, op)
            body += bytes([sop, a])
            if sop in ABC_OPS:
                body += bytes([c, b])
            else:
                d = c | (b << 8)
                body += bytes([d & 0xFF, (d >> 8) & 0xFF])
        for x in p.uv:
            body += bytes([x & 0xFF, (x >> 8) & 0xFF])
        # kgc: 标准顺序, 表常量 narray 在前
        for k in p.kgc:
            if k[0] == "child":
                body += uleb(0)
            elif k[0] == "tab":
                body += uleb(1)
                body += uleb(len(k[1]["array"]))
                body += uleb(len(k[1]["hash"]))
                for v in k[1]["array"]:
                    body += ktabk(v)
                for kk, vv in k[1]["hash"]:
                    body += ktabk(kk) + ktabk(vv)
            elif k[0] == "str":
                s = k[1].encode("utf-8", "surrogateescape")
                body += uleb(5 + len(s)) + s
            elif k[0] == "cdata":
                # k = ("cdata", t, [lo, hi, ...]); 标准: 2=int64 3=uint64 4=complex
                t = k[1]
                body += uleb(t)
                for v in k[2]:
                    body += uleb(v)
            else:
                raise ValueError("unsupported kgc: %r" % (k,))
        # knum
        for n in p.kn:
            if "num" in n:
                lo, hi = n["num"]
                body += uleb(1 + 2 * lo) + uleb(hi)
            else:
                body += uleb33(n["int"])
        out += uleb(len(body)) + body
    out += b"\x00"
    return bytes(out)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src.rsplit(".", 1)[0] + ".luac"
    data = open(src, "rb").read()
    open(dst, "wb").write(convert(data))
    print(f"{src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
