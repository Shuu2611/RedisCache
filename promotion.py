#!/usr/bin/env python3
import threading
import time
from datetime import datetime


class PromotionWorker:
    
    BATCH_SIZE = 10  
    
    def __init__(self, tier_manager, redis_client, compressor, interval=60, log_file=None, adaptive_partitioner=None):
        self.tier_manager = tier_manager
        self.redis_client = redis_client
        self.compressor = compressor
        self.interval = interval
        self.log_file = log_file
        self.adaptive_partitioner = adaptive_partitioner
        self.running = False
        self.thread = None
        
        self.stats = {
            'runs': 0,
            'keys_promoted': 0,
            'keys_demoted': 0,
            'keys_compressed': 0,
            'keys_decompressed': 0,
            'bytes_saved': 0,
            'batches_executed': 0
        }
    
    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()
        print(f"Promotion worker started (interval: {self.interval}s)")
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        print("Promotion worker stopped")
    
    def _worker_loop(self):
        while self.running:
            try:
                self._process_tier_changes()
                self.stats['runs'] += 1
                
                if self.adaptive_partitioner and self.stats['runs'] % 3 == 0:
                    self.adaptive_partitioner.adaptive_partition()
            except Exception as e:
                print(f"Promotion worker error: {e}")
            time.sleep(self.interval)
    
    def _process_tier_changes(self):
        all_keys = self.tier_manager.get_all_keys_snapshot()
        
        # Collect keys for batch processing
        keys_to_promote = []
        keys_to_demote = []
        
        for key in all_keys:
            current_tier = self.tier_manager.get_tier(key)
            should_promote, new_tier, reason = self.tier_manager.should_promote(key, current_tier)
            if should_promote:
                keys_to_promote.append((key, current_tier, new_tier))
                continue
            
            should_demote, new_tier = self.tier_manager.should_demote(key, current_tier)
            if should_demote:
                keys_to_demote.append((key, current_tier, new_tier))
        
        # Process promotions in batches
        promotions = self._process_batch_operations(keys_to_promote, is_promotion=True)
        
        # Process demotions in batches
        demotions = self._process_batch_operations(keys_to_demote, is_promotion=False)
        
        if promotions > 0 or demotions > 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"Promotions: {promotions}, Demotions: {demotions}")
    
    def _process_batch_operations(self, keys_list, is_promotion=True):
        """Process keys in batches of BATCH_SIZE"""
        if not keys_list:
            return 0
        
        total_processed = 0
        
        # Process in batches
        for i in range(0, len(keys_list), self.BATCH_SIZE):
            batch = keys_list[i:i + self.BATCH_SIZE]
            
            if is_promotion:
                count = self._promote_batch(batch)
            else:
                count = self._demote_batch(batch)
            
            total_processed += count
            self.stats['batches_executed'] += 1
        
        return total_processed
    
    def _promote_batch(self, keys_batch):
        """Promote nhiều keys trong một pipeline.
        [IMPROVE] Phase đọc (GET cold data) cũng được pipeline,
        thay vì N round-trip tuần tự trước khi build write pipeline.
        Với batch 10 keys: trước = 10 GET + 1 pipeline write.
        Sau = 1 pipeline GET + 1 pipeline write.
        """
        if not keys_batch:
            return 0

        try:
            # Phase 1: Pipeline toàn bộ GET để đọc COLD data
            get_pipe = self.redis_client.pipeline()
            promote_keys = []
            for key, from_tier, to_tier in keys_batch:
                if from_tier == 'cold' and to_tier == 'hot':
                    get_pipe.get(f"data:cold:{key}")
                    promote_keys.append((key, to_tier))

            if not promote_keys:
                return 0

            compressed_list = get_pipe.execute()

            # Phase 2: Decompress rồi build write pipeline
            write_pipe = self.redis_client.pipeline()
            valid_keys = []

            for (key, to_tier), compressed in zip(promote_keys, compressed_list):
                if not compressed:
                    continue
                hash_data = self.compressor.decompress(compressed)
                for field, value in hash_data.items():
                    write_pipe.hset(f"data:hot:{key}", field, str(value))
                write_pipe.delete(f"data:cold:{key}")
                hot_size = sum(
                    len(str(field).encode('utf-8')) + len(str(value).encode('utf-8'))
                    for field, value in hash_data.items()
                )
                valid_keys.append((key, to_tier, hot_size))

            # Phase 3: Thực thi write pipeline một lần
            if valid_keys:
                write_pipe.execute()

                for key, to_tier, hot_size in valid_keys:
                    self.tier_manager.set_key_size(key, hot_size)
                    self.tier_manager.update_tier(key, to_tier)
                    self.stats['keys_promoted'] += 1
                    self.stats['keys_decompressed'] += 1

                    if self.log_file and not self.log_file.closed:
                        self.log_file.write(f"{key} -> promotion (background: COLD->HOT, pipelined)\n")

                if self.log_file and not self.log_file.closed:
                    self.log_file.flush()

            return len(valid_keys)

        except Exception as e:
            if "closed file" not in str(e):
                print(f"Batch promotion error ({len(keys_batch)} keys): {e}")
            return 0
    
    def _demote_batch(self, keys_batch):
        """Demote multiple keys in one pipeline"""
        if not keys_batch:
            return 0
        
        try:
            pipe = self.redis_client.pipeline()
            valid_keys = []
            compressed_sizes = {}
            
            # Build pipeline for all keys in batch
            for key, from_tier, to_tier in keys_batch:
                if from_tier == 'hot' and to_tier == 'cold':
                    source_key = f"data:hot:{key}"
                    hash_data = self.redis_client.hgetall(source_key)
                    
                    if hash_data:
                        hash_dict = {k.decode('utf-8'): v.decode('utf-8') for k, v in hash_data.items()}
                        compressed = self.compressor.compress(hash_dict)
                        
                        pipe.set(f"data:cold:{key}", compressed)
                        pipe.delete(source_key)
                        
                        original_size = sum(len(k) + len(v) for k, v in hash_data.items())
                        compressed_size = len(compressed)
                        valid_keys.append((key, to_tier, compressed_size, original_size - compressed_size))
                        compressed_sizes[key] = compressed_size
            
            # Execute entire batch at once
            if valid_keys:
                pipe.execute()
                
                # Update tier manager and stats
                for key, to_tier, compressed_size, bytes_saved in valid_keys:
                    self.tier_manager.set_key_size(key, compressed_size)
                    self.tier_manager.update_tier(key, to_tier)
                    self.stats['keys_demoted'] += 1
                    self.stats['keys_compressed'] += 1
                    self.stats['bytes_saved'] += bytes_saved
                    
                    if self.log_file and not self.log_file.closed:
                        comp_size = compressed_sizes.get(key, 0)
                        self.log_file.write(f"{key} -> demotion (background worker: HOT -> COLD, compressed, {comp_size} bytes, batched)\n")
                
                if self.log_file and not self.log_file.closed:
                    self.log_file.flush()
            
            return len(valid_keys)
            
        except Exception as e:
            if "closed file" not in str(e):
                print(f"Batch demotion error ({len(keys_batch)} keys): {e}")
            return 0
    
    def get_statistics(self):
        return {
            'runs': self.stats['runs'],
            'keys_promoted': self.stats['keys_promoted'],
            'keys_demoted': self.stats['keys_demoted'],
            'keys_compressed': self.stats['keys_compressed'],
            'keys_decompressed': self.stats['keys_decompressed'],
            'bytes_saved': self.stats['bytes_saved'],
            'bytes_saved_mb': self.stats['bytes_saved'] / (1024 * 1024),
            'batches_executed': self.stats['batches_executed'],
            'avg_keys_per_batch': self.stats['keys_promoted'] / max(1, self.stats['batches_executed'])
        }