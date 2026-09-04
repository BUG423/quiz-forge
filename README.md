# 本地音视频转文档

简单的离线工作流：

```text
选择多个音视频 → 本地依次转写 → 分别复核 → 单独导出或合并导出
```

## 已实现

- `faster-whisper-small` 模型从项目本地目录加载；
- 服务启动后强制启用 Hugging Face / Transformers 离线模式；
- 支持常见音频和视频格式，媒体解码由本地 PyAV/FFmpeg 库完成；
- 多文件按顺序处理，避免并发耗尽显存或内存；
- 每个文件有独立的可编辑结果和复核确认；
- 可以逐份导出 TXT，也可以合并为一个 TXT；
- 不使用数据库，上传临时文件在处理结束后删除。

## 启动

模型默认位于：

```text
models/faster-whisper-small/
```

启动本地服务：

```bash
chmod +x run-local.sh
./run-local.sh
```

访问 <http://127.0.0.1:8080>。

## 接口

```http
GET /api/health
POST /api/transcribe
Content-Type: multipart/form-data
file: <audio-or-video>
```

返回包含完整文字、时长、分段置信度、本地处理耗时和使用的模型名称。

## 测试素材

本地测试使用 Wikimedia Commons 的两段普通话视频，文件放在：

```text
test-data/VOA 中国官方媒体为逮捕艾未未辩解.ogv
test-data/VOA 上海为罢工卡车司机削减收费.ogv
```

该视频页面：<https://commons.wikimedia.org/wiki/File:VOA_中国官方媒体为逮捕艾未未辩解.ogv>

运行文档导出测试：

```bash
node tests/test-document-export.js
```

测试会在 `test-data/exports/` 生成两份单独文档和一份合并文档。
