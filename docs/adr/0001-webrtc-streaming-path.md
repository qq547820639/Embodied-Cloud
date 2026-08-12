# ADR 0001: Isaac Sim WebRTC Streaming 路径

状态：Accepted（2026-08-12）

## 背景
浏览器需实时显示 Isaac Sim 仿真。曾考虑自研视频协议或通用 WebRTC 网关。

## 决策
1. **不重新发明视频协议**：优先 Isaac Sim 官方 WebRTC 路径（LIVESTREAM + signaling/media 端口）。
2. 控制面只管理 `StreamingSession` 状态机（starting/ready/connected/disconnected/failed）；媒体面由 workspace 内 simulator 提供。
3. 单机 MVP 采用固定端口（49100/TCP + 47998/UDP），同宿主同时只允许 1 个 streaming workspace。
4. Streaming 与 Workspace 生命周期隔离：workspace stop → streaming session 终止并清理端口。

## 后果
- 多实例 streaming 需验证后的 gateway（未来工作），本轮不做。
- 端口清理与状态恢复由 streaming service 负责并有测试覆盖。
