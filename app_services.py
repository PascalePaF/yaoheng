"""Application-level service composition, independent of Tk widgets."""

from __future__ import annotations

from c2c import BinanceP2PAdapter, C2CQuoteService, OkxP2PAdapter
from rate_service import RateService


class AppC2CService:
    """Expose read-only platform quotes without leaking adapters into views."""

    def __init__(self, rate_service: RateService) -> None:
        self.rate_service = rate_service
        self.providers = {
            "binance": BinanceP2PAdapter(),
            "okx": OkxP2PAdapter(),
        }
        self.service = C2CQuoteService(
            self.providers,
            market_fallback=self._market_fallback,
        )

    def _market_fallback(self, request: object) -> dict[str, str]:
        asset = str(getattr(request, "asset", ""))
        fiat = str(getattr(request, "fiat", ""))
        return {
            "price": self.rate_service.convert_exact("1", asset, fiat),
            "source": "ordinary_market",
        }

    def quote(self, request: object, *, cancel: object | None = None) -> object:
        return self.service.quote(request, cancel=cancel)  # type: ignore[arg-type]

    def capabilities(self) -> object:
        return self.service.capabilities()

    def clear_memory_cache(self) -> None:
        self.service.clear_memory_cache()

    def payment_methods(self, provider: str, fiat: str) -> tuple[object, ...]:
        """Fetch official provider identifiers; callers run this off Tk."""

        selected = str(provider or "auto").lower()
        candidates = (
            tuple(self.providers.values())
            if selected == "auto"
            else (self.providers.get(selected),)
        )
        methods: list[object] = []
        identifiers: set[str] = set()
        for adapter in candidates:
            if adapter is None:
                continue
            capability = adapter.capability
            if not capability.enabled or not capability.configured or not capability.trade_methods:
                continue
            loader = getattr(adapter, "list_trade_methods", None)
            if not callable(loader):
                continue
            try:
                rows = tuple(loader(fiat))
            except Exception:
                continue
            for row in rows:
                identifier = str(getattr(row, "identifier", ""))
                if identifier and identifier not in identifiers:
                    identifiers.add(identifier)
                    methods.append(row)
        return tuple(methods)
