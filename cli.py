#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AzurLanePaintingExtract CLI

子命令:
    batch [--jobs N] [--limit N] [--full] [--hx]
        先检查并下载 painting/paintingface 更新, 只转换有更新的立绘;
        --full 强制转换全部; --hx 额外导出和谐立绘 (默认跳过)
        人物层 (_rw) 叠到背景 (key / _n) 上, 不单独导出半身/front/shop_hx
    restore TEX_PATH [--bust] [--out PNG]
        还原单个 *_tex 包 (Mesh 拼接 / Sprite 原图)

示例:
    python3 cli.py batch --jobs 16
    python3 cli.py batch --jobs 16 --full
"""

import argparse
import os
import sys

from core.src.static_classes import batch_restore

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(
        description="AzurLanePaintingExtract CLI",
        epilog=(
            "示例:\n"
            "  python3 cli.py batch --jobs 16\n"
            "  python3 cli.py batch --jobs 16 --full\n"
            "  python3 cli.py restore xxx_tex --out x.png\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("batch", help="批量还原立绘")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--full", action="store_true", help="忽略增量, 强制转换全部立绘")
    p.add_argument(
        "--hx",
        action="store_true",
        help="导出 _hx 和谐立绘 (默认跳过)",
    )

    p = sub.add_parser("restore", help="还原单个 *_tex 包")
    p.add_argument("tex", help="Assets/painting/*_tex 路径")
    p.add_argument("--bust", action="store_true", help="半身模式 (Sprite 原图)")
    p.add_argument("--out", default=None, help="输出 PNG 路径")

    args = ap.parse_args()
    if args.cmd == "batch":
        batch_restore.run(
            PROJECT_ROOT,
            jobs=args.jobs,
            limit=args.limit,
            full=args.full,
            include_hx=args.hx,
        )
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
