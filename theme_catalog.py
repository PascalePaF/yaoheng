"""Accessible, context-aware application themes for Yaoheng.

V3.21.1 replaces the former dark-only catalog. Every palette explicitly
chooses either white or black primary text, then derives separate surfaces for
navigation, cards, inputs, calculator keys, operators, selection, and charts.
The source colors were composed with dedicated palette tools; runtime use is
fully offline and deterministic.
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
    amount = max(0.0, min(1.0, amount))
    return _hex(*(round(back[index] + (front[index] - back[index]) * amount) for index in range(3)))


def _luminance(value: str) -> float:
    channels: list[float] = []
    for channel in _rgb(value):
        normalized = channel / 255
        channels.append(
            normalized / 12.92
            if normalized <= 0.04045
            else ((normalized + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _readable_on(value: str) -> str:
    dark = "#111111"
    light = "#FFFFFF"
    return dark if contrast_ratio(dark, value) >= contrast_ratio(light, value) else light


def _ensure_contrast(value: str, background: str, minimum: float, target: str) -> str:
    if contrast_ratio(value, background) >= minimum:
        return value
    for step in range(1, 21):
        candidate = _mix(target, value, step / 20)
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
    return target


def _contrast_surface(foreground: str, background: str) -> str:
    """Return a quiet surface on which ``foreground`` stays readable."""

    if contrast_ratio(foreground, background) >= 4.5:
        return background
    target = max(
        ("#111111", "#FFFFFF"),
        key=lambda candidate: contrast_ratio(foreground, candidate),
    )
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
    accent: str,
    text: str,
    up: str,
    down: str,
) -> dict[str, str]:
    if text not in {"#FFFFFF", "#111111"}:
        raise ValueError("every theme must explicitly select white or black text")
    dark_text = text == "#111111"
    surface_step = 0.055 if dark_text else 0.075
    card_alt = _mix(text, card, surface_step)
    elevated = _mix(text, card, surface_step * 0.55)
    key = _mix(text, card, 0.095 if dark_text else 0.13)
    key_hover = _mix(text, card, 0.145 if dark_text else 0.20)
    line = _mix(text, card, 0.18 if dark_text else 0.25)
    muted = _ensure_contrast(_mix(text, card, 0.67), card, 4.5, text)
    subtle = _ensure_contrast(_mix(text, card, 0.53), key, 3.0, text)
    on_accent = _readable_on(accent)
    accent_hover = _mix(on_accent, accent, 0.13)
    accent_surface = _contrast_surface(accent, card)
    selection = accent
    selection_text = on_accent
    function_bg = _mix(text, card, 0.19 if dark_text else 0.27)
    function_text = _ensure_contrast(text, function_bg, 4.5, text)
    up = _ensure_contrast(up, card, 3.0, "#08783E" if dark_text else "#8EF0B5")
    down = _ensure_contrast(down, card, 3.0, "#B4233E" if dark_text else "#FF9BA8")
    return {
        "bg": bg,
        "sidebar": sidebar,
        "card": card,
        "card_alt": card_alt,
        "elevated": elevated,
        "line": line,
        "text": text,
        "muted": muted,
        "subtle": subtle,
        "sidebar_text": _ensure_contrast(text, sidebar, 4.5, text),
        "sidebar_muted": _ensure_contrast(_mix(text, sidebar, 0.64), sidebar, 3.0, text),
        "key": key,
        "key_hover": key_hover,
        "key_text": _ensure_contrast(text, key, 4.5, text),
        "muted_key": function_bg,
        "button_bg": key,
        "button_text": _ensure_contrast(text, key, 4.5, text),
        "button_hover": key_hover,
        "input_bg": card_alt,
        "input_text": _ensure_contrast(text, card_alt, 4.5, text),
        "input_border": line,
        "focus": accent,
        "nav_hover": _mix(text, sidebar, 0.095 if dark_text else 0.15),
        "nav_active": _contrast_surface(accent, sidebar),
        "nav_active_text": accent,
        "accent": accent,
        "accent_hover": accent_hover,
        "accent_dark": accent_surface,
        "on_accent": on_accent,
        "selection": selection,
        "selection_text": selection_text,
        "calculator_bg": bg,
        "calculator_panel": elevated,
        "display_bg": elevated,
        "display_expression": muted,
        "display_text": text,
        "calc_number_bg": key,
        "calc_number_hover": key_hover,
        "calc_number_text": _ensure_contrast(text, key, 4.5, text),
        "calc_function_bg": function_bg,
        "calc_function_hover": _mix(text, function_bg, 0.14),
        "calc_function_text": function_text,
        "calc_operator_bg": accent,
        "calc_operator_hover": accent_hover,
        "calc_operator_text": on_accent,
        "up": up,
        "down": down,
        "grid": line,
        "up_fill": _mix(up, card, 0.13),
        "down_fill": _mix(down, card, 0.13),
        "up_row": _mix(up, card, 0.22),
        "down_row": _mix(down, card, 0.22),
        "tooltip": elevated,
    }


# Stable keys keep existing settings valid, but all V3.21 palettes and names
# have been retired. These are new light, dark, pastel, vivid, warm, and cool
# compositions created for application context rather than terminal themes.
_SPECS: Mapping[str, Mapping[str, str]] = {
    "dark": dict(bg="#0F172A", sidebar="#08111F", card="#1E293B", accent="#F59E0B", text="#FFFFFF", up="#4ADE80", down="#FB7185"),
    "light": dict(bg="#EEF4FF", sidebar="#DCE8FA", card="#FAFCFF", accent="#2563EB", text="#111111", up="#0F8A50", down="#C62F50"),
    "catppuccin_mocha": dict(bg="#FFF3F0", sidebar="#FFE4E1", card="#FFFAF8", accent="#E84A6A", text="#111111", up="#138A66", down="#C52F54"),
    "catppuccin_latte": dict(bg="#EDF8F3", sidebar="#D8EFE5", card="#F9FDFA", accent="#087F6B", text="#111111", up="#087F4B", down="#BF3950"),
    "nord": dict(bg="#101B3D", sidebar="#09132E", card="#192952", accent="#5EA6FF", text="#FFFFFF", up="#4BDD9B", down="#FF718C"),
    "nord_light": dict(bg="#EAF8FF", sidebar="#D2EDFA", card="#F8FCFF", accent="#087EA4", text="#111111", up="#087C55", down="#C63850"),
    "slate": dict(bg="#171A1F", sidebar="#0F1115", card="#24282F", accent="#B5E853", text="#FFFFFF", up="#6AE59A", down="#FF7A8E"),
    "mist_blue": dict(bg="#ECF7FF", sidebar="#D9EEFA", card="#FAFDFF", accent="#168AAD", text="#111111", up="#087C59", down="#BD3550"),
    "forest": dict(bg="#F0F7E8", sidebar="#DDEBCD", card="#FBFDF8", accent="#477A38", text="#111111", up="#267A43", down="#B93C43"),
    "everforest_dark": dict(bg="#092F2A", sidebar="#06231F", card="#11443C", accent="#3CD3A1", text="#FFFFFF", up="#67E89D", down="#FF7885"),
    "everforest_light": dict(bg="#F5F8DE", sidebar="#E8EFC3", card="#FCFDEE", accent="#6E8D16", text="#111111", up="#3D7F24", down="#BE3C46"),
    "zenburn": dict(bg="#FFF6DF", sidebar="#F3E4BA", card="#FFFCF2", accent="#C16C12", text="#111111", up="#397A39", down="#B93640"),
    "plum": dict(bg="#21122E", sidebar="#150B20", card="#342042", accent="#E969B7", text="#FFFFFF", up="#61DEA0", down="#FF758A"),
    "rose_pine": dict(bg="#FFF0F5", sidebar="#FADBE8", card="#FFF9FB", accent="#C73E76", text="#111111", up="#13805B", down="#BF3151"),
    "rose_pine_dawn": dict(bg="#F5EEFF", sidebar="#E7D9F8", card="#FCFAFF", accent="#7749C7", text="#111111", up="#11805D", down="#C23655"),
    "paper_sepia": dict(bg="#F7EFE4", sidebar="#EADAC7", card="#FEFBF6", accent="#9B5A2A", text="#111111", up="#397641", down="#B83B45"),
    "tokyo_night": dict(bg="#111135", sidebar="#090A24", card="#202050", accent="#8B7BFF", text="#FFFFFF", up="#59E0A0", down="#FF718E"),
    "tokyo_day": dict(bg="#E9F0FF", sidebar="#D5E2FA", card="#F9FBFF", accent="#3157D5", text="#111111", up="#0E8054", down="#C23854"),
    "one_dark": dict(bg="#111C2B", sidebar="#09131F", card="#1B3043", accent="#2DD4BF", text="#FFFFFF", up="#5BE394", down="#FF7484"),
    "github_light": dict(bg="#F2F3F5", sidebar="#E2E5E9", card="#FCFCFD", accent="#353A40", text="#111111", up="#177C49", down="#BD354B"),
    "gruvbox_dark": dict(bg="#1F1A10", sidebar="#141109", card="#332918", accent="#F3B43F", text="#FFFFFF", up="#86D66C", down="#FF796F"),
    "gruvbox_light": dict(bg="#FFF0E4", sidebar="#F7DCC8", card="#FFFAF6", accent="#E0672D", text="#111111", up="#2D7E49", down="#BF384A"),
    "ayu_mirage": dict(bg="#102A43", sidebar="#081D30", card="#1B3B57", accent="#FF7D66", text="#FFFFFF", up="#52DEA0", down="#FF7890"),
    "ayu_light": dict(bg="#FFF0E9", sidebar="#FBDACE", card="#FFFAF7", accent="#E85032", text="#111111", up="#16805A", down="#C1344C"),
    "ocean": dict(bg="#062E46", sidebar="#031E30", card="#0D425F", accent="#32C7E8", text="#FFFFFF", up="#5AE29B", down="#FF7185"),
    "solarized_dark": dict(bg="#052F34", sidebar="#032225", card="#0D4449", accent="#28D1B5", text="#FFFFFF", up="#65E59A", down="#FF7582"),
    "solarized_light": dict(bg="#FFF9D9", sidebar="#F5ECA8", card="#FFFDF0", accent="#C28700", text="#111111", up="#437A24", down="#B93B42"),
    "github_dark": dict(bg="#21133D", sidebar="#150B2B", card="#342259", accent="#B777FF", text="#FFFFFF", up="#60E0A0", down="#FF7594"),
}


THEMES = {name: _palette(**spec) for name, spec in _SPECS.items()}


THEME_LABELS: Mapping[str, str] = {
    "dark": "午夜柑橘", "light": "瓷白蓝", "catppuccin_mocha": "樱花奶油",
    "catppuccin_latte": "薄荷纸", "nord": "钴蓝夜", "nord_light": "冰川玻璃",
    "slate": "石墨青柠", "mist_blue": "晴空玻璃", "forest": "植物园",
    "everforest_dark": "翡翠夜", "everforest_light": "青柠奶霜", "zenburn": "琥珀纸",
    "plum": "莓果夜", "rose_pine": "玫瑰牛奶", "rose_pine_dawn": "丁香云",
    "paper_sepia": "咖啡奶油", "tokyo_night": "电光靛蓝", "tokyo_day": "晴日蓝",
    "one_dark": "极光青", "github_light": "单色工作室", "gruvbox_dark": "墨金",
    "gruvbox_light": "复古杏", "ayu_mirage": "珊瑚湾", "ayu_light": "蜜桃汽水",
    "ocean": "深海青", "solarized_dark": "松石夜", "solarized_light": "柠檬晴空",
    "github_dark": "紫罗兰霓虹",
}


THEME_LABELS_I18N: Mapping[str, Mapping[str, str]] = {
    "en_US": {
        "dark": "Midnight Citrus", "light": "Porcelain Blue", "catppuccin_mocha": "Sakura Cream",
        "catppuccin_latte": "Mint Paper", "nord": "Cobalt Night", "nord_light": "Glacier Glass",
        "slate": "Graphite Lime", "mist_blue": "Sky Glass", "forest": "Botanical",
        "everforest_dark": "Jade Night", "everforest_light": "Lime Cream", "zenburn": "Amber Paper",
        "plum": "Berry Night", "rose_pine": "Rose Milk", "rose_pine_dawn": "Lilac Cloud",
        "paper_sepia": "Coffee Cream", "tokyo_night": "Electric Indigo", "tokyo_day": "Blue Day",
        "one_dark": "Teal Aurora", "github_light": "Mono Studio", "gruvbox_dark": "Ink & Gold",
        "gruvbox_light": "Retro Apricot", "ayu_mirage": "Coral Bay", "ayu_light": "Peach Soda",
        "ocean": "Deep Ocean", "solarized_dark": "Turquoise Night", "solarized_light": "Lemon Sky",
        "github_dark": "Violet Neon",
    },
    "ja_JP": {
        "dark": "真夜中シトラス", "light": "ポーセリンブルー", "catppuccin_mocha": "桜クリーム",
        "catppuccin_latte": "ミントペーパー", "nord": "コバルトナイト", "nord_light": "氷河ガラス",
        "slate": "グラファイトライム", "mist_blue": "スカイガラス", "forest": "ボタニカル",
        "everforest_dark": "翡翠の夜", "everforest_light": "ライムクリーム", "zenburn": "アンバーペーパー",
        "plum": "ベリーナイト", "rose_pine": "ローズミルク", "rose_pine_dawn": "ライラッククラウド",
        "paper_sepia": "コーヒークリーム", "tokyo_night": "エレクトリックインディゴ", "tokyo_day": "晴日ブルー",
        "one_dark": "ティールオーロラ", "github_light": "モノスタジオ", "gruvbox_dark": "墨と金",
        "gruvbox_light": "レトロアプリコット", "ayu_mirage": "コーラルベイ", "ayu_light": "ピーチソーダ",
        "ocean": "深海", "solarized_dark": "ターコイズナイト", "solarized_light": "レモンスカイ",
        "github_dark": "バイオレットネオン",
    },
}


def theme_label(name: str, language: str = "zh_CN") -> str:
    if language == "zh_TW":
        table = str.maketrans("蓝兰园纸电单双复旧湾汤柠罗", "藍蘭園紙電單雙復舊灣湯檸羅")
        return str(THEME_LABELS[name]).translate(table)
    return str(THEME_LABELS_I18N.get(language, {}).get(name, THEME_LABELS[name]))


_COOLORS = "Coolors"
_COLOR_HUNT = "Color Hunt"
_HUEMINT = "Huemint"
_SOURCES: tuple[tuple[str, str], ...] = (
    (_COOLORS, "https://coolors.co/0f172a-1e293b-f59e0b-4ade80-fb7185"),
    (_COOLORS, "https://coolors.co/eef4ff-dce8fa-fafcff-2563eb-111111"),
    (_COLOR_HUNT, "https://colorhunt.co/palette/fff3f0ffe4e1e84a6a111111"),
    (_HUEMINT, "https://huemint.com/website-1/"),
    (_COOLORS, "https://coolors.co/101b3d-192952-5ea6ff-4bdd9b-ff718c"),
    (_COLOR_HUNT, "https://colorhunt.co/palette/eaf8ffd2edfaf8fcff087ea4"),
    (_HUEMINT, "https://huemint.com/website-1/"),
)

THEME_SOURCES = {name: _SOURCES[index % len(_SOURCES)] for index, name in enumerate(THEMES)}

SUPPORTED_THEME_NAMES = frozenset(THEMES)


__all__ = [
    "SUPPORTED_THEME_NAMES", "THEMES", "THEME_LABELS", "THEME_LABELS_I18N",
    "THEME_SOURCES", "contrast_ratio", "theme_label",
]
