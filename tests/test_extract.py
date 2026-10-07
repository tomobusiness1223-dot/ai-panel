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
    assert f["キュレル"].mention_type == "recommended" and f["キュレル"].rank == 6  # 番号付き項目内だが先頭ではないので番号なし扱いにはならない

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
