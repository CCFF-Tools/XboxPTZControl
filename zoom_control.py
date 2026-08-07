"""Protocol-aware zoom command scheduling.

Both VISCA-over-TCP and UDP cameras receive one start packet per direction
change; stop packets are repeated briefly to tolerate packet loss.
"""

from dataclasses import dataclass


@dataclass
class ZoomCommandState:
    """State carried between input-loop iterations."""

    last_direction: int = 0
    last_speed: int = -1
    stop_retries_remaining: int = 0

    def reset(self) -> None:
        """Forget command history, such as after switching cameras."""

        self.last_direction = 0
        self.last_speed = -1
        self.stop_retries_remaining = 0


def next_zoom_command(
    requested_direction: int,
    state: ZoomCommandState,
    *,
    stop_packets: int = 3,
    requested_speed: int | None = None,
) -> int | None:
    """Return a zoom command to send this iteration, or ``None``.

    Both transports emit starts only when direction changes and emit a
    bounded burst of stop packets across subsequent iterations after release.
    """

    direction = 1 if requested_direction > 0 else -1 if requested_direction < 0 else 0
    command = None

    speed_changed = requested_speed is not None and requested_speed != state.last_speed
    if direction != state.last_direction or (direction != 0 and speed_changed):
        command = direction
        state.last_direction = direction
        if requested_speed is not None:
            state.last_speed = requested_speed
        if direction == 0:
            state.stop_retries_remaining = max(0, stop_packets - 1)
        else:
            state.stop_retries_remaining = 0
    elif direction == 0 and state.stop_retries_remaining > 0:
        command = 0
        state.stop_retries_remaining -= 1
    if requested_speed is not None and direction == 0:
        state.last_speed = requested_speed
    return command
