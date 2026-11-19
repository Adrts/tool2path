# tool2path · 工具链 PATH 管理器

> 用一个可读、可编辑、可版本管理的配置文件，管理 Windows **用户级 PATH** 里的工具链入口目录。

[![Platform](https://img.shields.io/badge/platform-Windows-blue)](#快速开始)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#从源码运行)
[![GUI](https://img.shields.io/badge/GUI-PySide6%20%28Qt6%29-41cd52)](https://pypi.org/project/PySide6/)
[![License](https://img.shields.io/badge/license-CC0--1.0-lightgrey)](./LICENSE)
[![Download](https://img.shields.io/badge/download-latest%20release-2ea44f)](https://github.com/Adrts/tool2path/releases/latest)

---

## 它解决什么问题

机器上装了一堆工具链（Node、Python、JDK、Go、FFmpeg……），每个都有一个入口目录；调用哪个版本，全看 PATH。手动整理既麻烦又容易误删系统路径。

**tool2path 的做法**：把所有入口目录登记在与程序同级的 `registered-paths.yaml` 中（纯路径列表，可手改、可进 Git）；界面只是这份清单的可视化编辑器，按需把目录移入 / 移出用户级 PATH。

- **集中管理**：一份清单看清所有工具链入口。
- **随时切换**：勾选即可移入 / 移出，不用反复改环境变量。
- **安全**：未报备的系统路径是只读"锚点"，顺序永远不动；改动先预览，点「应用」才写注册表。
- **可携带**：配置文件可手工编辑，跟着 dotfiles / 仓库走。

## 快速开始

### 下载即用

1. 到 [Releases](https://github.com/Adrts/tool2path/releases/latest) 下载最新压缩包。
2. 解压到有写权限的目录，运行 `tool2path.exe`。
3. 首次启动会在 exe 同级自动生成 `registered-paths.yaml`。

> 解压后的 `_internal` 等文件是运行依赖，不要只把 exe 单独拷走。普通用户权限即可，无需管理员。

### 从源码运行

要求 Windows + Python 3.10+，且命令行能调用 `python`。

```powershell
git clone https://github.com/Adrts/tool2path.git
cd tool2path
```

- 双击 [run.bat](./run.bat)：自动创建 `.venv`、安装依赖并启动。
- 或分两步：先双击 [setup.bat](./setup.bat) 准备环境，再执行 `.\.venv\Scripts\python.exe tool2path.py`。

自测（可选，48 项）：

```powershell
.\.venv\Scripts\python.exe tests\selftest.py
```

### 打包

双击 [build.bat](./build.bat)，产物为 `dist\tool2path\tool2path.exe`（onedir，需连同 `_internal` 一起分发）。

## 怎么用

三步：**报备**（写进配置文件）→ **移入**（放进 PATH）→ **应用**（写回注册表，新开的终端生效）。

| 想做什么 | 怎么做 |
| --- | --- |
| 启用工具链 | 左栏「＋ 报备新目录」→ 勾选 →「移入 →」→ 应用 |
| 放到 PATH 指定位置 | 左栏条目直接拖到右栏插入位（拖到哪落到哪） |
| 临时停用 | 右栏选中报备行 →「← 移出」→ 应用（仍在报备库，可再移入） |
| 调整 PATH 顺序 | 右栏拖动报备行或点 `▲` / `▼` → 应用 |
| 整理清单 | 左栏拖拽排序（立即写文件，无需应用） |
| 手工编辑清单 | 修改 `registered-paths.yaml` → 按提示重新载入 |
| 处理失效目录 | 行内黄标「目录不存在」→ 重建目录 / 删除声明 |

三条注意：

- 左栏顺序 ≠ PATH 顺序：左栏只管清单，右栏才是生效顺序。
- 只有点「应用 / 确认」才写注册表；未应用前可 `Ctrl+Z` 撤销。
- 已打开的终端不会自动刷新，新开一个窗口才生效。

## 配置文件

`registered-paths.yaml` 与程序同级（源码运行 = 脚本目录，打包运行 = exe 目录），首次启动自动创建。内容只有目录路径，每行一条：

```yaml
- C:\Program Files\nodejs
- C:\Python312\Scripts
```

可加注释、可手工调整行序；同一目录不区分大小写去重。写回只重写路径列表，不产生 `.bak` / `.tmp`。

## 更多

- **设计与规则**：R0–R8、界面草图、数据模型、架构、边界情况与验收标准，见 [设计文档](./design/Design%20Doc.md)。
- **贡献**：欢迎 Issue / PR；涉及规则或交互的改动请先读设计文档，提交前运行 `tests\selftest.py`。

---

## AI 使用声明

本仓库源码与文档由 AI 辅助生成并经作者审校（`ai-generated`）。作者对最终内容负责；AI 生成内容仍可能有疏漏，使用前请自行评估。

机器可读：`# SPDX-AI-Disclosure: ai-generated`

## 开源许可

本项目基于 [CC0 1.0 Universal](./LICENSE) 发布，可自由使用、修改、分发，无需署名。

> 本工具会修改用户级 PATH，首次使用前建议备份（注册表导出 `HKCU\Environment`，或记录 `echo $env:Path`）。
