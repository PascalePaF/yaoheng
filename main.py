"""曜衡 portable Windows entry point."""

from app_ui import YaohengApp, enable_dpi_awareness


if __name__ == "__main__":
    enable_dpi_awareness()
    YaohengApp().run()
