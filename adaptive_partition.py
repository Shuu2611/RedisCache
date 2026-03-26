import time
import threading
from datetime import datetime

class AdaptivePartitioner:
    def __init__(self, tier_manager, log_file=None):
        self.tier_manager = tier_manager
        self.log_file = log_file

        # Cost model đã được calibrate
        self.C_HOT = 500    # μs - Redis HGETALL + proxy overhead
        self.C_COLD = 700   # μs - Redis GET + decompress + proxy overhead
        self.C_MISS = 5400  # μs - MongoDB + proxy overhead

        self.STEP_SIZE_PERCENT = 1
        self.MIN_HOT_PERCENT = 5
        self.MAX_HOT_PERCENT = 30

        # [IMPROVED] Thread-Local Counters thay vì Global Lock
        # Mỗi thread sẽ có một dictionary đếm riêng biệt
        self._init_lock = threading.Lock()
        self.thread_stats = {} # Map từ thread_id -> dictionary stats
        self.last_reset = time.time()
        self.adjustment_history = []

    def _get_local_stats(self):
        """Lấy hoặc khởi tạo bộ đếm riêng cho thread hiện tại."""
        tid = threading.get_ident()
        stats = self.thread_stats.get(tid)
        
        # Chỉ dùng lock khi khởi tạo lần đầu cho một thread mới
        if stats is None:
            with self._init_lock:
                if tid not in self.thread_stats:
                    self.thread_stats[tid] = {
                        'total_requests': 0,
                        'hot_hits': 0,
                        'cold_hits': 0,
                        'misses': 0
                    }
                stats = self.thread_stats[tid]
        return stats

    def record_request(self, tier):
        """Ghi nhận kết quả request — siêu tốc, lock-free trên hot path."""
        stats = self._get_local_stats()
        
        # Chỉ thread hiện tại mới ghi vào biến stats này nên không lo race condition
        stats['total_requests'] += 1
        if tier == 'hot':
            stats['hot_hits'] += 1
        elif tier == 'cold':
            stats['cold_hits'] += 1
        else:
            stats['misses'] += 1
    
    def calculate_cost(self, hot_hit_rate, cold_hit_rate):
        """Calculate total cost based on hit rates"""
        miss_rate = 1.0 - hot_hit_rate - cold_hit_rate
        cost = (hot_hit_rate * self.C_HOT + 
                cold_hit_rate * self.C_COLD + 
                miss_rate * self.C_MISS)
        return cost
    
    def get_current_hit_rates(self):
        """Tính hit rates bằng cách gom (aggregate) tất cả các thread-local counters."""
        total = 0
        hot = 0
        cold = 0
        miss = 0
        
        # list() để tránh lỗi runtime nếu có thread mới được thêm vào lúc đang lặp
        for stats in list(self.thread_stats.values()):
            total += stats['total_requests']
            hot += stats['hot_hits']
            cold += stats['cold_hits']
            miss += stats['misses']
            
        if total == 0:
            return 0.0, 0.0, 0.0
            
        hot_rate = hot / total
        cold_rate = cold / total
        miss_rate = miss / total
        return hot_rate, cold_rate, miss_rate
    
    def estimate_hit_rates_after_adjustment(self, current_hot_percent, new_hot_percent):
        hot_rate, cold_rate, miss_rate = self.get_current_hit_rates()
        delta_percent = new_hot_percent - current_hot_percent
        
        if delta_percent > 0:
            extra_hot_capacity = delta_percent / 100.0
            miss_to_hot_transfer = miss_rate * 0.3 * extra_hot_capacity
            cold_to_hot_transfer = cold_rate * 0.2 * extra_hot_capacity
            
            new_hot_rate = hot_rate + miss_to_hot_transfer + cold_to_hot_transfer
            new_cold_rate = cold_rate - cold_to_hot_transfer
            new_miss_rate = miss_rate - miss_to_hot_transfer
        else:
            reduced_hot_capacity = abs(delta_percent) / 100.0
            hot_to_cold_transfer = hot_rate * 0.4 * reduced_hot_capacity
            hot_to_miss_transfer = hot_rate * 0.1 * reduced_hot_capacity
            
            compression_benefit = reduced_hot_capacity * 0.5
            miss_to_cold_transfer = miss_rate * 0.3 * compression_benefit
            
            new_hot_rate = hot_rate - hot_to_cold_transfer - hot_to_miss_transfer
            new_cold_rate = cold_rate + hot_to_cold_transfer + miss_to_cold_transfer
            new_miss_rate = miss_rate + hot_to_miss_transfer - miss_to_cold_transfer
        
        new_hot_rate = max(0.0, min(1.0, new_hot_rate))
        new_cold_rate = max(0.0, min(1.0, new_cold_rate))
        new_miss_rate = max(0.0, min(1.0, new_miss_rate))
        
        total = new_hot_rate + new_cold_rate + new_miss_rate
        if total > 0:
            new_hot_rate /= total
            new_cold_rate /= total
            new_miss_rate /= total
            
        return new_hot_rate, new_cold_rate, new_miss_rate
    
    def adaptive_partition(self):
        """SlimCache Algorithm 1: Adaptive Partitioning"""
        # Phải gom tổng request trước khi kiểm tra ngưỡng < 1000
        total_requests = sum(s['total_requests'] for s in list(self.thread_stats.values()))
        if total_requests < 1000:
            return False
        
        current_hot_percent = self.tier_manager.HOT_MEMORY_PERCENT
        current_hot_rate, current_cold_rate, current_miss_rate = self.get_current_hit_rates()
        current_cost = self.calculate_cost(current_hot_rate, current_cold_rate)
        
        increase_hot_percent = min(current_hot_percent + self.STEP_SIZE_PERCENT, self.MAX_HOT_PERCENT)
        decrease_hot_percent = max(current_hot_percent - self.STEP_SIZE_PERCENT, self.MIN_HOT_PERCENT)
        
        hot_inc, cold_inc, miss_inc = self.estimate_hit_rates_after_adjustment(
            current_hot_percent, increase_hot_percent
        )
        cost_increase = self.calculate_cost(hot_inc, cold_inc)
        
        hot_dec, cold_dec, miss_dec = self.estimate_hit_rates_after_adjustment(
            current_hot_percent, decrease_hot_percent
        )
        cost_decrease = self.calculate_cost(hot_dec, cold_dec)
        
        best_cost = current_cost
        best_percent = current_hot_percent
        best_action = "maintain"
        
        if cost_increase < best_cost:
            best_cost = cost_increase
            best_percent = increase_hot_percent
            best_action = "increase"
        
        if cost_decrease < best_cost:
            best_cost = cost_decrease
            best_percent = decrease_hot_percent
            best_action = "decrease"
        
        if best_action != "maintain":
            self._apply_adjustment(best_percent, best_action, current_cost, best_cost)
            self._reset_stats()
            return True
        
        self._log_decision(current_hot_percent, current_cost, "maintain")
        self._reset_stats()
        return False
    
    def _apply_adjustment(self, new_hot_percent, action, old_cost, new_cost):
        """Apply the new partition boundary"""
        old_hot_percent = self.tier_manager.HOT_MEMORY_PERCENT
        
        self.tier_manager.HOT_MEMORY_PERCENT = new_hot_percent
        self.tier_manager.HOT_MAX_MEMORY_BYTES = int(
            self.tier_manager.TOTAL_REDIS_MEMORY_MB * 1024 * 1024 * (new_hot_percent / 100)
        )
        self.tier_manager.COLD_MAX_MEMORY_BYTES = int(
            self.tier_manager.TOTAL_REDIS_MEMORY_MB * 1024 * 1024 * ((100 - new_hot_percent) / 100)
        )
        
        self.adjustment_history.append({
            'timestamp': datetime.now(),
            'old_percent': old_hot_percent,
            'new_percent': new_hot_percent,
            'action': action,
            'old_cost': old_cost,
            'new_cost': new_cost,
            'improvement': ((old_cost - new_cost) / old_cost * 100) if old_cost > 0 else 0
        })
        
        msg = (f"[ADAPTIVE] {action.upper()} HOT tier: {old_hot_percent}% → {new_hot_percent}% "
               f"(cost: {old_cost:.1f}μs → {new_cost:.1f}μs, improvement: "
               f"{((old_cost - new_cost) / old_cost * 100):.1f}%)")
        print(msg)
        
        if self.log_file:
            self.log_file.write(msg + "\n")
            self.log_file.flush()
    
    def _log_decision(self, hot_percent, cost, action):
        """Log decision to maintain current partition"""
        hot_rate, cold_rate, miss_rate = self.get_current_hit_rates()
        msg = (f"[ADAPTIVE] MAINTAIN HOT tier at {hot_percent}% "
               f"(cost: {cost:.1f}μs, hot:{hot_rate*100:.1f}%, cold:{cold_rate*100:.1f}%, miss:{miss_rate*100:.1f}%)")
        print(msg)
        
        if self.log_file:
            self.log_file.write(msg + "\n")
            self.log_file.flush()
    
    def _reset_stats(self):
        """Reset request stats bằng cách clear số đếm của tất cả threads."""
        for stats in list(self.thread_stats.values()):
            stats['total_requests'] = 0
            stats['hot_hits'] = 0
            stats['cold_hits'] = 0
            stats['misses'] = 0
        self.last_reset = time.time()
    
    def get_statistics(self):
        """Get adaptive partitioning statistics"""
        return {
            'adjustments': len(self.adjustment_history),
            'current_hot_percent': self.tier_manager.HOT_MEMORY_PERCENT,
            'history': self.adjustment_history[-5:] if self.adjustment_history else []
        }