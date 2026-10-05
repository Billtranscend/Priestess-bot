# Third-Party Notices

This repository contains material derived from, or distributed together with, the following
third-party works. Each remains under its own license.

## nonebot-plugin-skland

- Source: https://github.com/FrostN0v0/nonebot-plugin-skland
- Used in: `plugins/skland_ef_theme/templates/` (`endfield_card.html.jinja2`, `endfield_macros.html.jinja2`,
  `ef_gacha.html.jinja2`, `ef_gacha_macros.html.jinja2`). These files are restyled versions of the
  templates of the same names shipped with nonebot-plugin-skland 0.7.1; the data fields and template
  logic come from the original work.
- License: MIT

```
MIT License

Copyright (c) 2025 FrostN0v0

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Barlow Condensed

- Source: https://github.com/jpt/barlow
- Used in: `plugins/ef_theme/fonts/` (`BarlowCondensed-Medium.ttf`, `BarlowCondensed-SemiBold.ttf`,
  `BarlowCondensed-Bold.ttf`), unmodified.
- License: SIL Open Font License 1.1. The full license text is included as
  `plugins/ef_theme/fonts/OFL.txt`.

## Game data and assets

Operator, weapon, stage, enemy, activity and gacha pool data are downloaded at runtime from
AKEData (https://www.akedata.wiki/) and from Skland; they are not stored in this repository.
Arknights activity and gacha pool times are downloaded at runtime from
yuanyan3060/ArknightsGameResource (https://github.com/yuanyan3060/ArknightsGameResource) and from
the public API of PRTS Wiki (https://prts.wiki/, content under CC BY-NC-SA 4.0); they are not stored
in this repository either. The banners shown in the activity calendar are fetched at runtime as well:
Endfield activity pictures from AKEData, Endfield pool banners from the game's own pool page, and
Arknights activity and pool banners from PRTS Wiki. They are cached locally and never committed.
The screenshots under `docs/images/` and `plugins/skl_help/help.webp` contain names and artwork from
Arknights and Arknights: Endfield, which are the property of their respective rights holders and
are shown only to illustrate the bot's output.
