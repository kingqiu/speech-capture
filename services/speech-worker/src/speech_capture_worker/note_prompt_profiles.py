"""Versioned, content-type-specific instructions for evidence-grounded notes."""

from __future__ import annotations

NOTE_PROMPT_VERSION = "2026-08-02.9"

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
受访者的核心观点；对提问者应写清实际追问方向，不能只说“进行了提问”。没有明确决定或待办
时允许为空。必须保留关键问题与回答之间的对应关系：每个重要问题用具体问题或业务主题命名，
summary 直接概括受访者的回答，details 分别保留问题、回答、依据和必要追问，并同时引用双方
直接证据。允许多个 question_answer 或 experience 章节，不得只创建一个笼统“关键问答”。
scene section 的 title 必须是具体问题、观点或案例名称，不能直接使用“受访者背景、关键问答、
核心观点、判断依据、经历与案例、分歧与保留、待追问问题”等类型标签作为标题。不得把同一组
证据换词复制为观点、依据和案例三个章节；观点与依据应在同一具体主题内建立关系。
只有原文存在实质反对、矛盾或明确保留时才写 tension；“嗯、啊、哦”等停顿、口头禅、转写
含糊或思考状态不是分歧、限制或风险。只有通读后仍未获得回答的问题才写 unanswered_question
或 open_questions；同一段或后续内容已经回答的问题必须合并进问答章节。actions 只记录受访者
或提问者明确面向未来的承诺、安排或要求，不得把已经上线、已经发布、已经建立或开发完成的
案例写成后续事项。把观点、理由、经历或案例、矛盾或保留意见以及仍待追问之处分开整理。
""".strip(),
    "course": """
这是课程笔记。按知识体系组织学习目标、概念、原理、方法、步骤、案例、适用边界和可复习结论；
保留重要定义以及概念之间的关系。不要为了满足会议式结构而编造决定和待办。
speaker_summaries 只总结讲师或实质答疑者。
""".strip(),
    "speech": """
这是演讲笔记。还原演讲主题、核心论点、论据、案例、推论和结论，区分主张与事实。
演讲者的铺垫和修辞不能自动成为重点；speaker_summaries 只保留有实质内容的发言者。
如果演讲主体由多个具名项目、产品、组织或实践案例共同构成，逐一核对完整逐字稿：每个会改变
读者理解的独立案例都应作为单独的 example 或 evidence 章节展开，不能只在 summary 中点名，
也不能因为讲述较短就遗漏。保留案例的业务问题、做法、结果或阶段，以及它如何支撑核心论点。
按照实际演讲脉络组织“开场目标—主要论点或案例—反馈或限制—结论”，不要把同一句概述复制到
summary、highlights 和 scene_sections。actions 只记录演讲者明确承诺、安排或要求执行的事项；
一般性建议、听众可获得的启发和“还可继续完善”等评价应放在 implication 或 takeaway，不能写成待办。
不得把相邻但不同的案例合并命名，也不得用一个案例的名称描述另一个案例；如果无法确认项目名称，
使用原文可证实的业务场景名称。不得把“得到认可”“可以推广”等描述扩写成已经决定推广。
""".strip(),
    "voice_memo": """
这是个人语音备忘。优先整理记录意图、想法、已经形成的判断、明确任务、待验证假设、约束和
后续跟进。保留尚未做决定的探索，不要把随口设想升级成结论。不要强行生成多人观点或会议
决定；单人语音备忘的 speaker_summaries 应为空，避免把正文再复制成“说话人观点”。
scene section 的 title 必须直接写具体想法、判断、方法、任务或问题，不能直接使用“记录意图、
想法、当前判断、明确任务、待验证假设、约束、后续跟进”等类型标签。kind 只是结构元数据，
不得为了覆盖所有 kind 而把同一内容拆成多个标签式章节；“这段语音记录了……”等 Meta 描述
不能单独成章。若原文提出一套有先后关系的方法或步骤，应在一个具体方法章节中完整保留顺序、
每一步的目的与边界，不能只留一句概述，也不能把每个方法步骤都升级成实际待办。
task 和 actions 只记录说话人明确准备、要求或安排执行的未来事项；原则、建议、方法步骤和已经
完成的案例不自动构成任务。hypothesis 必须是原文尚未确认、确实需要用结果验证的命题；“使用
MVP进行验证”如果只是方法步骤，应写入方法正文，而不是伪造为待验证假设。constraint 只保留
真正影响选择或执行的成本、安全、能力、资源、时间等边界。summary、highlights、正文和下一步
各司其职，避免把同一句六步概述重复多次。
""".strip(),
    "generic": """
这是通用语音记录，仅用于内容缺少会议、访谈、课程/演讲或个人备忘的明确结构时。根据材料
本身整理实际主题、信息层级、依据、例子、结果、保留意见和真正未解决的问题。title 应是简洁
具体的内容标题，不能把整句处理目标或“梳理……”指令直接复制为标题。scene section 的 title
必须写实际主题，不能直接使用“背景、主题、核心信息、重要细节、结果、后续行动、开放问题”
等 kind 标签；kind 只是结构元数据。不得用“本次记录旨在……”“本次记录的核心信息是……”
等 Meta 套话单独成章，也不得把同一概述换标签复制为多个章节。
如果一段概述中实际包含分类、能力模块、实施思路等多个独立信息层，应按具体内容拆开并保留
各自例子或依据，而不是只留下笼统摘要。区分已经完成的材料、暂定思路、明确结果和未来承诺：
“已经整理方案图”不是待办；“后续从哪些需求切入仍待确认”不是已确认决定或明确行动。
actions 只记录原文明示的未来承诺、安排或执行要求；没有则为空。单人说明性材料的
speaker_summaries 应为空，避免把正文再复制成“说话人观点”。无法确认的字段允许为空，不得
用套话补足数量。
""".strip(),
}

_SCENE_SECTION_KINDS = {
    "interview": (
        "interviewee_background",
        "question_answer",
        "viewpoint",
        "reasoning",
        "experience",
        "tension",
        "unanswered_question",
    ),
    "course": (
        "learning_objective",
        "concept",
        "principle",
        "method",
        "example",
        "limitation",
        "takeaway",
    ),
    "speech": (
        "theme",
        "argument",
        "evidence",
        "example",
        "implication",
        "takeaway",
    ),
    "voice_memo": (
        "intent",
        "idea",
        "judgment",
        "task",
        "hypothesis",
        "constraint",
        "follow_up",
    ),
    "generic": (
        "context",
        "theme",
        "insight",
        "detail",
        "outcome",
        "action",
        "open_question",
    ),
}

_SCENE_SECTION_LABELS = {
    "interview": {
        "interviewee_background": "受访者背景",
        "question_answer": "关键问答",
        "viewpoint": "核心观点",
        "reasoning": "判断依据",
        "experience": "经历与案例",
        "tension": "分歧与保留",
        "unanswered_question": "待追问问题",
    },
    "course": {
        "learning_objective": "学习目标",
        "concept": "核心概念",
        "principle": "原理",
        "method": "方法与步骤",
        "example": "案例",
        "limitation": "适用边界",
        "takeaway": "复习要点",
    },
    "speech": {
        "theme": "演讲主题",
        "argument": "核心论点",
        "evidence": "论据",
        "example": "案例",
        "implication": "推论与影响",
        "takeaway": "结论与启发",
    },
    "voice_memo": {
        "intent": "记录意图",
        "idea": "想法",
        "judgment": "当前判断",
        "task": "明确任务",
        "hypothesis": "待验证假设",
        "constraint": "约束",
        "follow_up": "后续跟进",
    },
    "generic": {
        "context": "背景",
        "theme": "主题",
        "insight": "核心信息",
        "detail": "重要细节",
        "outcome": "结果",
        "action": "后续行动",
        "open_question": "开放问题",
    },
}

_RENDER_HEADINGS = {
    "meeting": {
        "context": "背景与参与方",
        "highlights": "核心结论",
        "body": "议题与讨论",
        "speakers": "参与者与各方观点",
        "decisions": "已确认的决定",
        "actions": "待办事项",
        "risks": "风险与注意事项",
        "questions": "未决问题",
    },
    "interview": {
        "context": "访谈背景与人物",
        "highlights": "访谈核心洞察",
        "body": "访谈内容",
        "speakers": "访谈双方与观点",
        "decisions": "明确结论",
        "actions": "后续事项",
        "risks": "限制与注意事项",
        "questions": "待追问问题",
    },
    "course": {
        "context": "课程背景",
        "highlights": "学习要点",
        "body": "知识结构",
        "speakers": "讲师与答疑观点",
        "decisions": "明确结论",
        "actions": "练习与后续学习",
        "risks": "适用边界与注意事项",
        "questions": "待理解问题",
    },
    "speech": {
        "context": "演讲背景",
        "highlights": "核心论点",
        "body": "演讲脉络",
        "speakers": "演讲者观点",
        "decisions": "明确结论",
        "actions": "明确后续事项",
        "risks": "限制与注意事项",
        "questions": "开放问题",
    },
    "voice_memo": {
        "context": "记录背景",
        "highlights": "核心思路",
        "body": "想法整理",
        "speakers": "相关人物观点",
        "decisions": "已经形成的判断",
        "actions": "下一步",
        "risks": "约束与风险",
        "questions": "待验证问题",
    },
    "generic": {
        "context": "内容背景",
        "highlights": "核心信息",
        "body": "内容结构",
        "speakers": "相关人物观点",
        "decisions": "明确结论",
        "actions": "后续事项",
        "risks": "风险与注意事项",
        "questions": "开放问题",
    },
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

    return (
        _COMMON_SALIENCE + "\n" + _PROFILE_GUIDANCE.get(content_type, _PROFILE_GUIDANCE["generic"])
    )


def scene_section_kinds(content_type: str) -> tuple[str, ...]:
    """Return the evidence section kinds allowed for a non-meeting note profile."""

    return _SCENE_SECTION_KINDS.get(content_type, ())


def scene_section_labels(content_type: str) -> dict[str, str]:
    """Return stable human labels for scene-specific section kinds."""

    return dict(_SCENE_SECTION_LABELS.get(content_type, {}))


def render_headings(content_type: str) -> dict[str, str]:
    """Return note headings tailored to the content type."""

    return dict(_RENDER_HEADINGS.get(content_type, _RENDER_HEADINGS["generic"]))


def output_contract_guidance(content_type: str) -> str:
    """Describe the scene-specific document contract without imposing fixed counts."""

    kinds = scene_section_kinds(content_type)
    if not kinds:
        return (
            "按实际内容决定 highlights 和 topics 的数量，不得为了达到固定条数而填充；"
            "没有证据的字段返回空数组。"
        )
    labels = scene_section_labels(content_type)
    choices = "、".join(f"{kind}（{labels[kind]}）" for kind in kinds)
    return (
        "scene_sections 是最终笔记的场景专用正文结构；kind 只能从以下类型选择："
        f"{choices}。每项必须写清 title、summary、details 和 evidence；只创建原文确有"
        "信息的章节，允许同类出现多项，也允许不适用的类型完全不出现。topics 仅作为兼容的"
        "通用索引，不得替代 scene_sections。highlights、topics 和 scene_sections 的数量都由"
        "内容决定，不得为了固定条数填充。逐一检查完整逐字稿中的具名人物、组织、项目、产品、"
        "数字和独立案例：凡是理解主题所必需且有直接证据的内容，都必须进入合适章节，不能只在"
        "概述中一带而过。"
    )
