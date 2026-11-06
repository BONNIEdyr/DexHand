"""赛项 I 悬挂负载：抓握 φ40×100 圆柱侧面，悬挂配重并保持 10 秒。

    py task_load.py home            夹爪位置模式回零（须已在零位附近）
    py task_load.py open            张开
    py task_load.py release-open    使用独立参数张开，用于释放物体和标定
    py task_load.py close           闭合并夹紧，回车后切换为实际位置保持
    py task_load.py release         撤去夹紧力并卸力
    py task_load.py park            从张开状态回到零位
    py task_load.py run             完整流程：回零 → 张开 → 放入圆柱 → 夹紧 → 回车位置保持 → 回车张开 → 回车归零
"""

from pathlib import Path
import sys

from dexhand.gripper import Gripper
from dexhand import task_common
from dexhand.task_common import confirm, gripper_params, hold_until_enter

DEFAULT_PARAMS = Path(__file__).resolve().parent / "params" / "task_load.toml"


def make_parser():
    parser = task_common.make_parser("赛项 I 悬挂负载", DEFAULT_PARAMS)
    parser.add_argument("action", choices=("home", "open", "release-open", "close", "release", "park", "run"))
    return parser


def release_open_params(data, overrides):
    """释放张开使用独立覆盖表；旧参数文件省略此表时沿用原张开参数。"""
    table = data.get("release_open", {})
    allowed = {"transport_control", "transport_speed", "settle_s", "move_time_ms",
               "open_time_s", "open_position"}
    for values, name in ((table, "release_open"), (overrides, "--set release_open")):
        if not isinstance(values, dict):
            raise ValueError(f"{name} 必须为 TOML 表")
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"{name} 存在不可覆盖或未知的参数：{', '.join(sorted(unknown))}")
    return gripper_params(data, overrides, (table, "release_open"))


def body(args):
    def inner(controller, data, overrides):
        release_overrides = {key.removeprefix("release_open."): value
                             for key, value in overrides.items() if key.startswith("release_open.")}
        base_overrides = {key: value for key, value in overrides.items()
                          if not key.startswith("release_open.")}
        params = gripper_params(data, base_overrides)
        # 在任何动作前验证第二套参数，避免夹紧后才发现释放参数错误。
        release_params = (release_open_params(data, release_overrides)
                          if args.action in ("run", "release-open") or release_overrides else None)
        hold = data.get("hold", {})
        interval = hold.get("status_interval_s", 1.0)
        gripper = Gripper(controller, params)
        action = args.action
        if action == "home":
            gripper.home()
        elif action == "open":
            gripper.open()
        elif action == "release-open":
            print("释放张开：使用 [release_open] 参数", flush=True)
            Gripper(controller, release_params).open()
        elif action == "close":
            gripper.close()
            hold_until_enter(controller, "夹紧中，回车停止持续夹紧并保持当前位置", params.ids, interval)
            gripper.hold_position()
        elif action == "release":
            gripper.release()
        elif action == "park":
            gripper.park()
        else:
            gripper.home()
            gripper.open()
            confirm(controller, "把圆柱放入夹爪，确认可以闭合")
            gripper.close()
            seconds = hold_until_enter(
                controller, "持续夹紧中，回车停止持续夹紧并切换当前位置保持",
                params.ids, interval)
            print(f"本次持续夹紧 {seconds:.1f} 秒", flush=True)
            gripper.hold_position()
            hold_until_enter(
                controller, "位置保持中。负载离开支撑后计时 10 秒；测试结束先承托负载，再回车释放并张开",
                params.ids, interval, target_s=10)
            print("释放张开：使用 [release_open] 参数", flush=True)
            Gripper(controller, release_params).open()
            confirm(controller, "确认圆柱已取出，夹爪将回到零位")
            gripper.park()
        if controller.bus.dry_run:
            print("以上为离线预览报文，未打开串口。", flush=True)
        return 0
    return inner


def main(argv=None) -> int:
    args = make_parser().parse_args(argv)
    return task_common.run(args, body(args))


if __name__ == "__main__":
    sys.exit(main())
