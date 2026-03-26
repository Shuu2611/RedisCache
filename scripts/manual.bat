@echo off
REM Manual commands for Cache-Aside pattern with YCSB, Redis, and MongoDB
REM You can run these commands step-by-step to understand the process

echo ========================================
echo CACHE-ASIDE PATTERN - MANUAL COMMANDS
echo ========================================
echo.
echo These are the basic YCSB commands for cache-aside pattern:
echo.

REM Set paths (relative to project root)
cd /d "%~dp0.."
set YCSB_HOME=ycsb
set WORKLOAD=ycsb\workloads\workloadc

echo ----------------------------------------
echo STEP 1: Clear Redis cache
echo ----------------------------------------
echo Command:
echo redis-cli FLUSHALL
echo.
pause

echo ----------------------------------------
echo STEP 2: Clear MongoDB
echo ----------------------------------------
echo Command:
echo mongosh --eval "db.getSiblingDB('ycsb').dropDatabase()"
echo.
pause

echo ----------------------------------------
echo STEP 3: Load 1000 records into MongoDB
echo ----------------------------------------
echo Command:
call echo %YCSB_HOME%\bin\ycsb.bat load mongodb -s ^
     -P %WORKLOAD% ^
     -p mongodb.url=mongodb://localhost:27017/ycsb
echo.
echo Executing...
%YCSB_HOME%\bin\ycsb.bat load mongodb -s -P %WORKLOAD% -p recordcount=1000 -p mongodb.url=mongodb://localhost:27017/ycsb
echo.
pause

echo ----------------------------------------
echo STEP 4: Run workload against Redis (Cold Cache)
echo ----------------------------------------
echo This will result in cache MISSES since Redis is empty
echo Command:
call echo %YCSB_HOME%\bin\ycsb.bat run redis -s ^
     -P %WORKLOAD% ^
     -p redis.host=localhost ^
     -p redis.port=6379
echo.
echo Executing...
%YCSB_HOME%\bin\ycsb.bat run redis -s -P %WORKLOAD% -p operationcount=5000 -p requestdistribution=zipfian -p redis.host=localhost -p redis.port=6379 > cold_cache_results.txt
type cold_cache_results.txt
echo.
echo Results saved to: cold_cache_results.txt
pause

echo ----------------------------------------
echo STEP 5: Populate Redis from MongoDB
echo ----------------------------------------
echo In a real cache-aside, this happens automatically on cache miss
echo For this demo, we'll manually populate Redis from MongoDB
echo.
echo You can use a script or manually copy data:
echo   mongosh ycsb --eval "db.usertable.find().forEach(function(doc) { print(doc._id); })"
echo.
pause

echo ----------------------------------------
echo STEP 6: Run workload against Redis (Warm Cache)
echo ----------------------------------------
echo Now Redis has data, so we should see cache HITS
echo Command:
call echo %YCSB_HOME%\bin\ycsb.bat run redis -s ^
     -P %WORKLOAD% ^
     -p redis.host=localhost ^
     -p redis.port=6379
echo.
echo Executing...
%YCSB_HOME%\bin\ycsb.bat run redis -s -P %WORKLOAD% -p operationcount=5000 -p requestdistribution=zipfian -p redis.host=localhost -p redis.port=6379 > warm_cache_results.txt
type warm_cache_results.txt
echo.
echo Results saved to: warm_cache_results.txt
pause

echo ----------------------------------------
echo COMPARISON: Cold vs Warm Cache
echo ----------------------------------------
echo.
echo Cold Cache Results (Cache Misses):
findstr /C:"RunTime" /C:"Throughput" /C:"Operations" cold_cache_results.txt
echo.
echo Warm Cache Results (Cache Hits):
findstr /C:"RunTime" /C:"Throughput" /C:"Operations" warm_cache_results.txt
echo.
echo ----------------------------------------
echo Cache-Aside Pattern Summary:
echo ----------------------------------------
echo 1. Cold cache = All misses = Slow (need to fetch from MongoDB)
echo 2. Warm cache = Many hits = Fast (serve from Redis)
echo 3. Zipfian distribution = Hot items get cached = High hit rate
echo.
pause
