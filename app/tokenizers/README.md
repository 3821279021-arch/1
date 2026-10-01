# Qwen 文本 Token 词表

`qwen2-vocab.json` 来自 [Qwen/Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct)，仅包含公开的 Token 词表，不包含模型权重。原始许可证见 `LICENSE-QWEN.txt`（Apache 2.0）。

来源：`https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/resolve/main/vocab.json`

SHA-256：`ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910`

`app/tokens.py` 用 Qwen2/3 的 ByteLevel 字节映射、分词正则和 tiktoken BPE 在本地计数，启动后无需网络下载。DashScope 的消息封装另加预留，并根据实际 Usage 校准；服务端别名若切换到不同分词器，计数仍按实际 Usage 校准。未知模型继续使用经验估算。
