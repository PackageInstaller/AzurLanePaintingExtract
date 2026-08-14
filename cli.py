#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AzurLanePaintingExtract CLI

子命令:
    batch [--out DIR] [--jobs N] [--limit N]
        批量还原 Assets/painting 全部立绘并重命名
        (自动把默认表情合成进主立绘)
    restore TEX_PATH [--bust] [--out PNG]
        还原单个 *_tex 包 (Mesh 拼接 / Sprite 原图)

示例:
    python3 cli.py batch --out /home/rikka/Games/碧蓝/tools --jobs 16
"""

import argparse
import os
import sys

from core.src.static_classes import batch_restore


def main():
    ap = argparse.ArgumentParser(
        description="AzurLanePaintingExtract CLI",
        epilog=(
            "示例:\n"
            "  python3 cli.py batch --out <游戏目录> --jobs 16\n"
            "  python3 cli.py restore xxx_tex --out x.png\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("batch", help="批量还原立绘")
    p.add_argument("--out", default=os.getcwd(), help="游戏根目录")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("restore", help="还原单个 *_tex 包")
    p.add_argument("tex", help="Assets/painting/*_tex 路径")
    p.add_argument("--bust", action="store_true", help="半身模式 (Sprite 原图)")
    p.add_argument("--out", default=None, help="输出 PNG 路径")

    args = ap.parse_args()
    if args.cmd == "batch":
        batch_restore.run(args.out, jobs=args.jobs, limit=args.limit)
    elif args.cmd == "restore":
        img = batch_restore.restore_bundle(args.tex, bust=args.bust)
        if img is None:
            print("无贴图, 跳过")
            sys.exit(1)
        out = args.out or os.path.splitext(args.tex)[0] + ".png"
        img.save(out)
        print(out)


if __name__ == "__main__":
    main()
