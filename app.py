from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import socket
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
    has_request_context,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

try:
    from openvino import Core as OpenVINOCore
except ImportError:  # pragma: no cover
    OpenVINOCore = None
try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "yolo_monitor.db"
DETECTION_IMAGE_DIR = BASE_DIR / "instance" / "detection_images"


def resolve_database_uri() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        # Render/Heroku 历史上可能提供 postgres:// 前缀，SQLAlchemy 2 需要 postgresql://
        if database_url.startswith("postgres://"):
            return database_url.replace("postgres://", "postgresql://", 1)
        return database_url
    return f"sqlite:///{DB_PATH}"


DB_URI = resolve_database_uri()
IS_SQLITE = DB_URI.startswith("sqlite:")
AUTO_LOGIN_COOKIE = "auto_login_opt_in"

REMEMBER_COOKIE_NAME = os.getenv("REMEMBER_COOKIE_NAME", "remember_token")
ACTIVE_SESSION_TIMEOUT_SECONDS = int(os.getenv("ACTIVE_SESSION_TIMEOUT_SECONDS", "5"))
ACTIVE_SESSION_HEARTBEAT_SECONDS = int(os.getenv("ACTIVE_SESSION_HEARTBEAT_SECONDS", "2"))
DETECTION_RECORD_INTERVAL_SECONDS = float(os.getenv("DETECTION_RECORD_INTERVAL_SECONDS", "5.0"))
DETECTION_IMAGE_JPEG_QUALITY = min(
    max(int(os.getenv("DETECTION_IMAGE_JPEG_QUALITY", "40")), 20),
    95,
)
DETECTION_IMAGE_MAX_SIZE = max(
    int(os.getenv("DETECTION_IMAGE_MAX_SIZE", "320")),
    160,
)
DETECTION_IMAGE_MIN_CONFIDENCE = min(
    max(float(os.getenv("DETECTION_IMAGE_MIN_CONFIDENCE", "0.65")), 0.0),
    1.0,
)
MAX_IMAGE_UPLOAD_BYTES = int(os.getenv("MAX_IMAGE_UPLOAD_BYTES", str(12 * 1024 * 1024)))
INTELLIGENCE_API_KEY = (
    os.getenv("INTELLIGENCE_API_KEY", "").strip()
    or os.getenv("DEEPSEEK_API_KEY", "").strip()
)
INTELLIGENCE_API_BASE = os.getenv("INTELLIGENCE_API_BASE", "https://api.deepseek.com").rstrip("/")
INTELLIGENCE_MODEL = os.getenv("INTELLIGENCE_MODEL", "deepseek-v4-flash").strip()
INTELLIGENCE_TIMEOUT_SECONDS = float(os.getenv("INTELLIGENCE_TIMEOUT_SECONDS", "25"))


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "replace-this-with-a-long-random-secret"),
    SQLALCHEMY_DATABASE_URI=DB_URI,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={
        "pool_pre_ping": True,
        # Render 免费实例连接数有限，保持小池并及时回收，防止 QueuePool 耗尽。
        "pool_recycle": int(os.getenv("SQLALCHEMY_POOL_RECYCLE", "180")),
        "pool_size": int(os.getenv("SQLALCHEMY_POOL_SIZE", "2")),
        "max_overflow": int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "3")),
        "pool_timeout": int(os.getenv("SQLALCHEMY_POOL_TIMEOUT", "15")),
        "pool_use_lifo": True,
    }
    if not IS_SQLITE
    else {},
    REMEMBER_COOKIE_DURATION=timedelta(days=180),
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=MAX_IMAGE_UPLOAD_BYTES,
)
app.permanent_session_lifetime = timedelta(days=180)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录后再访问监控系统。"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active_account = db.Column(db.Boolean, default=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    email = db.Column(db.String(120), nullable=False, default="")
    phone = db.Column(db.String(30), nullable=False, default="")
    avatar_url = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_logged_in = db.Column(db.Boolean, nullable=False, default=False)
    active_session_token = db.Column(db.String(128), nullable=True)


class DetectionRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    detect_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    category = db.Column(db.String(50), nullable=False, index=True)
    count = db.Column(db.Integer, nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    operator_name = db.Column(db.String(50), nullable=False, default="system")
    operation_type = db.Column(db.String(50), nullable=False, default="自动检测")
    note = db.Column(db.String(255), nullable=False, default="")
    image_path = db.Column(db.String(255), nullable=False, default="")
    boxes_json = db.Column(db.Text, nullable=False, default="[]")
    frame_width = db.Column(db.Integer, nullable=False, default=0)
    frame_height = db.Column(db.Integer, nullable=False, default=0)


class SystemLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    operator = db.Column(db.String(50), nullable=False, default="system")
    ip = db.Column(db.String(64), nullable=False, default="-")
    result = db.Column(db.String(20), nullable=False, default="成功")
    log_type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class SystemConfig(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserCameraConfig(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    resolution = db.Column(db.String(10), nullable=False, default="720P")
    fps = db.Column(db.Integer, nullable=False, default=20)
    flip_horizontal = db.Column(db.Boolean, nullable=False, default=False)
    flip_vertical = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyDetectionDuration(db.Model):
    __tablename__ = "user_daily_detection_duration"

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    day = db.Column(db.String(10), primary_key=True)  # YYYY-MM-DD
    seconds = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


class YoloModelService:
    """
    直接使用 OpenVINO IR 模型推理，避免安装体积很大的 PyTorch 和 Ultralytics。
    返回格式与原程序一致，前端和数据库无需改动。

    返回格式:
    {
      "boxes": [
        {"x": 120, "y": 90, "w": 160, "h": 180, "label": "person", "conf": 0.87}
      ],
      "counts": {"person": 2, "car": 1}
    }
    """

    def __init__(self):
        self.lock = threading.RLock()
        custom_path = os.getenv("YOLO_MODEL_PATH", "").strip()
        custom_model_path = Path(custom_path) if custom_path else None
        if custom_model_path and not custom_model_path.is_absolute():
            custom_model_path = BASE_DIR / custom_model_path
        self.model_path = custom_model_path or self._default_model_path()
        self.model = None
        self.input_layer = None
        self.output_layer = None
        self.class_names = {}
        self.model_name = self.model_path.name
        self.backend = "OpenVINO IR"
        self.imgsz = int(os.getenv("YOLO_IMGSZ", "640"))
        self.conf = float(os.getenv("YOLO_CONF", "0.25"))
        self.iou = float(os.getenv("YOLO_IOU", "0.45"))
        self.warmup_ms = 0.0
        self.last_error = ""
        self.last_reload_attempt_ts = 0.0
        self.reload()

    @staticmethod
    def _default_model_path() -> Path:
        return BASE_DIR / "models" / "best_openvino_model"

    def _model_files(self) -> tuple[Path, Path]:
        if self.model_path.is_dir():
            xml_files = sorted(self.model_path.glob("*.xml"))
            if not xml_files:
                raise FileNotFoundError("OpenVINO 模型目录中没有 XML 文件")
            return xml_files[0], self.model_path / "metadata.yaml"
        return self.model_path, self.model_path.with_name("metadata.yaml")

    @staticmethod
    def _load_class_names(metadata_path: Path) -> dict[int, str]:
        if not metadata_path.exists():
            return {0: "Fissure", 1: "Crater"}
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
        names = metadata.get("names") or {}
        if isinstance(names, list):
            return {index: str(name) for index, name in enumerate(names)}
        return {int(index): str(name) for index, name in names.items()}

    def reload(self) -> tuple[bool, str]:
        with self.lock:
            self.model = None
            self.input_layer = None
            self.output_layer = None
            self.model_name = self.model_path.name
            self.warmup_ms = 0.0
            self.last_error = ""
            self.last_reload_attempt_ts = time.time()
            if OpenVINOCore is None:
                self.last_error = "未安装 openvino，无法加载检测模型"
                return False, self.last_error
            if not self.model_path.exists():
                self.last_error = f"模型文件不存在：{self.model_path.name}"
                return False, self.last_error
            try:
                xml_path, metadata_path = self._model_files()
                core = OpenVINOCore()
                openvino_model = core.read_model(model=str(xml_path))
                self.model = core.compile_model(openvino_model, "CPU")
                self.input_layer = self.model.input(0)
                self.output_layer = self.model.output(0)
                self.class_names = self._load_class_names(metadata_path)
                input_shape = list(self.input_layer.shape)
                if len(input_shape) == 4 and int(input_shape[2]) > 0:
                    self.imgsz = int(input_shape[2])
                if os.getenv("YOLO_WARMUP", "1") != "0":
                    warmup_start = time.perf_counter()
                    tensor = np.zeros((1, 3, self.imgsz, self.imgsz), dtype=np.float32)
                    self.model([tensor])[self.output_layer]
                    self.warmup_ms = round((time.perf_counter() - warmup_start) * 1000, 2)
                return True, f"模型已加载：{self.model_path.name}（{self.backend}）"
            except Exception as exc:
                self.last_error = f"模型加载失败：{exc}"
                self.model = None
                return False, self.last_error

    def _prepare_input(self, source: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        height, width = source.shape[:2]
        ratio = min(self.imgsz / width, self.imgsz / height)
        resized_width = max(1, int(round(width * ratio)))
        resized_height = max(1, int(round(height * ratio)))
        rgb_source = Image.fromarray(source[:, :, ::-1])
        resized_rgb = rgb_source.resize(
            (resized_width, resized_height),
            Image.Resampling.BILINEAR,
        )
        resized = np.asarray(resized_rgb)[:, :, ::-1]
        pad_x = (self.imgsz - resized_width) // 2
        pad_y = (self.imgsz - resized_height) // 2
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        canvas[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = resized
        tensor = canvas[:, :, ::-1].transpose(2, 0, 1)
        tensor = np.ascontiguousarray(tensor, dtype=np.float32) / 255.0
        return tensor[None], ratio, pad_x, pad_y

    @staticmethod
    def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
        if len(boxes) == 0:
            return []
        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 0] + boxes[:, 2]
        y2 = boxes[:, 1] + boxes[:, 3]
        areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
        order = scores.argsort()[::-1]
        selected = []
        while order.size:
            current = int(order[0])
            selected.append(current)
            if order.size == 1:
                break
            remaining = order[1:]
            inter_x1 = np.maximum(x1[current], x1[remaining])
            inter_y1 = np.maximum(y1[current], y1[remaining])
            inter_x2 = np.minimum(x2[current], x2[remaining])
            inter_y2 = np.minimum(y2[current], y2[remaining])
            inter_area = np.maximum(0, inter_x2 - inter_x1) * np.maximum(0, inter_y2 - inter_y1)
            union = areas[current] + areas[remaining] - inter_area
            iou = np.divide(inter_area, union, out=np.zeros_like(inter_area), where=union > 0)
            order = remaining[iou <= iou_threshold]
        return selected

    def _decode_output(
        self,
        prediction: np.ndarray,
        ratio: float,
        pad_x: int,
        pad_y: int,
        frame_width: int,
        frame_height: int,
    ) -> list[dict]:
        rows = np.squeeze(prediction)
        if rows.ndim != 2:
            raise ValueError(f"模型输出形状不受支持：{prediction.shape}")
        if rows.shape[0] < rows.shape[1] and rows.shape[0] <= 256:
            rows = rows.T

        class_count = len(self.class_names)
        if class_count <= 0 or rows.shape[1] < 4 + class_count:
            raise ValueError(f"模型输出类别数不匹配：{prediction.shape}")
        class_scores = rows[:, -class_count:]
        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(len(rows)), class_ids]
        candidate_indices = np.flatnonzero(confidences >= self.conf)
        if candidate_indices.size == 0:
            return []

        nms_boxes = []
        nms_scores = []
        kept_rows = []
        for index in candidate_indices:
            center_x, center_y, box_width, box_height = rows[index, :4]
            x = float(center_x - box_width / 2)
            y = float(center_y - box_height / 2)
            nms_boxes.append([x, y, float(box_width), float(box_height)])
            nms_scores.append(float(confidences[index]))
            kept_rows.append(int(index))

        selected = []
        for class_id in sorted(set(int(class_ids[index]) for index in kept_rows)):
            positions = [
                pos for pos, row_index in enumerate(kept_rows)
                if int(class_ids[row_index]) == class_id
            ]
            indices = self._nms(
                np.asarray([nms_boxes[pos] for pos in positions], dtype=np.float32),
                np.asarray([nms_scores[pos] for pos in positions], dtype=np.float32),
                self.iou,
            )
            for local_index in indices:
                selected.append(positions[int(local_index)])

        boxes = []
        for position in selected:
            x, y, box_width, box_height = nms_boxes[position]
            row_index = kept_rows[position]
            x1 = max(0, min(frame_width, int(round((x - pad_x) / ratio))))
            y1 = max(0, min(frame_height, int(round((y - pad_y) / ratio))))
            x2 = max(0, min(frame_width, int(round((x + box_width - pad_x) / ratio))))
            y2 = max(0, min(frame_height, int(round((y + box_height - pad_y) / ratio))))
            if x2 <= x1 or y2 <= y1:
                continue
            class_id = int(class_ids[row_index])
            boxes.append({
                "x": x1,
                "y": y1,
                "w": x2 - x1,
                "h": y2 - y1,
                "label": self.class_names.get(class_id, str(class_id)),
                "conf": round(float(confidences[row_index]), 2),
            })
        return boxes

    def ensure_loaded(self, cooldown_sec: float = 5.0) -> bool:
        with self.lock:
            if self.model is not None:
                return True
            now = time.time()
            if now - self.last_reload_attempt_ts < cooldown_sec:
                return False
            ok, _ = self.reload()
            return ok

    def predict_from_frame(self, frame_meta: dict | None = None) -> dict:
        with self.lock:
            if self.model:
                source = frame_meta.get("frame_array") if frame_meta else None
                if source is None:
                    return {"boxes": [], "counts": {}, "warning": "未收到可用视频帧，无法进行推理"}
                height, width = source.shape[:2]
                tensor, ratio, pad_x, pad_y = self._prepare_input(source)
                prediction = self.model([tensor])[self.output_layer]
                boxes = self._decode_output(prediction, ratio, pad_x, pad_y, width, height)
                counts = Counter(box["label"] for box in boxes)
                return {
                    "boxes": boxes,
                    "counts": dict(counts),
                    "frame_width": int(width),
                    "frame_height": int(height),
                    "backend": self.backend,
                }
            return {"boxes": [], "counts": {}, "warning": self.last_error or "模型未加载，无法推理"}


model_service = YoloModelService()

DEFECT_KNOWLEDGE = {
    "Crater": {
        "name": "坑状缺陷",
        "explanation": "表面出现局部凹坑、孔洞或冲击状损伤，可能与材料剥落、腐蚀、碰撞或成型不良有关。",
        "solutions": {
            "low": [
                "当前识别可信度较低，暂不直接判定为坑状缺陷。",
                "清洁表面污渍和反光区域，调整光照与拍摄角度后重新检测。",
                "若复拍后置信度仍低且现场肉眼无明显凹陷，可列入常规观察记录。",
            ],
            "medium": [
                "将该区域标记为疑似坑状缺陷，并安排一次近距离人工复核。",
                "使用直尺、深度尺或侧光观察确认凹坑边界和大致深度。",
                "复核前不建议直接维修；确认后再决定清理、填补或继续观察。",
            ],
            "high": [
                "该区域较大概率为坑状缺陷，应纳入近期处置清单。",
                "测量直径、深度和边缘剥落范围，并检查周边是否存在腐蚀扩展。",
                "浅表缺陷可清理、打磨、填补并恢复防护层；较深缺陷应进行补强评估。",
            ],
            "critical": [
                "该区域高度疑似坑状缺陷，建议优先隔离标记并尽快人工确认。",
                "立即测量深度、剩余厚度及周边材料完整性，必要时采用无损检测。",
                "若位于受力部位、持续剥落或深度超限，应暂停相关区域使用并评估补强或更换。",
            ],
        },
    },
    "Fissure": {
        "name": "条状缺陷",
        "explanation": "表面出现细长裂纹或条带状异常，可能与疲劳、应力集中、材料开裂或施工接缝异常有关。",
        "solutions": {
            "low": [
                "当前识别可信度较低，可能受到划痕、阴影、接缝或纹理干扰。",
                "擦拭表面并采用垂直光和侧光分别复拍，确认条带是否真实存在。",
                "若多角度复拍均未重复出现，可作为误检记录，不立即采取维修措施。",
            ],
            "medium": [
                "将该区域列为疑似条状缺陷，沿走向两端扩大拍摄范围。",
                "使用放大观察或着色标记确认长度、宽度及端部是否继续延伸。",
                "在人工确认前保持重点观察，避免在该区域进行冲击或额外加载。",
            ],
            "high": [
                "该区域较大概率为条状缺陷，应尽快进行人工复核和长度测量。",
                "建议采用渗透检测、放大检查或其他适用的无损检测方法确认裂纹性质。",
                "若确认是表面裂纹，应进行止裂、修补或专项维修，并建立复查周期。",
            ],
            "critical": [
                "该区域高度疑似条状裂纹，存在继续扩展的可能，应优先处置。",
                "立即标记裂纹两端，检查是否位于焊缝、连接处或主要受力区域。",
                "受力区域应限制使用并组织专项检测；确认扩展或贯穿时，应制定补强、更换或停用方案。",
            ],
        },
    },
}

CONFIDENCE_BANDS = (
    (0.40, "low", "低可信提示", "待复核"),
    (0.65, "medium", "疑似缺陷", "低"),
    (0.85, "high", "较高可信", "中"),
    (1.01, "critical", "高可信缺陷", "高"),
)


def confidence_profile(confidence: float) -> tuple[str, str, str, str]:
    lower_bound = 0.0
    for upper_bound, key, title, risk_level in CONFIDENCE_BANDS:
        if confidence < upper_bound:
            display_upper = min(upper_bound, 1.0)
            interval = f"{lower_bound * 100:.0f}%–{display_upper * 100:.0f}%"
            return key, title, risk_level, interval
        lower_bound = upper_bound
    return "critical", "高可信缺陷", "高", "85%–100%"


def decode_image_bytes(raw: bytes):
    if not raw:
        return None
    if Image is not None:
        try:
            from io import BytesIO

            rgb = np.array(Image.open(BytesIO(raw)).convert("RGB"))
            if rgb.size > 0:
                return rgb[:, :, ::-1].copy()
        except Exception:
            return None
    return None


def extract_jpeg_frame(buffer: bytearray) -> bytes:
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        if len(buffer) > 4 * 1024 * 1024:
            del buffer[:-2048]
        return b""
    end = buffer.find(b"\xff\xd9", start + 2)
    if end < 0:
        if start > 0:
            del buffer[:start]
        return b""
    frame = bytes(buffer[start : end + 2])
    del buffer[: end + 2]
    return frame


def extract_serial_text_messages(buffer: bytearray) -> list[str]:
    jpeg_start = buffer.find(b"\xff\xd8")
    text_end = jpeg_start if jpeg_start >= 0 else len(buffer)
    if text_end <= 0:
        return []
    prefix = bytes(buffer[:text_end])
    consume_to = max(prefix.rfind(b"\n"), prefix.rfind(b"\r"))
    if consume_to < 0:
        return []
    raw = bytes(buffer[: consume_to + 1])
    del buffer[: consume_to + 1]
    text = raw.decode("utf-8", errors="ignore")
    return [line.strip()[:500] for line in text.replace("\r", "\n").split("\n") if line.strip()]


def close_machine_serial(
    requesting_user_id: int | None = None,
    *,
    force: bool = False,
) -> tuple[bool, int | None]:
    """关闭串口；非占用者不能释放其他账号正在使用的设备。"""
    global machine_serial_conn, machine_serial_buffer
    global machine_serial_owner_user_id, machine_serial_owner_username
    global machine_last_frame_at, machine_last_rx_bytes, machine_last_messages

    with machine_serial_lock:
        owner_user_id = machine_serial_owner_user_id
        if owner_user_id is not None and not force and requesting_user_id != owner_user_id:
            return False, owner_user_id
        if machine_serial_conn:
            try:
                machine_serial_conn.close()
            except Exception:
                pass
        machine_serial_conn = None
        machine_serial_buffer = bytearray()
        machine_serial_owner_user_id = None
        machine_serial_owner_username = ""
        machine_last_frame_at = None
        machine_last_rx_bytes = 0
        machine_last_messages = []
        return True, owner_user_id


def save_detection_snapshot(frame: np.ndarray | None) -> str:
    if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
        return ""
    height, width = frame.shape[:2]
    longest_side = max(width, height)
    if longest_side > DETECTION_IMAGE_MAX_SIZE:
        scale = DETECTION_IMAGE_MAX_SIZE / longest_side
        thumbnail_size = (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
        if Image is not None:
            rgb_image = Image.fromarray(frame[:, :, ::-1])
            rgb_image.thumbnail(
                (DETECTION_IMAGE_MAX_SIZE, DETECTION_IMAGE_MAX_SIZE),
                Image.Resampling.LANCZOS,
            )
            frame = np.asarray(rgb_image)[:, :, ::-1]

    DETECTION_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}_{secrets.token_hex(4)}.jpg"
    target = DETECTION_IMAGE_DIR / filename
    if Image is not None:
        try:
            Image.fromarray(frame[:, :, ::-1]).save(
                target,
                format="JPEG",
                quality=DETECTION_IMAGE_JPEG_QUALITY,
            )
            return filename
        except Exception:
            pass
    return ""


def record_detection_result(
    result: dict,
    operation_type: str,
    note: str = "",
    frame: np.ndarray | None = None,
) -> None:
    boxes_json = json.dumps(result.get("boxes", []), ensure_ascii=False)
    frame_width = int(result.get("frame_width") or (frame.shape[1] if frame is not None else 0))
    frame_height = int(result.get("frame_height") or (frame.shape[0] if frame is not None else 0))
    category_confidences = {}
    for category, count in result.get("counts", {}).items():
        confidences = [
            float(box["conf"])
            for box in result.get("boxes", [])
            if box.get("label") == category
        ]
        category_confidences[category] = (
            sum(confidences) / len(confidences)
            if confidences
            else 0.0
        )

    should_save_image = any(
        confidence >= DETECTION_IMAGE_MIN_CONFIDENCE
        for confidence in category_confidences.values()
    )
    image_path = save_detection_snapshot(frame) if should_save_image else ""

    for category, count in result.get("counts", {}).items():
        if count <= 0:
            continue
        avg_conf = category_confidences.get(category, 0.0)
        category_image_path = (
            image_path
            if avg_conf >= DETECTION_IMAGE_MIN_CONFIDENCE
            else ""
        )
        db.session.add(
            DetectionRecord(
                user_id=current_user.id,
                category=category,
                count=count,
                confidence=round(avg_conf, 2),
                operator_name=current_user.username,
                operation_type=operation_type,
                note=note[:255],
                image_path=category_image_path,
                boxes_json=boxes_json,
                frame_width=frame_width,
                frame_height=frame_height,
            )
        )
    db.session.commit()


def delete_detection_image_if_unused(record: DetectionRecord) -> None:
    image_path = (record.image_path or "").strip()
    if not image_path:
        return
    remaining = DetectionRecord.query.filter(
        DetectionRecord.image_path == image_path,
        DetectionRecord.id != record.id,
    ).first()
    if remaining:
        return
    target = DETECTION_IMAGE_DIR / Path(image_path).name
    if target.exists() and target.is_file():
        try:
            target.unlink()
        except OSError:
            app.logger.warning("无法删除检测图片: %s", target)


def build_rule_assessment(boxes: list[dict], frame_width: int, frame_height: int) -> dict:
    frame_area = max(frame_width * frame_height, 1)
    findings = []
    highest_level = "待复核"
    level_rank = {"待复核": 0, "低": 1, "中": 2, "高": 3}

    for index, box in enumerate(boxes, start=1):
        label = str(box.get("label", ""))
        knowledge = DEFECT_KNOWLEDGE.get(
            label,
            {
                "name": label or "未知缺陷",
                "explanation": "检测到异常区域，需要结合现场情况进行人工复核。",
                "solutions": {
                    key: ["记录位置与尺寸。", "安排人工复核。", "根据复核结果制定处置方案。"]
                    for key in ("low", "medium", "high", "critical")
                },
            },
        )
        area_ratio = max(float(box.get("w", 0)) * float(box.get("h", 0)) / frame_area, 0.0)
        confidence = float(box.get("conf", 0))
        profile_key, confidence_title, level, confidence_interval = confidence_profile(confidence)
        area_escalated = False
        if area_ratio >= 0.12 and level in {"待复核", "低", "中"}:
            next_level = {"待复核": "低", "低": "中", "中": "高"}
            level = next_level[level]
            area_escalated = True
        if level_rank[level] > level_rank[highest_level]:
            highest_level = level
        center_x = float(box.get("x", 0)) + float(box.get("w", 0)) / 2
        center_y = float(box.get("y", 0)) + float(box.get("h", 0)) / 2
        horizontal = "左侧" if center_x < frame_width / 3 else ("右侧" if center_x > frame_width * 2 / 3 else "中部")
        vertical = "上部" if center_y < frame_height / 3 else ("下部" if center_y > frame_height * 2 / 3 else "中部")
        findings.append(
            {
                "index": index,
                "label": label,
                "name": knowledge["name"],
                "confidence": round(confidence, 3),
                "area_ratio": round(area_ratio, 4),
                "position": f"{vertical}{horizontal}",
                "risk_level": level,
                "confidence_band": confidence_title,
                "confidence_interval": confidence_interval,
                "area_escalated": area_escalated,
                "explanation": knowledge["explanation"],
                "actions": knowledge["solutions"][profile_key],
            }
        )

    if not findings:
        report = "本次图像中未检测到坑状或条状缺陷。建议在光照均匀、画面清晰的条件下复拍，并继续保持周期性巡检。"
        return {"risk_level": "未发现", "findings": [], "report": report}

    lines = [
        f"综合风险等级：{highest_level}",
        "",
        f"共发现 {len(findings)} 个待研判区域：",
    ]
    for item in findings:
        lines.extend(
            [
                "",
                f"{item['index']}. {item['name']}（置信度 {item['confidence'] * 100:.1f}%）",
                f"置信分档：{item['confidence_band']}（{item['confidence_interval']}）；位置：{item['position']}。",
                f"框选面积约占图像 {item['area_ratio'] * 100:.2f}%；风险等级：{item['risk_level']}"
                + ("（因区域面积较大上调一级）" if item["area_escalated"] else "")
                + "。",
                f"研判说明：{item['explanation']}",
                "分级处置方案：",
                *[f"- {action}" for action in item["actions"]],
            ]
        )
    lines.extend(
        [
            "",
            "提示：自动研判用于辅助筛查，涉及停用、维修或结构安全的决定仍应由现场专业人员复核。",
        ]
    )
    return {"risk_level": highest_level, "findings": findings, "report": "\n".join(lines)}


def request_intelligence_assessment(rule_result: dict) -> tuple[str, bool]:
    if not INTELLIGENCE_API_KEY:
        return rule_result["report"], False

    prompt = (
        "你是工业表面缺陷智能研判模型。请根据检测结果，用简洁、专业的中文给出："
        "1. 缺陷解释；2. 风险等级及依据；3. 建议的复核步骤；4. 分级处置方案。"
        "必须严格区分不同置信区间，低置信区域以复拍和排除误检为主，中等置信区域以人工确认和测量为主，"
        "高置信区域以优先检查、维修或限制使用为主。不得虚构图像中未提供的信息，并明确自动研判仅作辅助。\n\n"
        f"检测结构化数据：{json.dumps(rule_result['findings'], ensure_ascii=False)}"
    )
    payload = {
        "model": INTELLIGENCE_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是本系统的工业缺陷智能研判模块，只输出中文专业研判报告。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1000,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{INTELLIGENCE_API_BASE}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {INTELLIGENCE_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=INTELLIGENCE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        report = data["choices"][0]["message"]["content"].strip()
        return report or rule_result["report"], bool(report)
    except Exception:
        app.logger.exception("智能研判服务调用失败，已回退到本地规则库")
        return rule_result["report"], False


DEFAULT_RUNTIME_STATE = {
    "camera_on": False,
    "camera_state": "未连接",
    "detection_on": False,
    "last_detection_time": None,
    "camera_type": "builtin",
    "camera_label": "未选择",
    "camera_started_at": None,
    "last_inference_ms": 0,
    "last_recorded_detection_at": 0.0,
}
DEFAULT_CAMERA_SETTINGS = {
    "resolution": "720P",
    "fps": 20,
    "flip_horizontal": False,
    "flip_vertical": False,
}
runtime_states: dict[int, dict] = {}
runtime_states_lock = threading.RLock()
user_camera_settings_cache: dict[int, dict] = {}
camera_settings_lock = threading.RLock()
legacy_camera_settings = DEFAULT_CAMERA_SETTINGS.copy()
machine_settings = {
    "port": "",
    "baudrate": 115200,
    "timeout_ms": 300,
}
machine_serial_lock = threading.RLock()
machine_serial_conn = None
machine_serial_buffer = bytearray()
machine_serial_owner_user_id = None
machine_serial_owner_username = ""
machine_last_frame_at = None
machine_last_rx_bytes = 0
machine_last_messages = []


def get_user_runtime_state(user_id: int) -> dict:
    user_id = int(user_id)
    with runtime_states_lock:
        state = runtime_states.get(user_id)
        if state is None:
            state = DEFAULT_RUNTIME_STATE.copy()
            runtime_states[user_id] = state
        return state


def get_runtime_overview() -> dict:
    with runtime_states_lock:
        states = list(runtime_states.values())
        camera_count = sum(1 for state in states if state["camera_on"])
        detection_count = sum(1 for state in states if state["detection_on"])
    return {
        "camera_on": camera_count > 0,
        "camera_state": "已连接" if camera_count else "未连接",
        "detection_on": detection_count > 0,
        "camera_count": camera_count,
        "detection_count": detection_count,
    }


def normalize_camera_settings(value: dict | None = None) -> dict:
    source = {**legacy_camera_settings, **(value or {})}
    resolution = str(source.get("resolution", "720P")).upper()
    if resolution not in {"QVGA", "VGA", "720P", "1080P"}:
        resolution = "720P"
    try:
        fps = min(max(int(source.get("fps", 20)), 1), 60)
    except (TypeError, ValueError):
        fps = 20
    return {
        "resolution": resolution,
        "fps": fps,
        "flip_horizontal": bool(source.get("flip_horizontal", False)),
        "flip_vertical": bool(source.get("flip_vertical", False)),
    }


def get_user_camera_settings(user_id: int) -> dict:
    user_id = int(user_id)
    with camera_settings_lock:
        cached = user_camera_settings_cache.get(user_id)
        if cached is not None:
            return cached.copy()
        with Session(db.engine, expire_on_commit=False) as local_session:
            record = local_session.get(UserCameraConfig, user_id)
            if record is None:
                settings = normalize_camera_settings()
                record = UserCameraConfig(user_id=user_id, **settings)
                local_session.add(record)
                local_session.commit()
            else:
                settings = normalize_camera_settings(
                    {
                        "resolution": record.resolution,
                        "fps": record.fps,
                        "flip_horizontal": record.flip_horizontal,
                        "flip_vertical": record.flip_vertical,
                    }
                )
        user_camera_settings_cache[user_id] = settings
        return settings.copy()


def save_user_camera_settings(user_id: int, settings: dict) -> dict:
    user_id = int(user_id)
    normalized = normalize_camera_settings(settings)
    with camera_settings_lock:
        with Session(db.engine, expire_on_commit=False) as local_session:
            record = local_session.get(UserCameraConfig, user_id)
            if record is None:
                record = UserCameraConfig(user_id=user_id)
                local_session.add(record)
            record.resolution = normalized["resolution"]
            record.fps = normalized["fps"]
            record.flip_horizontal = normalized["flip_horizontal"]
            record.flip_vertical = normalized["flip_vertical"]
            local_session.commit()
        user_camera_settings_cache[user_id] = normalized.copy()
    return normalized


def get_machine_status(user_id: int) -> dict:
    with machine_serial_lock:
        connected = bool(
            machine_serial_conn is not None
            and getattr(machine_serial_conn, "is_open", True)
        )
        owner_user_id = machine_serial_owner_user_id if connected else None
        owned_by_current_user = connected and owner_user_id == int(user_id)
        return {
            "connected": owned_by_current_user,
            "occupied": connected and not owned_by_current_user,
            "owned_by_current_user": owned_by_current_user,
            "owner_user_id": owner_user_id,
            "owner_username": machine_serial_owner_username if connected else "",
        }


def bjt_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def to_bjt(dt: datetime) -> datetime:
    return dt + timedelta(hours=8)


def from_bjt(dt: datetime) -> datetime:
    return dt - timedelta(hours=8)


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)




def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def bind_user_session(user: User) -> str:
    token = generate_session_token()
    user.is_logged_in = True
    user.active_session_token = token
    session["auth_session_token"] = token
    session["heartbeat_at"] = int(time.time())
    return token


def clear_bound_session(user: User | None) -> None:
    if user:
        user.is_logged_in = False
        user.active_session_token = None
        db.session.commit()
    session.pop("auth_session_token", None)
    session.pop("heartbeat_at", None)
    session.pop("remember_login", None)
    session.pop("password_verified", None)


def stop_user_camera(user_id: int) -> None:
    state = get_user_runtime_state(user_id)
    with runtime_states_lock:
        started_at = state["camera_started_at"]
        state["camera_on"] = False
        state["detection_on"] = False
        state["camera_state"] = "未连接"
        state["camera_label"] = "未选择"
        state["camera_started_at"] = None
        state["last_inference_ms"] = 0
    if started_at:
        add_duration_seconds(
            user_id,
            datetime.utcfromtimestamp(started_at),
            datetime.utcnow(),
        )


def clear_runtime_after_logout(user_id: int) -> None:
    stop_user_camera(user_id)
    close_machine_serial(user_id)


def is_active_session_stale(user: User) -> bool:
    if not user.is_logged_in or not user.active_session_token:
        return True
    if not user.last_login_at:
        return True
    return (datetime.utcnow() - user.last_login_at).total_seconds() > ACTIVE_SESSION_TIMEOUT_SECONDS


def safe_fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "-"
    return to_bjt(dt).strftime("%Y-%m-%d %H:%M:%S")


def is_camera_connected(user_id: int) -> bool:
    state = get_user_runtime_state(user_id)
    if state["camera_type"] == "serial":
        return bool(state["camera_on"] and get_machine_status(user_id)["connected"])
    return bool(state["camera_on"] and state["camera_state"] != "未连接")


def sync_camera_state(user_id: int) -> bool:
    state = get_user_runtime_state(user_id)
    connected = is_camera_connected(user_id)
    with runtime_states_lock:
        state["camera_state"] = "已连接" if connected else "未连接"
        if not connected:
            state["detection_on"] = False
    return connected

def add_log(log_type: str, content: str, user_id: int | None = None, result: str = "成功"):
    """记录系统日志，且避免日志写入异常影响主流程。"""
    operator = "system"
    ip = request.remote_addr if has_request_context() else "-"
    try:
        with Session(db.engine, expire_on_commit=False) as local_session:
            if user_id:
                user = local_session.get(User, user_id)
                if user:
                    operator = user.username
            local_session.add(
                SystemLog(log_type=log_type, content=content, user_id=user_id, operator=operator, ip=ip or "-", result=result)
            )
            local_session.commit()
    except Exception:
        app.logger.exception("写入系统日志失败: %s", content)


def set_config_json(key: str, value: dict):
    payload = json.dumps(value, ensure_ascii=False)
    with Session(db.engine, expire_on_commit=False) as local_session:
        record = local_session.get(SystemConfig, key)
        if record:
            record.value = payload
        else:
            local_session.add(SystemConfig(key=key, value=payload))
        local_session.commit()


def get_config_json(key: str, default: dict):
    with Session(db.engine, expire_on_commit=False) as local_session:
        record = local_session.get(SystemConfig, key)
    if not record:
        return default
    try:
        data = json.loads(record.value)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return default


def add_duration_seconds(user_id: int, start_dt: datetime, end_dt: datetime):
    if end_dt <= start_dt:
        return
    user_id = int(user_id)
    with Session(db.engine, expire_on_commit=False) as local_session:
        cursor = start_dt
        while cursor < end_dt:
            cursor_bjt = to_bjt(cursor)
            next_day_bjt = datetime(cursor_bjt.year, cursor_bjt.month, cursor_bjt.day) + timedelta(days=1)
            next_day_utc = next_day_bjt - timedelta(hours=8)
            seg_end = min(end_dt, next_day_utc)
            seg_seconds = int((seg_end - cursor).total_seconds())
            day_key = cursor_bjt.strftime("%Y-%m-%d")
            rec = local_session.get(DailyDetectionDuration, (user_id, day_key))
            if not rec:
                rec = DailyDetectionDuration(user_id=user_id, day=day_key, seconds=0)
                local_session.add(rec)
            rec.seconds += max(seg_seconds, 0)
            cursor = seg_end
        local_session.commit()


def get_today_detection_seconds(user_id: int) -> int:
    user_id = int(user_id)
    now = datetime.utcnow()
    now_bjt = to_bjt(now)
    day_key = now_bjt.strftime("%Y-%m-%d")
    with Session(db.engine, expire_on_commit=False) as local_session:
        rec = local_session.get(DailyDetectionDuration, (user_id, day_key))
    total = rec.seconds if rec else 0
    state = get_user_runtime_state(user_id)
    if state["camera_on"] and state["camera_started_at"]:
        start_ts = state["camera_started_at"]
        start_dt = datetime.utcfromtimestamp(start_ts)
        start_bjt = to_bjt(start_dt)
        if start_bjt.strftime("%Y-%m-%d") == day_key:
            total += int((now - start_dt).total_seconds())
        elif start_dt < now:
            day_start_bjt = datetime(now_bjt.year, now_bjt.month, now_bjt.day)
            day_start = day_start_bjt - timedelta(hours=8)
            total += int((now - day_start).total_seconds())
    return max(total, 0)


def init_db():
    global legacy_camera_settings
    if IS_SQLITE:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with app.app_context():
        db.create_all()
        inspector = inspect(db.engine)

        def ensure_column(table: str, name: str, ddl: str):
            columns = {col["name"] for col in inspector.get_columns(table)}
            if name in columns:
                return
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            db.session.commit()

        user_columns = {col["name"] for col in inspector.get_columns("user")}
        if "is_active_account" not in user_columns:
            db.session.execute(
                text(
                    "ALTER TABLE user ADD COLUMN is_active_account BOOLEAN NOT NULL DEFAULT 1"
                )
            )
            db.session.commit()
        if "last_login_at" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN last_login_at DATETIME"))
            db.session.commit()
        if "email" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN email VARCHAR(120) NOT NULL DEFAULT ''"))
            db.session.commit()
        if "phone" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN phone VARCHAR(30) NOT NULL DEFAULT ''"))
            db.session.commit()
        if "avatar_url" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN avatar_url VARCHAR(255) NOT NULL DEFAULT ''"))
            db.session.commit()
        if "is_logged_in" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN is_logged_in BOOLEAN NOT NULL DEFAULT 0"))
            db.session.commit()
        if "active_session_token" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN active_session_token VARCHAR(128)"))
            db.session.commit()
        ensure_column("detection_record", "operator_name", "VARCHAR(50) NOT NULL DEFAULT 'system'")
        ensure_column("detection_record", "operation_type", "VARCHAR(50) NOT NULL DEFAULT '目标检测'")
        ensure_column("detection_record", "note", "VARCHAR(255) NOT NULL DEFAULT ''")
        ensure_column("detection_record", "image_path", "VARCHAR(255) NOT NULL DEFAULT ''")
        ensure_column("detection_record", "boxes_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column("detection_record", "frame_width", "INTEGER NOT NULL DEFAULT 0")
        ensure_column("detection_record", "frame_height", "INTEGER NOT NULL DEFAULT 0")
        ensure_column("system_log", "operator", "VARCHAR(50) NOT NULL DEFAULT 'system'")
        ensure_column("system_log", "ip", "VARCHAR(64) NOT NULL DEFAULT '-'")
        ensure_column("system_log", "result", "VARCHAR(20) NOT NULL DEFAULT '成功'")

        saved_camera = get_config_json("camera_settings", DEFAULT_CAMERA_SETTINGS.copy())
        legacy_camera_settings = normalize_camera_settings(saved_camera)
        if not db.session.get(SystemConfig, "camera_settings"):
            set_config_json("camera_settings", legacy_camera_settings)
        saved_machine = get_config_json("machine_settings", machine_settings.copy())
        for key in machine_settings:
            if key in saved_machine:
                machine_settings[key] = saved_machine[key]
        if not db.session.get(SystemConfig, "machine_settings"):
            set_config_json("machine_settings", machine_settings)

        primary_admin = User.query.filter_by(username="rtxq").first()
        legacy_admin = User.query.filter_by(username="admin").first()

        if not primary_admin and legacy_admin:
            legacy_admin.username = "rtxq"
            legacy_admin.is_admin = True
            db.session.commit()
            primary_admin = legacy_admin
            legacy_admin = None
            add_log("system", "已将历史管理员账号 admin 迁移为 rtxq")

        if not primary_admin:
            primary_admin = User(
                username="rtxq",
                password_hash=hash_password("admin123456"),
                is_admin=True,
            )
            db.session.add(primary_admin)
            db.session.commit()
            add_log("system", "初始化主管理员账号：rtxq/admin123456")

        if legacy_admin and legacy_admin.id != primary_admin.id:
            DetectionRecord.query.filter_by(user_id=legacy_admin.id).delete(synchronize_session=False)
            SystemLog.query.filter_by(user_id=legacy_admin.id).delete(synchronize_session=False)
            db.session.delete(legacy_admin)
            db.session.commit()
            add_log("system", "已删除多余的 legacy 管理员账号：admin")

        for user in User.query.all():
            if not db.session.get(UserCameraConfig, user.id):
                db.session.add(
                    UserCameraConfig(
                        user_id=user.id,
                        **legacy_camera_settings,
                    )
                )
        db.session.commit()

        duration_migration = get_config_json("duration_user_migration_v1", {})
        if (
            inspector.has_table("daily_detection_duration")
            and not duration_migration.get("completed")
        ):
            legacy_rows = db.session.execute(
                text("SELECT day, seconds FROM daily_detection_duration")
            ).all()
            for day, seconds in legacy_rows:
                record = db.session.get(
                    DailyDetectionDuration,
                    (primary_admin.id, str(day)),
                )
                if record is None:
                    record = DailyDetectionDuration(
                        user_id=primary_admin.id,
                        day=str(day),
                        seconds=0,
                    )
                    db.session.add(record)
                record.seconds = max(record.seconds, max(int(seconds or 0), 0))
            db.session.commit()
            set_config_json(
                "duration_user_migration_v1",
                {
                    "completed": True,
                    "legacy_owner_user_id": primary_admin.id,
                },
            )


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


init_db()


def parse_time_range(range_key: str, start_time: str, end_time: str):
    now_bjt = bjt_now()
    if range_key == "today":
        start_bjt = datetime(now_bjt.year, now_bjt.month, now_bjt.day)
        end_bjt = now_bjt
    elif range_key == "yesterday":
        start_bjt = datetime(now_bjt.year, now_bjt.month, now_bjt.day) - timedelta(days=1)
        end_bjt = datetime(now_bjt.year, now_bjt.month, now_bjt.day) - timedelta(seconds=1)
    elif range_key == "7d":
        start_bjt = now_bjt - timedelta(days=7)
        end_bjt = now_bjt
    elif range_key == "30d":
        start_bjt = now_bjt - timedelta(days=30)
        end_bjt = now_bjt
    elif range_key == "custom":
        try:
            start_bjt = datetime.fromisoformat(start_time)
            end_bjt = datetime.fromisoformat(end_time)
        except (TypeError, ValueError):
            return None, None
    else:
        start_bjt = now_bjt - timedelta(days=1)
        end_bjt = now_bjt
    return from_bjt(start_bjt), from_bjt(end_bjt)


@app.before_request
def make_session_permanent():
    if current_user.is_authenticated:
        if session.get("password_verified") is not True:
            clear_bound_session(current_user)
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify(
                    {
                        "ok": False,
                        "code": "AUTH_RELOGIN_REQUIRED",
                        "message": "请先在登录页面输入账号密码完成验证",
                        "redirect": url_for("login"),
                    }
                ), 401
            flash("请先在登录页面输入账号密码完成验证", "warning")
            return redirect(url_for("login"))

        opted_in_auto_login = session.get("remember_login") is True or request.cookies.get(AUTO_LOGIN_COOKIE) == "1"
        if opted_in_auto_login:
            session["remember_login"] = True

        session.permanent = bool(session.get("remember_login", False))
        token_in_session = session.get("auth_session_token")
        token_in_db = current_user.active_session_token

        if token_in_db and token_in_session and token_in_db != token_in_session:
            logout_user()
            session.pop("auth_session_token", None)
            session.pop("heartbeat_at", None)
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "code": "AUTH_INVALID", "message": "当前会话无效，请重新登录", "redirect": url_for("login")}), 401
            flash("当前会话无效，请重新登录", "warning")
            return redirect(url_for("login"))

        if not token_in_db and not token_in_session:
            token_in_session = bind_user_session(current_user)
            db.session.commit()
        elif token_in_db and not token_in_session:
            session["auth_session_token"] = token_in_db
            token_in_session = token_in_db
        elif token_in_session and not token_in_db:
            current_user.is_logged_in = True
            current_user.active_session_token = token_in_session
            db.session.commit()

        now_ts = int(time.time())
        last_heartbeat = int(session.get("heartbeat_at", 0) or 0)
        if now_ts - last_heartbeat >= ACTIVE_SESSION_HEARTBEAT_SECONDS:
            current_user.last_login_at = datetime.utcnow()
            db.session.commit()
            session["heartbeat_at"] = now_ts


def scoped_detection_query():
    query = DetectionRecord.query
    if not current_user.is_admin:
        query = query.filter(DetectionRecord.user_id == current_user.id)
    return query


def _cleanup_db_session(exception=None):
    """统一清理 DB 会话，避免连接池连接被长时间占用。"""
    if exception is not None:
        db.session.rollback()
    db.session.remove()


@app.teardown_request
def shutdown_session_request(exception=None):
    _cleanup_db_session(exception)


@app.teardown_appcontext
def shutdown_session_appcontext(exception=None):
    _cleanup_db_session(exception)


@app.route("/")
def index():
    if current_user.is_authenticated:
        clear_runtime_after_logout(current_user.id)
        clear_bound_session(current_user)
        logout_user()
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET" and current_user.is_authenticated:
        clear_runtime_after_logout(current_user.id)
        clear_bound_session(current_user)
        logout_user()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        try:
            user = User.query.filter_by(username=username).first()
            if user and verify_password(user.password_hash, password):
                if not user.is_active_account:
                    flash("账号已被禁用，请联系管理员", "danger")
                    return render_template("login.html")
                if user.is_logged_in and user.active_session_token:

                    was_stale = is_active_session_stale(user)
                    if was_stale:
                        previous_token = user.active_session_token

                        user.is_logged_in = False
                        user.active_session_token = None
                        db.session.commit()
                        add_log("auth", f"检测到旧会话已过期，自动释放登录状态: {username}", user.id)

                        app.logger.info("用户 %s 登录释放过期会话，旧token=%s", username, previous_token)
                    else:
                        flash("当前账号已在其他设备登录，请稍后再试", "warning")
                        add_log("auth", f"登录被拒绝（账号在线且会话活跃）: {username}", user.id, result="失败")

                        return render_template("login.html")

                login_user(user, remember=False)
                session["remember_login"] = False
                session["password_verified"] = True
                bind_user_session(user)
                user.last_login_at = datetime.utcnow()
                db.session.commit()
                add_log("auth", f"用户登录: {username}", user.id)
                response = make_response(redirect(url_for("monitor")))
                response.delete_cookie(AUTO_LOGIN_COOKIE)
                response.delete_cookie(REMEMBER_COOKIE_NAME)

                return response

            flash("账号或密码错误", "danger")
        except Exception:
            db.session.rollback()
            app.logger.exception("登录流程数据库操作失败")
            flash("登录失败：数据库连接异常，请稍后重试", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or len(password) < 6:
            flash("用户名不能为空，且密码长度至少 6 位", "warning")
            return render_template("register.html")
        if password != confirm_password:
            flash("两次密码输入不一致", "warning")
            return render_template("register.html")
        if User.query.filter_by(username=username).first():
            flash("用户名已存在", "warning")
            return render_template("register.html")

        user = User(username=username, password_hash=hash_password(password))
        db.session.add(user)
        db.session.commit()
        add_log("auth", f"新用户注册: {username}", user.id)
        flash("注册成功，请登录", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    add_log("auth", f"用户退出: {current_user.username}", current_user.id)
    clear_runtime_after_logout(current_user.id)
    clear_bound_session(current_user)
    logout_user()
    flash("您已退出登录", "info")
    response = make_response(redirect(url_for("login")))
    response.delete_cookie(AUTO_LOGIN_COOKIE)

    response.delete_cookie(REMEMBER_COOKIE_NAME)

    return response


@app.route("/relogin")
@login_required
def relogin():
    if current_user.is_authenticated:
        add_log("auth", f"用户重新登录: {current_user.username}", current_user.id)
        clear_runtime_after_logout(current_user.id)
        clear_bound_session(current_user)
        logout_user()
    flash("请重新登录以继续", "info")
    response = make_response(redirect(url_for("login")))
    response.delete_cookie(AUTO_LOGIN_COOKIE)

    response.delete_cookie(REMEMBER_COOKIE_NAME)

    return response


@app.post("/api/auth/logout")
@login_required
def api_logout():
    add_log("auth", f"用户退出: {current_user.username}", current_user.id)
    clear_runtime_after_logout(current_user.id)
    clear_bound_session(current_user)
    logout_user()
    response = make_response(jsonify({"ok": True, "action": "logout", "redirect": url_for("login")}))
    response.delete_cookie(AUTO_LOGIN_COOKIE)

    response.delete_cookie(REMEMBER_COOKIE_NAME)

    return response


@app.post("/api/auth/relogin")
@login_required
def api_relogin():
    add_log("auth", f"用户重新登录: {current_user.username}", current_user.id)
    clear_runtime_after_logout(current_user.id)
    clear_bound_session(current_user)
    logout_user()
    response = make_response(jsonify({"ok": True, "action": "relogin", "redirect": url_for("login")}))
    response.delete_cookie(AUTO_LOGIN_COOKIE)

    response.delete_cookie(REMEMBER_COOKIE_NAME)

    return response


@app.route("/profile")
@login_required
def profile_page():
    return render_template("profile.html")


@app.route("/monitor")
@login_required
def monitor():
    return render_template("monitor.html")


@app.route("/image-detection")
@login_required
def image_detection_page():
    return render_template("image_detection.html")


@app.route("/stats")
@login_required
def stats_page():
    return render_template("stats.html")


@app.route("/history")
@login_required
def history_page():
    return render_template("history.html")


@app.route("/admin")
@login_required
def admin_page():
    if not current_user.is_admin:
        flash("只有管理员可以访问该页面", "warning")
        return redirect(url_for("monitor"))
    return render_template("admin.html")


@app.get("/api/camera/status")
@login_required
def camera_status():
    state = get_user_runtime_state(current_user.id)
    machine_status = get_machine_status(current_user.id)
    connected = sync_camera_state(current_user.id)
    text = state["camera_label"] if connected else "离线"
    phase = "running" if connected else "offline"

    return jsonify(
        {
            "ok": True,
            "status": "connected" if connected else "disconnected",
            "connected": connected,

            "text": text,
            "phase": phase,

            "camera_on": state["camera_on"],
            "camera_state": state["camera_state"],
            "camera_type": state["camera_type"],
            "camera_label": state["camera_label"],
            "machine_connected": machine_status["connected"],
            "machine_occupied": machine_status["occupied"],
            "machine_owner": machine_status["owner_username"],
        }
    )


@app.get("/api/system/status")
@login_required
def system_status():
    state = get_user_runtime_state(current_user.id)
    camera_settings = get_user_camera_settings(current_user.id)
    machine_status = get_machine_status(current_user.id)
    model_service.ensure_loaded(cooldown_sec=3.0)
    sync_camera_state(current_user.id)
    camera_state = state["camera_state"] if state["camera_on"] else "未连接"
    return jsonify(
        {
            "ok": True,
            "camera_on": state["camera_on"],
            "camera_state": camera_state,
            "detection_on": state["detection_on"],
            "camera_type": state["camera_type"],
            "camera_label": state["camera_label"],
            "camera_settings": camera_settings,
            "machine_connected": machine_status["connected"],
            "machine_occupied": machine_status["occupied"],
            "machine_owned_by_current_user": machine_status["owned_by_current_user"],
            "machine_owner": machine_status["owner_username"],
            "machine_settings": machine_settings,
            "machine_last_frame_at": safe_fmt_dt(machine_last_frame_at),
            "machine_last_rx_bytes": machine_last_rx_bytes,
            "last_detection_time": state["last_detection_time"],
            "camera_started_at": state["camera_started_at"],
            "last_inference_ms": state["last_inference_ms"],
            "today_detection_seconds": get_today_detection_seconds(current_user.id),
            "model_name": model_service.model_name,
            "model_path": str(model_service.model_path),
            "model_loaded": bool(model_service.model),
            "model_error": model_service.last_error,
            "model_backend": model_service.backend,
            "model_imgsz": model_service.imgsz,
            "model_conf": model_service.conf,
            "model_warmup_ms": model_service.warmup_ms,

            "server_time": bjt_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


@app.post("/api/model/reload")
@login_required
def reload_model():
    ok, message = model_service.reload()
    code = 200 if ok else 400
    return jsonify(
        {
            "ok": ok,
            "message": message,
            "model_name": model_service.model_name,
            "model_path": str(model_service.model_path),
            "model_loaded": bool(model_service.model),
            "model_error": model_service.last_error,
            "model_backend": model_service.backend,
            "model_warmup_ms": model_service.warmup_ms,
        }
    ), code


@app.post("/api/camera/settings")
@login_required
def update_camera_settings():
    payload = request.get_json(silent=True) or {}
    current_settings = get_user_camera_settings(current_user.id)
    try:
        settings = save_user_camera_settings(
            current_user.id,
            {
                "resolution": payload.get("resolution", current_settings["resolution"]),
                "fps": payload.get("fps", current_settings["fps"]),
                "flip_horizontal": payload.get(
                    "flip_horizontal",
                    current_settings["flip_horizontal"],
                ),
                "flip_vertical": payload.get(
                    "flip_vertical",
                    current_settings["flip_vertical"],
                ),
            },
        )
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "摄像头参数格式错误"}), 400
    add_log("device", f"更新个人摄像头配置: {settings}", current_user.id)
    return jsonify({"ok": True, "settings": settings})


@app.get("/api/device/ports")
@login_required
def device_ports():
    ports = []
    if list_ports:
        ports = [
            {
                "device": port.device,
                "description": port.description or "串口设备",
                "manufacturer": port.manufacturer or "",
            }
            for port in list_ports.comports()
        ]
    return jsonify({"ok": True, "ports": ports})


@app.post("/api/device/connect")
@login_required
def device_connect():
    global machine_serial_conn, machine_serial_buffer
    global machine_serial_owner_user_id, machine_serial_owner_username
    if serial is None:
        return jsonify({"ok": False, "message": "当前环境未安装 pyserial"}), 500
    machine_status = get_machine_status(current_user.id)
    if machine_status["occupied"]:
        owner = machine_status["owner_username"] or "其他用户"
        return jsonify(
            {
                "ok": False,
                "code": "DEVICE_OCCUPIED",
                "message": f"该串口设备正由 {owner} 使用，请等待对方断开",
                "owner": owner,
            }
        ), 409
    payload = request.get_json(silent=True) or {}
    port = str(payload.get("port") or "").strip()
    if not port:
        return jsonify({"ok": False, "message": "请选择串口"}), 400
    try:
        baudrate = int(payload.get("baudrate") or machine_settings["baudrate"])
        timeout_ms = min(max(int(payload.get("timeout_ms") or machine_settings["timeout_ms"]), 50), 5000)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "串口参数格式错误"}), 400

    with machine_serial_lock:
        machine_status = get_machine_status(current_user.id)
        if machine_status["occupied"]:
            owner = machine_status["owner_username"] or "其他用户"
            return jsonify(
                {
                    "ok": False,
                    "code": "DEVICE_OCCUPIED",
                    "message": f"该串口设备正由 {owner} 使用，请等待对方断开",
                    "owner": owner,
                }
            ), 409
        close_machine_serial(current_user.id)
        try:
            machine_serial_conn = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=timeout_ms / 1000,
                write_timeout=timeout_ms / 1000,
            )
        except Exception as exc:
            return jsonify({"ok": False, "message": f"串口连接失败：{exc}"}), 400
        machine_serial_buffer = bytearray()
        machine_serial_owner_user_id = current_user.id
        machine_serial_owner_username = current_user.username
        machine_settings.update(
            {"port": port, "baudrate": baudrate, "timeout_ms": timeout_ms}
        )
    set_config_json("machine_settings", machine_settings)
    add_log("device", f"设备通信模块已连接: {port}@{baudrate}", current_user.id)
    return jsonify(
        {
            "ok": True,
            "connected": True,
            "owned_by_current_user": True,
            "settings": machine_settings,
        }
    )


@app.post("/api/device/disconnect")
@login_required
def device_disconnect():
    machine_status = get_machine_status(current_user.id)
    if machine_status["occupied"]:
        owner = machine_status["owner_username"] or "其他用户"
        return jsonify(
            {
                "ok": False,
                "code": "DEVICE_OCCUPIED",
                "message": f"设备由 {owner} 占用，当前账号不能断开",
            }
        ), 409
    closed, _ = close_machine_serial(current_user.id)
    if not closed:
        return jsonify({"ok": False, "message": "当前账号无权断开该设备"}), 409
    state = get_user_runtime_state(current_user.id)
    if state["camera_type"] == "serial":
        stop_user_camera(current_user.id)
    add_log("device", "设备通信模块已断开", current_user.id)
    return jsonify({"ok": True, "connected": False})


@app.post("/api/device/command")
@login_required
def device_command():
    if not get_machine_status(current_user.id)["connected"]:
        return jsonify({"ok": False, "message": "设备通信模块未连接或不属于当前账号"}), 400
    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command") or "").strip()
    if not command:
        return jsonify({"ok": False, "message": "请输入需要发送的指令"}), 400
    if len(command) > 256:
        return jsonify({"ok": False, "message": "单条指令不能超过 256 个字符"}), 400
    try:
        with machine_serial_lock:
            raw = (command + "\n").encode("utf-8")
            machine_serial_conn.write(raw)
            machine_serial_conn.flush()
    except Exception as exc:
        close_machine_serial(current_user.id)
        state = get_user_runtime_state(current_user.id)
        if state["camera_type"] == "serial":
            stop_user_camera(current_user.id)
        return jsonify({"ok": False, "message": f"指令发送失败：{exc}"}), 400
    add_log("device", f"串口发送指令: {command}", current_user.id)
    return jsonify({"ok": True, "bytes": len(raw), "command": command})


@app.get("/api/device/frame")
@login_required
def device_frame():
    global machine_serial_buffer, machine_last_frame_at, machine_last_rx_bytes, machine_last_messages
    if not get_machine_status(current_user.id)["connected"]:
        return jsonify({"ok": False, "message": "设备通信模块未连接或不属于当前账号"}), 400
    try:
        with machine_serial_lock:
            waiting = int(getattr(machine_serial_conn, "in_waiting", 0) or 0)
            chunk = machine_serial_conn.read(min(waiting, 262144)) if waiting > 0 else b""
            if chunk:
                machine_last_rx_bytes = len(chunk)
                machine_serial_buffer.extend(chunk)
            messages = extract_serial_text_messages(machine_serial_buffer)
            if messages:
                machine_last_messages = messages[-10:]
            frame = extract_jpeg_frame(machine_serial_buffer)
    except Exception as exc:
        close_machine_serial(current_user.id)
        state = get_user_runtime_state(current_user.id)
        if state["camera_type"] == "serial":
            stop_user_camera(current_user.id)
        return jsonify({"ok": False, "message": f"串口数据读取失败：{exc}"}), 400

    if not frame:
        return jsonify(
            {
                "ok": True,
                "waiting": True,
                "message": "等待设备发送 JPEG 图像帧",
                "buffer_bytes": len(machine_serial_buffer),
                "rx_bytes": machine_last_rx_bytes,
                "messages": machine_last_messages,
            }
        )

    machine_last_frame_at = datetime.utcnow()
    state = get_user_runtime_state(current_user.id)
    if state["camera_type"] == "serial" and state["camera_on"]:
        state["camera_state"] = "已连接"
    return jsonify(
        {
            "ok": True,
            "waiting": False,
            "frame": base64.b64encode(frame).decode("ascii"),
            "frame_bytes": len(frame),
            "rx_bytes": machine_last_rx_bytes,
            "messages": machine_last_messages,
        }
    )


@app.post("/api/camera/start")
@login_required
def start_camera():
    state = get_user_runtime_state(current_user.id)
    payload = request.get_json(silent=True) or {}
    camera_type = str(payload.get("camera_type") or "builtin").strip().lower()
    if camera_type not in {"builtin", "usb", "serial"}:
        return jsonify({"ok": False, "message": "不支持的摄像头来源"}), 400
    if camera_type == "serial":
        machine_status = get_machine_status(current_user.id)
        if not machine_status["connected"]:
            if machine_status["occupied"]:
                owner = machine_status["owner_username"] or "其他用户"
                return jsonify(
                    {
                        "ok": False,
                        "code": "DEVICE_OCCUPIED",
                        "message": f"串口设备正由 {owner} 使用",
                    }
                ), 409
            return jsonify({"ok": False, "message": "请先连接设备通信模块"}), 400
    camera_label = str(payload.get("camera_label") or "浏览器摄像头").strip()[:120]
    model_service.ensure_loaded(cooldown_sec=0.0)
    with runtime_states_lock:
        state["camera_type"] = camera_type
        state["camera_label"] = camera_label
        state["camera_on"] = True
        state["detection_on"] = False
        state["camera_state"] = "已连接"
        if state["camera_started_at"] is None:
            state["camera_started_at"] = time.time()

    add_log("device", f"摄像头已开启({camera_label})", current_user.id)
    return jsonify(
        {
            "ok": True,
            "camera_on": True,
            "camera_type": camera_type,
            "camera_label": camera_label,

            "detection_on": state["detection_on"],
            "model_loaded": bool(model_service.model),
            "model_error": "",
            "message": "",

        }
    )


@app.post("/api/camera/stop")
@login_required
def stop_camera():
    stop_user_camera(current_user.id)
    add_log("device", "摄像头已关闭", current_user.id)
    return jsonify({"ok": True, "camera_on": False})


@app.post("/api/detection/start")
@login_required
def start_detection():
    state = get_user_runtime_state(current_user.id)
    if not state["camera_on"]:
        return jsonify({"ok": False, "message": "请先打开摄像头"}), 400

    if not model_service.ensure_loaded(cooldown_sec=0.0):
        return jsonify({"ok": False, "message": model_service.last_error or "检测模型未加载"}), 503

    state["detection_on"] = True
    add_log("detection", "目标检测已开启", current_user.id)
    return jsonify({"ok": True, "detection_on": True})


@app.post("/api/detection/stop")
@login_required
def stop_detection():
    state = get_user_runtime_state(current_user.id)
    state["detection_on"] = False
    add_log("detection", "目标检测已停止", current_user.id)
    return jsonify({"ok": True, "detection_on": False})


@app.route("/api/detection/frame-data", methods=["GET", "POST"])
@login_required
def frame_data():
    state = get_user_runtime_state(current_user.id)
    if not state["camera_on"]:
        return jsonify({"ok": False, "message": "摄像头未开启", "boxes": [], "counts": {}})

    if not state["detection_on"]:
        return jsonify({"ok": True, "boxes": [], "counts": {}, "detection_on": False})
    model_service.ensure_loaded(cooldown_sec=0.0)
    if not model_service.model:
        return jsonify(
            {
                "ok": False,
                "message": model_service.last_error or "检测模型未加载",
                "boxes": [],
                "counts": {},
                "detection_on": True,
            }
        ), 503


    payload = request.get_json(silent=True) or {}
    frame_meta = {"source": state["camera_type"]}
    frame_b64 = (payload.get("frame") or "").strip()
    frame_provided = bool(frame_b64)
    if frame_b64:
        if "," in frame_b64:
            frame_b64 = frame_b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(frame_b64)
            frame = decode_image_bytes(raw)
            if frame is not None:
                frame_meta["frame_array"] = frame
        except Exception:
            pass

    if frame_provided and "frame_array" not in frame_meta:

        return jsonify({"ok": False, "message": "摄像头帧解析失败，请检查图像编码格式", "boxes": [], "counts": {}})

    infer_start = time.perf_counter()
    result = model_service.predict_from_frame(frame_meta=frame_meta)
    state["last_inference_ms"] = round((time.perf_counter() - infer_start) * 1000, 2)
    state["last_detection_time"] = bjt_now().strftime("%Y-%m-%d %H:%M:%S")

    record_now = time.time()
    with runtime_states_lock:
        should_record = (
            bool(result["counts"])
            and record_now - state["last_recorded_detection_at"]
            >= DETECTION_RECORD_INTERVAL_SECONDS
        )
        if should_record:
            state["last_recorded_detection_at"] = record_now
    if should_record:
        record_detection_result(
            result,
            "实时检测",
            state["camera_label"],
            frame_meta.get("frame_array"),
        )

    return jsonify(
        {
            "ok": True,
            **result,
            "detection_on": True,
            "inference_ms": state["last_inference_ms"],
            "model_name": model_service.model_name,
        }
    )


@app.post("/api/image-detection")
@login_required
def image_detection():
    state = get_user_runtime_state(current_user.id)
    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify({"ok": False, "message": "请选择需要检测的图片"}), 400

    raw = image_file.read(MAX_IMAGE_UPLOAD_BYTES + 1)
    if len(raw) > MAX_IMAGE_UPLOAD_BYTES:
        return jsonify({"ok": False, "message": "图片过大，请上传 12MB 以内的文件"}), 413
    frame = decode_image_bytes(raw)
    if frame is None:
        return jsonify({"ok": False, "message": "无法解析图片，请使用 JPG、PNG、BMP 或 WebP 格式"}), 400
    if not model_service.ensure_loaded(cooldown_sec=0.0):
        return jsonify({"ok": False, "message": model_service.last_error or "检测模型未加载"}), 503

    infer_start = time.perf_counter()
    result = model_service.predict_from_frame({"source": "image", "frame_array": frame})
    inference_ms = round((time.perf_counter() - infer_start) * 1000, 2)
    state["last_inference_ms"] = inference_ms
    state["last_detection_time"] = bjt_now().strftime("%Y-%m-%d %H:%M:%S")
    safe_filename = Path(image_file.filename).name[:120]
    if result.get("counts"):
        record_detection_result(result, "图片检测", f"上传图片：{safe_filename}", frame)
    add_log(
        "detection",
        f"图片检测完成: {safe_filename}，发现 {sum(result.get('counts', {}).values())} 个目标",
        current_user.id,
    )
    return jsonify(
        {
            "ok": True,
            **result,
            "filename": safe_filename,
            "inference_ms": inference_ms,
            "model_name": model_service.model_name,
        }
    )


@app.post("/api/intelligence/analyze")
@login_required
def intelligence_analyze():
    payload = request.get_json(silent=True) or {}
    boxes = payload.get("boxes") or []
    if not isinstance(boxes, list):
        return jsonify({"ok": False, "message": "研判数据格式错误"}), 400
    boxes = boxes[:30]
    frame_width = max(int(payload.get("frame_width") or 1), 1)
    frame_height = max(int(payload.get("frame_height") or 1), 1)
    rule_result = build_rule_assessment(boxes, frame_width, frame_height)
    report, enhanced = request_intelligence_assessment(rule_result)
    add_log(
        "analysis",
        f"完成智能研判，区域数={len(boxes)}，综合风险={rule_result['risk_level']}",
        current_user.id,
    )
    return jsonify(
        {
            "ok": True,
            "report": report,
            "risk_level": rule_result["risk_level"],
            "findings": rule_result["findings"],
            "enhanced": enhanced,
            "message": "智能研判完成" if enhanced else "已使用系统规则库完成研判",
        }
    )


@app.get("/api/stats/live")
@login_required
def live_stats():
    state = get_user_runtime_state(current_user.id)
    camera_settings = get_user_camera_settings(current_user.id)
    now = datetime.utcnow()
    now_bjt = to_bjt(now)
    today_start = from_bjt(datetime(now_bjt.year, now_bjt.month, now_bjt.day))
    today_records = scoped_detection_query().filter(DetectionRecord.detect_time >= today_start).all()

    total_events = len(today_records)
    total_objects = sum(r.count for r in today_records)
    category_counter = Counter()
    for r in today_records:
        category_counter[r.category] += r.count

    hourly = [0] * 24
    for r in today_records:
        hourly[to_bjt(r.detect_time).hour] += r.count

    timeline_window_start = now - timedelta(minutes=1)
    recent_records = scoped_detection_query().filter(DetectionRecord.detect_time >= timeline_window_start).all()
    sec_bucket = Counter()
    for record in recent_records:
        sec_bucket[to_bjt(record.detect_time).strftime("%H:%M:%S")] += record.count
    timeline = []
    for i in range(20):
        t = now_bjt - timedelta(seconds=(19 - i) * 3)
        key = t.strftime("%H:%M:%S")
        timeline.append({"time": key, "value": sec_bucket.get(key, 0)})

    return jsonify(
        {
            "ok": True,
            "cards": {
                "today_events": total_events,
                "today_objects": total_objects,
                "active_users": User.query.filter(
                    User.is_active_account.is_(True),
                    User.last_login_at.isnot(None),
                    User.last_login_at >= today_start,
                ).count(),
                "camera_type": state["camera_type"],
                "camera_label": state["camera_label"],
                "resolution": camera_settings["resolution"],
                "fps": camera_settings["fps"],
                "inference_ms": state["last_inference_ms"],
                "camera_on": state["camera_on"],
                "camera_state": state["camera_state"],
                "camera_settings": camera_settings,
            },
            "series_meta": {
                "categories": sorted(list(category_counter.keys())),
                "total": total_objects,
            },
            "line": timeline,
            "pie": [{"name": k, "value": v} for k, v in category_counter.items()],
            "bar": hourly,
        }
    )


@app.get("/api/stats/advanced")
@login_required
def advanced_stats():
    range_key = request.args.get("range", "today")
    start_time = request.args.get("start_time", "")
    end_time = request.args.get("end_time", "")
    categories = [c.strip() for c in request.args.get("categories", "").split(",") if c.strip()]

    start, end = parse_time_range(range_key, start_time, end_time)
    if not start or not end:
        return jsonify({"ok": False, "message": "自定义时间格式错误"}), 400

    records = scoped_detection_query().filter(
        DetectionRecord.detect_time >= start,
        DetectionRecord.detect_time <= end,
    )
    if categories:
        records = records.filter(DetectionRecord.category.in_(categories))
    records = records.all()

    total = sum(r.count for r in records)
    cate = Counter()
    timeline = []
    grouped = Counter()
    confidence_grouped = {}
    for r in records:
        cate[r.category] += r.count
        key = to_bjt(r.detect_time).strftime("%Y-%m-%d %H:%M")
        grouped[key] += r.count
        confidence_key = (key, r.category)
        current = confidence_grouped.setdefault(confidence_key, {"sum": 0.0, "count": 0})
        current["sum"] += float(r.confidence)
        current["count"] += 1
    timeline_keys = sorted(grouped.keys())[-100:]
    for key in timeline_keys:
        timeline.append({"time": key, "value": grouped[key], "dist": dict(cate)})

    bar_data = []
    for name, value in cate.items():
        bar_data.append({"name": name, "value": value})

    confidence_categories = sorted({r.category for r in records})
    confidence_series = []
    for category_name in confidence_categories:
        values = []
        for key in timeline_keys:
            bucket = confidence_grouped.get((key, category_name))
            values.append(
                round(bucket["sum"] / bucket["count"] * 100, 2)
                if bucket and bucket["count"]
                else None
            )
        confidence_series.append({"name": category_name, "data": values})

    return jsonify(
        {
            "ok": True,
            "timeline": timeline,
            "pie": [{"name": n, "value": v} for n, v in cate.items()],
            "bar": bar_data,
            "confidence_trend": {
                "times": timeline_keys,
                "series": confidence_series,
            },
            "categories": sorted(list({r.category for r in scoped_detection_query().all()})),
            "total": total,
            "range": {"start": to_bjt(start).isoformat(sep=" "), "end": to_bjt(end).isoformat(sep=" ")},
        }
    )


@app.get("/api/stats/export")
@login_required
def export_stats():
    fmt = request.args.get("format", "csv").lower()
    query = scoped_detection_query()
    keyword = request.args.get("keyword", "").strip()
    category = request.args.get("category", "").strip()
    note = request.args.get("note", "").strip()
    start_time = request.args.get("start_time", "").strip()
    end_time = request.args.get("end_time", "").strip()

    if keyword:
        query = query.filter((DetectionRecord.category.contains(keyword)) | (DetectionRecord.note.contains(keyword)))
    if category:
        query = query.filter_by(category=category)
    if note:
        query = query.filter(DetectionRecord.note.contains(note))
    if start_time:
        try:
            query = query.filter(DetectionRecord.detect_time >= from_bjt(datetime.fromisoformat(start_time)))
        except ValueError:
            pass
    if end_time:
        try:
            query = query.filter(DetectionRecord.detect_time <= from_bjt(datetime.fromisoformat(end_time)))
        except ValueError:
            pass

    records = query.order_by(DetectionRecord.detect_time.desc()).limit(3000).all()
    rows = [
        {
            "id": r.id,
            "time": to_bjt(r.detect_time).strftime("%Y-%m-%d %H:%M:%S"),
            "category": r.category,
            "count": r.count,
            "confidence": r.confidence,
            "operator": r.operator_name,
            "operation_type": r.operation_type,
            "has_image": bool(r.image_path),
        }
        for r in records
    ]

    if fmt == "json":
        return jsonify({"ok": True, "data": rows})

    if fmt == "excel":
        lines = ["ID\t时间\t类别\t数量\t置信度"]
        for r in rows:
            lines.append(f"{r['id']}\t{r['time']}\t{r['category']}\t{r['count']}\t{r['confidence']}")
        content = "\n".join(lines)
        mimetype = "application/vnd.ms-excel"
        suffix = "xls"
    else:
        lines = ["id,time,category,count,confidence"]
        for r in rows:
            lines.append(f"{r['id']},{r['time']},{r['category']},{r['count']},{r['confidence']}")
        content = "\n".join(lines)
        mimetype = "text/csv"
        suffix = "csv"

    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename=stats_export.{suffix}"},
    )


@app.delete("/api/admin/history")
@login_required
def clear_history():
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    image_paths = [
        Path(path).name
        for (path,) in db.session.query(DetectionRecord.image_path)
        .filter(DetectionRecord.image_path != "")
        .distinct()
        .all()
    ]
    DetectionRecord.query.delete()
    db.session.commit()
    for image_path in image_paths:
        target = DETECTION_IMAGE_DIR / image_path
        if target.exists() and target.is_file():
            try:
                target.unlink()
            except OSError:
                app.logger.warning("无法删除检测图片: %s", target)
    add_log("admin", "管理员清空全部检测记录", current_user.id)
    return jsonify({"ok": True})


@app.get("/api/history")
@login_required
def get_history():
    query = scoped_detection_query()
    keyword = request.args.get("keyword", "").strip()
    category = request.args.get("category", "").strip()
    note = request.args.get("note", "").strip()
    start_time = request.args.get("start_time", "").strip()
    end_time = request.args.get("end_time", "").strip()
    page = max(int(request.args.get("page", 1)), 1)
    page_size = min(max(int(request.args.get("page_size", 20)), 1), 100)

    if keyword:
        query = query.filter((DetectionRecord.category.contains(keyword)) | (DetectionRecord.note.contains(keyword)))
    if category:
        query = query.filter_by(category=category)
    if note:
        query = query.filter(DetectionRecord.note.contains(note))
    if start_time:
        try:
            query = query.filter(DetectionRecord.detect_time >= from_bjt(datetime.fromisoformat(start_time)))
        except ValueError:
            pass
    if end_time:
        try:
            query = query.filter(DetectionRecord.detect_time <= from_bjt(datetime.fromisoformat(end_time)))
        except ValueError:
            pass

    total = query.count()
    records = (
        query.order_by(DetectionRecord.detect_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    data = [
        {
            "id": r.id,
            "time": to_bjt(r.detect_time).strftime("%Y-%m-%d %H:%M:%S"),
            "category": r.category,
            "count": r.count,
            "confidence": r.confidence,
            "operator": r.operator_name,
            "operation_type": r.operation_type,
            "has_image": bool(r.image_path),
        }
        for r in records
    ]
    return jsonify({"ok": True, "records": data, "total": total, "page": page, "page_size": page_size})


@app.get("/api/admin/overview")
@login_required
def admin_overview():
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403

    runtime_overview = get_runtime_overview()
    users = User.query.order_by(User.created_at.desc()).all()
    offset = max(int(request.args.get("offset", 0)), 0)
    limit = min(max(int(request.args.get("limit", 50)), 1), 100)
    operator_filter = request.args.get("operator", "").strip()
    log_query = SystemLog.query
    if operator_filter:
        log_query = log_query.filter(SystemLog.operator.contains(operator_filter))
    log_total = log_query.count()
    logs = log_query.order_by(SystemLog.created_at.desc()).offset(offset).limit(limit).all()

    return jsonify(
        {
            "ok": True,
            "pagination": {
                "user_total": len(users),
                "log_total": log_total,
                "log_offset": offset,
                "log_limit": limit,
            },
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "is_admin": u.is_admin,
                    "status": "启用" if u.is_active_account else "禁用",
                    "email": u.email,
                    "phone": u.phone,
                    "avatar_url": u.avatar_url,
                    "created_at": safe_fmt_dt(u.created_at),
                    "last_login_at": safe_fmt_dt(u.last_login_at),
                }
                for u in users
            ],
            "metrics": {
                "user_count": User.query.count(),
                "history_total": DetectionRecord.query.count(),
                "history_total_desc": "累计检测记录=系统中累计保存的检测记录条数",
                "camera_on": runtime_overview["camera_on"],
                "camera_state": runtime_overview["camera_state"],
                "detection_on": runtime_overview["detection_on"],
                "camera_count": runtime_overview["camera_count"],
                "detection_count": runtime_overview["detection_count"],
                "today_logs": db.session.query(func.count(SystemLog.id))
                .filter(SystemLog.created_at >= from_bjt(datetime(bjt_now().year, bjt_now().month, bjt_now().day)))
                .scalar(),
            },
            "logs": [
                {
                    "time": safe_fmt_dt(l.created_at),
                    "operator": l.operator,
                    "ip": l.ip,
                    "content": l.content,
                    "result": l.result,
                }
                for l in logs
            ],
        }
    )


@app.delete("/api/admin/history/<int:record_id>")
@login_required
def delete_history(record_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403

    record = DetectionRecord.query.get_or_404(record_id)
    delete_detection_image_if_unused(record)
    db.session.delete(record)
    db.session.commit()
    add_log("admin", f"管理员删除历史记录 ID={record_id}", current_user.id)
    return jsonify({"ok": True})


@app.delete("/api/history/<int:record_id>")
@login_required
def delete_history_self(record_id: int):
    record = DetectionRecord.query.get_or_404(record_id)
    if (not current_user.is_admin) and record.user_id != current_user.id:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    delete_detection_image_if_unused(record)
    db.session.delete(record)
    db.session.commit()
    add_log("history", f"删除历史记录 ID={record_id}", current_user.id)
    return jsonify({"ok": True})


@app.post("/api/admin/user/<int:user_id>/reset-password")
@login_required
def reset_user_password(user_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    user = User.query.get_or_404(user_id)
    user.password_hash = hash_password("12345678")
    db.session.commit()
    add_log("admin", f"重置用户密码: {user.username}", current_user.id)
    return jsonify({"ok": True})


@app.post("/api/admin/user/<int:user_id>/role")
@login_required
def update_user_role(user_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    user = User.query.get_or_404(user_id)
    payload = request.get_json(silent=True) or {}
    user.is_admin = bool(payload.get("is_admin", False))
    db.session.commit()
    add_log("admin", f"修改用户角色: {user.username}=>{'管理员' if user.is_admin else '普通用户'}", current_user.id)
    return jsonify({"ok": True})


@app.post("/api/admin/user/<int:user_id>/status")
@login_required
def update_user_status(user_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    user = User.query.get_or_404(user_id)
    payload = request.get_json(silent=True) or {}
    user.is_active_account = bool(payload.get("is_active", True))
    db.session.commit()
    add_log("admin", f"修改用户状态: {user.username}=>{'启用' if user.is_active_account else '禁用'}", current_user.id)
    return jsonify({"ok": True})


@app.post("/api/admin/users")
@login_required
def create_user():
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or len(password) < 6:
        return jsonify({"ok": False, "message": "用户名不能为空且密码至少6位"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"ok": False, "message": "用户名已存在"}), 400
    user = User(
        username=username,
        password_hash=hash_password(password),
        email=(payload.get("email") or "").strip(),
        phone=(payload.get("phone") or "").strip(),
        avatar_url=(payload.get("avatar_url") or "").strip(),
        is_admin=bool(payload.get("is_admin", False)),
    )
    db.session.add(user)
    db.session.commit()
    add_log("admin", f"新增用户: {username}", current_user.id)
    return jsonify({"ok": True})


@app.put("/api/admin/users/<int:user_id>")
@login_required
def update_user(user_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    user = User.query.get_or_404(user_id)
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or user.username).strip()
    duplicate = User.query.filter(User.username == username, User.id != user.id).first()
    if duplicate:
        return jsonify({"ok": False, "message": "用户名已存在"}), 400
    user.username = username
    user.email = (payload.get("email") or "").strip()
    user.phone = (payload.get("phone") or "").strip()
    user.avatar_url = (payload.get("avatar_url") or "").strip()
    user.is_admin = bool(payload.get("is_admin", user.is_admin))
    user.is_active_account = bool(payload.get("is_active", user.is_active_account))
    db.session.commit()
    add_log("admin", f"更新用户信息: {user.username}", current_user.id)
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<int:user_id>")
@login_required
def remove_user(user_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({"ok": False, "message": "不能删除当前登录管理员"}), 400
    username = user.username
    stop_user_camera(user.id)
    close_machine_serial(user.id)
    UserCameraConfig.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    DailyDetectionDuration.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    with runtime_states_lock:
        runtime_states.pop(user.id, None)
    with camera_settings_lock:
        user_camera_settings_cache.pop(user.id, None)
    db.session.delete(user)
    db.session.commit()
    add_log("admin", f"删除用户: {username}", current_user.id)
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/password")
@login_required
def update_user_password(user_id: int):
    if not current_user.is_admin:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    user = User.query.get_or_404(user_id)
    payload = request.get_json(silent=True) or {}
    password = payload.get("password") or ""
    if len(password) < 6:
        return jsonify({"ok": False, "message": "密码长度至少6位"}), 400
    user.password_hash = hash_password(password)
    db.session.commit()
    add_log("admin", f"管理员修改用户密码: {user.username}", current_user.id)
    return jsonify({"ok": True})


@app.get("/api/account/me")
@login_required
def account_me():
    return jsonify(
        {
            "ok": True,
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "email": current_user.email,
                "phone": current_user.phone,
                "avatar_url": current_user.avatar_url,
                "is_admin": current_user.is_admin,
                "created_at": safe_fmt_dt(current_user.created_at),
            },
        }
    )

@app.post("/api/account/profile")
@login_required
def update_account_profile():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or current_user.username).strip()
    duplicate = User.query.filter(User.username == username, User.id != current_user.id).first()
    if duplicate:
        return jsonify({"ok": False, "message": "用户名已存在"}), 400
    current_user.username = username
    current_user.email = (payload.get("email") or "").strip()
    current_user.phone = (payload.get("phone") or "").strip()
    current_user.avatar_url = (payload.get("avatar_url") or "").strip()
    db.session.commit()
    add_log("account", f"用户更新个人信息: {current_user.username}", current_user.id)
    return jsonify({"ok": True})


@app.post("/api/account/password")
@login_required
def update_account_password():
    payload = request.get_json(silent=True) or {}
    old_password = payload.get("old_password") or ""
    new_password = payload.get("new_password") or ""
    if not verify_password(current_user.password_hash, old_password):
        return jsonify({"ok": False, "message": "旧密码错误"}), 400
    if len(new_password) < 6:
        return jsonify({"ok": False, "message": "新密码长度至少6位"}), 400
    current_user.password_hash = hash_password(new_password)
    db.session.commit()
    add_log("account", f"用户修改密码: {current_user.username}", current_user.id)
    return jsonify({"ok": True})


@app.get("/api/history/<int:record_id>")
@login_required
def history_detail(record_id: int):
    r = DetectionRecord.query.get_or_404(record_id)
    if (not current_user.is_admin) and r.user_id != current_user.id:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    try:
        boxes = json.loads(r.boxes_json or "[]")
        if not isinstance(boxes, list):
            boxes = []
    except json.JSONDecodeError:
        boxes = []
    image_file = DETECTION_IMAGE_DIR / Path(r.image_path or "").name
    has_image = bool(r.image_path and image_file.exists() and image_file.is_file())
    return jsonify(
        {
            "ok": True,
            "record": {
                "id": r.id,
                "time": to_bjt(r.detect_time).strftime("%Y-%m-%d %H:%M:%S"),
                "category": r.category,
                "count": r.count,
                "confidence": r.confidence,
                "operator": r.operator_name,
                "operation_type": r.operation_type,
                "note": r.note,
                "has_image": has_image,
                "image_url": url_for("history_image", record_id=r.id) if has_image else "",
                "boxes": boxes,
                "frame_width": r.frame_width,
                "frame_height": r.frame_height,
            },
        }
    )


@app.get("/api/history/<int:record_id>/image")
@login_required
def history_image(record_id: int):
    record = DetectionRecord.query.get_or_404(record_id)
    if (not current_user.is_admin) and record.user_id != current_user.id:
        return jsonify({"ok": False, "message": "forbidden"}), 403
    filename = Path(record.image_path or "").name
    if not filename or not (DETECTION_IMAGE_DIR / filename).exists():
        return jsonify({"ok": False, "message": "该记录没有保存检测图片"}), 404
    return send_from_directory(DETECTION_IMAGE_DIR, filename, conditional=True)

if __name__ == "__main__":
    lan_ip = get_lan_ip()
    print("Local:   http://127.0.0.1:5000")
    print("All NIC: http://0.0.0.0:5000")
    print(f"LAN:     http://{lan_ip}:5000")
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
