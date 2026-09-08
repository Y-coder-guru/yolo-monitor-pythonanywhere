"""只读统计部署目录占用，不会删除或修改数据。"""

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def readable_size(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


image_dir = BASE_DIR / "instance" / "detection_images"
images = [item for item in image_dir.rglob("*") if item.is_file()] if image_dir.exists() else []

print(f"项目总大小: {readable_size(size_of(BASE_DIR))}")
print(f"数据库大小: {readable_size(size_of(BASE_DIR / 'instance' / 'yolo_monitor.db'))}")
print(f"检测照片: {len(images)} 张，共 {readable_size(sum(item.stat().st_size for item in images))}")
print(f"OpenVINO 模型: {readable_size(size_of(BASE_DIR / 'models' / 'best_openvino_model'))}")
