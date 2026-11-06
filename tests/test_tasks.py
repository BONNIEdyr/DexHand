"""赛项脚本与夹爪开合动作的无硬件测试：验证报文顺序、镜像方向和参数叠加。"""

from contextlib import redirect_stdout, redirect_stderr
import io
import struct
import types
import unittest
from unittest.mock import patch

from dexhand.gripper import Gripper, GripperParams
from dexhand.servo_controller import Command, ServoController, decode_packet, encode_packet
from dexhand.settings import Config, SerialSettings, ServoSettings
from dexhand import task_common
import task_grasping
import task_load
import task_manipulation


def config():
    serial = SerialSettings(command_interval=0, timeout=0.005, read_retries=0)
    return Config(serial, (ServoSettings(1), ServoSettings(2), ServoSettings(3)))


class RecordingBus:
    dry_run = True

    def __init__(self):
        self.packets = []
        self.closed = False

    def write(self, servo_id, command, parameters=b""):
        self.packets.append(decode_packet(encode_packet(servo_id, command, parameters)))

    def close(self):
        self.closed = True


def mode_writes(packets):
    """返回 (servo_id, mode, control, speed) 列表，只含模式写指令。"""
    return [(p.servo_id, *struct.unpack("<BBh", p.parameters))
            for p in packets if p.command == Command.MODE_WRITE]


def quiet(bus):
    return ServoController(config(), bus=bus)


class GripperMotionTests(unittest.TestCase):
    def test_enter_hold_uses_measured_positions_and_survives_exit(self):
        bus = RecordingBus()
        bus.dry_run = False
        readings = {1: 321, 2: 654}
        reads = []

        def read(servo_id, command, count):
            self.assertEqual((command, count), (Command.POS_READ, 2))
            self.assertEqual(mode_writes(bus.packets[-2:]),
                             [(1, 1, 0, 0), (2, 1, 0, 0)])
            reads.append(servo_id)
            return struct.pack("<h", readings[servo_id])

        bus.read = read
        with patch("dexhand.servo_controller.time.sleep"):
            with quiet(bus) as controller:
                controller.run_motor({1: -300, 2: 300}, control="duty")
                Gripper(controller, GripperParams(), log=lambda _: None).hold_position()
                self.assertFalse(controller.has_running_motors)
                packet_count = len(bus.packets)
        self.assertEqual(reads, [1, 2])
        self.assertEqual(len(bus.packets), packet_count)  # 正常退出不卸力
        targets = [(p.servo_id, *struct.unpack("<HH", p.parameters))
                   for p in bus.packets if p.command == Command.MOVE_TIME_WAIT_WRITE]
        self.assertEqual(targets, [(1, 321, 0), (2, 654, 0)])
        for servo_id in readings:
            loads = [p.parameters for p in bus.packets
                     if p.servo_id == servo_id and p.command == Command.LOAD_WRITE]
            self.assertEqual(loads[-1], b"\x01")

    def test_hold_invalid_or_unreadable_position_stops_without_target(self):
        for reading in (-1, 1001, TimeoutError("读取失败")):
            with self.subTest(reading=reading):
                bus = RecordingBus()
                bus.dry_run = False

                def read(servo_id, command, count):
                    if isinstance(reading, Exception):
                        raise reading
                    return struct.pack("<h", 300 if servo_id == 1 else reading)

                bus.read = read
                with quiet(bus) as controller:
                    controller.run_motor({1: -300, 2: 300}, control="duty")
                    with self.assertRaises((ValueError, TimeoutError)):
                        controller.hold_position([1, 2])
                    self.assertFalse(controller.has_running_motors)
                self.assertFalse(any(p.command == Command.MOVE_TIME_WAIT_WRITE for p in bus.packets))
                for servo_id in (1, 2):
                    loads = [p.parameters for p in bus.packets
                             if p.servo_id == servo_id and p.command == Command.LOAD_WRITE]
                    self.assertEqual(loads[-1], b"\x00")

    def test_home_and_park_use_calibrated_positions_in_id_order(self):
        for action in ("home", "park"):
            with self.subTest(action=action):
                bus = RecordingBus()
                params = GripperParams(ids=(3, 2), home_position=[420, 580])
                with quiet(bus) as controller:
                    gripper = Gripper(controller, params, log=lambda _: None)
                    getattr(gripper, action)()
                presets = [(p.servo_id, *struct.unpack("<HH", p.parameters))
                           for p in bus.packets if p.command == Command.MOVE_TIME_WAIT_WRITE]
                self.assertEqual(presets, [(3, 420, 800), (2, 580, 800)])

    def test_home_respects_software_limits_before_sending(self):
        bus = RecordingBus()
        cfg = Config(SerialSettings(), (ServoSettings(1, min_position=100), ServoSettings(2)))
        with ServoController(cfg, bus=bus) as controller:
            with self.assertRaises(ValueError):
                Gripper(controller, GripperParams(home_position=[50, 580]), log=lambda _: None).home()
        self.assertEqual(bus.packets, [])

    def test_open_sends_mirrored_speeds_back_to_back_then_positions(self):
        bus = RecordingBus()
        with quiet(bus) as controller:
            Gripper(controller, GripperParams(), log=lambda _: None).open()
        packets = bus.packets
        # 两条非零速度报文必须相邻，方向镜像。
        index = next(i for i, p in enumerate(packets)
                     if p.command == Command.MODE_WRITE and p.parameters[2:] != b"\x00\x00")
        first, second = packets[index], packets[index + 1]
        self.assertEqual((first.servo_id, second.servo_id), (1, 2))
        self.assertEqual(struct.unpack("<BBh", first.parameters), (1, 1, 20))
        self.assertEqual(struct.unpack("<BBh", second.parameters), (1, 1, -20))
        # 停转时两条零速报文也相邻。
        self.assertEqual(struct.unpack("<BBh", packets[index + 2].parameters), (1, 1, 0))
        self.assertEqual(struct.unpack("<BBh", packets[index + 3].parameters), (1, 1, 0))
        self.assertEqual((packets[index + 2].servo_id, packets[index + 3].servo_id), (1, 2))
        # 随后位置模式到 600，子集启动不使用广播。
        presets = [p for p in packets[index:] if p.command == Command.MOVE_TIME_WAIT_WRITE]
        self.assertEqual([struct.unpack("<HH", p.parameters) for p in presets],
                         [(600, 800), (600, 800)])
        starts = [p.servo_id for p in packets if p.command == Command.MOVE_START]
        self.assertEqual(starts, [1, 2])

    def test_close_holds_grip_duty_until_release(self):
        bus = RecordingBus()
        with quiet(bus) as controller:
            gripper = Gripper(controller, GripperParams(grip_duty=300), log=lambda _: None)
            gripper.close()
            self.assertTrue(controller.has_running_motors)
            self.assertEqual(mode_writes(bus.packets[-2:]), [(1, 1, 0, -300), (2, 1, 0, 300)])
            count = len(bus.packets)
            gripper.release()
            self.assertFalse(controller.has_running_motors)
            tail = bus.packets[count:]
            self.assertEqual([(p.servo_id, p.command) for p in tail],
                             [(1, Command.MODE_WRITE), (2, Command.MODE_WRITE),
                              (1, Command.MOVE_STOP), (1, Command.LOAD_WRITE),
                              (2, Command.MOVE_STOP), (2, Command.LOAD_WRITE)])
            self.assertEqual(mode_writes(tail), [(1, 1, 0, 0), (2, 1, 0, 0)])

    def test_grip_duty_zero_skips_grip_phase(self):
        bus = RecordingBus()
        with quiet(bus) as controller:
            Gripper(controller, GripperParams(grip_duty=0), log=lambda _: None).close()
            self.assertFalse(controller.has_running_motors)
        self.assertEqual(bus.packets[-1].command, Command.MOVE_START)

    def test_non_mirrored_and_negative_transport_speed(self):
        params = GripperParams(mirror=False, transport_speed=-15)
        self.assertEqual(params.speeds(+1), {1: -15, 2: -15})
        self.assertEqual(params.speeds(-1), {1: 15, 2: 15})
        self.assertEqual(params.grip_duties(), {1: 300, 2: 300})

    def test_run_motor_mapping_is_validated_before_sending(self):
        bus = RecordingBus()
        with quiet(bus) as controller:
            with self.assertRaises(ValueError):
                controller.run_motor({1: 20, 2: 60}, control="speed")
            with self.assertRaisesRegex(ValueError, "与 ids 一致"):
                controller.run_motor({1: 20}, [1, 2], control="speed")
            with self.assertRaisesRegex(ValueError, "配置文件中的 ID"):
                controller.run_motor({1: 20, 9: 20}, control="speed")
        self.assertEqual(bus.packets, [])


class ParamTests(unittest.TestCase):
    def test_release_open_params_are_independent_and_backward_compatible(self):
        data = {"gripper": {"ids": [3, 2], "transport_control": "duty",
                            "transport_speed": 210, "open_time_s": 1, "open_position": 123}}
        self.assertEqual(task_load.release_open_params(data, {}).open_position, (123, 123))
        data["release_open"] = {"transport_speed": 320, "open_time_s": 0.4,
                                "open_position": [222, 333], "move_time_ms": 500}
        first = task_common.gripper_params(data, {"open_time_s": 0.8})
        release = task_load.release_open_params(data, {"open_time_s": 0.2})
        self.assertEqual((first.transport_speed, first.open_time_s, first.open_position),
                         (210, 0.8, (123, 123)))
        self.assertEqual((release.transport_speed, release.open_time_s, release.open_position,
                          release.move_time_ms, release.ids), (320, 0.2, (222, 333), 500, (3, 2)))
        for invalid in ({"ids": [1, 2]}, {"grip_duty": 400}, {"open_position": -1},
                        {"transport_speed": 1001}, {"open_time_s": -1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                task_load.release_open_params(data, invalid)

    def test_home_position_defaults_scalar_pair_and_overrides(self):
        self.assertEqual(GripperParams().home_position, (0, 0))
        self.assertEqual(GripperParams.from_table({"home_position": 420}).home_position, (420, 420))
        params = task_common.gripper_params(
            {"gripper": {"home_position": [100, 200]}},
            task_common.parse_overrides(["home_position=[420,580]"]),
            ({"home_position": [300, 400]}, "object"))
        self.assertEqual(params.home_position, (420, 580))
        for value in (-1, 1001, [0], [0, 1, 2], [0, 1001], [True, 0], 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GripperParams(home_position=value)

    def test_pair_and_override_rules(self):
        params = GripperParams.from_table({"ids": [1, 2], "open_position": [600, 580]})
        self.assertEqual(params.open_position, (600, 580))
        self.assertEqual(params.targets(params.open_position), {1: 600, 2: 580})
        self.assertEqual(params.with_overrides({"grip_duty": 50}, "x").grip_duty, 50)
        with self.assertRaisesRegex(ValueError, "不可覆盖"):
            params.with_overrides({"ids": [2, 3]}, "x")
        with self.assertRaises(ValueError):
            params.with_overrides({"grip_duty": 1500}, "x")
        with self.assertRaises(ValueError):
            GripperParams(transport_speed=0)
        with self.assertRaises(ValueError):
            GripperParams(transport_control="speed", transport_speed=60)
        with self.assertRaises(ValueError):
            GripperParams(open_position=[1, 2, 3])
        with self.assertRaises(ValueError):
            GripperParams(ids=(1, 1))

    def test_parse_cli_overrides(self):
        self.assertEqual(task_common.parse_overrides(["grip_duty=400", "open_position=[600,580]"]),
                         {"grip_duty": 400, "open_position": [600, 580]})
        with self.assertRaises(ValueError):
            task_common.parse_overrides(["grip_duty"])
        with self.assertRaises(ValueError):
            task_common.parse_overrides(["grip_duty=[1,"])

    def test_shipped_param_files_load(self):
        for path in (task_load.DEFAULT_PARAMS, task_grasping.DEFAULT_PARAMS,
                     task_manipulation.DEFAULT_PARAMS):
            data = task_common.load_params(path)
            task_common.gripper_params(data, {})
        data = task_common.load_params(task_grasping.DEFAULT_PARAMS)
        for code in data["objects"]:
            task_grasping.object_params(data, {}, code)


class CliTests(unittest.TestCase):
    def test_load_close_enter_holds_instead_of_releasing(self):
        args = task_load.make_parser().parse_args(["close"])
        controller = types.SimpleNamespace(bus=types.SimpleNamespace(dry_run=False))
        data = task_common.load_params(task_load.DEFAULT_PARAMS)
        with patch("task_load.Gripper") as gripper_class, patch("task_load.hold_until_enter") as wait:
            self.assertEqual(task_load.body(args)(controller, data, {}), 0)
            self.assertEqual([call[0] for call in gripper_class.return_value.method_calls],
                             ["close", "hold_position"])
            self.assertIn("保持当前位置", wait.call_args.args[1])

    def test_load_close_preview_does_not_invent_positions(self):
        code, out, err = self.run_cli(task_load.main, ["--dry-run", "close"])
        self.assertEqual(code, 0, err)
        self.assertIn("离线无法读取实际位置", out)

    def test_load_run_waits_in_position_hold_before_opening(self):
        args = task_load.make_parser().parse_args(["run"])
        controller = types.SimpleNamespace(bus=types.SimpleNamespace(dry_run=False))
        data = task_common.load_params(task_load.DEFAULT_PARAMS)
        with patch("task_load.Gripper") as gripper_class, \
                patch("task_load.hold_until_enter") as wait, \
                patch("task_load.confirm") as confirm, redirect_stdout(io.StringIO()):
            gripper = gripper_class.return_value
            stages = []

            def wait_for_enter(*args, **kwargs):
                stages.append([call[0] for call in gripper.method_calls])
                return 1.0

            wait.side_effect = wait_for_enter
            self.assertEqual(task_load.body(args)(controller, data, {}), 0)
            self.assertEqual(stages, [["home", "open", "close"],
                                      ["home", "open", "close", "hold_position"]])
            self.assertEqual([call[0] for call in gripper.method_calls],
                             ["home", "open", "close", "hold_position", "open", "park"])
            self.assertNotIn("target_s", wait.call_args_list[0].kwargs)
            self.assertEqual(wait.call_args_list[1].kwargs["target_s"], 10)
            self.assertEqual(confirm.call_count, 2)  # 闭合前确认、取出后归零确认

    def run_cli(self, entry, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch("dexhand.task_common.load_config", return_value=config()), \
                redirect_stdout(out), redirect_stderr(err):
            code = entry(argv)
        return code, out.getvalue(), err.getvalue()

    def test_load_run_preview(self):
        code, out, _ = self.run_cli(task_load.main, ["--dry-run", "run"])
        self.assertEqual(code, 0)
        for phrase in ("回零", "张开", "闭合", "夹紧", "释放", "回退", "未打开串口"):
            self.assertIn(phrase, out)

    def test_load_two_open_profiles_preview(self):
        code, out, err = self.run_cli(task_load.main,
            ["--dry-run", "run", "--set", "transport_speed=210",
             "--set", "release_open.transport_speed=320",
             "--set", "release_open.open_position=[222,333]"])
        self.assertEqual(code, 0, err)
        self.assertIn("{3: 210, 2: -210}", out)
        release_out = out.split("释放张开：使用 [release_open] 参数", 1)[1]
        self.assertIn("{3: 320, 2: -320}", release_out)
        self.assertIn("{3: 222, 2: 333}", release_out)
        code, out, err = self.run_cli(task_load.main,
            ["--dry-run", "release-open", "--set", "release_open.open_time_s=0.2"])
        self.assertEqual(code, 0, err)
        self.assertIn("转动 0.2 秒", out)
        self.assertNotIn("闭合", out)

    def test_load_invalid_release_profile_rejected_before_motion(self):
        code, out, err = self.run_cli(task_load.main,
            ["--dry-run", "run", "--set", "release_open.open_position=1001"])
        self.assertEqual(code, 1)
        self.assertIn("open_position", err)
        self.assertNotIn("TX ", out)

    def test_load_calibrated_home_preview(self):
        for action in ("home", "park", "run"):
            with self.subTest(action=action):
                code, out, err = self.run_cli(task_load.main,
                    ["--dry-run", action, "--set", "home_position=[420,580]"])
                self.assertEqual(code, 0, err)
                self.assertIn("机械零位 {3: 420, 2: 580}", out)

    def test_grasping_object_selection(self):
        code, out, _ = self.run_cli(task_grasping.main, ["--dry-run", "list"])
        self.assertEqual(code, 0)
        self.assertIn("G01", out)
        code, out, _ = self.run_cli(task_grasping.main,
                                    ["--dry-run", "run", "--object", "G09", "--set", "grip_duty=80"])
        self.assertEqual(code, 0)
        self.assertIn("海绵", out)
        self.assertIn("{1: -80, 2: 80}", out)
        code, _, err = self.run_cli(task_grasping.main, ["--dry-run", "run", "--object", "G99"])
        self.assertEqual(code, 1)
        self.assertIn("没有物体 G99", err)

    def test_manipulation_actions(self):
        code, out, _ = self.run_cli(task_manipulation.main, ["--dry-run", "clip"])
        self.assertEqual(code, 0)
        self.assertEqual(out.count("次捏合"), 2)
        code, out, _ = self.run_cli(task_manipulation.main, ["--dry-run", "knob"])
        self.assertEqual(code, 0)
        self.assertIn("90°", out)
        code, _, err = self.run_cli(task_manipulation.main, ["--dry-run", "wrist"])
        self.assertEqual(code, 1)
        self.assertIn("--position", err)

    def test_keyboard_interrupt_stops_everything(self):
        args = types.SimpleNamespace(config="unused", params=task_load.DEFAULT_PARAMS, set=[],
                                     dry_run=True, verbose=False)

        def body(controller, data, overrides):
            controller.run_motor({1: 20, 2: -20}, control="speed")
            raise KeyboardInterrupt

        out = io.StringIO()
        with patch("dexhand.task_common.load_config", return_value=config()), redirect_stdout(out):
            code = task_common.run(args, body)
        self.assertEqual(code, 130)
        self.assertIn("急停", out.getvalue())
        self.assertIn("55 55 03 04 1F 00", out.getvalue().upper())  # 3 号也被卸力


if __name__ == "__main__":
    unittest.main()
