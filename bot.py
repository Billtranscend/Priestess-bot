import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


nonebot.init(
    localstore_use_cwd=True,
    alembic_startup_check=True,
    htmlrender_browser="chromium",
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)


def load_required_plugin(module_name: str) -> None:
    if nonebot.load_plugin(module_name) is None:
        raise RuntimeError(f"Required plugin failed to load: {module_name}")


load_required_plugin("nonebot_plugin_perithacus")
load_required_plugin("plugins.perithacus_guard")
load_required_plugin("plugins.perithacus_trigger_resilience")
load_required_plugin("nonebot_plugin_skland")
load_required_plugin("plugins.skland_resource_resilience")
load_required_plugin("plugins.skland_compact_images")
load_required_plugin("plugins.skland_ef_theme")
load_required_plugin("plugins.skland_efgacha_compat")
load_required_plugin("plugins.skland_auto_gacha")
load_required_plugin("plugins.skland_shortcuts")
load_required_plugin("plugins.skl_help")
load_required_plugin("plugins.skland_gacha_rank")
load_required_plugin("plugins.endfield_roster")
load_required_plugin("plugins.sanity_reminder")
load_required_plugin("plugins.endfield_wiki")
load_required_plugin("plugins.endfield_guide")
load_required_plugin("plugins.weekly_report")
load_required_plugin("plugins.skland_health")
nonebot.load_builtin_plugin("echo")


if __name__ == "__main__":
    nonebot.run()
