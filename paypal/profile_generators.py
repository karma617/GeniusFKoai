from dataclasses import asdict, dataclass
import calendar
from datetime import date
import random
import string


@dataclass(frozen=True)
class GeneratedProfile:
    country: str
    last_name: str
    first_name: str
    birthday: str
    zip: str
    house_number: str
    state: str
    city: str
    street: str
    full_address: str
    card_type: str
    card_number: str
    exp_date: str
    cvv: str
    cpf: str
    email: str
    password: str


SUPPORTED_PROFILE_COUNTRIES = ("JP", "BR", "US", "GB", "BA", "ID")

JP_LOCATIONS = [
    {"city": "Chiyoda-ku", "city_ja": "千代田区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["100-0001", "100-0002", "100-0004", "100-0005", "100-0011", "100-0013", "101-0021", "101-0032", "101-0051", "102-0073", "102-0082", "102-0093"]},
    {"city": "Minato-ku", "city_ja": "港区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["105-0001", "105-0011", "105-0014", "106-0031", "106-0032", "106-0041", "106-0044", "106-0045", "106-0046", "106-0047", "107-0051", "107-0052", "107-0061", "108-0014", "108-0023"]},
    {"city": "Shibuya-ku", "city_ja": "渋谷区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["150-0001", "150-0002", "150-0011", "150-0012", "150-0013", "150-0021", "150-0031", "150-0041", "150-0042", "150-0043", "150-0044", "150-0045", "150-0046"]},
    {"city": "Shinjuku-ku", "city_ja": "新宿区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["160-0001", "160-0011", "160-0021", "160-0022", "160-0023", "160-0024", "161-0031", "162-0801", "162-0814", "162-0825", "162-0843"]},
    {"city": "Setagaya-ku", "city_ja": "世田谷区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["154-0001", "154-0011", "154-0023", "155-0031", "155-0033", "157-0061", "158-0091", "158-0094", "156-0043", "156-0044"]},
    {"city": "Meguro-ku", "city_ja": "目黒区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["152-0001", "152-0011", "152-0021", "152-0031", "153-0041", "153-0051", "153-0061", "153-0062", "153-0063"]},
    {"city": "Toshima-ku", "city_ja": "豊島区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["170-0001", "170-0011", "170-0013", "170-0021", "170-0031", "171-0021", "171-0022", "171-0031", "171-0041", "171-0051"]},
    {"city": "Nakano-ku", "city_ja": "中野区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["164-0001", "164-0011", "164-0012", "164-0013", "164-0014", "165-0021", "165-0022", "165-0023", "165-0024", "165-0025"]},
    {"city": "Suginami-ku", "city_ja": "杉並区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["166-0001", "166-0002", "166-0003", "166-0004", "166-0011", "166-0012", "166-0013", "166-0014", "166-0015", "167-0021"]},
    {"city": "Nerima-ku", "city_ja": "練馬区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["176-0001", "176-0002", "176-0003", "176-0004", "176-0005", "176-0006", "176-0011", "176-0012", "176-0021", "177-0031"]},
    {"city": "Ota-ku", "city_ja": "大田区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["143-0001", "143-0011", "143-0013", "143-0014", "143-0015", "143-0016", "143-0021", "143-0022", "143-0023", "143-0024"]},
    {"city": "Edogawa-ku", "city_ja": "江戸川区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["132-0001", "132-0011", "132-0021", "132-0022", "132-0023", "132-0024", "132-0025", "133-0041", "133-0043", "133-0044"]},
    {"city": "Koto-ku", "city_ja": "江東区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["135-0001", "135-0002", "135-0003", "135-0004", "135-0011", "135-0016", "135-0021", "135-0022", "135-0023", "135-0024"]},
    {"city": "Taito-ku", "city_ja": "台東区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["110-0001", "110-0002", "110-0003", "110-0004", "110-0005", "110-0008", "110-0011", "110-0012", "110-0013", "110-0015"]},
    {"city": "Bunkyo-ku", "city_ja": "文京区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["112-0001", "112-0002", "112-0003", "112-0004", "112-0005", "112-0006", "112-0011", "112-0012", "112-0013", "112-0014"]},
    {"city": "Shinagawa-ku", "city_ja": "品川区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["140-0001", "140-0002", "140-0003", "140-0004", "140-0005", "140-0011", "140-0013", "140-0014", "140-0015", "141-0001"]},
    {"city": "Itabashi-ku", "city_ja": "板橋区", "prefecture": "Tokyo", "prefecture_ja": "東京都", "zips": ["173-0001", "173-0003", "173-0004", "173-0005", "173-0006", "173-0011", "173-0012", "173-0013", "173-0014", "173-0015"]},
    {"city": "Yokohama", "city_ja": "横浜市", "prefecture": "Kanagawa", "prefecture_ja": "神奈川県", "zips": ["220-0001", "220-0011", "220-0012", "221-0801", "221-0802", "221-0822", "231-0001", "231-0011", "231-0023", "231-0031", "231-0045", "232-0001"]},
    {"city": "Kawasaki", "city_ja": "川崎市", "prefecture": "Kanagawa", "prefecture_ja": "神奈川県", "zips": ["210-0001", "210-0006", "210-0011", "210-0012", "212-0011", "212-0013", "212-0023", "213-0001", "213-0011", "215-0004"]},
    {"city": "Sagamihara", "city_ja": "相模原市", "prefecture": "Kanagawa", "prefecture_ja": "神奈川県", "zips": ["252-0001", "252-0011", "252-0131", "252-0141", "252-0206", "252-0211", "252-0221", "252-0231", "252-0241", "252-0302"]},
    {"city": "Fujisawa", "city_ja": "藤沢市", "prefecture": "Kanagawa", "prefecture_ja": "神奈川県", "zips": ["251-0001", "251-0011", "251-0014", "251-0015", "251-0021", "251-0023", "251-0024", "251-0025", "251-0026", "251-0028"]},
    {"city": "Saitama", "city_ja": "さいたま市", "prefecture": "Saitama", "prefecture_ja": "埼玉県", "zips": ["330-0001", "330-0011", "330-0021", "330-0031", "330-0041", "330-0051", "336-0001", "336-0011", "336-0021", "336-0031"]},
    {"city": "Kawaguchi", "city_ja": "川口市", "prefecture": "Saitama", "prefecture_ja": "埼玉県", "zips": ["332-0001", "332-0003", "332-0006", "332-0011", "332-0012", "332-0014", "332-0015", "332-0016", "332-0017", "332-0021"]},
    {"city": "Kawagoe", "city_ja": "川越市", "prefecture": "Saitama", "prefecture_ja": "埼玉県", "zips": ["350-0001", "350-0011", "350-0014", "350-0015", "350-0021", "350-0022", "350-0023", "350-0024", "350-0025", "350-0026"]},
    {"city": "Chiba", "city_ja": "千葉市", "prefecture": "Chiba", "prefecture_ja": "千葉県", "zips": ["260-0001", "260-0011", "260-0021", "260-0031", "260-0041", "261-0001", "261-0011", "261-0021", "263-0001", "263-0011"]},
    {"city": "Funabashi", "city_ja": "船橋市", "prefecture": "Chiba", "prefecture_ja": "千葉県", "zips": ["273-0001", "273-0002", "273-0003", "273-0005", "273-0011", "273-0012", "273-0013", "273-0014", "273-0015", "273-0021"]},
    {"city": "Kashiwa", "city_ja": "柏市", "prefecture": "Chiba", "prefecture_ja": "千葉県", "zips": ["277-0001", "277-0003", "277-0004", "277-0005", "277-0011", "277-0014", "277-0021", "277-0022", "277-0024", "277-0025"]},
    {"city": "Osaka", "city_ja": "大阪市", "prefecture": "Osaka", "prefecture_ja": "大阪府", "zips": ["530-0001", "530-0011", "530-0021", "531-0061", "531-0072", "541-0041", "541-0051", "542-0081", "542-0082", "550-0001", "550-0011", "550-0014"]},
    {"city": "Sakai", "city_ja": "堺市", "prefecture": "Osaka", "prefecture_ja": "大阪府", "zips": ["590-0001", "590-0011", "590-0014", "590-0021", "590-0023", "590-0024", "590-0025", "590-0026", "590-0028", "590-0048"]},
    {"city": "Higashiosaka", "city_ja": "東大阪市", "prefecture": "Osaka", "prefecture_ja": "大阪府", "zips": ["577-0001", "577-0002", "577-0003", "577-0004", "577-0005", "577-0006", "577-0011", "577-0012", "577-0013", "577-0801"]},
    {"city": "Suita", "city_ja": "吹田市", "prefecture": "Osaka", "prefecture_ja": "大阪府", "zips": ["564-0001", "564-0002", "564-0003", "564-0004", "564-0011", "564-0012", "564-0013", "564-0014", "564-0015", "564-0016"]},
    {"city": "Kyoto", "city_ja": "京都市", "prefecture": "Kyoto", "prefecture_ja": "京都府", "zips": ["600-8001", "600-8011", "600-8021", "600-8031", "604-0001", "604-0011", "604-0021", "604-0091", "604-8005", "605-0001", "605-0073", "605-0801"]},
    {"city": "Kobe", "city_ja": "神戸市", "prefecture": "Hyogo", "prefecture_ja": "兵庫県", "zips": ["650-0001", "650-0011", "650-0021", "650-0031", "650-0041", "651-0001", "651-0011", "651-0078", "651-0086", "651-0094"]},
    {"city": "Nishinomiya", "city_ja": "西宮市", "prefecture": "Hyogo", "prefecture_ja": "兵庫県", "zips": ["662-0001", "662-0011", "662-0021", "662-0822", "662-0832", "662-0834", "662-0911", "662-0912", "662-0921", "662-0927"]},
    {"city": "Amagasaki", "city_ja": "尼崎市", "prefecture": "Hyogo", "prefecture_ja": "兵庫県", "zips": ["660-0001", "660-0051", "660-0052", "660-0053", "660-0054", "660-0055", "660-0801", "660-0802", "660-0803", "660-0804"]},
    {"city": "Nagoya", "city_ja": "名古屋市", "prefecture": "Aichi", "prefecture_ja": "愛知県", "zips": ["450-0001", "450-0002", "450-0011", "450-0021", "450-0031", "451-0011", "451-0021", "451-0031", "460-0001", "460-0008", "460-0011"]},
    {"city": "Toyota", "city_ja": "豊田市", "prefecture": "Aichi", "prefecture_ja": "愛知県", "zips": ["471-0001", "471-0011", "471-0013", "471-0014", "471-0015", "471-0016", "471-0017", "471-0021", "471-0023", "471-0024"]},
    {"city": "Sapporo", "city_ja": "札幌市", "prefecture": "Hokkaido", "prefecture_ja": "北海道", "zips": ["060-0001", "060-0011", "060-0021", "060-0031", "060-0041", "060-0051", "060-0061", "060-0807", "060-0808", "064-0801"]},
    {"city": "Asahikawa", "city_ja": "旭川市", "prefecture": "Hokkaido", "prefecture_ja": "北海道", "zips": ["070-0001", "070-0011", "070-0021", "070-0022", "070-0023", "070-0024", "070-0025", "070-0026", "070-0027", "070-0028"]},
    {"city": "Fukuoka", "city_ja": "福岡市", "prefecture": "Fukuoka", "prefecture_ja": "福岡県", "zips": ["810-0001", "810-0011", "810-0021", "810-0031", "810-0041", "812-0011", "812-0013", "812-0018", "812-0023", "813-0001"]},
    {"city": "Kitakyushu", "city_ja": "北九州市", "prefecture": "Fukuoka", "prefecture_ja": "福岡県", "zips": ["800-0001", "800-0011", "800-0021", "800-0022", "800-0023", "800-0024", "800-0025", "800-0026", "802-0001", "802-0003"]},
    {"city": "Sendai", "city_ja": "仙台市", "prefecture": "Miyagi", "prefecture_ja": "宮城県", "zips": ["980-0001", "980-0011", "980-0013", "980-0021", "980-0031", "980-0801", "980-0811", "980-0821", "983-0001", "984-0011"]},
    {"city": "Hiroshima", "city_ja": "広島市", "prefecture": "Hiroshima", "prefecture_ja": "広島県", "zips": ["730-0001", "730-0011", "730-0013", "730-0021", "730-0031", "730-0041", "730-0051", "732-0011", "732-0021", "732-0822"]},
    {"city": "Shizuoka", "city_ja": "静岡市", "prefecture": "Shizuoka", "prefecture_ja": "静岡県", "zips": ["420-0001", "420-0011", "420-0021", "420-0022", "420-0031", "420-0032", "420-0033", "420-0034", "420-0035", "420-0036"]},
    {"city": "Hamamatsu", "city_ja": "浜松市", "prefecture": "Shizuoka", "prefecture_ja": "静岡県", "zips": ["430-0001", "430-0011", "430-0012", "430-0021", "430-0022", "430-0023", "430-0024", "430-0025", "430-0026", "430-0027"]},
    {"city": "Niigata", "city_ja": "新潟市", "prefecture": "Niigata", "prefecture_ja": "新潟県", "zips": ["950-0001", "950-0011", "950-0012", "950-0021", "950-0022", "950-0031", "950-0032", "950-0065", "950-0071", "950-0072"]},
    {"city": "Okayama", "city_ja": "岡山市", "prefecture": "Okayama", "prefecture_ja": "岡山県", "zips": ["700-0001", "700-0011", "700-0021", "700-0022", "700-0023", "700-0024", "700-0025", "700-0026", "700-0031", "700-0032"]},
    {"city": "Kumamoto", "city_ja": "熊本市", "prefecture": "Kumamoto", "prefecture_ja": "熊本県", "zips": ["860-0001", "860-0002", "860-0003", "860-0004", "860-0005", "860-0006", "860-0007", "860-0008", "860-0011", "860-0012"]},
    {"city": "Nagano", "city_ja": "長野市", "prefecture": "Nagano", "prefecture_ja": "長野県", "zips": ["380-0801", "380-0802", "380-0803", "380-0811", "380-0812", "380-0813", "380-0821", "380-0822", "380-0823", "380-0824"]},
    {"city": "Kanazawa", "city_ja": "金沢市", "prefecture": "Ishikawa", "prefecture_ja": "石川県", "zips": ["920-0001", "920-0011", "920-0021", "920-0022", "920-0023", "920-0024", "920-0025", "920-0031", "920-0032", "920-0033"]},
]

JP_TOWN_NAMES = [
    {"en": "Marunouchi", "ja": "丸の内"}, {"en": "Otemachi", "ja": "大手町"}, {"en": "Yurakucho", "ja": "有楽町"},
    {"en": "Ginza", "ja": "銀座"}, {"en": "Roppongi", "ja": "六本木"}, {"en": "Akasaka", "ja": "赤坂"},
    {"en": "Aoyama", "ja": "青山"}, {"en": "Omotesando", "ja": "表参道"}, {"en": "Harajuku", "ja": "原宿"},
    {"en": "Ebisu", "ja": "恵比寿"}, {"en": "Daikanyama", "ja": "代官山"}, {"en": "Nakameguro", "ja": "中目黒"},
    {"en": "Jiyugaoka", "ja": "自由が丘"}, {"en": "Shimokitazawa", "ja": "下北沢"},
    {"en": "Yotsuya", "ja": "四谷"}, {"en": "Ichigaya", "ja": "市ヶ谷"}, {"en": "Iidabashi", "ja": "飯田橋"},
    {"en": "Kagurazaka", "ja": "神楽坂"}, {"en": "Ikebukuro", "ja": "池袋"}, {"en": "Mejiro", "ja": "目白"},
    {"en": "Nakano", "ja": "中野"}, {"en": "Koenji", "ja": "高円寺"}, {"en": "Asagaya", "ja": "阿佐ヶ谷"},
    {"en": "Ogikubo", "ja": "荻窪"}, {"en": "Kichijoji", "ja": "吉祥寺"}, {"en": "Mitaka", "ja": "三鷹"},
    {"en": "Sangenjaya", "ja": "三軒茶屋"}, {"en": "Gotanda", "ja": "五反田"}, {"en": "Osaki", "ja": "大崎"},
    {"en": "Tamachi", "ja": "田町"}, {"en": "Hamamatsucho", "ja": "浜松町"},
    {"en": "Toranomon", "ja": "虎ノ門"}, {"en": "Kasumigaseki", "ja": "霞が関"}, {"en": "Nagatacho", "ja": "永田町"},
    {"en": "Kojimachi", "ja": "麹町"}, {"en": "Hirakawacho", "ja": "平河町"},
    {"en": "Honmachi", "ja": "本町"}, {"en": "Umeda", "ja": "梅田"}, {"en": "Namba", "ja": "難波"},
    {"en": "Tennoji", "ja": "天王寺"}, {"en": "Shinsaibashi", "ja": "心斎橋"}, {"en": "Kitashinchi", "ja": "北新地"},
    {"en": "Sannomiya", "ja": "三宮"}, {"en": "Motomachi", "ja": "元町"}, {"en": "Kitano", "ja": "北野"},
    {"en": "Karasuma", "ja": "烏丸"}, {"en": "Kawaramachi", "ja": "河原町"}, {"en": "Kiyamachi", "ja": "木屋町"},
    {"en": "Sakae", "ja": "栄"}, {"en": "Fushimi", "ja": "伏見"}, {"en": "Osu", "ja": "大須"},
    {"en": "Kanayama", "ja": "金山"}, {"en": "Hakata", "ja": "博多"}, {"en": "Tenjin", "ja": "天神"},
    {"en": "Daimyo", "ja": "大名"}, {"en": "Yakuin", "ja": "薬院"},
    {"en": "Susukino", "ja": "すすきの"}, {"en": "Odori", "ja": "大通"},
    {"en": "Kotodai", "ja": "勾当台"}, {"en": "Aoba", "ja": "青葉"},
    {"en": "Takadanobaba", "ja": "高田馬場"}, {"en": "Waseda", "ja": "早稲田"},
    {"en": "Ochanomizu", "ja": "お茶の水"}, {"en": "Jinbocho", "ja": "神保町"},
    {"en": "Kanda", "ja": "神田"}, {"en": "Nihonbashi", "ja": "日本橋"},
    {"en": "Tsukiji", "ja": "築地"}, {"en": "Tsukishima", "ja": "月島"},
    {"en": "Toyosu", "ja": "豊洲"}, {"en": "Ariake", "ja": "有明"}, {"en": "Odaiba", "ja": "お台場"},
    {"en": "Shinbashi", "ja": "新橋"}, {"en": "Azabu", "ja": "麻布"}, {"en": "Hiroo", "ja": "広尾"},
    {"en": "Shirokane", "ja": "白金"}, {"en": "Takanawa", "ja": "高輪"}, {"en": "Mita", "ja": "三田"},
    {"en": "Shibakoen", "ja": "芝公園"}, {"en": "Yoyogi", "ja": "代々木"}, {"en": "Sendagaya", "ja": "千駄ヶ谷"},
    {"en": "Hatagaya", "ja": "幡ヶ谷"}, {"en": "Sasazuka", "ja": "笹塚"},
    {"en": "Komaba", "ja": "駒場"}, {"en": "Todoroki", "ja": "等々力"},
    {"en": "Yoga", "ja": "用賀"}, {"en": "Futakotamagawa", "ja": "二子玉川"},
    {"en": "Okusawa", "ja": "奥沢"}, {"en": "Denenchofu", "ja": "田園調布"},
    {"en": "Kamata", "ja": "蒲田"}, {"en": "Omori", "ja": "大森"},
    {"en": "Kinshicho", "ja": "錦糸町"}, {"en": "Ryogoku", "ja": "両国"},
    {"en": "Asakusa", "ja": "浅草"}, {"en": "Ueno", "ja": "上野"},
    {"en": "Yanaka", "ja": "谷中"}, {"en": "Nezu", "ja": "根津"}, {"en": "Sendagi", "ja": "千駄木"},
    {"en": "Nishi-Shinjuku", "ja": "西新宿"}, {"en": "Akabane", "ja": "赤羽"},
    {"en": "Oji", "ja": "王子"}, {"en": "Jujo", "ja": "十条"}, {"en": "Itabashi", "ja": "板橋"},
    {"en": "Shakujii", "ja": "石神井"}, {"en": "Oizumi", "ja": "大泉"}, {"en": "Hikarigaoka", "ja": "光が丘"},
    {"en": "Tachikawa", "ja": "立川"}, {"en": "Fuchu", "ja": "府中"}, {"en": "Chofu", "ja": "調布"},
    {"en": "Machida", "ja": "町田"}, {"en": "Hachioji", "ja": "八王子"}, {"en": "Musashino", "ja": "武蔵野"},
    {"en": "Motoyama", "ja": "本山"}, {"en": "Chikusa", "ja": "千種"}, {"en": "Imaike", "ja": "今池"},
    {"en": "Yagoto", "ja": "八事"},
    {"en": "Ibaraki", "ja": "茨木"}, {"en": "Takatsuki", "ja": "高槻"},
    {"en": "Moriguchi", "ja": "守口"}, {"en": "Neyagawa", "ja": "寝屋川"}, {"en": "Hirakata", "ja": "枚方"},
    {"en": "Abeno", "ja": "阿倍野"}, {"en": "Tsuruhashi", "ja": "鶴橋"},
    {"en": "Nishinari", "ja": "西成"},
]

JP_LAST_SYLLABLES_1 = ["サ", "ス", "タ", "ナ", "ハ", "マ", "ヤ", "カ", "ワ", "イ", "オ", "コ", "モ", "ア", "フ", "ニ", "エ", "ミ", "ク", "シ"]
JP_LAST_SYLLABLES_2 = ["トウ", "ズキ", "カハシ", "ナカ", "タナベ", "マモト", "カムラ", "バヤシ", "ツモト", "ノウエ", "ムラ", "ヤシ", "ミズ", "マザキ", "リ", "ベ", "ケダ", "シモト", "マシタ", "シカワ", "カジマ", "エダ", "ジタ", "ガワ", "カダ", "セガワ", "ラカミ", "ンドウ", "シイ", "カモト", "オキ", "ジイ", "シムラ", "クダ", "ウラ", "ジワラ", "ツダ", "カガワ", "カノ", "ハラ", "ノ", "ダ", "ワタ", "グチ", "ヤマ", "タ", "モト", "ウチ", "サワ", "キ"]
JP_FIRST_PARTS_A = ["ヒロ", "タカ", "アキ", "ケン", "ダイ", "ユウ", "ショウ", "リョウ", "ナオ", "タツ", "ハル", "カイ", "イツ", "ジュン", "マサ", "コウ", "タク", "レン", "ツバ", "ソウ", "シン", "ゲン", "トモ", "ノブ", "ヨシ", "カズ", "テツ", "ミツ", "ヒデ", "キヨ"]
JP_FIRST_PARTS_B = ["シ", "キ", "ラ", "ジ", "キ", "タ", "ト", "ヤ", "タロウ", "スケ", "イチ", "ヘイ", "マサ", "ノリ", "ヒコ", "オ", "ヤ", "", "サ", "タ", "ジ", "キ", "ヤ", "ヒロ", "ノリ", "ヤ", "ヤ", "ル", "アキ", "シ"]
JP_FIRST_PARTS_F_A = ["ユ", "ヒ", "メ", "ア", "サ", "ハ", "アオ", "カ", "ミ", "アキ", "ナオ", "マリ", "ケイ", "アヤ", "ミサ", "リ", "ハル", "ナナ", "カナ", "アス", "ホノ", "メグ", "エリ", "チ", "マ", "レ", "ノ", "ミ", "サ", "ヒ"]
JP_FIRST_PARTS_F_B = ["イ", "ナ", "イ", "イ", "クラ", "ナ", "イ", "ナ", "オ", "コ", "ミ", "コ", "コ", "カ", "キ", "ナ", "カ", "ミ", "コ", "カ", "カ", "ミ", "カ", "ヒロ", "ドカ", "イ", "ゾミ", "ユキ", "ヤカ", "マリ"]
JP_CARD_BINS = [{"bin": "414709", "length": 16, "brand": "Visa Debit", "issuer": "SMBC"}]
JP_EMAIL_DOMAINS = ["gmail.com", "yahoo.co.jp", "hotmail.com", "outlook.jp", "icloud.com", "me.com", "live.jp"]
JP_LAST_NAMES_ROMAJI = [
    "sato", "suzuki", "takahashi", "tanaka", "watanabe", "ito", "yamamoto", "nakamura", "kobayashi", "kato",
    "yoshida", "yamada", "sasaki", "yamaguchi", "matsumoto", "inoue", "kimura", "hayashi", "shimizu",
    "yamazaki", "mori", "abe", "ikeda", "hashimoto", "yamashita", "ishikawa", "nakajima", "maeda", "fujita",
    "ogawa", "goto", "okada", "hasegawa", "murakami", "kondo", "ishii", "sakamoto", "endo", "aoki",
    "fujii", "nishimura", "fukuda", "ota", "miura", "fujiwara", "okamoto", "matsuda", "nakagawa", "nakano",
]
JP_FIRST_NAMES_ROMAJI = [
    "hiroshi", "takashi", "akira", "kenji", "daiki", "yuki", "sho", "ryo", "kenta", "naoki",
    "tatsuya", "shota", "takeshi", "haruto", "sora", "hayato", "kaito", "yuto", "riku", "itsuki",
    "ren", "tsubasa", "daisuke", "junichi", "masaki", "kohei", "ryota", "takuya", "yusuke", "takahiro",
    "yui", "hina", "mei", "ai", "yuna", "sakura", "hana", "aoi", "kana", "mio",
    "akiko", "yumi", "naomi", "mariko", "keiko", "ayaka", "misaki", "saki", "rina", "yuka",
    "haruka", "nanami", "riko", "kanako", "asuka", "mayu", "honoka", "megumi", "erika",
]

BR_LAST_NAMES = [
    "Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Almeida", "Pereira", "Lima", "Gomes",
    "Costa", "Ribeiro", "Martins", "Carvalho", "Rocha", "Barbosa", "Melo", "Cardoso", "Teixeira", "Correia",
    "Moura", "Cunha", "Dias", "Nunes", "Moreira", "Vieira", "Monteiro", "Castro", "Araujo", "Campos",
    "Freitas", "Pinto", "Mendes", "Cavalcanti", "Nascimento", "Batista", "Andrade", "Reis", "Duarte", "Machado",
    "Farias", "Borges", "Miranda", "Fonseca", "Ramos", "Neves", "Tavares", "Peixoto", "Siqueira", "Moraes",
]
BR_FIRST_NAMES = [
    "Lucas", "Miguel", "Arthur", "Gabriel", "Pedro", "Matheus", "Rafael", "Bruno", "Felipe", "Gustavo",
    "Diego", "Caio", "Andre", "Thiago", "Leonardo", "Eduardo", "Henrique", "Vinicius", "Marcos", "Daniel",
    "Ana", "Maria", "Julia", "Laura", "Mariana", "Beatriz", "Camila", "Leticia", "Larissa", "Amanda",
    "Fernanda", "Carolina", "Isabela", "Renata", "Aline", "Patricia", "Bianca", "Bruna", "Clara", "Luana",
    "Sofia", "Helena", "Manuela", "Valentina", "Yasmin", "Alice", "Livia", "Lorena", "Vitoria", "Nina",
]
BR_LOCATIONS = [
    {"state": "SP", "city": "Sao Paulo", "ceps": ["01001-000", "01310-100", "01415-001", "04094-050", "04543-011", "05010-000"]},
    {"state": "RJ", "city": "Rio de Janeiro", "ceps": ["20040-020", "22010-000", "22250-040", "22410-002", "22640-102", "23050-000"]},
    {"state": "MG", "city": "Belo Horizonte", "ceps": ["30130-010", "30140-071", "30310-009", "30421-169", "30640-070"]},
    {"state": "BA", "city": "Salvador", "ceps": ["40020-000", "40140-110", "40210-630", "41820-020", "41940-040"]},
    {"state": "PR", "city": "Curitiba", "ceps": ["80010-010", "80230-010", "80420-090", "80530-000", "81200-100"]},
    {"state": "RS", "city": "Porto Alegre", "ceps": ["90010-150", "90110-001", "90430-001", "90560-002", "91340-000"]},
    {"state": "PE", "city": "Recife", "ceps": ["50010-000", "51020-000", "52011-000", "52050-000", "51030-000"]},
    {"state": "CE", "city": "Fortaleza", "ceps": ["60025-060", "60160-230", "60325-000", "60410-440", "60811-341"]},
    {"state": "DF", "city": "Brasilia", "ceps": ["70040-010", "70297-400", "70390-025", "70770-522", "71919-540"]},
    {"state": "SC", "city": "Florianopolis", "ceps": ["88010-400", "88015-201", "88020-300", "88034-000", "88062-000"]},
    {"state": "GO", "city": "Goiania", "ceps": ["74003-010", "74110-010", "74210-010", "74605-010", "74810-100"]},
    {"state": "PA", "city": "Belem", "ceps": ["66010-000", "66015-160", "66035-170", "66050-000", "66110-000"]},
    {"state": "AM", "city": "Manaus", "ceps": ["69005-040", "69010-000", "69020-010", "69050-001", "69058-795"]},
    {"state": "ES", "city": "Vitoria", "ceps": ["29010-120", "29015-120", "29050-335", "29055-450", "29060-270"]},
    {"state": "MT", "city": "Cuiaba", "ceps": ["78005-370", "78010-000", "78020-400", "78048-000", "78060-900"]},
    {"state": "MS", "city": "Campo Grande", "ceps": ["79002-071", "79004-000", "79010-040", "79020-210", "79040-450"]},
    {"state": "RN", "city": "Natal", "ceps": ["59010-000", "59020-100", "59030-200", "59064-100", "59090-000"]},
    {"state": "PB", "city": "Joao Pessoa", "ceps": ["58010-000", "58013-000", "58030-001", "58045-010", "58051-900"]},
    {"state": "AL", "city": "Maceio", "ceps": ["57020-000", "57035-000", "57036-000", "57046-000", "57055-000"]},
    {"state": "SE", "city": "Aracaju", "ceps": ["49010-000", "49015-000", "49020-000", "49035-000", "49050-000"]},
    {"state": "SP", "city": "Campinas", "ceps": ["13010-001", "13015-000", "13020-060", "13024-200", "13083-970", "13100-000"]},
    {"state": "SP", "city": "Santos", "ceps": ["11010-150", "11013-001", "11015-200", "11025-001", "11045-400", "11060-001"]},
    {"state": "RJ", "city": "Niteroi", "ceps": ["24020-125", "24030-060", "24210-200", "24220-900", "24340-005", "24350-010"]},
    {"state": "MG", "city": "Uberlandia", "ceps": ["38400-100", "38400-170", "38405-202", "38408-100", "38411-186", "38414-064"]},
    {"state": "BA", "city": "Feira de Santana", "ceps": ["44001-000", "44002-000", "44020-000", "44050-000", "44075-000", "44088-000"]},
    {"state": "PR", "city": "Londrina", "ceps": ["86010-000", "86015-000", "86020-000", "86026-010", "86039-000", "86050-000"]},
    {"state": "RS", "city": "Caxias do Sul", "ceps": ["95010-000", "95020-000", "95032-000", "95040-000", "95052-000", "95070-560"]},
    {"state": "PE", "city": "Olinda", "ceps": ["53010-000", "53020-000", "53120-000", "53130-000", "53240-000", "53330-000"]},
    {"state": "CE", "city": "Juazeiro do Norte", "ceps": ["63010-000", "63020-000", "63030-000", "63040-000", "63050-000", "63060-000"]},
    {"state": "GO", "city": "Anapolis", "ceps": ["75020-010", "75023-040", "75024-030", "75043-010", "75110-390", "75113-570"]},
    {"state": "PA", "city": "Santarem", "ceps": ["68005-000", "68010-000", "68015-000", "68020-000", "68035-000", "68040-000"]},
    {"state": "SC", "city": "Joinville", "ceps": ["89201-000", "89202-000", "89203-000", "89204-000", "89218-000", "89221-000"]},
]
BR_STATE_NAMES = {
    "SP": "São Paulo", "RJ": "Rio de Janeiro", "MG": "Minas Gerais", "BA": "Bahia", "PR": "Paraná",
    "RS": "Rio Grande do Sul", "PE": "Pernambuco", "CE": "Ceará", "DF": "Distrito Federal",
    "SC": "Santa Catarina", "GO": "Goiás", "PA": "Pará", "AM": "Amazonas", "ES": "Espírito Santo",
    "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "RN": "Rio Grande do Norte", "PB": "Paraíba",
    "AL": "Alagoas", "SE": "Sergipe",
}
BR_STREET_NAMES = [
    "Avenida Paulista", "Rua Augusta", "Rua Oscar Freire", "Rua Vergueiro", "Rua Haddock Lobo",
    "Avenida Atlantica", "Rua Voluntarios da Patria", "Rua Visconde de Piraja", "Rua das Laranjeiras",
    "Avenida Afonso Pena", "Rua da Bahia", "Rua Paraiba", "Avenida do Contorno", "Rua Curitiba",
    "Avenida Sete de Setembro", "Rua Chile", "Rua das Hortensias", "Avenida Tancredo Neves",
    "Rua XV de Novembro", "Avenida Batel", "Rua Marechal Deodoro", "Rua Comendador Araujo",
    "Avenida Ipiranga", "Rua dos Andradas", "Rua Padre Chagas", "Avenida Borges de Medeiros",
    "Rua da Aurora", "Avenida Boa Viagem", "Rua do Hospicio", "Rua Benfica",
    "Avenida Beira Mar", "Rua Barão de Aracati", "Rua Costa Barros", "Avenida Dom Luis",
    "SQS 308 Bloco A", "CLN 102 Bloco B", "SHIS QI 05 Conjunto 02", "Avenida das Nacoes",
    "Rua Bocaiuva", "Rua Felipe Schmidt", "Avenida Mauro Ramos", "Rua Esteves Junior",
    "Avenida Goias", "Rua 10", "Avenida T-63", "Rua 9",
    "Avenida Nazare", "Travessa Padre Eutiquio", "Rua dos Mundurucus", "Avenida Almirante Barroso",
]
BR_STREETS_BY_CITY = {
    "Sao Paulo": ["Avenida Paulista", "Rua Augusta", "Rua Oscar Freire", "Rua Vergueiro", "Rua Haddock Lobo"],
    "Rio de Janeiro": ["Avenida Atlantica", "Rua Voluntarios da Patria", "Rua Visconde de Piraja", "Rua das Laranjeiras"],
    "Belo Horizonte": ["Avenida Afonso Pena", "Rua da Bahia", "Rua Paraiba", "Avenida do Contorno", "Rua Curitiba"],
    "Salvador": ["Avenida Sete de Setembro", "Rua Chile", "Rua das Hortensias", "Avenida Tancredo Neves"],
    "Curitiba": ["Rua XV de Novembro", "Avenida Batel", "Rua Marechal Deodoro", "Rua Comendador Araujo"],
    "Porto Alegre": ["Avenida Ipiranga", "Rua dos Andradas", "Rua Padre Chagas", "Avenida Borges de Medeiros"],
    "Recife": ["Rua da Aurora", "Avenida Boa Viagem", "Rua do Hospicio", "Rua Benfica"],
    "Fortaleza": ["Avenida Beira Mar", "Rua Barão de Aracati", "Rua Costa Barros", "Avenida Dom Luis"],
    "Brasilia": ["SQS 308 Bloco A", "CLN 102 Bloco B", "SHIS QI 05 Conjunto 02", "Avenida das Nacoes"],
    "Florianopolis": ["Rua Bocaiuva", "Rua Felipe Schmidt", "Avenida Mauro Ramos", "Rua Esteves Junior"],
    "Goiania": ["Avenida Goias", "Rua 10", "Avenida T-63", "Rua 9"],
    "Belem": ["Avenida Nazare", "Travessa Padre Eutiquio", "Rua dos Mundurucus", "Avenida Almirante Barroso"],
    "Manaus": ["Avenida Eduardo Ribeiro", "Rua Miranda Leao", "Avenida Djalma Batista", "Rua Ramos Ferreira"],
    "Vitoria": ["Avenida Jeronimo Monteiro", "Rua Sete de Setembro", "Avenida Nossa Senhora da Penha", "Rua Aleixo Netto"],
    "Cuiaba": ["Avenida Getulio Vargas", "Rua Barão de Melgaço", "Avenida Historiador Rubens de Mendonça", "Rua 24 de Outubro"],
    "Campo Grande": ["Avenida Afonso Pena", "Rua 14 de Julho", "Rua Dom Aquino", "Avenida Mato Grosso"],
    "Natal": ["Avenida Prudente de Morais", "Rua Mossoro", "Avenida Hermes da Fonseca", "Rua Potengi"],
    "Joao Pessoa": ["Avenida Epitacio Pessoa", "Rua Duque de Caxias", "Avenida Almirante Tamandare", "Rua das Trincheiras"],
    "Maceio": ["Avenida Fernandes Lima", "Rua do Comercio", "Avenida Doutor Antonio Gouveia", "Rua Barao de Maceio"],
    "Aracaju": ["Avenida Beira Mar", "Rua Itabaiana", "Avenida Ivo do Prado", "Rua Laranjeiras"],
    "Campinas": ["Avenida Francisco Glicerio", "Rua Conceicao", "Avenida Orosimbo Maia", "Rua Barreto Leme", "Avenida Jose de Souza Campos"],
    "Santos": ["Avenida Conselheiro Nebias", "Avenida Ana Costa", "Rua XV de Novembro", "Avenida Washington Luis", "Rua Tolentino Filgueiras"],
    "Niteroi": ["Rua da Conceicao", "Avenida Amaral Peixoto", "Rua Gavio Peixoto", "Avenida Roberto Silveira", "Rua Miguel de Frias"],
    "Uberlandia": ["Avenida Afonso Pena", "Rua Olegario Maciel", "Avenida Joao Naves de Avila", "Rua Duque de Caxias", "Avenida Rondon Pacheco"],
    "Feira de Santana": ["Avenida Getulio Vargas", "Rua Conselheiro Franco", "Avenida Senhor dos Passos", "Rua Marechal Deodoro", "Avenida Maria Quiteria"],
    "Londrina": ["Avenida Higienopolis", "Rua Sergipe", "Avenida Juscelino Kubitschek", "Rua Pio XII", "Avenida Madre Leonia Milito"],
    "Caxias do Sul": ["Avenida Julio de Castilhos", "Rua Sinimbu", "Rua Feijo Junior", "Avenida Rio Branco", "Rua Os Dezoito do Forte"],
    "Olinda": ["Avenida Presidente Kennedy", "Rua do Sol", "Avenida Getulio Vargas", "Rua Prudente de Morais", "Avenida Carlos de Lima Cavalcanti"],
    "Juazeiro do Norte": ["Rua Sao Pedro", "Avenida Padre Cicero", "Rua Santa Luzia", "Avenida Castelo Branco", "Rua Sao Francisco"],
    "Anapolis": ["Avenida Brasil", "Rua Engenheiro Portela", "Avenida Goias", "Rua Manoel DAbadia", "Avenida Universitaria"],
    "Santarem": ["Avenida Rui Barbosa", "Travessa dos Martires", "Avenida Mendonca Furtado", "Rua Galdino Veloso", "Avenida Borges Leal"],
    "Joinville": ["Rua XV de Novembro", "Rua Blumenau", "Avenida Getulio Vargas", "Rua do Principe", "Rua Otto Boehm"],
}
BR_CARD_BINS = [
    {"bin": "414709", "length": 16, "brand": "Visa Debit"},
    {"bin": "516292", "length": 16, "brand": "Mastercard Debit"},
]
BR_EMAIL_DOMAINS = ["gmail.com", "hotmail.com", "outlook.com", "yahoo.com.br", "icloud.com", "uol.com.br", "bol.com.br"]

US_FIRST_NAMES = ["James", "John", "Robert", "Michael", "David", "William", "Daniel", "Matthew", "Joseph", "Andrew", "Emily", "Olivia", "Emma", "Sophia", "Ava", "Mia", "Charlotte", "Amelia"]
US_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris"]
US_LOCATIONS = [
    {"city": "Los Angeles", "state": "CA", "zips": ["90026", "90028", "90036", "90046"], "streets": ["Sunset Boulevard", "Wilshire Boulevard", "Melrose Avenue", "North Western Avenue", "Beverly Boulevard"]},
    {"city": "San Francisco", "state": "CA", "zips": ["94102", "94103", "94109", "94117"], "streets": ["Market Street", "Geary Boulevard", "Van Ness Avenue", "California Street", "Mission Street"]},
    {"city": "New York", "state": "NY", "zips": ["10001", "10003", "10011", "10019"], "streets": ["West 34th Street", "Madison Avenue", "Lexington Avenue", "Broadway", "West 23rd Street"]},
    {"city": "Brooklyn", "state": "NY", "zips": ["11201", "11211", "11215", "11222"], "streets": ["Atlantic Avenue", "Bedford Avenue", "Flatbush Avenue", "Court Street", "Nostrand Avenue"]},
    {"city": "Chicago", "state": "IL", "zips": ["60601", "60605", "60611", "60614"], "streets": ["North Michigan Avenue", "West Madison Street", "South State Street", "North Clark Street", "West Belmont Avenue"]},
    {"city": "Houston", "state": "TX", "zips": ["77002", "77006", "77019", "77027"], "streets": ["Main Street", "Westheimer Road", "Louisiana Street", "Kirby Drive", "Richmond Avenue"]},
    {"city": "Dallas", "state": "TX", "zips": ["75201", "75204", "75219", "75225"], "streets": ["McKinney Avenue", "Main Street", "Oak Lawn Avenue", "Preston Road", "Cedar Springs Road"]},
    {"city": "Austin", "state": "TX", "zips": ["78701", "78703", "78704", "78705"], "streets": ["Congress Avenue", "South Lamar Boulevard", "Guadalupe Street", "West 6th Street", "Barton Springs Road"]},
    {"city": "Phoenix", "state": "AZ", "zips": ["85004", "85006", "85012", "85016"], "streets": ["North Central Avenue", "East Van Buren Street", "West Washington Street", "East Camelback Road", "North 7th Street"]},
    {"city": "Seattle", "state": "WA", "zips": ["98101", "98102", "98109", "98121"], "streets": ["Pike Street", "1st Avenue", "Pine Street", "Queen Anne Avenue North", "Westlake Avenue"]},
    {"city": "Miami", "state": "FL", "zips": ["33130", "33131", "33133", "33137"], "streets": ["Brickell Avenue", "South Miami Avenue", "Biscayne Boulevard", "Coral Way", "North Miami Avenue"]},
    {"city": "Orlando", "state": "FL", "zips": ["32801", "32803", "32806", "32819"], "streets": ["East Colonial Drive", "Orange Avenue", "South Street", "International Drive", "Mills Avenue"]},
    {"city": "Boston", "state": "MA", "zips": ["02108", "02111", "02116", "02118"], "streets": ["Beacon Street", "Tremont Street", "Boylston Street", "Commonwealth Avenue", "Newbury Street"]},
    {"city": "Denver", "state": "CO", "zips": ["80202", "80203", "80205", "80206"], "streets": ["Colfax Avenue", "Broadway", "17th Street", "Speer Boulevard", "East 6th Avenue"]},
    {"city": "Portland", "state": "OR", "zips": ["97205", "97209", "97210", "97214"], "streets": ["West Burnside Street", "Northwest 23rd Avenue", "Southeast Hawthorne Boulevard", "Northeast Alberta Street", "Southwest Broadway"]},
    {"city": "Atlanta", "state": "GA", "zips": ["30303", "30305", "30308", "30309"], "streets": ["Peachtree Street", "Piedmont Avenue", "North Avenue", "West Paces Ferry Road", "Juniper Street"]},
    {"city": "Philadelphia", "state": "PA", "zips": ["19103", "19106", "19107", "19130"], "streets": ["Market Street", "Chestnut Street", "Walnut Street", "South Broad Street", "Spring Garden Street"]},
]
US_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com"]

BA_FIRST_NAMES = ["Amar", "Emir", "Adnan", "Edin", "Jasmin", "Nermin", "Amina", "Lejla", "Emina", "Amra", "Selma", "Sara"]
BA_LAST_NAMES = ["Hadzic", "Kovacevic", "Dedic", "Basic", "Music", "Delic", "Avdic", "Besic", "Imamovic", "Mujic", "Osmanovic", "Smajic"]
BA_LOCATIONS = [
    {"city": "Sarajevo", "state": "Federation of BiH", "zips": ["71000", "71001", "71210"], "streets": ["Zmaja od Bosne", "Marsala Tita", "Ferhadija", "Obala Kulina bana"]},
    {"city": "Mostar", "state": "Federation of BiH", "zips": ["88000", "88104"], "streets": ["Kneza Mihajla Visevica Humskog", "Marsala Tita", "Bulevar narodne revolucije"]},
    {"city": "Tuzla", "state": "Federation of BiH", "zips": ["75000", "75001"], "streets": ["Marsala Tita", "Aleja Alije Izetbegovica", "ZAVNOBiH-a"]},
    {"city": "Zenica", "state": "Federation of BiH", "zips": ["72000", "72001"], "streets": ["Marsala Tita", "Bulevar kralja Tvrtka I", "Sarajevska"]},
    {"city": "Banja Luka", "state": "Republika Srpska", "zips": ["78000", "78001"], "streets": ["Kralja Petra I Karadordevica", "Veselina Maslese", "Bulevar cara Dusana"]},
    {"city": "Brcko", "state": "Brcko District", "zips": ["76100", "76101"], "streets": ["Bulevar mira", "Bosne Srebrene", "Miroslava Krleze"]},
]
BA_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.com", "icloud.com", "yahoo.com"]

ID_FIRST_NAMES = [
    "Agus", "Budi", "Dimas", "Eko", "Fajar", "Hendra", "Rizky", "Andi",
    "Siti", "Dewi", "Ayu", "Putri", "Nur", "Maya", "Rina", "Indah",
]
ID_LAST_NAMES = [
    "Santoso", "Wijaya", "Saputra", "Pratama", "Hidayat", "Susanto",
    "Kurniawan", "Setiawan", "Wibowo", "Utomo", "Nugroho", "Permadi",
]
ID_LOCATIONS = [
    {"city": "Jakarta", "state": "DKI Jakarta", "zips": ["10110", "10220", "10310", "11470"], "streets": ["Jalan Sudirman", "Jalan Thamrin", "Jalan Gatot Subroto", "Jalan Kuningan"]},
    {"city": "Bandung", "state": "Jawa Barat", "zips": ["40111", "40115", "40251", "40614"], "streets": ["Jalan Asia Afrika", "Jalan Braga", "Jalan Diponegoro", "Jalan Pasteur"]},
    {"city": "Surabaya", "state": "Jawa Timur", "zips": ["60111", "60271", "60281", "60293"], "streets": ["Jalan Basuki Rahmat", "Jalan Darmo", "Jalan Pemuda", "Jalan Tunjungan"]},
    {"city": "Medan", "state": "Sumatera Utara", "zips": ["20111", "20152", "20212", "20236"], "streets": ["Jalan Gatot Subroto", "Jalan Ahmad Yani", "Jalan Diponegoro", "Jalan Sisingamangaraja"]},
    {"city": "Denpasar", "state": "Bali", "zips": ["80111", "80113", "80221", "80235"], "streets": ["Jalan Teuku Umar", "Jalan Imam Bonjol", "Jalan Hayam Wuruk", "Jalan Diponegoro"]},
]
ID_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com"]

GB_FIRST_NAMES = ["Oliver", "George", "Harry", "Jack", "Charlie", "Thomas", "Emily", "Olivia", "Amelia", "Sophie", "Grace", "Charlotte"]
GB_LAST_NAMES = ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Robinson", "Wright", "Thompson", "Evans"]
GB_LOCATIONS = [
    {"city": "London", "state": "England", "zips": ["NW1 6XE", "W1D 1BS", "SW3 4UD", "EC1A 1BB"], "streets": ["Baker Street", "Oxford Street", "King's Road", "Fleet Street"]},
    {"city": "Manchester", "state": "England", "zips": ["M3 2BW", "M1 7ED", "M1 3BE"], "streets": ["Deansgate", "Oxford Road", "Portland Street"]},
    {"city": "Birmingham", "state": "England", "zips": ["B2 4QA", "B1 2HF", "B4 6TB"], "streets": ["New Street", "Broad Street", "Corporation Street"]},
    {"city": "Edinburgh", "state": "Scotland", "zips": ["EH2 2ER", "EH2 2PF", "EH1 1SG"], "streets": ["Princes Street", "George Street", "Royal Mile"]},
    {"city": "Cardiff", "state": "Wales", "zips": ["CF10 1EP", "CF10 2HE", "CF11 9HB"], "streets": ["Queen Street", "Westgate Street", "Cathedral Road"]},
]
GB_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.co.uk", "icloud.com", "yahoo.co.uk"]


def _pick(values):
    return random.choice(values)


def _int(min_value: int, max_value: int) -> int:
    return random.randint(min_value, max_value)


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _luhn_check(partial: str) -> int:
    total = 0
    should_double = True
    for ch in reversed(partial):
        n = int(ch)
        if should_double:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        should_double = not should_double
    return 0 if total % 10 == 0 else 10 - (total % 10)


def generate_profile_password() -> str:
    letters = string.ascii_letters
    required = "0123456789!@#$%&*"
    all_chars = letters + required
    chars = [_pick(required)]
    while len(chars) < _int(8, 20):
        chars.append(_pick(all_chars))
    random.shuffle(chars)
    return "".join(chars)


def _generate_debit_card(bins: list[dict]) -> tuple[str, str]:
    chosen = _pick(bins)
    middle_length = chosen["length"] - len(chosen["bin"]) - 1
    middle = "".join(str(_int(0, 9)) for _ in range(middle_length))
    partial = chosen["bin"] + middle
    card_number = partial + str(_luhn_check(partial))
    formatted = " ".join(card_number[i:i + 4] for i in range(0, len(card_number), 4))
    return chosen["brand"], formatted


def _expiry_date() -> str:
    year = date.today().year + _int(2, 5)
    return f"{_int(1, 12):02d}/{str(year)[-2:]}"


def _birthday(year_first: bool = False, month_first: bool = False) -> str:
    year = _int(1970, 2000)
    month = _int(1, 12)
    day = _int(1, _days_in_month(year, month))
    if year_first:
        return f"{year}/{month:02d}/{day:02d}"
    if month_first:
        return f"{month:02d}/{day:02d}/{year}"
    return f"{day:02d}/{month:02d}/{year}"


def generate_jp_data() -> GeneratedProfile:
    last_name = _pick(JP_LAST_SYLLABLES_1) + _pick(JP_LAST_SYLLABLES_2)
    if random.random() < 0.5:
        idx = _int(0, len(JP_FIRST_PARTS_A) - 1)
        first_name = JP_FIRST_PARTS_A[idx] + JP_FIRST_PARTS_B[idx]
    else:
        idx = _int(0, len(JP_FIRST_PARTS_F_A) - 1)
        first_name = JP_FIRST_PARTS_F_A[idx] + JP_FIRST_PARTS_F_B[idx]

    loc = _pick(JP_LOCATIONS)
    zip_code = _pick(loc["zips"])
    town = _pick(JP_TOWN_NAMES)
    chome = _int(1, 9)
    banchi = _int(1, 32)
    go = _int(1, 28)
    street = f"{town['ja']} {chome}-{banchi}-{go}"
    full_address = f"〒{zip_code} {loc['prefecture_ja']} {loc['city_ja']} {town['ja']}{chome}-{banchi}-{go}"
    card_type, card_number = _generate_debit_card(JP_CARD_BINS)
    email = f"{_pick(JP_FIRST_NAMES_ROMAJI)}{_pick(JP_LAST_NAMES_ROMAJI)}{_int(100, 9999)}@{_pick(JP_EMAIL_DOMAINS)}"
    return GeneratedProfile(
        country="JP",
        last_name=last_name,
        first_name=first_name,
        birthday=_birthday(year_first=True),
        zip=zip_code,
        house_number=str(go),
        state=loc["prefecture_ja"],
        city=loc["city_ja"],
        street=street,
        full_address=full_address,
        card_type=card_type,
        card_number=card_number,
        exp_date=_expiry_date(),
        cvv=str(_int(100, 999)),
        cpf="-",
        email=email,
        password=generate_profile_password(),
    )


def _br_cpf_digits(base_digits: list[int]) -> list[int]:
    first_sum = sum(digit * (10 - idx) for idx, digit in enumerate(base_digits))
    first = 0 if first_sum % 11 < 2 else 11 - (first_sum % 11)
    second_base = [*base_digits, first]
    second_sum = sum(digit * (11 - idx) for idx, digit in enumerate(second_base))
    second = 0 if second_sum % 11 < 2 else 11 - (second_sum % 11)
    return [*base_digits, first, second]


def _br_generate_cpf() -> str:
    while True:
        base = [_int(0, 9) for _ in range(9)]
        if not all(digit == base[0] for digit in base):
            break
    digits = "".join(str(digit) for digit in _br_cpf_digits(base))
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def generate_br_data() -> GeneratedProfile:
    last_name = _pick(BR_LAST_NAMES)
    first_name = _pick(BR_FIRST_NAMES)
    loc = _pick(BR_LOCATIONS)
    state_name = BR_STATE_NAMES.get(loc["state"], loc["state"])
    zip_code = _pick(loc["ceps"])
    street = _pick(BR_STREETS_BY_CITY.get(loc["city"], BR_STREET_NAMES))
    house_number = str(_int(12, 4899))
    full_address = f"{street}, {house_number}, {loc['city']} - {loc['state']}, CEP {zip_code}"
    card_type, card_number = _generate_debit_card(BR_CARD_BINS)
    email = f"{first_name.lower()}.{last_name.lower()}{_int(10, 9999)}@{_pick(BR_EMAIL_DOMAINS)}"
    return GeneratedProfile(
        country="BR",
        last_name=last_name,
        first_name=first_name,
        birthday=_birthday(),
        zip=zip_code,
        house_number=house_number,
        state=f"{state_name} ({loc['state']})",
        city=loc["city"],
        street=street,
        full_address=full_address,
        card_type=card_type,
        card_number=card_number,
        exp_date=_expiry_date(),
        cvv=str(_int(100, 999)),
        cpf=_br_generate_cpf(),
        email=email,
        password=generate_profile_password(),
    )


def generate_us_data() -> GeneratedProfile:
    first_name = _pick(US_FIRST_NAMES)
    last_name = _pick(US_LAST_NAMES)
    loc = _pick(US_LOCATIONS)
    zip_code = _pick(loc["zips"])
    house_number = str(_int(100, 9999))
    street = f"{house_number} {_pick(loc['streets'])}"
    if random.random() < 0.42:
        unit_number = str(_int(1, 999)) if random.random() < 0.5 else f"{_int(1, 30)}{_pick(['A', 'B', 'C', 'D'])}"
        street += f", {_pick(['Apt', 'Suite', 'Unit', '#'])} {unit_number}"
    full_address = f"{street}, {loc['city']}, {loc['state']} {zip_code}, USA"
    card_type, card_number = _generate_debit_card([*JP_CARD_BINS, *BR_CARD_BINS])
    email = f"{first_name.lower()}.{last_name.lower()}{_int(10, 9999)}@{_pick(US_EMAIL_DOMAINS)}"
    return GeneratedProfile(
        country="US",
        last_name=last_name,
        first_name=first_name,
        birthday=_birthday(month_first=True),
        zip=zip_code,
        house_number=house_number,
        state=loc["state"],
        city=loc["city"],
        street=street,
        full_address=full_address,
        card_type=card_type,
        card_number=card_number,
        exp_date=_expiry_date(),
        cvv=str(_int(100, 999)),
        cpf="-",
        email=email,
        password=generate_profile_password(),
    )


def generate_ba_data() -> GeneratedProfile:
    first_name = _pick(BA_FIRST_NAMES)
    last_name = _pick(BA_LAST_NAMES)
    loc = _pick(BA_LOCATIONS)
    zip_code = _pick(loc["zips"])
    house_number = str(_int(1, 199))
    street = f"{house_number} {_pick(loc['streets'])}"
    full_address = f"{street}, {loc['city']}, {zip_code}, Bosnia and Herzegovina"
    card_type, card_number = _generate_debit_card([*JP_CARD_BINS, *BR_CARD_BINS])
    email = f"{first_name.lower()}.{last_name.lower()}{_int(10, 9999)}@{_pick(BA_EMAIL_DOMAINS)}"
    return GeneratedProfile(
        country="BA",
        last_name=last_name,
        first_name=first_name,
        birthday=_birthday(),
        zip=zip_code,
        house_number=house_number,
        state=loc["state"],
        city=loc["city"],
        street=street,
        full_address=full_address,
        card_type=card_type,
        card_number=card_number,
        exp_date=_expiry_date(),
        cvv=str(_int(100, 999)),
        cpf="-",
        email=email,
        password=generate_profile_password(),
    )


def generate_id_data() -> GeneratedProfile:
    first_name = _pick(ID_FIRST_NAMES)
    last_name = _pick(ID_LAST_NAMES)
    loc = _pick(ID_LOCATIONS)
    zip_code = _pick(loc["zips"])
    house_number = str(_int(1, 199))
    street = f"{_pick(loc['streets'])} No. {house_number}"
    full_address = f"{street}, {loc['city']}, {loc['state']} {zip_code}, Indonesia"
    card_type, card_number = _generate_debit_card([*JP_CARD_BINS, *BR_CARD_BINS])
    email = f"{first_name.lower()}.{last_name.lower()}{_int(10, 9999)}@{_pick(ID_EMAIL_DOMAINS)}"
    return GeneratedProfile(
        country="ID",
        last_name=last_name,
        first_name=first_name,
        birthday=_birthday(),
        zip=zip_code,
        house_number=house_number,
        state=loc["state"],
        city=loc["city"],
        street=street,
        full_address=full_address,
        card_type=card_type,
        card_number=card_number,
        exp_date=_expiry_date(),
        cvv=str(_int(100, 999)),
        cpf="-",
        email=email,
        password=generate_profile_password(),
    )


def generate_gb_data() -> GeneratedProfile:
    first_name = _pick(GB_FIRST_NAMES)
    last_name = _pick(GB_LAST_NAMES)
    loc = _pick(GB_LOCATIONS)
    zip_code = _pick(loc["zips"])
    house_number = str(_int(1, 199))
    street = f"{house_number} {_pick(loc['streets'])}"
    if random.random() < 0.35:
        street = f"Flat {_int(1, 30)}, {street}"
    full_address = f"{street}, {loc['city']}, {zip_code}, United Kingdom"
    card_type, card_number = _generate_debit_card([*JP_CARD_BINS, *BR_CARD_BINS])
    email = f"{first_name.lower()}.{last_name.lower()}{_int(10, 9999)}@{_pick(GB_EMAIL_DOMAINS)}"
    return GeneratedProfile(
        country="GB",
        last_name=last_name,
        first_name=first_name,
        birthday=_birthday(),
        zip=zip_code,
        house_number=house_number,
        state=loc["state"],
        city=loc["city"],
        street=street,
        full_address=full_address,
        card_type=card_type,
        card_number=card_number,
        exp_date=_expiry_date(),
        cvv=str(_int(100, 999)),
        cpf="-",
        email=email,
        password=generate_profile_password(),
    )


def normalize_profile_country(country: str) -> str:
    value = (country or "").strip().upper()
    aliases = {"JA": "JP", "JPN": "JP", "UK": "GB", "GBR": "GB", "USA": "US", "BRA": "BR", "BIH": "BA", "IDN": "ID"}
    value = aliases.get(value, value)
    if value not in SUPPORTED_PROFILE_COUNTRIES:
        raise ValueError(f"unsupported profile country: {country!r}")
    return value


def generate_profile_data(country: str = "JP") -> GeneratedProfile:
    country = normalize_profile_country(country)
    if country == "BR":
        return generate_br_data()
    if country == "US":
        return generate_us_data()
    if country == "BA":
        return generate_ba_data()
    if country == "ID":
        return generate_id_data()
    if country == "GB":
        return generate_gb_data()
    return generate_jp_data()


def generate_profile_dict(country: str = "JP") -> dict:
    return asdict(generate_profile_data(country))
