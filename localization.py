"""Small, dependency-free UI localization layer for Yaoheng.

The calculation and market models deliberately keep stable machine values.
Only text shown to a person is translated here, so changing language can never
change a currency code, expression, provider identifier, or stored setting.
"""

from __future__ import annotations

import ctypes
import re
import weakref
from datetime import datetime
from functools import lru_cache
from typing import Any, Mapping


DEFAULT_LANGUAGE = "zh_CN"
SUPPORTED_LANGUAGES = ("zh_CN", "zh_TW", "en_US", "ja_JP")
LANGUAGE_LABELS: Mapping[str, str] = {
    "zh_CN": "中文（简体）",
    "zh_TW": "中文（繁體）",
    "en_US": "English",
    "ja_JP": "日本語",
}

_language = DEFAULT_LANGUAGE
_display_variables: "weakref.WeakSet[DisplayStringVar]" = weakref.WeakSet()


# The source language is Simplified Chinese.  Entries cover the complete app
# shell and every interactive settings/calculator surface.  Longer sentences
# are also used as replacements inside dynamic status messages.
_EN: dict[str, str] = {
    "计算器": "Calculator",
    "C2C 兑换": "C2C Exchange",
    "市场兑换": "Market Exchange",
    "货币": "Currencies",
    "货币行情趋势": "Currency Trends",
    "虚拟币": "Crypto",
    "虚拟币行情趋势": "Crypto Trends",
    "设置": "Settings",
    "精准计算 · 实时金融": "Precise calculation · Live markets",
    "标准模式": "Standard",
    "专业模式": "Scientific",
    "标准": "Standard",
    "专业": "Scientific",
    "标准模式 · 苹果式键盘 · 直接输入公式": "Standard · Apple-style keypad · Type immediately",
    "专业模式 · 科学键盘 · 直接输入公式": "Scientific · Extended keypad · Type immediately",
    "复制结果": "Copy Result",
    "已复制": "Copied",
    "打开历史记录": "Open History",
    "关闭历史记录": "Close History",
    "历史记录": "History",
    "双击结果可重新使用": "Double-click a result to reuse it",
    "清空历史记录": "Clear History",
    "外观主题": "Appearance Themes",
    "主题与文字会同步切换，布局和字号保持不变": "Colors and text contrast switch together; layout and font sizes stay unchanged",
    "配色预览依次显示背景、卡片、按钮、强调色与文字色。": "Swatches show background, card, control, accent, and text colors.",
    "展开主题列表": "Expand themes",
    "收回主题列表": "Collapse themes",
    "当前": "Current",
    "白色文字": "White text",
    "黑色文字": "Black text",
    "配色参考": "Palette reference",
    "程序语言与刷新时区": "Language & Display Time Zone",
    "程序默认语言": "App language",
    "刷新显示时区": "Display time zone",
    "可使用中文城市、国家/地区或 IANA 名称搜索": "Search by Chinese city/region or IANA name",
    "所选时区当前时间：": "Selected time zone: ",
    "自动刷新": "Automatic Refresh",
    "货币与虚拟币使用独立分钟间隔": "Separate intervals for currencies and crypto",
    "启用自动刷新": "Enable automatic refresh",
    "最小化后继续按设定时间刷新": "Keep refreshing while minimized",
    "启动与关闭": "Startup & Close",
    "曜衡只运行一个窗口；重复启动会唤醒现有窗口": "Yaoheng runs one window; launching again restores it",
    "默认启动页面": "Startup page",
    "点击关闭按钮": "Close button action",
    "记住上次打开的页面": "Remember last page",
    "记住窗口大小和位置": "Remember window size and position",
    "点击 × 时最小化到任务栏": "Minimize to taskbar when × is clicked",
    "点击 × 时彻底退出软件": "Exit completely when × is clicked",
    "模式、角度、历史记录与复制格式": "Mode, angle, history, and copy format",
    "默认模式": "Default mode",
    "角度模式": "Angle mode",
    "复制结果格式": "Copy format",
    "纯数字": "Number only",
    "带千位分隔符": "Thousands separators",
    "完整算式与结果": "Expression and result",
    "记住上次标准/专业模式": "Remember Standard/Scientific mode",
    "退出后保留历史记录": "Keep history after exit",
    "历史记录条数": "History entries",
    "应用与缓存位置": "App & Cache Locations",
    "绿色版可整体迁移，也可单独指定数据目录": "Move the portable app or choose a separate data folder",
    "应用文件夹": "App folder",
    "缓存与行情数据文件夹": "Cache and market data folder",
    "打开文件夹": "Open Folder",
    "迁移应用与数据…": "Move App & Data…",
    "更改缓存目录…": "Change Cache Folder…",
    "应用与相关数据全部放在同一文件夹": "Keep the app and its data in one folder",
    "API 接入": "API Access",
    "启用本机 API": "Enable local API",
    "固定地址  127.0.0.1": "Fixed address  127.0.0.1",
    "端口": "Port",
    "生成令牌": "Generate Token",
    "轮换令牌": "Rotate Token",
    "连接测试": "Connection Test",
    "一次性令牌": "One-time token",
    "复制": "Copy",
    "软件更新": "Software Update",
    "检查更新": "Check for Updates",
    "下载并升级": "Download & Upgrade",
    "缓存与设置管理": "Cache & Settings",
    "限制缓存占用、清理缓存以及导入导出设置": "Limit cache usage, clear cache, and import/export settings",
    "缓存上限 MB（0 为不限制）": "Cache limit MB (0 = unlimited)",
    "重新统计": "Recalculate",
    "清理缓存": "Clear Cache",
    "导出设置…": "Export Settings…",
    "导入设置…": "Import Settings…",
    "恢复默认设置": "Restore Defaults",
    "立即退出曜衡": "Exit Yaoheng Now",
    "下一主题": "Next Theme",
    "切换下一个主题": "Next Theme",
    "当前版本": "Current version",
    "尚未检查更新": "Not checked yet",
    "正在从 GitHub 检查最新正式版本…": "Checking the latest GitHub release…",
    "正在下载曜衡": "Downloading Yaoheng",
    "并校验 SHA-256…": "and verifying SHA-256…",
    "当前已是最新版本": "You already have the latest version",
    "发现曜衡": "Yaoheng update found",
    "更新失败：": "Update failed: ",
    "正在统计…": "Calculating…",
    "当前缓存：": "Current cache: ",
    "正在准备网络连接…": "Preparing network connection…",
    "等待首次连接": "Waiting for first connection",
    "正在准备联网": "Preparing network",
    "正在重新连接": "Reconnecting",
    "重新连接网络": "Reconnect",
    "已联网": "Online",
    "当前网络连接成功！": "Network connected",
    "当前网络连接失败！": "Network unavailable",
    "部分数据已更新": "Some data updated",
    "汇率已联网": "Rates online",
    "刷新汇率": "Refresh Rates",
    "刷新全部汇率": "Refresh All Rates",
    "刷新全部行情": "Refresh All Markets",
    "刷新": "Refresh",
    "刷新中": "Refreshing",
    "最新刷新：": "Last refresh: ",
    "等待联网": "Waiting for network",
    "货币换算": "Currency Converter",
    "虚拟币换算": "Crypto Converter",
    "货币市场": "Currency Market",
    "虚拟币市场": "Crypto Market",
    "三端金额联动换算": "Three-way linked conversion",
    "代码": "Code",
    "名称": "Name",
    "换算数量": "Converted Amount",
    "地区": "Region",
    "币种": "Asset",
    "收藏": "Favorite",
    "置顶": "Pin",
    "搜索": "Search",
    "输入代码或名称搜索 · 双击查看走势": "Search code or name · Double-click for trend",
    "参考币种": "Reference Asset",
    "参考币数额": "Reference Amount",
    "参考数额": "Reference Amount",
    "计价货币": "Quote Currency",
    "默认顺序": "Default Order",
    "换算模式": "Conversion Mode",
    "来源": "Source",
    "支付": "Payment",
    "平台": "Platform",
    "跨币结算法币": "Bridge Fiat",
    "结算支付": "Settlement Payment",
    "主货币": "Primary Asset",
    "设为主币": "Set as Primary",
    "设为主币并换算": "Set Primary & Convert",
    "当前输入": "Current input",
    "普通汇率": "Market Rate",
    "C2C 按金额": "C2C by Amount",
    "不限支付方式": "Any payment method",
    "自动": "Auto",
    "实时": "Live",
    "含缓存": "Cached",
    "正在刷新…": "Refreshing…",
    "正在加载…": "Loading…",
    "暂无联网行情": "No online market data",
    "时间未知": "Time unknown",
    "汇率": "Rate",
    "价格": "Price",
    "最高": "High",
    "最低": "Low",
    "1日": "1D",
    "7日": "7D",
    "30日": "30D",
    "90日": "90D",
    "展开全部": "Show all",
    "套": "themes",
}


_JA: dict[str, str] = {
    "计算器": "電卓", "C2C 兑换": "C2C交換", "市场兑换": "市場レート換算",
    "货币": "通貨", "货币行情趋势": "通貨トレンド", "虚拟币": "暗号資産",
    "虚拟币行情趋势": "暗号資産トレンド", "设置": "設定",
    "精准计算 · 实时金融": "正確な計算 · リアルタイム市場",
    "标准模式": "標準", "专业模式": "関数", "标准": "標準", "专业": "関数",
    "标准模式 · 苹果式键盘 · 直接输入公式": "標準 · Apple風キーパッド · すぐに入力可能",
    "专业模式 · 科学键盘 · 直接输入公式": "関数 · 拡張キーパッド · すぐに入力可能",
    "复制结果": "結果をコピー", "已复制": "コピー済み", "打开历史记录": "履歴を開く",
    "关闭历史记录": "履歴を閉じる", "历史记录": "履歴", "清空历史记录": "履歴を消去",
    "双击结果可重新使用": "結果をダブルクリックして再利用",
    "外观主题": "外観テーマ", "主题与文字会同步切换，布局和字号保持不变": "配色と文字色を同時に切替。配置と文字サイズは変わりません",
    "配色预览依次显示背景、卡片、按钮、强调色与文字色。": "背景、カード、ボタン、アクセント、文字色を表示します。",
    "展开主题列表": "テーマを展開", "收回主题列表": "テーマを折りたたむ", "当前": "選択中",
    "白色文字": "白文字", "黑色文字": "黒文字", "配色参考": "配色参照",
    "程序语言与刷新时区": "言語と表示タイムゾーン", "程序默认语言": "表示言語",
    "刷新显示时区": "表示タイムゾーン", "可使用中文城市、国家/地区或 IANA 名称搜索": "中国語の都市・地域名または IANA 名で検索できます",
    "所选时区当前时间：": "選択タイムゾーン：",
    "自动刷新": "自動更新", "货币与虚拟币使用独立分钟间隔": "通貨と暗号資産の更新間隔を別々に設定",
    "启用自动刷新": "自動更新を有効にする", "最小化后继续按设定时间刷新": "最小化中も更新する",
    "启动与关闭": "起動と終了", "曜衡只运行一个窗口；重复启动会唤醒现有窗口": "曜衡は1つのウィンドウのみ起動し、再起動時は既存画面を表示します",
    "默认启动页面": "起動ページ", "点击关闭按钮": "閉じるボタンの動作",
    "记住上次打开的页面": "最後のページを記憶", "记住窗口大小和位置": "画面サイズと位置を記憶",
    "点击 × 时最小化到任务栏": "×でタスクバーへ最小化", "点击 × 时彻底退出软件": "×で完全終了",
    "模式、角度、历史记录与复制格式": "モード、角度、履歴、コピー形式", "默认模式": "既定モード",
    "角度模式": "角度モード", "复制结果格式": "コピー形式", "纯数字": "数値のみ",
    "带千位分隔符": "桁区切り付き", "完整算式与结果": "式と結果",
    "记住上次标准/专业模式": "前回のモードを記憶", "退出后保留历史记录": "終了後も履歴を保持",
    "历史记录条数": "履歴件数", "应用与缓存位置": "アプリとキャッシュの場所",
    "绿色版可整体迁移，也可单独指定数据目录": "ポータブル版全体の移動、またはデータ保存先を指定できます",
    "应用文件夹": "アプリフォルダー", "缓存与行情数据文件夹": "キャッシュ・市場データフォルダー",
    "打开文件夹": "フォルダーを開く", "迁移应用与数据…": "アプリとデータを移動…",
    "更改缓存目录…": "キャッシュ先を変更…", "应用与相关数据全部放在同一文件夹": "アプリとデータを同じフォルダーに保存",
    "API 接入": "API接続", "启用本机 API": "ローカルAPIを有効化", "固定地址  127.0.0.1": "固定アドレス  127.0.0.1",
    "端口": "ポート", "生成令牌": "トークン生成", "轮换令牌": "トークン更新", "连接测试": "接続テスト",
    "一次性令牌": "一回限りのトークン", "复制": "コピー", "软件更新": "ソフトウェア更新",
    "检查更新": "更新を確認", "下载并升级": "ダウンロードして更新", "缓存与设置管理": "キャッシュと設定",
    "限制缓存占用、清理缓存以及导入导出设置": "キャッシュ上限、消去、設定の入出力",
    "缓存上限 MB（0 为不限制）": "キャッシュ上限 MB（0=無制限）", "重新统计": "再計算", "清理缓存": "キャッシュ消去",
    "导出设置…": "設定を書き出す…", "导入设置…": "設定を読み込む…", "恢复默认设置": "初期設定に戻す",
    "立即退出曜衡": "曜衡を終了", "下一主题": "次のテーマ", "切换下一个主题": "次のテーマ",
    "当前版本": "現在のバージョン", "尚未检查更新": "未確認", "正在从 GitHub 检查最新正式版本…": "GitHubの最新版を確認中…",
    "正在统计…": "計算中…", "当前缓存：": "現在のキャッシュ：", "正在准备网络连接…": "ネットワークを準備中…",
    "等待首次连接": "初回接続待ち", "正在准备联网": "接続準備中", "正在重新连接": "再接続中", "重新连接网络": "再接続",
    "已联网": "オンライン", "当前网络连接成功！": "ネットワーク接続済み", "当前网络连接失败！": "ネットワークに接続できません",
    "部分数据已更新": "一部データを更新", "汇率已联网": "レート接続済み", "刷新汇率": "レート更新", "刷新": "更新",
    "最新刷新：": "最終更新：", "等待联网": "接続待ち", "货币换算": "通貨換算", "虚拟币换算": "暗号資産換算",
    "货币市场": "通貨市場", "虚拟币市场": "暗号資産市場", "三端金额联动换算": "3通貨連動換算",
    "代码": "コード", "名称": "名称", "换算数量": "換算額", "地区": "地域", "币种": "銘柄",
    "收藏": "お気に入り", "置顶": "固定", "搜索": "検索", "参考币种": "基準通貨", "参考币数额": "基準額",
    "参考数额": "基準額", "计价货币": "表示通貨", "默认顺序": "既定順", "换算模式": "換算モード",
    "来源": "ソース", "支付": "支払", "平台": "プラットフォーム", "跨币结算法币": "仲介法定通貨",
    "结算支付": "決済方法", "主货币": "主通貨", "设为主币": "主通貨に設定", "设为主币并换算": "主通貨にして換算",
    "当前输入": "入力中", "普通汇率": "市場レート", "C2C 按金额": "金額別C2C", "不限支付方式": "支払方法指定なし",
    "自动": "自動", "实时": "リアルタイム", "含缓存": "キャッシュ", "正在刷新…": "更新中…", "正在加载…": "読み込み中…",
    "暂无联网行情": "オンライン相場なし", "时间未知": "時刻不明", "汇率": "レート", "价格": "価格",
    "最高": "高値", "最低": "安値", "1日": "1日", "7日": "7日", "30日": "30日", "90日": "90日",
    "展开全部": "すべて表示", "套": "件",
}


# Complete the live/status/dialog vocabulary separately from the compact
# dictionaries above.  Phrase-level entries are intentionally preferred over
# individual-character replacement so financial wording stays unambiguous.
_EN.update({
    "A  金额": "A  Amount", "B  金额": "B  Amount", "C  金额": "C  Amount",
    "⇅  交换 A / B": "⇅  Swap A / B", "⇅  交换 B / C": "⇅  Swap B / C",
    "已按 A 端输入同步换算另外两端": "Updated the other two from side A",
    "已按 B 端输入同步换算另外两端": "Updated the other two from side B",
    "已按 C 端输入同步换算另外两端": "Updated the other two from side C",
    "亚洲": "Asia", "欧洲": "Europe", "非洲": "Africa", "北美洲": "North America",
    "南美洲": "South America", "大洋洲": "Oceania", "国际/其他": "International / Other",
    "已关闭；固定地址": "Disabled; fixed address",
    "不会监听外网": "never listens on external interfaces",
    "套  ▾": "themes  ▾",
    "不限支付方式（平台未提供列表）": "Any payment method (platform list unavailable)",
    "支持 +  −  ×  ÷  %  ( )；任意一端计算后另外两端同步更新": "Supports + − × ÷ % and parentheses; calculate on any side to update the other two",
    "支持 +  −  ×  ÷  % 和括号；按 Enter 或点击别处计算。": "Supports + − × ÷ % and parentheses; press Enter or click elsewhere to calculate.",
    "支持全球货币 A/B/C 三端联动；金额框可直接计算加减乘除、除余和括号。": "Three-way global currency conversion; amount fields accept arithmetic, modulo, and parentheses.",
    "法币与虚拟币可自由组合为 A/B/C 三端；金额框可直接输入基础算式。": "Mix fiat and crypto across A/B/C; amount fields accept basic expressions.",
    "在任意一端输入金额或算式，按回车、= 或点击输入框外完成计算与换算": "Enter an amount or expression on any side; press Enter, =, or click outside to calculate and convert",
    "法币与虚拟币按实际输入金额查询 C2C；虚拟币之间经所选法币执行双段参考。": "C2C uses the entered amount; crypto pairs use two legs through the selected fiat.",
    "使用公开市场与法币参考汇率同步换算；仅供参考，不执行购买或交易。": "Uses public market and fiat reference rates; reference only, with no purchase or trade.",
    "非 C2C · 公开现货/市场行情与法币参考汇率，不代表任何平台可直接成交": "Non-C2C · Public spot/market and fiat reference rates; not an executable platform quote",
    "C2C 页面不使用普通虚拟币行情降级；法币↔虚拟币查单广告，虚拟币↔虚拟币经结算法币双段查询。": "C2C never falls back to ordinary crypto rates; fiat/crypto checks one ad and crypto/crypto uses two fiat-settlement legs.",
    "公开行情仅供换算参考，不代表可直接成交。": "Public market data is for conversion reference only and is not directly executable.",
    "普通行情仅供参考，不代表任何单广告可成交。": "Ordinary market data is reference only and does not represent an executable ad.",
    "最低展示价与本金额匹配价分开展示；不保证成交。": "Lowest displayed and amount-matched prices are separate; execution is not guaranteed.",
    "最低展示价与本金额匹配价不会混用。": "Lowest displayed and amount-matched prices are never mixed.",
    "最低展示价不会冒充本金额可成交价": "The lowest display price is never presented as an executable price for this amount",
    "已有广告，但本金额未命中单广告范围；未拿最低价冒充可成交价。": "Ads exist, but this amount matches none of their limits; the lowest price was not misrepresented as executable.",
    "当前输入 · 同步换算其他六项": "Current input · Updating the other six",
    "请在任意一个货币金额框输入金额或算式。": "Enter an amount or expression in any currency field.",
    "金额算式有误：": "Invalid amount expression: ",
    "算式结果：": "Expression result: ",
    "等待汇率数据。": "Waiting for rate data.",
    "报价时间：": "Quote time: ",
    "等待汇率": "Waiting for rates",
    "正在刷新普通参考汇率与 C2C 依赖数据；旧一轮结果不会覆盖新输入。": "Refreshing reference rates and C2C dependencies; older results cannot overwrite newer input.",
    "正在刷新公开市场与法币参考汇率；旧一轮结果不会覆盖新输入。": "Refreshing public-market and fiat reference rates; older results cannot overwrite newer input.",
    "刷新失败；若有可信缓存仍会标明缓存状态。": "Refresh failed; trusted cached data remains clearly labeled.",
    "正在获取 C2C 双段本金额匹配价": "Fetching the two-leg C2C price for this amount",
    "正在获取 C2C 本金额匹配价": "Fetching the C2C price for this amount",
    "页面打开后获取 C2C 报价": "C2C quote will load when the page opens",
    "页面不可见时不持续刷新 C2C。": "C2C is not continuously refreshed while this page is hidden.",
    "普通汇率 · 实时": "Market rate · Live",
    "普通汇率 · 缓存": "Market rate · Cached",
    "普通汇率暂不可用": "Market rate unavailable",
    "法币参考汇率 · 缓存": "Fiat reference rate · Cached",
    "法币参考汇率": "Fiat reference rate",
    "普通行情降级": "Ordinary market fallback",
    "非 C2C 可成交价": "Not an executable C2C price",
    "缓存（新鲜）": "Cache (fresh)",
    "缓存（宽限）": "Cache (grace)",
    "失败缓存": "Failure cache",
    "状态未知": "Status unknown",
    "等待换算": "Waiting to convert",
    "平台未返回可用报价。": "The platform returned no usable quote.",
    "C2C 报价服务未配置": "C2C quote service is not configured",
    "C2C 报价暂不可用": "C2C quote is temporarily unavailable",
    "C2C 双段报价暂不可用": "Two-leg C2C quote is temporarily unavailable",
    "C2C 请求已取消": "C2C request cancelled",
    "C2C 请求受限，请稍后重试": "C2C request is rate-limited; try again later",
    "C2C 暂停请求，等待服务恢复": "C2C requests paused while the service recovers",
    "OKX 官方 P2P API 未配置或无权限": "The official OKX P2P API is not configured or permitted",
    "金额越界 · 无本金额匹配价": "Outside ad limits · No matching price for this amount",
    "货币参考表": "Currency Reference Table",
    "主流虚拟币行情": "Major Crypto Markets",
    "货币行情趋势": "Currency Trends",
    "虚拟币行情趋势": "Crypto Trends",
    "全球货币周期趋势与多币种计价将在这里同步呈现。": "Global currency trends and multi-currency quotes are shown here.",
    "虚拟币批量行情、周期趋势与多币种计价将在这里同步呈现。": "Crypto market snapshots, trends, and multi-currency quotes are shown here.",
    "双击币种查看趋势；计价货币、时间周期和表格顺序均可自由切换。": "Double-click an asset for its trend; quote currency, period, and table order are configurable.",
    "选择币种后加载趋势行情": "Select an asset to load its trend",
    "鼠标移入图表可查看具体时点": "Hover over the chart for exact timestamps",
    "日行情": "-day trend",
    "正在加载": "Loading",
    "的": " ",
    "参考表当前按": "Reference table uses",
    "换算为列表中的币种数量": "to calculate the listed asset amounts",
    "参考数额输入有误：": "Invalid reference amount: ",
    "参考币数额输入有误：": "Invalid reference amount: ",
    "请输入有效金额或算式，并为 A、B、C 三端选择币种": "Enter a valid amount or expression and select assets for A, B, and C",
    "端金额输入有误：": " amount is invalid: ",
    "货币与虚拟币使用独立分钟间隔": "Currencies and crypto use separate minute intervals",
    "供本机微信、QQ、Telegram 等机器人桥接；曜衡本身只监听回环地址": "For local WeChat, QQ, Telegram, and other bot bridges; Yaoheng listens on loopback only",
    "明文令牌只在生成或轮换当次显示，请立即保存。支持": "The plain token is shown only when generated or rotated; save it now. Supports",
    "不包含下单接口。": "No order-placement endpoint is included.",
    "从曜衡官方 GitHub Release 检查更新；安装包下载后必须通过 SHA-256 校验": "Checks official Yaoheng GitHub Releases; installers must pass SHA-256 verification",
    "升级时会打开覆盖安装程序并安全退出曜衡；原设置、收藏、历史和缓存默认保留。": "Upgrade opens the overwrite installer and safely exits Yaoheng; settings, favorites, history, and cache are preserved.",
    "校验通过后会打开覆盖安装程序并退出当前曜衡；用户设置和缓存默认保留。": "After verification, the overwrite installer opens and this instance exits; settings and cache are preserved.",
    "将下载并校验曜衡": "Yaoheng will download and verify",
    "安装包。": " installer.",
    "是否继续？": "Continue?",
    "可下载新的正式版本": "A new stable version is available",
    "当前没有可下载的新版本": "No newer version is available",
    "更新操作正在进行，请稍候。": "An update operation is already running; please wait.",
    "更新操作没有返回有效结果。": "The update operation returned no valid result.",
    "请先检查更新。": "Check for updates first.",
    "连接测试失败；本机 API 未确认可用。": "Connection test failed; the local API could not be confirmed.",
    "连接测试失败：本机 API 当前未运行。": "Connection test failed: the local API is not running.",
    "连接测试失败：本机监听未返回有效健康状态。": "Connection test failed: the local listener returned no valid health status.",
    "已有 API 操作正在进行，请稍候。": "An API operation is already running; please wait.",
    "正在安全生成令牌…": "Securely generating a token…",
    "正在测试本机 API 连接…": "Testing the local API connection…",
    "没有可复制的一次性令牌，请先生成或轮换。": "No one-time token is available; generate or rotate one first.",
    "令牌已复制到剪贴板；请保存到可信密码管理器。": "Token copied; save it in a trusted password manager.",
    "无法复制令牌，请手动选择保存。": "Could not copy the token; select and save it manually.",
    "已有本机 API 令牌；如需新令牌，请使用“轮换令牌”。": "A local API token already exists; use Rotate Token to create a new one.",
    "新令牌仅本次显示，请立即保存。": "The new token is shown once; save it now.",
    "缓存已清理。当前会话中的汇率仍可继续使用。": "Cache cleared. Rates already loaded in this session remain available.",
    "确定清理汇率与行情缓存吗？": "Clear the rate and market cache?",
    "应用设置、收藏和置顶不会被删除。": "App settings, favorites, and pinned items will not be deleted.",
    "确定恢复所有默认设置吗？": "Restore all default settings?",
    "收藏、置顶和计算历史也会重置。": "Favorites, pins, and calculation history will also be reset.",
    "默认设置已写入，重新启动曜衡后全部生效。": "Defaults saved; restart Yaoheng to apply everything.",
    "设置已导入，重新启动曜衡后全部生效。": "Settings imported; restart Yaoheng to apply everything.",
    "当前设置无法写入应用文件夹": "Settings cannot be written to the app folder",
    "无法写入应用设置文件；本次更改仅在当前会话有效。请检查应用文件夹权限。": "Cannot write app settings; this change applies only to the current session. Check folder permissions.",
    "已恢复默认设置": "Defaults restored",
    "缓存已清理": "Cache cleared",
    "导出完成": "Export complete",
    "导入完成": "Import complete",
    "导出失败": "Export failed",
    "导入失败": "Import failed",
    "迁移完成": "Move complete",
    "迁移失败": "Move failed",
    "无需迁移": "No move needed",
    "无法打开文件夹": "Cannot open folder",
    "刷新、启动、计算器与便携数据管理": "Refresh, startup, calculator, and portable data management",
    "套全新配色；每套独立指定白色或黑色文字": " new palettes; each explicitly uses white or black text",
    "展开全部": "Show all",
    "收回主题列表": "Collapse themes",
    "当前：": "Current: ",
    "下一套：": "Next: ",
    "切换下一个主题": "Switch to next theme",
})

_JA.update({
    "A  金额": "A  金額", "B  金额": "B  金額", "C  金额": "C  金額",
    "⇅  交换 A / B": "⇅  A / Bを交換", "⇅  交换 B / C": "⇅  B / Cを交換",
    "已按 A 端输入同步换算另外两端": "A側の入力から他の2欄を更新しました",
    "已按 B 端输入同步换算另外两端": "B側の入力から他の2欄を更新しました",
    "已按 C 端输入同步换算另外两端": "C側の入力から他の2欄を更新しました",
    "亚洲": "アジア", "欧洲": "ヨーロッパ", "非洲": "アフリカ", "北美洲": "北米",
    "南美洲": "南米", "大洋洲": "オセアニア", "国际/其他": "国際・その他",
    "已关闭；固定地址": "無効；固定アドレス",
    "不会监听外网": "外部インターフェースでは待受しません",
    "不限支付方式（平台未提供列表）": "支払方法指定なし（一覧未提供）",
    "支持 +  −  ×  ÷  %  ( )；任意一端计算后另外两端同步更新": "+ − × ÷ % と括弧に対応。どの欄で計算しても他の2欄を更新します",
    "支持 +  −  ×  ÷  % 和括号；按 Enter 或点击别处计算。": "+ − × ÷ % と括弧に対応。Enterまたは欄外クリックで計算します。",
    "支持全球货币 A/B/C 三端联动；金额框可直接计算加减乘除、除余和括号。": "世界の通貨をA/B/Cで連動換算。金額欄では四則演算、剰余、括弧を使えます。",
    "法币与虚拟币可自由组合为 A/B/C 三端；金额框可直接输入基础算式。": "法定通貨と暗号資産をA/B/Cで自由に組み合わせ、金額欄に式を入力できます。",
    "在任意一端输入金额或算式，按回车、= 或点击输入框外完成计算与换算": "任意の欄に金額や式を入力し、Enter、=、または欄外クリックで計算・換算します",
    "法币与虚拟币按实际输入金额查询 C2C；虚拟币之间经所选法币执行双段参考。": "C2Cは入力金額で照会し、暗号資産間は選択した法定通貨を介した2段階参照です。",
    "使用公开市场与法币参考汇率同步换算；仅供参考，不执行购买或交易。": "公開市場と法定通貨の参考レートで換算します。参照専用で売買は行いません。",
    "非 C2C · 公开现货/市场行情与法币参考汇率，不代表任何平台可直接成交": "非C2C · 公開スポット・市場・法定通貨の参考レート。取引可能価格ではありません",
    "C2C 页面不使用普通虚拟币行情降级；法币↔虚拟币查单广告，虚拟币↔虚拟币经结算法币双段查询。": "C2C画面は通常相場へフォールバックしません。法定通貨↔暗号資産は単一広告、暗号資産間は2段階で照会します。",
    "公开行情仅供换算参考，不代表可直接成交。": "公開相場は換算の参考専用で、取引可能価格ではありません。",
    "普通行情仅供参考，不代表任何单广告可成交。": "通常相場は参考専用で、個別広告の取引可能価格ではありません。",
    "最低展示价与本金额匹配价分开展示；不保证成交。": "表示最安値と金額一致価格は別表示です。約定は保証されません。",
    "最低展示价与本金额匹配价不会混用。": "表示最安値と金額一致価格は混同しません。",
    "已有广告，但本金额未命中单广告范围；未拿最低价冒充可成交价。": "広告はありますが、この金額は範囲外です。最安値を取引可能価格として表示していません。",
    "当前输入 · 同步换算其他六项": "入力中 · 他の6項目を同期換算",
    "请在任意一个货币金额框输入金额或算式。": "いずれかの金額欄に金額または式を入力してください。",
    "金额算式有误：": "金額式が無効です：",
    "算式结果：": "式の結果：",
    "等待汇率数据。": "レートデータ待ちです。",
    "报价时间：": "見積時刻：",
    "等待汇率": "レート待ち",
    "正在刷新普通参考汇率与 C2C 依赖数据；旧一轮结果不会覆盖新输入。": "参考レートとC2C依存データを更新中。古い結果は新しい入力を上書きしません。",
    "正在刷新公开市场与法币参考汇率；旧一轮结果不会覆盖新输入。": "公開市場と法定通貨レートを更新中。古い結果は新しい入力を上書きしません。",
    "刷新失败；若有可信缓存仍会标明缓存状态。": "更新に失敗しました。信頼できるキャッシュは状態を明記して使用します。",
    "正在获取 C2C 双段本金额匹配价": "この金額の2段階C2C価格を取得中",
    "正在获取 C2C 本金额匹配价": "この金額のC2C価格を取得中",
    "页面打开后获取 C2C 报价": "画面を開いた時にC2C見積を取得",
    "页面不可见时不持续刷新 C2C。": "非表示中はC2Cを継続更新しません。",
    "普通汇率 · 实时": "市場レート · リアルタイム",
    "普通汇率 · 缓存": "市場レート · キャッシュ",
    "普通汇率暂不可用": "市場レートを利用できません",
    "法币参考汇率 · 缓存": "法定通貨参考レート · キャッシュ",
    "法币参考汇率": "法定通貨参考レート",
    "普通行情降级": "通常相場フォールバック",
    "非 C2C 可成交价": "C2C取引可能価格ではありません",
    "缓存（新鲜）": "キャッシュ（新鮮）",
    "缓存（宽限）": "キャッシュ（猶予）",
    "失败缓存": "失敗キャッシュ",
    "状态未知": "状態不明",
    "等待换算": "換算待ち",
    "平台未返回可用报价。": "プラットフォームから有効な見積が返りませんでした。",
    "C2C 报价服务未配置": "C2C見積サービスが未設定です",
    "C2C 报价暂不可用": "C2C見積を一時的に利用できません",
    "C2C 双段报价暂不可用": "2段階C2C見積を一時的に利用できません",
    "C2C 请求已取消": "C2Cリクエストをキャンセルしました",
    "C2C 请求受限，请稍后重试": "C2Cリクエストが制限されています。後で再試行してください",
    "C2C 暂停请求，等待服务恢复": "サービス回復までC2Cリクエストを停止中",
    "OKX 官方 P2P API 未配置或无权限": "OKX公式P2P APIが未設定または権限なしです",
    "金额越界 · 无本金额匹配价": "広告範囲外 · この金額に一致する価格なし",
    "货币参考表": "通貨参考表",
    "主流虚拟币行情": "主要暗号資産市場",
    "全球货币周期趋势与多币种计价将在这里同步呈现。": "世界の通貨トレンドと複数通貨表示をここに表示します。",
    "虚拟币批量行情、周期趋势与多币种计价将在这里同步呈现。": "暗号資産の相場、トレンド、複数通貨表示をここに表示します。",
    "双击币种查看趋势；计价货币、时间周期和表格顺序均可自由切换。": "銘柄をダブルクリックしてトレンドを表示。表示通貨、期間、並び順を変更できます。",
    "选择币种后加载趋势行情": "銘柄を選択してトレンドを読み込み",
    "鼠标移入图表可查看具体时点": "グラフにカーソルを合わせて時刻を表示",
    "日行情": "日トレンド",
    "参考数额输入有误：": "基準額が無効です：",
    "参考币数额输入有误：": "基準額が無効です：",
    "请输入有效金额或算式，并为 A、B、C 三端选择币种": "有効な金額または式を入力し、A/B/Cの銘柄を選択してください",
    "端金额输入有误：": "側の金額が無効です：",
    "供本机微信、QQ、Telegram 等机器人桥接；曜衡本身只监听回环地址": "ローカルのWeChat、QQ、Telegram等のボット連携用。曜衡はループバックのみ待受します",
    "明文令牌只在生成或轮换当次显示，请立即保存。支持": "平文トークンは生成・更新時に一度だけ表示されます。すぐ保存してください。対応：",
    "不包含下单接口。": "注文APIは含みません。",
    "从曜衡官方 GitHub Release 检查更新；安装包下载后必须通过 SHA-256 校验": "曜衡公式GitHub Releaseを確認し、インストーラーをSHA-256で検証します",
    "升级时会打开覆盖安装程序并安全退出曜衡；原设置、收藏、历史和缓存默认保留。": "上書きインストーラーを開いて安全に終了します。設定、お気に入り、履歴、キャッシュは保持されます。",
    "可下载新的正式版本": "新しい正式版をダウンロードできます",
    "当前没有可下载的新版本": "ダウンロード可能な新バージョンはありません",
    "更新操作正在进行，请稍候。": "更新処理中です。しばらくお待ちください。",
    "更新操作没有返回有效结果。": "更新処理から有効な結果が返りませんでした。",
    "请先检查更新。": "先に更新を確認してください。",
    "连接测试失败；本机 API 未确认可用。": "接続テストに失敗し、ローカルAPIを確認できませんでした。",
    "连接测试失败：本机 API 当前未运行。": "接続テスト失敗：ローカルAPIは停止中です。",
    "已有 API 操作正在进行，请稍候。": "API処理中です。しばらくお待ちください。",
    "正在安全生成令牌…": "安全にトークンを生成中…",
    "正在测试本机 API 连接…": "ローカルAPI接続をテスト中…",
    "没有可复制的一次性令牌，请先生成或轮换。": "コピーできるトークンがありません。生成または更新してください。",
    "令牌已复制到剪贴板；请保存到可信密码管理器。": "トークンをコピーしました。信頼できるパスワード管理ツールへ保存してください。",
    "无法复制令牌，请手动选择保存。": "トークンをコピーできません。手動で選択して保存してください。",
    "缓存已清理。当前会话中的汇率仍可继续使用。": "キャッシュを消去しました。このセッションで読み込んだレートは引き続き使えます。",
    "确定清理汇率与行情缓存吗？": "レートと市場キャッシュを消去しますか？",
    "应用设置、收藏和置顶不会被删除。": "設定、お気に入り、固定項目は削除されません。",
    "确定恢复所有默认设置吗？": "すべて初期設定に戻しますか？",
    "收藏、置顶和计算历史也会重置。": "お気に入り、固定、計算履歴もリセットされます。",
    "默认设置已写入，重新启动曜衡后全部生效。": "初期設定を保存しました。曜衡の再起動後に反映されます。",
    "设置已导入，重新启动曜衡后全部生效。": "設定を読み込みました。曜衡の再起動後に反映されます。",
    "无法写入应用设置文件；本次更改仅在当前会话有效。请检查应用文件夹权限。": "設定ファイルに書き込めません。この変更は現在のセッションのみ有効です。フォルダー権限を確認してください。",
    "已恢复默认设置": "初期設定に戻しました",
    "缓存已清理": "キャッシュを消去しました",
    "导出完成": "書き出し完了", "导入完成": "読み込み完了",
    "导出失败": "書き出し失敗", "导入失败": "読み込み失敗",
    "迁移完成": "移動完了", "迁移失败": "移動失敗", "无需迁移": "移動不要",
    "无法打开文件夹": "フォルダーを開けません",
    "刷新、启动、计算器与便携数据管理": "更新、起動、電卓、ポータブルデータ管理",
    "套全新配色；每套独立指定白色或黑色文字": "種類の新配色。各テーマで白または黒文字を明示",
    "下一套：": "次：",
})


_TRADITIONAL_CHARACTERS = str.maketrans({
    "计": "計", "算": "算", "器": "器", "兑": "兌", "换": "換", "货": "貨", "币": "幣",
    "拟": "擬", "设": "設", "置": "置", "显": "顯", "示": "示", "时": "時", "区": "區",
    "语": "語", "简": "簡", "体": "體", "默": "默", "认": "認", "刷": "刷", "新": "新",
    "启": "啟", "动": "動", "关": "關", "闭": "閉", "记": "記", "录": "錄", "历": "歷",
    "结": "結", "果": "果", "复": "複", "制": "製", "开": "開", "专": "專", "业": "業",
    "标": "標", "准": "準", "应": "應", "用": "用", "缓": "緩", "存": "存", "数": "數",
    "据": "據", "导": "導", "入": "入", "出": "出", "径": "徑", "网": "網", "络": "絡",
    "连": "連", "线": "線", "额": "額", "选": "選", "择": "擇", "输": "輸", "页": "頁",
    "面": "面", "颜": "顏", "色": "色", "块": "塊", "当": "當", "前": "前", "张": "張",
    "扩": "擴", "展": "展", "收": "收", "回": "回", "与": "與", "为": "為", "后": "後",
    "续": "續", "进": "進", "行": "行", "实": "實", "现": "現", "长": "長", "间": "間",
    "宽": "寬", "条": "條", "万": "萬", "仅": "僅", "软": "軟", "件": "件", "这": "這",
    "个": "個", "从": "從", "双": "雙", "击": "擊", "无": "無", "发": "發", "现": "現",
    "对": "對", "话": "話", "阶": "階", "过": "過", "滤": "濾", "签": "簽", "约": "約",
    "户": "戶", "现": "現", "盘": "盤", "树": "樹", "东": "東", "风": "風", "门": "門",
    "题": "題", "务": "務", "图": "圖", "损": "損", "涨": "漲", "跌": "跌", "价": "價",
    "码": "碼", "称": "稱", "类": "類", "别": "別", "国": "國", "华": "華", "亚": "亞",
    "洲": "洲", "纽": "紐", "尔": "爾", "韩": "韓", "湾": "灣", "伦": "倫", "敦": "敦",
    "尔": "爾", "这": "這", "删": "刪", "除": "除", "储": "儲", "单": "單", "击": "擊",
})

_TRADITIONAL_UI_PHRASES: Mapping[str, str] = {
    "軟件": "軟體", "設置": "設定", "默認": "預設", "程序": "程式",
    "應用文件夾": "應用程式資料夾", "文件夾": "資料夾", "文件": "檔案",
    "數據": "資料", "緩存": "快取", "網絡": "網路", "窗口": "視窗",
    "接口": "介面", "鼠標": "滑鼠", "點擊": "點選", "回車": "Enter",
    "搜索": "搜尋", "置頂": "釘選", "收藏": "我的最愛", "刷新": "重新整理",
    "視頻": "影片", "信息": "資訊", "打印": "列印", "用戶": "使用者",
}


@lru_cache(maxsize=4096)
def _to_traditional(text: str) -> str:
    converted = text
    try:
        mapper = ctypes.windll.kernel32.LCMapStringEx
        mapper.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int,
            ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_long,
        ]
        mapper.restype = ctypes.c_int
        size = mapper("zh-Hant", 0x04000000, text, len(text), None, 0, None, None, 0)
        if size > 0:
            buffer = ctypes.create_unicode_buffer(size)
            written = mapper("zh-Hant", 0x04000000, text, len(text), buffer, size, None, None, 0)
            if written > 0:
                converted = buffer.value
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    converted = converted.translate(_TRADITIONAL_CHARACTERS)
    for source, replacement in _TRADITIONAL_UI_PHRASES.items():
        converted = converted.replace(source, replacement)
    return converted


def normalize_language(value: object) -> str:
    candidate = str(value or "").strip()
    aliases = {
        "zh-cn": "zh_CN", "zh_cn": "zh_CN", "zh-hans": "zh_CN", "zh": "zh_CN",
        "zh-tw": "zh_TW", "zh_tw": "zh_TW", "zh-hant": "zh_TW",
        "en": "en_US", "en-us": "en_US", "en_us": "en_US",
        "ja": "ja_JP", "ja-jp": "ja_JP", "ja_jp": "ja_JP",
    }
    return aliases.get(candidate.casefold(), candidate if candidate in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE)


def get_language() -> str:
    return _language


def set_language(language: object) -> str:
    global _language
    _language = normalize_language(language)
    for variable in tuple(_display_variables):
        try:
            variable.retranslate()
        except Exception:
            pass
    return _language


def _replace_catalog(text: str, catalog: Mapping[str, str]) -> str:
    if text in catalog:
        return catalog[text]
    translated = text
    # Longest-first makes dynamic sentences predictable (e.g. "当前版本 …").
    for source in sorted(catalog, key=len, reverse=True):
        if len(source) >= 2 and source in translated:
            translated = translated.replace(source, catalog[source])
    return translated


def tr(value: object, language: str | None = None) -> str:
    text = str(value)
    selected = normalize_language(language or _language)
    if selected == "zh_CN" or not text:
        return text
    if selected == "zh_TW":
        return _to_traditional(text)
    return _replace_catalog(text, _EN if selected == "en_US" else _JA)


def format_datetime(value: datetime, language: str | None = None) -> str:
    selected = normalize_language(language or _language)
    if selected == "en_US":
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if selected == "ja_JP":
        return value.strftime("%Y年%m月%d日 %H:%M:%S")
    if selected == "zh_TW":
        return value.strftime("%Y年%m月%d日 %H:%M:%S")
    return value.strftime("%Y年%m月%d日 %H:%M:%S")


_ASSET_NAMES_EN: Mapping[str, str] = {
    "CNY": "Chinese Yuan", "CNH": "Offshore Chinese Yuan", "USD": "US Dollar",
    "EUR": "Euro", "JPY": "Japanese Yen", "HKD": "Hong Kong Dollar",
    "GBP": "British Pound", "AUD": "Australian Dollar", "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc", "SGD": "Singapore Dollar", "KRW": "South Korean Won",
    "TWD": "New Taiwan Dollar", "NZD": "New Zealand Dollar", "THB": "Thai Baht",
    "INR": "Indian Rupee", "RUB": "Russian Ruble", "BRL": "Brazilian Real",
    "MXN": "Mexican Peso", "AED": "UAE Dirham", "SAR": "Saudi Riyal",
    "BTC": "Bitcoin", "ETH": "Ethereum", "USDT": "Tether", "BNB": "BNB",
    "USDC": "USD Coin", "XRP": "XRP", "SOL": "Solana", "DOGE": "Dogecoin",
    "ADA": "Cardano", "BCH": "Bitcoin Cash", "LINK": "Chainlink", "LTC": "Litecoin",
}
_ASSET_NAMES_JA: Mapping[str, str] = {
    "CNY": "中国人民元", "CNH": "オフショア人民元", "USD": "米ドル", "EUR": "ユーロ",
    "JPY": "日本円", "HKD": "香港ドル", "GBP": "英ポンド", "AUD": "豪ドル",
    "CAD": "カナダドル", "CHF": "スイスフラン", "SGD": "シンガポールドル",
    "KRW": "韓国ウォン", "TWD": "ニュー台湾ドル", "NZD": "ニュージーランドドル",
    "THB": "タイバーツ", "INR": "インドルピー", "RUB": "ロシアルーブル",
    "BRL": "ブラジルレアル", "MXN": "メキシコペソ", "AED": "UAEディルハム",
    "SAR": "サウジリヤル", "BTC": "ビットコイン", "ETH": "イーサリアム",
    "USDT": "テザー", "BNB": "BNB", "USDC": "USDコイン", "XRP": "XRP",
    "SOL": "ソラナ", "DOGE": "ドージコイン", "ADA": "カルダノ",
    "BCH": "ビットコインキャッシュ", "LINK": "チェーンリンク", "LTC": "ライトコイン",
}


def localized_asset_name(
    code: object,
    source_name: object = "",
    language: str | None = None,
) -> str:
    """Return a readable asset name without leaking Chinese labels into EN/JA UI."""

    normalized_code = str(code or "").strip().upper()
    source = str(source_name or normalized_code).strip()
    selected = normalize_language(language or _language)
    if selected == "zh_CN":
        return source
    if selected == "zh_TW":
        return _to_traditional(source)
    catalog = _ASSET_NAMES_EN if selected == "en_US" else _ASSET_NAMES_JA
    if normalized_code in catalog:
        return catalog[normalized_code]
    # Provider names that are already Latin-script remain useful.  A Chinese
    # provider label falls back to the stable asset code instead of appearing
    # untranslated in an English or Japanese interface.
    return source if not re.search(r"[\u3400-\u9fff]", source) else normalized_code


class DisplayStringVar:  # Replaced with a real Tk subclass by install_tk_localization().
    pass


_installed = False


def install_tk_localization(tk: Any, ttk: Any, messagebox: Any, filedialog: Any) -> type:
    """Install localized text widgets once and return DisplayStringVar."""

    global _installed, DisplayStringVar
    if _installed:
        return DisplayStringVar

    class _LocalizedTextMixin:
        _yaoheng_source_text: str | None = None
        _yaoheng_translation_guard = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if "text" in kwargs and kwargs["text"] is not None:
                self._yaoheng_source_text = str(kwargs["text"])
                kwargs["text"] = tr(self._yaoheng_source_text)
            super().__init__(*args, **kwargs)

        def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
            if isinstance(cnf, dict):
                kwargs = {**cnf, **kwargs}
                cnf = None
            if "text" in kwargs and not self._yaoheng_translation_guard:
                self._yaoheng_source_text = str(kwargs["text"])
                kwargs["text"] = tr(self._yaoheng_source_text)
            return super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)

        config = configure

        def _yaoheng_retranslate(self) -> None:
            if self._yaoheng_source_text is None:
                return
            self._yaoheng_translation_guard = True
            try:
                super().configure(text=tr(self._yaoheng_source_text))
            finally:
                self._yaoheng_translation_guard = False

    for name in ("Label", "Button", "Checkbutton", "Radiobutton", "Menubutton", "LabelFrame"):
        original = getattr(tk, name)
        localized = type(f"Localized{name}", (_LocalizedTextMixin, original), {})
        setattr(tk, name, localized)

    original_treeview = ttk.Treeview

    class LocalizedTreeview(original_treeview):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._yaoheng_heading_sources: dict[str, str] = {}
            super().__init__(*args, **kwargs)

        def heading(self, column: Any, option: Any = None, **kwargs: Any) -> Any:
            if "text" in kwargs:
                source = str(kwargs["text"])
                self._yaoheng_heading_sources[str(column)] = source
                kwargs["text"] = tr(source)
            return super().heading(column, option, **kwargs)

        def _yaoheng_retranslate(self) -> None:
            for column, source in self._yaoheng_heading_sources.items():
                super().heading(column, text=tr(source))

    ttk.Treeview = LocalizedTreeview

    original_string_var = tk.StringVar

    class _DisplayStringVar(original_string_var):
        __hash__ = object.__hash__

        def __init__(self, master: Any = None, value: Any = None, name: str | None = None) -> None:
            self._yaoheng_source_value = "" if value is None else str(value)
            super().__init__(master=master, value=tr(self._yaoheng_source_value), name=name)
            _display_variables.add(self)

        def set(self, value: Any) -> None:
            self._yaoheng_source_value = str(value)
            super().set(tr(self._yaoheng_source_value))

        def retranslate(self) -> None:
            super().set(tr(self._yaoheng_source_value))

        def source_value(self) -> str:
            return self._yaoheng_source_value

    DisplayStringVar = _DisplayStringVar

    for name in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askretrycancel"):
        original = getattr(messagebox, name, None)
        if not callable(original):
            continue

        def wrapper(title: Any, message: Any, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            return _original(tr(title), tr(message), *args, **kwargs)

        setattr(messagebox, name, wrapper)

    for name in ("askopenfilename", "asksaveasfilename", "askdirectory"):
        original = getattr(filedialog, name, None)
        if not callable(original):
            continue

        def dialog_wrapper(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            if "title" in kwargs:
                kwargs["title"] = tr(kwargs["title"])
            if "filetypes" in kwargs:
                kwargs["filetypes"] = [(tr(label), pattern) for label, pattern in kwargs["filetypes"]]
            return _original(*args, **kwargs)

        setattr(filedialog, name, dialog_wrapper)

    _installed = True
    return DisplayStringVar


def refresh_widget_tree(widget: Any) -> None:
    refresh = getattr(widget, "_yaoheng_retranslate", None)
    if callable(refresh):
        try:
            refresh()
        except Exception:
            pass
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        refresh_widget_tree(child)


_ZONE_ZH: Mapping[str, str] = {
    "UTC": "协调世界时",
    "Asia/Shanghai": "中国标准时间（上海/北京）",
    "Asia/Hong_Kong": "中国香港",
    "Asia/Macau": "中国澳门",
    "Asia/Taipei": "中国台湾（台北）",
    "Asia/Tokyo": "日本（东京）",
    "Asia/Seoul": "韩国（首尔）",
    "Asia/Singapore": "新加坡",
    "Asia/Bangkok": "泰国（曼谷）",
    "Asia/Kuala_Lumpur": "马来西亚（吉隆坡）",
    "Asia/Jakarta": "印度尼西亚（雅加达）",
    "Asia/Manila": "菲律宾（马尼拉）",
    "Asia/Kolkata": "印度（加尔各答）",
    "Asia/Dubai": "阿联酋（迪拜）",
    "Europe/London": "英国（伦敦）",
    "Europe/Paris": "法国（巴黎）",
    "Europe/Berlin": "德国（柏林）",
    "Europe/Rome": "意大利（罗马）",
    "Europe/Madrid": "西班牙（马德里）",
    "Europe/Moscow": "俄罗斯（莫斯科）",
    "America/New_York": "美国东部（纽约）",
    "America/Chicago": "美国中部（芝加哥）",
    "America/Denver": "美国山地（丹佛）",
    "America/Los_Angeles": "美国西部（洛杉矶）",
    "America/Toronto": "加拿大（多伦多）",
    "America/Vancouver": "加拿大（温哥华）",
    "America/Mexico_City": "墨西哥（墨西哥城）",
    "America/Sao_Paulo": "巴西（圣保罗）",
    "Australia/Sydney": "澳大利亚（悉尼）",
    "Australia/Melbourne": "澳大利亚（墨尔本）",
    "Australia/Perth": "澳大利亚（珀斯）",
    "Pacific/Auckland": "新西兰（奥克兰）",
    "Pacific/Honolulu": "美国夏威夷（檀香山）",
    "Africa/Cairo": "埃及（开罗）",
    "Africa/Johannesburg": "南非（约翰内斯堡）",
    "Africa/Lagos": "尼日利亚（拉各斯）",
    "Africa/Nairobi": "肯尼亚（内罗毕）",
}


def timezone_chinese_name(zone: str) -> str:
    if zone in _ZONE_ZH:
        return _ZONE_ZH[zone]
    parts = zone.replace("_", " ").split("/")
    regions = {
        "Africa": "非洲", "America": "美洲", "Antarctica": "南极洲", "Arctic": "北极",
        "Asia": "亚洲", "Atlantic": "大西洋", "Australia": "澳大利亚", "Europe": "欧洲",
        "Indian": "印度洋", "Pacific": "太平洋",
    }
    return " · ".join([regions.get(parts[0], parts[0]), *parts[1:]])


def timezone_display_name(zone: str, language: str | None = None) -> str:
    selected = normalize_language(language or _language)
    chinese = timezone_chinese_name(zone)
    if selected == "zh_CN":
        return chinese
    if selected == "zh_TW":
        return _to_traditional(chinese)
    # IANA identifiers remain the clearest localized name for EN/JA, while the
    # Chinese alias stays in the searchable index rather than cluttering UI.
    return zone.replace("_", " ").replace("/", " · ")


def timezone_search_text(zone: str) -> str:
    return f"{zone} {zone.replace('_', ' ')} {timezone_chinese_name(zone)}"


__all__ = [
    "DEFAULT_LANGUAGE", "DisplayStringVar", "LANGUAGE_LABELS", "SUPPORTED_LANGUAGES",
    "format_datetime", "get_language", "install_tk_localization", "localized_asset_name", "normalize_language",
    "refresh_widget_tree", "set_language", "timezone_chinese_name", "timezone_display_name",
    "timezone_search_text", "tr",
]
