"""Derive field-device IPs from pole numbers."""

from enum import Enum, auto


class DeviceType(Enum):
    TF_CPU = auto()
    TF_CAMERA = auto()
    CERBO = auto()
    CBW = auto()
    DRAGONEYE = auto()


def pole_to_ip_address(pole: str, device: DeviceType, lane: int = 0) -> str:
    if pole == "0":
        return "0.0.0.0"

    pole = str(pole).strip()
    if len(pole) < 4:
        raise ValueError(f"pole number too short: {pole!r}")

    ipq1 = "10"
    if pole[:2] == "21":
        ipq2 = "21"
    elif pole[2] == "0":
        ipq2 = "1"
    else:
        ipq2 = pole[2]

    ipq3 = pole[3:]
    while ipq3 and ipq3[0] == "0":
        ipq3 = ipq3[1:]
    if not ipq3:
        ipq3 = "0"

    match device:
        case DeviceType.TF_CPU:
            ipq4 = "14" + str(lane - 1)
        case DeviceType.TF_CAMERA:
            ipq4 = "15" + str(lane - 1)
        case DeviceType.CBW:
            ipq4 = "177"
        case DeviceType.CERBO:
            ipq4 = "170"
        case _:
            raise ValueError(f"unsupported device type: {device}")

    return f"{ipq1}.{ipq2}.{ipq3}.{ipq4}"


def cbw_ip(pole: str) -> str:
    return pole_to_ip_address(pole, DeviceType.CBW)


def tf_cpu_ips(pole: str, lane_count: int = 2) -> list[tuple[str, str]]:
    return [
        (f"TF_CPU {lane}", pole_to_ip_address(pole, DeviceType.TF_CPU, lane))
        for lane in range(1, lane_count + 1)
    ]
