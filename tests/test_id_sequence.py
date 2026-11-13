"""ID 管理与命令行的无硬件测试。"""

from contextlib import redirect_stdout
import io
import time
import unittest
from unittest.mock import patch

import main
from dexhand.servo_controller import (BROADCAST_ID, Command, ServoBus, ServoController,
                              decode_packet, encode_packet)
from dexhand.settings import Config, SerialSettings, ServoSettings


def config():
    serial = SerialSettings(command_interval=0, timeout=0.005, read_retries=0)
    return Config(serial, (ServoSettings(1), ServoSettings(2)))


class FakeSerial:
    """模拟只有一只舵机的串口，并按 ID 读写指令更新硬件 ID。"""

    def __init__(self, servo_id=1, *, write_changes_id=True, occupied_id=None):
        self.servo_id = servo_id
        self.write_changes_id = write_changes_id
        self.occupied_id = occupied_id
        self.timeout = 0.005
        self.pending = bytearray()
        self.packets = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.pending)

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, data):
        packet = decode_packet(data)
        self.packets.append(packet)
        if packet.command == Command.ID_READ and packet.servo_id in (self.servo_id, BROADCAST_ID):
            self.pending.extend(encode_packet(self.servo_id, Command.ID_READ,
                                              bytes((self.servo_id,))))
        elif packet.command == Command.ID_READ and packet.servo_id == self.occupied_id:
            self.pending.extend(encode_packet(self.occupied_id, Command.ID_READ,
                                              bytes((self.occupied_id,))))
        elif (packet.command == Command.ID_WRITE and packet.servo_id == self.servo_id
              and self.write_changes_id):
            self.servo_id = packet.parameters[0]
        return len(data)

    def flush(self):
        pass

    def read(self, size):
        data = bytes(self.pending[:size])
        del self.pending[:size]
        if not data:
            time.sleep(0.0005)
        return data

    def close(self):
        self.closed = True


class IdTests(unittest.TestCase):
    def test_known_and_broadcast_id_read(self):
        wire = FakeSerial()
        with ServoController(config(), bus=ServoBus(config().serial, transport=wire)) as controller:
            self.assertEqual(controller.read_id(1), 1)
            self.assertEqual(controller.read_id(), 1)
            with self.assertRaises(ValueError):
                controller.bus.read(BROADCAST_ID, Command.POS_READ, 2)
        self.assertTrue(wire.closed)
        self.assertEqual(wire.packets[-1].servo_id, BROADCAST_ID)

    def test_set_id_verifies_and_updates_in_memory_config(self):
        wire = FakeSerial()
        with ServoController(config(), bus=ServoBus(config().serial, transport=wire)) as controller:
            self.assertEqual(controller.set_id(1, 4), 4)
            self.assertEqual(controller.read_id(4), 4)
            self.assertEqual([item.id for item in controller.config.servos], [4, 2])
            self.assertEqual(set(controller.servos), {2, 4})
        self.assertEqual(wire.servo_id, 4)
        commands = [packet.command for packet in wire.packets]
        self.assertEqual(commands.count(Command.ID_WRITE), 1)
        self.assertLess(commands.index(Command.MOVE_STOP), commands.index(Command.ID_WRITE))
        self.assertLess(commands.index(Command.LOAD_WRITE), commands.index(Command.ID_WRITE))

    def test_unverified_id_write_is_not_retried(self):
        wire = FakeSerial(write_changes_id=False)
        with ServoController(config(), bus=ServoBus(config().serial, transport=wire)) as controller:
            with self.assertRaisesRegex(RuntimeError, "可能已经改变"):
                controller.set_id(1, 4)
            self.assertEqual([item.id for item in controller.config.servos], [1, 2])
        self.assertEqual(sum(p.command == Command.ID_WRITE for p in wire.packets), 1)

    def test_configured_id_collision_rejected_before_transmission(self):
        wire = FakeSerial()
        with ServoController(config(), bus=ServoBus(config().serial, transport=wire)) as controller:
            with self.assertRaises(ValueError):
                controller.set_id(1, 2)
        self.assertEqual(wire.packets, [])

    def test_hardware_id_collision_rejected_before_write(self):
        wire = FakeSerial(occupied_id=4)
        with ServoController(config(), bus=ServoBus(config().serial, transport=wire)) as controller:
            with self.assertRaisesRegex(ValueError, "总线上已有舵机"):
                controller.set_id(1, 4)
        self.assertFalse(any(packet.command == Command.ID_WRITE for packet in wire.packets))

    def test_cli_id_discovery(self):
        wire = FakeSerial()
        output = io.StringIO()
        with patch("main.load_config", return_value=config()), \
                patch("dexhand.servo_controller.serial.Serial", return_value=wire), \
                redirect_stdout(output):
            self.assertEqual(main.main(["read-id", "--discover"]), 0)
        self.assertIn("当前硬件 ID：1", output.getvalue())

    def test_cli_set_id_and_preview(self):
        wire = FakeSerial()
        output = io.StringIO()
        with patch("main.load_config", return_value=config()), \
                patch("dexhand.servo_controller.serial.Serial", return_value=wire), \
                redirect_stdout(output):
            self.assertEqual(main.main(["set-id", "1", "4"]), 0)
        self.assertEqual(wire.servo_id, 4)
        self.assertIn("已验证硬件 ID 从 1 改为 4", output.getvalue())

        output = io.StringIO()
        with patch("main.load_config", return_value=config()), \
                patch("dexhand.servo_controller.serial.Serial") as serial_factory, \
                redirect_stdout(output):
            self.assertEqual(main.main(["--dry-run", "set-id", "1", "4"]), 0)
        serial_factory.assert_not_called()
        self.assertIn("硬件 ID 未改变", output.getvalue())


if __name__ == "__main__":
    unittest.main()
