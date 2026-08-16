"""Online fiat and cryptocurrency rates with a portable local cache."""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
BINANCE_CRYPTOS = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "BNB": "BNB", "XRP": "XRP", "USDC": "USDC",
    "SOL": "Solana", "TRX": "TRON", "DOGE": "Dogecoin", "ADA": "Cardano", "BCH": "Bitcoin Cash",
    "LINK": "Chainlink", "XLM": "Stellar", "LTC": "Litecoin", "AVAX": "Avalanche", "SUI": "Sui",
    "DOT": "Polkadot", "UNI": "Uniswap", "AAVE": "Aave", "NEAR": "NEAR Protocol", "ETC": "Ethereum Classic",
    "ICP": "Internet Computer", "FIL": "Filecoin", "ATOM": "Cosmos", "ALGO": "Algorand", "VET": "VeChain",
    "SHIB": "Shiba Inu", "PEPE": "Pepe", "ARB": "Arbitrum", "OP": "Optimism",
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
    series: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("base", "USD")) != "USD":
            continue
        code = str(row.get("quote") or "").upper()
        date = str(row.get("date") or "")
        try:
            rate = float(row.get("rate") or 0)
        except (TypeError, ValueError):
            continue
        if code and date and rate > 0:
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
    base_factor = 1 + base_change / 100
    target_factor = 1 + target_change / 100
    if base_factor <= 0:
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
        self.data_dir = data_dir or portable_dir() / "data"
        self.cache_path = self.data_dir / "rates_cache.json"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Yaoheng/3.12 (Windows portable)"})
        self.cache_limit_mb = 0
        self.snapshot = self.load_cache()

    def set_data_dir(self, data_dir: Path) -> None:
        """Switch future cache reads/writes without touching the previous folder."""
        self.data_dir = Path(data_dir).resolve()
        self.cache_path = self.data_dir / "rates_cache.json"
        cached = self.load_cache()
        if cached.rates:
            self.snapshot = cached
        elif self.snapshot.rates:
            self.save_cache(self.snapshot)

    def load_cache(self) -> RateSnapshot:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return RateSnapshot(**data)
        except (OSError, ValueError, TypeError):
            return RateSnapshot.empty()

    def save_cache(self, snapshot: RateSnapshot) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self.cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(snapshot), ensure_ascii=False), encoding="utf-8")
            temp.replace(self.cache_path)
            self.enforce_cache_limit()
        except OSError:
            # A read-only USB drive should not stop online conversion from working.
            pass

    def set_cache_limit(self, limit_mb: int) -> None:
        self.cache_limit_mb = max(0, int(limit_mb))
        self.enforce_cache_limit()

    def cache_size_bytes(self) -> int:
        try:
            return sum(path.stat().st_size for path in self.data_dir.rglob("*") if path.is_file())
        except OSError:
            return 0

    def enforce_cache_limit(self) -> None:
        if self.cache_limit_mb <= 0 or not self.data_dir.exists():
            return
        limit = self.cache_limit_mb * 1024 * 1024
        try:
            files = [path for path in self.data_dir.rglob("*") if path.is_file()]
            total = sum(path.stat().st_size for path in files)
            removable = sorted(
                (path for path in files if path.resolve() != self.cache_path.resolve()),
                key=lambda path: path.stat().st_mtime,
            )
            for path in removable:
                if total <= limit:
                    break
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                total -= size
        except OSError:
            pass

    def clear_cache(self) -> None:
        if not self.data_dir.exists():
            return
        for path in sorted(self.data_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
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
        if not isinstance(payload, list) or not payload:
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
        if not isinstance(payload, list) or not payload:
            raise ValueError("批量行情数据无效")
        return payload

    def refresh(self, section: str = "all") -> RateSnapshot:
        if section not in {"all", "fiat", "crypto"}:
            raise ValueError("未知刷新范围")
        old = self.snapshot
        rates = dict(old.rates)
        names = dict(old.names)
        kinds = dict(old.kinds)
        changes = dict(old.changes)
        coin_ids = dict(old.coin_ids or {})
        errors: list[str] = []
        fiat_updated = old.fiat_updated_at
        fiat_payload: dict[str, Any] | None = None
        fiat_history_rows: list[dict[str, Any]] | None = None
        crypto_rows: list[dict[str, Any]] | None = None
        crypto_primary_error: Exception | None = None
        if section == "all":
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="rates") as executor:
                fiat_future = executor.submit(self._fetch_fiat_payload)
                fiat_history_future = executor.submit(self._fetch_fiat_history_rows)
                crypto_future = executor.submit(self._fetch_crypto_rows)
                try:
                    fiat_payload = fiat_future.result()
                except Exception as exc:
                    errors.append(f"法币：{type(exc).__name__}")
                try:
                    fiat_history_rows = fiat_history_future.result()
                except Exception as exc:
                    errors.append(f"法币24h：{type(exc).__name__}")
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
                    errors.append(f"法币：{type(exc).__name__}")
                try:
                    fiat_history_rows = fiat_history_future.result()
                except Exception as exc:
                    errors.append(f"法币24h：{type(exc).__name__}")
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
                fiat_changes = fiat_daily_changes(fiat_history_rows)
            except Exception as exc:
                errors.append(f"法币24h数据：{type(exc).__name__}")

        if fiat_payload is not None:
            try:
                payload = fiat_payload
                if payload.get("result") != "success" or not payload.get("rates"):
                    raise ValueError("返回数据无效")
                fiat_rates = {str(code): float(value) for code, value in payload["rates"].items()}
                for code in [key for key, kind in kinds.items() if kind == "fiat"]:
                    rates.pop(code, None)
                    names.pop(code, None)
                    kinds.pop(code, None)
                    changes.pop(code, None)
                for code, value in fiat_rates.items():
                    rates[code] = value
                    names[code] = fiat_display_name(code, FIAT_NAMES.get(code, code))
                    kinds[code] = "fiat"
                    changes[code] = fiat_changes.get(code)
                unix_time = payload.get("time_last_update_unix")
                if unix_time:
                    fiat_updated = datetime.fromtimestamp(int(unix_time), timezone.utc).astimezone().isoformat()
            except Exception as exc:
                errors.append(f"法币数据：{type(exc).__name__}")

        if section in {"all", "crypto"} and crypto_rows is not None:
            try:
                self._apply_binance_rows(crypto_rows, rates, names, kinds, changes, coin_ids)
            except Exception as exc:
                errors.append(f"虚拟币数据：{type(exc).__name__}")
        elif section in {"all", "crypto"}:
            try:
                response = self.session.get(CRYPTO_API, timeout=(3, 10))
                response.raise_for_status()
                coins: list[dict[str, Any]] = response.json()
                if not isinstance(coins, list) or not coins or "CNY" not in rates:
                    raise ValueError("返回数据无效")
                cny_per_usd = rates["CNY"]
                for coin in coins:
                    code = str(coin.get("symbol", "")).upper()
                    cny_price = coin.get("current_price")
                    if not code or not cny_price or (code in rates and kinds.get(code) == "fiat"):
                        continue
                    usd_price = float(cny_price) / cny_per_usd
                    rates[code] = 1.0 / usd_price
                    names[code] = str(coin.get("name") or code)
                    kinds[code] = "crypto"
                    coin_ids[code] = str(coin.get("id") or "")
                    change = coin.get("price_change_percentage_24h")
                    changes[code] = float(change) if change is not None else None
                errors.append(f"虚拟币批量源：{type(crypto_primary_error).__name__ if crypto_primary_error else '未知错误'}，已切换备用源")
            except Exception as exc:
                primary_name = type(crypto_primary_error).__name__ if crypto_primary_error else "未知错误"
                errors.append(f"虚拟币：{primary_name}/{type(exc).__name__}")

        if not rates:
            raise ConnectionError("暂时无法获取汇率，且本机没有可用缓存")
        fetched_at = datetime.now().astimezone().isoformat()
        snapshot = RateSnapshot(rates, names, kinds, changes, fetched_at, fiat_updated, errors, coin_ids)
        self.snapshot = snapshot
        self.save_cache(snapshot)
        return snapshot

    def _apply_binance_rows(
        self,
        rows: list[dict[str, Any]],
        rates: dict[str, float],
        names: dict[str, str],
        kinds: dict[str, str],
        changes: dict[str, float | None],
        coin_ids: dict[str, str],
    ) -> None:
        rates["USDT"] = 1.0
        names["USDT"] = "Tether"
        kinds["USDT"] = "crypto"
        changes["USDT"] = 0.0
        coin_ids.setdefault("USDT", "tether")
        for row in rows:
            symbol = str(row.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            code = symbol[:-4]
            last_price = float(row.get("lastPrice") or 0)
            open_price = float(row.get("openPrice") or 0)
            if code not in BINANCE_CRYPTOS or last_price <= 0:
                continue
            rates[code] = 1.0 / last_price
            names[code] = BINANCE_CRYPTOS[code]
            kinds[code] = "crypto"
            changes[code] = ((last_price / open_price) - 1) * 100 if open_price else None

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
        if source not in self.snapshot.rates or target not in self.snapshot.rates:
            raise ValueError("尚无所选币种的汇率")
        usd_value = amount / self.snapshot.rates[source]
        return usd_value * self.snapshot.rates[target]

    def fetch_market_chart(self, code: str, days: int = 7) -> list[tuple[int, float]]:
        """Return timestamp/CNY price points, falling back to the last local chart cache."""
        coin_id = (self.snapshot.coin_ids or {}).get(code, "")
        if not coin_id:
            fallback_ids = {
                "BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether", "BNB": "binancecoin",
                "XRP": "ripple", "USDC": "usd-coin", "SOL": "solana", "DOGE": "dogecoin",
                "ADA": "cardano", "TRX": "tron",
            }
            coin_id = fallback_ids.get(code, "")
        if not coin_id and code not in BINANCE_CRYPTOS and code != "USDT":
            raise ValueError("暂时没有该币种的行情标识")
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", coin_id or code.lower())
        safe_days = days if days in {1, 7, 30, 90, 365} else 7
        cache_path = self.data_dir / f"chart_{safe_id}_{safe_days}.json"
        url = f"https://api.coingecko.com/api/v3/coins/{safe_id}/market_chart"
        try:
            points = self._fetch_binance_chart(code, safe_days)
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(points), encoding="utf-8")
                self.enforce_cache_limit()
            except OSError:
                pass
            return points
        except Exception:
            pass
        try:
            response = self.session.get(
                url,
                params={"vs_currency": "cny", "days": safe_days},
                timeout=(5, 20),
            )
            response.raise_for_status()
            payload = response.json()
            prices = payload.get("prices", [])
            points = [(int(item[0]), float(item[1])) for item in prices if len(item) >= 2]
            if len(points) < 2:
                raise ValueError("行情数据不足")
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(points), encoding="utf-8")
                self.enforce_cache_limit()
            except OSError:
                pass
            return points
        except Exception:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                points = [(int(item[0]), float(item[1])) for item in cached if len(item) >= 2]
                if len(points) >= 2:
                    return points
            except (OSError, ValueError, TypeError):
                pass
            raise ConnectionError("行情获取失败，请检查网络后重试")

    def fetch_fiat_chart(self, code: str, days: int = 7, quote: str = "CNY") -> list[tuple[int, float]]:
        """Return official daily fiat time-series points for a selected pair."""
        safe_days = days if days in {1, 7, 30, 90, 365} else 7
        code = re.sub(r"[^A-Z]", "", code.upper())
        quote = re.sub(r"[^A-Z]", "", quote.upper())
        if code not in self.snapshot.rates or quote not in self.snapshot.rates:
            raise ValueError("尚无所选货币的汇率")
        cache_path = self.data_dir / f"fiat_chart_{code}_{quote}_{safe_days}.json"
        now = datetime.now(timezone.utc)
        if code == quote:
            span = max(safe_days, 1) * 86_400_000
            return [(int(now.timestamp() * 1000) - span, 1.0), (int(now.timestamp() * 1000), 1.0)]
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
            points: list[tuple[int, float]] = []
            for row in payload:
                if not isinstance(row, dict) or row.get("quote") != quote:
                    continue
                stamp = datetime.fromisoformat(str(row.get("date"))).replace(tzinfo=timezone.utc)
                value = float(row.get("rate") or 0)
                if value > 0:
                    points.append((int(stamp.timestamp() * 1000), value))
            points.sort(key=lambda item: item[0])
            if safe_days == 1:
                points = points[-2:]
            else:
                cutoff = int((now - timedelta(days=safe_days)).timestamp() * 1000)
                points = [point for point in points if point[0] >= cutoff]
            if len(points) < 2:
                raise ValueError("货币历史行情数据不足")
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(points), encoding="utf-8")
                self.enforce_cache_limit()
            except OSError:
                pass
            return points
        except Exception:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                points = [(int(item[0]), float(item[1])) for item in cached if len(item) >= 2]
                if len(points) >= 2:
                    return points
            except (OSError, ValueError, TypeError):
                pass
            # The live cross-rate still gives a truthful flat fallback when a
            # currency is outside the historical provider's coverage.
            current = self.convert(1, code, quote)
            span = max(safe_days, 1) * 86_400_000
            return [(int(now.timestamp() * 1000) - span, current), (int(now.timestamp() * 1000), current)]

    def _fetch_binance_chart(self, code: str, days: int) -> list[tuple[int, float]]:
        intervals = {
            1: ("5m", 288),
            7: ("1h", 168),
            30: ("4h", 180),
            90: ("1d", 90),
            365: ("1d", 365),
        }
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
        cny_per_usd = self.snapshot.rates.get("CNY")
        if not isinstance(rows, list) or len(rows) < 2 or not cny_per_usd:
            raise ValueError("备用趋势数据无效")
        points: list[tuple[int, float]] = []
        for row in rows:
            close = float(row[4])
            usd_price = (1.0 / close) if invert and close else close
            points.append((int(row[0]), usd_price * cny_per_usd))
        return points
