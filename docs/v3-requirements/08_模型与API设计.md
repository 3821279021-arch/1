# 08. 模型与 API 设计

## 三种使用方式

### 1. 平台 Key
站长配置，玩家直接使用。

### 2. 用户 BYOK
用户添加自己的 API Key，费用由用户自己的账号承担。

### 3. 临时 Key
只在当前会话/房间使用，不持久保存。

## Credential 数据

保存：
- id
- owner_id
- provider
- base_url
- encrypted_secret
- masked_label
- created_at
- last_verified_at

绝不向前端返回 encrypted_secret 或原始 Key。

## 模型发现

### 支持列表接口
验证成功后获取账号实际可用模型。

### OpenAI-compatible
尝试：
`GET {base_url}/v1/models`

失败则允许手动输入模型 ID。

## 前端示例

OpenAI
状态：已连接
Key：sk-••••••ABCD
可用模型：27

[刷新模型] [更换密钥] [删除]

## 房间分配

每个 AI 座位配置：
- credential
- model_id
- 可选推理/温度参数
- 模型显示名

这样一局可以真正实现：
GPT vs Claude vs Gemini vs Qwen vs DeepSeek ...
