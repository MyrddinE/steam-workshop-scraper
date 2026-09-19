import pytest
import os
import json
import math
import random
import time
from src.database import initialize_database, insert_or_update_item, get_connection
from src.daemon import wilson_lower

ASYNC_PAUSE = 0.25


def restore_pre_rename_table_names(conn) -> None:
    """Undo migration 29->30's table renames for a rewound version marker.

    The migration tests age a current database by rewinding
    ``PRAGMA user_version`` alone. Before 29->30 renamed the two tables that
    was enough to reconstruct an older shape; now the marker can say 27 while
    the tables are already ``creators``/``app_discovery``, and the replayed
    chain's historical SQL (for example migration 27->28) still names
    ``users``. Rename them back so the database actually matches the version
    its marker claims.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if "creators" in tables and "users" not in tables:
        conn.execute("ALTER TABLE creators RENAME TO users")
    if "app_discovery" in tables and "app_tracking" not in tables:
        conn.execute("ALTER TABLE app_discovery RENAME TO app_tracking")

# Deterministic test database constants
_DET_SEED = 42
_DET_NUM_ITEMS = 10000
_DET_LOREM = (
    "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua Ut enim ad minim veniam quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat Duis aute "
    "irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla "
    "pariatur Excepteur sint occaecat cupidatat non proident sunt in culpa qui officia "
    "deserunt mollit anim id est laborum"
)
_DET_LOREM_WORDS = list(dict.fromkeys(_DET_LOREM.lower().split()))

_DET_CHINESE = (
    "天地玄黄 宇宙洪荒 日月盈昃 辰宿列张 寒来暑往 秋收冬藏 闰余成岁 律吕调阳 "
    "云腾致雨 露结为霜 金生丽水 玉出昆冈 剑号巨阙 珠称夜光 果珍李柰 菜重芥姜 "
    "海咸河淡 鳞潜羽翔 龙师火帝 鸟官人皇 始制文字 乃服衣裳 推位让国 有虞陶唐 "
    "吊民伐罪 周发殷汤 坐朝问道 垂拱平章 爱育黎首 臣伏戎羌 遐迩一体 率宾归王 "
    "鸣凤在竹 白驹食场 化被草木 赖及万方 盖此身发 四大五常 恭惟鞠养 岂敢毁伤 "
    "女慕贞洁 男效才良 知过必改 得能莫忘 罔谈彼短 靡恃己长 信使可覆 器欲难量 "
    "墨悲丝染 诗赞羔羊 景行维贤 克念作圣 德建名立 形端表正 空谷传声 虚堂习听 "
    "祸因恶积 福缘善庆 尺璧非宝 寸阴是竞 资父事君 曰严与敬 孝当竭力 忠则尽命 "
    "临深履薄 夙兴温清 似兰斯馨 如松之盛 川流不息 渊澄取映 容止若思 言辞安定 "
    "笃初诚美 慎终宜令 荣业所基 籍甚无竟 学优登仕 摄职从政 存以甘棠 去而益咏 "
    "乐殊贵贱 礼别尊卑 上和下睦 夫唱妇随 外受傅训 入奉母仪 诸姑伯叔 犹子比儿 "
    "孔怀兄弟 同气连枝 交友投分 切磨箴规 仁慈隐恻 造次弗离 节义廉退 颠沛匪亏 "
    "性静情逸 心动神疲 守真志满 逐物意移 坚持雅操 好爵自縻"
)
_DET_CHINESE_PHRASES = [_DET_CHINESE[i:i+2] for i in range(0, len(_DET_CHINESE), 2) if len(_DET_CHINESE[i:i+2].strip()) == 2]

_DET_TAGS = ["mod", "texture", "sound", "map", "skin", "script", "animation", "weapon", "character", "ui"]


def _det_log_random(rng, lo, hi):
    """Uniform in log10 space with zero bias (subtract 2, clamp to 0)."""
    lo_log = math.log10(lo) if lo > 0 else 0
    hi_log = math.log10(hi)
    return max(0, int(10 ** rng.uniform(lo_log, hi_log)) - 2)


def _det_pick_words(rng, source, count):
    return " ".join(rng.choices(source, k=count))


@pytest.fixture(scope="session")
def deterministic_db(tmp_path_factory):
    """Creates a deterministically-random database with 10000 workshop items.
    Uses a fixed seed (42) for reproducible test data. Session-scoped so all
    filter tests share the same pre-built database."""
    db_path = str(tmp_path_factory.mktemp("data") / "test.db")
    initialize_database(db_path)
    rng = random.Random(_DET_SEED)
    now = int(time.time())  # used for first_seen_at in insert_or_update_item

    # Pre-generate IDs with random spacing
    app_ids = []
    nxt = 10000
    for _ in range(5):
        app_ids.append(nxt)
        nxt += 1 + rng.randint(1, 100)

    author_ids = []
    nxt = 1_000_000_000
    for _ in range(1000):
        author_ids.append(nxt)
        nxt += 1 + rng.randint(1, 100)

    workshop_ids = []
    nxt = 1_000_000
    for _ in range(_DET_NUM_ITEMS):
        workshop_ids.append(nxt)
        nxt += 1 + rng.randint(1, 100)

    for i in range(_DET_NUM_ITEMS):
        wid = workshop_ids[i]
        use_chinese = i >= _DET_NUM_ITEMS // 2
        has_translation = use_chinese and (i % 2 == 0)
        source = _DET_CHINESE_PHRASES if use_chinese else _DET_LOREM_WORDS

        title = _det_pick_words(rng, source, rng.randint(1, 10))
        desc = _det_pick_words(rng, source, rng.randint(1, 50))
        short_desc = desc[:200] if len(desc) > 200 else desc

        file_size = _det_log_random(rng, 100_000, 100_000_000_000)
        views = _det_log_random(rng, 1, 1_000_000)
        lifetime_subs = _det_log_random(rng, 1, 10_000_000)
        lifetime_favs = _det_log_random(rng, 1, 100_000_000)
        subs_pct = rng.uniform(0.01, 1.0)
        favs_pct = rng.uniform(0.01, 1.0)
        current_subs = int(lifetime_subs * subs_pct)
        current_favs = int(lifetime_favs * favs_pct)

        wfs = wilson_lower(current_favs, lifetime_subs)
        wss = wilson_lower(current_subs, lifetime_subs)

        num_tags = rng.randint(2, 5)
        item_tags = rng.sample(_DET_TAGS, num_tags)

        age_days = rng.randint(1, 1095)
        created = now - age_days * 86400
        updated = created + rng.randint(0, min(age_days, 30)) * 86400

        item = {
            "workshop_id": wid,
            "title": title,
            "short_description": short_desc,
            "extended_description": desc,
            "creator": rng.choice(author_ids),
            "consumer_appid": rng.choice(app_ids),
            "file_size": file_size,
            "views": views,
            "subscriptions": current_subs,
            "lifetime_subscriptions": lifetime_subs,
            "favorited": current_favs,
            "lifetime_favorited": lifetime_favs,
            "wilson_favorite_score": wfs,
            "wilson_subscription_score": wss,
            "tags": item_tags,
            "steam_created_at": created,
            "steam_updated_at": updated,
            "status": 200,        }

        if has_translation:
            item["title_en"] = _det_pick_words(rng, _DET_LOREM_WORDS, rng.randint(1, 10))
            item["short_description_en"] = _det_pick_words(rng, _DET_LOREM_WORDS, rng.randint(1, 50))
            item["extended_description_en"] = _det_pick_words(rng, _DET_LOREM_WORDS, rng.randint(1, 50))
            item["translate_version"] = item["steam_updated_at"]

        insert_or_update_item(db_path, item)

    # Rebuild FTS5 content-sync index
    conn = get_connection(db_path)
    conn.execute("INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()

    return db_path

@pytest.fixture
def mock_config(db_path):
    return {
        "database": {"path": db_path},
        "logging": {"level": "INFO"}
    }

@pytest.fixture
def mock_config_with_api(db_path):
    """Config with API key for daemon tests."""
    return {
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"api_batch_size": 2, "request_delay_seconds": 0.01, "target_appids": [123]}
    }

@pytest.fixture
def db_path(tmp_path):
    """Fixture providing a temporary initialized database."""
    path = str(tmp_path / "test_workshop.db")
    initialize_database(path)
    return path

@pytest.fixture(autouse=True)
def cleanup_tui_state():
    """Remove state files written beside the working directory by a test.

    Both are runtime state, not source: a test that constructs a worker with its
    default paths would otherwise leave one in the repository root.
    """
    yield
    for name in (".tui_state.yaml", ".daemon_state.yaml", ".daemon_state.yaml.tmp"):
        if os.path.exists(name):
            os.remove(name)


@pytest.fixture(autouse=True)
def cleanup_crash_hooks():
    """The entry points install process-wide crash hooks; no test may leak them.

    `src.tui:main`, `src.web_runner:main` and `src.daemon_runner:main` all call
    `crash.install`, and a test that drives one of them would otherwise leave
    `sys.excepthook` and a root log handler installed for every test after it.
    """
    yield
    from src import crash
    crash.uninstall()
