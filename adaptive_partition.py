import time
import threading
from datetime import datetime

class AdaptivePartitioner:
    def __init__(self, tier_manager, log_file=None):
        self.tier_manager = tier_manager
        self.log_file = log_file

        self.C_HOT = 500    
        self.C_COLD = 700   
        self.C_MISS = 5400  

        self.STEP_SIZE_PERCENT = 1
        self.MIN_HOT_PERCENT = 5
        self.MAX_HOT_PERCENT = 30

        self._init_lock = threading.Lock()
        self.thread_stats = {} 
        
        # Bộ đếm gom số liệu của các luồng đã chết (chống tụt số đếm)
        self.retired_stats = {
            'total_requests': 0,
            'hot_hits': 0,
            'cold_hits': 0,
            'misses': 0
        }
        
        self.snapshot = {
            'total': 0,
            'hot': 0,
            'cold': 0,
            'miss': 0
        }
        self.last_reset = time.time()
        self.adjustment_history = []

    def _get_local_stats(self):
        """khởi tạo bộ đếm riêng cho thread hiện tại."""
        tid = threading.get_ident()
        stats = self.thread_stats.get(tid)
        
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
        """Ghi nhận siêu tốc, lock-free."""
        stats = self._get_local_stats()
        
        stats['total_requests'] += 1
        if tier == 'hot':
            stats['hot_hits'] += 1
        elif tier == 'cold':
            stats['cold_hits'] += 1
        else:
            stats['misses'] += 1
            
    def _get_absolute_totals(self):
        """
        Lấy tổng tích lũy, đồng thời cleanup các thread đã chết.
        """
        # 1. Lấy danh sách ID của các luồng ĐANG SỐNG trong hệ thống
        active_tids = {t.ident for t in threading.enumerate()}
        
        with self._init_lock:
            # 2. Tìm các luồng đã chết (có trong dict nhưng không có trong active_tids)
            dead_tids = [tid for tid in self.thread_stats.keys() if tid not in active_tids]
            
            # 3. Cộng dồn di sản của chúng vào retired_stats và xóa khỏi bộ nhớ
            for tid in dead_tids:
                dead_stats = self.thread_stats.pop(tid)
                self.retired_stats['total_requests'] += dead_stats['total_requests']
                self.retired_stats['hot_hits'] += dead_stats['hot_hits']
                self.retired_stats['cold_hits'] += dead_stats['cold_hits']
                self.retired_stats['misses'] += dead_stats['misses']
        
        # 4. Bắt đầu tính tổng: Khởi tạo bằng số liệu của các luồng đã nghỉ
        total = self.retired_stats['total_requests']
        hot = self.retired_stats['hot_hits']
        cold = self.retired_stats['cold_hits']
        miss = self.retired_stats['misses']
        
        # 5. Cộng thêm số liệu của các luồng đang chạy
        # (Dùng list() để tránh lỗi dictionary changed size during iteration)
        for stats in list(self.thread_stats.values()):
            total += stats['total_requests']
            hot += stats['hot_hits']
            cold += stats['cold_hits']
            miss += stats['misses']
            
        return total, hot, cold, miss
    
    def calculate_cost(self, hot_hit_rate, cold_hit_rate):
        miss_rate = 1.0 - hot_hit_rate - cold_hit_rate
        return (hot_hit_rate * self.C_HOT + 
                cold_hit_rate * self.C_COLD + 
                miss_rate * self.C_MISS)
    
    def get_current_hit_rates(self):
        """Tính hit rates dựa trên Delta (Hiện tại - Snapshot)"""
        abs_total, abs_hot, abs_cold, abs_miss = self._get_absolute_totals()
        
        delta_total = abs_total - self.snapshot['total']
        delta_hot = abs_hot - self.snapshot['hot']
        delta_cold = abs_cold - self.snapshot['cold']
        delta_miss = abs_miss - self.snapshot['miss']
            
        if delta_total <= 0:
            return 0.0, 0.0, 0.0
            
        return (delta_hot / delta_total), (delta_cold / delta_total), (delta_miss / delta_total)
    
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
        # Tính delta total để xem chu kỳ này đã nhận đủ 1000 requests chưa
        abs_total, _, _, _ = self._get_absolute_totals()
        delta_total = abs_total - self.snapshot['total']
        
        if delta_total < 1000:
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
        hot_rate, cold_rate, miss_rate = self.get_current_hit_rates()
        msg = (f"[ADAPTIVE] MAINTAIN HOT tier at {hot_percent}% "
               f"(cost: {cost:.1f}μs, hot:{hot_rate*100:.1f}%, cold:{cold_rate*100:.1f}%, miss:{miss_rate*100:.1f}%)")
        print(msg)
        
        if self.log_file:
            self.log_file.write(msg + "\n")
            self.log_file.flush()
    
    def _reset_stats(self):
        """
        Thay vì zero-out các dict của thread khác (gây lỗi TOCTOU),
        chỉ cần chốt sổ (snapshot) số liệu tuyệt đối ở thời điểm hiện tại.
        """
        abs_total, abs_hot, abs_cold, abs_miss = self._get_absolute_totals()
        
        self.snapshot['total'] = abs_total
        self.snapshot['hot'] = abs_hot
        self.snapshot['cold'] = abs_cold
        self.snapshot['miss'] = abs_miss
        
        self.last_reset = time.time()
    
    def get_statistics(self):
        return {
            'adjustments': len(self.adjustment_history),
            'current_hot_percent': self.tier_manager.HOT_MEMORY_PERCENT,
            'history': self.adjustment_history[-5:] if self.adjustment_history else []
        }