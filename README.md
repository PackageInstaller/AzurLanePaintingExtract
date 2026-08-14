# AzurLanePaintingExtract

碧蓝航线立绘还原 CLI

## 安装依赖

- Python 3.9+
- `pip install UnityPy Pillow numpy requests rich`
- [luajit-decompiler](https://github.com/PackageInstaller/luajit-decompiler)（Arch 下可用 AUR 包 `luajit-decompiler-git`）

## 用法

```bash
python3 cli.py --help
```

### 批量还原

```bash
python3 cli.py batch --jobs 16
python3 cli.py batch --jobs 16 --full
```

### 单张还原

```bash
python3 cli.py restore Assets/painting/xxx_tex [--bust] [--out out.png]
```
