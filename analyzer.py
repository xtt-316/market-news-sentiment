# -*- coding: utf-8 -*-
"""新闻情绪打分器：词典匹配 + 否定词翻转 + 程度副词加权

输出 score ∈ [-1, 1]（tanh 压缩），label: 1利好 / -1利空 / 0中性。
另提供热词提取（jieba + 词性 + 停用词过滤）。
"""

import math
import re

import jieba
import jieba.analyse

from lexicon import (POSITIVE, NEGATIVE, NEGATORS, DEGREE_ADV, STOPWORDS)

# 预处理：去掉对情绪无意义的符号噪声
_CLEAN_RE = re.compile(r"[\|◎◆▲▼●■【】\[\]（）(){}]+")
# 最长情绪词长度（用于窗口匹配上限）
_MAX_WORD_LEN = max(max(len(w) for w in POSITIVE), max(len(w) for w in NEGATIVE))
_MAX_DEG_LEN = max(len(w) for w in DEGREE_ADV) if DEGREE_ADV else 2

# 按长度分桶加速查找：{长度: 词集合}
_POS_BY_LEN = {}
for _w in POSITIVE:
    _POS_BY_LEN.setdefault(len(_w), set()).add(_w)
_NEG_BY_LEN = {}
for _w in NEGATIVE:
    _NEG_BY_LEN.setdefault(len(_w), set()).add(_w)

_ALL_LENS = sorted(_POS_BY_LEN.keys() | _NEG_BY_LEN.keys(), reverse=True)


def _match_at(text, i):
    """从位置 i 起做最长优先的情绪词匹配，返回 (词, 极性, 权重) 或 None"""
    for L in _ALL_LENS:
        if i + L > len(text):
            continue
        seg = text[i:i + L]
        if L in _POS_BY_LEN and seg in _POS_BY_LEN[L]:
            return seg, +1, POSITIVE[seg]
        if L in _NEG_BY_LEN and seg in _NEG_BY_LEN[L]:
            return seg, -1, NEGATIVE[seg]
    return None


def _negated_before(text, start, window=3):
    """情绪词前 window 个字符内是否有否定词"""
    lo = max(0, start - window)
    for n in NEGATORS:
        if n in text[lo:start]:
            return True
    return False


def _degree_before(text, start, window=4):
    """情绪词前的程度副词系数（取窗口内命中的最大系数）"""
    lo = max(0, start - window)
    seg = text[lo:start]
    best = 1.0
    for adv, mult in DEGREE_ADV.items():
        if adv in seg and mult > best:
            best = mult
    return best


def score_text(title, summary=""):
    """对一条新闻打分。标题命中权重 ×1.8。

    返回 dict(score, label, label_text, pos_words, neg_words)
    """
    title = _CLEAN_RE.sub(" ", title or "")
    summary = _CLEAN_RE.sub(" ", summary or "")

    pos_sum = 0.0
    neg_sum = 0.0
    pos_words, neg_words = [], []

    for text, base_mult in ((title, 1.8), (summary, 1.0)):
        if not text:
            continue
        i = 0
        while i < len(text):
            hit = _match_at(text, i)
            if hit is None:
                i += 1
                continue
            word, pol, weight = hit
            mult = base_mult * _degree_before(text, i)
            if _negated_before(text, i):
                pol = -pol  # 否定翻转：未增长 → 负
                mult *= 0.8  # 否定语义通常弱于直接表述
            if pol > 0:
                pos_sum += weight * mult
                pos_words.append(word)
            else:
                neg_sum += weight * mult
                neg_words.append(word)
            i += len(word)

    raw = pos_sum - neg_sum
    score = math.tanh(raw / 2.0)
    if score >= 0.12:
        label, label_text = 1, "利好"
    elif score <= -0.12:
        label, label_text = -1, "利空"
    else:
        label, label_text = 0, "中性"
    return {
        "score": round(score, 4),
        "label": label,
        "label_text": label_text,
        "pos_words": pos_words,
        "neg_words": neg_words,
    }


# ---------------------------------------------------------------- 热词提取
_TERM_FILTER = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9]+$")


def extract_keywords(title, summary="", topk=8):
    """TF-IDF 关键词（jieba.analyse），过滤停用词与单字"""
    text = (title or "") + "。" + (summary or "")
    words = []
    for w, _w in jieba.analyse.extract_tags(text, topK=topk * 3, withWeight=True):
        if len(w) < 2 or w in STOPWORDS or w.isdigit() or not _TERM_FILTER.match(w):
            continue
        words.append(w)
        if len(words) >= topk:
            break
    return words


if __name__ == "__main__":
    cases = [
        ("央行宣布降准0.5个百分点，释放长期资金约1万亿元", "此次降准超出市场预期，A股金融板块应声上涨"),
        ("某公司业绩爆雷，上半年巨亏20亿元，股价跌停", "公司同时公告被证监会立案调查"),
        ("美联储官员发表讲话，市场关注后续利率路径", ""),
        ("三大指数集体收跌，创业板指跌超2%", "北向资金净流出超50亿元，两市成交缩量"),
        ("新能源汽车销量创新高，产业链公司订单饱满", ""),
    ]
    for t, s in cases:
        r = score_text(t, s)
        print(f"[{r['label_text']}] {r['score']:+.3f}  {t[:24]}  正:{r['pos_words']} 负:{r['neg_words']}")
        print("   热词:", extract_keywords(t, s, 5))
