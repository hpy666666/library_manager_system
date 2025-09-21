#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能环境控制系统 - Web版本
基于Flask的Web界面，支持实时数据监控和设备控制
"""

from flask import Flask, render_template, jsonify, request, make_response
from flask_socketio import SocketIO, emit
import threading
import time
import random
import math
from datetime import datetime
import json

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    print("Warning: pyserial not available, using simulation mode only")

# 串口通信协议常量
SOF = 0xAA
EOF = 0x55

def calc_cs(data: bytes) -> int:
    """计算校验和"""
    cs = 0
    for b in data:
        cs ^= b
    return cs & 0xFF

def pack_frame(cmd: int, payload: bytes) -> bytes:
    """打包数据帧"""
    length = 1 + len(payload)
    body = bytes([length, cmd]) + payload
    cs = calc_cs(body)
    return bytes([SOF]) + body + bytes([cs, EOF])

class FrameParser:
    """数据帧解析器"""
    def __init__(self):
        self.state = 0
        self.buf = bytearray()
        self.length = 0
        self.expected_payload = 0
        self.payload = bytearray()

    def feed(self, b: int):
        """输入字节并解析"""
        if self.state == 0:
            if b == SOF:
                self.buf.clear()
                self.state = 1
        elif self.state == 1:
            self.length = b
            self.buf = bytearray([b])
            self.state = 2
        elif self.state == 2:
            self.buf.append(b)
            if self.length == 1:
                self.state = 3
            else:
                self.state = 21
                self.expected_payload = self.length - 1
                self.payload = bytearray()
        elif self.state == 21:
            self.payload.append(b)
            if len(self.payload) >= self.expected_payload:
                self.buf += self.payload
                self.state = 3
        elif self.state == 3:
            cs_calc = calc_cs(bytes(self.buf))
            if cs_calc != b:
                self.state = 0
                return None
            self.state = 4
        elif self.state == 4:
            if b == EOF:
                length = self.buf[0]
                cmd = self.buf[1]
                payload = bytes(self.buf[2:2 + (length - 1)])
                self.state = 0
                return (cmd, payload)
            else:
                self.state = 0
        return None

class SerialManager:
    """串口管理器"""
    def __init__(self, callback=None):
        self.callback = callback
        self.ser = None
        self.rx_thread = None
        self.stop_flag = False
        self.parser = FrameParser()
        self.connected = False

    def list_ports(self):
        """列出可用串口"""
        if not SERIAL_AVAILABLE:
            return []
        
        try:
            ports = []
            for port in serial.tools.list_ports.comports():
                ports.append(port.device)  # 只返回设备名称，如 COM1, COM2
            return ports
        except Exception as e:
            print(f"Error listing serial ports: {e}")
            return []

    def connect(self, port, baudrate=115200):
        """连接串口"""
        if not SERIAL_AVAILABLE:
            return False, "Serial library not available"
        
        try:
            if self.ser and self.ser.is_open:
                self.disconnect()
            
            self.ser = serial.Serial(port, baudrate, timeout=1)
            self.connected = True
            self.stop_flag = False
            
            # 启动接收线程
            self.rx_thread = threading.Thread(target=self._rx_worker)
            self.rx_thread.daemon = True
            self.rx_thread.start()
            
            return True, f"Connected to {port}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    def disconnect(self):
        """断开串口连接"""
        self.connected = False
        self.stop_flag = True
        
        if self.rx_thread:
            self.rx_thread.join(timeout=1)
        
        if self.ser and self.ser.is_open:
            self.ser.close()
        
        return True, "Disconnected"

    def send_command(self, cmd, payload=b''):
        """发送命令"""
        if not self.connected or not self.ser:
            return False, "Not connected"
        
        try:
            frame = pack_frame(cmd, payload)
            self.ser.write(frame)
            return True, "Command sent"
        except Exception as e:
            return False, f"Send failed: {str(e)}"

    def _rx_worker(self):
        """接收数据线程"""
        while not self.stop_flag and self.ser and self.ser.is_open:
            try:
                if self.ser.in_waiting > 0:
                    data = self.ser.read(self.ser.in_waiting)
                    for byte in data:
                        result = self.parser.feed(byte)
                        if result and self.callback:
                            self.callback(result[0], result[1])
                time.sleep(0.01)
            except Exception as e:
                print(f"Serial RX error: {e}")
                break

app = Flask(__name__)
app.config['SECRET_KEY'] = 'environment_control_secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

class EnvironmentData:
    """环境数据管理"""
    def __init__(self):
        self.data = {
            'temperature': 25.0,
            'humidity': 60.0,
            'co2': 400.0,
            'light': 350.0,
            'smoke': 0.0
        }
        self.history = []
        self.device_states = {
            "heating": False,
            "cooling": False,
            "humidify": False,
            "dehumidify": False,
            "ventilation": False,
            "close_vent": False
        }
        self.thresholds = {
            'temperature': {'min': 20, 'max': 26},
            'humidity': {'min': 40, 'max': 70},
            'co2': {'max': 1000},
            'light': {'min': 100, 'max': 800},
            'smoke': {'max': 50}
        }
        self.events = []
        self.running = True
        self.use_simulation = True  # 默认使用模拟数据
        self.data_mode = 'simulation'  # 'serial' 或 'simulation'
        
        # 串口管理器
        self.serial_manager = SerialManager(callback=self.on_serial_data)
        
    def add_event(self, event_type, message, level="INFO"):
        """添加事件记录"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        event = {
            'timestamp': timestamp,
            'type': event_type,
            'message': message,
            'level': level
        }
        self.events.append(event)
        if len(self.events) > 100:  # 保持最近100条记录
            self.events.pop(0)
    
    def simulate_data(self):
        """模拟环境数据"""
        while self.running:
            # 只有在模拟模式下才更新模拟数据
            if self.data_mode == 'simulation':
                # 温度模拟
                base_temp = 25 + math.sin(time.time() / 60) * 3
                self.data['temperature'] = base_temp + random.uniform(-1, 1)
                
                # 湿度模拟
                base_humidity = 60 + math.cos(time.time() / 80) * 10
                self.data['humidity'] = max(0, min(100, base_humidity + random.uniform(-2, 2)))
                
                # CO2模拟
                base_co2 = 450 + math.sin(time.time() / 120) * 200
                self.data['co2'] = max(300, base_co2 + random.uniform(-20, 20))
                
                # 光照强度模拟
                base_light = 400 + math.sin(time.time() / 150) * 200
                self.data['light'] = max(50, base_light + random.uniform(-30, 30))
                
                # 烟雾模拟
                self.data['smoke'] = max(0, random.uniform(0, 10))
            
            # 自动控制逻辑（无论哪种模式都执行）
            self.auto_control()
            
            # 发送实时数据
            data_to_send = self.data.copy()
            
            # 如果是串口模式但未连接，不发送数据更新（保持前端显示--）
            if self.data_mode == 'serial' and not self.serial_manager.connected:
                # 串口模式未连接时，发送特殊标记
                data_to_send = {
                    'temperature': None,
                    'humidity': None,
                    'co2': None,
                    'light': None,
                    'smoke': None
                }
            
            socketio.emit('data_update', {
                'data': data_to_send,
                'devices': self.device_states,
                'events': self.events[-5:],  # 最近5条事件
                'data_mode': self.data_mode,
                'serial_connected': self.serial_manager.connected
            })
            
            time.sleep(2)  # 每2秒更新一次
    
    def auto_control(self):
        """自动控制设备"""
        changes = []
        
        # 温度控制
        if self.data['temperature'] < self.thresholds['temperature']['min']:
            if not self.device_states['heating']:
                self.device_states['heating'] = True
                self.device_states['cooling'] = False
                changes.append("启动加热系统")
        elif self.data['temperature'] > self.thresholds['temperature']['max']:
            if not self.device_states['cooling']:
                self.device_states['cooling'] = True
                self.device_states['heating'] = False
                changes.append("启动制冷系统")
        else:
            if self.device_states['heating']:
                self.device_states['heating'] = False
                changes.append("关闭加热系统")
            if self.device_states['cooling']:
                self.device_states['cooling'] = False
                changes.append("关闭制冷系统")
        
        # 湿度控制
        if self.data['humidity'] < self.thresholds['humidity']['min']:
            if not self.device_states['humidify']:
                self.device_states['humidify'] = True
                self.device_states['dehumidify'] = False
                changes.append("启动加湿系统")
        elif self.data['humidity'] > self.thresholds['humidity']['max']:
            if not self.device_states['dehumidify']:
                self.device_states['dehumidify'] = True
                self.device_states['humidify'] = False
                changes.append("启动除湿系统")
        else:
            if self.device_states['humidify']:
                self.device_states['humidify'] = False
                changes.append("关闭加湿系统")
            if self.device_states['dehumidify']:
                self.device_states['dehumidify'] = False
                changes.append("关闭除湿系统")
        
        # CO2控制
        if self.data['co2'] > self.thresholds['co2']['max']:
            if not self.device_states['ventilation']:
                self.device_states['ventilation'] = True
                changes.append("启动通风系统")
        else:
            if self.device_states['ventilation']:
                self.device_states['ventilation'] = False
                changes.append("关闭通风系统")
        
        # 记录变化事件
        for change in changes:
            self.add_event("DEVICE", change, "INFO")
    
    def on_serial_data(self, cmd, payload):
        """处理串口接收到的数据"""
        try:
            if cmd == 0x01:  # 环境数据命令
                if len(payload) >= 20:  # 5个float值，每个4字节
                    import struct
                    values = struct.unpack('<5f', payload[:20])
                    self.data['temperature'] = values[0]
                    self.data['humidity'] = values[1]
                    self.data['co2'] = values[2]
                    self.data['pm25'] = values[3]
                    self.data['smoke'] = values[4]
                    
                    self.add_event("SERIAL", "接收到环境数据", "INFO")
                    
            elif cmd == 0x02:  # 设备状态命令
                if len(payload) >= 1:
                    device_byte = payload[0]
                    self.device_states['heating'] = bool(device_byte & 0x01)
                    self.device_states['cooling'] = bool(device_byte & 0x02)
                    self.device_states['humidify'] = bool(device_byte & 0x04)
                    self.device_states['dehumidify'] = bool(device_byte & 0x08)
                    self.device_states['ventilation'] = bool(device_byte & 0x10)
                    self.device_states['close_vent'] = bool(device_byte & 0x20)
                    
                    self.add_event("SERIAL", "接收到设备状态", "INFO")
                    
        except Exception as e:
            self.add_event("ERROR", f"串口数据解析错误: {str(e)}", "ERROR")
    
    def set_data_mode(self, mode):
        """设置数据模式"""
        if mode in ['serial', 'simulation']:
            old_mode = self.data_mode
            self.data_mode = mode
            self.use_simulation = (mode == 'simulation')
            
            if old_mode != mode:
                mode_name = "串口数据" if mode == 'serial' else "模拟数据"
                self.add_event("SYSTEM", f"数据模式切换到: {mode_name}", "INFO")
                
                # 如果切换到串口模式但未连接，给出提示
                if mode == 'serial' and not self.serial_manager.connected:
                    self.add_event("WARNING", "串口模式已启用，但串口未连接", "WARNING")
            
            return True, f"数据模式已切换到: {mode_name}"
        else:
            return False, "无效的数据模式"
    
    def get_data_mode(self):
        """获取当前数据模式"""
        return self.data_mode

    def send_device_command(self, device, state):
        """发送设备控制命令到串口"""
        if not self.serial_manager.connected:
            return False, "串口未连接"
        
        try:
            # 构建设备控制字节
            device_map = {
                'heating': 0x01,
                'cooling': 0x02,
                'humidify': 0x04,
                'dehumidify': 0x08,
                'ventilation': 0x10,
                'close_vent': 0x20
            }
            
            if device in device_map:
                # 获取当前所有设备状态
                current_state = 0
                for dev, is_on in self.device_states.items():
                    if dev == device:
                        is_on = state  # 使用新状态
                    if is_on and dev in device_map:
                        current_state |= device_map[dev]
                
                # 发送控制命令 (cmd=0x03, payload=设备状态字节)
                payload = bytes([current_state])
                success, msg = self.serial_manager.send_command(0x03, payload)
                
                if success:
                    self.add_event("SERIAL", f"发送设备控制命令: {device}={state}", "INFO")
                
                return success, msg
            else:
                return False, "未知设备"
                
        except Exception as e:
            return False, f"发送命令失败: {str(e)}"

# 全局环境数据实例
env_data = EnvironmentData()

@app.route('/')
def index():
    """主页面"""
    response = make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/data')
def get_data():
    """获取当前环境数据"""
    return jsonify({
        'data': env_data.data,
        'devices': env_data.device_states,
        'thresholds': env_data.thresholds,
        'events': env_data.events[-10:]  # 最近10条事件
    })

@app.route('/api/control', methods=['POST'])
def control_device():
    """设备控制接口"""
    data = request.get_json()
    device = data.get('device')
    action = data.get('action')  # 'on' 或 'off'
    
    if device in env_data.device_states:
        old_state = env_data.device_states[device]
        env_data.device_states[device] = (action == 'on')
        
        if old_state != env_data.device_states[device]:
            device_names = {
                "heating": "加热系统",
                "cooling": "制冷系统",
                "humidify": "加湿系统",
                "dehumidify": "除湿系统",
                "ventilation": "通风系统",
                "close_vent": "通风关闭"
            }
            device_name = device_names.get(device, device)
            action_text = "启动" if action == 'on' else "关闭"
            env_data.add_event("MANUAL", f"手动{action_text}{device_name}", "INFO")
        
        return jsonify({'success': True, 'device': device, 'state': env_data.device_states[device]})
    
    return jsonify({'success': False, 'error': 'Invalid device'})

@app.route('/api/threshold', methods=['POST'])
def update_threshold():
    """更新阈值设置"""
    data = request.get_json()
    sensor = data.get('sensor')
    threshold_type = data.get('type')  # 'min' 或 'max'
    value = data.get('value')
    
    if sensor in env_data.thresholds and threshold_type in ['min', 'max']:
        if threshold_type in env_data.thresholds[sensor]:
            env_data.thresholds[sensor][threshold_type] = float(value)
            env_data.add_event("SYSTEM", f"更新{sensor}阈值: {threshold_type}={value}", "INFO")
            return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Invalid threshold parameter'})

@app.route('/api/serial/ports')
def list_serial_ports():
    """获取可用串口列表"""
    try:
        ports = env_data.serial_manager.list_ports()
        print(f"Debug: Found {len(ports)} serial ports: {ports}")
        return jsonify({
            'success': True,
            'ports': ports,
            'serial_available': SERIAL_AVAILABLE
        })
    except Exception as e:
        print(f"Error in list_serial_ports: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'ports': [],
            'serial_available': SERIAL_AVAILABLE
        })

@app.route('/api/serial/connect', methods=['POST'])
def connect_serial():
    """连接串口"""
    data = request.get_json()
    port = data.get('port')
    baudrate = data.get('baudrate', 115200)
    
    success, message = env_data.serial_manager.connect(port, baudrate)
    
    if success:
        # 串口连接成功后，如果当前是串口模式，则切换到串口数据
        if env_data.data_mode == 'serial':
            env_data.use_simulation = False
        env_data.add_event("SERIAL", f"串口连接成功: {port}", "INFO")
    
    return jsonify({
        'success': success,
        'message': message,
        'connected': env_data.serial_manager.connected,
        'data_mode': env_data.data_mode
    })

@app.route('/api/serial/disconnect', methods=['POST'])
def disconnect_serial():
    """断开串口连接"""
    success, message = env_data.serial_manager.disconnect()
    
    if success:
        # 串口断开后，如果当前是串口模式，给出警告但不自动切换模式
        if env_data.data_mode == 'serial':
            env_data.add_event("WARNING", "串口已断开，但仍处于串口数据模式", "WARNING")
        env_data.add_event("SERIAL", "串口连接已断开", "INFO")
    
    return jsonify({
        'success': success,
        'message': message,
        'connected': env_data.serial_manager.connected,
        'data_mode': env_data.data_mode
    })

@app.route('/api/serial/status')
def serial_status():
    """获取串口连接状态"""
    return jsonify({
        'connected': env_data.serial_manager.connected,
        'use_simulation': env_data.use_simulation,
        'serial_available': SERIAL_AVAILABLE,
        'data_mode': env_data.data_mode
    })

@app.route('/api/data/mode', methods=['GET', 'POST'])
def data_mode():
    """数据模式管理"""
    if request.method == 'GET':
        # 获取当前数据模式
        return jsonify({
            'success': True,
            'mode': env_data.get_data_mode(),
            'serial_connected': env_data.serial_manager.connected,
            'serial_available': SERIAL_AVAILABLE
        })
    
    elif request.method == 'POST':
        # 设置数据模式
        data = request.get_json()
        mode = data.get('mode')
        
        success, message = env_data.set_data_mode(mode)
        
        return jsonify({
            'success': success,
            'message': message,
            'mode': env_data.get_data_mode(),
            'serial_connected': env_data.serial_manager.connected
        })

@socketio.on('connect')
def handle_connect():
    """WebSocket连接处理"""
    print('Client connected')
    emit('connected', {'data': 'Connected to Environment Control System'})

@socketio.on('disconnect')
def handle_disconnect():
    """WebSocket断开处理"""
    print('Client disconnected')

if __name__ == '__main__':
    # 启动数据模拟线程
    data_thread = threading.Thread(target=env_data.simulate_data)
    data_thread.daemon = True
    data_thread.start()
    
    # 添加初始事件
    env_data.add_event("SYSTEM", "智能环境控制系统启动", "SYSTEM")
    env_data.add_event("SYSTEM", "开始环境数据监控", "INFO")
    
    # 启动Flask应用 - 固定端口用于比赛演示
    print("=" * 60)
    print("🏠 智能环境控制系统 - 比赛演示版")
    print("=" * 60)
    print("📱 本地访问地址: http://127.0.0.1:5000")
    print("🌐 局域网访问地址: http://192.168.1.19:5000")
    print("🔗 固定演示地址: http://localhost:5000")
    print("=" * 60)
    print("💡 比赛时可使用以上任一地址进行演示")
    print("🚀 系统启动中...")
    print("=" * 60)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)