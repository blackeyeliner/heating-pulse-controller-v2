"""Window input and fixed ventilation policy, independent of Home Assistant."""

PWM_OPEN_SECONDS = 120
PWM_CLOSED_SECONDS = 480
ROOM_CLOSE_TEMPERATURE = 16
RADIATOR_CLOSE_TEMPERATURE = 10


def cold_reason(room: float | None, radiator: float | None) -> str | None:
    if room is not None and room <= ROOM_CLOSE_TEMPERATURE:
        return "window_room_cold"
    if radiator is not None and radiator <= RADIATOR_CLOSE_TEMPERATURE:
        return "window_radiator_cold"
    return None


def is_window_open(state: str | None, position=None) -> bool | None:
    """A moving or partially open cover is exposed; unknown is never closed."""
    if state in ("open", "opening", "closing"):
        return True
    if state == "closed":
        return position > 0 if isinstance(position, (int, float)) else False
    return None
