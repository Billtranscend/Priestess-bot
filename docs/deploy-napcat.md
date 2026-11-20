# NapCat 部署

这一页按最后一版 `napcat-final` 写，之后不会再改。这种装法是用一个个人 QQ 号当机器人。
硬件和系统要求见首页的 [运行环境](../README.md#运行环境)。

- [工作方式](#工作方式)
- [部署步骤](#部署步骤)
- [帮助图](#帮助图)
- [配置](#配置)
- [群与好友](#群与好友)
- [搭配 nonebot-bison](#搭配-nonebot-bison)
- [常见问题](#常见问题)

## 工作方式

```
QQ  <-->  NapCat (Docker, OneBot v11)  <-- 反向 WebSocket -->  NoneBot2 (bot.py, systemd)
                                                                   |
                                                                   +-- 上游插件: nonebot-plugin-skland, nonebot-plugin-pErithacus ...
                                                                   +-- 本地插件: plugins/
                                                                   +-- SQLite (data/)，无头 Chromium 出图
```

NapCat 登录 QQ，对外提供 OneBot v11 接口。NoneBot2 在 `127.0.0.1:8080` 等着，NapCat 用反向 WebSocket 连过来。
B 站动态之类的推送可以另外装 [nonebot-bison](https://github.com/MountainDash/nonebot-bison)，这个仓库里没有它。

## 部署步骤

下面以 Ubuntu 24.04、装在 `/opt/priestess-bot` 为例。从头到尾用同一个普通用户，不要用 root：
目录归它，出图用的浏览器装在它的家目录下，NapCat 容器和 systemd 服务也用它来跑。

1. 装系统依赖

   ```bash
   sudo apt update
   sudo apt install -y python3.12-venv fonts-wqy-zenhei git curl
   # Docker 按官方文档装: https://docs.docker.com/engine/install/ubuntu/
   # 当前用户不在 docker 组的话，下面的 docker 命令前面加 sudo
   sudo install -d -o "$(id -un)" -g "$(id -gn)" /opt/priestess-bot
   ```

2. 拿代码，装 Python 依赖

   ```bash
   git clone --branch napcat-final https://github.com/Billtranscend/Priestess-bot.git /opt/priestess-bot
   cd /opt/priestess-bot
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt -c requirements.lock
   ```

   一定要带 `-c requirements.lock`。`requirements.txt` 只固定了直接依赖，`requirements.lock` 是最后一次测试时每个包的版本。
   不带的话，间接依赖会装成当天的最新版，那组版本没人跑过。

3. 装出图用的浏览器

   ```bash
   sudo .venv/bin/playwright install-deps chromium   # Chromium 要的系统库
   .venv/bin/playwright install chromium             # 浏览器本身，不要加 sudo
   ```

   这一步不能省。不装的话第一次启动会直接退出，日志里是 `Playwright environment is not set up correctly`。
   nonebot-plugin-htmlrender 启动时会自己试着装，但它找的是 PATH 里的 `playwright`，还要 root，按这里的装法是装不上的。

   浏览器装在执行第二条命令的那个用户的 `~/.cache/ms-playwright` 下，所以手动启动和 systemd 服务都要用这个用户。
   想装到别处，就在安装、手动启动和服务里设同一个 `PLAYWRIGHT_BROWSERS_PATH`（见 `deploy/nonebot.service.example`）。
   nonebot-plugin-htmlrender 别升级。

4. 写配置

   ```bash
   cp .env.example .env
   chmod 600 .env
   # 编辑 .env: 设好 ONEBOT_ACCESS_TOKEN 和 SUPERUSERS
   ```

5. 拿 NapCat 镜像

   NapCat 已经停更了。[NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ) 大约在 2026-11-12 清空，
   Docker Hub 上的 `mlikiowa/napcat-docker` 也拉不到了，我原来用的镜像已经没有了。

   现在能拿到的只有社区存档（XingYunStar/NapCatQQ）。我把它原样备份到了
   [Billtranscend/NapCatQQ](https://github.com/Billtranscend/NapCatQQ/releases)，要用就从那里下。
   这个备份已经归档，只能读，和社区存档逐个文件一致。

   > 这是第三方重新上传的，不是 NapCat 官方发布，内容我没法保证，用不用你自己决定。
   > 以后 QQ 一更新，NapCat 随时可能用不了。

   只推荐用 Docker 装。发布里那些非 Docker 的包要另装 QQ，而它们指向的 QQ 安装包已经下不到了。
   NapCat 的文档站也没了，要用到的 NapCat 设置我都写在下面。

   ```bash
   mkdir -p ~/napcat-image && cd ~/napcat-image
   f=napcat-docker-v4.18.42-arm64.tar   # x86_64 服务器换成 napcat-docker-v4.18.42-amd64.tar
   curl -fLO https://github.com/Billtranscend/NapCatQQ/releases/download/v4.18.42/$f
   curl -fLO https://github.com/Billtranscend/NapCatQQ/releases/download/v4.18.42/$f.sha256
   sha256sum -c $f.sha256   # 输出 “…: OK” 才往下走
   docker load -i $f
   ```

   `.sha256` 和镜像包在同一个地方，只能说明下载完整，说明不了内容可信。我测的是 arm64 包，它的 sha256 是
   `0d14594844e9135c760e710754a8778d98649de2ca837fce5a61b69b7d39954d`，算出来不一样就说明存档被改过。amd64 包我没测过。

   `docker load` 最后一行会打印 `Loaded image: <镜像名>`。arm64 包导入后叫 `docker.m.daocloud.io/mlikiowa/napcat-docker:v4.18.42`，
   和 `compose.example.yaml` 里写的一样。打印出来的名字不一样，就把 compose 文件里的 `image` 改成打印的那个。
   amd64 包的清单里写的是 `mlikiowa/napcat-docker:v4.18.42`，同样以打印出来的为准。导入完可以删掉 `~/napcat-image`。

6. 启动 NapCat，登录 QQ

   ```bash
   cd /opt/priestess-bot
   cp compose.example.yaml compose.yaml
   # 按文件里的注释改 image、platform、NAPCAT_UID/GID、ACCOUNT、WEBUI_TOKEN 和路径
   mkdir -p data/nonebot_plugin_perithacus/media
   docker compose up -d
   ```

   `compose.yaml` 里的 `platform` 要和导入的镜像包对上，arm64 包是 `linux/arm64`。`compose.yaml` 已经写进 `.gitignore`，
   填了口令以后也别公开。

   - **WebUI 口令**：在 `compose.yaml` 里设好 `WEBUI_TOKEN`（一串长的随机字符）再第一次启动，镜像会按它生成
     `napcat/config/webui.json`。这个文件生成以后，再改 `WEBUI_TOKEN` 就没用了，可以把它从 `compose.yaml` 里删掉。
     没设的话 NapCat 会自己生成一个随机口令，用 `docker compose logs napcat | grep -i token` 在启动日志里找。
   - **WebUI 只听本机**：WebUI 默认监听所有地址，按 `WEBUI_TOKEN` 生成的文件里写的是 `0.0.0.0`。compose 用的是主机网络，
     所以 6099 端口会直接暴露在服务器的公网地址上。第一次启动后，把 `napcat/config/webui.json` 里的 `"host"` 改成 `"127.0.0.1"`，
     再 `docker compose restart napcat`。改好之前，别在防火墙或云服务商的安全组里放行 6099。
     以后在自己电脑上用 SSH 隧道访问：

     ```bash
     ssh -N -L 6099:127.0.0.1:6099 用户名@服务器地址
     # 然后浏览器打开 http://127.0.0.1:6099/webui ，输入口令
     ```

   - **登录**：在 WebUI 里扫码登录机器人的 QQ 号。在 `compose.yaml` 里把 `ACCOUNT` 设成这个 QQ 号（一开始就填也行），
     以后容器重启时会用 `napcat/qq` 里存的登录状态快速登录。不设的话，每次重启都要重新扫码。
     主机网络下容器用的是服务器自己的主机名和网卡地址，重建容器后 QQ 看到的还是同一台设备，登录状态不会因此失效。
     改用桥接网络的话，要在 compose 里固定 `hostname` 和 `mac_address`（见文件里的注释）。
   - **网络配置**：在 WebUI 的「网络配置」里新建一个「Websocket客户端」，地址填 `ws://127.0.0.1:8080/onebot/v11/ws`，
     Token 填 `.env` 里的 `ONEBOT_ACCESS_TOKEN`，消息格式选 `array`。

7. 初始化数据库，第一次启动

   ```bash
   .venv/bin/python deploy/orm_cli.py upgrade
   .venv/bin/python bot.py
   ```

   第一次启动要下载森空岛资源和 AKEData 数据表，得等几分钟。看到 `Bot ... connected` 就说明和 NapCat 连上了，
   在群里发 `/skl帮助` 试一下。没问题就按 Ctrl+C 停掉，接着注册成系统服务。

8. 注册成系统服务

   ```bash
   # 按文件里的注释改路径和用户（User/Group 填上面一直在用的那个用户）
   sudo cp deploy/nonebot.service.example /etc/systemd/system/nonebot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now nonebot
   ```

怎么重启见首页的 [更新与重启](../README.md#更新与重启)。

## 帮助图

`/skl帮助` 发的图是机器人启动时用 `plugins/skl_help/help.html` 生成的，存在 `data/skl_help/`。
装了 nonebot-bison 的，在 `.env` 里设 `HELP_BISON=true`，图里会多出 Bison 的订阅指令。

改了 `help.html` 或者这个设置，重启就会重新生成。只改了 `help.html` 的话也可以不重启：
用装浏览器的那个用户运行 `.venv/bin/python deploy/render_skl_help.py`，马上生效。改了 `.env` 就一定要重启。

## 配置

`.env` 里主要是这几项：

| 配置项 | 示例 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8080` | NoneBot 监听的地址，NapCat 从这里连进来；别暴露到公网 |
| `ONEBOT_ACCESS_TOKEN` | 随机长字符串 | 和 NapCat 里 Websocket客户端的 Token 一样 |
| `SUPERUSERS` | `["123456789"]` | 能用管理指令的 QQ 号。不能空着，空着机器人起不来 |
| `COMMAND_START` | `["/"]` | 指令前缀 |
| `API_TIMEOUT` | `180` | 等 NapCat 回应的秒数。传图慢的时候别调小 |
| `LOCALSTORE_USE_CWD` | `true` | 插件数据放在工程目录下的 `data/`、`cache/`、`config/` |
| `HELP_BISON` | `true` | 装了 nonebot-bison 才设，帮助图里会列出它的订阅指令 |

时区要注意的地方见首页的 [配置](../README.md#配置)。

## 群与好友

- **入群**：入群邀请自动同意。机器人被拉进群、有人加它好友，它都不说话，没有欢迎语。
- **好友申请**：机器人不会自动同意好友申请。想让群友加好友后私聊用，就在机器人 QQ 号的设置里把加好友方式设成允许任何人，
  或者手动同意。每周一的账号体检会解绑既不在机器人任何群里、也不是机器人好友的人；只要有一个群的成员名单或好友列表没读全，
  这一项这次就跳过。
- **机器人离开群以后**：收到通知后先用群列表确认，再删掉这个群的关键词回复和发到这个群的理智回满提醒。
  关键词回复只在这个群生效的，整条删掉；还在别的群生效的，只去掉这个群。掉线期间被移出、群已经解散这种没确认上的，
  每天 04:50 按群列表补清一次。森空岛绑定不会因为退群就删，交给每周体检处理。这个群的 Bison 订阅不会跟着删，
  见 [搭配 nonebot-bison](#搭配-nonebot-bison)。

## 搭配 nonebot-bison

[nonebot-bison](https://github.com/MountainDash/nonebot-bison) 不在这个仓库里，要单独部署。NapCat 要另外连它：
在 WebUI 的「网络配置」里再建一个 Websocket客户端，指向 Bison 那个 NoneBot 的地址，端口不能和这个机器人的 8080 一样，
Token 和 Bison 的配置一致。具体以 Bison 的文档为准。

各群的订阅由群主和管理员在群里用 Bison 自带的指令管：`@机器人 /添加订阅`、`@机器人 /删除订阅`，所有人都能用
`@机器人 /查询订阅`。在 `.env` 里设了 `HELP_BISON=true`，帮助图里也会列出这几条。
Bison 自己不会在机器人退群时删订阅。我自己的机器人会自动删，但那是我服务器上单独的脚本（要 root 权限改 Bison 的数据库），没放进这个仓库。照这个仓库部署的话，退群后去 Bison 的后台删。

## 常见问题

**第一次启动就退出，日志里有 `Playwright environment is not set up correctly`。** 浏览器没装，或者跑机器人的用户
不是装浏览器的那个用户。用跑机器人的用户重做一遍 [部署步骤](#部署步骤) 第 3 步。

**`docker compose up` 报拉取镜像失败或者平台不匹配。** 镜像只能从存档里的镜像包导入，见 [部署步骤](#部署步骤) 第 5 步。
`compose.yaml` 里的 `image` 要和 `docker load` 打印的名字一样，`platform` 要和导入的镜像包对上。

**NapCat 每次重启都要扫码。** 在 `compose.yaml` 里设 `ACCOUNT`。用桥接网络的话，还要固定 `hostname` 和 `mac_address`，
不然每次重建容器，QQ 都会把它当成新设备，存下来的登录状态就没用了。

**这个仓库还有测试或者更新吗。** 没有。我自己的离线测试（`tests/`）没放进公开仓库，这里也没有任何自动检查。
你改了代码，先在测试群里实际试一遍。

更多问题见首页的 [常见问题](../README.md#常见问题)。

[返回首页](../README.md)
