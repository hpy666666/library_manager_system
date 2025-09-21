#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能环境控制系统 - 云端部署版本
支持公网访问和二维码生成
"""

from flask import Flask, render_template, jsonify, request, make_response
from flask_socketio import SocketIO, emit
import threading
import time
import random
import math
from datetime import datetime
import json
import qrcode
import io
import base64
import socket

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
                ports.append(port.device)
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

def get_local_ip():
    """获取本机IP地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def generate_qr_code(url):
    """生成二维码"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # 转换为base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()
        
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        print(f"QR Code generation error: {e}")
        return None

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
        self.use_simulation = True
        self.data_mode = 'simulation'
        
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
        if len(self.events) > 100:
            self.events.pop(0)
    
    def simulate_data(self):
        """模拟环境数据"""
        while self.running:
            if self.data_mode == 'simulation':
                base_temp = 25 + math.sin(time.time() / 60) * 3
                self.data['temperature'] = base_temp + random.uniform(-1, 1)
                
                base_humidity = 60 + math.cos(time.time() / 80) * 10
                self.data['humidity'] = max(0, min(100, base_humidity + random.uniform(-2, 2)))
                
                base_co2 = 450 + math.sin(time.time() / 120) * 200
                self.data['co2'] = max(300, base_co2 + random.uniform(-20, 20))
                
                base_light = 400 + math.sin(time.time() / 150) * 200
                self.data['light'] = max(50, base_light + random.uniform(-30, 30))
                
                self.data['smoke'] = max(0, random.uniform(0, 10))
            
            self.auto_control()
            
            data_to_send = self.data.copy()
            
            if self.data_mode == 'serial' and not self.serial_manager.connected:
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
                'events': self.events[-5:],
                'data_mode': self.data_mode,
                'serial_connected': self.serial_manager.connected
            })
            
            time.sleep(2)
    
    def auto_control(self):
        """自动控制设备"""
        changes = []
        
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
        
        if self.data['co2'] > self.thresholds['co2']['max']:
            if not self.device_states['ventilation']:
                self.device_states['ventilation'] = True
                changes.append("启动通风系统")
        else:
            if self.device_states['ventilation']:
                self.device_states['ventilation'] = False
                changes.append("关闭通风系统")
        
        for change in changes:
            self.add_event("DEVICE", change, "INFO")
    
    def on_serial_data(self, cmd, payload):
        """处理串口接收到的数据"""
        try:
            if cmd == 0x01:
                if len(payload) >= 20:
                    import struct
                    values = struct.unpack('<5f', payload[:20])
                    self.data['temperature'] = values[0]
                    self.data['humidity'] = values[1]
                    self.data['co2'] = values[2]
                    self.data['pm25'] = values[3]
                    self.data['smoke'] = values[4]
                    
                    self.add_event("SERIAL", "接收到环境数据", "INFO")
                    
            elif cmd == 0x02:
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
            device_map = {
                'heating': 0x01,
                'cooling': 0x02,
                'humidify': 0x04,
                'dehumidify': 0x08,
                'ventilation': 0x10,
                'close_vent': 0x20
            }
            
            if device in device_map:
                current_state = 0
                for dev, is_on in self.device_states.items():
                    if dev == device:
                        is_on = state
                    if is_on and dev in device_map:
                        current_state |= device_map[dev]
                
                payload = bytes([current_state])
                success, msg = self.serial_manager.send_command(0x03, payload)
                
                if success:
                    self.add_event("SERIAL", f"发送设备控制命令: {device}={state}", "INFO")
                
                return success, msg
            else:
                return False, "未知设备"
                
        except Exception as e:
            return False, f"发送命令失败: {str(e)}"

env_data = EnvironmentData()

@app.route('/')
def index():
    """主页面"""
    response = make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/qr')
def qr_page():
    """二维码页面"""
    local_ip = get_local_ip()
    port = 5000
    
    # 生成访问地址
    local_url = f"http://localhost:{port}"
    network_url = f"http://{local_ip}:{port}"
    
    # 生成二维码
    qr_code_local = generate_qr_code(local_url)
    qr_code_network = generate_qr_code(network_url)
    
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>智能环境控制系统 - 访问地址</title>
        <style>
            body {{
                font-family: 'Microsoft YaHei UI', Arial, sans-serif;
                background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                color: #ecf0f1;
                margin: 0;
                padding: 20px;
                min-height: 100vh;
            }}
            .container {{
                max-width: 800px;
                margin: 0 auto;
                text-align: center;
            }}
            h1 {{
                color: #3282b8;
                margin-bottom: 30px;
            }}
            .qr-section {{
                display: flex;
                justify-content: space-around;
                flex-wrap: wrap;
                gap: 30px;
                margin: 30px 0;
            }}
            .qr-card {{
                background: rgba(22, 33, 62, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 16px;
                padding: 20px;
                min-width: 300px;
            }}
            .qr-code {{
                margin: 20px 0;
            }}
            .qr-code img {{
                max-width: 200px;
                border-radius: 8px;
            }}
            .url {{
                background: rgba(255, 255, 255, 0.1);
                padding: 10px;
                border-radius: 8px;
                font-family: monospace;
                word-break: break-all;
                margin: 10px 0;
            }}
            .btn {{
                display: inline-block;
                padding: 12px 24px;
                background: linear-gradient(135deg, #3282b8, #0f4c75);
                color: white;
                text-decoration: none;
                border-radius: 8px;
                margin: 10px;
                transition: all 0.3s ease;
            }}
            .btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(50, 130, 184, 0.4);
            }}
            .info {{
                background: rgba(52, 152, 219, 0.2);
                border: 1px solid rgba(52, 152, 219, 0.3);
                border-radius: 8px;
                padding: 15px;
                margin: 20px 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🏠 智能环境控制系统</h1>
            <h2>📱 访问地址与二维码</h2>
            
            <div class="qr-section">
                <div class="qr-card">
                    <h3>💻 本地访问</h3>
                    <div class="url">{local_url}</div>
                    <div class="qr-code">
                        <img src="{qr_code_local}" alt="本地访问二维码">
                    </div>
                    <a href="{local_url}" class="btn" target="_blank">直接访问</a>
                </div>
                
                <div class="qr-card">
                    <h3>🌐 网络访问</h3>
                    <div class="url">{network_url}</div>
                    <div class="qr-code">
                        <img src="{qr_code_network}" alt="网络访问二维码">
                    </div>
                    <a href="{network_url}" class="btn" target="_blank">直接访问</a>
                </div>
            </div>
            
            <div class="info">
                <h3>📋 使用说明</h3>
                <p><strong>本地访问</strong>: 在本机浏览器中使用</p>
                <p><strong>网络访问</strong>: 手机扫描二维码或其他设备访问</p>
                <p><strong>比赛演示</strong>: 推荐使用本地访问地址</p>
            </div>
            
            <a href="/" class="btn">🚀 进入系统</a>
        </div>
    </body>
    </html>
    """

@app.route('/api/data')
def get_data():
    """获取当前环境数据"""
    return jsonify({
        'data': env_data.data,
        'devices': env_data.device_states,
        'thresholds': env_data.thresholds,
        'events': env_data.events[-10:]
    })

@app.route('/api/control', methods=['POST'])
def control_device():
    """设备控制接口"""
    data = request.get_json()
    device = data.get('device')
    action = data.get('action')
    
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
    threshold_type = data.get('type')
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
        return jsonify({
            'success': True,
            'ports': ports,
            'serial_available': SERIAL_AVAILABLE
        })
    except Exception as e:
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
        return jsonify({
            'success': True,
            'mode': env_data.get_data_mode(),
            'serial_connected': env_data.serial_manager.connected,
            'serial_available': SERIAL_AVAILABLE
        })
    
    elif request.method == 'POST':
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
    # 获取网络信息
    local_ip = get_local_ip()
    port = 5000
    
    # 启动数据模拟线程
    data_thread = threading.Thread(target=env_data.simulate_data)
    data_thread.daemon = True
    data_thread.start()
    
    # 添加初始事件
    env_data.add_event("SYSTEM", "智能环境控制系统启动", "SYSTEM")
    env_data.add_event("SYSTEM", "开始环境数据监控", "INFO")
    
    # 显示启动信息
    print("=" * 80)
    print("🏠 智能环境控制系统 - 云端部署版")
    print("=" * 80)
    print(f"📱 本地访问地址: http://localhost:{port}")
    print(f"🌐 网络访问地址: http://{local_ip}:{port}")
    print(f"📋 二维码页面: http://localhost:{port}/qr")
    print("=" * 80)
    print("💡 比赛演示建议:")
    print("   1. 访问 /qr 页面获取二维码")
    print("   2. 手机扫描二维码进行移动端演示")
    print("   3. 使用本地地址进行主要演示")
    print("=" * 80)
    print("🚀 系统启动中...")
    print("=" * 80)
    
    # 启动Flask应用
    socketio.run(app, host='0.0.0.0', port=port, debug=False)