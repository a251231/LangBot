# 问道签到助手

适用于 LangBot 4.9.9 和 `langbot-plugin 0.3.8`。插件支持手机号短信登录，也兼容粘贴问道社区 App 登录响应或旧版 curl 绑定账号；绑定后可手动查询、签到、补签和查看“问道心动周报”，并自动完成每日签到、每天一次补签、签到里程碑奖励领取和每周周报推送。

## 使用方式

除 `问道帮助` 外，所有命令仅在机器人私聊中执行：

```text
问道登录 <手机号>
问道验证码 <短信码>
问道查询
问道签到
问道补签
问道时间 HH:MM
问道自动 开/关
问道自动补签 开/关
问道自动里程碑 开/关
问道周报
问道自动周报 开/关
问道设置
问道解绑
问道帮助
```

### 手机号登录

手机号登录使用服务器托管的一次性腾讯验证码网址：

```text
问道登录 13800138000
# 机器人返回 http://服务器IP:8788/wendao/captcha/一次性随机值
# 人机验证通过并收到短信后，直接回复：
123456
```

1. 管理员先把 `captcha_public_base_url` 设置为手机浏览器能访问的服务器地址，例如 `http://服务器IP:8788`。
2. 私聊发送 `问道登录 <手机号>`。插件动态读取问道 App 的腾讯验证码 App ID，创建 10 分钟登录会话，并返回一次性腾讯验证码网址。
3. 打开网址完成人工点选。网页只加载腾讯官方 `TCaptcha.js`；成功票据通过随机 nonce 回传插件，插件自动发送短信验证码，并在私聊中通知结果。
4. 收到短信后直接回复短信中的验证码。手机号登录成功后，插件调用签到列表验证新凭据，验证通过才覆盖旧绑定并保留原来的自动开关和执行时间。

`问道验证码 <短信码>`、`问道验证 <randstr> <ticket>` 继续作为高级兼容入口。正常用户只需执行 `问道登录 <手机号>`、打开机器人返回的网址，再直接回复短信中的验证码。插件只在当前私聊用户存在有效登录会话且短信已发送时接收 4 至 8 位纯数字，其他纯数字消息保持为普通聊天。插件不识别或代点验证码图片。

登录请求按 App 契约动态生成 `guestId`、随机 UUID、Android ID 和 OAID；请求头 `guestId` 与短信体 `verificationCode.imei` 始终一致。验证码网址不包含手机号，nonce 在短信发送成功后立即失效且不可重放。手机号、ticket、randstr 和短信验证码不会写入插件存储，登录成功、会话过期或插件进程重载后即从内存清除。

### 登录响应兼容绑定

`问道绑定 <登录响应>` 是高级兼容入口。绑定时，在问道社区 App 完成手机号验证码登录，将登录成功响应的 JSON 正文或包含响应头的完整文本粘贴在 `问道绑定` 后。插件提取并保存 `accessToken`、`refreshToken`、`accessTokenValidTime`、`ltUid` 和绑定角色名，然后调用签到列表接口验证凭据。

```text
问道绑定
{"code":2000,"data":{"ltUid":"LTUID_EXAMPLE","token":{"accessToken":"ACCESS_TOKEN_EXAMPLE","accessTokenValidTime":1784219114121,"refreshToken":"REFRESH_TOKEN_EXAMPLE"},"bindServer":{"name":"角色名"}}}
```

旧登录响应不包含公共设备请求头，该兼容入口固定使用已确认的设备字段：

| 请求头 | 固定值 |
| --- | --- |
| `device` | `2211133C/Android/16` |
| `version` | `2.26.1` |
| `versionCode` | `260604` |
| `guestId` | `1030620167932793` |
| `clientType` | `wd_android` |

插件会先调用签到列表接口验证新凭据，验证成功后才覆盖旧绑定。每次请求重新生成 `timestamp`，并按 App 规则计算签名：

```text
sign = MD5(UTF-8(device + timestamp))
```

插件会在 `accessTokenValidTime` 到期前约 60 秒调用 `PUT https://wdappapi.roguelike.com/api/app/account/token/refresh`，使用已保存的 `refreshToken` 自动换取并原子保存新的 `accessToken`、`refreshToken` 和有效期。普通签到接口首次返回 HTTP 401/403 时，也会自动刷新并将整轮工作流重试一次；刷新凭据缺失或被服务端拒绝时才提示重新登录绑定。

兼容旧版 curl：原有完整签到 curl 仍可用于绑定。解析器只读取 curl 文本，不执行命令，也不保存其中旧的 `timestamp` 和 `sign`。

手动执行 `问道签到` 时，插件会先完成当天签到，并按当前“自动补签”和“自动里程碑”开关继续执行补签、里程碑领取及最终复查。`问道补签` 只显式触发补签分支。

手动执行 `问道周报` 时，插件先通过社区接口换取一次性活动地址，再按当前活动页契约在同一 Cookie 会话中依次调用 `userData`、`page_status` 和 `index`，查询上一周期“问道心动周报”。回复标题固定为“问道周报”，展示角色、区服、周期、登录天数、活跃、刷道、道行增长、精彩时刻和本周签语等服务端已返回内容。

周报的道行增长字段以天为单位，1 年按 360 天换算；例如 `4872` 天显示为 `13年192天`。不足一年只显示天数，整年不追加 `0天`。

## 自动流程

默认时区为 `Asia/Shanghai`，新账号默认每天 `08:00` 执行。自动流程依次查询当天状态、签到、复查、尝试补签、处理可自动完成的阅读任务、复查、领取可领取的里程碑奖励并最终复查。

- 自动补签每天最多请求一次，达到月度补签次数上限后跳过。
- 补签业务码 `4102` 且任务类型为发布动态时，插件只发送 App 操作提示。
- 补签业务码 `4102` 且任务类型为阅读文章、响应包含数字 `sourceId` 时，插件调用 `GET /v2/api/wd_content/post/read_report/{sourceId}?postType=1` 上报阅读，然后只重试一次补签；缺少文章 ID 或第二次仍返回 `4102` 时停止自动尝试并发送提示。
- 里程碑状态为 `GET=3` 时领取，同一轮最多调用一次。
- 网络超时、连接失败和 HTTP 5xx 按 5、15、30 分钟退避；中间重试保持静默，成功或最终失败发送一条私聊通知。
- 访问凭据临期时主动刷新；普通接口首次返回 HTTP 401/403 时刷新后整轮重试一次。
- 刷新请求的网络超时、连接失败和 HTTP 5xx 进入同一套退避；刷新凭据缺失、HTTP 401/403 或刷新业务错误会将账号标记为需要重新绑定。

## 周报流程

- 新账号默认开启自动周报；每周一 `09:00` 按 `Asia/Shanghai` 时区私聊推送上一周周报。
- 手动查询和自动任务以服务端报告周期去重；手动查询已取得待推送周期时，自动任务跳过，插件重启后也不会重复推送。
- 活动页状态为 `1` 时按“周报生成中”重试，状态为 `2` 时提示上周暂无可展示数据，仅在状态为 `3` 时读取周报详情。
- 社区 HTTP 401/403 复用账号 refresh token 自动刷新；活动 token 校验失败时重新换票一次。
- 网络超时、HTTP 5xx 或周报尚未更新时按 5、15、30 分钟静默重试，成功或最终失败只发送一条私聊通知。
- 临时 `sid`、`rid` 和活动 token 只用于当前请求，活动 token 不会写入插件存储或回复；插件当前不自动领取分享奖励。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `timezone` | `Asia/Shanghai` | 调度时区和日期边界 |
| `default_schedule_time` | `08:00` | 新绑定账号默认执行时间 |
| `request_timeout_seconds` | `15` | 单次 HTTP 请求超时 |
| `scheduler_poll_seconds` | `30` | 后台调度扫描间隔 |
| `max_concurrency` | `3` | 同时执行的账号上限 |
| `login_session_ttl_seconds` | `600` | 手机号登录短时会话有效期，范围 60 到 1800 秒 |
| `captcha_public_base_url` | 空 | 手机浏览器可访问的验证码服务地址，例如 `http://服务器IP:8788`；留空时保留手工票据入口但不启动网页服务 |
| `captcha_bind_host` | `0.0.0.0` | 验证码 HTTP 服务监听地址 |
| `captcha_bind_port` | `8788` | 验证码 HTTP 服务监听端口 |
| `default_auto_signin` | `true` | 新账号默认自动签到 |
| `default_auto_resign` | `true` | 新账号默认自动补签 |
| `default_auto_milestone` | `true` | 新账号默认自动领取里程碑 |
| `default_auto_weekly_report` | `true` | 新账号默认每周一 09:00 自动推送周报 |

局域网部署可直接使用 LangBot 所在服务器的局域网 IP，并在系统防火墙开放 TCP `8788`。跨网络部署建议让现有 HTTPS 反向代理把 `/wendao/captcha/` 转发到插件运行环境的 `8788` 端口，再把 `captcha_public_base_url` 设置为公开 HTTPS 地址。

LangBot 使用 Docker 时，验证码服务实际运行在插件运行时容器中，需要把容器 TCP `8788` 映射到宿主机，或由反向代理直接访问该容器端口。服务器地址必须从用户打开链接的手机可达；`127.0.0.1` 只适用于浏览器与插件在同一台机器的情况。

## 凭据安全

插件按 `bot_uuid + sender_id` 为每位聊天用户保存一个账号，存储键为身份摘要，值使用 LangBot 官方插件存储保存原始凭据 JSON。该数据未额外加密，数据库备份可直接读取 token。请限制 LangBot 数据库与备份的访问权限，并设置合理的备份保留周期。

绑定成功后，请立即删除聊天记录中的绑定消息和本次手机号登录相关消息，避免登录响应、access token、refresh token、手机号、ticket 或短信验证码长期保留在聊天历史中。插件日志、异常和正常回复不会回显这些敏感字段。

## 开发验证

```powershell
$env:PYTHONPATH='D:\code\LangBot\plugins\wendao_signin_plugin'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests -q
D:\code\LangBot\.venv\Scripts\python.exe -m ruff check plugins\wendao_signin_plugin

Set-Location D:\code\LangBot\plugins\wendao_signin_plugin
D:\code\LangBot\.venv\Scripts\lbp.exe build -o dist
```
