"""Comfort-oriented application theme catalog.

The palettes intentionally avoid pure black/white and highly saturated large
surfaces.  Derived semantic colours keep every theme complete while making it
cheap to add or audit a palette.
"""

from __future__ import annotations

from collections.abc import Mapping


def _rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError("theme colour must use #RRGGBB")
    return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _hex(red: int, green: int, blue: int) -> str:
    return f"#{red:02X}{green:02X}{blue:02X}"


def _mix(foreground: str, background: str, amount: float) -> str:
    front = _rgb(foreground)
    back = _rgb(background)
    return _hex(*(round(back[i] + (front[i] - back[i]) * amount) for i in range(3)))


def _luminance(value: str) -> float:
    channels = []
    for channel in _rgb(value):
        normalized = channel / 255
        channels.append(
            normalized / 12.92
            if normalized <= 0.04045 else
            ((normalized + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _readable_on(value: str) -> str:
    dark = "#10171C"
    light = "#FAF8F3"
    return dark if contrast_ratio(dark, value) >= contrast_ratio(light, value) else light


def _contrast_surface(foreground: str, background: str) -> str:
    """Return the nearest quiet surface keeping foreground text at 4.5:1."""

    if contrast_ratio(foreground, background) >= 4.5:
        return background
    target = "#10171C" if _luminance(foreground) > _luminance(background) else "#FAF8F3"
    for step in range(1, 21):
        candidate = _mix(target, background, step / 20)
        if contrast_ratio(foreground, candidate) >= 4.5:
            return candidate
    return target


def _palette(
    *,
    bg: str,
    sidebar: str,
    card: str,
    card_alt: str,
    key: str,
    key_hover: str,
    accent: str,
    text: str,
    muted: str,
    line: str,
    up: str,
    down: str,
) -> dict[str, str]:
    accent_hover = _mix(text, accent, 0.16)
    selection = _mix(accent, card, 0.38)
    return {
        "bg": bg,
        "sidebar": sidebar,
        "card": card,
        "card_alt": card_alt,
        "key": key,
        "key_hover": key_hover,
        "muted_key": _mix(muted, key, 0.12),
        "accent": accent,
        "accent_hover": accent_hover,
        "accent_dark": _contrast_surface(accent, card),
        "text": text,
        "muted": muted,
        "line": line,
        "up": up,
        "down": down,
        "on_accent": _readable_on(accent),
        "subtle": muted,
        "grid": line,
        "up_fill": _mix(up, card, 0.10),
        "down_fill": _mix(down, card, 0.10),
        "up_row": _mix(up, card, 0.23),
        "down_row": _mix(down, card, 0.23),
        "tooltip": card_alt,
        "selection": selection,
        "selection_text": _readable_on(selection),
    }


_SPECS: Mapping[str, Mapping[str, str]] = {
    # Stable internal keys preserve existing saved settings.  The visible V3.21
    # palettes are restrained dark adaptations of established community colour
    # systems rather than twenty-eight almost-identical greys.
    "dark": dict(bg="#1E1E2E", sidebar="#181825", card="#28283A", card_alt="#313244", key="#393A4D", key_hover="#45475A", accent="#AAA6D2", text="#CDD6F4", muted="#A6ADC8", line="#45475A", up="#9CCB9B", down="#D58B9F"),
    "light": dict(bg="#24273A", sidebar="#1E2030", card="#2B2E44", card_alt="#363A52", key="#3E425B", key_hover="#494D64", accent="#8EAACF", text="#CAD3F5", muted="#A5ADCB", line="#494D64", up="#94C49C", down="#D28C9F"),
    "catppuccin_mocha": dict(bg="#201C26", sidebar="#191720", card="#2B2532", card_alt="#362E3E", key="#403849", key_hover="#4B4255", accent="#D0A0B2", text="#E6DCEC", muted="#B7A9BF", line="#4B4255", up="#9FC49D", down="#D1899D"),
    "catppuccin_latte": dict(bg="#303446", sidebar="#292C3C", card="#373B4D", card_alt="#414559", key="#4A4F65", key_hover="#555B73", accent="#8DB7CA", text="#C6D0F5", muted="#A5ADCE", line="#555B73", up="#99C09A", down="#D28C9D"),

    "nord": dict(bg="#2E3440", sidebar="#272C36", card="#363D49", card_alt="#404957", key="#495463", key_hover="#566273", accent="#88B8C5", text="#D8DEE9", muted="#AAB3C2", line="#4C566A", up="#9AB89A", down="#C88991"),
    "nord_light": dict(bg="#262D38", sidebar="#202630", card="#303845", card_alt="#3A4452", key="#44505E", key_hover="#4F5C6C", accent="#86B3AD", text="#D7DFE8", muted="#A8B3BC", line="#4C5968", up="#99B89D", down="#C98B92"),
    "slate": dict(bg="#242832", sidebar="#1E222B", card="#2D333E", card_alt="#373E4B", key="#414A59", key_hover="#4D5869", accent="#A6A6C4", text="#DDE1E8", muted="#AFB5BF", line="#4B5565", up="#9DB69B", down="#C98A98"),
    "mist_blue": dict(bg="#27323A", sidebar="#212A31", card="#303D45", card_alt="#3A4851", key="#45545E", key_hover="#50616C", accent="#91B6C0", text="#DBE2E6", muted="#AFBBC0", line="#4E5F69", up="#9BB8A0", down="#CA8D92"),

    "forest": dict(bg="#2D353B", sidebar="#272F33", card="#343F44", card_alt="#3D484D", key="#465258", key_hover="#505C62", accent="#92B193", text="#D3C6AA", muted="#A7B0A1", line="#4F585E", up="#A7C080", down="#D18A89"),
    "everforest_dark": dict(bg="#272E31", sidebar="#22282B", card="#30383C", card_alt="#394348", key="#424D52", key_hover="#4C585D", accent="#86B4A0", text="#D1C7B0", muted="#A5ACA2", line="#4C565A", up="#96B58C", down="#CD8888"),
    "everforest_light": dict(bg="#303837", sidebar="#293130", card="#384240", card_alt="#424D4A", key="#4B5753", key_hover="#56625E", accent="#A6B28A", text="#D7CBB2", muted="#AFB3A3", line="#56615C", up="#A8BF87", down="#D28E86"),
    "zenburn": dict(bg="#292E2A", sidebar="#232824", card="#323832", card_alt="#3C433C", key="#464E46", key_hover="#515A51", accent="#B0AD84", text="#D5D2BC", muted="#AAAFA0", line="#50594F", up="#A5B88C", down="#C78D88"),

    "plum": dict(bg="#191724", sidebar="#14131E", card="#211F30", card_alt="#2A273D", key="#343047", key_hover="#403D52", accent="#B5A1CF", text="#E0DEF4", muted="#A49FB8", line="#403D52", up="#9DBE9E", down="#D18B9F"),
    "rose_pine": dict(bg="#191724", sidebar="#15131F", card="#1F1D2E", card_alt="#29263A", key="#322E45", key_hover="#403D52", accent="#8FBBC4", text="#E0DEF4", muted="#A29DB6", line="#403D52", up="#9CBDA0", down="#D08B9B"),
    "rose_pine_dawn": dict(bg="#232136", sidebar="#1D1B2B", card="#2A273F", card_alt="#353149", key="#3E3A52", key_hover="#4A4660", accent="#C0A5D8", text="#E0DEF4", muted="#A8A3BC", line="#4A4660", up="#A1BE9F", down="#D18D9F"),
    "paper_sepia": dict(bg="#211C1B", sidebar="#1B1717", card="#2A2422", card_alt="#342D2A", key="#3E3732", key_hover="#4A433C", accent="#C8AA78", text="#E4DED4", muted="#AFA69B", line="#4A433C", up="#A5B891", down="#CE8B8A"),

    "tokyo_night": dict(bg="#1A1B26", sidebar="#16161E", card="#24283B", card_alt="#2D3247", key="#363C52", key_hover="#414868", accent="#829DD2", text="#C0CAF5", muted="#9FA9CF", line="#414868", up="#96BC83", down="#D08192"),
    "tokyo_day": dict(bg="#24283B", sidebar="#1F2335", card="#2B3045", card_alt="#343A51", key="#3D455C", key_hover="#48516A", accent="#85A7C7", text="#C0CAF5", muted="#9FAACD", line="#48516A", up="#97B887", down="#CE8392"),
    "one_dark": dict(bg="#1C1D2A", sidebar="#171823", card="#252738", card_alt="#2E3145", key="#383C51", key_hover="#444960", accent="#A59BD0", text="#C7CFF2", muted="#A3AAC9", line="#444960", up="#9ABA8B", down="#D18496"),
    "github_light": dict(bg="#182027", sidebar="#141A20", card="#212B34", card_alt="#2A3641", key="#33414D", key_hover="#3D4D5A", accent="#79B8C2", text="#C5D3E2", muted="#A1B0BF", line="#3D4D5A", up="#92B99A", down="#CF858F"),

    "gruvbox_dark": dict(bg="#1D2021", sidebar="#191B1C", card="#282828", card_alt="#32302F", key="#3C3836", key_hover="#504945", accent="#83A598", text="#EBDBB2", muted="#BDAE93", line="#504945", up="#A5AD67", down="#C97970"),
    "gruvbox_light": dict(bg="#242321", sidebar="#1E1D1B", card="#2E2B28", card_alt="#383430", key="#423D38", key_hover="#504945", accent="#C19D69", text="#E8D8B0", muted="#BBAE94", line="#504945", up="#A8AE6D", down="#C77B72"),
    "ayu_mirage": dict(bg="#202426", sidebar="#1A1E20", card="#292E30", card_alt="#33393B", key="#3D4446", key_hover="#494F50", accent="#86A8A1", text="#DDD4B8", muted="#B0A995", line="#4C514F", up="#9EAE78", down="#C77D74"),
    "ayu_light": dict(bg="#24201C", sidebar="#1E1B18", card="#2E2924", card_alt="#38322C", key="#433B34", key_hover="#50473E", accent="#C49A68", text="#E9D9B8", muted="#BAAC94", line="#51483F", up="#A7AD74", down="#C97D70"),

    "ocean": dict(bg="#002B36", sidebar="#06252D", card="#073642", card_alt="#0E404A", key="#174A54", key_hover="#225661", accent="#5C9DB1", text="#C2CDCA", muted="#93A1A1", line="#2B5B63", up="#8FA67A", down="#C47775"),
    "solarized_dark": dict(bg="#082A31", sidebar="#062229", card="#103740", card_alt="#18434B", key="#214D55", key_hover="#2B5961", accent="#6F9FC0", text="#C3CECA", muted="#98A8A5", line="#2F5C63", up="#92A879", down="#C47A72"),
    "solarized_light": dict(bg="#242821", sidebar="#1E221C", card="#2D322A", card_alt="#373D33", key="#42493D", key_hover="#4D5748", accent="#B19A63", text="#DDD8BD", muted="#B1AD94", line="#505A4B", up="#9FAB72", down="#C27C70"),
    "github_dark": dict(bg="#0E292B", sidebar="#0B2325", card="#163436", card_alt="#1E4042", key="#284A4C", key_hover="#325659", accent="#72A99F", text="#C8D2CB", muted="#9CAC9F", line="#365B5B", up="#91AC7A", down="#C27B73"),
}


THEMES = {name: _palette(**spec) for name, spec in _SPECS.items()}

THEME_LABELS = {
    "dark": "Catppuccin · 摩卡薰衣草",
    "light": "Catppuccin · 玛奇朵蓝",
    "catppuccin_mocha": "Catppuccin · 摩卡玫瑰",
    "catppuccin_latte": "Catppuccin · 果冻蓝",
    "nord": "Nord · 极光蓝",
    "nord_light": "Nord · 冰川青",
    "slate": "Nord · 极夜灰",
    "mist_blue": "Nord · 霜雾蓝",
    "forest": "Everforest · 森林",
    "everforest_dark": "Everforest · 深林",
    "everforest_light": "Everforest · 青苔",
    "zenburn": "Everforest · 暖叶",
    "plum": "Rosé Pine · 鸢尾",
    "rose_pine": "Rosé Pine · 松雾",
    "rose_pine_dawn": "Rosé Pine · 月夜",
    "paper_sepia": "Rosé Pine · 金砂",
    "tokyo_night": "Tokyo Night · 夜蓝",
    "tokyo_day": "Tokyo Night · 风暴",
    "one_dark": "Tokyo Night · 月紫",
    "github_light": "Tokyo Night · 霓青",
    "gruvbox_dark": "Gruvbox · 复古",
    "gruvbox_light": "Gruvbox · 暖棕",
    "ayu_mirage": "Gruvbox · 静蓝",
    "ayu_light": "Gruvbox · 灰橙",
    "ocean": "Solarized · 深海",
    "solarized_dark": "Solarized · 青蓝",
    "solarized_light": "Solarized · 暮金",
    "github_dark": "Solarized · 青绿",
}

_SOURCE_CATALOG = {
    "catppuccin": ("Catppuccin", "https://catppuccin.com/palette/"),
    "nord": ("Nord", "https://www.nordtheme.com/"),
    "everforest": ("Everforest", "https://github.com/sainnhe/everforest/blob/master/palette.md"),
    "rose_pine": ("Rosé Pine", "https://rosepinetheme.com/palette/"),
    "tokyo_night": ("Tokyo Night", "https://github.com/tokyo-night/tokyo-night-vscode-theme"),
    "gruvbox": ("Gruvbox", "https://github.com/morhetz/gruvbox"),
    "solarized": ("Solarized", "https://ethanschoonover.com/solarized/"),
}

_THEME_FAMILIES = {
    **{name: "catppuccin" for name in ("dark", "light", "catppuccin_mocha", "catppuccin_latte")},
    **{name: "nord" for name in ("nord", "nord_light", "slate", "mist_blue")},
    **{name: "everforest" for name in ("forest", "everforest_dark", "everforest_light", "zenburn")},
    **{name: "rose_pine" for name in ("plum", "rose_pine", "rose_pine_dawn", "paper_sepia")},
    **{name: "tokyo_night" for name in ("tokyo_night", "tokyo_day", "one_dark", "github_light")},
    **{name: "gruvbox" for name in ("gruvbox_dark", "gruvbox_light", "ayu_mirage", "ayu_light")},
    **{name: "solarized" for name in ("ocean", "solarized_dark", "solarized_light", "github_dark")},
}

# Public provenance lets the Settings gallery and project documentation state
# which published palette informed each adapted, application-specific theme.
THEME_SOURCES = {name: _SOURCE_CATALOG[family] for name, family in _THEME_FAMILIES.items()}

SUPPORTED_THEME_NAMES = frozenset(THEMES)


__all__ = [
    "SUPPORTED_THEME_NAMES",
    "THEMES",
    "THEME_LABELS",
    "THEME_SOURCES",
    "contrast_ratio",
]
