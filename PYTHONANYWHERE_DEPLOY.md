# PythonAnywhere 部署说明

这个目录是从源项目复制出来的部署副本。原数据库和 267 张历史检测照片已经保留；`.git`、缓存、开发环境、测试文件和 `best.pt` 没有放进来。

## 已做的空间优化

- 检测模型改为直接调用 OpenVINO，并用 Pillow 处理图片，不再安装 PyTorch、Ultralytics 和 OpenCV。
- 数值检测记录仍然每 5 秒最多保存一次。
- 达到原有置信度条件时，实时检测照片也保持每 5 秒最多保存一张。
- 手动上传图片检测时仍会正常保存检测照片。
- 没有自动删除历史数据库或历史照片。

可在 PythonAnywhere Bash 控制台运行以下命令查看当前占用：

```bash
cd ~/pythonanywhere_deploy
python storage_report.py
```

## 上传后执行

假设上传后的目录为 `/home/你的用户名/pythonanywhere_deploy`：

```bash
cd ~/pythonanywhere_deploy
mkvirtualenv --system-site-packages --python=/usr/bin/python3.13 yolo-monitor
python -m pip install --upgrade pip
pip install --no-cache-dir -r requirements_pythonanywhere.txt
rm -rf ~/.cache/pip
python storage_report.py
```

然后在 PythonAnywhere 的 **Web** 页面新建 Web App，选择 **Manual configuration** 与 Python 3.13，并填写：

- Source code: `/home/你的用户名/pythonanywhere_deploy`
- Working directory: `/home/你的用户名/pythonanywhere_deploy`
- Virtualenv: `/home/你的用户名/.virtualenvs/yolo-monitor`

打开该 Web App 的 WSGI 配置文件，把 `pythonanywhere_wsgi.py` 的内容复制进去，并将其中的用户名和 `SECRET_KEY` 改掉。最后点击 **Reload**。

网站地址通常是：

```text
https://你的用户名.pythonanywhere.com
```

免费账户需要按 PythonAnywhere 页面提示定期续期 Web App。续期不会要求删除数据库或照片，但仍应定期下载备份 `instance` 文件夹。

如果上传的是 `pythonanywhere_deploy.zip`，先在主目录 Bash 控制台运行：

```bash
cd ~
unzip pythonanywhere_deploy.zip
rm pythonanywhere_deploy.zip
```

解压后马上删掉 ZIP，可以避免同一份文件占用两次空间。若 Web 页面没有 Python 3.13，就在创建 Web App 和虚拟环境时选择页面提供的同一个 Python 版本。
