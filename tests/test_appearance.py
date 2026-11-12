import json

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ofs.appearance import _variant, appearance_is_dark, apply_appearance
from ofs.settings import theme, set_theme
from ofs.ui import MainWindow, SfsApplication


def _widgets(widget):
    yield widget
    children = []
    child = widget.get_first_child()
    while child is not None:
        children.append(child)
        yield from _widgets(child)
        child = child.get_next_sibling()
    extra = widget.get_child() if hasattr(widget, "get_child") else None
    if extra is not None and extra not in children:
        yield from _widgets(extra)


def _dropdown_options(widget):
    model = widget.get_model()
    return [model.get_string(index) for index in range(model.get_n_items())]


def _foreground(widget):
    color = widget.get_style_context().get_color()
    return (color.red + color.green + color.blue) / 3


def test_named_dark_theme_variants_are_selected_without_renaming_the_family():
    assert _variant("Yaru-olive-dark", False) == "Yaru-olive"
    assert _variant("Yaru-olive", True) == "Yaru-olive-dark"
    assert _variant("Adwaita-dark", True) == "Adwaita-dark"
    assert _variant("Adwaita", False) == "Adwaita"
    assert _variant("", True) == ""


def test_theme_setting_defaults_to_system_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path))
    assert theme() == "system"
    set_theme("dark")
    assert theme() == "dark"
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["theme"] == "dark"
    with pytest.raises(ValueError):
        set_theme("midnight")
    assert theme() == "dark"
    (tmp_path / "settings.json").write_text('{"theme":"unknown"}')
    assert theme() == "system"


def test_settings_save_applies_light_dark_and_system(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestAppearance")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window.present()
            settings = window._settings()
            choices = [item for item in _widgets(settings) if isinstance(item, Gtk.DropDown)]
            appearance = next(item for item in choices if "Light" in _dropdown_options(item))
            seen["labels"] = _dropdown_options(appearance)
            appearance.set_selected(2)  # Dark
            save = next(item for item in _widgets(settings) if isinstance(item, Gtk.Button) and item.get_label() == "Save")
            save.emit("clicked")
            seen["dark"] = appearance_is_dark()
            seen["dark_foreground"] = _foreground(window.search)
            seen["toggle_dark"] = window.appearance_button.get_active()
            seen["saved_dark"] = theme()
            appearance.set_selected(1)  # Light
            save.emit("clicked")
            seen["light"] = appearance_is_dark()
            seen["light_foreground"] = _foreground(window.search)
            seen["toggle_light"] = window.appearance_button.get_active()
            seen["saved_light"] = theme()
            window.appearance_button.emit("clicked")
            seen["quick_dark"] = (window.appearance_button.get_active(), appearance_is_dark(), theme())
            window.appearance_button.emit("clicked")
            seen["quick_light"] = (window.appearance_button.get_active(), appearance_is_dark(), theme())
            set_theme("system")
            apply_appearance("system")
            seen["system"] = appearance_is_dark()
            self.quit()

    assert App().run(None) == 0
    assert seen["labels"] == ["System", "Light", "Dark"]
    assert seen["dark"] is True
    assert seen["saved_dark"] == "dark"
    assert seen["light"] is False
    assert seen["saved_light"] == "light"
    assert seen["dark_foreground"] > 0.8
    assert seen["light_foreground"] < 0.5
    assert seen["toggle_dark"] is True and seen["toggle_light"] is False
    assert seen["quick_dark"] == (True, True, "dark")
    assert seen["quick_light"] == (False, False, "light")
    assert seen["system"] in {True, False}
    set_theme("dark")

    class StartupApp(SfsApplication):
        def do_activate(self):
            super().do_activate()
            seen["startup_dark"] = appearance_is_dark()
            self.quit()

    assert StartupApp().run(None) == 0
    assert seen["startup_dark"] is True
    set_theme("system")
    apply_appearance("system")
