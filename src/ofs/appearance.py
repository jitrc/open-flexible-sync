"""Application appearance using GTK settings, with an optional desktop dark preference."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk

from ofs.settings import theme

_DESKTOP = None
_INITIAL_DARK: bool | None = None


def _desktop_dark(settings: Gtk.Settings) -> bool:
    """Read GNOME's color scheme when exposed, and watch it in System mode."""

    global _DESKTOP, _INITIAL_DARK
    if _INITIAL_DARK is None:
        _INITIAL_DARK = bool(settings.get_property("gtk-application-prefer-dark-theme"))
    if _DESKTOP is not None:
        return _DESKTOP.get_string("color-scheme") == "prefer-dark"
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup("org.gnome.desktop.interface", True) if source is not None else None
    if schema is None or not schema.has_key("color-scheme"):
        return _INITIAL_DARK
    _DESKTOP = Gio.Settings.new("org.gnome.desktop.interface")
    _DESKTOP.connect("changed::color-scheme", _desktop_changed)
    return _desktop_dark(settings)


def _desktop_changed(*_args) -> None:
    if theme() == "system":
        apply_appearance("system")


def apply_appearance(name: str) -> None:
    """Override light/dark for this application; system follows the desktop preference."""

    if name not in {"system", "light", "dark"}:
        raise ValueError("appearance must be system, light, or dark")
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    dark = name == "dark" or (name == "system" and _desktop_dark(settings))
    settings.set_property("gtk-application-prefer-dark-theme", dark)


def appearance_is_dark() -> bool | None:
    """Current effective GTK preference, primarily useful for integration checks."""

    settings = Gtk.Settings.get_default()
    return None if settings is None else bool(settings.get_property("gtk-application-prefer-dark-theme"))
