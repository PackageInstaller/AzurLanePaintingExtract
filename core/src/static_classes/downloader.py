"""碧蓝航线立绘资源增量下载。

只同步 painting/ 与 paintingface/ 两个前缀, 其余资产不动。
每次运行先与登录服握手取最新清单, 对比本地 md5, 缺失/变化的
直接写回 Assets (覆盖), 返回本次更新的 *_tex 文件名列表。
"""

import hashlib
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    raise RuntimeError("需要 requests: pip install requests")


console = Console()
session = requests.Session()
session.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))
session.mount("http://", HTTPAdapter(pool_connections=32, pool_maxsize=32))

DEFAULT_CDNS = [
    "https://blhx-patch-oss.oss-cn-hangzhou.aliyuncs.com",
    "https://line3-patch-blhx.bilibiligame.net",
    "https://line1-patch-blhx.bilibiligame.net",
    "https://line4-patch-blhx.bilibiligame.net",
]
DEFAULT_PLATFORM = "android"
LOGIN_HOST = "line1-login-bili-blhx.bilibiligame.net"
LOGIN_PORT = 80
LOGIN_HEX = "000a002a300000083d120130"
DEFAULT_MANIFESTS = [
    "$azhash$9$7$324$b485153420a85ef9",
    "$maphash$331$cb6c048ebea2844d",
    "$dormhash$1529$40634b51c332f14d",
    "$cipherhash$1304$a289769c12c76172",
    "$mangahash$1334$690943546c0baa7c",
    "$paintinghash$1721$522baf6dc3f4f431",
    "$bgmhash$1300$d10f89b716830704",
    "$pichash$1311$757c596c4ef21956",
    "$l2dhash$1522$120e2ce1a981ac61",
    "$cvhash$1415$d53050872f4174ea",
]
PAINTING_PREFIXES = ("painting/", "paintingface/")


def fetch_live_manifest_names(timeout: float = 3.0):
    """登录服 TCP 握手, 返回 (清单名列表, APK 地址)。"""
    data = b""
    with socket.create_connection((LOGIN_HOST, LOGIN_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(bytes.fromhex(LOGIN_HEX))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
    txt = data.decode("utf-8", "ignore")
    names = [f"${k}hash{v}" for k, v in re.findall(r"\$(.*?)hash(.*?)\"", txt)]
    if not names:
        raise RuntimeError("登录服响应中未找到 hash 清单")
    apks = re.findall(r"https?://[^\"\s]+", txt)
    return names, (apks[0] if apks else None)


def load_manifest(cdns, platform, manifest_name, retries: int = 2):
    """下载单份清单 -> {path: (size, md5)}。"""
    last_err = None
    for cdn in cdns:
        for _ in range(retries):
            url = f"{cdn}/{platform}/hash/{manifest_name}"
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
                entries = {}
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    path, size, md5 = line.split(",")
                    entries[path] = (int(size), md5)
                console.print(f"[green]清单 {manifest_name}: {len(entries)} 条 ({cdn})[/green]")
                return entries
            except Exception as e:
                last_err = e
    raise RuntimeError(f"清单下载失败: {last_err}")


def load_manifests(cdns, platform, manifest_names):
    """合并多份清单。"""
    merged = {}
    for name in manifest_names:
        entries = load_manifest(cdns, platform, name)
        for path, value in entries.items():
            if path.startswith(PAINTING_PREFIXES):
                merged[path] = value
    console.print(f"[cyan]立绘清单合并: {len(merged)} 条[/cyan]")
    return merged


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def need_update(local_path, size, md5):
    if not os.path.isfile(local_path):
        return True
    if os.path.getsize(local_path) != size:
        return True
    try:
        with open(local_path, "rb") as f:
            return md5_of(f.read()) != md5
    except OSError:
        return True


def download_one(cdns, platform, path, size, md5, dest, progress, task_id):
    """带 CDN 切换的下载, 直接覆盖 dest。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    last_err = None
    for cdn in cdns:
        try:
            url = f"{cdn}/{platform}/resource/{md5}"
            with session.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                window_start = time.time()
                window_bytes = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        progress.advance(task_id, len(chunk))
                        window_bytes += len(chunk)
                        if window_bytes >= 1 << 20:
                            if time.time() - window_start > 10:
                                raise RuntimeError(f"速度过慢 {cdn}")
                            window_start = time.time()
                            window_bytes = 0
            if os.path.getsize(tmp) != size or md5_of(open(tmp, "rb").read()) != md5:
                raise RuntimeError(f"校验失败 {path}")
            os.replace(tmp, dest)
            return path, None
        except Exception as e:
            last_err = e
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path, last_err


def sync_paintings(out_root, jobs=8):
    """检查并下载立绘资源, 返回本次更新的 painting/*_tex 文件名列表。"""
    cdns = list(DEFAULT_CDNS)
    try:
        names, _ = fetch_live_manifest_names()
        console.print(f"[green]登录服握手成功, 获取 {len(names)} 份清单[/green]")
    except Exception as e:
        names = list(DEFAULT_MANIFESTS)
        console.print(f"[yellow]握手失败({e}), 使用内置清单[/yellow]")

    manifest = load_manifests(cdns, DEFAULT_PLATFORM, names)
    assets_dir = os.path.join(out_root, "Assets")
    os.makedirs(assets_dir, exist_ok=True)

    pending = []
    for path, (size, md5) in manifest.items():
        dest = os.path.join(assets_dir, path)
        if need_update(dest, size, md5):
            pending.append((path, size, md5, dest))
    if not pending:
        console.print("[green]立绘无更新[/green]")
        return []

    console.print(f"[cyan]更新立绘 {len(pending)} 条 -> Assets[/cyan]")
    pending.sort(key=lambda t: -t[1])
    fail = 0
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("下载立绘", total=sum(t[1] for t in pending))
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [
                ex.submit(download_one, cdns, DEFAULT_PLATFORM, p, s, m, d, progress, task_id)
                for p, s, m, d in pending
            ]
            for fut in as_completed(futs):
                path, err = fut.result()
                if err:
                    fail += 1
                    console.print(f"[red]失败 {path}: {err}[/red]")

    updated = [
        os.path.basename(p)
        for p, _, _, _ in pending
        if p.startswith("painting/") and p.endswith("_tex") and os.path.isfile(os.path.join(assets_dir, p))
    ]
    console.print(f"[green]立绘下载完成: 更新 {len(updated)} 个 *_tex, 失败 {fail}[/green]")
    return updated
