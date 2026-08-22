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
    "dark": dict(bg="#1E2228", sidebar="#242930", card="#2A3038", card_alt="#323943", key="#39424D", key_hover="#46515E", accent="#C58B52", text="#ECE9E4", muted="#ADB3BA", line="#4B5561", up="#79A487", down="#C17B7D"),
    "light": dict(bg="#F2F0EB", sidebar="#F8F6F1", card="#FCFAF6", card_alt="#EBE8E1", key="#E1DDD4", key_hover="#D5D0C5", accent="#8A5C32", text="#292D32", muted="#626A72", line="#CCC7BC", up="#47765A", down="#985257"),
    "ocean": dict(bg="#16242D", sidebar="#1B2C36", card="#223640", card_alt="#2A414D", key="#334B57", key_hover="#3E5966", accent="#78A9BD", text="#E8EEF0", muted="#A8BAC2", line="#49616C", up="#75A68E", down="#C38080"),
    "forest": dict(bg="#1B2721", sidebar="#223029", card="#293A31", card_alt="#32453A", key="#3A5043", key_hover="#465E50", accent="#B69A62", text="#E9ECE7", muted="#AAB7AD", line="#526458", up="#7EA287", down="#BC8179"),
    "plum": dict(bg="#28212C", sidebar="#302736", card="#392F40", card_alt="#44384C", key="#504159", key_hover="#5D4D67", accent="#A98BAF", text="#EEE9EF", muted="#B8AEBB", line="#68586F", up="#7FA28D", down="#BD808C"),
    "solarized_dark": dict(bg="#002B36", sidebar="#073642", card="#0B3B46", card_alt="#114550", key="#174F5B", key_hover="#205C68", accent="#B58900", text="#EEE8D5", muted="#93A1A1", line="#2B5962", up="#7C9B35", down="#C26C6C"),
    "solarized_light": dict(bg="#F4EEDC", sidebar="#FAF5E5", card="#FFF9EA", card_alt="#EAE3CF", key="#DED6C0", key_hover="#D2C8AE", accent="#856B12", text="#3E4A4D", muted="#647A80", line="#C8BFAB", up="#5D781F", down="#A44E4E"),
    "nord": dict(bg="#242933", sidebar="#2E3440", card="#343B49", card_alt="#3B4352", key="#444D5E", key_hover="#4E596D", accent="#81A1C1", text="#ECEFF4", muted="#B3BDCA", line="#586477", up="#8FBCBB", down="#C47B82"),
    "nord_light": dict(bg="#E9EDF2", sidebar="#F2F4F7", card="#F8F9FB", card_alt="#E1E6ED", key="#D5DCE5", key_hover="#C9D2DE", accent="#4F718F", text="#2E3440", muted="#5F6978", line="#BFC8D4", up="#507C74", down="#99565F"),
    "gruvbox_dark": dict(bg="#282828", sidebar="#32302F", card="#3A3836", card_alt="#45413D", key="#504A44", key_hover="#5C554E", accent="#C49A55", text="#EBDBB2", muted="#BDAE93", line="#665C54", up="#8FA66B", down="#CC7B6B"),
    "gruvbox_light": dict(bg="#F2E8CF", sidebar="#F8EFD8", card="#FCF4DF", card_alt="#E9DDC1", key="#DED0AF", key_hover="#D0C09D", accent="#805B1B", text="#3C3836", muted="#6D665B", line="#C6B894", up="#617A3D", down="#A54F43"),
    "everforest_dark": dict(bg="#26332C", sidebar="#2D3B33", card="#34443A", card_alt="#3D5044", key="#465B4D", key_hover="#526858", accent="#A7A96A", text="#E4E8D3", muted="#A7B3A4", line="#5A6F60", up="#83A779", down="#C18475"),
    "everforest_light": dict(bg="#EEEBDD", sidebar="#F4F1E5", card="#FAF7EB", card_alt="#E3E2D2", key="#D7D6C5", key_hover="#CACABB", accent="#59692F", text="#37423B", muted="#68736B", line="#BFC2B1", up="#4F7757", down="#9C574F"),
    "catppuccin_mocha": dict(bg="#242431", sidebar="#2B2B3A", card="#323243", card_alt="#3B3B4E", key="#454559", key_hover="#525268", accent="#A89AC3", text="#E8E6EF", muted="#B5B2C5", line="#5B5B70", up="#87A98B", down="#BF838B"),
    "catppuccin_latte": dict(bg="#ECECF2", sidebar="#F4F3F7", card="#FAF9FC", card_alt="#E2E1E9", key="#D6D5DF", key_hover="#CAC8D4", accent="#75639A", text="#343442", muted="#656477", line="#C0BECA", up="#4F7B5C", down="#9A5664"),
    "rose_pine": dict(bg="#24212A", sidebar="#2B2732", card="#332E3B", card_alt="#3D3746", key="#484052", key_hover="#554A5F", accent="#B294A8", text="#ECE6E9", muted="#B6ABB5", line="#625667", up="#7DA08A", down="#BE8288"),
    "rose_pine_dawn": dict(bg="#F0E9E8", sidebar="#F7F1EF", card="#FCF7F4", card_alt="#E7DFDE", key="#DCD2D2", key_hover="#CFC4C5", accent="#8D657B", text="#3B343D", muted="#6D626C", line="#C8BBBD", up="#547963", down="#995866"),
    "tokyo_night": dict(bg="#202432", sidebar="#272C3B", card="#2E3445", card_alt="#373E51", key="#414A5F", key_hover="#4C566D", accent="#8296C3", text="#E6E9F2", muted="#AAB2C5", line="#58637A", up="#7DA690", down="#C17D88"),
    "tokyo_day": dict(bg="#E9EBF1", sidebar="#F1F2F6", card="#F8F8FB", card_alt="#DFE2EA", key="#D3D7E1", key_hover="#C7CCD8", accent="#536F9D", text="#30343F", muted="#626A7A", line="#BBC1CE", up="#4E7B63", down="#995565"),
    "github_dark": dict(bg="#20252B", sidebar="#272D34", card="#2E353D", card_alt="#373F49", key="#414B56", key_hover="#4C5764", accent="#6F9BC4", text="#E6E9EC", muted="#ADB5BD", line="#58636F", up="#73A181", down="#C27C80"),
    "github_light": dict(bg="#EFF1F3", sidebar="#F6F7F8", card="#FCFCFD", card_alt="#E5E8EB", key="#DADDE1", key_hover="#CDD2D7", accent="#496F96", text="#2D333B", muted="#626B75", line="#C3C8CE", up="#49775A", down="#98535A"),
    "one_dark": dict(bg="#24272D", sidebar="#2B2F36", card="#32363F", card_alt="#3B404A", key="#454B57", key_hover="#515864", accent="#8094B3", text="#E5E8EC", muted="#ABB2BF", line="#5B6370", up="#82A77A", down="#C27D81"),
    "ayu_mirage": dict(bg="#242A31", sidebar="#2B323A", card="#333B44", card_alt="#3C4650", key="#46515C", key_hover="#525E6A", accent="#BE9563", text="#E8E5DF", muted="#ABB2B8", line="#5C6873", up="#82A486", down="#C1807E"),
    "ayu_light": dict(bg="#F0EEE8", sidebar="#F7F5EF", card="#FCFAF5", card_alt="#E6E3DB", key="#DAD6CC", key_hover="#CEC9BD", accent="#87622F", text="#333538", muted="#666C71", line="#C5C0B5", up="#4D7758", down="#98534F"),
    "zenburn": dict(bg="#30332F", sidebar="#383B36", card="#40443E", card_alt="#494E46", key="#545A51", key_hover="#60675D", accent="#A9A477", text="#E4E5D7", muted="#B3B8A9", line="#6A7165", up="#8BA78A", down="#C58A82"),
    "paper_sepia": dict(bg="#ECE5D6", sidebar="#F3ECDE", card="#F9F2E4", card_alt="#E1D8C8", key="#D5CBB9", key_hover="#C8BDA9", accent="#80613E", text="#37332D", muted="#696258", line="#BDB3A2", up="#55745B", down="#92564E"),
    "slate": dict(bg="#252A2F", sidebar="#2C3238", card="#343B42", card_alt="#3D464E", key="#47515A", key_hover="#535E68", accent="#869BA8", text="#E7E9EA", muted="#AFB6BA", line="#5F6971", up="#82A08B", down="#BC8383"),
    "mist_blue": dict(bg="#E8EDF0", sidebar="#F0F4F5", card="#F7F9F9", card_alt="#DEE6E9", key="#D2DDE1", key_hover="#C5D2D7", accent="#536F7E", text="#30383D", muted="#606D73", line="#B9C7CC", up="#4E7560", down="#92585A"),
}


THEMES = {name: _palette(**spec) for name, spec in _SPECS.items()}

THEME_LABELS = {
    "dark": "静谧深灰",
    "light": "柔和米白",
    "ocean": "雾海蓝",
    "forest": "苔原绿",
    "plum": "灰暮紫",
    "solarized_dark": "日光深色",
    "solarized_light": "日光浅色",
    "nord": "北境夜",
    "nord_light": "北境昼",
    "gruvbox_dark": "复古深棕",
    "gruvbox_light": "复古浅棕",
    "everforest_dark": "常青深色",
    "everforest_light": "常青浅色",
    "catppuccin_mocha": "摩卡灰紫",
    "catppuccin_latte": "拿铁灰白",
    "rose_pine": "玫瑰松夜",
    "rose_pine_dawn": "玫瑰松晨",
    "tokyo_night": "东京夜",
    "tokyo_day": "东京昼",
    "github_dark": "代码深灰",
    "github_light": "代码浅灰",
    "one_dark": "一体深灰",
    "ayu_mirage": "蜃景暖灰",
    "ayu_light": "蜃景浅色",
    "zenburn": "禅意灰绿",
    "paper_sepia": "纸张棕褐",
    "slate": "石板灰",
    "mist_blue": "晨雾蓝",
}

SUPPORTED_THEME_NAMES = frozenset(THEMES)


__all__ = [
    "SUPPORTED_THEME_NAMES",
    "THEMES",
    "THEME_LABELS",
    "contrast_ratio",
]
