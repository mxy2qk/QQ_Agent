# NapCat / OneBot 11 接入

本项目采用独立普通 QQ 账号 + NapCat/OneBot 11。它是非官方接入，使用者需接受平台兼容性和账号限制风险，并在授权群完成成员告知。

## NapCat

按 [官方 Shell 安装说明](https://napneko.github.io/guide/boot/Shell)下载安装。当前根目录启动脚本指向 `napcat/v4.18.42/launcher.bat`；更新版本时同步修改脚本路径。本仓库不分发 NapCat、QQ 或登录数据。

在 NapCat 中创建 WebSocket **服务端（正向 WS）**，监听 `127.0.0.1:3001`，消息格式选 `array`，设置强令牌并启用。根路径 `/` 同时支持事件和动作；不是 WebUI 管理端口，也不是 `/api` 或 `/event` 单功能路径。

## 本地配置

复制 `.env.example` 后填写自己的信息；以下为格式模板，不是可直接使用的账号配置：

```dotenv
QQ_ADAPTER_MODE=onebot
QQ_WS_URL=ws://127.0.0.1:3001/
QQ_ACCESS_TOKEN=填写与NapCat网络服务相同的令牌
QQ_SELF_ID=填写机器人QQ号
QQ_ALLOWED_GROUPS=["填写授权群号"]
QQ_ALLOWED_USERS=["填写授权好友QQ号"]
QQ_PRIVATE_ENABLED=true
QQ_LIVE_ENABLED=false
```

白名单用 JSON 字符串数组，空列表不允许任何对应对象。临时会话不处理，私聊双方需为好友。明文 WS 仅允许本机回环，远程使用 WSS。访问令牌与 WebUI 登录令牌是不同配置；不把令牌放在 URL 或提交到 Git。

## 启用与验证

先登录 NapCat，并确认授权与告知，再将 `QQ_LIVE_ENABLED=true`。关闭占用同一数据库的其他 Agent 后执行：

```powershell
.\.venv\Scripts\qq-agent.exe --qq
```

程序先核验 `get_login_info`，再处理白名单群的实际 @消息和白名单好友私聊。非@群消息不交给 Agent 或保存。没有自动加好友、入群、群管理或主动推送行为。

可由群管理者发布告知：仅处理明确@的文字消息；原始消息及会话保留7天，明确记忆独立保存；个人记忆请私聊管理，群共享使用 `/remember-group`；用 `/forget ID` 删除自己的记忆，`/reset` 清除近期会话。

## 测试

使用者可在群内@后发送 `/help`，或在好友私聊发送 `/help`，查看按当前场景筛选的命令说明。`/help 命令名` 查看详细解释。完整使用说明见 [OPERATIONS.md](OPERATIONS.md)。

1. 非@消息、其他群、非白名单用户应不回复。
2. 群@与好友私聊正常回复，昵称变化不影响身份。
3. 私聊保存的 private/global 信息不用于群公开回答。
4. 明确创建的群记忆可供同群成员读取，其他群不可访问。
5. 重启 Agent 后会话与记忆恢复；关闭/启动 NapCat 后自动重连。
6. 结果不确定的旧消息不重复发送。断线期间的消息不保证补发，以重连后新消息验证。

回复仅为纯文本，上限2000字符，CQ外观不转为@全体或其他动作。默认队列64条、目的地/全局发送间隔3秒、ACK超时15秒、重连退避1–30秒。

后台进程信息及重定向日志在本地 `data/run/`，结构化日志在 `logs/`。这些目录、账号配置及登录数据均排除版本控制。一次只运行一个使用同一数据库的 Agent。

参考：[OneBot WS](https://github.com/botuniverse/onebot-11/blob/master/communication/ws.md)、[NapCat 配置](https://napneko.github.io/config/basic)。
