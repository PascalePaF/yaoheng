<p align="center">
  <img src="app.png" width="112" alt="曜衡 Logo">
</p>

<h1 align="center">曜衡</h1>

<p align="center">
  一款面向 Windows 的桌面计算器、全球货币换算与虚拟币行情工具。
</p>

<p align="center">
  <a href="https://github.com/alokxfox/yaoheng/releases/latest"><img src="https://img.shields.io/github/v/release/alokxfox/yaoheng?display_name=tag&sort=semver" alt="Latest release"></a>
  <a href="https://github.com/alokxfox/yaoheng/actions/workflows/tests.yml"><img src="https://github.com/alokxfox/yaoheng/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows" alt="Windows 10 / 11">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
</p>

## 简介

曜衡把日常计算、三端货币换算、虚拟币换算和趋势行情整合在一个高对比度桌面界面中。它支持键盘公式输入、专业函数、计算历史、法币与虚拟币实时搜索、收藏置顶、多计价币种以及 1 / 7 / 30 / 90 日趋势查看。

程序无需登录，不包含广告或遥测。设置、历史记录和行情缓存保存在本机。

## 下载

请前往 [最新 Release](https://github.com/alokxfox/yaoheng/releases/latest) 下载：

| 版本 | 文件 | 适合场景 |
| --- | --- | --- |
| Windows 安装版（推荐） | `曜衡-<版本>-Windows-x64-安装版.exe` | 自动创建开始菜单入口，支持可选桌面快捷方式和标准卸载 |
| 绿色免安装版 | `曜衡-绿色免安装版.zip` | 解压即用，适合移动硬盘、U 盘或自定义目录 |

系统要求：Windows 10 或 Windows 11，64 位。实时汇率和行情需要网络连接；短时断网时会尝试使用最近一次可信缓存。

> 当前安装包未使用商业代码签名证书。Windows SmartScreen 可能在首次运行时显示未知发布者提示，请只从本仓库 Release 页面下载，并核对 Release 中公布的 SHA-256。

每次正式构建会生成 `SHA256SUMS.txt`。下载后可用 PowerShell 计算文件摘要，并与清单中的同名文件核对：

```powershell
Get-FileHash -Algorithm SHA256 ".\曜衡-<版本>-Windows-x64-安装版.exe"
Get-FileHash -Algorithm SHA256 ".\曜衡-绿色免安装版.zip"
```

## 功能亮点

| 模块 | 能力 |
| --- | --- |
| 计算器 | 标准与专业模式、键盘公式、隐式乘法、科学计数法、角度制、历史记录和多种复制格式 |
| 货币换算 | 全球法币 A / B / C 三端联动、任意端输入、快速交换、搜索、收藏、置顶和排序 |
| 虚拟币换算 | 法币与主流虚拟币自由组合，小额价格保留有效数字，24 小时涨跌即时着色 |
| 行情趋势 | 法币与虚拟币列表、可切换计价货币、参考数额、24 小时变化及 1 / 7 / 30 / 90 日走势图 |
| 个性化 | 黑夜 / 白天主题、IANA 时区、启动页、窗口行为、自动刷新和缓存上限 |
| 可靠性 | 多数据源校验与回退、并发安全缓存、原子保存、损坏恢复和后台请求竞态保护 |

## 快速使用

1. 安装版直接运行安装程序；绿色版解压后双击 `曜衡.exe`。
2. 在计算器公式框中输入算式，按 `Enter` 或 `=` 计算。
3. 在货币或虚拟币页面的任意金额框中输入数值或四则算式，另外两端会同步换算。
4. 在行情页双击币种查看趋势；切换计价货币后，整表价格、涨跌方向和颜色会同步重算。
5. 在设置页调整自动刷新、主题、时区、历史记录和数据目录。

更完整的操作说明随安装包和绿色版一同提供，文件名为 `使用说明.txt`。

## 数据、隐私与风险提示

- 默认设置与缓存位于应用目录；也可在设置页迁移数据目录、导入或导出设置。卸载器只会按用户确认删除应用目录内的数据，不会删除自定义数据目录。
- 发布包不会包含开发电脑上的设置、历史记录、收藏、缓存或自定义路径。
- 法币汇率来自 ExchangeRate-API，法币历史趋势来自 Frankfurter；虚拟币行情主要来自 Binance，并使用 CoinGecko 作为补充或备用。
- 外部数据源可能延迟、中断或调整接口。曜衡会校验响应并尽量回退到最后可信数据，但不保证行情的实时性、完整性或结算准确性。
- 所有价格、汇率和趋势仅供参考，不构成交易、投资或结算建议。

## 从源码运行

需要 Python 3.11 或更高版本。

```powershell
git clone https://github.com/alokxfox/yaoheng.git
cd yaoheng
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --disable-pip-version-check --no-deps --only-binary=:all: -r requirements.txt
python main.py
```

## 测试与构建

运行自动化测试：

```powershell
python -B -m unittest discover -s tests -v
```

构建完整 Windows 发行包需要 Python 3.13.15+ 与 Inno Setup 6。构建脚本还会检查 Python 捆绑的 OpenSSL 是否达到当前安全修复基线，避免把过期原生运行时带入安装包：

```powershell
winget install --id Python.Python.3.13 -e
winget install --id JRSoftware.InnoSetup -e
.\build.ps1
```

如需使用未加入 `PATH` 的隔离 Python，可将其绝对路径放入 `$env:YAO_HENG_PYTHON` 后再运行构建脚本。

构建脚本会在隔离环境中安装完整锁定的依赖、拒绝隐式依赖和源码包、检查语法、运行全部测试，并对暂存目录和压缩包执行内容一致性与隐私扫描，然后生成：

- `release\曜衡\曜衡.exe`：本机可直接运行的文件夹版；
- `release\曜衡-绿色免安装版.zip`：不含本机隐私数据的绿色版；
- `release\曜衡-<版本>-Windows-x64-安装版.exe`：当前用户安装包。
- `release\SHA256SUMS.txt`：上述可发布 ZIP 与安装器的 SHA-256 清单。

安装器使用 Inno Setup 的稳定版工具链构建。简体中文安装界面采用项目内固定版本的第三方翻译，来源与许可证见 `installer/THIRD-PARTY-NOTICES.txt`；成品中的 `licenses/` 目录包含实际捆绑的 Python、OpenSSL、Tcl/Tk 与 Python 依赖许可证文本。

## 项目结构

```text
main.py                  程序入口
app_ui.py                界面与交互逻辑
calculator_core.py       计算核心
rate_service.py          汇率、行情、趋势与缓存服务
settings_service.py      设置与数据目录管理
tests/                   自动化回归测试
installer/               Windows 安装器配置与语言资源
build.ps1                测试并构建完整发行包
CHANGELOG.md             版本更新记录
```

`app_settings.json`、`data/`、`build/`、`dist/` 和 `release/` 均为本机运行或构建生成内容，不纳入源码版本控制。

## 参与改进

欢迎通过 [Issues](https://github.com/alokxfox/yaoheng/issues) 报告可复现的问题或提出建议。提交问题时请附上 Windows 版本、曜衡版本、复现步骤、预期结果和实际结果；请勿上传包含个人设置或缓存的文件。

版本变化请查看 [CHANGELOG.md](CHANGELOG.md)。

## 许可

当前仓库未附加开源许可证。除非作者另行明确授权，仓库公开可见不代表授予复制、修改、再分发或商业使用权。
