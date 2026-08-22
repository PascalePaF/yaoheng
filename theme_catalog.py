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
    # Legacy keys remain stable so saved settings migrate without resetting.  All
    # visual variants are now layered, low-saturation dark palettes.
    "dark": dict(bg="#121518", sidebar="#181C20", card="#20252A", card_alt="#282E34", key="#30373E", key_hover="#39424A", accent="#A3A9AE", text="#E7E9EA", muted="#A6ADB2", line="#3D454D", up="#8FA892", down="#B98E8E"),
    "light": dict(bg="#171719", sidebar="#1E1E21", card="#26272B", card_alt="#2E3035", key="#363940", key_hover="#40444C", accent="#B0A495", text="#E9E7E3", muted="#ADA9A2", line="#45474E", up="#91A591", down="#B7908A"),
    "ocean": dict(bg="#11191E", sidebar="#182228", card="#202C33", card_alt="#283740", key="#31414A", key_hover="#3A4C56", accent="#94ABB5", text="#E5EAEC", muted="#A2B0B6", line="#40515A", up="#89A69B", down="#B68C8E"),
    "forest": dict(bg="#121A16", sidebar="#19231D", card="#212D26", card_alt="#29372F", key="#314138", key_hover="#3A4B41", accent="#A1AE98", text="#E5E9E5", muted="#A5AFA7", line="#415149", up="#8EA895", down="#B58D89"),
    "plum": dict(bg="#19151B", sidebar="#211C24", card="#2A242D", card_alt="#342D38", key="#3E3643", key_hover="#49404F", accent="#AA9BAD", text="#EAE6EA", muted="#AFA7B0", line="#4D4352", up="#91A397", down="#B78F99"),
    "solarized_dark": dict(bg="#10191B", sidebar="#172225", card="#1F2C2F", card_alt="#27373A", key="#304145", key_hover="#394C50", accent="#A8A78E", text="#E8E8E1", muted="#A7AEAA", line="#405155", up="#93A58B", down="#B68D86"),
    "solarized_light": dict(bg="#1A1713", sidebar="#221E19", card="#2B261F", card_alt="#352F27", key="#40392F", key_hover="#4B4439", accent="#B0A087", text="#EBE7DF", muted="#B0A89C", line="#51493E", up="#98A28A", down="#B7908B"),
    "nord": dict(bg="#141820", sidebar="#1B202A", card="#232A36", card_alt="#2B3442", key="#343E4E", key_hover="#3D495B", accent="#98A9BC", text="#E7EAF0", muted="#A7AFBB", line="#455165", up="#91A7A4", down="#B88D95"),
    "nord_light": dict(bg="#171C23", sidebar="#1E252E", card="#262F3A", card_alt="#2F3946", key="#384452", key_hover="#424F5F", accent="#9FADBA", text="#E8EBED", muted="#AAB2B8", line="#495562", up="#91A69E", down="#B78F92"),
    "gruvbox_dark": dict(bg="#191816", sidebar="#211F1C", card="#2A2723", card_alt="#342F2A", key="#3E3932", key_hover="#49433A", accent="#B0A087", text="#EAE6DC", muted="#AEA79A", line="#504A41", up="#98A48B", down="#B7908B"),
    "gruvbox_light": dict(bg="#1C1814", sidebar="#241F1A", card="#2D2721", card_alt="#372F28", key="#423931", key_hover="#4D443A", accent="#B39F84", text="#ECE7DE", muted="#B1A89B", line="#534A40", up="#99A38A", down="#B8918C"),
    "everforest_dark": dict(bg="#141A17", sidebar="#1B231F", card="#232D28", card_alt="#2C3831", key="#35423A", key_hover="#3F4D44", accent="#A4AC91", text="#E7E9E3", muted="#A8AEA4", line="#465149", up="#91A58F", down="#B58F8A"),
    "everforest_light": dict(bg="#171B16", sidebar="#1E241D", card="#272E25", card_alt="#30392E", key="#3A4437", key_hover="#454F41", accent="#A8AD92", text="#E9EAE4", muted="#AAAEA4", line="#495247", up="#94A691", down="#B6908B"),
    "catppuccin_mocha": dict(bg="#181720", sidebar="#201F29", card="#292833", card_alt="#33313E", key="#3D3B49", key_hover="#484655", accent="#AAA0B8", text="#E9E7EC", muted="#ACA8B4", line="#4D4A59", up="#91A397", down="#B78E99"),
    "catppuccin_latte": dict(bg="#1B181D", sidebar="#231F25", card="#2C272F", card_alt="#362F39", key="#413945", key_hover="#4C4351", accent="#B0A0AC", text="#EBE7EA", muted="#AFA8AD", line="#514850", up="#93A395", down="#B99096"),
    "rose_pine": dict(bg="#19161B", sidebar="#211D23", card="#2A252D", card_alt="#342E38", key="#3E3743", key_hover="#49414F", accent="#B09EA9", text="#EBE6E9", muted="#AFA6AC", line="#4E4552", up="#90A294", down="#BA8E97"),
    "rose_pine_dawn": dict(bg="#1C1719", sidebar="#241E21", card="#2D272A", card_alt="#382F33", key="#42393D", key_hover="#4D4348", accent="#B3A0A2", text="#ECE7E7", muted="#B1A8A8", line="#53484B", up="#96A397", down="#BB9093"),
    "tokyo_night": dict(bg="#131720", sidebar="#1A202A", card="#222A36", card_alt="#2B3442", key="#343F4E", key_hover="#3E4A5B", accent="#99A8BD", text="#E6E9EF", muted="#A5AEBB", line="#455165", up="#8FA59B", down="#B78C96"),
    "tokyo_day": dict(bg="#161A21", sidebar="#1D232C", card="#252D38", card_alt="#2E3744", key="#374250", key_hover="#414D5D", accent="#9DAABC", text="#E8EAEF", muted="#A8AFBA", line="#485462", up="#91A59B", down="#B88E95"),
    "github_dark": dict(bg="#13171B", sidebar="#1A1F24", card="#22282E", card_alt="#2B3239", key="#343C45", key_hover="#3E4852", accent="#98A8B4", text="#E6E9EB", muted="#A4ABB1", line="#444E57", up="#8FA493", down="#B68C8E"),
    "github_light": dict(bg="#181A1D", sidebar="#1F2226", card="#282C31", card_alt="#31363C", key="#3A4047", key_hover="#454B54", accent="#A4ACB3", text="#E9EAEA", muted="#AAADB1", line="#4A5058", up="#94A594", down="#B89090"),
    "one_dark": dict(bg="#15171B", sidebar="#1C2025", card="#24292F", card_alt="#2D333A", key="#363D46", key_hover="#404852", accent="#9DA8B4", text="#E7E9EB", muted="#A6ABB2", line="#464E58", up="#91A493", down="#B78D90"),
    "ayu_mirage": dict(bg="#171A1D", sidebar="#1E2226", card="#272C31", card_alt="#30363C", key="#3A4148", key_hover="#454C54", accent="#B0A18D", text="#EAE8E3", muted="#ADA9A2", line="#4A5158", up="#95A38F", down="#B9918A"),
    "ayu_light": dict(bg="#1A1815", sidebar="#221F1B", card="#2B2722", card_alt="#352F29", key="#403A32", key_hover="#4B443B", accent="#B1A28C", text="#EBE8E1", muted="#AFA99F", line="#514A41", up="#98A38E", down="#B7908C"),
    "zenburn": dict(bg="#181B17", sidebar="#20241F", card="#292E27", card_alt="#33382F", key="#3D4338", key_hover="#484F42", accent="#A8AA91", text="#E8E9E2", muted="#AAAEA3", line="#4D5348", up="#95A58F", down="#B6908B"),
    "paper_sepia": dict(bg="#1B1814", sidebar="#231F1A", card="#2C2721", card_alt="#362F28", key="#413930", key_hover="#4C4339", accent="#B1A18B", text="#EBE7DE", muted="#B0A89B", line="#52493F", up="#98A28D", down="#B7908B"),
    "slate": dict(bg="#15191D", sidebar="#1C2126", card="#242A30", card_alt="#2D343B", key="#363E46", key_hover="#404951", accent="#9EA9B0", text="#E7E9EA", muted="#A7ADB1", line="#464F57", up="#91A397", down="#B68F8F"),
    "mist_blue": dict(bg="#141A1E", sidebar="#1B2227", card="#232C32", card_alt="#2C363D", key="#354149", key_hover="#3F4C55", accent="#9CAEB7", text="#E7EBEC", muted="#A6B0B4", line="#45525A", up="#90A69A", down="#B68D91"),
}


THEMES = {name: _palette(**spec) for name, spec in _SPECS.items()}

THEME_LABELS = {
    "dark": "墨夜石墨",
    "light": "月岩深灰",
    "ocean": "深海雾蓝",
    "forest": "松林墨绿",
    "plum": "暮色灰紫",
    "solarized_dark": "暗潮青灰",
    "solarized_light": "旧铜夜色",
    "nord": "北境深蓝",
    "nord_light": "峡湾蓝灰",
    "gruvbox_dark": "复古炭棕",
    "gruvbox_light": "胡桃夜棕",
    "everforest_dark": "常青墨色",
    "everforest_light": "松针深绿",
    "catppuccin_mocha": "摩卡夜紫",
    "catppuccin_latte": "奶咖深灰",
    "rose_pine": "玫瑰松夜",
    "rose_pine_dawn": "烟玫深色",
    "tokyo_night": "东京深夜",
    "tokyo_day": "雨巷蓝灰",
    "github_dark": "代码炭灰",
    "github_light": "钛银深灰",
    "one_dark": "一体夜灰",
    "ayu_mirage": "蜃景暖夜",
    "ayu_light": "砂岩夜色",
    "zenburn": "禅意墨绿",
    "paper_sepia": "墨纸深棕",
    "slate": "深石板灰",
    "mist_blue": "雾港深蓝",
}

SUPPORTED_THEME_NAMES = frozenset(THEMES)


__all__ = [
    "SUPPORTED_THEME_NAMES",
    "THEMES",
    "THEME_LABELS",
    "contrast_ratio",
]
