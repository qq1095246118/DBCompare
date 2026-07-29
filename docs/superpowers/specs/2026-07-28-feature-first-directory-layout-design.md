# 功能优先的目录结构调整设计

## 目标

将项目从“Bubblemaps 业务在上、功能在下”的结构，调整为“功能在上、业务在下”的结构。`getDB` 和 `getMarket` 成为顶层功能包，`bubblemaps` 成为各功能下的业务实现，以便后续在同一功能下增加其他业务实现。

根目录 `common` 只存放跨功能、跨业务可复用的方法，不再包含 Bubblemaps 专属的数据契约。

## 目标结构

```text
DBCompare/
├── common/
│   ├── __init__.py
│   ├── artifacts.py
│   └── time_window.py
├── getDB/
│   ├── __init__.py
│   └── bubblemaps/
│       ├── __init__.py
│       ├── tool/
│       │   ├── __init__.py
│       │   ├── contract.py
│       │   ├── db_source.py
│       │   └── export_bubblemaps_db.py
│       └── db/
├── getMarket/
│   ├── __init__.py
│   └── bubblemaps/
│       ├── __init__.py
│       ├── README.md
│       ├── tool/
│       │   ├── __init__.py
│       │   └── ...
│       ├── market/
│       └── market_manual/
├── tests/
├── docs/
└── pyproject.toml
```

## 迁移映射

| 原路径 | 新路径 |
| --- | --- |
| `bubblemaps/common/artifacts.py` | `common/artifacts.py` |
| `bubblemaps/common/time_window.py` | `common/time_window.py` |
| `bubblemaps/common/contract.py` | `getDB/bubblemaps/tool/contract.py` |
| `bubblemaps/getDB/tool/` | `getDB/bubblemaps/tool/` |
| `bubblemaps/getDB/db/` | `getDB/bubblemaps/db/` |
| `bubblemaps/getMarket/tool/` | `getMarket/bubblemaps/tool/` |
| `bubblemaps/getMarket/db/` | `getMarket/bubblemaps/market/` |
| `bubblemaps/getMarket/db_manual/` | `getMarket/bubblemaps/market_manual/` |
| `bubblemaps/README.md` | `getMarket/bubblemaps/README.md` |

日期目录、运行产物和 `_backups`、`_failed`、`_locks`、`_staging`、`_trash` 等内部结构原样保留。

## Python 包和入口

顶层 Python 包为 `common`、`getDB` 和 `getMarket`。两个功能下的 Bubblemaps 实现分别使用以下导入前缀：

```text
getDB.bubblemaps.tool
getMarket.bubblemaps.tool
```

公共方法使用 `common` 前缀。Bubblemaps 专属的 `contract` 随 `getDB` 迁移，不再作为公共模块。

命令行入口调整为：

```bash
python -m getDB.bubblemaps.tool.export_bubblemaps_db
python -m getMarket.bubblemaps.tool.export_bubblemaps_market
```

`pyproject.toml` 的包发现范围同步覆盖 `common*`、`getDB*` 和 `getMarket*`，不再发现旧的顶层 `bubblemaps*` 包。

## 输出路径

`getDB` 默认输出到：

```text
getDB/bubblemaps/db/YYYY-MM-DD/
```

`getMarket` 默认输出到：

```text
getMarket/bubblemaps/market/YYYY-MM-DD/
```

手工市场数据位于：

```text
getMarket/bubblemaps/market_manual/
```

迁移只改变目录位置和名称，不改变产物格式、生成流程、日期规则或发布语义。

## 兼容范围

本次迁移同步修改源码导入、测试导入、模块运行命令、默认输出根目录、路径断言和项目文档。旧的 `bubblemaps.getDB` 与 `bubblemaps.getMarket` 导入路径不提供兼容层，避免长期保留两套结构。

## 验证标准

1. 原有数据和手工数据全部出现在对应新目录，文件内容与数量不因迁移发生变化。
2. 项目中不再存在有效的旧导入路径或旧默认输出路径。
3. 两个模块入口的 `--help` 可以正常执行。
4. 默认离线测试套件全部通过。
5. 搬空后的根目录 `bubblemaps/` 被移除。

真实数据库和 Bubblemaps API smoke test 不属于本次纯目录迁移的必需验证；除非另行明确要求，不访问外部服务。
