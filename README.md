<p align="center">
  <img src="app.png" width="112" alt="曜衡 Logo">
</p>

<h1 align="center">曜衡</h1>

<p align="center">
  Windows 桌面计算器、七币种兑换、法币/虚拟币换算、C2C 按金额报价与行情趋势工具。
</p>

<p align="center">
  <a href="https://github.com/alokxfox/yaoheng/releases/latest"><img src="https://img.shields.io/github/v/release/alokxfox/yaoheng?display_name=tag&sort=semver" alt="Latest release"></a>
  <a href="https://github.com/alokxfox/yaoheng/actions/workflows/tests.yml"><img src="https://github.com/alokxfox/yaoheng/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows" alt="Windows 10 / 11">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
</p>

## 简介

曜衡 3.20.1 把日常/专业计算、七栏 C2C 兑换、独立市场兑换、三端换算、金额匹配报价以及法币和虚拟币趋势整合在一个桌面应用中，并支持从官方 GitHub Release 检查和覆盖升级。

应用无需账号，不包含广告或遥测。设置、历史记录、行情缓存和本机 API 令牌校验材料都保存在本机。

## 下载

前往 [最新 Release](https://github.com/alokxfox/yaoheng/releases/latest) 下载：

| 版本 | 文件 | 用途 |
| --- | --- | --- |
| Windows 安装版（推荐） | `Yaoheng-3.20.1-Windows-x64-Setup.exe` | 当前用户安装、覆盖升级、开始菜单、可选桌面快捷方式、标准卸载 |
| 绿色免安装版 | `Yaoheng-3.20.1-Windows-x64-Portable.zip` | 解压即用，适合 D 盘、移动硬盘或 U 盘 |
| 校验清单 | `SHA256SUMS.txt` | 核对安装版与绿色版的 SHA-256 |

系统要求：64 位 Windows 10 或 Windows 11。实时汇率、趋势与 C2C 报价需要网络；断网时普通行情会尽量使用最近一次可信缓存。

> 安装包暂未使用商业代码签名证书。Windows SmartScreen 首次运行时可能显示“未知发布者”。请只从本仓库 Release 下载并核对 SHA-256。

```powershell
Get-FileHash -Algorithm SHA256 ".\Yaoheng-3.20.1-Windows-x64-Setup.exe"
Get-FileHash -Algorithm SHA256 ".\Yaoheng-3.20.1-Windows-x64-Portable.zip"
```

## 功能

| 模块 | 主要能力 |
| --- | --- |
| 计算器 | 标准/专业模式、键盘公式、隐式乘法、科学计数法、DEG/RAD、历史与多种复制格式 |
| C2C 兑换 | 主币独占首行、其余六币三列两行；七栏均可输入，按实际金额查询法币/虚拟币 C2C，虚拟币之间经所选法币双段换算 |
| 市场兑换 | 与 C2C 完全分开的七栏普通换算页，使用公开现货/市场行情和法币参考汇率，不冒充可执行购买价 |
| 货币换算 | 全球法币 A/B/C 三端联动、任意端输入、搜索、收藏、置顶、地区与 24h 涨跌 |
| 虚拟币换算 | 法币与虚拟币三端自由组合，可切换普通汇率或 C2C 按金额 |
| 行情趋势 | 法币/虚拟币整表、计价币、参考数额、搜索排序及 1/7/30/90 日趋势 |
| 本机 API | 默认关闭，仅 `127.0.0.1`；计算、换算、命令和能力查询，为未来机器人接入预留 |
| 软件更新 | 在设置页检查官方 GitHub Release，校验附件大小和 SHA-256 后启动覆盖升级 |
| 外观主题 | 28 套低刺激深浅主题即时切换，避免纯黑/纯白大面积背景，保持相同布局和字号 |
| 可靠性 | 十进制精确换算、响应校验、并发合并、原子缓存/设置、备份恢复和旧结果隔离 |

## 七币种兑换

侧栏在计算器与货币页之间提供彼此独立的“C2C 兑换”和“市场兑换”：

- 主币独占第一整行且文字左对齐，其余六币种按三列两行排列；七个槽位保持唯一。
- 七个币种框都可点击输入搜索，支持上下键选择和回车确认，与货币/虚拟币页保持一致。
- 七个金额框都可直接输入 `+ - * / %` 和括号算式；最后编辑的金额框成为当前输入源，按回车、输入 `=` 或离开输入框后精确求值并同步更新另外六项。
- 主币仍固定占据首行用于布局和重点观察；任意下方卡片也可设为主币，或使用内部未舍入结果设为主币并继续换算。
- 两个兑换页分别保存币种、主币、当前输入、金额、平台、结算法币和支付方式，下次打开恢复上次设置。

“C2C 兑换”不会在平台失败时用普通虚拟币行情冒充 C2C：法币与虚拟币之间按输入金额匹配单个广告；虚拟币与虚拟币之间先按 C2C 卖成所选结算法币，再用实际法币结果按 C2C 买入目标币；法币之间使用明确标注的参考汇率。“市场兑换”只使用普通公开行情，结果不代表平台可直接购买或成交。

## C2C 能力与边界

曜衡查询的是参考报价，不执行交易：

- Binance：只使用官方 P2P Skill 的公共只读报价、广告列表和支付方式接口；不含账户、下单、订单或商家管理能力。
- OKX：仅提供官方白名单/商家 P2P 的可配置只读框架。默认未配置、不会进入自动报价；需要集成方取得官方授权、字段契约和瞬时凭据。
- 支付方式来自平台返回的官方 identifier，不硬编码网页内部标识。
- “最低展示价”和“当前金额可匹配价”分开显示；C2C 兑换页不启用普通行情回退。
- Binance 广告解析同时兼容官方当前与旧版字段；金额、库存、完成率均从原始十进制值进入匹配逻辑。
- KYC、地区、账户年龄、付款资格、广告方条件和实际库存仍由平台决定，曜衡不保证成交。

应用不使用网页内部或逆向接口，也没有任何 C2C 写入/交易接口。

## 页面状态与本机数据

C2C 兑换、市场兑换、货币、虚拟币和两个行情页会分别保存自己的币种、主输入、金额、计价币、参考数额、周期和筛选，不会在重启时恢复成默认币种。设置采用 schema v3、原子替换与备份恢复；从旧版本升级时会保留迁移前备份，未来版本设置不会被旧程序覆盖。

默认数据位于应用目录，也可以在设置页迁移到自定义目录。卸载器只在用户确认后删除应用目录内的设置、历史、缓存和本机 API 令牌；自定义数据目录不会被卸载器删除。

## 软件更新与覆盖升级

设置页可手动检查曜衡官方 GitHub 仓库的最新正式 Release。发现新版本后，曜衡只接受固定命名的 Windows 安装包和 `SHA256SUMS.txt`，下载完成后同时核对附件大小与 SHA-256；校验失败的临时文件会被丢弃，不会启动。

安装版使用稳定的应用标识识别旧版本，并默认沿用原安装目录、开始菜单组和快捷方式选择。升级前会替换旧运行文件，同时保留 `app_settings*.json`、`private/` 和 `data/` 中的设置、收藏、历史、缓存与本机 API 校验材料。绿色版若使用应用内升级，会通过安装器覆盖当前目录并登记为安装版；如需继续保持纯绿色方式，请手动下载新版 ZIP。

## 本机 API 与机器人接入

设置页的“API 接入”默认关闭。启用前需生成令牌：明文只在生成或轮换成功时显示一次，请立即保存到可信密码管理器；磁盘仅保存带盐的 scrypt 校验材料，令牌不会进入设置导出、日志或 Release 包。

安全边界：

- 只允许固定地址 `127.0.0.1`，不监听局域网或公网；生产模式拒绝随机端口。
- 除 `GET /health` 外均要求 `Authorization: Bearer <token>`。
- 校验 Host、浏览器 Origin、JSON 大小/重复键/NaN，并提供请求限流与 C2C 并发上限。
- 连接测试只确认本机监听，不会把未认证的健康检查描述成令牌认证成功。

接口包括：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 无认证健康检查，仅确认本机监听 |
| GET | `/v1/capabilities` | 当前计算、换算与 C2C 平台能力 |
| POST | `/v1/calculate` | 安全计算器 |
| POST | `/v1/convert` | 普通或 C2C 换算 |
| POST | `/v1/command` | 机器人友好的命令入口 |

命令层支持 `/calc (12.5+7)*3`、`/fx 100 CNY USD`、`/c2c 1000 CNY USDT --provider binance`，以及 `兑换 10 CNY USD` 等中文形式。微信、QQ、Telegram 等机器人应作为独立适配层调用本机 API；仓库不捆绑第三方机器人 SDK 或平台凭据。

## 数据源与风险提示

- 法币现价来自 ExchangeRate-API，法币历史来自 Frankfurter。
- 虚拟币行情主要来自 Binance，并以 CoinGecko 作为补充或备用。
- C2C 来源与能力边界见上节；外部服务可能限流、中断、改变字段或受地区限制。
- 所有价格、汇率、趋势和 C2C 结果仅供参考，不构成交易、投资、付款或结算建议。

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

```powershell
python -B -m unittest discover -s tests -v
```

完整 Windows 发布构建要求 Python 3.13.15+、达到脚本安全基线的 OpenSSL，以及 Inno Setup 6：

```powershell
winget install --id JRSoftware.InnoSetup -e
$env:YAO_HENG_PYTHON = "C:\Path\To\Python313\python.exe"
.\build.ps1
```

构建脚本会创建隔离环境、安装完整锁定依赖、检查运行时/语法、运行全部测试、构建 PyInstaller 文件夹版和 Inno Setup 安装器，并扫描设置、缓存、令牌、迁移备份、本机路径与常见秘密特征。最终输出：

- `release\曜衡\曜衡.exe`
- `release\Yaoheng-3.20.1-Windows-x64-Portable.zip`
- `release\Yaoheng-3.20.1-Windows-x64-Setup.exe`
- `release\SHA256SUMS.txt`

## 项目结构

```text
main.py                  程序入口
app_ui.py                主窗口、页面与交互
calculator_core.py       计算核心
conversion_core.py       十进制精确换算
exchange_page.py         七币种状态与路由
rate_service.py          汇率、趋势与缓存
c2c/                     Binance/OKX 只读 C2C 层
command_service.py       机器人命令解析与执行
local_api.py             仅本机 HTTP API
secret_store.py          一次性令牌校验材料
update_service.py        GitHub Release 更新检查、下载与校验
settings_service.py      设置、迁移与数据目录
tests/                   离线自动化回归测试
installer/               Windows 安装器配置
build.ps1                完整发布构建
```

`app_settings.json`、`private/`、`data/`、`build/`、`dist/` 与 `release/` 都是本机运行或构建内容，不纳入源码版本控制。

## 参与改进

欢迎通过 [Issues](https://github.com/alokxfox/yaoheng/issues) 提交可复现问题。请附 Windows/曜衡版本、复现步骤、预期和实际结果；不要上传个人设置、缓存或令牌文件。

版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

当前仓库未附加开源许可证。仓库公开可见不代表授予复制、修改、再分发或商业使用权，除非作者另行明确授权。
