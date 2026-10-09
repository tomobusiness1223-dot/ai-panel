"""保存前の伏字処理（仕様書3.3）。人名・メール・電話・住所・生年月日。"""
import re
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(0\d{1,4}[-‐ー]?\d{1,4}[-‐ー]?\d{3,4})(?!\d)")
# 「田中さん」「佐々木様」「山田氏」。漢字・かな1〜4文字＋敬称。一般語（皆さん・お客さん・患者さん等）は除外
NAME = re.compile(r"([\u4e00-\u9fff]{1,4}|[\u30a0-\u30ff]{2,6})(さん|様|氏|くん|ちゃん)(?=[のはがにもとへでをや、。,.!！?？\s）)」\n]|$)")
NAME_STOP = {"皆", "客", "子", "嬢", "宅", "娘", "孫", "嫁", "婿", "姑", "舅", "神", "仏", "王", "姫", "殿", "みな", "お客", "患者", "お子", "子供", "娘", "息子", "奥", "旦那", "お母", "お父", "母", "父", "妻", "夫", "兄", "姉", "弟", "妹", "赤ちゃん", "赤", "店員", "先生", "美容師", "担当", "業者", "お医者", "医者", "看護師", "彼氏", "彼女", "友達", "ご家族", "家族", "ご主人", "主人", "お兄", "お姉", "おじい", "おばあ", "おじ", "おば", "お嬢", "お宅", "みなさ"}
ADDRESS = re.compile(r"(東京都|北海道|(?:京都|大阪)府|[一-鿿]{2,3}県)[一-鿿]{1,6}(市|区|町|村)[一-鿿\d丁目番地号ー－-]{0,20}")
BIRTH = re.compile(r"(19|20)\d{2}年\s?\d{1,2}月\s?\d{1,2}日生?まれ?")

# 「様」「氏」で終わる普通の言葉（仕様、同様、模様、彼氏、摂氏 など）。語の末尾がこれなら人名として扱わない
SAMA_WORDS = ("仕", "模", "多", "同", "異", "一", "各", "有", "逆", "左", "無", "態", "図", "紋", "今", "外", "何", "貴", "上", "神", "仏", "王", "殿", "姫", "皆", "客", "奥", "別", "不", "両")
SHI_WORDS = ("彼", "同", "両", "諸", "源", "平", "華", "摂", "某", "各")

def _name_sub(m: re.Match) -> str:
    base, hon = m.group(1), m.group(2)
    if base in NAME_STOP:
        return m.group(0)
    if hon == "様" and base.endswith(SAMA_WORDS):
        return m.group(0)
    if hon == "氏" and base.endswith(SHI_WORDS):
        return m.group(0)
    return "〇〇" + m.group(2)

def redact(text: str, known_names: list[str] | None = None) -> str:
    t = EMAIL.sub("[メール]", text)
    t = PHONE.sub("[電話番号]", t)
    t = BIRTH.sub("[生年月日]", t)
    t = ADDRESS.sub(lambda m: m.group(1) + "[住所]", t)
    t = NAME.sub(_name_sub, t)
    for n in known_names or []:
        if n and len(n) >= 2:
            t = t.replace(n, "〇〇")
    return t
