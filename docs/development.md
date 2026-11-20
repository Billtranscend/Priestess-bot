# 开发说明

这个仓库已经不更新了，这一页写的是最后一版 `napcat-final` 的代码。想接着改，就 fork 一份自己维护。
仓库里没有测试，也没有任何自动检查，见 [测试](#测试)。部署看 [NapCat 部署](deploy-napcat.md)。

- [架构](#架构)
- [目录结构](#目录结构)
- [上游插件的校验](#上游插件的校验)
- [修改帮助图](#修改帮助图)
- [图片清晰度与体积](#图片清晰度与体积)
- [测试](#测试)

## 架构

```
QQ  <-->  NapCat (Docker, OneBot v11)  <-- 反向 WebSocket -->  NoneBot2 (bot.py, systemd)
                                                                   |
                                                                   +-- 上游插件: nonebot-plugin-skland, nonebot-plugin-pErithacus ...
                                                                   +-- 本地插件: plugins/
                                                                   +-- SQLite (data/)，无头 Chromium 出图
```

NapCat 登录 QQ，对外提供 OneBot v11 接口。NoneBot2 在 `127.0.0.1:8080` 等着，NapCat 用反向 WebSocket 连过来。
`bot.py` 只注册 OneBot v11 适配器，按顺序加载插件，哪个插件加载失败就直接报错退出。
动态推送可以另外搭配 [nonebot-bison](https://github.com/MountainDash/nonebot-bison)，这个仓库里没有它。

有几个插件里还留着官方机器人的分支。它们找的桥接层 `plugins.official_bridge` 已经不在这个仓库里，这些分支走不到。`sanity_reminder` 和帮助图里的 `<!--official-->` 部分会看 `QQ_BOTS`，这个设置别加。

## 目录结构

```
bot.py                         入口，按顺序加载插件
requirements.txt               直接依赖，固定版本
requirements.lock              最后一次实测时所有包的版本，安装时用 -c 约束（pip install -r requirements.txt -c requirements.lock）
compose.example.yaml           NapCat 的 Docker Compose 示例（社区存档的 v4.18.42 镜像，见 docs/deploy-napcat.md）
.env.example                   配置示例
docs/                          部署和开发说明，README 用到的截图
deploy/
  nonebot.service.example      systemd 单元示例
  render_skl_help.py           马上重新生成帮助图（机器人启动时也会按需自动生成）
  orm_cli.py                   数据库迁移命令行
  restart_when_idle.py         等到没有指令在处理，也没有战绩收集、卫戍榜统计、账号体检或夜间签到在跑，再重启服务
  restore_health_backup.py     把账号体检删掉的账号、角色和抽卡记录从备份写回数据库（先核对，加 --apply 才写）
plugins/
  ef_theme/                    共用的终末地风格样式和字体
  ark_theme/                   共用的明日方舟风格样式
  endfield_wiki/               终末地资料库（AKEData 同步、检索、资料卡）
  endfield_guide/              高难关卡资料、竞速榜、干员统计
  endfield_roster/             终末地角色卡（/zmd开盒）
  arknights_roster/            明日方舟角色卡（/mrfz开盒）
  arknights_garrison/          卫戍协议战绩（/卫，数据用上游的 GarrisonView，图是自己画的）和卫戍榜（每天 10:30 统计，结果在 data/arknights_garrison/board.json）
  skland_gacha_rank/           欧非榜（所有绑定用户一起排；统计结果缓存在 data/skland_gacha_rank/snapshot.json）
  heybox_import/               从小黑盒导入早期抽卡记录
  weekly_report/               本周任务（/本周任务，发了才做，不定时发）
  game_calendar/               终末地和明日方舟的活动日历、活动开启通知
  skland_health/               账号体检（登录过期、角色全部失效，或者本人既不在任何群、也不是机器人好友时解绑；接口临时报错不算）；
                               夜间签到和森空岛请求的网络超时重试；backup.py 是删除前的备份（backups/skland-health/ 下只追加的 .jsonl）
  sanity_reminder/             /理智 查询，理智回满提醒 /理智提醒，以及机器人离开群后的清理
  skl_help/                    帮助图：render.py 把 help.html 生成图片；guide.py 出 /帮助 分类名 的文字版
  friend_welcome/              /抽卡记录：用文字列出两款游戏的抽卡记录指令（加好友、进群时机器人不说话）
  skland_ef_theme/             抽卡记录等页面的终末地风格模板，上游图片模板的小修正
  skland_compact_images/       上游插件出的图统一转成 WebP，特别长的页面适当缩小；同时出图的数量有上限
  render_image_cache/          出图时用到的游戏图片（头像、图标、横幅）存到本地，下次直接读，出图更快，图本身不变
  skland_ark_assets/           上游明日方舟页面的外部图片改用本地缓存，缺的先补齐，外站慢时出图不会超时
  skland_auto_gacha/           终末地抽卡记录每天自动同步
  skland_ef_pools/             用 AKEData 数据生成终末地卡池表；全新部署第一次启动、还没有表的时候，先用上游从 GitHub 下载的表
  skland_ark_gacha/            明日方舟抽卡记录：按年份分页看已保存的记录、手动更新、每天自动同步
  skland_bind_sign/            绑定规则和保护、/skl添加账号、绑定后的签到和抽卡同步通知
  skland_index_tolerance/      选账号序号时认「01」和全角数字
  skland_roles.py              取成员默认角色的公共查询
  skland_pools.py              上游没收录的卡池类型（重构寻访）、游戏卡池页的内容接口地址
  skland_limited.py            限定池的 UP 判定和 UP 平均（抽卡记录和欧非榜共用）
  sqlite_foreign_keys/         给 SQLite 打开外键约束，解绑时角色和抽卡记录跟着账号一起删
  log_privacy/                 日志不记聊天内容，只留消息编号、发送者和群号
  skland_shortcuts/            指令别名，上游提示语改成这里的指令名；没绑定、没有默认角色时告诉对方接着发什么
  skland_reaction_order/       上游指令的「完成」表情改到回复发出之后再发
  skland_resource_resilience/  上游资源下载的超时和重试
  perithacus_guard/            pErithacus 管理指令的权限保护；机器人离开群后删掉这个群的关键词回复（先按群列表确认，每天补清一次）
  perithacus_trigger_resilience/  pErithacus 关键词触发时的下载超时保护
  command_tolerance/           指令进来之前统一写法：去掉开头的文字版「@机器人」、「角色2」换成 -r 2、去掉末尾的空白
  temp_session_forward/        临时会话 / 私聊里触发的合并转发只发给本人
  forward_reply_owner/         群里多页结果的聊天记录卡片标上群名片，卡片后 @ 查询人
  group_onboarding/            自动同意入群邀请
  strict_command.py            指令严格匹配的规则
  quick_buttons.py             回复后面「还可以发：…」的提示和文字菜单，群里文字菜单的撤回
  shared.py                    共用的小工具：原子写 JSON、表情回应、取当前的 OneBot 机器人、私聊通知超级用户、
                               机器人离开群的判断和确认（bot_left_group、left_for_sure、groups_left）、图片上传超时的判断
  skland_buttons/              不带序号发 /skl切换…角色 时，用文字列出这个游戏的角色
local_plugins/                 只在本机用的插件：放进来的包 bot.py 会自动加载，这个目录不进仓库（可选）
```

运行时生成、不进仓库的目录：`data/`（数据库和各插件的数据）、`cache/`、`config/`、`backups/`（账号体检的备份），
还有 NapCat 的 `napcat/`。

## 上游插件的校验

有几个本地插件是包着上游函数工作的，启动时会校验上游文件的 SHA-256，所以 `requirements.txt` 固定了 nonebot-plugin-skland 的版本。
升级上游以后校验不通过的话，对应的插件会拒绝启动，或者自动退回上游原来的行为。真要升级，先把代码重新核对一遍，再更新插件里的校验值。

间接依赖也要固定。`skland_shortcuts`、`forward_reply_owner`、`temp_session_forward` 这些依赖 nonebot-plugin-alconna 的内部行为，
版本对不上时可能启动就报错，也可能不声不响地失效。`requirements.lock` 是最后一次实测时的完整版本表，安装时带上
`-c requirements.lock`。nonebot-plugin-htmlrender 固定在 0.6.7，别升级。

## 修改帮助图

帮助图来自 `plugins/skl_help/help.html`。机器人启动时会比较这次生成的页面和上次的一不一样，不一样才重新出图，所以改完重启就行。
只改了 `help.html` 的话，可以不重启，用装浏览器的那个用户跑 `.venv/bin/python deploy/render_skl_help.py`，马上生成；
加一个文件路径参数，就只输出到那个文件，不动机器人在用的图。改了 `.env` 里的设置一定要重启：
运行中的机器人按启动时的设置判断，会把脚本按新设置生成的图再盖回去。

页面里用注释括起来的部分，生成时按设置保留或去掉：

| 标记 | 什么时候保留 |
|---|---|
| `<!--bison-->…<!--/bison-->` | `HELP_BISON=true`。Bison 的指令不属于这个项目，没装 Bison 就不该出现 |
| `<!--napcat-->…<!--/napcat-->` | 一直保留 |
| `<!--official-->`、`<!--migration-->` | 一直去掉，见 [架构](#架构) 最后一段 |

`/帮助 分类名` 的文字版由 `guide.py` 从生成后的页面里读出来，不另写一份。分区是 `ef-sec`（`auto`、`warn`、`sec`），
标题是里面的 `h3` 和 `small`，指令表单元格的类名是 `cmd`、`fn`、`note`，说明文字在 `div.colh`、`div.dim`、`div.alias` 里
（`div.alias` 里的 `div.legend` 是表格各列的说明，只留在图片里）。改版式时保留这些类名，不然文字版会缺内容。

## 图片清晰度与体积

所有图片都用 WebP 发：清晰度一样的话，大约是 JPEG 的四成大小。往 QQ 传图的速度在不同时段差得很多，图越大等得越久。
清晰度和体积在这几处调：

- 这个仓库自己的页面：`plugins/ef_theme/__init__.py` 的 `PAGE_SCALE` 和 `PAGE_QUALITY`
- `/zmd开盒`、`/mrfz开盒`：`plugins/endfield_roster/__init__.py`、`plugins/arknights_roster/__init__.py` 顶部的常量
- 上游出的图：`plugins/skland_compact_images/__init__.py` 的 `QUALITY`

## 测试

我自己部署里有一套离线测试（`tests/`），没放进公开仓库。这里也没有 CI 或者别的自动检查。
改了代码就自己试：在测试群里把改到的指令实际发一遍。

[返回首页](../README.md)
