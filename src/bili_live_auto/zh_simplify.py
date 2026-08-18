"""Convert generated subtitle text to simplified Chinese."""

from __future__ import annotations

from functools import lru_cache

try:  # Optional for source-only installs; the packaged client includes it.
    from opencc import OpenCC
except ImportError:  # pragma: no cover - depends on the local environment
    OpenCC = None  # type: ignore[assignment,misc]


# This fallback keeps a minimal source checkout usable. The Windows client
# bundles OpenCC's complete t2s dictionary, so normal use is fully covered.
_FALLBACK_TABLE = str.maketrans({
    "萬": "万", "與": "与", "為": "为", "這": "这", "個": "个",
    "測": "测", "試": "试", "臺": "台", "灣": "湾", "國": "国", "門": "门",
    "後": "后", "來": "来", "開": "开", "發": "发", "現": "现", "說": "说",
    "話": "话", "時": "时", "間": "间", "長": "长", "點": "点", "對": "对",
    "於": "于", "場": "场", "從": "从", "經": "经", "濟": "济", "業": "业",
    "無": "无", "專": "专", "產": "产", "實": "实", "際": "际", "號": "号",
    "題": "题", "問": "问", "麼": "么", "們": "们", "會": "会", "機": "机",
    "關": "关", "係": "系", "進": "进", "過": "过", "還": "还", "應": "应",
    "該": "该", "讓": "让", "語": "语", "電": "电", "腦": "脑", "網": "网",
    "絡": "络", "廣": "广", "東": "东", "體": "体", "裡": "里", "見": "见",
    "視": "视", "覺": "觉", "畫": "画", "轉": "转", "錄": "录", "製": "制",
    "傳": "传", "統": "统", "簡": "简", "記": "记", "憑": "凭", "證": "证",
    "區": "区", "別": "别",
})


@lru_cache(maxsize=1)
def _converter():
    if OpenCC is None:
        return None
    try:
        return OpenCC("t2s")
    except Exception:
        return None


def to_simplified(text: str) -> str:
    """Return *text* in simplified Chinese without changing its timing."""

    value = str(text)
    converter = _converter()
    if converter is not None:
        try:
            return converter.convert(value)
        except Exception:
            pass
    return value.translate(_FALLBACK_TABLE)

