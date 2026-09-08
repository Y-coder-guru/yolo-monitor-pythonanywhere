"""把 USERNAME 和项目路径改成你的 PythonAnywhere 用户名后，粘贴到 Web 页面的 WSGI 文件。"""

import os
import sys

USERNAME = "你的PythonAnywhere用户名"
PROJECT_DIR = f"/home/{USERNAME}/pythonanywhere_deploy"

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("SECRET_KEY", "部署前换成一串很长的随机字符")
os.environ.setdefault("DETECTION_RECORD_INTERVAL_SECONDS", "5")
os.environ.setdefault("YOLO_WARMUP", "1")

from app import app as application  # noqa: E402
