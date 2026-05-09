"""Analysis functions for the Steam Workshop Scraper."""

import statistics
import time
from src.database import get_connection


def view_window_analysis(db_path: str, bucket_days: int = 7) -> dict:
    """
    Buckets workshop items by age and computes median views per bucket.
    Finds the 'knee' where views plateau, indicating the likely view-window cutoff.
    Returns analysis dict with buckets and estimated_window_days.
    """
    conn = get_connection(db_path)
    cursor = conn.execute("""
        SELECT time_created, views FROM workshop_items
        WHERE time_created IS NOT NULL
          AND time_created > 0
          AND views IS NOT NULL
          AND views > 0
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return {"buckets": [], "estimated_window_days": None, "items_analyzed": 0}

    now = int(time.time())
    max_bucket_days = 365 * 3  # 3 years max range
    num_buckets = (max_bucket_days // bucket_days) + 1
    buckets = [[] for _ in range(num_buckets)]

    for row in rows:
        age_days = max(0, (now - row["time_created"]) // 86400)
        idx = min(age_days // bucket_days, num_buckets - 1)
        buckets[idx].append(row["views"])

    result_buckets = []
    for i, bv in enumerate(buckets):
        if not bv:
            continue
        bv_sorted = sorted(bv)
        n = len(bv_sorted)
        result_buckets.append({
            "age_start": i * bucket_days,
            "age_end": (i + 1) * bucket_days,
            "count": n,
            "median": bv_sorted[n // 2],
            "p10": bv_sorted[n // 10],
            "p90": bv_sorted[n * 9 // 10],
        })

    # Find the knee: first bucket where median drops below 25% of the rolling max
    if len(result_buckets) < 3:
        return {"buckets": result_buckets, "estimated_window_days": None,
                "items_analyzed": len(rows)}

    # Compute a smoothed peak (max median in first N buckets)
    early_buckets = result_buckets[:min(10, len(result_buckets))]
    window_baseline = max(b["median"] for b in early_buckets) if early_buckets else 0

    estimated = None
    for b in result_buckets:
        if b["median"] < window_baseline * 0.25 and estimated is None:
            estimated = b["age_start"]
            break

    return {
        "buckets": result_buckets,
        "estimated_window_days": estimated,
        "items_analyzed": len(rows),
    }
