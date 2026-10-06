# Priestess Bot

面向《明日方舟：终末地》与《明日方舟》玩家群的 QQ 群机器人，基于 NoneBot2 与 NapCat。
A QQ group bot for Arknights: Endfield / Arknights communities, built on NoneBot2 and NapCat.

![Python](https://img.shields.io/badge/python-3.12-blue)
![NoneBot2](https://img.shields.io/badge/nonebot2-2.5.0-green)
![Platform](https://img.shields.io/badge/platform-linux%20arm64%20%7C%20x86__64-lightgrey)
![License](https://img.shields.io/badge/license-MIT-yellow)

本项目是一套可直接部署的机器人工程：`bot.py` 加上 `plugins/` 下的本地插件。森空岛绑定、签到、
角色卡等基础能力来自上游插件 [nonebot-plugin-skland](https://github.com/FrostN0v0/nonebot-plugin-skland)，
本仓库在其之上增加了终末地资料库、高难关卡攻略与榜单、群周报、账号体检和统一的终末地风格出图，
并包含若干针对慢速网络环境的稳定性补丁。上游安装包本身不做任何修改。

## 目录

- [功能](#功能)
- [截图](#截图)
- [运行环境](#运行环境)
- [架构](#架构)
- [部署](#部署)
- [配置](#配置)
- [指令](#指令)
- [定时任务](#定时任务)
- [目录结构](#目录结构)
- [数据与隐私](#数据与隐私)
- [常见问题](#常见问题)
- [反馈](#反馈)
- [致谢](#致谢)
- [免责声明](#免责声明)
- [许可证](#许可证)

## 功能

- 入群与默认订阅：自动同意入群邀请；进群后可按模板为新群添加 nonebot-bison 的 B 站订阅
  （明日方舟、明日方舟终末地的动态与开播提醒，规则与多数群一致），由沙箱外的一个小服务写入 Bison 数据库，见「部署」
- 临时会话或私聊里触发的合并转发回复（例如多页的抽卡记录）只发给本人，不会被发到群里
- 群里的多页结果（抽卡记录、干员列表）以聊天记录卡片发送时，卡片标题与预览行使用查询人的群名片，
  卡片发出后紧跟一条 @ 查询人的消息（QQ 的聊天记录卡片本身不能带 @）
- 长图底部自带一块说明板：手机 QQ 查看长图时底部会被「查看原图」等按钮遮挡，说明板垫在该位置，
  正文不会被挡住；只有高度超过宽度 1.75 倍的图片才会出现
- 森空岛账号绑定（扫码或 Token），明日方舟与终末地每日自动签到，签到遇到网络超时自动重试；
  首次绑定成功后立即自动签到一次，避免绑定当天漏签
- 一个 QQ 可绑定多个森空岛账号（上游 0.7.2 的多账号模型）：`/skl添加账号` 扫码后核对角色列表并回复「确认」保存；
  `/skl角色` 查看全部角色与序号，`/skl切换终末地角色 序号`、`/skl切换方舟角色 序号` 更换默认角色，
  查询类指令可加 `-r 序号` 临时查看另一个角色。首次绑定仍是扫码即绑定，不需要确认。
  每日签到与抽卡同步覆盖全部账号和角色；榜单、统计、周报与理智提醒只取默认角色
- 绑定保护：已绑定且登录有效的成员不再发放二维码；扫码的森空岛账号若已绑定在另一个 QQ 上则拒绝；
  登录过期后重新扫码必须使用原来的森空岛账号，避免群里的二维码被他人扫码后顶掉原绑定。
  查询抽卡记录用的凭证单独过期时（森空岛登录仍有效），
  允许用原账号重新扫码续期，已保存的记录不受影响
- 绑定后的抽卡同步：绑定成功的回复会提示正在自动拉取，拉取完成前本人发送的抽卡记录指令不会执行，
  只回复请等待；手动更新与后台同步写入同一批记录时自动跳过重复项
- 撤回二维码失败（协议端偶发超时）不再中断绑定，只记录日志
- 终末地角色卡（开盒）、全干员练度详情、抽卡记录与每日自动同步、群内欧非榜；首次绑定后的第一次抽卡同步完成时会通知本人
- 抽卡记录图把满抽数的赠送（信物、武库箱、UP 武器等，规则取自游戏数据表）单独标为「赠送」，抽数一律不含免费十连，并注明记录的起始日期
- 从小黑盒「抽卡分析」导入官方接口已无法拉取的早期抽卡记录，导入前校验游戏 UID，官方记录不会被改动，可一键撤销；之后官方记录有新增时自动重算导入部分，不会重复计数
- 终末地资料库：干员、武器与装备套装资料卡，支持昵称、简称、同音字和模糊匹配；数据来自 AKEData，
  解包数据有新版本时自动重建索引并在群内通知
- 群友配装统计：游戏数据没有推荐装备表，干员卡改为展示已绑定成员的实际配装（各套装占比、
  常见搭配的装备图标、每个词条的平均精锻次数），只显示汇总，可用 `/攻略统计 退出` 退出
- 高难关卡（战争回响、影拓丰碑）：带封面的关卡一览（顺序与游戏内一致并标注序号）、
  关卡攻略卡（机制、计入出生加成与关卡加成后的敌人数值与抗性、通关阵容、B 站推荐视频）、只含机制与敌人属性的精简卡、
  群内竞速榜、全体绑定玩家的干员出场率
- 理智查询，理智回满时在群内提醒
- 活动日历与活动开启通知（终末地、明日方舟）：`/zmd活动`、`/mrfz活动` 列出正在开放和即将开启的活动、
  卡池及截止时间；有活动开启时在各群自动发送一张通知图（不 @全体，夜间开启的活动在早上统一通知）。
  明日方舟的数据合并自游戏数据表与 PRTS Wiki 的公开接口，停机维护日以公告的开启时间为准。
  两款游戏各用自己的界面风格（终末地为浅色黑黄，明日方舟为深色蓝白），每个活动和卡池都配有
  它自己的横幅图，一眼能看出是什么活动
- 群周报（每周日 19:00）：每周任务、本群竞速前三、关卡轮换、活动与卡池倒计时；
  机器人是管理员的群会附带 @全体成员
- 森空岛账号体检（每周一 04:00）：自动清理登录失效、或全部角色都无法使用的绑定，删除前自动备份；
  接口临时报错不算失效（账号原样保留，下次再查），角色被接口拒绝需连续两次才会移除
- 数据库开启 SQLite 外键约束：上游解绑依赖级联删除，未开启时角色与抽卡记录会残留；
  `deploy/orm_cli.py` 执行迁移时会临时关闭约束
- 所有图片统一为终末地风格的版式，并针对 QQ 上传速度做了体积控制
- 指令严格匹配：指令后必须是空格或结尾，避免聊天内容误触发；消息开头手打或复制的文字版「@机器人」会被自动忽略

## 截图

| 帮助 | 关卡攻略 | 干员资料 |
|---|---|---|
| ![帮助](docs/images/help.jpg) | ![关卡攻略](docs/images/guide.jpg) | ![干员资料](docs/images/wiki-operator.jpg) |

## 运行环境

| 项目 | 已验证的环境 | 说明 |
|---|---|---|
| 操作系统 | Ubuntu 24.04 LTS | 其他带 systemd 的 Linux 发行版应可使用 |
| CPU 架构 | aarch64（ARM64） | 在 Oracle Cloud Ampere A1（2 核 / 12 GB）上长期运行；x86_64 理论可用，未实测，需把 compose 文件中的 `platform` 改为 `linux/amd64` |
| Python | 3.12 | 只在 3.12 上验证过，建议使用同一版本 |
| Docker | 29.x | 仅用于运行 NapCat |
| 内存 | 建议 2 GB 以上 | 出图使用无头 Chromium |
| 中文字体 | 文泉驿正黑（`fonts-wqy-zenhei`） | 图片中的中文字体；数字与英文使用仓库内置的 Barlow Condensed |
| 网络 | 可访问 QQ、森空岛、AKEData、哔哩哔哩、GitHub、PRTS Wiki | 部分资源从 GitHub 下载 |

Windows 与 macOS 未测试：systemd 单元、字体安装方式和部分路径假设都以 Linux 为准。

## 架构

```
QQ  <-->  NapCat (Docker, OneBot v11)  <-- 反向 WebSocket -->  NoneBot2 (bot.py, systemd)
                                                                   |
                                                                   +-- 上游插件: nonebot-plugin-skland, nonebot-plugin-pErithacus ...
                                                                   +-- 本地插件: plugins/
                                                                   +-- SQLite (data/)，无头 Chromium 出图
```

NapCat 负责登录 QQ 并提供 OneBot v11 接口；NoneBot2 作为 WebSocket 服务端监听 `127.0.0.1:8080`，
NapCat 以反向 WebSocket 客户端连入。动态推送可另外搭配
[nonebot-bison](https://github.com/MountainDash/nonebot-bison)，本仓库不包含它。

## 部署

以下以 Ubuntu 24.04 为例，假设安装在 `/opt/priestess-bot`。

1. 安装系统依赖

   ```bash
   sudo apt update
   sudo apt install -y python3.12-venv fonts-wqy-zenhei git
   # Docker 请按官方文档安装: https://docs.docker.com/engine/install/ubuntu/
   ```

2. 获取代码并安装 Python 依赖

   ```bash
   git clone https://github.com/Billtranscend/Priestess-bot.git /opt/priestess-bot
   cd /opt/priestess-bot
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

3. 写配置

   ```bash
   cp .env.example .env
   chmod 600 .env
   # 编辑 .env: 设置 ONEBOT_ACCESS_TOKEN 和 SUPERUSERS
   ```

4. 启动 NapCat 并登录 QQ

   ```bash
   cp compose.example.yaml compose.yaml
   # 按文件内注释修改 platform、NAPCAT_UID/GID 和路径
   mkdir -p data/nonebot_plugin_perithacus/media
   docker compose up -d
   ```

   在 NapCat WebUI（默认只监听本机 `127.0.0.1:6099`，可通过 SSH 隧道访问）中扫码登录，然后在
   「网络配置」里新增一个 **WebSocket 客户端**：地址 `ws://127.0.0.1:8080/onebot/v11/ws`，
   Token 填 `.env` 里的 `ONEBOT_ACCESS_TOKEN`，消息格式选 `array`。

5. 初始化数据库并首次启动

   ```bash
   .venv/bin/python deploy/orm_cli.py upgrade
   .venv/bin/python bot.py
   ```

   首次启动会下载无头 Chromium、森空岛资源和 AKEData 数据表，需要几分钟。看到
   `Bot ... connected` 即表示已与 NapCat 连通，在群里发 `/skl帮助` 测试。

6. 注册为系统服务

   ```bash
   # 按文件内注释修改路径和用户
   sudo cp deploy/nonebot.service.example /etc/systemd/system/nonebot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now nonebot
   ```

修改 `plugins/skl_help/help.html` 后，运行 `.venv/bin/python deploy/render_skl_help.py` 重新生成帮助图，
无需重启。

更新代码后建议用 `.venv/bin/python deploy/restart_when_idle.py` 重启：它会等到没有指令正在处理、
也没有战绩收集在进行时才重启，避免打断群友的操作。该脚本假设服务名为 `nonebot.service` 且当前用户可免密执行 `sudo`。

### 可选：新群自动添加 Bison 订阅

`plugins/group_onboarding` 会自动同意入群邀请，并在机器人进群后把群号写入
`data/group_onboarding/requests/`（一个以群号命名的空文件）。如果同一台机器上还运行着
[nonebot-bison](https://github.com/MountainDash/nonebot-bison)，可以让 `deploy/bison_subscribe.py`
据此为新群添加默认订阅：

- Bison 的管理接口需要超级用户在聊天里临时申请令牌，无法自动调用，所以脚本直接写 Bison 的 SQLite 数据库，
  写入的行与 Bison 自己添加订阅时相同；每次写入前先备份数据库。
- 只会订阅 Bison 已经在跟踪的目标（至少有一个群订阅过），只添加该群还没有的订阅，不修改已有订阅；
  Bison 每次推送时从数据库读取订阅者，无需重启。
- 订阅模板（平台、目标、分类、标签）写在脚本顶部的常量里，按需修改。
- 机器人进程通常没有权限写 Bison 的数据库，脚本由 systemd 的 path 单元在机器人沙箱之外触发，
  示例见 `deploy/bison-subscribe.service.example` 与 `deploy/bison-subscribe.path.example`。
  请求目录对机器人可写，脚本只使用其中纯数字的文件名，不读取文件内容。

不部署这两个单元时，插件仍会同意邀请，只是不会添加订阅。

## 配置

`.env` 中的主要配置项：

| 配置项 | 示例 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8080` | NoneBot 监听地址，供 NapCat 反向连接；不要暴露到公网 |
| `ONEBOT_ACCESS_TOKEN` | 随机长字符串 | 与 NapCat WebSocket 客户端的 Token 一致 |
| `SUPERUSERS` | `["123456789"]` | 可使用管理指令的 QQ 号 |
| `COMMAND_START` | `["/"]` | 指令前缀 |
| `API_TIMEOUT` | `180` | 等待 NapCat 响应的秒数；上传图片较慢时不要调小 |
| `LOCALSTORE_USE_CWD` | `true` | 插件数据放在工程目录下的 `data/`、`cache/`、`config/` |
| `SKLAND__EF_GACHA_RENDER_MAX` | `8` | `/zmd抽卡记录` 单张图里每类卡池最多画几个，超过则分页发送；上游默认 5 |

定时任务使用 `nonebot-plugin-apscheduler` 的默认时区 Asia/Shanghai，与服务器系统时区无关。

上游 nonebot-plugin-skland 用进程本地时间格式化日期（苏醒日、抽卡记录日期、4 点刷新倒计时等）。
服务器不在东八区时，请给机器人进程设置 `TZ=Asia/Shanghai`（示例 systemd 单元已包含），否则北京时间
0 点到 8 点之间的日期会早一天。

所有图片都以 WebP 发送：同等清晰度下约为 JPEG 的四成大小。上传到 QQ 的速度随时段变化很大
（实测白天与晚高峰相差约十倍），图片越大等待越久。清晰度与体积在三处调整：

- 本仓库自己的页面（资料卡、攻略、榜单、周报、欧非榜、帮助图）：`plugins/ef_theme/__init__.py` 的
  `PAGE_SCALE` 与 `PAGE_QUALITY`；
- `/zmd账号详情`：`plugins/endfield_roster/__init__.py` 顶部的 `SCALE` 与 `QUALITY`，文件内注释给出了各档的实测大小；
- 上游 nonebot-plugin-skland 渲染的全部图片（开盒、抽卡记录、明日方舟卡片等）：
  `plugins/skland_compact_images/__init__.py` 的 `QUALITY`，以及超长页面的缩放阈值。

## 指令

所有指令都以 `/` 开头。「可 @他人」表示可以在指令后 @ 群成员查看对方的数据。

### 账号

| 指令 | 作用 | 备注 |
|---|---|---|
| `/skl绑定` | 扫二维码绑定森空岛（同 `/skl扫码`） | 群聊、私聊均可 |
| `/skltoken绑定 凭据` | 用 Token 凭据绑定 | 只能私聊 |
| `/skl添加账号` | 再绑定一个森空岛账号 | 扫码后核对角色列表，回复「确认」保存 |
| `/skl角色更新` | 刷新已绑定的游戏角色 | |
| `/skl角色` | 查看已绑定的账号、角色和序号 | 标有「插件默认」的是指令默认使用的角色 |
| `/skl切换终末地角色 序号` | 更换默认角色 | 方舟用 `/skl切换方舟角色 序号` |
| `指令 -r 序号` | 临时查看另一个角色 | 如 `/zmd抽卡记录 -r 2`、`/zmd开盒 -r 2`、`/mrfz卡片 -r 2` |
| `/skl解绑` | 解绑并删除角色与抽卡记录 | 先选账号序号或「全部」，再回复「确认」 |

### 终末地

| 指令 | 作用 | 可 @他人 |
|---|---|---|
| `/zmd开盒`、`/zmd开盒 all` | 终末地角色卡 / 展示全部角色 | 是 |
| `/zmd账号详情`（别名 `/账号详情`） | 全部干员的练度详情图：武器、技能等级、装备 | 是 |
| `/zmd签到`、`/zmd签到详情` | 手动签到 / 查看签到状态 | 否 |
| `/zmd抽卡记录`、`/zmd抽卡记录更新` | 查看 / 立即拉取抽卡记录 | 否 |
| `/zmd导入小黑盒 小黑盒ID` | 从小黑盒补回更早的抽卡记录；只能导入与自己绑定的终末地 UID 相同的小黑盒账号 | 否 |
| `/zmd撤销小黑盒导入` | 删除自己从小黑盒导入的全部记录 | 否 |
| `/zmd欧非榜`（别名 `/欧非榜`） | 本群限定池欧非排行；`/zmd欧非榜 退出` 可不上榜 | 仅群聊 |

### 终末地资料库

| 指令 | 作用 |
|---|---|
| `/干员名` | 干员资料卡，支持昵称与同音字，例如 `/提丰`、`/小庄` |
| `/干员名专武` | 该干员专属武器的资料卡 |
| `/武器名` | 武器资料卡，支持简称 |
| `/套装名`、`/装备名` | 装备套装资料卡：3 件套效果、各件装备属性（未精锻与精锻满级）、成员使用统计，例如 `/险关`、`/潮涌手甲` |
| `/干员名配装` | 同 `/干员名`，干员卡内含群友配装统计 |

### 活动日历

| 指令 | 作用 |
|---|---|
| `/zmd活动`（`/终末地活动`、`/zmd日历` 等） | 终末地正在开放和即将开启的活动、卡池及截止时间 |
| `/mrfz活动`（`/明日方舟活动`、`/粥活动`、`/舟日历` 等） | 明日方舟，同上 |
| `/活动`、`/日历` | 两款游戏各发一张 |

游戏名与「活动 / 日历 / 活动日历 / 活动列表」之间可以有空格或「的」。时间均为北京时间。

### 高难关卡

| 指令 | 作用 |
|---|---|
| `/战争回响` | 本期轮换的关卡、封面与剩余时间 |
| `/影拓丰碑 [丰碑名]` | 全部丰碑的封面与关卡序号；带丰碑名时显示该丰碑各关的封面 |
| `/敌人 关卡名或丰碑名 [序号] [难度]` | 只看关卡机制与敌人属性；`/回响敌人` 为本期轮换，`/丰碑敌人` 为当前丰碑 |
| `/攻略 关卡名 [难度]` | 机制、敌人数值、通关阵容、推荐视频；也可写 `/攻略 丰碑名 序号` |
| `/竞速榜` | 竞速榜目录 |
| `/回响竞速 [关卡名]` | 战争回响群内最快通关排名 |
| `/丰碑竞速 [丰碑名或关卡名]` | 影拓丰碑群内最快通关排名 |
| `/榜单角色出场率 [范围]` | 全部绑定玩家的高难干员出场率 |
| `/攻略统计 退出` / `加入` | 退出或重新加入阵容统计与榜单 |

### 明日方舟与其他

| 指令 | 作用 |
|---|---|
| `/mrfz卡片`、`/mrfz签到`、`/mrfz签到详情`、`/mrfz抽卡记录` | 明日方舟角色卡、签到与抽卡记录 |
| `/方舟干员 [筛选词]` | 干员一览 |
| `/树海肉鸽` 等 | 集成战略战绩 |
| `/理智` | 查看方舟与终末地当前理智 |
| `/理智提醒 开` / `关` | 理智回满时在本群提醒 |
| `/skl帮助` | 帮助图 |

### 管理指令（仅 SUPERUSERS）

| 指令 | 作用 |
|---|---|
| `/周报预览` | 在当前聊天生成周报，不会 @全体成员 |
| `/森空岛体检` | 检查全部绑定，只报告不改动 |
| `/森空岛体检 清理` | 检查并解除失效绑定 |
| `/攻略数据更新` | 立即收集高难关卡战绩 |

## 定时任务

时间均为北京时间。

| 时间 | 任务 | 来源 |
|---|---|---|
| 每天 00:15 / 00:20 | 明日方舟 / 终末地自动签到 | nonebot-plugin-skland |
| 每天 01:00 | 终末地抽卡记录自动同步 | `skland_auto_gacha` |
| 每天 05:30、17:30 | 收集高难关卡战绩（竞速榜、出场率、通关阵容）；05:30 那次同时收集成员配装 | `endfield_guide` |
| 每天 06:00 | 检查机器人在哪些群是管理员 | `weekly_report` |
| 每 10 分钟 | 理智回满检查 | `sanity_reminder` |
| 定期 | 检查 AKEData 数据版本 | `endfield_wiki` |
| 每 5 分钟 | 检查两款游戏是否有活动开启，有则向所有群发送通知图（0–8 点开启的在 8 点发送） | `game_calendar` |
| 每 30 分钟 | 检查明日方舟游戏数据版本，每小时查询一次 PRTS Wiki 已登记的活动 | `game_calendar` |
| 每周日 19:00 | 向所有群发送周报 | `weekly_report` |
| 每周一 04:00 | 森空岛账号体检 | `skland_health` |

## 目录结构

```
bot.py                         入口，按顺序加载插件
requirements.txt               固定版本的依赖
compose.example.yaml           NapCat 的 Docker Compose 示例
.env.example                   配置示例
deploy/
  nonebot.service.example      systemd 单元示例
  render_skl_help.py           生成帮助图
  orm_cli.py                   数据库迁移命令行
  restart_when_idle.py         等待没有指令在处理时再重启服务
  bison_subscribe.py           为新加入的群添加默认的 nonebot-bison 订阅（可选）
  bison-subscribe.*.example    触发上述脚本的 systemd 单元示例
plugins/
  ef_theme/                    共享的终末地风格样式与字体
  endfield_wiki/               终末地资料库（AKEData 同步、检索、资料卡）
  endfield_guide/              高难关卡攻略、竞速榜、出场率
  endfield_roster/             账号详情
  skland_gacha_rank/           欧非榜
  heybox_import/               从小黑盒导入早期抽卡记录
  weekly_report/               群周报
  game_calendar/               终末地与明日方舟的活动日历、活动开启通知
  skland_health/               账号体检与签到网络重试
  sanity_reminder/             理智查询与提醒
  skl_help/                    帮助图
  skland_ef_theme/             开盒的终末地风格模板；抽卡记录页面与其指令处理（缓存查看 / 更新）
  skland_compact_images/       上游插件渲染的图片统一转为 WebP，超长页面适当缩小
  skland_auto_gacha/           抽卡记录每日自动同步
  skland_bind_sign/            绑定规则（首绑免确认、/skl添加账号、一个森空岛账号只属于一个 QQ）、绑定后签到与同步通知
  skland_roles.py              取成员默认角色的公共查询
  sqlite_foreign_keys/         为 SQLite 开启外键约束
  skland_shortcuts/            指令别名（含 `-r 序号` 形式），并把上游提示里的原始指令改写为本机指令名
  skland_efgacha_compat/       上游武器池数据兼容
  skland_resource_resilience/  上游资源下载的超时与重试
  perithacus_guard/            pErithacus 管理指令权限保护
  perithacus_trigger_resilience/  pErithacus 关键词触发的下载超时保护
  command_tolerance/           忽略消息开头的文字版「@机器人」
  temp_session_forward/        临时会话 / 私聊触发的合并转发只发给本人
  forward_reply_owner/         群内多页结果的聊天记录卡片标注群名片，并在卡片后 @ 查询人
  group_onboarding/            自动同意入群邀请，为新群排队添加默认订阅
  strict_command.py            指令严格匹配规则
```

若干本地插件通过包装上游函数工作，并在启动时校验上游文件的 SHA-256。升级上游插件后如果校验不通过，
对应插件会拒绝启动或自动退回上游原始行为，需要重新核对后更新代码中的校验值。

## 数据与隐私

- `data/` 中的数据库保存了用户的森空岛凭证，`.env` 和 `napcat/` 中有访问令牌和 QQ 登录态。
  这些目录已在 `.gitignore` 中排除，请勿提交或公开，并保持目录权限为仅本人可读。
- 账号体检在删除任何绑定前，会把被删除的数据导出到 `backups/skland-health/`。
- 小黑盒导入只在用户本人发出指令时读取其小黑盒「抽卡分析」页面的公开数据，并在 `data/heybox_import/` 保留一份原始响应用于追溯；
  小黑盒只保存汇总数据，导入的记录中六星及其抽数是准确的，四星以占位记录补足数量。
- 竞速榜和出场率只使用已绑定用户的最佳通关记录；用户可用 `/攻略统计 退出`、`/zmd欧非榜 退出` 退出统计。
- 机器人日志中会出现 QQ 号和群号，分享日志前请自行脱敏。

## 常见问题

**图片发送很慢或超时。** 从海外服务器向 QQ 上传图片可能只有每秒十几到二十几 KB。本项目已对大图做了缩放与
压缩，并把 `API_TIMEOUT` 调到 180 秒；NapCat 的上传超时也建议相应调大。

**从旧版本（nonebot-plugin-skland 0.7.1）升级到本版本。** 0.7.2 改了数据表结构。先停止机器人并备份
`data/nonebot_plugin_orm/db.sqlite3`，再 `pip install -r requirements.txt`、更新代码，然后运行
`.venv/bin/python deploy/orm_cli.py upgrade`（抽卡记录多时需要几分钟），完成后再启动。

**升级 nonebot-plugin-skland 后启动失败。** 见上文关于 SHA-256 校验的说明，这是有意的保护，
避免本地补丁在上游代码变化后静默失效。

**图片里的中文显示为方块。** 安装 `fonts-wqy-zenhei` 后重启。

**活动、卡池或下期轮换没有显示。** 这些来自 AKEData 的游戏解包数据，版本更新后可能有延迟，
以游戏内公告为准。

## 反馈

使用中遇到问题，或者有改进的想法，请到 [Issues](https://github.com/Billtranscend/Priestess-bot/issues/new/choose) 选择对应的模板提交：

- **使用问题**：部署不成功、指令没有反应、报错，或者显示的数据不对。
- **改进建议**：希望增加新功能，或者觉得现有功能可以做得更好。

提交前请把日志和截图里的 Token、Cookie、二维码、QQ 号和群号去掉。上游插件、NapCat、nonebot-bison 自身的问题，
请到各自的仓库反馈，模板选择页里有对应的链接。

## 致谢

本项目建立在以下开源项目与数据源之上，感谢各位作者的工作。

| 项目 | 用途 | 许可证 |
|---|---|---|
| [nonebot/nonebot2](https://github.com/nonebot/nonebot2) | 机器人框架 | MIT |
| [nonebot/adapter-onebot](https://github.com/nonebot/adapter-onebot) | OneBot v11 适配器 | MIT |
| [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ)、[NapNeko/NapCat-Docker](https://github.com/NapNeko/NapCat-Docker) | QQ 协议端及其 Docker 镜像 | 见各仓库 |
| [FrostN0v0/nonebot-plugin-skland](https://github.com/FrostN0v0/nonebot-plugin-skland) | 森空岛绑定、签到、角色卡、抽卡记录等核心能力；`plugins/skland_ef_theme/templates/` 中的模板改写自该项目 | MIT |
| [FrostN0v0/EndfieldGachaPoolTable](https://github.com/FrostN0v0/EndfieldGachaPoolTable) | 终末地卡池数据 | 见仓库 |
| [SnowMoonSS/nonebot-plugin-pErithacus](https://github.com/SnowMoonSS/nonebot-plugin-pErithacus) | 关键词词库与回复 | MIT |
| [kexue-z/nonebot-plugin-htmlrender](https://github.com/kexue-z/nonebot-plugin-htmlrender) | HTML 渲染出图 | MIT |
| [nonebot/plugin-alconna](https://github.com/nonebot/plugin-alconna) | 指令解析与跨平台消息 | MIT |
| [nonebot/plugin-orm](https://github.com/nonebot/plugin-orm) | 数据库 ORM | MIT |
| [nonebot/plugin-apscheduler](https://github.com/nonebot/plugin-apscheduler) | 定时任务 | MIT |
| [nonebot/plugin-localstore](https://github.com/nonebot/plugin-localstore) | 本地数据目录 | MIT |
| [he0119/nonebot-plugin-user](https://github.com/he0119/nonebot-plugin-user) | 用户与平台账号绑定 | MIT |
| [KomoriDev/nonebot-plugin-argot](https://github.com/KomoriDev/nonebot-plugin-argot) | 上游插件依赖 | MIT |
| [MountainDash/nonebot-bison](https://github.com/MountainDash/nonebot-bison) | 可选搭配的动态订阅推送（不包含在本仓库中） | MIT |
| [microsoft/playwright-python](https://github.com/microsoft/playwright-python) | 无头浏览器 | Apache-2.0 |
| [encode/httpx](https://github.com/encode/httpx) | HTTP 客户端 | BSD-3-Clause |
| [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | 图片处理 | MIT-CMU |
| [mozillazg/python-pinyin](https://github.com/mozillazg/python-pinyin) | 同音字检索 | MIT |
| [sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) | 数据库访问 | MIT |
| [jpt/barlow](https://github.com/jpt/barlow) | Barlow Condensed 字体（`plugins/ef_theme/fonts/`） | SIL OFL 1.1 |
| [AKEData](https://www.akedata.wiki/) | 终末地解包数据：干员、武器、关卡、敌人、活动与卡池时间 | 见站点说明 |
| [小黑盒](https://www.xiaoheihe.cn/) | 终末地「抽卡分析」：早期抽卡记录的导入来源 | 见站点说明 |
| [yuanyan3060/ArknightsGameResource](https://github.com/yuanyan3060/ArknightsGameResource) | 明日方舟游戏数据表：活动与卡池时间 | 见仓库说明 |
| [PRTS Wiki](https://prts.wiki/) | 明日方舟已公布活动的时间、活动与卡池横幅（仅使用其公开 API）、卡池 UP 干员 | CC BY-NC-SA 4.0 |

第三方代码与字体的版权声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 免责声明

本项目为玩家社区的非官方工具，与鹰角网络（Hypergryph）、GRYPHLINE、森空岛、小黑盒及腾讯 QQ 没有任何关联。
《明日方舟》《明日方舟：终末地》的名称、美术素材与游戏数据的著作权归其权利人所有，截图中的游戏素材仅用于
展示功能。森空岛接口为非公开接口，使用本项目产生的账号风险由使用者自行承担。请勿用于商业用途。

## 许可证

本仓库自有代码以 [MIT License](LICENSE) 发布。第三方组件遵循各自的许可证。
