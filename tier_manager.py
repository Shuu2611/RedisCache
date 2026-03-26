#!/usr/bin/env python3
import time
from collections import defaultdict
import threading


class TierManager:

    SCORE_THRESHOLD = 20
    HOT_MEMORY_PERCENT = 5
    TOTAL_REDIS_MEMORY_MB = 100
    WEIGHT_FREQUENCY = 0.8
    WEIGHT_RECENCY = 0.2

    # [IMPROVE] Tăng từ 60s lên 90s. Hardcode 60s quá nhạy với Zipfian workload
    DEMOTION_IDLE_SECONDS = 90

    def __init__(self, hot_memory_percent=5, total_memory_mb=100,
                 demotion_idle_seconds=None):
        self.HOT_MEMORY_PERCENT = hot_memory_percent
        self.TOTAL_REDIS_MEMORY_MB = total_memory_mb
        self.HOT_MAX_MEMORY_BYTES = int(total_memory_mb * 1024 * 1024 * (hot_memory_percent / 100))
        self.COLD_MAX_MEMORY_BYTES = int(total_memory_mb * 1024 * 1024 * ((100 - hot_memory_percent) / 100))

        if demotion_idle_seconds is not None:
            self.DEMOTION_IDLE_SECONDS = demotion_idle_seconds

        self.key_stats = defaultdict(lambda: {
            'count': 0,
            'last_access': time.time(),
            'insert_time': time.time(),
            'tier': 'cold',
            'promoted': False,
            'size': 0
        })

        self.lock = threading.RLock()
        self.stats = {
            'hot_keys': 0,
            'cold_keys': 0,
            'hot_memory_bytes': 0,
            'cold_memory_bytes': 0,
            'promotions': 0,
            'demotions': 0,
            'evictions': 0
        }

    def record_access(self, key, size_bytes=0):
        with self.lock:
            stats = self.key_stats[key]
            old_size = stats['size']
            old_tier = stats['tier']
            stats['count'] += 1
            stats['last_access'] = time.time()

            if size_bytes > 0 and size_bytes != old_size:
                stats['size'] = size_bytes
                delta = size_bytes - old_size
                # FIX #3: Cập nhật đúng tier hiện tại của key
                if old_tier == 'hot':
                    self.stats['hot_memory_bytes'] = max(0, self.stats['hot_memory_bytes'] + delta)
                else:
                    self.stats['cold_memory_bytes'] = max(0, self.stats['cold_memory_bytes'] + delta)

    def _calculate_score_unlocked(self, key, now):
        """Tính score mà KHÔNG acquire lock (caller phải đang giữ lock)."""
        if key not in self.key_stats:
            return 0
        stats = self.key_stats[key]
        hours_since_access = (now - stats['last_access']) / 3600.0
        recency_score = 100.0 / (1.0 + hours_since_access)
        return (stats['count'] * self.WEIGHT_FREQUENCY) + (recency_score * self.WEIGHT_RECENCY)

    def calculate_score(self, key):
        with self.lock:
            return self._calculate_score_unlocked(key, time.time())

    def classify_tier(self, key):
        if key not in self.key_stats:
            return 'cold'
            
        if self.key_stats[key]['count'] < 3:
            return 'cold'
            
        score = self.calculate_score(key)
        return 'hot' if score >= self.SCORE_THRESHOLD else 'cold'

    def should_promote(self, key, current_tier):
        if current_tier == 'hot':
            return False, 'hot', 'already_hot'

        with self.lock:
            if key not in self.key_stats or self.key_stats[key]['count'] < 3:
                return False, 'cold', 'insufficient_hits'

            now = time.time()
            score = self._calculate_score_unlocked(key, now)
            if score < self.SCORE_THRESHOLD:
                return False, 'cold', 'below_threshold'

            key_size = self.key_stats[key]['size']
            hot_memory = self.stats['hot_memory_bytes']
            if hot_memory + key_size > self.HOT_MAX_MEMORY_BYTES:
                return False, 'cold', 'hot_memory_full'

        return True, 'hot', 'threshold_met'
        if current_tier == 'hot':
            return False, 'hot', 'already_hot'

        with self.lock:
            now = time.time()
            score = self._calculate_score_unlocked(key, now)
            if score < self.SCORE_THRESHOLD:
                return False, 'cold', 'below_threshold'

            key_size = self.key_stats[key]['size']
            hot_memory = self.stats['hot_memory_bytes']
            if hot_memory + key_size > self.HOT_MAX_MEMORY_BYTES:
                return False, 'cold', 'hot_memory_full'

        return True, 'hot', 'threshold_met'

    def should_demote(self, key, current_tier):
        if current_tier == 'cold':
            return False, 'cold'

        with self.lock:
            if key not in self.key_stats:
                return False, 'hot'
            seconds_since = time.time() - self.key_stats[key]['last_access']

            # [IMPROVE] Demotion có 2 điều kiện thay vì chỉ timeout 150s:
            # 1. Key idle > DEMOTION_IDLE_SECONDS VÀ HOT đang chịu lượng WL (>85%)
            # 2. Key idle > 2× threshold thì demote bất kể lượng WL
            hot_pressure = (
                self.stats['hot_memory_bytes'] / self.HOT_MAX_MEMORY_BYTES
            ) if self.HOT_MAX_MEMORY_BYTES > 0 else 0

            if seconds_since > self.DEMOTION_IDLE_SECONDS and hot_pressure > 0.85:
                return True, 'cold'

            if seconds_since > self.DEMOTION_IDLE_SECONDS * 2:
                return True, 'cold'

        return False, 'hot'

    def update_tier(self, key, new_tier):
        with self.lock:
            old_tier = self.key_stats[key]['tier']
            key_size = self.key_stats[key]['size']
            self.key_stats[key]['tier'] = new_tier

            if old_tier != new_tier:
                # FIX #3: Bảo vệ bằng max(0,...) để tránh giá trị âm
                if old_tier == 'hot':
                    self.stats['hot_memory_bytes'] = max(0, self.stats['hot_memory_bytes'] - key_size)
                else:
                    self.stats['cold_memory_bytes'] = max(0, self.stats['cold_memory_bytes'] - key_size)

                if new_tier == 'hot':
                    self.stats['hot_memory_bytes'] += key_size
                    self.stats['promotions'] += 1
                else:
                    self.stats['cold_memory_bytes'] += key_size
                    self.stats['demotions'] += 1

    def get_tier(self, key):
        with self.lock:
            return self.key_stats[key]['tier']

    def get_keys_by_tier(self, tier):
        with self.lock:
            return [k for k, v in self.key_stats.items() if v['tier'] == tier]

    def get_all_keys_snapshot(self):
        with self.lock:
            return list(self.key_stats.keys())

    def set_key_size(self, key, size_bytes):
        with self.lock:
            if key in self.key_stats:
                old_size = self.key_stats[key]['size']
                tier = self.key_stats[key]['tier']
                delta = size_bytes - old_size
                self.key_stats[key]['size'] = size_bytes
                # FIX #3: Cập nhật memory accounting khi thay đổi size
                if tier == 'hot':
                    self.stats['hot_memory_bytes'] = max(0, self.stats['hot_memory_bytes'] + delta)
                else:
                    self.stats['cold_memory_bytes'] = max(0, self.stats['cold_memory_bytes'] + delta)

    def get_coldest_keys(self, limit=100):
        with self.lock:
            now = time.time()
            scored_keys = []
            for key, stats in self.key_stats.items():
                hours_since_access = (now - stats['last_access']) / 3600.0
                recency_score = 100.0 / (1.0 + hours_since_access)
                score = (stats['count'] * self.WEIGHT_FREQUENCY) + (recency_score * self.WEIGHT_RECENCY)
                scored_keys.append((key, score))

            sorted_keys = sorted(scored_keys, key=lambda x: x[1])
            return [k for k, _ in sorted_keys[:limit]]

    def _get_lru_key_unlocked(self, tier):
        """Trả về LRU key — KHÔNG acquire lock (caller phải đang giữ lock).
        Dùng tên _unlocked để tránh gọi nhầm từ ngoài mà không có lock."""
        tier_keys = [(k, v['last_access']) for k, v in self.key_stats.items() if v['tier'] == tier]
        if not tier_keys:
            return None
        return min(tier_keys, key=lambda x: x[1])[0]

    def get_lru_key(self, tier):
        """Alias an toàn — chỉ gọi khi đang giữ self.lock."""
        return self._get_lru_key_unlocked(tier)

    def check_memory_limit(self, tier):
        """Giữ lock duy nhất, gọi unlocked version của get_lru_key."""
        with self.lock:
            if tier == 'hot':
                current_memory = self.stats['hot_memory_bytes']
                max_memory = self.HOT_MAX_MEMORY_BYTES
            else:
                current_memory = self.stats['cold_memory_bytes']
                max_memory = self.COLD_MAX_MEMORY_BYTES

            if current_memory > max_memory:
                lru_key = self._get_lru_key_unlocked(tier)
                return True, lru_key

            return False, None

    def remove_key(self, key):
        with self.lock:
            if key not in self.key_stats:
                return False

            stats = self.key_stats[key]
            tier = stats['tier']
            size = stats['size']

            if tier == 'hot':
                self.stats['hot_memory_bytes'] = max(0, self.stats['hot_memory_bytes'] - size)
            else:
                self.stats['cold_memory_bytes'] = max(0, self.stats['cold_memory_bytes'] - size)

            del self.key_stats[key]
            self.stats['evictions'] += 1
            return True

    def get_statistics(self):
        acquired = self.lock.acquire(timeout=5.0)
        if not acquired:
            return {
                'total_keys': 0,
                'hot_keys': 0,
                'cold_keys': 0,
                'hot_pct': 0,
                'cold_pct': 0,
                'hot_memory_bytes': self.stats['hot_memory_bytes'],
                'cold_memory_bytes': self.stats['cold_memory_bytes'],
                'total_memory_bytes': self.stats['hot_memory_bytes'] + self.stats['cold_memory_bytes'],
                'hot_memory_mb': self.stats['hot_memory_bytes'] / (1024 * 1024),
                'cold_memory_mb': self.stats['cold_memory_bytes'] / (1024 * 1024),
                'total_memory_mb': (self.stats['hot_memory_bytes'] + self.stats['cold_memory_bytes']) / (1024 * 1024),
                'hot_memory_pct': 0,
                'cold_memory_pct': 0,
                'hot_memory_limit_mb': self.HOT_MAX_MEMORY_BYTES / (1024 * 1024),
                'cold_memory_limit_mb': self.COLD_MAX_MEMORY_BYTES / (1024 * 1024),
                'hot_memory_utilization': 0,
                'cold_memory_utilization': 0,
                'promotions': self.stats['promotions'],
                'demotions': self.stats['demotions'],
                'evictions': self.stats['evictions']
            }

        try:
            hot = sum(1 for v in self.key_stats.values() if v['tier'] == 'hot')
            cold = sum(1 for v in self.key_stats.values() if v['tier'] == 'cold')
            total = len(self.key_stats)

            hot_mem = self.stats['hot_memory_bytes']
            cold_mem = self.stats['cold_memory_bytes']
            total_mem = hot_mem + cold_mem

            return {
                'total_keys': total,
                'hot_keys': hot,
                'cold_keys': cold,
                'hot_pct': (hot / total * 100) if total > 0 else 0,
                'cold_pct': (cold / total * 100) if total > 0 else 0,
                'hot_memory_bytes': hot_mem,
                'cold_memory_bytes': cold_mem,
                'total_memory_bytes': total_mem,
                'hot_memory_mb': hot_mem / (1024 * 1024),
                'cold_memory_mb': cold_mem / (1024 * 1024),
                'total_memory_mb': total_mem / (1024 * 1024),
                'hot_memory_pct': (hot_mem / total_mem * 100) if total_mem > 0 else 0,
                'cold_memory_pct': (cold_mem / total_mem * 100) if total_mem > 0 else 0,
                'hot_memory_limit_mb': self.HOT_MAX_MEMORY_BYTES / (1024 * 1024),
                'cold_memory_limit_mb': self.COLD_MAX_MEMORY_BYTES / (1024 * 1024),
                'hot_memory_utilization': (hot_mem / self.HOT_MAX_MEMORY_BYTES * 100) if self.HOT_MAX_MEMORY_BYTES > 0 else 0,
                'cold_memory_utilization': (cold_mem / self.COLD_MAX_MEMORY_BYTES * 100) if self.COLD_MAX_MEMORY_BYTES > 0 else 0,
                'promotions': self.stats['promotions'],
                'demotions': self.stats['demotions'],
                'evictions': self.stats['evictions'],
                # [IMPROVE W1] Thêm config thực tế tại thời điểm query.
                # Giúp report luôn reflect đúng trạng thái runtime (sau adaptive adjust)
                # thay vì chỉ in snapshot config ban đầu.
                'hot_memory_percent_effective': self.HOT_MEMORY_PERCENT,
                'hot_memory_limit_effective_mb': self.HOT_MAX_MEMORY_BYTES / (1024 * 1024),
                'demotion_idle_seconds': self.DEMOTION_IDLE_SECONDS,
            }
        finally:
            self.lock.release()