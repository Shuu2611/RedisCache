#!/usr/bin/env python3
import socket
import threading
import redis
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import hiredis
import os
from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
import traceback

from tier_manager import TierManager
from cache_compression import create_compressor, estimate_size
from promotion import PromotionWorker
from adaptive_partition import AdaptivePartitioner


# LRU cache thay thế unbounded dict
class LRUSizeCache:
    """Thread-safe LRU cache với giới hạn kích thước."""
    def __init__(self, maxsize=10000):
        self._cache = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def set(self, key, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def delete(self, key):
        with self._lock:
            self._cache.pop(key, None)


class TieredCacheProxy:

    #Buffer log, chỉ flush định kỳ thay vì mỗi request
    LOG_FLUSH_INTERVAL = 2.0  # giây

    def __init__(self, proxy_port=6380, redis_host='localhost', redis_port=6379,
                 mongodb_url='mongodb://localhost:27017/ycsb',
                 compression='lz4', promotion_interval=60,
                 hot_memory_percent=5, total_memory_mb=100):
        self.proxy_port = proxy_port
        self.redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=False)
        self.mongo_client = MongoClient(mongodb_url)
        self.collection = self.mongo_client.ycsb.usertable
        self.running = False

        os.makedirs('result/log', exist_ok=True)
        log_filename = f"result/log/tiered_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        self.log_file = open(log_filename, 'w', encoding='utf-8', buffering=8192)
        self._last_flush = time.monotonic()
        self._log_lock = threading.Lock()

        self.tier_manager = TierManager(hot_memory_percent=hot_memory_percent, total_memory_mb=total_memory_mb)
        self.compressor = create_compressor(compression)
        self.adaptive_partitioner = AdaptivePartitioner(self.tier_manager, log_file=self.log_file)
        self.promotion_worker = PromotionWorker(
            self.tier_manager, self.redis_client, self.compressor,
            interval=promotion_interval, log_file=self.log_file,
            adaptive_partitioner=self.adaptive_partitioner
        )

        self.executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix='proxy')

        #Dùng LRU cache giới hạn thay vì unbounded dict
        self.size_cache = LRUSizeCache(maxsize=10000)

        print(f"Tiered cache proxy initialized:")
        print(f"  - Compression: {compression}")
        print(f"  - Promotion interval: {promotion_interval}s")
        print(f"  - HOT memory limit: {hot_memory_percent}% ({self.tier_manager.HOT_MAX_MEMORY_BYTES / (1024*1024):.2f} MB)")
        print(f"  - COLD memory limit: {100-hot_memory_percent}% ({self.tier_manager.COLD_MAX_MEMORY_BYTES / (1024*1024):.2f} MB)")
        print(f"  - Log: {log_filename}")

    def _write_log(self, msg: str):
        """Thread-safe log write với flush định kỳ."""
        with self._log_lock:
            self.log_file.write(msg)
            now = time.monotonic()
            if now - self._last_flush >= self.LOG_FLUSH_INTERVAL:
                self.log_file.flush()
                self._last_flush = now

    def start(self):
        self.running = True
        self.promotion_worker.start()

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('localhost', self.proxy_port))
        self.server.listen(5)
        self.server.settimeout(1.0)
        print(f"Tiered proxy started on port {self.proxy_port}")

        while self.running:
            try:
                client, addr = self.server.accept()
                self.executor.submit(self.handle_client, client)
            except socket.timeout:
                continue
            except OSError as e:
                if self.running:
                    print(f"Server accept error: {e}")
                break

    def stop(self):
        print("Stopping tiered cache proxy...")
        self.running = False
        self.promotion_worker.stop()

        try:
            if hasattr(self, 'server'):
                self.server.close()
        except OSError as e:
            print(f"Server close error: {e}")

        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)

        self._print_final_stats()

        if hasattr(self, 'log_file'):
            self.log_file.flush()
            self.log_file.close()

        if hasattr(self, 'mongo_client'):
            try:
                self.mongo_client.close()
            except Exception as e:
                print(f"Mongo client close error: {e}")

        print("Tiered cache proxy stopped")

    def handle_client(self, client):
        reader = hiredis.Reader()
        try:
            while self.running:
                data = client.recv(4096)
                if not data:
                    break

                reader.feed(data)
                while True:
                    command = reader.gets()
                    if command is False or command is None:
                        break
                    response = self.process_command(command)
                    client.sendall(response)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            print(f"Client handler error: {e}")
        finally:
            client.close()

    def process_command(self, command):
        if not command or not isinstance(command, list):
            return self.encode_error("ERR empty command")

        def _to_str(x):
            return x.decode('utf-8') if isinstance(x, bytes) else str(x)

        cmd = _to_str(command[0]).upper()

        try:
            if cmd == 'PING':
                return self.encode_simple_string("PONG")

            elif cmd == 'HGETALL':
                if len(command) < 2:
                    return self.encode_error("ERR wrong number of arguments")
                key = _to_str(command[1])
                return self._handle_read(key)

            elif cmd == 'HSET':
                if len(command) < 4:
                    return self.encode_error("ERR wrong number of arguments")
                key = _to_str(command[1])
                field = _to_str(command[2])
                value = _to_str(command[3])
                return self._handle_write(key, field, value)
            elif cmd == 'HMSET':
                # HMSET key field1 value1 field2 value2 ...
                if len(command) < 4 or (len(command) - 2) % 2 != 0:
                    return self.encode_error("ERR wrong number of arguments for HMSET")
    
                key = _to_str(command[1])
                fields = {}
                for i in range(2, len(command), 2):
                    fields[_to_str(command[i])] = _to_str(command[i + 1])
    
                    return self._handle_write_multi(key, fields)
            elif cmd == 'DEL':
                if len(command) < 2:
                    return self.encode_error("ERR wrong number of arguments")
                keys = [_to_str(command[i]) for i in range(1, len(command))]
                return self._handle_delete(keys)

            elif cmd == 'EXISTS':
                if len(command) < 2:
                    return self.encode_error("ERR wrong number of arguments")
                key = _to_str(command[1])
                result = self._key_exists(key)
                return self.encode_integer(1 if result else 0)

            else:
                return self.encode_error(f"ERR unknown command '{cmd}'")

        except Exception as e:
            tb = traceback.format_exc()
            try:
                self._write_log(f"ERROR processing command {cmd}: {e}\n{tb}\n")
            except Exception:
                pass
            return self.encode_error(f"ERR {str(e)}")

    def _estimate_size(self, hash_data, key=None):
        """FIX #8: Dùng estimate_size thực (json bytes) thay vì heuristic cố định."""
        if key:
            cached = self.size_cache.get(key)
            if cached is not None:
                return cached

        # Normalize keys/values to strings if they are bytes so json.dumps won't fail
        if isinstance(hash_data, dict):
            normalized = {}
            for k, v in hash_data.items():
                nk = k.decode('utf-8') if isinstance(k, bytes) else str(k)
                nv = v.decode('utf-8') if isinstance(v, bytes) else str(v)
                normalized[nk] = nv
        else:
            normalized = hash_data

        # Dùng hàm estimate_size từ cache_compression.py (json-based, chính xác hơn)
        size = estimate_size(normalized)

        if key:
            self.size_cache.set(key, size)
        return size

    def _check_and_evict_if_needed(self, tier):
        is_over, lru_key = self.tier_manager.check_memory_limit(tier)

        if is_over and lru_key:
            # normalize lru_key to str
            if isinstance(lru_key, bytes):
                lru_key = lru_key.decode('utf-8')
            else:
                lru_key = str(lru_key)

            if tier == 'hot':
                hot_key = f"data:hot:{lru_key}"
                hash_data = self.redis_client.hgetall(hot_key)

                if hash_data:
                    hash_dict = {
                        k.decode('utf-8') if isinstance(k, bytes) else k:
                        v.decode('utf-8') if isinstance(v, bytes) else str(v)
                        for k, v in hash_data.items()
                    }
                    compressed = self.compressor.compress(hash_dict)
                    cold_key = f"data:cold:{lru_key}"

                    # FIX #4: Dùng pipeline để atomic SET cold + DEL hot
                    pipe = self.redis_client.pipeline()
                    pipe.set(cold_key, compressed)
                    pipe.delete(hot_key)
                    pipe.execute()

                    new_size = len(compressed)
                    self.tier_manager.set_key_size(lru_key, new_size)
                    self.tier_manager.update_tier(lru_key, 'cold')
                    self.size_cache.delete(lru_key)

                    self._write_log(f"{lru_key} -> evicted from HOT (memory limit) -> compressed to COLD\n")
            else:
                cold_key = f"data:cold:{lru_key}"
                self.redis_client.delete(cold_key)
                self.tier_manager.remove_key(lru_key)
                self.size_cache.delete(lru_key)

                self._write_log(f"{lru_key} -> evicted from COLD (memory limit) -> deleted\n")
            return True
        return False

    def _handle_read(self, key):
        hot_key = f"data:hot:{key}"
        cold_key = f"data:cold:{key}"

        # Pipeline để giảm round-trip
        pipe = self.redis_client.pipeline()
        pipe.hgetall(hot_key)
        pipe.get(cold_key)
        try:
            value, compressed = pipe.execute()
        except Exception as e:
            tb = traceback.format_exc()
            self._write_log(f"ERROR during redis pipeline.execute in _handle_read for key={hot_key}: {e}\n{tb}\n")
            raise

        if value:
            size = self._estimate_size(value, key)
            self.tier_manager.record_access(key, size)
            self.adaptive_partitioner.record_request('hot')
            self._write_log(f"{key} -> HOT tier -> cache hit\n")
            return self._encode_hash_response(value)

        if compressed:
            hash_data = self.compressor.decompress(compressed)
            size = self._estimate_size(hash_data)
            self.tier_manager.record_access(key, size)
            self.adaptive_partitioner.record_request('cold')

            score = self.tier_manager.calculate_score(key)
            if score >= self.tier_manager.SCORE_THRESHOLD:
                should_promote, target_tier, reason = self.tier_manager.should_promote(key, 'cold')

                if should_promote:
                    self._check_and_evict_if_needed('hot')
                    promoted = self._promote_immediately(key, hash_data)
                    if promoted:
                        self._write_log(f"{key} -> COLD tier -> cache hit -> promotion (real-time)\n")
                    else:
                        self._write_log(f"{key} -> COLD tier -> cache hit (promotion failed)\n")
                else:
                    self._write_log(f"{key} -> COLD tier -> cache hit (promotion skipped - {reason})\n")
            else:
                self._write_log(f"{key} -> COLD tier -> cache hit (decompressed)\n")

            result = []
            for k, v in hash_data.items():
                result.append(k.encode('utf-8') if isinstance(k, str) else k)
                result.append(str(v).encode('utf-8'))
            return self.encode_array(result)

        # Cache miss → MongoDB
        doc = self.collection.find_one({'_id': key})
        if doc:
            hash_data = {}
            for k, v in doc.items():
                if k == '_id':
                    continue
                hash_data[k] = v.decode('utf-8') if isinstance(v, bytes) else str(v)

            self._check_and_evict_if_needed('cold')

            compressed = self.compressor.compress(hash_data)
            self.redis_client.set(cold_key, compressed)

            compressed_size = len(compressed)
            self.tier_manager.record_access(key, compressed_size)
            self.tier_manager.update_tier(key, 'cold')
            self.adaptive_partitioner.record_request('miss')

            self._write_log(f"{key} -> cache miss -> MongoDB -> found -> update redis (COLD tier, compressed)\n")

            result = []
            for k, v in hash_data.items():
                result.append(k.encode('utf-8'))
                result.append(str(v).encode('utf-8'))
            return self.encode_array(result)

        return self.encode_array([])

    def _promote_immediately(self, key, hash_data) -> bool:
        """
        FIX #4: Trả về bool để caller biết thành công hay không.
        FIX #4: Dùng pipeline atomic: write HOT rồi DEL COLD trong một batch.
        """
        try:
            size = self._estimate_size(hash_data)

            pipe = self.redis_client.pipeline()
            hot_key = f"data:hot:{key}"
            cold_key = f"data:cold:{key}"
            for field, value in hash_data.items():
                field_str = field.decode('utf-8') if isinstance(field, bytes) else str(field)
                pipe.hset(hot_key, field_str, str(value))
            pipe.delete(cold_key)
            pipe.execute()

            # Cập nhật TierManager SAU khi Redis thành công
            self.tier_manager.set_key_size(key, size)
            self.tier_manager.update_tier(key, 'hot')
            self.size_cache.delete(key)  # invalidate cache kích thước cũ
            return True
        except Exception as e:
            tb = traceback.format_exc()
            print(f"Immediate promotion error for {key}: {e}")
            try:
                self._write_log(f"Immediate promotion error for {key}: {e}\n{tb}\n")
            except Exception:
                pass
            return False

    def _handle_write(self, key, field, value):
        """
        Tách Network I/O và CPU ra khỏi Global Lock của TierManager.
        """
        try:
            # ==================================================
            # BƯỚC 1: SNAPSHOT (Lock-free hoặc Lock cực ngắn)
            # ==================================================
            # Giả định classify_tier hoặc get_tier đã tự xử lý thread-safe nội bộ
            current_tier = self.tier_manager.get_tier(key)
            
            new_size = 0
            final_tier = current_tier

            # ==================================================
            # BƯỚC 2: REDIS I/O & NÉN DATA (KHÔNG GIỮ LOCK)
            # ==================================================
            if current_tier == 'hot':
                # Ghi thẳng xuống Redis
                self.redis_client.hset(f"data:hot:{key}", field, str(value))
                
                # Ước lượng dung lượng tăng thêm (tránh gọi HGETALL đếm lại gây tốn RTT)
                # Kích thước cũ + byte của field mới + byte của value mới
                added_bytes = len(str(field).encode('utf-8')) + len(str(value).encode('utf-8'))
                
                # Lấy size hiện tại an toàn
                current_size = self.tier_manager.key_stats.get(key, {}).get('size', 0)
                new_size = current_size + added_bytes
                
            elif current_tier == 'cold':
                compressed_data = self.redis_client.get(f"data:cold:{key}")
                if compressed_data:
                    # Tác vụ nặng CPU: Giải nén & Nén lại (Tuyệt đối không giữ lock!)
                    hash_data = self.compressor.decompress(compressed_data)
                    hash_data[field] = str(value)
                    new_compressed = self.compressor.compress(hash_data)
                    
                    # Network I/O
                    self.redis_client.set(f"data:cold:{key}", new_compressed)
                    new_size = len(new_compressed)
                else:
                    # Fallback: Key bị miss hoặc đã bị xóa ở COLD, tạo mới trên HOT
                    self.redis_client.hset(f"data:hot:{key}", field, str(value))
                    final_tier = 'hot'
                    new_size = len(str(field).encode('utf-8')) + len(str(value).encode('utf-8'))

            # ==================================================
            # BƯỚC 3: CẬP NHẬT TRẠNG THÁI (Giao phó cho TierManager tự lock)
            # ==================================================
            if final_tier != current_tier:
                self.tier_manager.update_tier(key, final_tier)
                
            self.tier_manager.set_key_size(key, new_size)
            self.tier_manager.record_access(key, new_size)
            
            # Kích hoạt dọn dẹp nếu dung lượng vượt ngưỡng
            self._check_and_evict_if_needed(final_tier)
            return self.encode_integer(1)

        except Exception as e:
            print(f"Write error on {key}: {e}")

    def _handle_write_multi(self, key, fields: dict):
        """Xử lý HMSET — ghi nhiều field/value vào một key."""
        # Write MongoDB trước (source of truth)
        try:
            self.collection.update_one(
                {'_id': key},
                {'$set': fields},
                upsert=True
            )
        except PyMongoError as e:
            return self.encode_error(f"ERR mongodb write failed: {str(e)}")

        current_tier = self.tier_manager.get_tier(key)

        if current_tier == 'hot':
            # HSET nhiều field trong 1 pipeline
            pipe = self.redis_client.pipeline()
            for f, v in fields.items():
                pipe.hset(f"data:hot:{key}", f, str(v))
            pipe.execute()
            cached_size = self.size_cache.get(key) or 0
            added = sum(len(f.encode()) + len(str(v).encode()) for f, v in fields.items())
            new_size = cached_size + added

        else:  # cold
            compressed_data = self.redis_client.get(f"data:cold:{key}")
            if compressed_data:
                hash_data = self.compressor.decompress(compressed_data)
                hash_data.update(fields)
            else:
                hash_data = dict(fields)
            new_compressed = self.compressor.compress(hash_data)
            self.redis_client.set(f"data:cold:{key}", new_compressed)
            new_size = len(new_compressed)

        self.size_cache.delete(key)
        self.tier_manager.set_key_size(key, new_size)
        self.tier_manager.record_access(key, new_size)
        self._check_and_evict_if_needed(current_tier)

        # HMSET trả về "+OK" theo RESP protocol
        return self.encode_simple_string("OK")
    def _handle_delete(self, keys):
        count = 0
        for key in keys:
            # FIX #11: Pipeline 2 lệnh DEL
            pipe = self.redis_client.pipeline()
            pipe.delete(f"data:hot:{key}")
            pipe.delete(f"data:cold:{key}")
            results = pipe.execute()

            if any(results):
                count += 1
                self.tier_manager.remove_key(key)
                self.size_cache.delete(key)
        return self.encode_integer(count)

    def _key_exists(self, key):
        # FIX #11: Pipeline thay vì 2 round-trip riêng lẻ
        pipe = self.redis_client.pipeline()
        pipe.exists(f"data:hot:{key}")
        pipe.exists(f"data:cold:{key}")
        results = pipe.execute()
        return any(results)

    def _encode_hash_response(self, hash_data):
        result = []
        for k, v in hash_data.items():
            result.append(k)
            result.append(v)
        return self.encode_array(result)

    def _print_final_stats(self):
        stats = self.tier_manager.get_statistics()
        print("\n" + "="*50)
        print("FINAL STATS")
        print(f"  Total keys   : {stats['total_keys']}")
        print(f"  HOT keys     : {stats['hot_keys']} ({stats['hot_pct']:.1f}%)")
        print(f"  COLD keys    : {stats['cold_keys']} ({stats['cold_pct']:.1f}%)")
        print(f"  HOT memory   : {stats['hot_memory_mb']:.2f} MB / {stats['hot_memory_limit_mb']:.2f} MB")
        print(f"  COLD memory  : {stats['cold_memory_mb']:.2f} MB / {stats['cold_memory_limit_mb']:.2f} MB")
        print(f"  Promotions   : {stats['promotions']}")
        print(f"  Demotions    : {stats['demotions']}")
        print(f"  Evictions    : {stats['evictions']}")
        print("="*50)

    def encode_simple_string(self, s):
        return f"+{s}\r\n".encode('utf-8')

    def encode_error(self, err):
        return f"-{err}\r\n".encode('utf-8')

    def encode_integer(self, i):
        return f":{i}\r\n".encode('utf-8')

    def encode_bulk_string(self, s):
        if s is None:
            return b"$-1\r\n"
        if isinstance(s, str):
            s = s.encode('utf-8')
        return f"${len(s)}\r\n".encode('utf-8') + s + b"\r\n"

    def encode_array(self, arr):
        if not arr:
            return b"*0\r\n"
        result = f"*{len(arr)}\r\n".encode('utf-8')
        for item in arr:
            result += self.encode_bulk_string(item)
        return result


if __name__ == '__main__':
    proxy = TieredCacheProxy(
        proxy_port=6380,
        compression='lz4',
        promotion_interval=30
    )
    try:
        proxy.start()
    except KeyboardInterrupt:
        proxy.stop()
