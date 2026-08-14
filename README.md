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
python3 cli.py batch --jobs 16
python3 cli.py batch --jobs 16 --full
```

每次运行先通过登录服握手获取清单，只下载 `painting/` 与
`paintingface/`、`sharecfgdata/`（数据表）与 `scripts64`（64 位脚本）
的缺失/变化资源，然后用内置 Scipio 解密脚本从 scripts64 只提取
`painting_filte_map / name_code / ship_skin_expression` 三个 Lua 表，
再只转换本次更新的 `*_tex`；无更新时直接跳过。`--full` 可强制转换
全部。所有资源、输出与缓存都在脚本所在目录（`Assets/`、`Lua/`、
`Painting/`）。

增量判断依据 `Painting/painting_state.json`：记录每个 `*_tex` 已转换
时的源 md5。需要转换 = 本次资产有更新 ∪ 从未转换过 ∪ 输出文件缺失；
资产无更新但从未转换（如首次运行或 Painting 被清空）也会正常补转。

读取 `Assets/painting/*_tex`，内存还原后输出到
`Painting/<船名>/碧蓝航线_<船名>[<皮肤名>][<变体>].png`。

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
- 偏移索引直接从 `sharecfgdata` 二进制顺序流扫描重建
  （每条记录自带 uleb 长度和 id 常量），不依赖社区数据；
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
- `core/src/static_classes/batch_restore.py`：批量还原编排
- `core/src/static_classes/datatables.py`：sharecfg Lua 索引/表达式/namecode 解析
- `core/src/static_classes/downloader.py`：立绘清单获取与增量下载
- `core/src/static_classes/lua_extract.py`：scripts64 解密与所需 Lua 表提取
- `core/src/static_classes/scipio.py`：Scipio.www 资源解密
- `core/src/static_classes/skin_map.py`：皮肤/舰船名映射构建
- `core/src/static_classes/painting_assets.py`：Unity 资产还原与表情贴图
- `core/src/static_classes/naming.py`：输出命名
- `core/src/static_classes/azl2std.py` / `azl_bytecode.py`：魔改 LuaJIT
  字节码转标准格式
