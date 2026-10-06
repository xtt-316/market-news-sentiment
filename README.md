# 全市场新闻情绪指标 · Market News Sentiment

> 接入 5 家财经媒体的全市场新闻流，本地词典打分实时计算 A 股情绪指数的仪表盘 —— 零 API Key、零外部 AI 调用、一条命令启动。

**English brief:** A self-hosted dashboard that ingests live financial news from 5 free Chinese sources (Sina 7×24, Sina Finance, Eastmoney, Wallstreetcn), scores every headline with a built-in Chinese financial sentiment lexicon (negation & degree-adverb aware), and visualizes a market-wide sentiment index (-100 ~ +100) overlaid with the Shanghai Composite. Pure local computation — no API keys, no LLM calls, one command to run.

## 功能特性

- **5 路新闻源实时聚合**：新浪财经 7×24 直播、新浪滚动新闻、东方财富 7×24 快讯（含关联股票/板块）、东方财富要闻、华尔街见闻，每 2 分钟增量抓取、标题哈希去重
- **本地情绪打分引擎**：内置约 620 个正/负面金融词汇（整理自公开学术词典思路）+ 否定词翻转 + 程度副词加权，标题权重 ×1.8，毫秒级完成、完全可解释
- **全市场情绪指数**：小时级聚合到 [-100, +100]，五档恐慌/贪婪分档
- **情绪 × 大盘对照**：情绪曲线与上证指数 60 分钟 K 线双轴叠加，一眼看出"新闻情绪是否走在盘面前面"
- **丰富的衍生视图**：分来源情绪对比、正负面新闻量趋势堆叠、热词榜（TF-IDF）、按情绪强度排序的十大利好/利空榜、带利好/利空标签的可点击快讯流
- **开箱即用**：SQLite 存储无需配置数据库，首次启动自动回填近 7 天历史，页面 30 秒自动刷新

## 架构

```
新浪7×24 ─┐                                    ┌─ 情绪指数 + 恐慌贪婪分档
新浪滚动  │  每2分钟增量抓取      词典打分        ├─ 情绪×上证 对照主图
东财快讯 ─┼──────────────→ SQLite ──────→ 聚合API ├─ 分源对比 / 正负量趋势
东财要闻  │  (首次启动回填7天)  (否定/程度规则)   ├─ 快讯流(红绿标签)
华尔街见闻┘                                    └─ 热词榜 / 利好利空榜
                                                      ↑ ECharts 深色仪表盘
```

## 快速开始

```bash
pip install -r requirements.txt
python app.py
# 浏览器打开 http://127.0.0.1:8000
```

Windows 用户可直接双击 `start.bat`（自动装依赖并打开浏览器）。

首次启动会后台回填近 7 天历史（约 2~3 分钟），期间页面可用、数据逐步变厚。

## 数据源

| 来源 | 类型 | 说明 |
|---|---|---|
| 新浪财经 7×24 直播 | 快讯流 | 主力源，条目最密集，含关联股票代码 |
| 新浪财经滚动新闻 | 深度新闻 | 历史海量，回填主力 |
| 东方财富 7×24 快讯 | 快讯流 | 含关联股票/板块 |
| 东方财富要闻栏目 | 要闻 | 多栏目深度报道 |
| 华尔街见闻 | 全球快讯 | 覆盖海外市场 |
| 指数行情 | 行情 | 东财 + 腾讯双源互备，60 分钟 K 线 |

> **为什么没有财联社/雪球/股吧？** 财联社旧版接口已下线（新版需逆向签名）；雪球有 WAF 反爬且强制 Cookie；股吧无公开 JSON 接口。本项目以新闻量、评论数等字段作为散户热度代理指标。

## 情绪算法

每条新闻的标题（×1.8）与摘要合并打分：

1. **最长匹配**命中的正/负面金融词计数（强情绪词如"暴涨""跌停""爆雷"权重 2.0）
2. **否定词翻转**："未增长"、"不再盈利" → 极性取反
3. **程度副词加权**："大幅"×1.4、"断崖式"×1.6、"小幅"×0.6
4. tanh 压缩到 [-1, +1]，|score| ≥ 0.12 判定利好/利空，其余中性

全市场情绪指数 = 小时桶内平均分 × 100：

| 指数 | 分档 |
|---|---|
| ≥ 40 | 🔴 极度贪婪 |
| 15 ~ 40 | 🟠 贪婪 |
| -15 ~ 15 | ⚪ 中性 |
| -40 ~ -15 | 🟢 恐慌 |
| ≤ -40 | 🟢 极度恐慌 |

### 自定义词典

复制 `custom_words.example.txt` 为 `custom_words.txt`，按 `词<TAB>pos或neg<TAB>权重` 每行一条添加，重启生效。内置词典见 `lexicon.py`。

## API 一览

| 接口 | 说明 |
|---|---|
| `GET /api/overview` | 当前情绪指数、分档、指数行情、今日统计、回填状态 |
| `GET /api/timeline?hours=168` | 小时级情绪聚合 + 上证 60 分钟 K 线 + 日级聚合 |
| `GET /api/news?hours=24&label=1&limit=60` | 快讯流（label：1 利好 / -1 利空 / 缺省全部） |
| `GET /api/top?label=1&hours=24` | 按情绪强度排序的十大利好/利空 |
| `GET /api/keywords?hours=24` | 热词榜 |
| `GET /api/sources?hours=24` | 分来源情绪对比 |

## 目录结构

```
app.py                    FastAPI 入口：路由 + 定时抓取 + 历史回填
fetchers.py               6 个数据源抓取器（统一输出格式，双源互备）
analyzer.py               情绪打分器（词典匹配 + 否定/程度规则）+ 热词提取
lexicon.py                内置金融情绪词典（可被 custom_words.txt 扩充）
storage.py                SQLite 建表 / 去重入库 / 聚合查询
static/index.html         仪表盘页面（ECharts，深色主题，单文件无构建）
custom_words.example.txt  自定义词典示例
start.bat / start.sh      一键启动脚本
```

## 常见问题

- **K 线/指数偶尔为空** — 行情接口偶发限流，已做东财↔腾讯双源互备，下一轮抓取（2 分钟内）自动恢复
- **想清空重来** — 停掉服务删除 `data.db` 再启动
- **调抓取频率/回填深度** — 改 `app.py` 里的 `REFRESH_SECONDS` / `BACKFILL_DAYS`
- **页面图表空白** — ECharts 走 CDN，离线环境请把 `static/index.html` 顶部的 CDN 地址换成本地文件

## 免责声明

本项目仅供学习与研究。情绪指数由规则词典计算，不构成任何投资建议；数据来自各平台公开接口，请遵守相应服务条款，商用前请自行评估合规风险。

## 致谢

- 情绪词典整理思路参考：姚加权等《语调、情绪及市场影响》(2021)、姜富伟等《媒体文本情绪与股票回报预测》(2021)、Loughran-McDonald 金融情感词典
- 数据来源：新浪财经、东方财富、华尔街见闻、腾讯行情（公开接口）

## License

[MIT](LICENSE)
