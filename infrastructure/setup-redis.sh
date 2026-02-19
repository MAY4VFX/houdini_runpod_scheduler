#!/bin/bash
# RunPodFarm - Redis Setup Guide
# Upstash Serverless Redis (Frankfurt)
#
# 1. Go to https://console.upstash.com/
# 2. Create a new Redis database:
#    - Name: runpodfarm-{project_name}
#    - Region: EU-West-1 (Frankfurt)
#    - TLS: Enabled
#    - Eviction: Disabled
# 3. Copy the Redis URL (rediss://default:xxx@xxx.upstash.io:6379)
# 4. Set it as REDIS_URL in your .env file
#
# Key namespace convention:
#   juicefs:*           - JuiceFS metadata (managed by JuiceFS)
#   rp:tasks:{pid}:{uid} - Task queues per project/user
#   rp:results:{task_id} - Task results
#   rp:heartbeat:{pod_id} - Pod heartbeats (TTL 30s)
#   rp:logs:{task_id}    - Task logs
#   rp:pods:{pid}:{uid}  - Pod registry per project/user
#   rp:metrics:*         - Metrics and cost tracking

echo "RunPodFarm Redis Setup"
echo "====================="

if [ -z "$REDIS_URL" ]; then
    echo "ERROR: REDIS_URL environment variable not set"
    echo "Set it to your Upstash Redis URL: rediss://default:xxx@xxx.upstash.io:6379"
    exit 1
fi

# Test connection
echo "Testing Redis connection..."
python3 -c "
import redis
r = redis.from_url('$REDIS_URL')
r.ping()
print('Redis connection successful!')
print(f'Server: {r.info(\"server\")[\"redis_version\"]}')
"
