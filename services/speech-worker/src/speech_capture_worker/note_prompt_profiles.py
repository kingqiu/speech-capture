"""Versioned, content-type-specific instructions for evidence-grounded notes."""

from __future__ import annotations

NOTE_PROMPT_VERSION = "2026-08-01.19"

_COMMON_SALIENCE = """
信息优先级不是由发言长度、措辞宏大或出现顺序决定。请依次判断：
1. 这次记录为何发生、涉及哪些人或组织、各方关系和要解决的问题；
2. 明确确认的结论、边界、承诺、负责人、时间和下一步；
3. 被多位参与者回应、反复确认、存在分歧或会影响结果的内容；
4. 能解释方案选择的业务现状、数字、约束、风险和成功标准；
5. 单方的案例、类比、行业背景和泛泛观点只能作为支撑，不能挤占真正结论。
寒暄、口号、离题延伸、自我宣传和没有落到本次记录目标上的长篇举例不得列为核心结论。
""".strip()

_PROFILE_GUIDANCE = {
    "meeting": """
这是会议纪要。必须先还原会议背景、参与方、会议目标和合作关系，再整理议题。
参与方只包括本次会议中实际出现的个人或组织；过去任职公司、客户案例和被举例的机构不是参与方。
如果开场明确介绍了公司、人物、能力或合作背景，必须在 context 中单独保留 organization 或
participant 项，并在 topics 中保留与本次合作有关的实质信息，不能被后面的长篇方法论覆盖。
会议正文应按实际内容组织为少量互不重复的主题，不得为了凑数量拆分或填充。
区分“提议/设想”“讨论中的观点”“明确达成的决定”，不得把单方主张写成会议共识。
必须按时间追踪同一议题的“初始提议—他方修正或反对—最终方向”；后续出现的明确修正不能被
前面的提议覆盖，也不能把互相竞争的方案合并成一个含糊结论。
对多人会议，按 speaker_id 汇总有实质发言者的核心主张、承诺、顾虑和与其他方的分歧；
display_name、affiliation、role 只有原文明示或可由明确介绍唯一确认时才填写，否则留空。
待办应尽量说明由谁做什么、何时完成；原文没有负责人或期限时留空，不能猜测。
方法论、原则和建议通常属于主题或观点，不自动构成决定、待办、风险或未决问题。
决定、待办、风险、未决问题四类之间不得复制同一句内容；没有明确证据的类别必须返回空数组。
""".strip(),
    "interview": """
这是访谈笔记。优先说明受访者背景、访谈目的、关键经历、判断依据、具体案例和可验证事实。
区分受访者观点与采访者提问，不要把问题本身写成结论。speaker_summaries 应突出双方角色与
受访者的核心观点；没有明确决定或待办时允许为空。
""".strip(),
    "course": """
这是课程笔记。按知识体系组织概念、原理、方法、步骤、案例、限制和可复用结论；保留重要定义。
不要为了满足会议式结构而编造决定和待办。speaker_summaries 只总结讲师或实质答疑者。
""".strip(),
    "speech": """
这是演讲笔记。还原演讲主题、核心论点、论据、案例和结论，区分主张与事实。
演讲者的铺垫和修辞不能自动成为重点；speaker_summaries 只保留有实质内容的发言者。
""".strip(),
    "voice_memo": """
这是个人语音备忘。优先整理记录目的、想法、判断、明确任务、约束和仍需确认的问题。
不要强行生成多人观点或会议决定；speaker_summaries 可以为空。
""".strip(),
    "generic": """
这是通用语音记录。根据内容本身确定背景、目标、主要观点、结论、行动与风险；
无法确认的场景特有字段允许为空，不得用套话补足数量。
""".strip(),
}


def extraction_guidance(content_type: str) -> str:
    """Return scene-aware guidance for batch-level candidate extraction."""

    return (
        _COMMON_SALIENCE
        + "\n"
        + _PROFILE_GUIDANCE.get(content_type, _PROFILE_GUIDANCE["generic"])
        + "\n本批应覆盖重要背景实体、人物或组织关系以及明确事实，不能只摘取抽象观点。"
    )


def synthesis_guidance(content_type: str) -> str:
    """Return scene-aware guidance for the final full-transcript synthesis."""

    return _COMMON_SALIENCE + "\n" + _PROFILE_GUIDANCE.get(
        content_type, _PROFILE_GUIDANCE["generic"]
    )
