"""Pure input-loop state helpers used by the joystick bridge."""

from dataclasses import dataclass


@dataclass
class MotionState:
    last_move: tuple[int, int, int, int] | None = None
    focus_direction: int = 0
    focus_moved: bool = False
    move_stop_remaining: int = 0
    focus_stop_remaining: int = 0

    def reset(self) -> None:
        self.last_move = None
        self.focus_direction = 0
        self.focus_moved = False
        self.move_stop_remaining = 0
        self.focus_stop_remaining = 0

    def move_changed(
        self,
        command: tuple[int, int, int, int],
        protocol: str = "tcp",
        udp_stop_packets: int = 3,
    ) -> bool:
        neutral = command == (0, 0, 3, 3)
        if command != self.last_move:
            was_active = self.last_move is not None and self.last_move != (0, 0, 3, 3)
            self.last_move = command
            self.move_stop_remaining = (
                max(0, udp_stop_packets - 1)
                if neutral and was_active and protocol.lower() == "udp"
                else 0
            )
            return True
        if neutral and self.move_stop_remaining > 0:
            self.move_stop_remaining -= 1
            return True
        return False

    def next_focus(
        self, direction: int, protocol: str = "tcp", udp_stop_packets: int = 3
    ) -> int | None:
        direction = 1 if direction > 0 else -1 if direction < 0 else 0
        if direction == 0 and self.focus_direction == 0 and self.focus_stop_remaining > 0:
            self.focus_stop_remaining -= 1
            return 0
        if direction == 0 and not self.focus_moved:
            self.focus_direction = 0
            return None
        if direction == self.focus_direction:
            if direction == 0 and self.focus_stop_remaining > 0:
                self.focus_stop_remaining -= 1
                return 0
            return None
        was_active = self.focus_direction != 0
        self.focus_direction = direction
        self.focus_moved = direction != 0
        self.focus_stop_remaining = (
            max(0, udp_stop_packets - 1)
            if direction == 0 and was_active and protocol.lower() == "udp"
            else 0
        )
        return direction


@dataclass
class ZoomTriggerState:
    direction: int = 0
    release_loops: int = 0

    def reset(self) -> None:
        self.direction = 0
        self.release_loops = 0


def resolve_zoom_direction(
    zoom_value: float,
    state: ZoomTriggerState,
    *,
    start_deadzone: float = 0.10,
    release_loops: int = 3,
) -> int:
    """Resolve trigger direction, forcing release after a bounded grace period."""

    if abs(zoom_value) > start_deadzone:
        state.direction = 1 if zoom_value > 0 else -1
        state.release_loops = 0
        return state.direction
    state.release_loops += 1
    if state.release_loops >= release_loops:
        state.direction = 0
    return state.direction


def zoom_speed_for_trigger(zoom_value: float, maximum: int, *, deadzone: float = 0.10) -> int:
    """Map trigger magnitude to a VISCA zoom speed (0..maximum).

    Values inside the trigger deadzone command the slowest speed (0).  Active
    triggers ramp linearly up to the configured maximum, while never exceeding
    it.  This keeps a configured maximum of zero meaningful and safe.
    """

    maximum = max(0, min(int(maximum), 7))
    magnitude = abs(float(zoom_value))
    if magnitude <= deadzone or maximum == 0:
        return 0
    normalized = min(1.0, (magnitude - deadzone) / (1.0 - deadzone))
    return min(maximum, round(normalized * maximum))


@dataclass(frozen=True)
class ButtonLayout:
    lb: int
    rb: int
    ls: int


EVDEV_LAYOUT = ButtonLayout(lb=4, rb=5, ls=9)
# HIDAPI exposes the D-pad as buttons 11..14 and shifts the bumpers/stick
# click relative to the evdev layout.
HIDAPI_LAYOUT = ButtonLayout(lb=9, rb=10, ls=7)


def controller_layout(button_count: int, hat_count: int) -> ButtonLayout:
    """Select the known Xbox mapping and safely fall back to evdev."""

    if hat_count == 0 and button_count >= 15:
        return HIDAPI_LAYOUT
    return EVDEV_LAYOUT


@dataclass
class ButtonEdges:
    previous: set[str] | None = None

    def rising(self, values: dict[str, bool]) -> set[str]:
        current = {name for name, pressed in values.items() if pressed}
        previous = self.previous or set()
        self.previous = current
        return current - previous

    def reset(self) -> None:
        self.previous = None
