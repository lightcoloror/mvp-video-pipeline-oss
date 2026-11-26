"""Bounded deterministic process data for the native object-scene renderer."""
from __future__ import annotations

import math
import re

from .author_bindings import _number, _object
from .errors import ContractError

PROCESS_COMPONENTS = {"queue_flow_2d", "slider_crank_3d"}
THREE_COMPONENTS = {"stage_timeline_3d", "slider_crank_3d"}
PROCESS_ACTIONS = {
    "queue_flow_2d": {"accelerate", "open_exit", "focus"},
    "slider_crank_3d": {"cutaway", "orient", "focus"},
}

# Material colors describe the objects. Signal colors describe an operating
# state; neither is inherited from a creator's personal visual identity.
PROCESS_COLORS = {
    "queue_flow_2d": {
        "belt": "#626D78", "belt_edge": "#B3BDC7", "belt_slats": "#929EAA",
        "flow": "#E7EDF2", "gate_frame": "#435363", "open": "#247B53",
        "closed": "#C34D43", "waiting": "#A55C0C", "clear": "#247B53",
        "parcel": "#C99559", "parcel_edge": "#73512D", "tape": "#E6C58A",
        "label": "#FFF9ED", "parcel_ink": "#493522",
    },
    "slider_crank_3d": {
        "base": "#9AA7B5", "housing": "#697787", "housing_inner": "#AAB6C2",
        "cut_edge": "#C59433", "piston": "#D3DCE5", "piston_rings": "#374859",
        "rod": "#9DACBA", "pins": "#52697E", "crank": "#426A91",
        "spokes": "#ABBDCC", "shaft": "#C1CED9", "flywheel": "#426A91",
        "piston_annotation": "#356B92", "rod_annotation": "#566B7D",
        "crank_annotation": "#245E99",
    },
}


def _process_colors(component, overrides):
    colors = dict(PROCESS_COLORS[component])
    values = _object(overrides, "process colors")
    if set(values) - set(colors):
        raise ContractError("process colors have unknown material or signal roles")
    for role, color in values.items():
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise ContractError("process colors must be six-digit hexadecimal values")
        colors[role] = color
    return colors


def _integer(value, label, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ContractError(f"{label} must be an integer in [{lo}, {hi}]")
    return value


def _event(events, identity, action):
    matching = [row for row in events if row["id"] == identity and row["action"] == action]
    if len(matching) != 1:
        raise ContractError(f"process parameter needs one {action} event")
    return matching[0]


def _smooth(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def queue_schedule(parameters, events, duration):
    speed = _event(events, parameters["speed_event_id"], "accelerate")
    opening = _event(events, parameters["open_event_id"], "open_exit")
    free = [0, opening["endFrame"]]
    entry, parcels = 0.0, []
    headway = parameters["fork_headway_frames"]
    previous_release = -headway
    for index in range(parameters["parcel_count"]):
        frame = math.floor(entry + 0.5)
        if frame >= duration:
            break
        ready = frame + parameters["approach_frames"]
        gate = min(range(2), key=lambda value: (max(ready, free[value], previous_release + headway), value))
        release = max(ready, free[gate], previous_release + headway)
        previous_release = release
        free[gate] = release + parameters["service_frames"]
        parcels.append({"id": f"parcel-{index+1:03d}", "entryFrame": frame,
                        "readyFrame": ready, "releaseFrame": release, "gateIndex": gate,
                        "doneFrame": free[gate] + parameters["exit_travel_frames"]})
        t = _smooth((frame - speed["startFrame"]) / (speed["endFrame"] - speed["startFrame"]))
        entry += parameters["slow_arrival_frames"] + t * (parameters["fast_arrival_frames"] - parameters["slow_arrival_frames"])
    return parcels


def queue_counts(parcels, frame):
    entered = [row for row in parcels if row["entryFrame"] <= frame]
    done = sum(row["doneFrame"] <= frame for row in entered)
    waiting = sum(row["readyFrame"] <= frame < row["releaseFrame"] for row in entered)
    return {"entered": len(entered), "waiting": waiting, "done": done,
            "inSystem": len(entered) - done}


def slider_crank_state(radius, length, angle):
    """Slider constrained to x=0, constant-length rod, crank at the origin."""
    pin_x, pin_y = radius * math.sin(angle), radius * math.cos(angle)
    slider_y = pin_y + math.sqrt(length * length - pin_x * pin_x)
    return {"pin": [pin_x, pin_y, 0.0], "slider": [0.0, slider_y, 0.0],
            "rodMidpoint": [pin_x / 2, (pin_y + slider_y) / 2, 0.0],
            "rodAngle": math.atan2(pin_x, slider_y - pin_y)}


def resolve_process(component, raw_parameters, events, object_ids, duration):
    p = dict(_object(raw_parameters, "process parameters"))
    if component not in PROCESS_COMPONENTS:
        raise ContractError("unsupported process component")
    colors = _process_colors(component, p.pop("colors", {}))
    if component == "queue_flow_2d":
        if len(object_ids) != 3:
            raise ContractError("queue component needs exactly three object roles")
        keys = {"parcel_count", "slow_arrival_frames", "fast_arrival_frames", "service_frames",
                "approach_frames", "exit_travel_frames", "fork_headway_frames", "speed_event_id", "open_event_id",
                "parcel_object_id", "first_exit_object_id", "second_exit_object_id"}
        if set(p) != keys:
            raise ContractError("queue parameters have unknown or missing fields")
        p = dict(p)
        _integer(p["parcel_count"], "parcel count", 2, 64)
        for name in ("slow_arrival_frames", "fast_arrival_frames", "service_frames", "approach_frames", "exit_travel_frames"):
            _integer(p[name], name, 12, 240)
        _integer(p["fork_headway_frames"], "fork headway", 12, 60)
        if p["fast_arrival_frames"] >= p["slow_arrival_frames"]:
            raise ContractError("queue acceleration must shorten the arrival interval")
        roles = [p[name] for name in ("parcel_object_id", "first_exit_object_id", "second_exit_object_id")]
        if len(set(roles)) != 3 or any(role not in object_ids for role in roles):
            raise ContractError("queue roles must bind three distinct scene objects")
        speed = _event(events, p["speed_event_id"], "accelerate")
        opening = _event(events, p["open_event_id"], "open_exit")
        if speed["targetId"] != roles[0] or opening["targetId"] != roles[2]:
            raise ContractError("queue actions must target parcels and the second exit")
        parcels = queue_schedule(p, events, duration)
        peak = max(queue_counts(parcels, frame)["waiting"] for frame in range(duration))
        if peak > 6:
            raise ContractError("queue would exceed the readable portrait waiting area")
        return {"parameters": p, "colors": colors, "parcels": parcels, "maximumWaiting": peak,
                "simulationKind": "synthetic_discrete_event_queue"}
    if component != "slider_crank_3d":
        raise ContractError("unsupported process component")
    if len(object_ids) != 4:
        raise ContractError("slider-crank component needs exactly four object roles")
    keys = {"crank_radius", "rod_length", "piston_radius", "housing_radius", "cycle_frames",
            "initial_angle", "cutaway_event_id", "orient_event_id", "piston_object_id",
            "rod_object_id", "crank_object_id", "housing_object_id"}
    if set(p) != keys:
        raise ContractError("slider-crank parameters have unknown or missing fields")
    p = dict(p)
    for name, lo, hi in (("crank_radius", 0.4, 1.0), ("rod_length", 2.0, 3.0),
                         ("piston_radius", 0.55, 0.85), ("housing_radius", 0.9, 1.15),
                         ("initial_angle", -2 * math.pi, 2 * math.pi)):
        value = _number(p[name], name)
        if not lo <= value <= hi:
            raise ContractError(f"{name} exceeds the bounded mechanism design")
        p[name] = value
    _integer(p["cycle_frames"], "cycle frames", 90, 240)
    if p["rod_length"] <= 2 * p["crank_radius"] or p["piston_radius"] + 0.08 >= p["housing_radius"]:
        raise ContractError("slider-crank geometry would collide or lose its rod constraint")
    roles = [p[name] for name in ("piston_object_id", "rod_object_id", "crank_object_id", "housing_object_id")]
    if len(set(roles)) != 4 or any(role not in object_ids for role in roles):
        raise ContractError("mechanism roles must bind four distinct scene objects")
    cutting = _event(events, p["cutaway_event_id"], "cutaway")
    _event(events, p["orient_event_id"], "orient")
    if cutting["targetId"] != roles[3]:
        raise ContractError("cutaway must target the housing")
    return {"parameters": p, "colors": colors, "simulationKind": "constrained_slider_crank"}
