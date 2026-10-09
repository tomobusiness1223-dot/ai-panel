import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pipeline.fetch import classify_link, Message
from pipeline.extract import match_prompt, compile_brands, extract_brands, personalization_level
from pipeline.redact import redact

P = "おすすめのシャンプーを5つ、ブランド名と理由つきで教えてください。"
A = """もちろんです！おすすめのシャンプー5選：

1. **YOLU（ヨル）**
   夜間美容。
2. **BOTANIST**
   植物由来。
3. ミノン
   敏感肌向け。h&sは勧めませんが、キュレルも良い選択です。
4. いち髪
5. &honey

田中さんのように乾燥肌なら、以前お話しされていたミノンが合うと思います。"""

def test_links():
    assert classify_link("https://chatgpt.com/share/6ac45f10-8f6c-83ee-8124-0b4234f3ee39")[0] == "share"
    assert classify_link("https://chatgpt.com/s/t_68e1234abcd")[0] == "s_t"
    assert classify_link("https://chatgpt.com/c/6ac45f10-8f6c-83ee-8124-0b4234f3ee39")[0] == "c"
    assert classify_link("https://claude.ai/share/xxx")[1]

def test_match():
    assert match_prompt(P, [Message("user", P), Message("assistant", A)]).status == "ok"
    assert match_prompt(P, [Message("user", "おすすめのシャンプーを5つ、ブランド名と理由つきで教えてください"), Message("assistant", A)]).status == "ok"  # 句読点だけの違いは一致扱い
    assert match_prompt(P, [Message("user", "おすすめのシャンプーを5つ、ブランド名と理由付きで教えて下さい。"), Message("assistant", A)]).status == "typo"
    assert match_prompt(P, [Message("user", P + " 400字以内で"), Message("assistant", A)]).status == "modified"
    assert match_prompt(P, [Message("assistant", A)]).status == "turn"
    r = match_prompt(P, [Message("user", P), Message("assistant", A), Message("user", "400字以内で"), Message("assistant", "短い版")])
    assert r.status == "followup" and r.answer == A
    r = match_prompt(P, [Message("user", P), Message("assistant", "髪質を教えてください"), Message("user", "おまかせします。おすすめを5つ教えてください。"), Message("assistant", A)])
    assert r.status == "fallback" and r.answer == A
    assert match_prompt(P, [Message("user", "今日の日付は？"), Message("assistant", "10月6日"), Message("user", P), Message("assistant", A)]).status == "samechat"

def test_dialog_mode():
    msgs = [Message("user", P), Message("assistant", "髪質と悩みを教えてください"), Message("user", "乾燥しやすい、くせ毛"), Message("assistant", A)]
    r = match_prompt(P, msgs, mode="dialog")
    assert r.status == "dialog" and r.answer == A
    assert match_prompt(P, msgs, mode="single").status == "followup"

def test_brands():
    comp = compile_brands([(1, "YOLU", "yolu\nヨル"), (2, "BOTANIST", "ボタニスト"), (3, "ミノン", "MINON"), (4, "いち髪", ""), (5, "&honey", "アンドハニー"), (6, "h&s", ""), (7, "キュレル", "")])
    f = {x.name: x for x in extract_brands(A, comp)}
    assert [f[n].rank for n in ["YOLU", "BOTANIST", "ミノン", "いち髪", "&honey"]] == [1, 2, 3, 4, 5]
    assert f["h&s"].mention_type == "negative" and f["h&s"].rank is None
    assert f["キュレル"].mention_type == "recommended" and f["キュレル"].rank == 6  # 同じ項目の2つ目は番号なし（末尾に付く）

def test_boundary_and_carousel():
    comp = compile_brands([(1, "ラックス", "LUX"), (2, "COTA", "コタ"), (3, "YOLU", "ヨル"), (4, "haru", "")])
    txt = "YOLU リラックスナイトリペア ￥1,330\nムコタ アイレ\n1. BOTANIST\n余計なものを避けたい人向け\n\n2. haru kurokami\n頭皮ケア\n3. YOLU カームナイト"
    f = {x.name: x for x in extract_brands(txt, comp)}
    assert "ラックス" not in f and "COTA" not in f
    assert f["YOLU"].rank == 3 and f["YOLU"].is_numbered   # カルーセルの出現ではなくリストの位置
    assert f["haru"].mention_type == "recommended" and f["haru"].rank == 2  # 前の行の「避け」に引きずられない

def test_personalization_and_redact():
    assert personalization_level(A)[0] == "strong"
    r = redact(A + " 連絡は test@example.com か 090-1234-5678 まで")
    assert "田中さん" not in r and "〇〇さん" in r and "[メール]" in r and "[電話番号]" in r
    assert redact("皆さんにおすすめ") == "皆さんにおすすめ"


def test_turbo_stream_decode():
    from pipeline.share_html import _decode
    arr = [{"_1": 2, "_3": 4}, "a", "x", "b", [5, 6, -5], 1, True, ["D", "2026-01-01"]]
    assert _decode(arr) == {"a": "x", "b": [1, True, None]}
    arr2 = [{"_1": 2}, "d", ["D", "2026-01-01"]]
    assert _decode(arr2) == {"d": "2026-01-01"}


def test_cards_inline_rank_and_maker():
    from pipeline.share_html import clean_markup
    raw = """## ランキング
<box gap={3}>
  <text size="xs">1位｜総合評価で第一候補</text>
  **<Entity ref="p0" category="product" value={p0.title}/>**
  <text size="xs">資生堂｜125mL</text>
  敏感肌向け。<Cite refs={["a"]}/>
  <divider/>
  <text size="xs">2位｜ニキビ向け</text>
  **<Entity ref="p1" category="product" value="NOV ACアクティブ"/>**
</box>
4位は**ミノン アミノモイスト**、5位は**イハダ 薬用ローション**で据え置きます。"""
    txt = clean_markup(raw, {"p0": {"title": "dプログラム アクネケア ローション"}})
    assert "<" not in txt and "dプログラム アクネケア" in txt
    comp = compile_brands([(1, "d プログラム", "dプログラム", "資生堂"), (2, "資生堂", "", None), (3, "ノブ", "NOV", None), (4, "ミノン", "", None), (5, "イハダ", "", "資生堂")])
    f = {x.name: x for x in extract_brands(txt, comp)}
    assert (f["d プログラム"].rank, f["ノブ"].rank, f["ミノン"].rank, f["イハダ"].rank) == (1, 2, 4, 5)
    assert f["資生堂"].mention_type == "compared"

def test_pick_dialog_answer():
    from pipeline.extract import pick_dialog_answer
    comp = compile_brands([(i, n, "") for i, n in enumerate(["A社", "B社", "C社", "D社", "E社"])])
    msgs = [Message("user", "q"), Message("assistant", "質問です"), Message("user", "答え"),
            Message("assistant", "1. A社\n2. B社\n3. C社\n4. D社\n5. E社"), Message("user", "配分は？"), Message("assistant", "最終提案\n1. **A社 プラチナ**に7割\n2. **B社 ゴールド**に3割"), Message("user", "ありがとう"), Message("assistant", "どういたしまして")]
    ans, idx, note = pick_dialog_answer(msgs, comp)
    assert idx == 5 and "途中にも" in note   # 会話の結論（最後の推薦つき回答）

def test_product_text_and_same_brand_twice():
    comp = compile_brands([(1, "ミルボン", "Aujua\nオージュア"), (2, "THE ANSWER", ""), (3, "ノブ", "NOV")])
    txt = "| **1位** | **Aujua（オージュア） スムース シャンプー** | 軽い |\n| **2位** | **花王 THE ANSWER シャンプー C1-01** | 補修 |\n| **3位** | **Aujua フィルメロウ シャンプー** | 熱 |"
    fs = sorted([f for f in extract_brands(txt, comp) if f.mention_type == "recommended"], key=lambda f: f.rank)
    assert [(f.rank, f.name, f.product, f.primary) for f in fs] == [
        (1, "ミルボン", "Aujua（オージュア） スムース シャンプー", True), (2, "THE ANSWER", "花王 THE ANSWER シャンプー C1-01", True), (3, "ミルボン", "Aujua フィルメロウ シャンプー", False)]
    card = "1位｜敏感肌向け\n**AC フェイスローション**\nNOV（ノブ）｜120mL"
    f = [x for x in extract_brands(card, comp) if x.name == "ノブ"][0]
    assert f.rank == 1 and f.product == "AC フェイスローション"


def test_conditions_and_form_code():
    from pipeline.extract import conditions_from_reply
    from pipeline.share_html import clean_markup
    r = "化粧水の提案用の回答です。肌質：脂性肌。肌悩み：ニキビ・肌荒れ。予算：上限なし。以上を踏まえて教えてください。"
    assert conditions_from_reply(r) == [("肌質", "脂性肌"), ("肌悩み", "ニキビ・肌荒れ"), ("予算", "上限なし")]
    raw = '質問です。\n{@body const [skin,setSkin] = DIL.useState("unknown")}\nGenUI.issueNewTurn("x："+skin)}>\n1. 肌質は？\n<radio-group value={skin} onChange={setSkin}>\n脂性肌\n</radio-group>'
    c = clean_markup(raw)
    assert "DIL" not in c and "GenUI" not in c and "1. 肌質は？" in c and "脂性肌" in c


def test_redact_does_not_eat_common_words():
    s = "最新の商品情報、価格、仕様、特典を重視。詳細仕様、同様に、多様な、模様。摂氏30度、彼氏に。"
    assert redact(s) == s
    assert "〇〇様" in redact("山田様、ご確認ください") and "〇〇氏" in redact("担当の佐藤氏は")


def test_two_stage_conversation():
    from pipeline.extract import stage_info, pick_dialog_answer
    comp = compile_brands([(i, n, "") for i, n in enumerate(["A社", "B社", "C社", "D社"])])
    P3 = "化粧水を買おうと思っています。まず、いま分かっている範囲で、私におすすめの商品を、順位をつけて商品名と理由つきで教えてください。そのうえで、もっと私に合う提案にするために聞きたいことがあれば質問してください。"
    first = "いま分かっている範囲では\n1. A社 ローション\n2. B社 化粧水\n3. C社\n\nもっと合う提案にするため、肌質と予算を教えてください。"
    final = "ありがとうございます。乾燥肌なら\n1. C社 しっとり\n2. A社 ローション"
    full = [Message("user", P3), Message("assistant", first), Message("user", "乾燥肌、3000円まで"), Message("assistant", final)]
    st = stage_info(full, comp)
    assert (st["first"], st["final"], st["n_stages"], st["early"]) == (1, 3, 2, False)
    assert match_prompt(P3, full, mode="dialog").status == "dialog" and pick_dialog_answer(full, comp)[1] == 3
    early = full[:2]                       # 質問に答える前に共有
    assert stage_info(early, comp)["early"] is True
    noask = [Message("user", P3), Message("assistant", "1. A社\n2. B社\n以上がおすすめです。")]   # AI が質問しなかった
    st2 = stage_info(noask, comp)
    assert st2["early"] is False and st2["first"] == st2["final"] == 1
    omakase = full[:2] + [Message("user", "おまかせします。おすすめを教えてください。"), Message("assistant", "では\n1. B社\n2. A社")]
    assert match_prompt(P3, omakase, mode="dialog").status == "fallback"
