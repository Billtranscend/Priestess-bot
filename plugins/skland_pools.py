"""Endfield pool types the record API knows and nonebot-plugin-skland does not.

The API has a fifth character pool type, 重构寻访 (rerun banners such as 绚丽异彩, pool ids
"rerun_chr_..."), that nonebot-plugin-skland 0.7.2 never queries. SklandAPI.get_ef_gacha_history
only reads `.value` and compares with the weapon member, so a plain object stands in for the
missing enum member. A plain module (no plugin): imported by skland_auto_gacha and skland_ef_theme.
"""

from types import SimpleNamespace

RERUN = SimpleNamespace(name="RERUN", value="E_CharacterGachaPoolType_Rerun")
