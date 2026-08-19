"""Online fiat and cryptocurrency rates with a portable local cache."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException, localcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock, get_ident
from typing import Any

import requests


FIAT_API = "https://open.er-api.com/v6/latest/USD"
CRYPTO_API = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=cny&order=market_cap_desc&per_page=50&page=1"
    "&sparkline=false&price_change_percentage=24h"
)
BINANCE_API = "https://data-api.binance.vision/api/v3"
FRANKFURTER_API = "https://api.frankfurter.dev/v2/rates"
MANAGED_CACHE_PATTERNS = (
    "rates_cache.json", "rates_cache.tmp*", "chart_*.json", "chart_*.tmp*",
    "fiat_chart_*.json", "fiat_chart_*.tmp*",
)
MAX_CACHE_FILE_BYTES = 8 * 1024 * 1024
MAX_CHART_POINTS = 20_000
MAX_CHART_TIMESTAMP_MS = 32_503_680_000_000  # 3000-01-01 UTC; safely renderable on Windows.
MAX_CHART_VALUE = 1e300  # Leave headroom for chart padding without overflowing float coordinates.
MAX_FIAT_HISTORY_ROWS = 5_000
MAX_API_RATE_ENTRIES = 512
MAX_CRYPTO_ROWS = 200
BINANCE_CRYPTOS = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "BNB": "BNB", "XRP": "XRP", "USDC": "USDC",
    "SOL": "Solana", "TRX": "TRON", "DOGE": "Dogecoin", "ADA": "Cardano", "BCH": "Bitcoin Cash",
    "LINK": "Chainlink", "XLM": "Stellar", "LTC": "Litecoin", "AVAX": "Avalanche", "SUI": "Sui",
    "DOT": "Polkadot", "UNI": "Uniswap", "AAVE": "Aave", "NEAR": "NEAR Protocol", "ETC": "Ethereum Classic",
    "ICP": "Internet Computer", "FIL": "Filecoin", "ATOM": "Cosmos", "ALGO": "Algorand", "VET": "VeChain",
    "SHIB": "Shiba Inu", "PEPE": "Pepe", "ARB": "Arbitrum", "OP": "Optimism",
}
BINANCE_COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin", "XRP": "ripple",
    "USDC": "usd-coin", "SOL": "solana", "TRX": "tron", "DOGE": "dogecoin",
    "ADA": "cardano", "BCH": "bitcoin-cash", "LINK": "chainlink", "XLM": "stellar",
    "LTC": "litecoin", "AVAX": "avalanche-2", "SUI": "sui", "DOT": "polkadot",
    "UNI": "uniswap", "AAVE": "aave", "NEAR": "near", "ETC": "ethereum-classic",
    "ICP": "internet-computer", "FIL": "filecoin", "ATOM": "cosmos", "ALGO": "algorand",
    "VET": "vechain", "SHIB": "shiba-inu", "PEPE": "pepe", "ARB": "arbitrum",
    "OP": "optimism", "USDT": "tether",
}

CRYPTO_NAMES_ZH = {
    "BTC": "比特币", "ETH": "以太坊", "USDT": "泰达币", "BNB": "币安币",
    "USDC": "美元币", "XRP": "瑞波币", "SOL": "索拉纳", "TRX": "波场币",
    "FIGR_HELOC": "Figure 房屋净值信贷代币", "HYPE": "Hyperliquid 平台币",
    "DOGE": "狗狗币", "USDS": "Sky 美元稳定币", "RAIN": "Rain 平台币",
    "LEO": "LEO 平台币", "ZEC": "大零币", "WBT": "WhiteBIT 平台币",
    "XLM": "恒星币", "XMR": "门罗币", "LINK": "链克币", "ADA": "艾达币",
    "CC": "Canton 网络代币", "DAI": "戴稳定币", "BCH": "比特币现金",
    "USD1": "世界自由金融美元稳定币", "USDE": "Ethena 美元稳定币",
    "GRAM": "Gram 代币", "LTC": "莱特币", "USDG": "全球美元稳定币",
    "USYC": "Circle 收益型美元代币", "SUI": "Sui 代币", "HBAR": "哈希图币",
    "PYUSD": "PayPal 美元稳定币", "AVAX": "雪崩币", "CRO": "Cronos 平台币",
    "BUIDL": "贝莱德美元流动性基金代币", "NEAR": "NEAR 协议代币",
    "XAUT": "泰达黄金", "SHIB": "柴犬币", "UNI": "Uniswap 代币",
    "USDY": "Ondo 美元收益代币", "TAO": "Bittensor 代币",
    "WLFI": "世界自由金融代币", "PAXG": "PAX 黄金", "ONDO": "Ondo 金融代币",
    "OKB": "欧易平台币", "DEXE": "DeXe 协议代币", "ASTER": "Aster 代币",
    "M": "MemeCore 代币", "HTX": "火币 DAO 代币", "RLUSD": "瑞波美元稳定币",
    "ETC": "以太坊经典", "VET": "唯链币", "ATOM": "宇宙币",
    "ALGO": "阿尔格兰德", "DOT": "波卡币", "AAVE": "Aave 代币",
    "FIL": "文件币", "ICP": "互联网计算机代币", "OP": "Optimism 代币",
    "ARB": "Arbitrum 代币", "PEPE": "佩佩币", "TON": "Toncoin 代币",
    "WETH": "封装以太坊", "WBTC": "封装比特币", "STETH": "Lido 质押以太坊",
    "WSTETH": "封装 Lido 质押以太坊", "FDUSD": "第一数字美元稳定币",
    "TUSD": "TrueUSD 稳定币", "USDP": "Pax Dollar 稳定币", "ENA": "Ethena 代币",
    "MATIC": "Polygon 代币", "POL": "Polygon 生态代币", "RENDER": "Render 代币",
    "KAS": "Kaspa 代币", "QNT": "Quant 代币", "FLR": "Flare 代币",
    "MKR": "Maker 代币", "JUP": "Jupiter 代币", "BONK": "Bonk 代币",
}

FIAT_NAMES = {
    "AED": "阿联酋迪拉姆", "AFN": "阿富汗尼", "ALL": "阿尔巴尼亚列克", "AMD": "亚美尼亚德拉姆",
    "ANG": "荷属安的列斯盾", "AOA": "安哥拉宽扎", "ARS": "阿根廷比索", "AUD": "澳大利亚元",
    "AWG": "阿鲁巴弗罗林", "AZN": "阿塞拜疆马纳特", "BAM": "波黑可兑换马克", "BBD": "巴巴多斯元",
    "BDT": "孟加拉塔卡", "BGN": "保加利亚列弗", "BHD": "巴林第纳尔", "BIF": "布隆迪法郎",
    "BMD": "百慕大元", "BND": "文莱元", "BOB": "玻利维亚诺", "BRL": "巴西雷亚尔",
    "BSD": "巴哈马元", "BTN": "不丹努尔特鲁姆", "BWP": "博茨瓦纳普拉", "BYN": "白俄罗斯卢布",
    "BZD": "伯利兹元", "CAD": "加拿大元", "CDF": "刚果法郎", "CHF": "瑞士法郎",
    "CLF": "智利发展单位（通胀挂钩记账单位）", "CLP": "智利比索", "CNH": "离岸人民币", "CNY": "人民币", "COP": "哥伦比亚比索", "CRC": "哥斯达黎加科朗",
    "CUP": "古巴比索", "CVE": "佛得角埃斯库多", "CZK": "捷克克朗", "DJF": "吉布提法郎",
    "DKK": "丹麦克朗", "DOP": "多米尼加比索", "DZD": "阿尔及利亚第纳尔", "EGP": "埃及镑",
    "ERN": "厄立特里亚纳克法", "ETB": "埃塞俄比亚比尔", "EUR": "欧元", "FJD": "斐济元",
    "FKP": "福克兰群岛镑", "FOK": "法罗群岛克朗", "GBP": "英镑", "GEL": "格鲁吉亚拉里",
    "GGP": "根西镑", "GHS": "加纳塞地", "GIP": "直布罗陀镑", "GMD": "冈比亚达拉西",
    "GNF": "几内亚法郎", "GTQ": "危地马拉格查尔", "GYD": "圭亚那元", "HKD": "港元",
    "HNL": "洪都拉斯伦皮拉", "HRK": "克罗地亚库纳", "HTG": "海地古德", "HUF": "匈牙利福林",
    "IDR": "印度尼西亚盾", "ILS": "以色列新谢克尔", "IMP": "马恩岛镑", "INR": "印度卢比",
    "IQD": "伊拉克第纳尔", "IRR": "伊朗里亚尔", "ISK": "冰岛克朗", "JEP": "泽西镑",
    "JMD": "牙买加元", "JOD": "约旦第纳尔", "JPY": "日元", "KES": "肯尼亚先令",
    "KGS": "吉尔吉斯斯坦索姆", "KHR": "柬埔寨瑞尔", "KID": "基里巴斯元", "KMF": "科摩罗法郎",
    "KRW": "韩元", "KWD": "科威特第纳尔", "KYD": "开曼群岛元", "KZT": "哈萨克斯坦坚戈",
    "LAK": "老挝基普", "LBP": "黎巴嫩镑", "LKR": "斯里兰卡卢比", "LRD": "利比里亚元",
    "LSL": "莱索托洛蒂", "LYD": "利比亚第纳尔", "MAD": "摩洛哥迪拉姆", "MDL": "摩尔多瓦列伊",
    "MGA": "马达加斯加阿里亚里", "MKD": "北马其顿第纳尔", "MMK": "缅甸元", "MNT": "蒙古图格里克",
    "MOP": "澳门元", "MRU": "毛里塔尼亚乌吉亚", "MUR": "毛里求斯卢比", "MVR": "马尔代夫拉菲亚",
    "MWK": "马拉维克瓦查", "MXN": "墨西哥比索", "MYR": "马来西亚林吉特", "MZN": "莫桑比克梅蒂卡尔",
    "NAD": "纳米比亚元", "NGN": "尼日利亚奈拉", "NIO": "尼加拉瓜科多巴", "NOK": "挪威克朗",
    "NPR": "尼泊尔卢比", "NZD": "新西兰元", "OMR": "阿曼里亚尔", "PAB": "巴拿马巴波亚",
    "PEN": "秘鲁索尔", "PGK": "巴布亚新几内亚基那", "PHP": "菲律宾比索", "PKR": "巴基斯坦卢比",
    "PLN": "波兰兹罗提", "PYG": "巴拉圭瓜拉尼", "QAR": "卡塔尔里亚尔", "RON": "罗马尼亚列伊",
    "RSD": "塞尔维亚第纳尔", "RUB": "俄罗斯卢布", "RWF": "卢旺达法郎", "SAR": "沙特里亚尔",
    "SBD": "所罗门群岛元", "SCR": "塞舌尔卢比", "SDG": "苏丹镑", "SEK": "瑞典克朗",
    "SGD": "新加坡元", "SHP": "圣赫勒拿镑", "SLE": "塞拉利昂利昂", "SLL": "塞拉利昂旧利昂",
    "SOS": "索马里先令", "SRD": "苏里南元", "SSP": "南苏丹镑", "STN": "圣多美和普林西比多布拉",
    "SYP": "叙利亚镑", "SZL": "斯威士兰里兰吉尼", "THB": "泰铢", "TJS": "塔吉克斯坦索莫尼",
    "TMT": "土库曼斯坦马纳特", "TND": "突尼斯第纳尔", "TOP": "汤加潘加", "TRY": "土耳其里拉",
    "TTD": "特立尼达和多巴哥元", "TVD": "图瓦卢元", "TWD": "新台币", "TZS": "坦桑尼亚先令",
    "UAH": "乌克兰格里夫纳", "UGX": "乌干达先令", "USD": "美元", "UYU": "乌拉圭比索",
    "UZS": "乌兹别克斯坦苏姆", "VES": "委内瑞拉玻利瓦尔", "VND": "越南盾", "VUV": "瓦努阿图瓦图",
    "WST": "萨摩亚塔拉", "XAF": "中非法郎", "XCD": "东加勒比元", "XCG": "加勒比盾",
    "XDR": "特别提款权", "XOF": "西非法郎", "XPF": "太平洋法郎", "YER": "也门里亚尔",
    "ZAR": "南非兰特", "ZMW": "赞比亚克瓦查", "ZWG": "津巴布韦金", "ZWL": "津巴布韦元",
}

FIAT_REGION_GROUPS = {
    "亚洲": {
        "AED", "AFN", "AMD", "AZN", "BDT", "BHD", "BND", "BTN", "CNY", "CNH", "GEL",
        "HKD", "IDR", "ILS", "INR", "IQD", "IRR", "JOD", "JPY", "KGS", "KHR", "KRW",
        "KWD", "KZT", "LAK", "LBP", "LKR", "MMK", "MNT", "MOP", "MVR", "MYR", "NPR",
        "OMR", "PHP", "PKR", "QAR", "SAR", "SGD", "SYP", "THB", "TJS", "TMT", "TRY",
        "TWD", "UZS", "VND", "YER",
    },
    "欧洲": {
        "ALL", "BAM", "BGN", "BYN", "CHF", "CZK", "DKK", "EUR", "FKP", "FOK", "GBP",
        "GGP", "GIP", "HRK", "HUF", "IMP", "ISK", "JEP", "MDL", "MKD", "NOK", "PLN",
        "RON", "RSD", "RUB", "SEK", "SHP", "UAH",
    },
    "非洲": {
        "AOA", "BIF", "BWP", "CDF", "CVE", "DJF", "DZD", "EGP", "ERN", "ETB", "GHS",
        "GMD", "GNF", "KES", "KMF", "LRD", "LSL", "LYD", "MAD", "MGA", "MRU", "MUR",
        "MWK", "MZN", "NAD", "NGN", "RWF", "SCR", "SDG", "SLE", "SLL", "SOS", "SSP",
        "STN", "SZL", "TND", "TZS", "UGX", "XAF", "XOF", "ZAR", "ZMW", "ZWG", "ZWL",
    },
    "北美洲": {
        "ANG", "AWG", "BBD", "BMD", "BSD", "BZD", "CAD", "CRC", "CUP", "DOP", "GTQ",
        "HNL", "HTG", "JMD", "KYD", "MXN", "NIO", "PAB", "SVC", "TTD", "USD", "XCD",
        "XCG",
    },
    "南美洲": {
        "ARS", "BOB", "BRL", "CLF", "CLP", "COP", "GYD", "PEN", "PYG", "SRD", "UYU",
        "VES",
    },
    "大洋洲": {
        "AUD", "FJD", "KID", "NZD", "PGK", "SBD", "TOP", "TVD", "VUV", "WST", "XPF",
    },
}
FIAT_REGIONS = {
    code: region for region, codes in FIAT_REGION_GROUPS.items() for code in codes
}


def _finite_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("布尔值不是数字")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("数值不是有限数")
    return result


def _normalize_iso_timestamp(value: Any) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return ""
        return stamp.astimezone(timezone.utc).isoformat()
    except (OverflowError, TypeError, ValueError):
        return ""


def _unix_timestamp_iso(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("布尔值不是有效时间戳")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("时间戳不是数字") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("时间戳必须是正有限数")
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("时间戳超出支持范围") from exc


def _error_summary(label: str, exc: BaseException) -> str:
    detail = " ".join(str(exc).split())
    kind = type(exc).__name__
    if detail and detail != kind:
        return f"{label}：{kind}（{detail[:160]}）"
    return f"{label}：{kind}"


def _normalize_chart_points(raw_points: Any) -> list[tuple[int, float]]:
    if not isinstance(raw_points, (list, tuple)):
        raise ValueError("行情数据不是列表")
    if len(raw_points) > MAX_CHART_POINTS:
        raise ValueError("行情数据点过多")
    by_timestamp: dict[int, float] = {}
    for item in raw_points:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            raw_stamp = _finite_float(item[0])
            value = _finite_float(item[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            raw_stamp < 0
            or raw_stamp > MAX_CHART_TIMESTAMP_MS
            or not raw_stamp.is_integer()
            or value <= 0
            or value > MAX_CHART_VALUE
        ):
            continue
        by_timestamp[int(raw_stamp)] = value
    points = sorted(by_timestamp.items())
    if len(points) < 2:
        raise ValueError("行情数据不足")
    return points


def fiat_display_name(code: str, name: str = "") -> str:
    resolved = FIAT_NAMES.get(code, name or code)
    if re.search(r"[\u4e00-\u9fff]", resolved):
        return resolved
    return f"{resolved}（国际代码 {code}，暂缺中文译名）"


def fiat_region(code: str) -> str:
    return FIAT_REGIONS.get(code, "国际/其他")


def crypto_display_name(code: str, name: str = "") -> str:
    """Return a Chinese market label, with a clear fallback for newly listed assets."""
    resolved = CRYPTO_NAMES_ZH.get(code)
    if resolved:
        return resolved
    if re.search(r"[\u4e00-\u9fff]", name):
        return name
    return f"{name or code} 代币"


def fiat_daily_changes(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Return daily changes for quote-units per USD from one history batch."""
    if not isinstance(rows, list) or len(rows) > MAX_FIAT_HISTORY_ROWS:
        return {"USD": 0.0}
    series: dict[str, dict[datetime, float]] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("base", "USD")).upper() != "USD":
            continue
        code = str(row.get("quote") or "").upper()
        raw_date = str(row.get("date") or "")
        try:
            rate = _finite_float(row.get("rate"))
            date = datetime.strptime(raw_date, "%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            continue
        if re.fullmatch(r"[A-Z]{3}", code) and rate > 0:
            series.setdefault(code, {})[date] = rate
    changes: dict[str, float] = {"USD": 0.0}
    for code, dated_rates in series.items():
        ordered = sorted(dated_rates.items())
        if len(ordered) < 2:
            continue
        previous = ordered[-2][1]
        latest = ordered[-1][1]
        if previous:
            changes[code] = (latest / previous - 1) * 100
    return changes


def relative_rate_change(base_change: float | None, target_change: float | None) -> float | None:
    """Convert two USD-based daily moves into the displayed base-to-target move."""
    if base_change is None or target_change is None:
        return None
    if (
        isinstance(base_change, bool)
        or isinstance(target_change, bool)
        or not math.isfinite(base_change)
        or not math.isfinite(target_change)
    ):
        return None
    base_factor = 1 + base_change / 100
    target_factor = 1 + target_change / 100
    if base_factor <= 0 or target_factor <= 0:
        return None
    return (target_factor / base_factor - 1) * 100


def portable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass
class RateSnapshot:
    rates: dict[str, float]
    names: dict[str, str]
    kinds: dict[str, str]
    changes: dict[str, float | None]
    fetched_at: str
    fiat_updated_at: str = ""
    errors: list[str] | None = None
    coin_ids: dict[str, str] | None = None

    @classmethod
    def empty(cls) -> "RateSnapshot":
        return cls({}, {}, {}, {}, "", "", [], {})


class RateService:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._state_lock = RLock()
        self._cache_lock = RLock()
        self.data_dir = Path(data_dir) if data_dir is not None else portable_dir() / "data"
        self.cache_path = self.data_dir / "rates_cache.json"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Yaoheng/3.16 (Windows)"})
        self.cache_limit_mb = 0
        self.snapshot = self.load_cache()

    def set_data_dir(self, data_dir: Path) -> None:
        """Switch future cache reads/writes without touching the previous folder."""
        with self._state_lock:
            self.data_dir = Path(data_dir).resolve()
            self.cache_path = self.data_dir / "rates_cache.json"
            cached = self.load_cache()
            if cached.rates:
                self.snapshot = cached
            elif self.snapshot.rates:
                self.save_cache(self.snapshot)

    def load_cache(self) -> RateSnapshot:
        try:
            with self._cache_lock:
                if self.cache_path.stat().st_size > MAX_CACHE_FILE_BYTES:
                    raise ValueError("缓存文件过大")
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("rates"), dict):
                raise ValueError("缓存格式无效")
            rates: dict[str, float] = {}
            for raw_code, raw_value in data["rates"].items():
                code = str(raw_code).upper()
                try:
                    value = _finite_float(raw_value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if re.fullmatch(r"[A-Z0-9_]{1,24}", code) and value > 0:
                    rates[code] = value
            if not rates:
                raise ValueError("缓存中没有有效汇率")
            if "USD" in rates and not math.isclose(rates["USD"], 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("缓存的美元基准汇率无效")
            raw_names_source = data.get("names") if isinstance(data.get("names"), dict) else {}
            raw_kinds_source = data.get("kinds") if isinstance(data.get("kinds"), dict) else {}
            raw_changes_source = data.get("changes") if isinstance(data.get("changes"), dict) else {}
            raw_names = {str(code).upper(): value for code, value in raw_names_source.items()}
            raw_kinds = {str(code).upper(): value for code, value in raw_kinds_source.items()}
            raw_changes = {str(code).upper(): value for code, value in raw_changes_source.items()}
            names = {code: str(raw_names.get(code, code)) for code in rates}
            kinds = {code: str(raw_kinds.get(code, "")) for code in rates if raw_kinds.get(code) in {"fiat", "crypto"}}
            changes: dict[str, float | None] = {}
            for code in rates:
                raw_change = raw_changes.get(code)
                if raw_change is None:
                    changes[code] = None
                    continue
                try:
                    change = _finite_float(raw_change)
                except (TypeError, ValueError, OverflowError):
                    changes[code] = None
                else:
                    changes[code] = change
            raw_coin_ids_source = data.get("coin_ids") if isinstance(data.get("coin_ids"), dict) else {}
            raw_coin_ids = {str(code).upper(): value for code, value in raw_coin_ids_source.items()}
            coin_ids = {code: str(raw_coin_ids.get(code, "")) for code in rates if raw_coin_ids.get(code)}
            raw_errors = data.get("errors") if isinstance(data.get("errors"), list) else []
            return RateSnapshot(
                rates=rates,
                names=names,
                kinds=kinds,
                changes=changes,
                fetched_at=_normalize_iso_timestamp(data.get("fetched_at")),
                fiat_updated_at=_normalize_iso_timestamp(data.get("fiat_updated_at")),
                errors=[str(item)[:240] for item in raw_errors[:20]],
                coin_ids=coin_ids,
            )
        except (OSError, OverflowError, RecursionError, ValueError, TypeError):
            return RateSnapshot.empty()

    def save_cache(self, snapshot: RateSnapshot) -> None:
        try:
            self._atomic_write_json(self.cache_path, asdict(snapshot))
        except (OSError, RecursionError, TypeError, ValueError):
            # A read-only USB drive should not stop online conversion from working.
            pass

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_CACHE_FILE_BYTES:
            raise ValueError("缓存内容过大")
        with self._cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f"{path.stem}.tmp.{os.getpid()}.{get_ident()}")
            try:
                with temp.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                temp.replace(path)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            self.enforce_cache_limit()

    def _read_chart_cache(self, path: Path) -> list[tuple[int, float]]:
        with self._cache_lock:
            if path.stat().st_size > MAX_CACHE_FILE_BYTES:
                raise ValueError("行情缓存文件过大")
            payload = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_chart_points(payload)

    def set_cache_limit(self, limit_mb: int) -> None:
        self.cache_limit_mb = max(0, int(limit_mb))
        self.enforce_cache_limit()

    def _managed_cache_files(self) -> list[Path]:
        if not self.data_dir.exists():
            return []
        try:
            return [
                path for path in self.data_dir.iterdir()
                if path.is_file() and any(path.match(pattern) for pattern in MANAGED_CACHE_PATTERNS)
            ]
        except OSError:
            return []

    def cache_size_bytes(self) -> int:
        total = 0
        with self._cache_lock:
            for path in self._managed_cache_files():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def enforce_cache_limit(self) -> None:
        with self._cache_lock:
            if self.cache_limit_mb <= 0 or not self.data_dir.exists():
                return
            limit = self.cache_limit_mb * 1024 * 1024
            entries: list[tuple[Path, int, float]] = []
            for path in self._managed_cache_files():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((path, stat.st_size, stat.st_mtime))
            total = sum(size for _, size, _ in entries)
            removable = sorted(
                (entry for entry in entries if entry[0] != self.cache_path),
                key=lambda entry: entry[2],
            )
            for path, size, _ in removable:
                if total <= limit:
                    break
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                total -= size
            if total > limit:
                try:
                    size = self.cache_path.stat().st_size
                    self.cache_path.unlink(missing_ok=True)
                    total -= size
                except OSError:
                    pass

    def clear_cache(self) -> None:
        with self._cache_lock:
            for path in self._managed_cache_files():
                try:
                    path.unlink()
                except OSError:
                    pass

    def _fetch_fiat_payload(self) -> dict[str, Any]:
        response = self.session.get(FIAT_API, timeout=(4, 12))
        response.raise_for_status()
        return response.json()

    def _fetch_fiat_history_rows(self) -> list[dict[str, Any]]:
        today = datetime.now(timezone.utc).date()
        response = self.session.get(
            FRANKFURTER_API,
            params={"from": (today - timedelta(days=8)).isoformat(), "to": today.isoformat(), "base": "USD"},
            timeout=(4, 12),
        )
        response.raise_for_status()
        payload = response.json()
        if (
            not isinstance(payload, list)
            or not payload
            or len(payload) > MAX_FIAT_HISTORY_ROWS
        ):
            raise ValueError("法币24h数据无效")
        return payload

    def _fetch_crypto_rows(self) -> list[dict[str, Any]]:
        symbols = [f"{code}USDT" for code in BINANCE_CRYPTOS]
        response = self.session.get(
            f"{BINANCE_API}/ticker/24hr",
            params={"symbols": json.dumps(symbols, separators=(",", ":")), "type": "MINI"},
            timeout=(4, 12),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload or len(payload) > MAX_CRYPTO_ROWS:
            raise ValueError("批量行情数据无效")
        return payload

    def refresh(self, section: str = "all") -> RateSnapshot:
        if section not in {"all", "fiat", "crypto"}:
            raise ValueError("未知刷新范围")
        with self._state_lock:
            old = self.snapshot
        rates = dict(old.rates)
        names = dict(old.names)
        kinds = dict(old.kinds)
        changes = dict(old.changes)
        coin_ids = dict(old.coin_ids or {})
        errors: list[str] = []
        fiat_updated = _normalize_iso_timestamp(old.fiat_updated_at)
        fiat_payload: dict[str, Any] | None = None
        fiat_history_rows: list[dict[str, Any]] | None = None
        crypto_rows: list[dict[str, Any]] | None = None
        crypto_primary_error: Exception | None = None
        primary_updated = False
        fiat_succeeded = False
        crypto_succeeded = False
        if section == "all":
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="rates") as executor:
                fiat_future = executor.submit(self._fetch_fiat_payload)
                fiat_history_future = executor.submit(self._fetch_fiat_history_rows)
                crypto_future = executor.submit(self._fetch_crypto_rows)
                try:
                    fiat_payload = fiat_future.result()
                except Exception as exc:
                    errors.append(_error_summary("法币", exc))
                try:
                    fiat_history_rows = fiat_history_future.result()
                except Exception as exc:
                    errors.append(_error_summary("法币24h", exc))
                try:
                    crypto_rows = crypto_future.result()
                except Exception as exc:
                    crypto_primary_error = exc
        elif section == "fiat":
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fiat-rates") as executor:
                fiat_future = executor.submit(self._fetch_fiat_payload)
                fiat_history_future = executor.submit(self._fetch_fiat_history_rows)
                try:
                    fiat_payload = fiat_future.result()
                except Exception as exc:
                    errors.append(_error_summary("法币", exc))
                try:
                    fiat_history_rows = fiat_history_future.result()
                except Exception as exc:
                    errors.append(_error_summary("法币24h", exc))
        else:
            try:
                crypto_rows = self._fetch_crypto_rows()
            except Exception as exc:
                crypto_primary_error = exc

        fiat_changes = {
            code: change for code, change in old.changes.items()
            if old.kinds.get(code) == "fiat" and change is not None
        }
        if fiat_history_rows is not None:
            try:
                fresh_changes = fiat_daily_changes(fiat_history_rows)
                if len(fresh_changes) < 2:
                    raise ValueError("没有足够的有效日行情")
                fiat_changes.update(fresh_changes)
            except Exception as exc:
                errors.append(_error_summary("法币24h数据", exc))

        if fiat_payload is not None:
            try:
                payload = fiat_payload
                if not isinstance(payload, dict) or payload.get("result") != "success":
                    raise ValueError("返回数据无效")
                raw_rates = payload.get("rates")
                if not isinstance(raw_rates, dict) or len(raw_rates) > MAX_API_RATE_ENTRIES:
                    raise ValueError("返回数据中没有汇率表")
                fiat_rates: dict[str, float] = {}
                for raw_code, raw_value in raw_rates.items():
                    code = str(raw_code).upper()
                    if not re.fullmatch(r"[A-Z]{3}", code):
                        continue
                    try:
                        value = _finite_float(raw_value)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if value > 0:
                        fiat_rates[code] = value
                if len(fiat_rates) < 2 or "USD" not in fiat_rates or "CNY" not in fiat_rates:
                    raise ValueError("返回数据中没有有效汇率")
                if not math.isclose(fiat_rates["USD"], 1.0, rel_tol=0.0, abs_tol=1e-12):
                    raise ValueError("返回数据不是以美元为基准")
                fiat_rates["USD"] = 1.0
                unix_time = payload.get("time_last_update_unix")
                if unix_time is not None:
                    try:
                        fiat_updated = _unix_timestamp_iso(unix_time)
                    except ValueError as exc:
                        errors.append(_error_summary("法币时间戳", exc))
                old_fiat_codes = {code for code, kind in kinds.items() if kind == "fiat"}
                missing_fiat_codes = old_fiat_codes - fiat_rates.keys()
                if missing_fiat_codes:
                    errors.append(
                        f"法币数据：响应缺少 {len(missing_fiat_codes)} 个旧币种，已保留缓存值"
                    )
                for code, value in fiat_rates.items():
                    rates[code] = value
                    names[code] = fiat_display_name(code, FIAT_NAMES.get(code, code))
                    kinds[code] = "fiat"
                    changes[code] = fiat_changes.get(code)
                    coin_ids.pop(code, None)
                primary_updated = True
                fiat_succeeded = True
            except Exception as exc:
                errors.append(_error_summary("法币数据", exc))

        if section in {"all", "crypto"} and crypto_rows is not None:
            try:
                applied = self._apply_binance_rows(crypto_rows, rates, names, kinds, changes, coin_ids)
                if applied < len(BINANCE_CRYPTOS):
                    errors.append(f"虚拟币批量源：仅更新 {applied}/{len(BINANCE_CRYPTOS)} 个币种")
                else:
                    fresh_codes = set(BINANCE_CRYPTOS) | {"USDT"}
                    for code in [
                        key for key, kind in kinds.items()
                        if kind == "crypto" and key not in fresh_codes
                    ]:
                        rates.pop(code, None)
                        names.pop(code, None)
                        kinds.pop(code, None)
                        changes.pop(code, None)
                        coin_ids.pop(code, None)
                primary_updated = True
                crypto_succeeded = True
            except Exception as exc:
                crypto_primary_error = exc
        if section in {"all", "crypto"} and not crypto_succeeded:
            try:
                response = self.session.get(CRYPTO_API, timeout=(3, 10))
                response.raise_for_status()
                coins = response.json()
                if (
                    not isinstance(coins, list)
                    or not coins
                    or len(coins) > MAX_CRYPTO_ROWS
                    or "CNY" not in rates
                ):
                    raise ValueError("返回数据无效")
                cny_per_usd = _finite_float(rates["CNY"])
                if cny_per_usd <= 0:
                    raise ValueError("人民币基准汇率无效")
                staged_rates: dict[str, float] = {}
                staged_names: dict[str, str] = {}
                staged_changes: dict[str, float | None] = {}
                staged_coin_ids: dict[str, str] = {}
                for coin in coins:
                    if not isinstance(coin, dict):
                        continue
                    code = str(coin.get("symbol", "")).upper()
                    if (
                        not re.fullmatch(r"[A-Z0-9_]{1,24}", code)
                        or code in staged_rates
                        or (code in rates and kinds.get(code) == "fiat")
                    ):
                        continue
                    try:
                        cny_price = _finite_float(coin.get("current_price"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if cny_price <= 0:
                        continue
                    usd_price = cny_price / cny_per_usd
                    rate = 1.0 / usd_price
                    if not math.isfinite(rate) or rate <= 0:
                        continue
                    staged_rates[code] = rate
                    staged_names[code] = str(coin.get("name") or code)
                    coin_id = str(coin.get("id") or "")
                    if coin_id:
                        staged_coin_ids[code] = coin_id
                    try:
                        change = _finite_float(coin.get("price_change_percentage_24h"))
                    except (TypeError, ValueError, OverflowError):
                        change = None
                    staged_changes[code] = change
                if not staged_rates:
                    raise ValueError("备用源没有有效行情")
                if len(staged_rates) >= 10:
                    for code in [
                        key for key, kind in kinds.items()
                        if kind == "crypto" and key not in staged_rates
                    ]:
                        rates.pop(code, None)
                        names.pop(code, None)
                        kinds.pop(code, None)
                        changes.pop(code, None)
                        coin_ids.pop(code, None)
                else:
                    errors.append(f"虚拟币备用源：仅合并 {len(staged_rates)} 个有效币种")
                for code, rate in staged_rates.items():
                    rates[code] = rate
                    names[code] = staged_names[code]
                    kinds[code] = "crypto"
                    changes[code] = staged_changes[code]
                    if code in staged_coin_ids:
                        coin_ids[code] = staged_coin_ids[code]
                primary_updated = True
                crypto_succeeded = True
                if crypto_primary_error is not None:
                    errors.append(_error_summary("虚拟币批量源", crypto_primary_error) + "，已切换备用源")
            except Exception as exc:
                if crypto_primary_error is not None:
                    errors.append(
                        _error_summary("虚拟币", crypto_primary_error)
                        + "；"
                        + _error_summary("备用源", exc)
                    )
                else:
                    errors.append(_error_summary("虚拟币", exc))

        if not primary_updated:
            detail = "；".join(errors[:4])
            message = (
                "联网更新失败，继续使用上次缓存"
                if old.rates else "联网更新失败，且本机没有可用缓存"
            )
            raise ConnectionError(f"{message}：{detail}" if detail else message)
        if not rates:
            raise ConnectionError("暂时无法获取汇率，且本机没有可用缓存")
        with self._state_lock:
            current = self.snapshot
            if current is not old:
                if not fiat_succeeded:
                    self._replace_snapshot_kind(
                        "fiat", current, rates, names, kinds, changes, coin_ids
                    )
                    fiat_updated = _normalize_iso_timestamp(current.fiat_updated_at)
                if not crypto_succeeded:
                    self._replace_snapshot_kind(
                        "crypto", current, rates, names, kinds, changes, coin_ids
                    )
            fetched_at = datetime.now(timezone.utc).isoformat()
            snapshot = RateSnapshot(
                rates, names, kinds, changes, fetched_at, fiat_updated, errors[:20], coin_ids
            )
            self.snapshot = snapshot
            self.save_cache(snapshot)
            return snapshot

    @staticmethod
    def _replace_snapshot_kind(
        kind: str,
        source: RateSnapshot,
        rates: dict[str, float],
        names: dict[str, str],
        kinds: dict[str, str],
        changes: dict[str, float | None],
        coin_ids: dict[str, str],
    ) -> None:
        for code in [key for key, value in kinds.items() if value == kind]:
            rates.pop(code, None)
            names.pop(code, None)
            kinds.pop(code, None)
            changes.pop(code, None)
            coin_ids.pop(code, None)
        source_coin_ids = source.coin_ids or {}
        for code, rate in source.rates.items():
            if source.kinds.get(code) != kind:
                continue
            rates[code] = rate
            names[code] = source.names.get(code, code)
            kinds[code] = kind
            changes[code] = source.changes.get(code)
            if code in source_coin_ids:
                coin_ids[code] = source_coin_ids[code]

    def _apply_binance_rows(
        self,
        rows: list[dict[str, Any]],
        rates: dict[str, float],
        names: dict[str, str],
        kinds: dict[str, str],
        changes: dict[str, float | None],
        coin_ids: dict[str, str],
    ) -> int:
        if not isinstance(rows, list) or len(rows) > MAX_CRYPTO_ROWS:
            raise ValueError("批量行情格式无效")
        staged_prices: dict[str, tuple[float, float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            code = symbol[:-4]
            try:
                last_price = _finite_float(row.get("lastPrice"))
            except (TypeError, ValueError, OverflowError):
                continue
            try:
                open_price = _finite_float(row.get("openPrice"))
            except (TypeError, ValueError, OverflowError):
                open_price = 0.0
            if code not in BINANCE_CRYPTOS or last_price <= 0:
                continue
            staged_prices[code] = (last_price, open_price)
        if not staged_prices:
            raise ValueError("批量行情中没有有效价格")
        # USDCUSDT provides a stable USD anchor for all USDT-quoted pairs.
        # Falling back to parity keeps partial responses usable.
        usdc_last, usdc_open = staged_prices.get("USDC", (1.0, 1.0))
        usdt_per_usd = usdc_last
        calibration_factor = (
            usdc_last / usdc_open
            if math.isfinite(usdc_open) and usdc_open > 0 else 1.0
        )
        rates["USDT"] = usdt_per_usd
        names["USDT"] = "Tether"
        kinds["USDT"] = "crypto"
        usdt_change = (1.0 / calibration_factor - 1.0) * 100
        changes["USDT"] = usdt_change if math.isfinite(usdt_change) else None
        coin_ids["USDT"] = BINANCE_COIN_IDS["USDT"]
        applied = 0
        for code, (last_price, open_price) in staged_prices.items():
            rate = usdt_per_usd / last_price
            if not math.isfinite(rate) or rate <= 0:
                continue
            change: float | None = None
            if math.isfinite(open_price) and open_price > 0:
                candidate = ((last_price / open_price) / calibration_factor - 1.0) * 100
                if math.isfinite(candidate):
                    change = candidate
            rates[code] = rate
            names[code] = BINANCE_CRYPTOS[code]
            kinds[code] = "crypto"
            changes[code] = change
            coin_ids[code] = BINANCE_COIN_IDS[code]
            applied += 1
        if not applied:
            raise ValueError("批量行情中没有可换算价格")
        return applied

    def _merge_binance_crypto(
        self,
        rates: dict[str, float],
        names: dict[str, str],
        kinds: dict[str, str],
        changes: dict[str, float | None],
        coin_ids: dict[str, str],
    ) -> None:
        symbols = [f"{code}USDT" for code in BINANCE_CRYPTOS]
        response = self.session.get(
            f"{BINANCE_API}/ticker/24hr",
            params={"symbols": json.dumps(symbols, separators=(",", ":")), "type": "MINI"},
            timeout=(4, 15),
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError("备用行情数据无效")
        self._apply_binance_rows(rows, rates, names, kinds, changes, coin_ids)

    def convert(self, amount: float, source: str, target: str) -> float:
        source_code = str(source).strip().upper()
        target_code = str(target).strip().upper()
        with self._state_lock:
            snapshot = self.snapshot
        if source_code not in snapshot.rates or target_code not in snapshot.rates:
            raise ValueError("尚无所选币种的汇率")
        try:
            amount_value = Decimal(str(amount))
            source_rate = Decimal(str(snapshot.rates[source_code]))
            target_rate = Decimal(str(snapshot.rates[target_code]))
        except (DecimalException, TypeError, ValueError) as exc:
            raise ValueError("金额或汇率无效") from exc
        if not all(value.is_finite() for value in (amount_value, source_rate, target_rate)):
            raise ValueError("金额或汇率无效")
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("汇率必须大于零")
        try:
            with localcontext() as context:
                context.prec = 50
                decimal_result = amount_value / source_rate * target_rate
            result = float(decimal_result)
        except (DecimalException, OverflowError, ValueError) as exc:
            raise ValueError("换算结果超出范围") from exc
        if amount_value != 0 and decimal_result == 0:
            raise ValueError("换算结果超出范围")
        if not math.isfinite(result):
            raise ValueError("换算结果超出范围")
        if decimal_result != 0 and result == 0:
            raise ValueError("换算结果超出范围")
        return result

    def fetch_market_chart(self, code: str, days: int = 7) -> list[tuple[int, float]]:
        """Return timestamp/CNY price points, falling back to the last local chart cache."""
        code = str(code).strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{1,24}", code):
            raise ValueError("币种代码无效")
        with self._state_lock:
            snapshot = self.snapshot
            data_dir = self.data_dir
        coin_id = (snapshot.coin_ids or {}).get(code, "")
        if not coin_id:
            coin_id = BINANCE_COIN_IDS.get(code, "")
        if not coin_id and code not in BINANCE_CRYPTOS and code != "USDT":
            raise ValueError("暂时没有该币种的行情标识")
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", coin_id or code.lower())
        if not safe_id:
            raise ValueError("行情标识无效")
        safe_days = days if days in {1, 7, 30, 90, 365} else 7
        cache_path = data_dir / f"chart_{safe_id}_{safe_days}.json"
        url = f"https://api.coingecko.com/api/v3/coins/{safe_id}/market_chart"
        failures: list[str] = []
        if code in BINANCE_CRYPTOS or code == "USDT":
            try:
                points = self._fetch_binance_chart(code, safe_days)
                try:
                    self._atomic_write_json(cache_path, points)
                except (OSError, RecursionError, TypeError, ValueError):
                    pass
                return points
            except Exception as exc:
                failures.append(_error_summary("批量源趋势", exc))
        try:
            response = self.session.get(
                url,
                params={"vs_currency": "cny", "days": safe_days},
                timeout=(5, 20),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("行情数据格式无效")
            points = _normalize_chart_points(payload.get("prices"))
            try:
                self._atomic_write_json(cache_path, points)
            except (OSError, RecursionError, TypeError, ValueError):
                pass
            return points
        except Exception as exc:
            failures.append(_error_summary("备用源趋势", exc))
            try:
                return self._read_chart_cache(cache_path)
            except (OSError, RecursionError, TypeError, ValueError) as cache_exc:
                failures.append(_error_summary("本地行情缓存", cache_exc))
            detail = "；".join(failures[:3])
            raise ConnectionError(f"行情获取失败，请检查网络后重试：{detail}") from exc

    def fetch_fiat_chart(self, code: str, days: int = 7, quote: str = "CNY") -> list[tuple[int, float]]:
        """Return official daily fiat time-series points for a selected pair."""
        safe_days = days if days in {1, 7, 30, 90, 365} else 7
        code = str(code).strip().upper()
        quote = str(quote).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code) or not re.fullmatch(r"[A-Z]{3}", quote):
            raise ValueError("货币代码无效")
        with self._state_lock:
            snapshot = self.snapshot
            data_dir = self.data_dir
        if code not in snapshot.rates or quote not in snapshot.rates:
            raise ValueError("尚无所选货币的汇率")
        if snapshot.kinds.get(code) == "crypto" or snapshot.kinds.get(quote) == "crypto":
            raise ValueError("法币行情不支持虚拟币")
        cache_path = data_dir / f"fiat_chart_{code}_{quote}_{safe_days}.json"
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        if code == quote:
            span = max(safe_days, 1) * 86_400_000
            return [(now_ms - span, 1.0), (now_ms, 1.0)]
        try:
            lookback = max(7, safe_days) + 5
            start = (now.date() - timedelta(days=lookback)).isoformat()
            response = self.session.get(
                FRANKFURTER_API,
                params={"from": start, "to": now.date().isoformat(), "base": code, "quotes": quote},
                timeout=(5, 20),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("货币历史行情格式无效")
            raw_points: list[tuple[int, float]] = []
            for row in payload:
                if (
                    not isinstance(row, dict)
                    or str(row.get("base") or "").upper() != code
                    or str(row.get("quote") or "").upper() != quote
                ):
                    continue
                try:
                    stamp = datetime.strptime(str(row.get("date") or ""), "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    value = _finite_float(row.get("rate"))
                except (TypeError, ValueError, OverflowError):
                    continue
                raw_points.append((int(stamp.timestamp() * 1000), value))
            points = _normalize_chart_points(raw_points)
            if safe_days == 1:
                points = points[-2:]
            else:
                cutoff_day = now.date() - timedelta(days=safe_days)
                cutoff = int(
                    datetime.combine(cutoff_day, datetime.min.time(), timezone.utc).timestamp() * 1000
                )
                points = [point for point in points if point[0] >= cutoff]
            if len(points) < 2:
                raise ValueError("货币历史行情数据不足")
            try:
                self._atomic_write_json(cache_path, points)
            except (OSError, RecursionError, TypeError, ValueError):
                pass
            return points
        except Exception:
            try:
                return self._read_chart_cache(cache_path)
            except (OSError, RecursionError, TypeError, ValueError):
                pass
            # The live cross-rate still gives a truthful flat fallback when a
            # currency is outside the historical provider's coverage.
            current = self.convert(1, code, quote)
            span = max(safe_days, 1) * 86_400_000
            return [(now_ms - span, current), (now_ms, current)]

    def _fetch_binance_chart(self, code: str, days: int) -> list[tuple[int, float]]:
        intervals = {
            1: ("5m", 288),
            7: ("1h", 168),
            30: ("4h", 180),
            90: ("1d", 90),
            365: ("1d", 365),
        }
        code = str(code).strip().upper()
        if days not in intervals or (code not in BINANCE_CRYPTOS and code != "USDT"):
            raise ValueError("备用趋势请求无效")
        interval, limit = intervals[days]
        invert = code == "USDT"
        symbol = "USDCUSDT" if invert else f"{code}USDT"
        response = self.session.get(
            f"{BINANCE_API}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=(4, 18),
        )
        response.raise_for_status()
        rows = response.json()
        with self._state_lock:
            raw_cny_per_usd = self.snapshot.rates.get("CNY")
            raw_usdt_per_usd = self.snapshot.rates.get("USDT", 1.0)
        try:
            cny_per_usd = _finite_float(raw_cny_per_usd)
            usdt_per_usd = _finite_float(raw_usdt_per_usd)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("图表基准汇率无效") from exc
        if (
            not isinstance(rows, list)
            or len(rows) < 2
            or not math.isfinite(cny_per_usd)
            or cny_per_usd <= 0
            or not math.isfinite(usdt_per_usd)
            or usdt_per_usd <= 0
        ):
            raise ValueError("备用趋势数据无效")
        raw_points: list[tuple[int, float]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                if isinstance(row[0], bool):
                    raise TypeError("布尔值不是时间戳")
                stamp = int(row[0])
                close = _finite_float(row[4])
            except (TypeError, ValueError, OverflowError):
                continue
            if stamp < 0 or not math.isfinite(close) or close <= 0:
                continue
            usd_price = (1.0 / close) if invert else close / usdt_per_usd
            cny_price = usd_price * cny_per_usd
            if math.isfinite(cny_price) and cny_price > 0:
                raw_points.append((stamp, cny_price))
        return _normalize_chart_points(raw_points)
