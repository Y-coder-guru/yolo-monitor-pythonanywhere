# YOLOv5 Flask 监控管理系统（前端展示 + 模型接口预留）

## 1. 功能概览
- 用户注册 / 登录 / 持久化记忆（SQLite）
- 登录保护：未登录不可访问监控系统
- 5 大页面：登录、实时监控、数据统计、历史记录、管理员
- 三种采集来源：电脑内置摄像头、USB 外接摄像头、串口设备通信模块
- 多账号采集隔离：每个账号独立控制自己的摄像头、检测状态、参数和使用时长
- 串口设备独占：同一时间只允许一个账号占用，其他账号只能等待设备释放
- 图片上传检测与检测框交互
- 坑状缺陷、条状缺陷智能研判与处置建议
- YOLO 检测接口预留（当前返回模拟数据）
- 实时动态折线图、饼图、柱状图 + 今日统计卡片
- 历史记录按时间倒序、关键词筛选、时间区间筛选、分页
- 管理员查看用户、系统状态、日志、删除历史记录

## 2. 项目结构
```bash
GUI_YOLOv5/
├── app.py
├── requirements.txt
├── README.md
├── instance/
│   └── yolo_monitor.db            # 自动创建
├── models/
│   └── best.pt                    # 默认加载的模型文件
├── static/
│   ├── css/style.css
│   └── js/
│       ├── monitor.js
│       ├── stats.js
│       ├── history.js
│       └── admin.js
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── monitor.html
    ├── stats.html
    ├── history.html
    └── admin.html
```

## 3. 安装与启动
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

如果使用 Conda 环境：

```bash
conda activate py310_yolov5
python -m pip install "openvino>=2024.6,<2027"
python app.py
```

启动后控制台会显示：
- `http://127.0.0.1:5000`
- `http://0.0.0.0:5000`
- `http://你的局域网IP:5000`（同一 WiFi 手机/平板可访问）

## 4. 默认管理员账号
- 用户名：`rtxq`
- 密码：`rtxq123456`

## 5. OpenVINO 模型与实时检测

当前项目默认优先加载：

```text
models/best_openvino_model/
├── best.xml
├── best.bin
└── metadata.yaml
```

如果 OpenVINO 目录不存在，会自动回退到 `models/best.pt`。也可以通过环境变量
`YOLO_MODEL_PATH` 指定其他模型路径。

重新训练并替换 `models/best.pt` 后，执行：

```bash
python export_openvino.py
```

默认导出 640×640、静态 batch=1、FP16 压缩的 OpenVINO IR 模型。需要 FP32 时：

```bash
python export_openvino.py --fp32
```

启动应用后，服务会提前加载并预热 OpenVINO 模型。网页端打开摄像头并点击“开始检测”后，
会将缩放后的当前帧发送到 Flask 后端推理，前端自动绘制检测框、类别、置信度和数量。

左侧“实时监控”菜单包含两个模式：

- 实时检测：选择电脑内置或 USB 外接摄像头进行连续检测
- 图片检测：上传单张图片，点选检测区域并生成智能研判报告

实时检测页支持三种采集来源：

- 电脑内置摄像头：通过浏览器 `getUserMedia` 采集
- USB 外接摄像头：自动从浏览器视频设备中筛选外接设备
- 设备通信模块：通过串口连接单片机、工控机或视觉模块，可发送文本控制指令并接收 JPEG 图像

内置摄像头和 USB 摄像头的运行状态、检测开关与参数按登录账号隔离。一个账号关闭摄像头、
停止检测或退出登录，不会影响其他账号。YOLO 模型由服务端共享，并通过推理锁避免多个请求
同时操作模型。串口属于服务器上的实体设备，因此采用账号独占方式；占用者断开或退出后，
其他账号才能连接。

串口图像协议采用标准 JPEG 帧边界：设备直接发送从 `FF D8` 开始、以 `FF D9`
结束的完整 JPEG 二进制数据。控制指令由服务端以 UTF-8 文本加换行符发送。
传输图像时建议使用 `460800` 或 `921600` 波特率。

可选环境变量：

- `YOLO_MODEL_PATH`：模型目录或权重文件路径
- `YOLO_IMGSZ`：推理输入尺寸，默认 `640`
- `YOLO_CONF`：置信度阈值，默认 `0.25`
- `YOLO_IOU`：NMS IoU 阈值，默认 `0.45`
- `YOLO_WARMUP=0`：关闭启动时预热
- `DETECTION_RECORD_INTERVAL_SECONDS`：实时检测历史写库间隔，默认 `5.0` 秒
- `DETECTION_IMAGE_JPEG_QUALITY`：历史缩略图 JPEG 质量，默认 `40`
- `DETECTION_IMAGE_MAX_SIZE`：历史缩略图最长边尺寸，默认 `320` 像素，并保持原始宽高比例
- `DETECTION_IMAGE_MIN_CONFIDENCE`：保存历史图片的最低平均置信度，默认 `0.65`；低于该值只保存数据

智能研判接口使用服务端环境变量，密钥不会发送到浏览器：

```powershell
$env:INTELLIGENCE_API_KEY="你的 API Key"
$env:INTELLIGENCE_API_BASE="https://api.deepseek.com"
$env:INTELLIGENCE_MODEL="deepseek-v4-flash"
python app.py
```

未配置 API Key 时，系统会自动使用内置缺陷规则库生成基础研判，不影响图片检测功能。

## 6. 数据记忆说明
SQLite 文件：`instance/yolo_monitor.db`
永久存储：
- 用户账号密码（哈希）
- 每个账号的摄像头参数与每日使用时长
- 检测时间、类别、数量、置信度
- 历史记录
- 系统日志

重启电脑后数据仍保留。

## 7. 前后端对接
- `POST /api/camera/start` 开启摄像头状态
- `POST /api/camera/stop` 关闭摄像头状态
- `POST /api/detection/start` 开始检测状态
- `POST /api/detection/stop` 停止检测状态
- `POST /api/detection/frame-data` 使用 OpenVINO 获取本帧真实检测结果
- `POST /api/image-detection` 上传图片并返回检测框
- `POST /api/intelligence/analyze` 对全部或选中的检测框生成智能研判
- `GET /api/stats/live` 获取动态图表数据
- `GET /api/history` 获取历史记录（支持筛选+分页）
- `GET /api/admin/overview` 获取管理员总览
- `DELETE /api/admin/history/<id>` 删除单条历史
