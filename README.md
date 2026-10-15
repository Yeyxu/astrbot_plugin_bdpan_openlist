# 百度网盘转存 OpenList 分享

适用于 **AstrBot >=4.16,<5**。自动识别私聊、群聊中的百度网盘链接，使用配置的百度账户转存，然后刷新 OpenList 挂载目录并返回分享链接。支持 `/bdpan` 手动触发、分享失败降级、静态 Token 和 TOTP 两步验证。

## 安装

1. 将本目录放入 `AstrBot/data/plugins/astrbot_plugin_bdpan_openlist/`。`main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` 必须位于插件目录第一层。
2. 在 **AstrBot 使用的 Python 环境**安装依赖：

   ```bash
   python -m pip install -r data/plugins/astrbot_plugin_bdpan_openlist/requirements.txt
   ```

3. 在 AstrBot WebUI 的插件管理中加载插件，填写配置，保存并重载插件。
4. 若发布到自己的 Git 仓库，将 `metadata.yaml` 的 `author` 和 `repo` 改为真实信息；当前 `your_name` 为题目指定的元数据模板，不是可安装的现成仓库地址。

目录结构：

```text
astrbot_plugin_bdpan_openlist/
├── metadata.yaml
├── _conf_schema.json
├── main.py
├── requirements.txt
└── README.md
```

## 必填配置与路径

| 配置 | 含义 |
| --- | --- |
| `baidu_cookie` | 登录 `pan.baidu.com` 后，从浏览器开发者工具的请求头复制完整单行 Cookie 值，必须含非空 `BDUSS` 和 `STOKEN`。不带 `Cookie:` 前缀。 |
| `baidu_save_path` | 百度网盘自身的保存根目录，默认 `/AstrBot转存`，不存在时逐级创建。 |
| `openlist_url` | OpenList 完整服务地址，默认 `http://127.0.0.1:5244`；应填写用户可打开的地址。支持 `https://example.com/openlist` 这样的代理子路径。 |
| `openlist_username` | 默认 `admin`；需要读取、强制刷新目录和创建分享权限。 |
| `openlist_password` | 原始登录密码。也可使用下面的静态 Token 免登录。 |
| `openlist_save_path` | OpenList 中与百度保存根目录一一对应的路径，默认 `/baidu/AstrBot转存`。 |

例如，OpenList 将同一个百度账户挂载于 `/baidu`，驱动根目录是 `/`，则百度 `/我的资源/转存目录` 对应 OpenList `/baidu/我的资源/转存目录`。若驱动根目录是 `/我的资源`，则对应 OpenList `/baidu/转存目录`。两个配置必须指向**同一个账户、同一个实际目录**。

Docker 中 `127.0.0.1` 是 AstrBot 容器自身，OpenList 在另一个容器时请填写可达的容器服务名或宿主机地址。返回的链接也使用此地址。

## 其他配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_auto_detect` | `true` | 自动识别普通消息；关闭后 `/bdpan` 仍可用。 |
| `enable_private` / `enable_group` | `true` | 控制私聊/群聊，自动与手动触发均遵循。 |
| `openlist_token` | 空 | 静态管理员 Token，优先于用户名密码，不带 `Bearer `。无效时明确报错。 |
| `openlist_otp_key` | 空 | 登录时使用的 Base32 TOTP 密钥，插件生成 `otp_code`；不是六位临时验证码。静态 Token 模式不使用它。 |
| `openlist_directory_password` | 空 | 挂载目录的访问密码，用于 `fs/list` 与 `fs/get`。 |
| `openlist_share_pwd` | 空 | 新建分享的提取码；留空无密码。兼容程序配置中的旧名称 `share_pwd`，WebUI 请使用新名称。 |
| `share_max_accessed` | `0` | 分享最大访问次数，0 不限制，服务端决定统计口径。 |
| `openlist_share_prefix` | `/s/` | 分享前端路径前缀。`/sd/` 是部分版本的下载路由，仅在部署确实支持时使用。 |
| `openlist_share_api_format` | `files` | 默认发送 `files/pwd/max_accessed`；文档格式模式发送 `paths/password`，且仅允许访问次数为 0。 |
| `output_mode` | `share` | `share` 创建分享，失败自动降级；`direct` 直接获取 `raw_url`，目录返回前端入口。 |
| `max_links_per_message` | `3` | 范围 1～10，超出明确提示，同消息相同分享去重。 |
| `max_files_per_share` | `1000` | 顶层条目上限，范围 1～10000；超过则不转存，绝不静默遗漏后续页。 |
| `http_timeout` | `30` | 单个 HTTP 请求超时秒数，范围 5～120。 |
| `task_timeout` | `120` | 百度异步转存任务等待秒数，范围 10～600。 |

敏感配置使用 `secret: true`；AstrBot 只遮罩管理界面，不加密磁盘配置。所有可触发本插件的成员都使用配置的百度账户；如需限制范围，可结合 AstrBot 自身的会话及插件权限设置。

## 使用

直接发送整段分享文本即可，例如：

```text
https://pan.baidu.com/wap/init?surl=6m1QkJQGpA3TGgZER4GMQQ&pwd=6666
https://pan.baidu.com/wap/init?surl=6m1QkJQGpA3TGgZER4GMQQ 提取码:6666
https://pan.baidu.com/s/16m1QkJQGpA3TGgZER4GMQQ?pwd=6666
https://pan.baidu.com/s/16m1QkJQGpA3TGgZER4GMQQ 提取码： 6666
```

支持 `提取码`、`密码`、中英文冒号、空格及紧随链接的四位字母数字密码；也支持 `pan.baidu.com/init?surl=...`、不带协议的链接和 HTML 转义的 `&amp;`。`/s/1xxxx` 只移除第一个 `1`，`init?surl=xxxx` 不删任何字符。多链接消息中，密码绑定其前面的链接，链接自带 `pwd` 优先。

手动指令：

```text
/bdpan https://pan.baidu.com/s/16m1QkJQGpA3TGgZER4GMQQ 6666
/bdpan https://pan.baidu.com/wap/init?surl=6m1QkJQGpA3TGgZER4GMQQ 提取码：6666
```

指令前缀由 AstrBot 的唤醒配置决定。自动识别需要平台适配器实际将该消息交给 AstrBot，不能绕过平台的群消息接收限制。未匹配链接的普通消息直接放行；同一个事件同时命中命令和监听器时仅处理一次。

## 转存与分享行为

1. 提前登录 OpenList，缓存 Token。HTTP 401 或 JSON `code: 401` 时重新登录并重试一次；静态 Token 无效时不尝试密码登录。
2. 读取百度分享页，必要时调用 `share/verify`，将解码后的 `randsk` 写入本次会话的 `BDCLND`，重新读取 `shareid/share_uk`，获取登录态 `bdstoken`。只有百度请求携带百度 Cookie。
3. 分页读取全部顶层 `fs_id` 与文件名。逐级创建保存根目录，然后创建形如 `bdpan_20261016T010203Z_12位随机标识` 的本次独立目录，时间使用 UTC。
4. 调用 `share/transfer`，使用 `ondup=newcopy&async=1`。有 `taskid` 时轮询 `share/taskquery`，检查异步任务和每个条目的错误，再确认目标文件已落盘。没有 `taskid` 的同步响应也必须确认文件实际存在。
5. 使用 `refresh: true`、`per_page: 0` 强制刷新 OpenList 保存根目录及本次子目录，校验完整文件名与目录类型。整个同步过程最多 **3 次尝试**，失败间隔 **2 秒**；不使用同名旧文件替代新文件。
6. 将本次转存的全部顶层文件/文件夹路径作为一个 `files` 数组创建分享，返回分享链接与可选提取码。创建失败或选择 direct 模式时，文件优先返回 `fs/get.raw_url`；目录或无法取直链的文件返回经过 URL 编码的前端入口。

为控制消息长度，名称及直链最多展开前 20 项，其余通过本次目录入口查看；正常分享仍包含全部条目。降级链接不受新建分享的密码、访问次数限制；原目录入口可能需要用户登录，百度原始直链可能会过期或需要客户端请求头，插件不会将账户 Cookie 发给聊天用户。

任务在插件内串行执行，复用两个独立的异步 HTTP 连接池，避免并发分享的 `BDCLND` 串用。重载/卸载时取消本地等待并关闭连接池；已经提交到百度后台的任务可能继续执行。不同消息之间不会永久去重，重新发送链接会创建新目录。

## 常见问题

- **Cookie 失效、验证码/风控**：先在浏览器登录该账户并打开分享页，完成网站要求的验证，再更新完整 Cookie。插件不会绕过验证码。
- **提取码错误、分享取消**：检查原链接及四位提取码。服务端未知错误会保留错误码与脱敏后的提示。
- **转存数量/空间限制**：百度会限制文件夹内总文件数，顶层只有一个文件夹也可能触发；减少文件数、分批转存或检查剩余空间。
- **转存超时或部分失败**：回复会给出本次百度保存目录，先检查是否已有文件落盘。写入请求不会自动重发，避免重复转存；空目录或部分结果也不会自动删除。
- **百度成功但 OpenList 没找到文件**：检查账户、挂载根目录、两个保存路径的对应关系，以及百度驱动刷新是否正常。回复保留百度保存目录，修复挂载后可手动分享，无需再次转存。
- **OpenList 登录/分享失败**：检查凭据、2FA 密钥和服务器时间，以及账户创建分享权限。仅登录成功不代表拥有分享权限。404 可能表示服务端版本不支持 Sharing API，此时降级为直链/入口。
- **分享 API 字段差异**：指定的 Apifox 文档当前示例为 `paths/password`，而 OpenList 当前 `server/handles/sharing.go` 使用 `files/pwd/max_accessed`。本插件默认与源码、题目规范一致；特殊版本可切换格式，不会在创建超时后自动改格式再创建。

## 接口依据与验证范围

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 事件监听](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot 配置 Schema](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [OpenList API 文档](https://fox.oplist.org/)
- [OpenList 创建分享文档](https://fox.oplist.org/364155757e0)
- [OpenList Sharing API 源码](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/sharing.go)

本实现根据上述官方资料及题目提供的百度 Web API 规范独立编写。随交付附带的工作区测试使用 `httpx.MockTransport` 和 AstrBot 接口替身，验证解析、分页、Cookie 隔离、异步任务、Token 过期重登、刷新重试、降级与事件生命周期。真实百度账户、实际 AstrBot 平台和 OpenList 服务仍需使用你自己的配置进行一次联调；百度未公开的 Web API 可能变更。
