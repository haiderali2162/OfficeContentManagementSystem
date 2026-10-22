
import flet as ft


def elevated_button(*args, **kwargs):
    """Create a button compatible with different Flet versions."""
    button_class = getattr(ft, "ElevatedButton", None)

    if button_class is None:
        button_class = getattr(ft, "Button", None)

    if button_class is None:
        raise RuntimeError(
            "No compatible button class found. "
            "Please check your installed Flet version."
        )

    return button_class(*args, **kwargs)


def text_button(*args, **kwargs):
    """Create a text button with a compatibility fallback."""
    button_class = getattr(ft, "TextButton", None)

    if button_class is None:
        button_class = getattr(ft, "TextButton", None)

    if button_class is None:
        return elevated_button(*args, **kwargs)

    return button_class(*args, **kwargs)
