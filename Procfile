# 1 worker on purpose: boot() starts the email dispatcher + inbound poller, which
# poll a shared file-backed queue. Multiple workers = multiple dispatchers racing
# the same queue = duplicate sends. Keep workers=1; scale concurrency via threads.
web: gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 cwscraper.web.app:app
