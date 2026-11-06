"""基于工作区《02 总线舵机通信协议.pdf》的半双工总线控制。

需要带自动收发方向切换的 USB 转总线舵机接口；不直接连接普通全双工 TX/RX。
写指令没有应答，发送成功不等于机械运动已完成。
"""

from dataclasses import dataclass, replace
from enum import IntEnum
import logging
import struct
import threading
import time
from typing import Iterable, Mapping

import serial

from dexhand.settings import Config, SerialSettings, integer, motor_limit


LOG = logging.getLogger(__name__)
HEADER = b"\x55\x55"
BROADCAST_ID = 254


class Command(IntEnum):
    MOVE_TIME_WRITE = 1
    MOVE_TIME_WAIT_WRITE = 7
    MOVE_START = 11
    MOVE_STOP = 12
    ID_WRITE = 13
    ID_READ = 14
    TEMP_READ = 26
    VIN_READ = 27
    POS_READ = 28
    MODE_WRITE = 29
    MODE_READ = 30
    LOAD_WRITE = 31
    LOAD_READ = 32


class ProtocolError(RuntimeError):
    """应答格式或内容不符合协议。"""


class ResponseTimeout(TimeoutError):
    """在限定时间内没有收到匹配且校验正确的应答。"""


@dataclass(frozen=True)
class Packet:
    servo_id: int
    command: int
    parameters: bytes


def encode_packet(servo_id: int, command: int, parameters: bytes = b"") -> bytes:
    integer(servo_id, "ID", 0, BROADCAST_ID)
    integer(int(command), "command", 0, 255)
    if len(parameters) > 252:
        raise ValueError("参数过长")
    body = bytes((servo_id, len(parameters) + 3, command)) + parameters
    return HEADER + body + bytes((~sum(body) & 0xFF,))


def decode_packet(data: bytes) -> Packet:
    if len(data) < 6 or data[:2] != HEADER:
        raise ProtocolError("帧头或帧长度错误")
    if data[2] > BROADCAST_ID or data[3] < 3 or len(data) != data[3] + 3:
        raise ProtocolError("ID 或 Length 字段错误")
    if data[-1] != (~sum(data[2:-1]) & 0xFF):
        raise ProtocolError("校验和错误")
    return Packet(data[2], data[4], data[5:-1])


def pop_packet(buffer: bytearray) -> Packet | None:
    """从字节流取出完整帧；支持碎片、粘包、噪声和错误 Length 后重新同步。"""
    offset = 0
    while True:
        start = buffer.find(HEADER, offset)
        if start < 0 or start + 4 > len(buffer):
            break
        length = buffer[start + 3]
        end = start + length + 3
        if length >= 3 and end <= len(buffer):
            try:
                packet = decode_packet(bytes(buffer[start:end]))
            except ProtocolError:
                pass
            else:
                del buffer[:end]
                return packet
        # 即使错误的长度声称帧尚未结束，也继续寻找后面的有效帧。
        offset = start + 1
    # 最长合法帧为 258 字节，保留可能尚未接收完整的尾部。
    if len(buffer) > 258:
        del buffer[:-258]
    return None


class ServoBus:
    """一次只进行一笔收发；仅重试读取，不重发运动写指令。"""

    def __init__(self, settings: SerialSettings, *, dry_run: bool = False, transport=None):
        self.settings = settings
        self.dry_run = dry_run
        self._lock = threading.RLock()
        self._last_io = 0.0
        self._serial = transport
        if not dry_run and transport is None:
            self._serial = serial.Serial(
                port=settings.port, baudrate=settings.baudrate,
                bytesize=settings.bytesize, parity=settings.parity,
                stopbits=settings.stopbits, timeout=settings.timeout,
                write_timeout=settings.write_timeout,
                xonxoff=False, rtscts=False, dsrdtr=False,
            )

    def _send(self, servo_id: int, command: int, parameters: bytes = b""):
        frame = encode_packet(servo_id, command, parameters)
        if self.dry_run:
            print(f"TX {frame.hex(' ').upper()}")
            return
        remaining = self.settings.command_interval - (time.monotonic() - self._last_io)
        if remaining > 0:
            time.sleep(remaining)
        LOG.debug("TX %s", frame.hex(" ").upper())
        if self._serial.write(frame) != len(frame):
            raise serial.SerialTimeoutException("串口没有完整写入指令帧")
        self._serial.flush()
        self._last_io = time.monotonic()

    def write(self, servo_id: int, command: int, parameters: bytes = b""):
        with self._lock:
            self._send(servo_id, command, parameters)

    def read(self, servo_id: int, command: int, parameter_count: int) -> bytes:
        # 协议仅允许 ID_READ 使用广播地址读取应答，且总线上只能接一只舵机。
        broadcast_id_read = (servo_id == BROADCAST_ID and command == Command.ID_READ
                             and parameter_count == 1)
        integer(servo_id, "读取 ID", 0, BROADCAST_ID if broadcast_id_read else 253)
        if self.dry_run:
            raise ValueError("离线预览不读取真实应答，请去掉 --dry-run 后查询状态")
        with self._lock:
            original_timeout = self._serial.timeout
            try:
                for _ in range(self.settings.read_retries + 1):
                    self._serial.reset_input_buffer()
                    buffer = bytearray()
                    self._send(servo_id, command)
                    deadline = time.monotonic() + self.settings.timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._serial.timeout = min(remaining, 0.02)
                        buffer.extend(self._serial.read(max(1, min(self._serial.in_waiting, 256))))
                        while (packet := pop_packet(buffer)) is not None:
                            if ((packet.servo_id == servo_id or broadcast_id_read)
                                    and packet.command == command
                                    and len(packet.parameters) == parameter_count):
                                LOG.debug("RX ID=%d CMD=%d DATA=%s", packet.servo_id, command,
                                          packet.parameters.hex(" "))
                                return packet.parameters
                            # 忽略适配器回显、其他 ID/命令及参数长度不符的帧。
                    self._last_io = time.monotonic()
            finally:
                self._serial.timeout = original_timeout
                self._last_io = time.monotonic()
        raise ResponseTimeout(
            f"ID {servo_id} 指令 {int(command)} 无有效应答"
            f"（共 {self.settings.read_retries + 1} 次读取）；"
            "请检查串口、波特率、供电、半双工接口和舵机 ID"
        )

    def read_id(self, servo_id: int | None = None) -> int:
        """读取硬件 ID；省略 ID 时广播查询，须只连接一只舵机。"""
        target = BROADCAST_ID if servo_id is None else integer(servo_id, "舵机 ID", 0, 253)
        actual_id = self.read(target, Command.ID_READ, 1)[0]
        integer(actual_id, "舵机返回的 ID", 0, 253)
        if servo_id is not None and actual_id != servo_id:
            raise ProtocolError(f"ID {servo_id} 返回了不一致的 ID {actual_id}")
        return actual_id

    def close(self):
        if self._serial is not None:
            self._serial.close()


@dataclass(frozen=True)
class ServoStatus:
    servo_id: int
    mode: str
    mode_parameter_2: int
    motor_speed: int
    position: int
    voltage_mv: int
    temperature_c: int
    loaded: bool

    @property
    def angle_deg(self) -> float:
        return self.position * 0.24


class ServoController:
    """面向已配置舵机的高级控制接口。

    运动与状态：
        set_mode()：切换位置或电机模式，停止当前动作并保持卸力。
        move()：按 ID 设置目标位置和动作时间，并启动位置运动。
        move_then_motor()：等待位置动作设定时间后切换为电机模式。
        run_motor()：在电机模式下按占空比或转速参数运行。
        apply_config()：执行配置中各舵机的位置或电机参数。
        stop()：停止目标舵机并卸力。
        read_status()：读取模式、位置、电压、温度和装载状态。

    身份管理：
        read_id()：定向读取 ID，或在仅连接一只舵机时广播发现 ID。
        set_id()：验证旧 ID 和新 ID 后写入硬件 ID，并用新 ID 复查。

    辅助接口：
        selected_ids()：校验并返回目标 ID；has_running_motors 表示本实例
        是否记录了正在运行的非零速电机。

    运动与状态操作的目标 ID 必须已在配置中；省略 ids 时选择全部，
    move() 和 move_then_motor() 则由 positions 的键指定目标。ID 管理可
    操作配置外的 ID；set_id() 不修改磁盘上的配置文件。支持 with：退出时
    尝试停止本实例启动的电机并关闭总线。
    move(wait=True) 只等待设定的动作时间，不确认舵机实际到位。
    """

    def __init__(self, config: Config, *, dry_run: bool = False, bus: ServoBus | None = None):
        self.config = config
        self.servos = {item.id: item for item in config.servos}
        self.bus = bus if bus is not None else ServoBus(config.serial, dry_run=dry_run)
        self._active_motors: dict[int, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._active_motors:
                try:
                    self.stop(sorted(self._active_motors))
                except Exception:
                    if exc_type is None:
                        raise
                    LOG.exception("退出时停止电机失败，请检查硬件")
        finally:
            self.bus.close()

    def selected_ids(self, ids: Iterable[int] | None = None) -> list[int]:
        result = list(self.servos) if ids is None else list(ids)
        if not result:
            raise ValueError("至少选择一个舵机")
        for servo_id in result:
            integer(servo_id, "舵机 ID", 0, 253)
        if len(set(result)) != len(result):
            raise ValueError("选择的 ID 不得重复")
        if any(servo_id not in self.servos for servo_id in result):
            raise ValueError(f"只能操作配置文件中的 ID：{list(self.servos)}")
        return result

    def read_id(self, servo_id: int | None = None) -> int:
        """读取已知 ID；省略时广播发现，必须只连接一只舵机。"""
        return self.bus.read_id(servo_id)

    def set_id(self, old_id: int, new_id: int) -> int:
        """停止并卸力后改写硬件 ID；请确保目标独立接线且新 ID 未占用。"""
        integer(old_id, "旧舵机 ID", 0, 253)
        integer(new_id, "新舵机 ID", 0, 253)
        if old_id == new_id:
            raise ValueError("新旧 ID 相同，无需写入")

        if self._active_motors:
            raise RuntimeError("请先停止本实例运行中的电机，再修改舵机 ID")
        if not self.bus.dry_run:
            self.read_id(old_id)
            try:
                self.read_id(new_id)
            except ResponseTimeout:
                pass
            else:
                raise ValueError(f"总线上已有舵机使用新 ID {new_id}")
        self.bus.write(old_id, Command.MOVE_STOP)
        self.bus.write(old_id, Command.LOAD_WRITE, b"\x00")
        self.bus.write(old_id, Command.ID_WRITE, bytes((new_id,)))
        if self.bus.dry_run:
            return new_id
        try:
            self.read_id(new_id)
        except (TimeoutError, ProtocolError) as exc:
            raise RuntimeError(
                f"ID 写入指令已发送，但无法用新 ID {new_id} 验证；"
                "硬件 ID 可能已经改变，请先单独查询，不要盲目重发"
            ) from exc

        return new_id

    def _mode_packet(self, servo_id: int, mode: str, control: str, speed: int = 0):
        self.bus.write(servo_id, Command.MODE_WRITE,
                       struct.pack("<BBh", int(mode == "motor"), int(control == "speed"), speed))

    def _prepare(self, servo_id: int, mode: str, control: str):
        # 切换模式时先卸力，并清除正在执行的运动；电机先设成零速。
        self.bus.write(servo_id, Command.LOAD_WRITE, b"\x00")
        self.bus.write(servo_id, Command.MOVE_STOP)
        self._mode_packet(servo_id, mode, control)
        self._active_motors.pop(servo_id, None)

    def _cleanup_after_error(self, ids: Iterable[int]):
        try:
            self.stop(ids)
        except Exception:
            LOG.exception("异常后的停止指令未能全部发送，请检查硬件")

    def set_mode(self, mode: str, ids: Iterable[int] | None = None, *, control: str | None = None):
        if mode not in ("servo", "motor"):
            raise ValueError("mode 必须为 servo 或 motor")
        if control is not None:
            motor_limit(control)
        selected = self.selected_ids(ids)
        try:
            for servo_id in selected:
                self._prepare(servo_id, mode, control or self.servos[servo_id].motor_control)
        except BaseException:
            self._cleanup_after_error(selected)
            raise

    def move(self, positions: Mapping[int, int], *, time_ms: int | None = None, wait: bool = True):
        """预设所有目标；选中全部配置 ID 时广播启动，子集按 ID 启动。"""
        selected = self.selected_ids(positions)
        prepared = []
        for servo_id in selected:
            cfg = self.servos[servo_id]
            position = integer(positions[servo_id], "position", cfg.min_position, cfg.max_position)
            duration = integer(cfg.move_time_ms if time_ms is None else time_ms,
                               "time_ms", 0, 30000)
            prepared.append((servo_id, struct.pack("<HH", position, duration), duration))
        # 全部参数校验完成后才开始发送，防止后面的参数错误造成部分舵机先运动。
        try:
            for servo_id, parameters, _ in prepared:
                self._prepare(servo_id, "servo", "duty")
                self.bus.write(servo_id, Command.MOVE_TIME_WAIT_WRITE, parameters)
            for servo_id in selected:
                self.bus.write(servo_id, Command.LOAD_WRITE, b"\x01")
            if len(selected) > 1 and set(selected) == set(self.servos):
                self.bus.write(BROADCAST_ID, Command.MOVE_START)
            else:
                # 子集不能广播启动，否则未选中舵机的旧预设也可能被触发。
                for servo_id in selected:
                    self.bus.write(servo_id, Command.MOVE_START)
            if wait and not self.bus.dry_run:
                time.sleep(max(item[2] for item in prepared) / 1000)
        except BaseException:
            self._cleanup_after_error(selected)
            raise

    def hold_position(self, ids: Iterable[int] | None = None, *, time_ms: int = 0):
        """停下本实例的电机输出，读取实际位置并转入位置保持；失败时停止卸力。"""
        selected = self.selected_ids(ids)
        integer(time_ms, "time_ms", 0, 30000)
        if self.bus.dry_run:
            print(f"[预览] 停止夹紧转动，读取 ID {selected} 的实际位置并切换位置保持；"
                  "离线无法读取实际位置，不生成虚构的保持目标。")
            return None
        try:
            # 先归零所有正在运行的电机输出，不先卸力，避免读数期间继续夹紧。
            # 保留运行记录直到 move 成功准备，读数失败时仍能执行停止清理。
            for servo_id in selected:
                if servo_id in self._active_motors:
                    self._mode_packet(servo_id, "motor", self._active_motors[servo_id], 0)
            positions = {}
            for servo_id in selected:
                position, = struct.unpack("<h", self.bus.read(servo_id, Command.POS_READ, 2))
                cfg = self.servos[servo_id]
                positions[servo_id] = integer(position, f"ID {servo_id} 保持位置",
                                               cfg.min_position, cfg.max_position)
            self.move(positions, time_ms=time_ms)
            return positions
        except BaseException:
            self._cleanup_after_error(selected)
            raise

    def move_then_motor(self, positions: Mapping[int, int], speed: int, *,
                        time_ms: int | None = None, control: str | None = None):
        """先完成位置动作的计时，再将相同目标切换到电机模式。"""
        selected = self.selected_ids(positions)
        # 在开始位置运动前校验电机参数，避免第二阶段才发现参数错误。
        for servo_id in selected:
            selected_control = control or self.servos[servo_id].motor_control
            limit = motor_limit(selected_control)
            integer(speed, f"ID {servo_id} motor_speed", -limit, limit)
        self.move(positions, time_ms=time_ms)
        self.run_motor(speed, selected, control=control)

    def run_motor(self, speed: int | Mapping[int, int], ids: Iterable[int] | None = None, *,
                  control: str | None = None):
        """进入电机模式运行。speed 可为统一数值，或按 ID 给出各自速度的映射。

        先对所有目标完成模式准备和装载，最后连续发送各目标的速度报文，
        使多只电机的启动时刻只相差一个通信间隔。
        """
        if isinstance(speed, Mapping):
            selected = self.selected_ids(speed if ids is None else ids)
            if ids is not None and set(selected) != set(speed):
                raise ValueError("speed 映射的 ID 必须与 ids 一致")
            speeds = {servo_id: speed[servo_id] for servo_id in selected}
        else:
            selected = self.selected_ids(ids)
            speeds = {servo_id: speed for servo_id in selected}
        controls = {}
        for servo_id in selected:
            selected_control = control or self.servos[servo_id].motor_control
            limit = motor_limit(selected_control)
            integer(speeds[servo_id], f"ID {servo_id} motor_speed", -limit, limit)
            controls[servo_id] = selected_control
        try:
            for servo_id in selected:
                self._prepare(servo_id, "motor", controls[servo_id])
            for servo_id in selected:
                self.bus.write(servo_id, Command.LOAD_WRITE, b"\x01")
            for servo_id in selected:
                if speeds[servo_id]:
                    # 先登记再发送；即使串口在部分写入后报错，退出时仍尝试停止。
                    self._active_motors[servo_id] = controls[servo_id]
                self._mode_packet(servo_id, "motor", controls[servo_id], speeds[servo_id])
        except BaseException:
            self._cleanup_after_error(selected)
            raise

    def apply_config(self, ids: Iterable[int] | None = None):
        selected = self.selected_ids(ids)
        positions = {i: self.servos[i].position for i in selected if self.servos[i].mode == "servo"}
        try:
            if positions:
                self.move(positions)
            for servo_id in selected:
                cfg = self.servos[servo_id]
                if cfg.mode == "motor":
                    self.run_motor(cfg.motor_speed, [servo_id], control=cfg.motor_control)
        except BaseException:
            self._cleanup_after_error(selected)
            raise

    @property
    def has_running_motors(self) -> bool:
        return bool(self._active_motors)

    def stop(self, ids: Iterable[int] | None = None):
        """停止并卸力。一个 ID 发送失败不阻止处理其余 ID。

        本实例启动过的电机先连续写入零速，使多只电机几乎同时停转；
        随后再逐个发送停止和卸力。未知状态的舵机不强制切换模式。
        """
        selected = self.selected_ids(ids)
        errors = []
        failed = set()
        for servo_id in selected:
            if servo_id in self._active_motors:
                control = int(self._active_motors[servo_id] == "speed")
                try:
                    self.bus.write(servo_id, Command.MODE_WRITE, struct.pack("<BBh", 1, control, 0))
                except Exception as exc:
                    failed.add(servo_id)
                    errors.append(f"ID {servo_id}: {exc}")
        for servo_id in selected:
            for command, parameters in ((Command.MOVE_STOP, b""), (Command.LOAD_WRITE, b"\x00")):
                try:
                    self.bus.write(servo_id, command, parameters)
                except Exception as exc:
                    failed.add(servo_id)
                    errors.append(f"ID {servo_id}: {exc}")
            if servo_id not in failed:
                self._active_motors.pop(servo_id, None)
        if errors:
            raise RuntimeError("部分停止指令发送失败：" + "; ".join(errors))

    def read_status(self, servo_id: int) -> ServoStatus:
        self.selected_ids([servo_id])
        mode, second, speed = struct.unpack("<BBh", self.bus.read(servo_id, Command.MODE_READ, 4))
        if mode not in (0, 1):
            raise ProtocolError(f"ID {servo_id} 返回未知模式 {mode}")
        position, = struct.unpack("<h", self.bus.read(servo_id, Command.POS_READ, 2))
        voltage, = struct.unpack("<H", self.bus.read(servo_id, Command.VIN_READ, 2))
        temperature = self.bus.read(servo_id, Command.TEMP_READ, 1)[0]
        loaded = self.bus.read(servo_id, Command.LOAD_READ, 1)[0]
        if loaded not in (0, 1):
            raise ProtocolError(f"ID {servo_id} 返回未知装载状态 {loaded}")
        return ServoStatus(servo_id, "servo" if mode == 0 else "motor", second,
                           speed, position, voltage, temperature, bool(loaded))

