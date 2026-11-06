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
