import os
import time
import urllib.request
base=os.getenv('TEST_BASE_URL','http://127.0.0.1:8000')
for attempt in range(60):
    try:
        with urllib.request.urlopen(base+'/api/health',timeout=1) as response:
            if response.status==200:break
    except OSError:
        time.sleep(0.25)
else:raise SystemExit('Server did not become healthy')
