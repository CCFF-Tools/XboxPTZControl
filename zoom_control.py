"""Protocol-aware zoom command scheduling.

The VISCA-over-TCP cameras benefit from periodic start packets while a trigger
is held. UDP cameras, however, should receive one start packet per direction
change; stop packets are repeated briefly to tolerate packet loss.
"""

from dataclasses import dataclass


@dataclass
class ZoomCommandState:
    """State carried between input-loop iterations."""

    last_direction: int = 0
    last_sent_ms: float = 0.0
    stop_retries_remaining: int = 0

    def reset(self) -> None:
        """Forget command history, such as after switching cameras."""

        self.last_direction = 0
        self.last_sent_ms = 0.0
        self.stop_retries_remaining = 0


def next_zoom_command(
    requested_direction: int,
    protocol: str,
    now_ms: float,
    state: ZoomCommandState,
    *,
    repeat_ms: int = 200,
    udp_stop_packets: int = 3,
) -> int | None:
    """Return a zoom command to send this iteration, or ``None``.

    TCP repeats non-zero start commands at ``repeat_ms``. UDP emits starts
    only when direction changes, and emits a bounded burst of stop packets
    across subsequent iterations after a stop transition.
    """

    direction = 1 if requested_direction > 0 else -1 if requested_direction < 0 else 0
    is_udp = protocol.lower() == "udp"
    command = None

    if direction != state.last_direction:
        command = direction
        state.last_direction = direction
        state.last_sent_ms = now_ms
        if is_udp and direction == 0:
            state.stop_retries_remaining = max(0, udp_stop_packets - 1)
        else:
            state.stop_retries_remaining = 0
    elif is_udp and direction == 0 and state.stop_retries_remaining > 0:
        command = 0
        state.stop_retries_remaining -= 1
        state.last_sent_ms = now_ms
    elif not is_udp and direction != 0 and now_ms - state.last_sent_ms >= repeat_ms:
        command = direction
        state.last_sent_ms = now_ms

    return command
