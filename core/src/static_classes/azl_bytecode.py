"""
格式 (LuaJIT 2.1.0-beta3 魔改版):

文件头: 1B 4C 4A 02 <flags>
    flags: bit0=ENDIAN bit1=STRIP bit2=FFI bit3=FR2
    scripts32 无 FR2 (32 位格式), scripts64 带 FR2 (64 位格式)

原型块: uleb128(长度) + 块
    块头 4 字节 (XOR 编码):
        f0 = framesize
        f1 = flags ^ framesize
        f2 = numparams ^ flags
        f3 = sizeuv ^ numparams ^ flags
    计数: nkn(uleb), nkgc(uleb), nbc(uleb)+1  (注意 nkn 在前)
    指令: nbc-1 条, 每条 4 字节 [op, a, c, b]
        加载到 VM 后 (内部格式, 即标准 LuaJIT 布局):
            op' = op
            a'  = 255 - a          (A 字段补码)
            c'  = c                (C 字段原样)
            b'  = i ^ b            (B 字段与指令序号异或, i 从 0 起)
    上值: sizeuv * 2 字节
    knum: nkn 条 uleb128_33 (奇数=double: 后跟 hi uleb)
    kgc : nkgc 条:
            0   = 子原型引用
            1   = 表常量: nhash(uleb), narray(uleb),
                  narray 个数组元素, nhash 个 (key,value) 对
                  表常量 int 为 32 位无符号 uleb (负数=0xFFFFFFFF 段),
                  num 为两个普通 uleb (lo, hi)
            2/3 = 64 位整数 cdata, 4 = 复数 cdata
            >=5 = 字符串: 长度=type-5, 解密 enc[i] = ~raw[i] ^ i
"""

import struct


class BCOp:
    """游戏操作码 -> 语义"""

    # 0-5: 测试/拷贝
    ISTC = 0  # if D then A=D and JMP
    ISFC = 1  # if not D then A=D and JMP
    IST = 2  # if D then JMP
    ISF = 3  # if not D then JMP
    ISEQV = 4
    ISNEV = 5
    # 6-11: 常量
    KSTR = 6
    KCDATA = 7
    KSHORT = 8
    KNUM = 9
    KPRI = 10
    KNIL = 11
    # 12-17: 循环
    FORI = 12
    JFORI = 13
    FORL = 14
    IFORL = 15
    JFORL = 16
    ITERL = 17
    # 18-23: 比较 (含元方法回退)
    ISEQN = 22
    ISNEN = 23
    ISLT = 24
    ISGE = 25
    ISLE = 26
    ISGT = 27
    ISEQS = 28
    ISNES = 29
    ISEQP = 30
    ISNEP = 31
    # 36-43: 调用/变参/返回
    CALLM = 36
    CALL = 37
    CALLMT = 38
    CALLT = 39
    ITERC = 40
    VARG = 41
    RETM = 42
    ISNEXT = 43
    # 44-51: 一元/移动/返回
    MOV = 44
    NOT = 45
    UNM = 46
    LEN = 47
    RET0 = 48
    RET = 49
    RETM2 = 50
    RET1 = 51
    # 52-67: 算术
    ADDVN = 52
    SUBVN = 53
    MULVN = 54
    DIVVN = 55
    MODVN = 56
    ADDNV = 57
    SUBNV = 58
    MULNV = 59
    DIVNV = 60
    MODNV = 61
    ADDVV = 62
    SUBVV = 63
    MULVV = 64
    DIVVV = 65
    MODVV = 66
    POW = 67
    CAT = 68
    # 69-78: 上值/函数/表/全局
    UGET = 69
    USETV = 70
    USETN = 71
    USETP = 72
    UCLO = 73
    FNEW = 74
    TNEW = 75
    TDUP = 76
    GGET = 77
    GSET = 78
    # 79-87: 表读写
    TGETB = 79
    TGETR = 80
    TGETV = 81
    TGETS = 82
    TSETS = 83
    TSETV = 84
    TSETM = 85
    TSETB = 86
    TSETR = 87
    JMP = 88
    LOOP = 90
    FUNCF = 93
    FUNCV = 95
    FUNCC = 97
    FUNCCW = 98


def _uleb(data, pos):
    r = 0
    sh = 0
    while True:
        c = data[pos[0]]
        pos[0] += 1
        r |= (c & 0x7F) << sh
        if c < 0x80:
            return r
        sh += 7


def _uleb33(data, pos):
    b = data[pos[0]]
    pos[0] += 1
    v = b >> 1
    if v >= 0x40:
        v &= 0x3F
        sh = -1
        while True:
            c = data[pos[0]]
            pos[0] += 1
            sh += 7
            v |= (c & 0x7F) << sh
            if c < 0x80:
                break
    return v


def _dec_str(raw):
    return bytes((~b ^ i) & 0xFF for i, b in enumerate(raw))


class Proto:
    __slots__ = (
        "flags",
        "params",
        "framesize",
        "sizeuv",
        "nkn",
        "nkgc",
        "nbc",
        "ins",
        "uv",
        "kn",
        "kgc",
        "offset",
    )

    def __init__(self):
        self.ins = []
        self.uv = []
        self.kn = []
        self.kgc = []


def parse(data):
    """解析完整 TextAsset 字节码, 返回根 Proto 列表 (含子原型)。

    返回 (protos, end): protos 为按文件顺序的原型 (子原型先于父原型),
    每个 Proto.kgc 中 'child' 指向对应的 Proto 对象。
    """
    if len(data) < 6 or data[:3] != b"\x1bLJ" or data[3] != 2:
        raise ValueError("not a luajit bytecode")
    flags = data[4]
    pos = [5]
    protos = []

    def read_ktabk():
        k = _uleb(data, pos)
        if k >= 5:
            raw = data[pos[0] : pos[0] + k - 5]
            pos[0] += k - 5
            return ("str", _dec_str(raw).decode("utf-8", "replace"))
        if k == 3:
            # 表常量整数按 32 位无符号 uleb 存储, 高位为 1 表示负数
            v = _uleb(data, pos)
            if v >= 0x80000000:
                v -= 0x100000000
            return ("int", v)
        if k == 4:
            lo = _uleb(data, pos)
            hi = _uleb(data, pos)
            return ("num", lo, hi)
        return ("pri", k)

    def read_kgc():
        t = _uleb(data, pos)
        if t == 0:
            return ("child", None)  # 由调用者回填
        if t == 1:
            nh = _uleb(data, pos)
            na = _uleb(data, pos)
            h = [[read_ktabk(), read_ktabk()] for _ in range(nh)]
            a = [read_ktabk() for _ in range(na)]
            return ("tab", {"hash": h, "array": a})
        if t >= 5:
            raw = data[pos[0] : pos[0] + t - 5]
            pos[0] += t - 5
            return ("str", _dec_str(raw).decode("utf-8", "replace"))
        # cdata: 2=int64 3=uint64 4=complex
        n = 4 if t == 4 else 2
        c = [_uleb(data, pos) for _ in range(n)]
        return ("cdata", t, c)

    while pos[0] < len(data) and data[pos[0]] != 0:
        ln = _uleb(data, pos)
        start = pos[0]
        end = start + ln
        f0 = data[pos[0]]
        f1 = data[pos[0] + 1]
        f2 = data[pos[0] + 2]
        f3 = data[pos[0] + 3]
        pos[0] += 4
        p = Proto()
        p.flags = f0 ^ f1
        p.params = f2 ^ p.flags
        p.framesize = f0
        p.sizeuv = f3 ^ f2 ^ p.flags
        p.nkn = _uleb(data, pos)
        p.nkgc = _uleb(data, pos)
        p.nbc = _uleb(data, pos) + 1
        p.offset = start
        for i in range(p.nbc - 1):
            op, a, c, b = data[pos[0] : pos[0] + 4]
            pos[0] += 4
            # 还原为 VM 内部 (标准 LuaJIT) 指令
            p.ins.append((op, (255 - a) & 0xFF, c, (i ^ b) & 0xFF))
        p.uv = list(
            struct.unpack("<%dH" % p.sizeuv, data[pos[0] : pos[0] + 2 * p.sizeuv])
        )
        pos[0] += 2 * p.sizeuv
        for _ in range(p.nkn):
            b = data[pos[0]]
            v = _uleb33(data, pos)
            if b & 1:
                hi = _uleb(data, pos)
                p.kn.append({"num": (v, hi)})
            else:
                p.kn.append({"int": v})
        p.kgc = [read_kgc() for _ in range(p.nkgc)]
        protos.append(p)
        if pos[0] != end:
            # 允许容错: 只警告, 不中断
            pass
        pos[0] = end

    # 回填 child 引用: kgc 中 'child' 按文件顺序指向先出现的子原型
    by_off = {p.offset: p for p in protos}
    order = [p for p in protos]  # 文件顺序
    used = [False] * len(order)
    for p in protos:
        idx = [i for i, q in enumerate(order) if q.offset == p.offset]
        # 每个 child 条目取下一个未消费的原型
        for j, k in enumerate(p.kgc):
            if k[0] == "child":
                for oi, q in enumerate(order):
                    if not used[oi] and q.offset < p.offset:
                        p.kgc[j] = ("child", q)
                        used[oi] = True
                        break
    return protos


def dump_proto(p, indent=0):
    pad = "  " * indent
    print(
        f"{pad}proto flags={p.flags} params={p.params} fsz={p.framesize} "
        f"uv={p.sizeuv} nkn={p.nkn} nkgc={p.nkgc} nbc={p.nbc}"
    )
    for i, ins in enumerate(p.ins[:12]):
        print(f"{pad}  {i}: {ins}")
    if len(p.ins) > 12:
        print(f"{pad}  ... ({len(p.ins)} instructions)")
    for k in p.kgc:
        if k[0] == "child":
            dump_proto(k[1], indent + 1)


if __name__ == "__main__":
    import sys

    protos = parse(open(sys.argv[1], "rb").read())
    print(f"protos: {len(protos)}")
    for p in protos:
        dump_proto(p)
