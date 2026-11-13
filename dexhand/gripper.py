"""三个赛项共用的夹爪开合动作与参数。

夹爪由两只舵机（默认 ID 1、2）通过蜗杆带动平行杆机构，行程超过舵机位置模式的
240°，因此每次开合分两段：先在减速电机模式按时间转动（粗行程），再切回位置
模式移动到指定位置（精定位）。闭合后可再切电机占空比模式输出夹紧力。
电机阶段为开环计时，实际转过的角度受负载和电压影响，须实机标定。
"""

from dataclasses import dataclass, fields, replace
import time
from typing import Callable, Mapping

from dexhand.servo_controller import ServoController
from dexhand.settings import _from_table, integer, motor_limit, number


def _pair(value, name: str) -> tuple[int, int]:
    """接受一个整数（两只相同）或两个整数的列表（分别对应 ids 顺序）。"""
    if type(value) is int:
        value = (value, value)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} 必须是一个整数，或两个整数的列表（按 ids 顺序）")
    return integer(value[0], name, 0, 1000), integer(value[1], name, 0, 1000)


@dataclass(frozen=True)
class GripperParams:
    ids: tuple[int, int] = (1, 2)
    mirror: bool = True                 # 第二只舵机电机方向与第一只相反
    transport_control: str = "speed"    # 电机阶段控制方式：speed 受电压影响较小
    transport_speed: int = 20           # 第一只舵机张开方向的速度；闭合取反
    settle_s: float = 0.3               # 电机停转到切换位置模式之间的停顿
    move_time_ms: int = 800             # 位置模式阶段的动作时间
    open_time_s: float = 1.5            # 张开时电机阶段时长
    open_position: tuple[int, int] = (600, 600)
    close_time_s: float = 1.5           # 闭合时电机阶段时长
    close_position: tuple[int, int] = (150, 150)
    grip_duty: int = 300                # 闭合后沿闭合方向输出的占空比；0 表示不夹紧
    home_position: tuple[int, int] = (0, 0)  # 机械零位对应的舵机原始位置，按 ids 顺序

    def __post_init__(self):
        ids = self.ids
        if not isinstance(ids, (list, tuple)) or len(ids) != 2 or ids[0] == ids[1]:
            raise ValueError("gripper.ids 必须是两个不同的舵机 ID")
        object.__setattr__(self, "ids", tuple(integer(item, "gripper.ids", 0, 253) for item in ids))
        if not isinstance(self.mirror, bool):
            raise ValueError("mirror 必须为 true 或 false")
        limit = motor_limit(self.transport_control)
        integer(self.transport_speed, "transport_speed", -limit, limit)
        if self.transport_speed == 0:
            raise ValueError("transport_speed 不能为 0")
        number(self.settle_s, "settle_s", 0, 10)
        integer(self.move_time_ms, "move_time_ms", 0, 30000)
        number(self.open_time_s, "open_time_s", 0, 60)
        number(self.close_time_s, "close_time_s", 0, 60)
        object.__setattr__(self, "open_position", _pair(self.open_position, "open_position"))
        object.__setattr__(self, "close_position", _pair(self.close_position, "close_position"))
        object.__setattr__(self, "home_position", _pair(self.home_position, "home_position"))
        integer(self.grip_duty, "grip_duty", 0, 1000)

    @classmethod
    def from_table(cls, table, name: str = "gripper") -> "GripperParams":
        return _from_table(cls, table, name)

    def with_overrides(self, table: Mapping, name: str) -> "GripperParams":
        """用赛项或物体专用的参数覆盖默认值；ids 和 mirror 属于硬件，不允许覆盖。"""
        if not isinstance(table, Mapping):
            raise ValueError(f"{name} 必须为 TOML 表")
        allowed = {field.name for field in fields(self)} - {"ids", "mirror"}
        unknown = set(table) - allowed
        if unknown:
            raise ValueError(f"{name} 存在不可覆盖或未知的参数：{', '.join(sorted(unknown))}")
        return replace(self, **table)

    def speeds(self, direction: int) -> dict[int, int]:
        """电机阶段各舵机速度；direction 为 +1 张开、-1 闭合。"""
        first = direction * self.transport_speed
        return {self.ids[0]: first, self.ids[1]: -first if self.mirror else first}

    def grip_duties(self) -> dict[int, int]:
        close_sign = -1 if self.transport_speed > 0 else 1
        first = close_sign * self.grip_duty
        return {self.ids[0]: first, self.ids[1]: -first if self.mirror else first}

    def targets(self, pair: tuple[int, int]) -> dict[int, int]:
        return dict(zip(self.ids, pair))


class Gripper:
    """在 ServoController 之上封装开合动作。离线预览时不等待时间。"""

    def __init__(self, controller: ServoController, params: GripperParams,
                 log: Callable[[str], None] | None = None):
        self.controller = controller
        self.params = params
        self.log = log or (lambda text: print(text, flush=True))

    def sleep(self, seconds: float):
        if seconds > 0 and not self.controller.bus.dry_run:
            time.sleep(seconds)

    def transport(self, direction: int, seconds: float, label: str):
        """电机模式按时间转动，然后同时停转并等待机构静止。"""
        if seconds <= 0:
            return
        p = self.params
        self.log(f"{label}：电机 {p.transport_control} 模式转动 {seconds:g} 秒，"
                 f"速度 {p.speeds(direction)}")
        self.controller.run_motor(p.speeds(direction), control=p.transport_control)
        self.sleep(seconds)
        self.controller.stop(p.ids)
        self.sleep(p.settle_s)

    def open(self):
        p = self.params
        self.transport(+1, p.open_time_s, "张开")
        self.log(f"张开：位置模式到 {p.targets(p.open_position)}")
        self.controller.move(p.targets(p.open_position), time_ms=p.move_time_ms)

    def close(self, grip: bool = True):
        p = self.params
        self.transport(-1, p.close_time_s, "闭合")
        self.log(f"闭合：位置模式到 {p.targets(p.close_position)}")
        self.controller.move(p.targets(p.close_position), time_ms=p.move_time_ms)
        if grip and p.grip_duty:
            self.log(f"夹紧：电机 duty 模式输出 {p.grip_duties()}，保持中")
            self.controller.run_motor(p.grip_duties(), control="duty")

    def hold_position(self):
        """停止持续夹紧，读取实际位置并改为位置保持，而非回到 close_position。"""
        positions = self.controller.hold_position(self.params.ids)
        if positions is not None:
            self.log(f"位置保持：{positions}；保持上力，程序退出后仍保持。")

    def release(self):
        """撤去夹紧力并卸力。蜗杆自锁，夹爪不会自行张开，释放物体需随后 open()。"""
        self.log("释放：电机零速并卸力")
        self.controller.stop(self.params.ids)

    def home(self):
        """位置模式直接回零位；仅适用于夹爪已在零位附近（240° 范围内）时。"""
        p = self.params
        self.log(f"回零：位置模式到机械零位 {p.targets(p.home_position)}")
        self.controller.move(p.targets(p.home_position), time_ms=p.move_time_ms)

    def park(self):
        """从张开状态回到机械零位：电机反向转闭合时长，再位置模式定位。"""
        p = self.params
        self.transport(-1, p.close_time_s, "回退")
        self.home()
