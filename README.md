# 云巡 · 阿里云监控 Docker 网页版

把原来的 `config.json`、监控脚本和企业微信通知配置，改为一个简体中文工作台。一个管理员，可以管理多个阿里云账号、多台 ECS 实例；核心配置必填，企业微信和 Bark 均可选。默认只监控，不自动启停。

## 一、先看这几条

- 宿主机只需要 Docker 20.10.10 或以上，不需要安装 Python、sudo 或 Docker Compose。CentOS 7 的 root 用户直接执行下面的 `sh install.sh`。
- 安装向导只创建本应用容器和数据卷，不修改宿主机 Python、不改防火墙、不停止其他容器或原脚本。
- 首次构建需要访问 Docker Hub 和 PyPI。中国大陆网络可能需要你已有的合规镜像源；向导不会自动改 Docker 配置。
- CentOS 7 已于 2024-06-30 结束维护。容器不是宿主机安全更新的替代品，建议规划升级。[CentOS 官方公告](https://www.centos.org/centos-linux-eol/)
- 已在本地 Docker 20.10.16 / Linux 容器测试。不是在你的真实 CentOS 7 服务器内测试，不保证所有历史内核和 Docker 发行版均兼容；详见 `TEST_REPORT.md`。

## 二、一步一步部署

从 GitHub 获取项目，也可以在仓库页面选择 Code → Download ZIP，解压后进入项目目录执行 `sh install.sh`。使用 Git 时：

```sh
git clone https://github.com/FlamingYouth/aliyun-monitor-web.git
cd aliyun-monitor-web
sh install.sh
```

仓库不含真实密钥、通知地址、运行数据或 `.env`；安装时生成私有初始化口令，阿里云参数和管理员密码由你在网页中填写。测试目录中的凭据均为明确标注的虚构数据，不是正式部署的默认密码。`TEST_REPORT.md` 和 `aliyun-final-image-parity-1.1.0.json` 记录本次双架构验证结果。

将 `aliyun-monitor-web-1.1.0.tar.gz` 上传到 CentOS 7，例如 `/opt`，执行：

```sh
tar -xzf aliyun-monitor-web-1.1.0.tar.gz
cd aliyun-monitor-web
sh install.sh
```

向导顺序：检查 Docker → 选择仅本机/对外访问 → 设置端口 → 输入 `yes` → 构建并启动 → 显示网页地址和初始化口令。

推荐选择“仅本机访问”。在你自己的电脑建立隧道：

```sh
ssh -L 8088:127.0.0.1:8088 root@服务器IP
```

保持该连接，在电脑浏览器打开 `http://127.0.0.1:8088`。这样远程访问走 SSH 加密通道，无需将后台暴露到公网。端口自行替换为部署时选的端口。

选择“对外访问”时，地址是 `http://服务器IP:端口`。安全组只放行你的可信 IP；长期公网使用应放在 HTTPS 反向代理后。不要在无 HTTPS 的公共网络传输账号密钥。程序不会自动开放任何服务器端口。

使用自己控制的 HTTPS 反向代理时，将后端端口保持仅监听 `127.0.0.1`，代理传递原始 `Host` 和 `X-Forwarded-Proto: https`，在 `.env` 添加 `TRUST_PROXY=1` 后重建本应用容器使环境生效。Compose 已支持该变量；默认是 0。不要在直接暴露后端端口的情况下信任外部传来的代理头。此选项确保 HTTPS 来源校验和 Secure 会话 Cookie 正常工作。

首次网页配置分五步：

1. 输入部署脚本显示的初始化口令，创建管理员；密码至少 12 个字符。
2. 填写账号备注、国内/国际站、AccessKey ID 和 Secret；可点击“测试账号权限”，该测试只读。
3. 填写实例备注、区域代码、ECS 实例 ID、关机阈值和流量额度；可测试实例连接。资源组可选。
4. 设置巡检间隔、日报时间、时区和通知。企业微信/Bark 不必开启。测试发送会向你填写的真实目标发送一条消息，但不会自动保存。
5. 核对配置并进入总览。建议保留“只监控”，先手动巡检和生成日报确认数据，再到“调度与任务”决定是否启用自动止损。

忘记初始化口令，可在服务器读取：

```sh
docker exec aliyun-monitor-web python -c "from pathlib import Path; print(Path('/data/setup.token').read_text())"
```

该口令仅用于首次初始化。已初始化后不能重新创建管理员。

## 三、阿里云权限与数据口径

建议使用 RAM 子账号，按实际资源范围授权，不用主账号密钥。程序使用的 API：

| 用途 | API / 权限动作 |
| --- | --- |
| 账号 CDT 累积流量 | `cdt:ListCdtInternetTraffic` |
| ECS 状态/区域内实例发现 | `ecs:DescribeInstances` |
| 余额/本月账号账单 | `bssapi:QueryAccountBalance`、`bssapi:QueryBillOverview` |
| 手动启停或启用自动控制需要 | `ecs:StopInstance`、`ecs:StartInstance` |

RAM 支持的资源粒度及条件请以阿里云对应服务当前权限说明为准。界面的“测试权限”只检查读取；不会为了测试启停权限而实际关机。缺少启停权限时真实动作会失败并记入事件日志。

重要：保持原脚本的 CDT 统计逻辑，汇总 `ListCdtInternetTraffic` 返回的 `TrafficDetails[].Traffic`，按 `1024³` 换算为 GB（实际上为 GiB 口径）。这是账号级 CDT 用量，不是某台 ECS 的独立出站流量，也不能用来精确分摊机器费用。同账号的所有监控实例共享这份流量；总览汇总会按账号去重。界面“额度”用于展示和校验阈值，不代表已向阿里云购买的配额。

账单为当前月份账号汇总，余额/账单随日报任务刷新；没有查询结果时显示“待查询”，不会误显示 0。数据来自云 API，存在延迟；本系统不是计费上限保证。没有 CDT 数据或 API 权限异常时会停用这一轮自动决策，不能把查询失败当作 0 GB。

## 四、手动启停与有条件恢复（1.1.0）

- 总览、监控实例和实例详情都有“开机 / 普通关机”。需要登录、确认目标并输入完整 ECS 实例 ID；不能只点一次就关机。手动按钮是明确的真实操作，**不受“只监控”或“暂停自动监控”拦截**；自动任务仍受这些开关限制。
- 所有程序关机（手动或自动止损）都明确发送 `StopInstance` + `StoppedMode=KeepCharging` + `ForceStop=false`：普通、非强制停机，保留资源并继续计费，不做节省停机。程序仅允许 `StartInstance` / `StopInstance`，没有删除实例、删除云盘或创建替代实例的调用。[阿里云 StopInstance 参数说明](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-stopinstance)
- 手动关机的“保持关闭”标记在请求前保存，即使网络超时也不自动拉起；通过本页面手动开机成功后解除。启用自动控制时，手动开机还会检查最新 CDT 用量，超限或查询失败时拒绝启动。
- 默认 `只监控`：自动任务记录流量、状态和预警，不发启停请求。关闭只监控后，流量达到阈值且实例 `Running`、无云端锁定时才自动普通关机。
- 止损恢复：流量低于阈值、实例 `Stopped`、启用“自动恢复由本系统止损关闭的实例”、有本系统已确认的关机标记、无手动保持关闭和云端锁定时才启动；重试至少间隔 30 分钟。
- 抢占恢复是独立的**默认关闭**选项。在“编辑监控策略”中开启后，还必须关闭全局只监控并保持策略未暂停。只在巡检曾观察到原实例 `OperationLocks.LockReason=Recycling`、实例为抢占式且中断行为是 `Stop`、之后仍是同一个 `Stopped` 实例并且 `StoppedMode=StopCharging`、无其他云端锁定、流量低于阈值、无手动保持关闭或止损优先标记时才尝试开机。
- 抢占恢复不保证成功：库存不足、报价不足、权限不足或网络异常都可能失败。请求前保存重试时间，至少间隔 5 分钟再次尝试；没有创建新实例、恢复已删除实例或重建数据的功能。云平台真正释放实例及随实例释放的磁盘时，本程序无法挽回。[抢占实例数据保留说明](https://help.aliyun.com/zh/ecs/user-guide/preemptible-instance-data-retention-and-data-recovery-after-interrupt-reclamation/)
- 抢占预告可能只有约 5 分钟，建议巡检间隔不大于 300 秒；仍可能错过，**未观察到可信回收标记时保持关闭，不猜原因**。监控必须部署在另一台持续在线的机器上，否则自身停机后无法启动自己。云控制台外部手动关机前请先暂停本策略，避免已有待恢复标记与外部操作冲突。
- 暂停策略后不查询或自动控制该实例。`Starting/Stopping/Unknown`、查询失败、实例不存在时不提交启停请求。按钮遇到云端锁定也拒绝操作。
- 启停请求成功只是云 API 接受请求，下次巡检才确认最终状态。手动重复尝试至少间隔 60 秒，已经是目标状态时不重复提交。连续三次巡检失败可发送异常通知。
- 启停请求遇到网络超时可能已被云端接受，但本系统无法确认。此时日志会提示到云端核实；没有确认成功的关机不会设自动恢复标记，请人工处理，不保证超时等同于没有发生动作。
- 超限提醒成功后冷却 24 小时，异常提醒成功后冷却 1 小时。通知失败不会算作成功，也不会占用成功冷却标记。
- 定时日报每日一次；失败请在任务页查看并手动重试。未开通知时仍会生成网页日报。只有一个调度进程，不要将 Gunicorn 改成多个 worker，也不要同时启动多份程序共享数据。
- 本监控不替代你的业务健康检查。云 API 延迟、凭据过期、网络故障、系统宕机可能导致止损未能及时执行。

## 五、网页中的其他功能

监控策略增删改、暂停/恢复、区域发现实例、账号读取权限测试、通知测试发送、手动巡检/日报、任务结果详情、流量历史、事件日志、管理员改密、旧配置导入、无密钥配置导出、完整备份恢复。布局支持手机窄屏。

1.1.0 修复首次配置完成后通知“测试发送”误调用初始化接口、返回 409 的问题：进入工作台会移除旧向导，按实际点击的表单选择通知接口。已经初始化后 `/api/setup/test` 仍保持关闭，避免绕过管理员认证。没有加入每日定时开关机功能。

密钥用 Fernet 加密保存在 SQLite，解密密钥存储在同一个私有数据卷中；这是防止数据库单独泄露的保护，不是对拥有 Docker/root 权限的人保密。保存后页面不回显密钥。数据库与密钥文件权限为 600，容器使用非 root 用户、只读根文件系统与独立数据卷。

企业微信测试必须收到 HTTP 200 且 JSON `errcode=0`；HTML、非 JSON、错误码和重定向均按失败处理。长消息按 UTF-8 字节分片，每分钟最多尝试 18 片，超出时明确显示部分失败。通知重试可能重复已经成功的分片，请以任务结果为准。

Bark 是 iPhone 通知服务，不是必需项；支持设备密钥形式的公网 HTTPS 地址，例如 `https://api.day.app/你的设备密钥`。不支持内网、localhost 和私有 IP 服务地址。URL 留空保留已存地址，关闭渠道不会删除其地址；可使用“清除已保存地址”选项移除。

## 六、迁移原脚本

网页“系统设置 → 从原脚本迁移”上传原来的 `config.json`，包含 `users`、`wecom`、`bark`。导入前所有条目都会校验；同密钥账号复用、同账号实例重复导入会更新策略，不重复增加。旧脚本的历史状态/日志不迁移，旧 Telegram 机器人交互不迁移。

首次向导先建立一条有效策略；之后再导入旧配置。必须由你手动停用旧监控 cron，避免两套程序同时控制。程序不会替你清理 cron 或改其他服务。

## 七、备份、恢复、忘记密码

网页普通配置导出不包含账号密钥或通知地址，只供查看策略，不能直接当完整备份导入。

完整备份需再次输入管理员密码，包含数据库与解密密钥，请作为敏感文件离线保管。恢复接受 1.0.0 或 1.1.0 导出的三文件 ZIP，会校验数据库和密钥，覆盖当前数据并退出全部会话。恢复前副本保留在数据卷 `before-restore/`，再次恢复会覆盖上次的恢复前副本。恢复后使用备份中的管理员密码登录。

忘记密码，通过服务器上的交互命令重置，不将密码放在命令参数或日志中：

```sh
docker exec -it aliyun-monitor-web python admin_reset.py
```

## 八、日常维护和构建失败后的继续操作

```sh
docker ps -a --filter name=aliyun-monitor-web
docker logs --tail 100 aliyun-monitor-web
docker restart aliyun-monitor-web
docker stop aliyun-monitor-web
docker start aliyun-monitor-web
```

安装向导不会覆盖已有容器或 `.env`。若首次构建因网络失败，确认 `.env` 是本应用这次生成的，再按以下方式继续（不要重写其口令）：

```sh
docker build -t aliyun-monitor-web:1.1.0 .
docker volume create aliyun-monitor-web-data
# 如果有 Compose：
docker compose -f compose.yaml up -d
# 旧 docker-compose 使用：docker-compose -f compose.yaml up -d
```

没有 Compose 时，先查看 `.env` 里的 `WEB_BIND/WEB_PORT`，用相同监听和端口替换以下 `127.0.0.1:8088`：

```sh
docker run -d --name aliyun-monitor-web --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges:true --memory 512m \
  --env-file .env -p 127.0.0.1:8088:8080 \
  -v aliyun-monitor-web-data:/data aliyun-monitor-web:1.1.0
```

不要删除 `aliyun-monitor-web-data` 数据卷。重建容器应先完成完整备份并停用旧容器；不必重建数据卷。向导没有一键卸载或自动删除数据的功能。

部署内容是源代码构建包，不是预装镜像文件。基础镜像支持 amd64/arm64，但实测架构详见测试报告。

## 九、复现自动测试

只使用伪造测试数据，不访问真实云资源、不发送真实通知：

```sh
docker build -t aliyun-monitor-web:1.1.0 .
docker run --rm --network none --tmpfs /tmp:rw,size=128m \
  -v "$PWD:/app:ro" aliyun-monitor-web:1.1.0 \
  python -m unittest discover -s tests -v
```

`tests/preview.py` 只用于开发预览，Dockerfile 不会将它复制进生产镜像。生产程序没有“伪造云数据”的环境变量开关。部署后请使用向导中的读取测试和真实通知测试完成你自己账号的最终验收。

前端通知入口回归检查（有 Node.js 的测试机器）：`node tests/frontend.test.js`。生产服务器不需要 Node.js。

## 十、镜像架构与升级

本次镜像为 `codex-aliyun-monitor-final-arm64:1.1.0`（本地 Mac）和 `codex-aliyun-monitor-final-amd64:1.1.0`（常见 x86_64 服务器）。两版应用文件及依赖版本做过一致性核对，镜像 ID 不同是架构差异，详见测试报告和校验文件。请用 1.1.0 新标签，不要把旧 1.0.0 当作这次更新。

上传 Docker Hub 时把对应镜像重新标记到你自己的仓库再推送；此交付没有替你登录或上传。服务器使用匹配架构的镜像，保留原数据卷、端口和安全参数。不要同时启动两个监控容器共享同一数据卷。

升级前下载完整备份，停用旧容器；先设为只监控再启动新版。管理员与原配置不需要重新创建，新增抢占恢复选项默认关闭。若要回退镜像，务必保持只监控，确认数据兼容性后再重新启用自动控制。

## 十一、GitHub Packages 发布

仓库新增手动发布流程 `Verify and publish container 1.1.0`，目标为 `ghcr.io/flamingyouth/aliyun-monitor:1.1.0`。它不在普通代码提交时自动运行，也不需要保存个人 Token。只在发布阶段使用仓库临时 `GITHUB_TOKEN` 的 Packages 写入权限。

发布前分别在原生 AMD64、ARM64 环境构建候选镜像：11 个应用文件、20 个依赖版本及 Python 版本必须与已有 1.1.0 测试基线一致；两版均通过完整单元测试、前端回归和镜像内真实 HTTP 测试，才发布已测试的候选镜像，不在发布阶段重新构建。测试只使用虚构数据且断网，不使用实际账号、Webhook、管理员配置或运行数据卷。已有版本标签不会被覆盖。

公共版本统一使用 `1.1.0` 标签，Docker 根据机器架构选择 AMD64 或 ARM64；也保留 `1.1.0-amd64`、`1.1.0-arm64` 标签。通过镜像 source 标签关联本仓库，使包显示在右侧 Packages。新包默认为私有，拥有者需要在 Package settings 中改为 Public，其他人才能免登录拉取；公开后不能改回私有。[GitHub 官方说明](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)

此流程不操作阿里云镜像仓库，不修改任何正在运行的容器或部署配置。已有阿里云镜像地址继续有效；切换镜像源并非必需，已有部署不需要重新初始化或更换数据卷。后续应用版本变化时，需要重新完成对应版本验收并建立新基线，不能复用 1.1.0 的校验结果。
