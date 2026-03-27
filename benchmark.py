#!/usr/bin/env python3
import subprocess
import time
import os
import threading
from datetime import datetime
import redis
from pymongo import MongoClient
import sys


class TieredCacheBenchmark:
    def __init__(self):
        self.RECORD_COUNT = 60000
        self.OPERATION_COUNT = 500000
        self.WORKLOAD_TYPE = 'C'
        self.COMPRESSION_ALGORITHM = 'lz4'
        self.HOT_MEMORY_PERCENT = 12
        self.TOTAL_MEMORY_MB = 200
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.ycsb_path = os.path.join(script_dir, 'ycsb', 'bin', 'ycsb.bat')
        self.workload_file = os.path.join(script_dir, 'ycsb', 'workloads', f'workload{self.WORKLOAD_TYPE.lower()}')
        self.mongodb_url = 'mongodb://localhost:27017/ycsb'
        
        self.stats = {
            'load_time': 0,
            'run_time': 0,
            'throughput': 0,
            'avg_latency': 0,
            'wall_clock_start': 0,
            'wall_clock_end': 0,
            'wall_clock_time': 0
        }
    
    def check_redis_running(self):
        """Check if Redis is running and accessible"""
        try:
            r = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
            r.ping()
            return True
        except redis.exceptions.RedisError:
            return False
    
    def check_mongodb_running(self):
        """Check if MongoDB is running and accessible"""
        client = None
        try:
            client = MongoClient(self.mongodb_url, serverSelectionTimeoutMS=2000)
            client.server_info()
            return True
        except Exception:
            return False
        finally:
            if client:
                client.close()
    
    def start_redis_service(self):
        """Start Redis Windows service"""
        try:
            print("  Attempting to start Redis service...")
            result = subprocess.run(
                ['powershell', '-Command', 'Start-Service Redis'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                time.sleep(2)
                return self.check_redis_running()
            else:
                print(f"  Warning: Could not start Redis service (might need admin privileges)")
                return False
        except Exception as e:
            print(f"  Warning: Failed to start Redis: {e}")
            return False
    
    def start_mongodb_service(self):
        """Start MongoDB Windows service"""
        try:
            print("  Attempting to start MongoDB service...")
            # Try with elevated privileges
            cmd = "Start-Process powershell -Verb RunAs -ArgumentList 'Start-Service MongoDB; Start-Sleep 3' -Wait"
            result = subprocess.run(
                ['powershell', '-Command', cmd],
                capture_output=True,
                text=True,
                timeout=15
            )
            time.sleep(3)
            return self.check_mongodb_running()
        except Exception as e:
            print(f"  Warning: Failed to start MongoDB: {e}")
            return False
    
    def check_and_start_services(self):
        """Check if Redis and MongoDB are running, start them if not"""
        print("\n" + "="*70)
        print("CHECKING REQUIRED SERVICES")
        print("="*70)
        
        # Check Redis
        print("\n[1/2] Checking Redis...")
        redis_running = self.check_redis_running()
        if redis_running:
            print("  ✅ Redis is running")
        else:
            print("  ❌ Redis is not running")
            if self.start_redis_service():
                print("  ✅ Redis started successfully")
            else:
                print("  ❌ Failed to start Redis")
                print("\n  Please start Redis manually:")
                print("  - Run PowerShell as Administrator")
                print("  - Execute: Start-Service Redis")
                sys.exit(1)
        
        # Check MongoDB
        print("\n[2/2] Checking MongoDB...")
        mongodb_running = self.check_mongodb_running()
        if mongodb_running:
            print("  ✅ MongoDB is running")
        else:
            print("  ❌ MongoDB is not running")
            if self.start_mongodb_service():
                print("  ✅ MongoDB started successfully")
            else:
                print("  ❌ Failed to start MongoDB")
                print("\n  Please start MongoDB manually:")
                print("  - Run PowerShell as Administrator")
                print("  - Execute: Start-Service MongoDB")
                sys.exit(1)
        
        print("\n✅ All required services are running!")
        print("="*70)
    
    def clear_databases(self):
        print("\nClearing databases...")
        try:
            r = redis.Redis(host='localhost', port=6379)
            r.flushall()
            print("Redis cleared")
        except Exception as e:
            print(f"Redis clear failed: {e}")
        
        client = None
        try:
            client = MongoClient(self.mongodb_url)
            client.drop_database('ycsb')
            print("MongoDB cleared")
        except Exception as e:
            print(f"MongoDB clear failed: {e}")
        finally:
            if client:
                client.close()
    
    def load_data_with_ycsb(self):
        print("\n" + "="*70)
        print("YCSB Load -> MongoDB")
        print("="*70)
        
        env = os.environ.copy()
        env['JAVA_OPTS'] = '-Xmx512M -Xms256M'
        
        cmd = [
            self.ycsb_path,
            'load', 'mongodb',
            '-s',
            '-threads', '4',
            '-P', self.workload_file,
            '-p', f'recordcount={self.RECORD_COUNT}',
            '-p', f'operationcount={self.OPERATION_COUNT}',
            '-p', f'mongodb.url={self.mongodb_url}'
        ]
        
        start = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True, env=env)
        self.stats['load_time'] = time.time() - start
        
        print(result.stdout)
        print(f"\nLoaded {self.RECORD_COUNT} records in {self.stats['load_time']:.2f}s")
    
    def start_tiered_proxy(self):
        from redis_proxy import TieredCacheProxy
        
        print("\n" + "="*70)
        print("Starting Tiered Cache Proxy")
        print("="*70)
        print("Architecture: HOT (uncompressed) -> COLD (compressed)")
        print(f"Memory Limits: HOT {self.HOT_MEMORY_PERCENT}% ({self.TOTAL_MEMORY_MB * self.HOT_MEMORY_PERCENT/100:.1f} MB), COLD {100-self.HOT_MEMORY_PERCENT}% ({self.TOTAL_MEMORY_MB * (100-self.HOT_MEMORY_PERCENT)/100:.1f} MB)")
        print("="*70)
        
        self.proxy = TieredCacheProxy(
            proxy_port=6380,
            compression=self.COMPRESSION_ALGORITHM,
            promotion_interval=30,  # 30 seconds for benchmark
            hot_memory_percent=self.HOT_MEMORY_PERCENT,
            total_memory_mb=self.TOTAL_MEMORY_MB
        )
        proxy_thread = threading.Thread(target=self.proxy.start, daemon=True)
        proxy_thread.start()
        time.sleep(2)
        print("Tiered proxy started on port 6380\n")
    
    def run_ycsb_through_proxy(self):
        print("\n" + "="*70)
        print("YCSB Run -> Tiered Proxy")
        print("="*70)
        
        env = os.environ.copy()
        env['JAVA_OPTS'] = '-Xmx512M -Xms256M'
        
        cmd = [
            self.ycsb_path,
            'run', 'redis',
            '-s',
            '-threads', '4',
            '-P', self.workload_file,
            '-p', f'recordcount={self.RECORD_COUNT}',
            '-p', f'operationcount={self.OPERATION_COUNT}',
            '-p', 'requestdistribution=zipfian',
            '-p', 'zipfianconstant=1.5',
            '-p', 'redis.host=localhost',
            '-p', 'redis.port=6380',
            '-p', 'redis.timeout=20000'  # 20s timeout to handle large workloads
        ]
        
        start = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True, env=env)
        self.stats['run_time'] = time.time() - start
        
        print(result.stdout)
        
        # Parse stats
        for line in result.stdout.split('\n'):
            if '[OVERALL], Throughput(ops/sec)' in line:
                try:
                    self.stats['throughput'] = float(line.split(',')[-1].strip())
                except (ValueError, IndexError):
                    pass
            elif '[READ], AverageLatency(us)' in line:
                try:
                    self.stats['avg_latency'] = float(line.split(',')[-1].strip())
                except (ValueError, IndexError):
                    pass
        
        print(f"\nYCSB completed {self.OPERATION_COUNT} operations")
        if self.stats['throughput'] > 0:
            print(f"  Throughput: {self.stats['throughput']:.2f} ops/sec")
        if self.stats['avg_latency'] > 0:
            print(f"  Avg Latency: {self.stats['avg_latency']:.2f} us")
    
    def analyze_tier_performance(self):
        tier_stats = {'hot_hits': 0, 'cold_hits': 0, 'mongo_hits': 0, 'cold_promotes': 0}
        
        try:
            log_dir = 'result/log'
            if not os.path.exists(log_dir):
                return tier_stats
            
            log_files = [f for f in os.listdir(log_dir) if f.startswith('tiered_log_')]
            if not log_files:
                return tier_stats
            
            latest_log = sorted(log_files)[-1]
            log_path = os.path.join(log_dir, latest_log)
            
            with open(log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if 'HOT tier -> cache hit' in line:
                        tier_stats['hot_hits'] += 1
                    elif 'COLD tier -> cache hit' in line:
                        tier_stats['cold_hits'] += 1
                    elif 'MongoDB -> found' in line:
                        tier_stats['mongo_hits'] += 1
                    elif 'promotion (real-time)' in line or 'promotion (background' in line:
                        tier_stats['cold_promotes'] += 1
            
        except Exception as e:
            print(f"Could not analyze tier performance: {e}")
        
        return tier_stats
    
    def generate_report(self):
        tier_stats = self.analyze_tier_performance()
        total_hits = sum([tier_stats['hot_hits'], tier_stats['cold_hits'], tier_stats['mongo_hits']])
        memory_stats = self.proxy.tier_manager.get_statistics()
        
        tier_section = ""
        if total_hits > 0:
            tier_section = f"""
TIER PERFORMANCE:
  HOT tier:      {tier_stats['hot_hits']:,} hits ({tier_stats['hot_hits']/total_hits*100:.1f}%) - Uncompressed
  COLD tier:     {tier_stats['cold_hits']:,} hits ({tier_stats['cold_hits']/total_hits*100:.1f}%) - Compressed
  MongoDB:       {tier_stats['mongo_hits']:,} hits ({tier_stats['mongo_hits']/total_hits*100:.1f}%)
  
  Redis Hit Rate: {(total_hits - tier_stats['mongo_hits'])/total_hits*100:.2f}%
  Real-time promotions: {tier_stats['cold_promotes']:,}

MEMORY USAGE:
  HOT tier:  {memory_stats['hot_keys']:,} keys, {memory_stats['hot_memory_mb']:.2f} MB ({memory_stats['hot_memory_utilization']:.1f}% of {memory_stats['hot_memory_limit_mb']:.2f} MB limit)
  COLD tier: {memory_stats['cold_keys']:,} keys, {memory_stats['cold_memory_mb']:.2f} MB ({memory_stats['cold_memory_utilization']:.1f}% of {memory_stats['cold_memory_limit_mb']:.2f} MB limit)
  Total:     {memory_stats['total_keys']:,} keys, {memory_stats['total_memory_mb']:.2f} MB
  
  Memory Distribution: HOT {memory_stats['hot_memory_pct']:.1f}%, COLD {memory_stats['cold_memory_pct']:.1f}%
  Evictions: {memory_stats['evictions']:,}
"""
        
        report = f"""
{'='*70}
TIERED CACHE BENCHMARK REPORT
{'='*70}
Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

CONFIGURATION:
  Workload Type: {self.WORKLOAD_TYPE} (YCSB workload{self.WORKLOAD_TYPE.lower()})
  Load: YCSB -> MongoDB ({self.RECORD_COUNT:,} records)
  Run: YCSB -> Tiered Proxy ({self.OPERATION_COUNT:,} ops)
  Distribution: Zipfian (zipfianconstant=1.5)

ARCHITECTURE:
    HOT tier (uncompressed, no TTL)
    COLD tier ({self.COMPRESSION_ALGORITHM.upper()} compressed, no TTL)
    MongoDB (fallback)

MEMORY CONFIGURATION:
  Total Memory Budget: {self.TOTAL_MEMORY_MB} MB
  HOT tier limit:  {self.HOT_MEMORY_PERCENT}% ({self.TOTAL_MEMORY_MB * self.HOT_MEMORY_PERCENT/100:.1f} MB)
  COLD tier limit: {100-self.HOT_MEMORY_PERCENT}% ({self.TOTAL_MEMORY_MB * (100-self.HOT_MEMORY_PERCENT)/100:.1f} MB)

EXECUTION TIME:
  YCSB Load Time:   {self.stats['load_time']:.2f}s
  YCSB Run Time:    {self.stats['run_time']:.2f}s
  YCSB Total:       {self.stats['load_time'] + self.stats['run_time']:.2f}s
  
  Wall-Clock Time:  {self.stats['wall_clock_time']:.2f}s ({self.stats['wall_clock_time']/60:.2f} mins)
  Overhead:         {self.stats['wall_clock_time'] - (self.stats['load_time'] + self.stats['run_time']):.2f}s

PERFORMANCE:
  Throughput:       {self.stats['throughput']:.2f} ops/sec
  Avg Latency:      {self.stats['avg_latency']:.2f} us
{tier_section}
{'='*70}
"""
        print(report)
        
        os.makedirs('result/report', exist_ok=True)
        filename = f'result/report/tiered_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"Report saved: {filename}\n")
    
    def run(self):
        print("\n" + "="*70)
        print("TIERED CACHE BENCHMARK")
        print("="*70)
        
        # Record wall-clock start time
        self.stats['wall_clock_start'] = time.time()
        
        try:
            self.check_and_start_services()
            self.clear_databases()
            self.load_data_with_ycsb()
            self.start_tiered_proxy()
            self.run_ycsb_through_proxy()
            
            print("\nWaiting for final operations to complete...")
            time.sleep(5)
            
            # Stop proxy BEFORE generating report to ensure clean stats
            if hasattr(self, 'proxy'):
                self.proxy.stop()
                # Give worker threads time to fully stop before closing log file
                time.sleep(2)
            
            # Record wall-clock end time
            self.stats['wall_clock_end'] = time.time()
            self.stats['wall_clock_time'] = self.stats['wall_clock_end'] - self.stats['wall_clock_start']
            
            print("\nGenerating report...")
            self.generate_report()
            
            print(f"\n{'='*70}")
            print(f"Benchmark completed!")
            print(f"Total wall-clock time: {self.stats['wall_clock_time']:.2f}s ({self.stats['wall_clock_time']/60:.2f} mins)")
            print(f"{'='*70}\n")
        except Exception as e:
            print(f"\nERROR: {e}")
            import traceback
            traceback.print_exc()
            
            if hasattr(self, 'proxy'):
                try:
                    self.proxy.stop()
                except Exception as stop_error:
                    print(f"Failed to stop proxy cleanly: {stop_error}")


if __name__ == '__main__':
    benchmark = TieredCacheBenchmark()
    benchmark.run()
