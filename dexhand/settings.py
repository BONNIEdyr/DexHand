"""独立 TOML 配置的加载及校验（Python 3.11+）。"""

from dataclasses import dataclass, fields
from pathlib import Path
import math
import tomllib


def integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum}~{maximum} 的整数，收到 {value!r}")
    return value


def number(value: float, name: str, minimum: float, maximum: float) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f"{name} 必须是 {minimum}~{maximum} 的有限数值")
    return float(value)


def motor_limit(control: str) -> int:
    if control not in ("duty", "speed"):
        raise ValueError("motor_control 必须为 duty 或 speed")
    return 1000 if control == "duty" else 50


@dataclass(frozen=True)
class SerialSettings:
    port: str = "COM3"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout: float = 0.3
    write_timeout: float = 0.5
    command_interval: float = 0.005
    read_retries: int = 1

    def __post_init__(self):
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError("serial.port 不能为空")
        integer(self.baudrate, "baudrate", 1, 4_000_000)
        integer(self.bytesize, "bytesize", 5, 8)
        if self.parity not in ("N", "E", "O", "M", "S"):
            raise ValueError("parity 必须为 N/E/O/M/S")
        if type(self.stopbits) not in (int, float) or self.stopbits not in (1, 1.5, 2):
            raise ValueError("stopbits 必须为 1、1.5 或 2")
        number(self.timeout, "timeout", 0.001, 60)
        number(self.write_timeout, "write_timeout", 0.001, 60)
        number(self.command_interval, "command_interval", 0, 1)
        integer(self.read_retries, "read_retries", 0, 10)


@dataclass(frozen=True)
class ServoSettings:
    id: int
    mode: str = "servo"
    position: int = 500
    move_time_ms: int = 1000
    min_position: int = 0
    max_position: int = 1000
    motor_control: str = "duty"
    motor_speed: int = 0

    def __post_init__(self):
        integer(self.id, "舵机 ID", 0, 253)
        if self.mode not in ("servo", "motor"):
            raise ValueError("mode 必须为 servo 或 motor")
        integer(self.min_position, "min_position", 0, 1000)
        integer(self.max_position, "max_position", 0, 1000)
        if self.min_position >= self.max_position:
            raise ValueError("min_position 必须小于 max_position")
        integer(self.position, f"ID {self.id} position", self.min_position, self.max_position)
        integer(self.move_time_ms, "move_time_ms", 0, 30000)
        limit = motor_limit(self.motor_control)
        integer(self.motor_speed, "motor_speed", -limit, limit)


@dataclass(frozen=True)
class Config:
    serial: SerialSettings
    servos: tuple[ServoSettings, ...]

    def __post_init__(self):
        if not self.servos:
            raise ValueError("至少配置一个舵机")
        ids = [servo.id for servo in self.servos]
        if len(set(ids)) != len(ids):
            raise ValueError("舵机 ID 不得重复")


def _from_table(cls, table: dict, name: str):
    if not isinstance(table, dict):
        raise ValueError(f"{name} 必须为 TOML 表")
    unknown = set(table) - {field.name for field in fields(cls)}
    if unknown:
        raise ValueError(f"{name} 存在未知配置项：{', '.join(sorted(unknown))}")
    try:
        return cls(**table)
    except TypeError as exc:
        raise ValueError(f"{name} 配置不完整：{exc}") from exc


def load_config(path: str | Path) -> Config:
    # 使用 utf-8-sig，兼容 Windows 编辑器保存的 UTF-8 BOM。
    data = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    unknown = set(data) - {"serial", "servos"}
    if unknown:
        raise ValueError(f"未知配置分组：{', '.join(sorted(unknown))}")
    if "serial" not in data or "servos" not in data:
        raise ValueError("配置必须包含 [serial] 和 [[servos]]")
    if not isinstance(data["servos"], list):
        raise ValueError("servos 必须使用 [[servos]] 数组表")
    return Config(
        _from_table(SerialSettings, data["serial"], "serial"),
        tuple(_from_table(ServoSettings, item, "servos") for item in data["servos"]),
    )

