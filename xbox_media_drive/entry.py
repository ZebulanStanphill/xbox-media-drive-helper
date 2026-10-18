"""Entry point for the self-contained desktop application and its CLI helper."""
import json
import platform
import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--cli"]:
        # Keep Qt out of the privileged process. A frozen executable is not a
        # Python interpreter, so it needs this explicit dispatch instead of -m.
        from .cli import main as cli_main
        return cli_main(arguments[1:])
    if arguments == ["--self-test"]:
        # Packaging verification: initialize the real Qt platform plugin and
        # native security API, without opening or modifying any drive.
        from PySide6.QtWidgets import QApplication
        from . import __version__
        from .gui import MainWindow, STYLE
        from .native import load_library
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        app.setStyleSheet(STYLE)
        window = MainWindow(auto_refresh=False)
        window.show()
        app.processEvents()
        if window.grab().isNull():
            raise RuntimeError("The packaged Qt interface could not render.")
        native = load_library()
        print(json.dumps({"version": __version__, "architecture": platform.machine(),
                          "gui": True, "library": str(native._name)}), flush=True)
        window.close()
        return 0
    from .gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
