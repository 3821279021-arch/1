# 技术架构与验收标准

## 推荐分层

``` text
Mobile Web / PWA
      │
 WebSocket + HTTP
      │
Room / Session Manager
      │
Turn Scheduler / Server Clock
      │
Rule Engine
      │
InformationScope / PlayerView
      │
AI Orchestrator ── OpenAI / Claude / Gemini / Mock
      │
Pet AI Service
      │
Persistence (SQLite/PostgreSQL; Redis-ready)
```

## 三类频道

### Public Channel

所有活跃玩家可见的合法公开信息。

### Faction Channel

例如狼队夜间频道，仅对应阵营可见。

### Private Pet Channel

仅主人 + 主人的 AI 宠物可见。

三个频道必须在服务端隔离，不只是前端隐藏。

## 手机 UI

主要页面建议： - 顶部：Day、白天/黑夜、阶段、当前玩家、倒计时； -
中部：圆桌/半圆玩家座位； - 发言区域：当前玩家实时发言； -
历史区域：公开聊天； - 底部：发言、投票、角色技能、🐾 AI 搭档； -
夜晚整体视觉变暗； - 轮到真人时显示明显操作面板； - 触控目标适合手机。

## 核心验收

### 房间

-   两个浏览器创建两局，不串状态。
-   room_id/game_id 独立。

### 权限

-   村民 AI 请求上下文时不存在其他隐藏身份。
-   狼队频道不会发送给非狼人客户端。
-   宠物拿不到主人无权知道的数据。

### 回合

-   每个玩家独立发言回合。
-   current_player 正确推进。
-   deadline 在服务器保存。
-   刷新页面倒计时不会重置。

### AI

-   AI 发言逐个出现。
-   支持流式时逐 chunk 展示。
-   AI 动作必须经过 Rule Engine。
-   AI 输出非法 JSON 时系统可恢复。
-   API 失败可重试/fallback。

### 真人

-   真人可以提前结束发言。
-   超时自动处理。
-   投票和技能只能在合法阶段提交。

### 宠物

-   别人发言时可以同时和宠物聊天。
-   宠物聊天不会暂停游戏。
-   代打/托管必须由真人明确开启。
-   取消托管后控制权返回真人。

### 恢复

-   WebSocket 断开重连后收到 state snapshot。
-   页面刷新恢复当前阶段和 deadline。
-   持久化后的游戏不会因为普通进程状态丢失而立即消失。

### Mock 模式

无任何第三方 API Key 时： - 能创建房间； - 能开始； - AI 能逐个发言； -
能完成夜晚、投票、角色技能； - 能完整结束一局。

## 不要做的事情

-   不要把完整隐藏 GameState 塞给所有 AI。
-   不要让前端成为倒计时权威。
-   不要让 AI 直接修改游戏状态。
-   不要一次性生成所有 AI 发言再同时显示。
-   不要为了 UI 美化先牺牲房间、权限、实时回合等核心架构。
