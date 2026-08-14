# AzurLanePaintingExtract (CLI)

碧蓝航线立绘还原 CLI。已移除 wxPython GUI，只保留还原与批处理。

## 安装依赖

- Python 3.9+
- `pip install UnityPy Pillow numpy requests rich`
- `luajit-decompiler`（Arch 下可用 AUR 包 `luajit-decompiler-git`）
- 转换器 `azl2std.py / azl_bytecode.py` 已随仓库提供，无需外部依赖。

## 用法

```bash
python3 cli.py --help
```

### 批量还原（推荐）

```bash
python3 cli.py batch --out <游戏目录> --jobs 16
```

读取 `<游戏目录>/Assets/painting/*_tex`，内存还原后输出到
`<游戏目录>/Painting/<船名>/碧蓝航线_<船名>[<皮肤名>][<变体>].png`。

- 主立绘优先 `_rw`（全身人物立绘），无 `_rw` 时用无后缀；
- 有 `_rw` 时基础半身包输出为 `_半身`，是否用外部 Mesh 按 prefab 决定
  （半身节点 `mMesh != 0` 就拼 Mesh，否则用 Sprite 原图）；
- 主立绘（无后缀文件）只在 `ship_skin_expression[key].default` 非空时
  按 prefab `face` 节点定位并粘贴该默认表情，直接合成进
  `碧蓝航线_<人名>[<皮肤名>].png`；
- `default` 非空时，`paintingface/<key>` 里的全部表情差分导出为
  `..._表情<N>.png`；普通立绘不导出 `_表情` 文件；
- 表情位置按 `MeshImage.OnPopulateMesh` 公式计算，主立绘画布使用
  prefab 的 `mRawSpriteSize`，替代贴图 key（如 `xipeier_idolns`）
  同样按同一公式贴，不再硬编码偏移；
- 只有 Mesh 的变体包（`_bg` / `_shophx` / `_hx`）自动配对同名基础贴图；
- 纯动画包（`*_memory*` / `*_ani*`）跳过；
- 本地偏移索引不完整时直接从 `sharecfgdata` 二进制顺序流扫描重建
  完整索引（每条记录自带 uleb 长度和 id 常量），完全离线；
  社区完整索引仅作扫描失败时的回退；
- `{namecode:N}` 按 `name_code` 表解析为中文名。

### 单张还原

```bash
python3 cli.py restore Assets/painting/xxx_tex [--bust] [--out out.png]
```

表情图来自 `Assets/paintingface/<key>`。只有 `default` 槽位非空的皮肤
才会合成（如 `xipeier_idol` 的 default=1）；`aila` 这类 default 为空的
皮肤不会贴事件表情（其 home=6 只用于主界面事件）。

## 目录

- `cli.py`：命令行入口
- `core/src/static_classes/batch_restore.py`：批量还原 + 命名
- `core/src/static_classes/azl2std.py` / `azl_bytecode.py`：魔改 LuaJIT
  字节码转标准格式
- `core/src/static_classes/image_deal.py`：原版 Mesh 拼接与透明混合
- `core/src/static_classes/static_data.py` / `search_order.py`：纯逻辑
