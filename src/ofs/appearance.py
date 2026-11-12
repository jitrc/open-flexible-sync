"""GTK application appearance, including desktops that use named Dark theme variants."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk

from ofs.settings import theme

_ORIGINAL_THEME: str | None = None
_DESKTOP = None
_INITIAL_DARK: bool | None = None


def _capture_settings(settings: Gtk.Settings) -> None:
    global _ORIGINAL_THEME, _INITIAL_DARK
    if _ORIGINAL_THEME is None:
        _ORIGINAL_THEME = settings.get_property("gtk-theme-name") or ""
    if _INITIAL_DARK is None:
        _INITIAL_DARK = bool(settings.get_property("gtk-application-prefer-dark-theme"))


def _variant(theme_name: str, dark: bool) -> str:
    """Select a conventional -dark/-light variant without corrupting the family name."""

    if not theme_name:
        return ""
    base = theme_name
    for suffix in ("-dark", "-light"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base + ("-dark" if dark else "")


def _desktop_dark(settings: Gtk.Settings) -> bool:
    """Read the desktop's color scheme and watch it while System mode is active."""

    _capture_settings(settings)
    if _DESKTOP is not None:
        return _DESKTOP.get_string("color-scheme") == "prefer-dark"
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup("org.gnome.desktop.interface", True) if source is not None else None
    if schema is None or not schema.has_key("color-scheme"):
        return bool(_INITIAL_DARK)
    _set_desktop(schema)
    return _desktop_dark(settings)


def _set_desktop(schema) -> None:
    global _DESKTOP
    if _DESKTOP is not None:
        return
    _DESKTOP = Gio.Settings.new("org.gnome.desktop.interface")
    _DESKTOP.connect("changed::color-scheme", _desktop_changed)
    if schema.has_key("gtk-theme-name"):
        _DESKTOP.connect("changed::gtk-theme-name", _desktop_theme_changed)


def _desktop_changed(*_args) -> None:
    if theme() == "system":
        apply_appearance("system")


def _desktop_theme_changed(*_args) -> None:
    global _ORIGINAL_THEME
    if _DESKTOP is not None and _DESKTOP.get_schema().has_key("gtk-theme-name"):
        _ORIGINAL_THEME = _DESKTOP.get_string("gtk-theme-name")
    if theme() == "system":
        apply_appearance("system")


def apply_appearance(name: str) -> None:
    """Light/Dark override both the preference and a desktop's named theme variant."""

    if name not in {"system", "light", "dark"}:
        raise ValueError("appearance must be system, light, or dark")
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    _capture_settings(settings)
    if name == "system":
        selected_name = _ORIGINAL_THEME or ""
        dark = _desktop_dark(settings)
    else:
        selected_name = _variant(_ORIGINAL_THEME or "", name == "dark")
        dark = name == "dark"
    # A named Dark GTK theme can ignore the preference flag when asked for Light.
    # GTK Settings are scoped to this process; this does not edit desktop GSettings.
    if selected_name:
        settings.set_property("gtk-theme-name", selected_name)
    settings.set_property("gtk-application-prefer-dark-theme", dark)
    for window in Gtk.Window.get_toplevels():
        window.queue_draw()


def appearance_is_dark() -> bool | None:
    """Actual GTK preference or a named Dark variant, not just the preference flag."""

    settings = Gtk.Settings.get_default()
    if settings is None:
        return None
    name = settings.get_property("gtk-theme-name") or ""
    return name.lower().endswith(("-dark", ":dark")) or bool(settings.get_property("gtk-application-prefer-dark-theme"))
