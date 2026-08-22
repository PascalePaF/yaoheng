"""曜衡 portable Windows entry point."""

import tkinter as tk

from app_ui import YaohengApp, enable_dpi_awareness
from single_instance import SingleInstance, SingleInstanceError, show_startup_error


ACTIVATION_POLL_MS = 120


def run_application() -> int:
    try:
        instance = SingleInstance()
    except SingleInstanceError as exc:
        show_startup_error(str(exc))
        return 1

    with instance:
        if not instance.is_primary:
            instance.notify_existing()
            return 0

        enable_dpi_awareness()
        app = YaohengApp()

        def poll_activation() -> None:
            if app.exiting:
                return
            if instance.consume_activation():
                app.restore_window()
            try:
                app.root.after(ACTIVATION_POLL_MS, poll_activation)
            except (RuntimeError, tk.TclError):
                return

        app.root.after(ACTIVATION_POLL_MS, poll_activation)
        app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_application())
