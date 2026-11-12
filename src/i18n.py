"""
多语言支持模块 — LinkedIn UI 在不同语言下的按钮/标签文本映射。
支持: 英文 (EN), 中文 (ZH), 荷兰语 (NL)
"""

# Easy Apply 按钮文本
EASY_APPLY_TEXTS = [
    "easy apply",           # EN
    "快速申请",              # ZH (Quick Apply)
    "gemakkelijk solliciteren",  # NL
    "apply",                # EN — LinkedIn 新版 UI 卡片上只显示 "Apply"
]

# 普通 Apply 按钮文本（可能是外部跳转）
APPLY_TEXTS = [
    "apply",
    "申请",
    "solliciteren",
]

# 外部投递（Apply on company website）
EXTERNAL_APPLY_TEXTS = [
    "apply on company website",
    "apply on company site",
    "在雇主公司网站上申请",
    "去公司网站申请",
    "公司网站申请",
    "solliciteer op de website van het bedrijf",
    "op de website van het bedrijf solliciteren",
    "solliciteren op site van werkgever",
    "solliciteren op de website van de werkgever",
    "apply externally",
    "apply through company site",
]

# Easy Apply 弹窗步骤按钮
NEXT_TEXTS = [
    "next", "下一步", "volgende", "continue", "doorgaan",
]
REVIEW_TEXTS = [
    "review", "审核", "controleren",
]
SUBMIT_TEXTS = [
    "submit application", "提交申请", "sollicitatie indienen", "submit",
]
DONE_TEXTS = [
    "done", "完成", "klaar",
]

# 提交成功确认
SUCCESS_TEXTS = [
    "your application was sent",
    "your application was successfully submitted",
    "您的申请已发送",
    "je sollicitatie is verzonden",
    "application submitted",
    "已发送申请",
    "thank you for your application",
    "thank you for applying",
    "we have received your application",
    "thanks for applying",
]

# 表单字段 Label
FIELD_LABELS = {
    # 邮箱
    "email": ["email", "e-mail", "e-mailadres", "电子邮件", "邮箱"],
    # 电话区号
    "phone_country": ["phone country", "landcode", "电话国家代码", "电话区号"],
    # 电话号码
    "phone": ["phone", "mobile", "telefoon", "mobiel", "电话", "手机"],
    # 城市
    "city": ["city", "stad", "woonplaats", "城市"],
    # 名
    "first_name": ["first name", "voornaam", "名字", "名"],
    # 姓
    "last_name": ["last name", "achternaam", "姓氏", "姓"],
    # 简历
    "resume": ["resume", "cv", "bijlage", "简历", "上传简历", "upload", "uploaden"],
    # 工作授权
    "work_auth": ["authorized", "eligible", "right to work", "geautoriseerd", "werkvergunning", "工作授权"],
    # 签证
    "sponsorship": ["sponsor", "visa", "werkvisum", "赞助", "签证"],
    # 薪资
    "salary": ["salary", "compensation", "salaris", "vergoeding", "薪资", "薪水"],
    # 学历
    "education": ["education", "degree", "opleiding", "diploma", "教育", "学历"],
}
