"""几个插件共用的终末地卡池地址。这是普通模块（不是插件）：由 skland_ef_pools、skland_ef_theme 和 game_calendar 导入。"""

# 游戏内卡池页用的内容接口（公开，无需登录）：skland_ef_pools、skland_ef_theme、game_calendar 都从这里取卡池图
POOL_CONTENT_URL = "https://ef-webview.hypergryph.com/api/content"
