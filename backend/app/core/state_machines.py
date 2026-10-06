"""All allowed status transitions live HERE, in one place.

Why: scattered `if status == ...` checks drift apart and let illegal
transitions slip through. Routers/services call transition() and never
compare statuses themselves.
"""


class InvalidTransition(Exception):
    pass


ENROLMENT = {
    "pending": {"approved", "rejected"},
    "approved": set(),
    "rejected": set(),
}

CLUB_DAY = {
    "planned": {"approved"},
    "approved": {"announced"},
    "announced": {"open"},
    "open": {"submissions"},
    "submissions": {"evaluating"},
    "evaluating": {"closed"},
    "closed": set(),
}


def transition(machine: dict, current: str, target: str) -> str:
    if target not in machine.get(current, set()):
        raise InvalidTransition(f"Cannot move from '{current}' to '{target}'")
    return target