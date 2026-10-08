"""初期データ：カテゴリ、お題（質問文）、ブランド辞書。検証実験の辞書を流用し、2回目の収集用カテゴリを追加。
実行: .venv/bin/python seed.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from sqlalchemy import select
from server.db import SessionLocal, init_db, Category, Brand, Topic

PROMPT_V1 = "おすすめの{cat}を5つ、{unit}と理由つきで教えてください。"
PROMPT = "おすすめの{cat}を5つ、{unit}と理由つきで教えてください。私に合った提案にするために、必要なら先に質問して、回答の質を上げてください。"  # v2：ヒアリングあり
CATEGORIES = [
    # key, 表示名, 質問に入れる名前, 単位の言い方, 型
    ("toner", "化粧水", "化粧水", "ブランド名", "unknown"),
    ("credit_card", "クレジットカード", "クレジットカード", "カード名", "unknown"),
    ("protein", "プロテイン", "プロテイン", "ブランド名", "unknown"),
    ("shampoo", "シャンプー", "シャンプー", "ブランド名", "diverse"),
    ("earbuds", "ワイヤレスイヤホン", "ワイヤレスイヤホン", "ブランド名", "consensus"),
    ("vod", "動画配信サービス", "動画配信サービス", "サービス名", "consensus"),
]
# ブランド辞書：正規名 → 別名の正規表現（改行区切り、大小文字無視）。canonical 自体は自動で含める。
BRANDS = {
 "toner": {
  "ハトムギ化粧水": "ハトムギ|naturie|ナチュリエ", "ノブ": "NOV", "オードムーゲ": "", "アクネバリア": "ペアアクネ|ペア ", "ファンケル アクネケア": "", "エトヴォス": "", "オルビス クリアフル": "クリアフル", "プロアクティブ": "Proactiv", "シカペア": "Dr\\.?Jart|ドクタージャルト", "クレ・ド・ポー ボーテ": "クレドポー", "アスタリフト": "ASTALIFT", "カネボウ": "KANEBO", "ルルルン": "LuLuLun", "明色": "明色美顔水|美顔水", "ビフェスタ": "Bifesta", "サナ": "SANA", "ONE BY KOSE": "ワンバイコーセー", "コーセー": "KOSE|KOSÉ", "花王": "Kao", "肌ラボ": "肌ラボ|極潤|hadalabo", "キュレル": "curel", "無印良品": "無印", "イプサ": "IPSA",
  "オルビス": "ORBIS", "アルビオン": "ALBION|スキンコンディショナー", "SK-II": "SK2|SKII|エスケーツー", "ファンケル": "FANCL", "エリクシール": "ELIXIR",
  "ちふれ": "CHIFURE", "イハダ": "IHADA", "ミノン": "MINON", "菊正宗": "日本酒の化粧水", "メラノCC": "メラノ", "雪肌精": "SEKKISEI",
  "アクアレーベル": "AQUALABEL", "ドクターシーラボ": "Dr\\.?Ci:?Labo|シーラボ", "コスメデコルテ": "DECORTE|デコルテ", "アヌア": "Anua", "トリデン": "Torriden",
  "ラ ロッシュ ポゼ": "ラロッシュポゼ|La Roche-?Posay", "セタフィル": "Cetaphil", "ビオデルマ": "Bioderma", "ネイチャーリパブリック": "Nature Republic",
  "ロート製薬": "ロート", "資生堂": "SHISEIDO", "ソフィーナ": "SOFINA", "d プログラム": "dプログラム|d-program", "肌美精": "", "なめらか本舗": "豆乳イソフラボン",
  "トランシーノ": "TRANSINO", "イニスフリー": "innisfree", "VT": "VT ?Cosmetics|シカ", "CNP": "", "魔女工場": "Manyo", "ラネージュ": "LANEIGE", "ETVOS": "エトヴォス",
  "ハーバー": "HABA", "オバジ": "Obagi", "キールズ": "Kiehl'?s", "クリニーク": "CLINIQUE", "ランコム": "LANCOME|LANCÔME", "エスト": "est", "ポーラ": "POLA",
 },
 "credit_card": {
  "楽天カード": "楽天", "三井住友カード": "三井住友|SMBC|NL", "JCBカード W": "JCB ?CARD ?W", "JCBザ・クラス": "ザ・クラス|THE CLASS", "JCBゴールド": "JCBゴールド ザ・プレミア", "JCBプラチナ": "", "JALカード": "JAL・JCBカード|JALプラチナ|JAL CLUB-A|JALカード", "ANAカード": "ANA ?VISA|ANA ?JCB|ANAアメックス|ANAカード", "楽天プレミアムカード": "楽天プレミアム", "三井住友カード プラチナプリファード": "プラチナプリファード", "三井住友カード ゴールド": "ゴールド（NL）|ゴールドNL", "ラグジュアリーカード": "Luxury Card", "エポスゴールドカード": "エポスゴールド", "セゾンプラチナ・ビジネス・アメックス": "セゾンプラチナ", "dカード GOLD": "dカード ?GOLD|dカードゴールド", "ヒルトン・オナーズ アメックス": "ヒルトン", "PayPayカード": "PayPay", "イオンカード": "イオン", "エポスカード": "エポス|EPOS",
  "dカード": "dcard", "au PAYカード": "au ?PAY", "リクルートカード": "リクルート", "セゾンカード": "セゾン|SAISON", "ライフカード": "ライフ",
  "アメリカン・エキスプレス": "アメックス|American Express|AMEX", "ビューカード": "ビュー|VIEW", "Oliveフレキシブルペイ": "Olive",
  "三菱UFJカード": "三菱UFJ|MUFG", "Marriott Bonvoyアメックス": "Marriott|マリオット", "ダイナースクラブ": "ダイナース|Diners", "Amazon Mastercard": "Amazon",
  "メルカード": "メルカリ|mercard", "Visa LINE Payクレジットカード": "LINE ?Pay|LINEクレカ", "セブンカード・プラス": "セブンカード|nanaco", "ルミネカード": "ルミネ",
  "ヤフーカード": "Yahoo", "Orico Card": "オリコ|Orico", "P-oneカード": "P-one", "ACマスターカード": "ACマスター", "プロミスVisa": "プロミス",
 },
 "protein": {
  "ザバス": "SAVAS|明治", "マイプロテイン": "Myprotein|My ?Protein", "ビーレジェンド": "be ?LEGEND", "DNS": "", "VALX": "バルクス", "ゴールドスタンダード": "Gold Standard|Optimum Nutrition|オプティマム",
  "ウイダー": "ウィダー|Weider|森永", "ULTORA": "ウルトラ", "エクスプロージョン": "X-?PLOSION", "LÝFT": "LYFT|リフト", "タンパクオトメ": "", "アルプロン": "ALPRON",
  "ニチガ": "NICHIGA", "グロング": "GronG", "ハレオ": "HALEO", "ケンタイ": "Kentai|健康体力研究所", "ボディウイング": "Bodywing", "FIXIT": "", "REYS": "レイズ",
  "ザバス ミルクプロテイン": "ミルクプロテイン", "MARUKOME": "大豆プロテイン", "KANEKA": "", "バルクスポーツ": "Bulk ?Sports", "ファインラボ": "FINE ?LAB", "ゴールドジム": "GOLD'?S GYM",
  "Choice": "チョイス", "ANOMA": "アノマ", "SIXPACK": "", "Impact ホエイ": "Impact", "MUSASHI": "",
 },
 "shampoo": {
  "YOLU": "yolu|ヨル", "BOTANIST": "ボタニスト", "ミノン": "MINON", "THE ANSWER": "ジアンサー|アンサー", "いち髪": "いちかみ|いち髪ノ", "melt": "メルト", "無印良品": "無印",
  "&honey": "アンドハニー|＆honey|&Honey", "plus eau": "プリュスオー", "キュレル": "", "ディアボーテ": "HIMAWARI", "haru": "", "hiritu": "ヒリツ", "カウブランド": "", "ダイアン": "Diane|モイストダイアン|モイスト・ダイアン",
  "パンテーン": "Pantene", "ラックス": "LUX", "エッセンシャル": "Essential", "TSUBAKI": "ツバキ", "メリット": "", "h&s": "ヘッド＆ショルダーズ|Head & Shoulders", "スカルプD": "",
  "サクセス": "", "ジュレーム": "", "セグレタ": "", "オルビス": "ORBIS", "ミルボン": "Aujua|オージュア", "ラサーナ": "", "THERATIS": "セラティス", "ジョンマスター": "ジョンマスターオーガニック",
  "MARO": "MARO17", "unlabel": "アンレーベル", "ケラスターゼ": "KERASTASE|Kérastase|Kerastase", "資生堂プロフェッショナル": "SUBLIMIC|サブリミック", "ロレアル": "L'Or[ée]al|ロレアルプロ|セリエ エクスパート", "ナプラ": "N\\.", "ルベル": "Lebel|イオ ", "モロッカンオイル": "Moroccanoil", "ホーユー": "プロマスター|hoyu", "デミ": "DEMI|フローディア", "ナンバースリー": "no3|プロアクション", "ケアテクト": "", "ハホニコ": "", "ボズレー": "Bosley", "スカルプD": "", "オルナオーガニック": "ALLNA", "エイトザタラソ": "8 THE THALASSO", "クレージュ": "CLAYGE", "ステラシード": "", "ウルリス": "ululis", "Dove": "ダヴ", "クラシエ": "", "COTA": "コタ", "ウルオス": "", "デ・オウ": "", "ルシード": "", "シーブリーズ": "", "オクト": "",
 },
 "earbuds": {
  "Sony": "ソニー|SONY|WF-", "Apple": "AirPods|アップル", "Bose": "BOSE", "Technics": "テクニクス|Panasonic|パナソニック", "Anker": "Soundcore|サウンドコア", "Google": "Pixel Buds",
  "Sennheiser": "ゼンハイザー", "Samsung": "Galaxy Buds|サムスン", "Jabra": "", "Nothing": "", "JBL": "", "audio-technica": "オーディオテクニカ|オーテク", "AVIOT": "", "final": "", "EarFun": "",
  "Xiaomi": "", "JVC": "Victor", "Beats": "", "Shokz": "", "Huawei": "HUAWEI",
 },
 "vod": {
  "Netflix": "ネットフリックス", "Prime Video": "Amazon Prime|プライムビデオ|Amazonプライム", "U-NEXT": "", "Disney+": "ディズニープラス|Disney\\+", "Hulu": "フールー", "DMM TV": "DMM",
  "ABEMA": "Abema", "DAZN": "", "Lemino": "", "FOD": "", "TVer": "", "WOWOW": "", "Apple TV+": "Apple TV", "YouTube Premium": "YouTube", "dアニメストア": "dアニメ", "Rakuten TV": "楽天TV",
 },
}

# メーカー（同じ項目にブランドと並んで出たとき、メーカー名を別ブランドとして数えないため）
MAKERS = {
 "toner": {"d プログラム": "資生堂", "エリクシール": "資生堂", "アクアレーベル": "資生堂", "イハダ": "資生堂", "クレ・ド・ポー ボーテ": "資生堂",
           "肌ラボ": "ロート製薬", "メラノCC": "ロート製薬", "オバジ": "ロート製薬", "キュレル": "花王", "ソフィーナ": "花王", "エスト": "花王",
           "雪肌精": "コーセー", "コスメデコルテ": "コーセー", "ONE BY KOSE": "コーセー", "ミノン": "第一三共ヘルスケア", "トランシーノ": "第一三共ヘルスケア"},
 "shampoo": {"THE ANSWER": "花王", "エッセンシャル": "花王", "メリット": "花王", "セグレタ": "花王", "サクセス": "花王", "キュレル": "花王", "TSUBAKI": "資生堂プロフェッショナル"},
 "protein": {"ザバス": "明治", "ウイダー": "森永"},
}

CLOSED = {"shampoo", "earbuds", "vod"}   # 2回目の収集の対象外。再開するときはここから外して seed を実行
PREMISE_PROMPT = "買い物の相談をするとき、あなたが参考にしている私の情報を教えてください。"
SPECIAL = [  # key, 表示名, mode, 質問文, ポイント, 必須
    ("premise", "AIが知っているあなたの情報", "premise", PREMISE_PROMPT, 30, True),
    ("own", "AIに相談した買い物", "own", "", 90, False),
]

def run():
    init_db()
    db = SessionLocal()
    for key, name, mode, prompt, pts, req in SPECIAL:
        c = db.scalar(select(Category).where(Category.key == key))
        if not c:
            c = Category(key=key, name=name, type="special"); db.add(c); db.flush()
        t = db.scalar(select(Topic).where(Topic.category_id == c.id, Topic.prompt_version == "v1"))
        if not t:
            db.add(Topic(category_id=c.id, prompt_text=prompt, prompt_version="v1", point_value=pts, mode=mode, required=req))
        else:
            t.mode, t.required, t.point_value, t.prompt_text = mode, req, pts, prompt
    for key, name, cname, unit, ctype in CATEGORIES:
        c = db.scalar(select(Category).where(Category.key == key))
        if not c:
            c = Category(key=key, name=name, type=ctype); db.add(c); db.flush()
        from server.db import now
        for t in db.scalars(select(Topic).where(Topic.category_id == c.id, Topic.prompt_version == "v1", Topic.closes_at.is_(None))):
            t.closes_at = now()  # v1（ヒアリングなし）は締める
        if not db.scalar(select(Topic).where(Topic.category_id == c.id, Topic.prompt_version == "v2")):
            db.add(Topic(category_id=c.id, prompt_text=PROMPT.format(cat=cname, unit=unit), prompt_version="v2", point_value=30, mode="dialog"))
            db.flush()
        if key in CLOSED:        # 2回目の収集では出さないカテゴリ（検証実験のもの）
            for t in db.scalars(select(Topic).where(Topic.category_id == c.id, Topic.closes_at.is_(None))):
                t.closes_at = now()
        have = {b.canonical_name: b for b in db.scalars(select(Brand).where(Brand.category_id == c.id))}
        for canon, aliases in BRANDS.get(key, {}).items():
            al = "\n".join(a for a in aliases.split("|") if a)
            mk = MAKERS.get(key, {}).get(canon)
            if canon in have:      # 辞書の更新を既存の行にも反映する
                have[canon].aliases, have[canon].maker = al, mk
            else:
                db.add(Brand(category_id=c.id, canonical_name=canon, aliases=al, maker=mk))
    db.commit(); db.close()
    print("seeded")

if __name__ == "__main__":
    run()
