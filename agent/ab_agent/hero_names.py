"""英雄 ID → 中文名映射（终端展示用，票据 07）。

来源：Brotato 1.1.15.4 官方中文译名（人工整理）。
票据 11 已生成 `docs/knowledge/characters.json`（含全部 50 名英雄的官方译名）；
终端展示切换到知识库读取待后续票据处理，本表在接线前继续作为缺省来源。
未收录的 ID 直接展示原始 ID（`hero_display`），不阻塞流程。
"""
from __future__ import annotations

#: `character_data.my_id` → 中文名（游戏语言 zh）
HERO_NAMES = {
    "character_apprentice": "学徒",
    "character_arms_dealer": "军火商",
    "character_artificer": "技工",
    "character_baby": "宝宝",
    "character_beast_master": "驯兽师",
    "character_brawler": "斗殴者",
    "character_bull": "公牛",
    "character_chunky": "小胖",
    "character_crazy": "疯子",
    "character_cryptid": "神秘生物",
    "character_cyborg": "赛博格",
    "character_demon": "恶魔",
    "character_doctor": "医生",
    "character_engineer": "工程师",
    "character_entrepreneur": "企业家",
    "character_explorer": "探险家",
    "character_farmer": "农夫",
    "character_fisherman": "渔夫",
    "character_generalist": "多面手",
    "character_ghost": "幽灵",
    "character_gladiator": "角斗士",
    "character_glutton": "暴食者",
    "character_golem": "魔像",
    "character_hunter": "猎人",
    "character_jack": "杰克",
    "character_king": "国王",
    "character_knight": "骑士",
    "character_lich": "巫妖",
    "character_loud": "大嗓门",
    "character_lucky": "幸运儿",
    "character_mage": "法师",
    "character_masochist": "受虐狂",
    "character_multitasker": "多任务者",
    "character_mutant": "变异体",
    "character_old": "老人",
    "character_one_arm": "独臂",
    "character_pacifist": "和平主义者",
    "character_ranger": "游侠",
    "character_renegade": "叛徒",
    "character_saver": "守财奴",
    "character_sick": "病人",
    "character_soldier": "士兵",
    "character_speedy": "极速者",
    "character_streamer": "主播",
    "character_technomage": "科技法师",
    "character_vagabond": "流浪者",
    "character_vampire": "吸血鬼",
    "character_well_rounded": "全能者",
    "character_wildling": "野人",
    "character_wounded": "伤员",
}


def hero_display(hero_id: str) -> str:
    """终端展示串：``character_ranger（游侠）``；未收录/为空时回退原始 ID。"""
    if not hero_id:
        return "未知"
    name = HERO_NAMES.get(hero_id)
    return "%s（%s）" % (hero_id, name) if name else hero_id
