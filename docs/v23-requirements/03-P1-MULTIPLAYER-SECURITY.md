# P1：多人体验与安全

## 1. 房主控制

建议加入：

- 踢出玩家
- 锁定房间
- 禁止新玩家加入
- 可选房间密码
- 重新开放座位

原因：邀请链接一旦被转发，现在房主缺少处理陌生占座的能力。

---

## 2. 身份恢复

当前匿名 token 保存在浏览器 localStorage，清缓存/换设备后恢复能力较弱。

建议至少提供一种：

### 方案 A：恢复码

加入房间后显示一次：

```text
恢复码：ABCD-1234
```

用户换设备后可以恢复原座位。

### 方案 B：轻账号

允许可选绑定账号，但不要强迫所有试玩用户注册。

---

## 3. Token 生命周期

增加：

- revoke
- rotation
- 主动退出
- 房间结束后的 token 降权/失效
- 服务端 session 状态

避免完全依赖本地 token 长期有效。

---

## 4. 房间级限流

除了全局 API 限流，建议对：

- join
- chat
- action
- reconnect
- model test

增加 room/session 维度限制。

---

## 5. 浏览器安全

建议增加安全响应头：

```text
Content-Security-Policy
X-Content-Type-Options: nosniff
Referrer-Policy
Permissions-Policy
```

同时继续保持所有玩家昵称、聊天、模型内容在插入 HTML 前转义。

---

## 6. WebSocket 恢复

断线重连建议明确采用：

```text
客户端最后 event_id
→ 重连
→ 服务端补发差异 / 发送权威快照
```

避免仅靠客户端本地状态猜测。

验收：

- 手机锁屏 30 秒再回来可以恢复
- Wi-Fi / 蜂窝网络切换可以恢复
- 重连不会重复执行 action
