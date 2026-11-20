"""The area calculator page accepts the shorthand area expressions."""

import asyncio

import flet as ft

from ui.pages import area_calculator


class FakePage:
    def __init__(self):
        self.services = []
        self.dialogs = []

    def update(self):
        return None

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()

    def run_task(self, handler, *args):
        asyncio.run(handler(*args))
        return None


def _walk(control):
    yield control
    for child in getattr(control, "controls", ()) or ():
        yield from _walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from _walk(content)


def _find(root, control_type, label=None, text_prefix=None):
    for control in _walk(root):
        if not isinstance(control, control_type):
            continue
        if label is not None and control.label != label:
            continue
        if text_prefix is not None and not (control.value or "").startswith(text_prefix):
            continue
        return control
    raise AssertionError(f"Control not found: {control_type} {label or text_prefix}")


def _page_and_button():
    page = FakePage()
    root = area_calculator.build_area_calculator_page(page)
    value_a = _find(root, ft.TextField, label="A")
    value_b = _find(root, ft.TextField, label="B")
    operation = _find(root, ft.Dropdown, label="Operation")
    button = _find(root, ft.Button, label=None)
    assert button.content == "Calculate"
    return page, root, value_a, value_b, operation, button


def test_shorthand_preview_and_convert(monkeypatch):
    """The A preview understands 1k10m20f immediately."""
    monkeypatch.setattr(area_calculator, "save_calculation", lambda **kwargs: 1)
    monkeypatch.setattr(area_calculator, "list_calculations", lambda limit=100: [])
    _page, root, value_a, _b, _operation, _button = _page_and_button()
    value_a.value = "1k10m20f"
    value_a.on_change(None)
    preview = _find(root, ft.Text, text_prefix="Acres:")
    # 1 kanal + 10 marla + 20 sq ft: sarsahi (272/9 ft) rounds to 1.
    assert "K-M-S: 1-10-1" in preview.value
    assert "Marlas: 30.073529" in preview.value


def test_shorthand_in_calculate_operations(monkeypatch):
    """1k10m20f, 10m20f, 100f, and plain kanal work in A and B."""
    # Keep the calculation history out of the live office database.
    monkeypatch.setattr(area_calculator, "save_calculation", lambda **kwargs: 1)
    monkeypatch.setattr(area_calculator, "list_calculations", lambda limit=100: [])
    _page, root, value_a, value_b, operation, button = _page_and_button()

    # Convert a compound shorthand value.
    value_a.value = "1k10m20f"
    operation.value = "Convert"
    asyncio.run(button.on_click(None))
    result = _find(root, ft.Text, text_prefix="Result:")
    assert "K-M-F: 1-10-20" in result.value
    assert "Marlas: 30.073529" in result.value

    # Add two shorthand values: 10m20f + 10m20f = 20 marla + 40 ft
    # = exactly 1 kanal, 0 marla, 40 ft.
    value_a.value = "10m20f"
    value_b.value = "10m20f"
    operation.value = "Add"
    asyncio.run(button.on_click(None))
    result = _find(root, ft.Text, text_prefix="A = ")
    assert "A = 1-0-40 K-M-F" in result.value

    # A plain number still means kanal; 100f is exact square feet.
    value_a.value = "2"
    value_b.value = "100f"
    operation.value = "Add"
    asyncio.run(button.on_click(None))
    result = _find(root, ft.Text, text_prefix="A = ")
    assert "A = 2-0-100 K-M-F" in result.value

    # Both systems mix freely: K-M-F plus shorthand.
    value_a.value = "3-10-120"
    value_b.value = "10m20f"
    operation.value = "Add"
    asyncio.run(button.on_click(None))
    result = _find(root, ft.Text, text_prefix="A = ")
    assert "A = 4-0-140 K-M-F" in result.value

    # Invalid shorthand reports the combined guidance in the status line.
    value_a.value = "banana"
    operation.value = "Convert"
    asyncio.run(button.on_click(None))
    status = _find(root, ft.Text, text_prefix="Please check your inputs:")
    assert "shorthand such as 10k5m" in status.value
