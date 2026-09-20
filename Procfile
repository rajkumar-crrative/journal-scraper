# Advanced Enterprise Deployment & Automation Configuration
web: gunicorn main_app:app --workers=4 --threads=2 --worker-class=gthread --timeout 120 --keep-alive 5 --log-level=info

# Step 1: Security Headers & Protection Configuration
security:
  enabled: true
  xss_protection: "1; mode=block"
  content_type_nosniff: true
  frame_options: "DENY"
  server_header_mask: "Secure-Engine"

# Step 2: High-Speed Web Scanning & Crawler Engine Setup
crawler_engine:
  max_concurrent_connections: 100
  request_timeout_seconds: 6
  max_page_depth: 1500
  stream_enabled: true
  pdf_parsing_support: true

# Step 3: Performance Optimization
performance:
  asyncio_loop: "uvloop"
  buffer_size_kb: 1024
  gzip_compression: true
  keepalive_timeout: 65

# Step 4: Python Runtime Environment Setup
environment:
  python_version: "3.11"
  pip_auto_upgrade: true
  requirements_file: "requirements.txt"
  entry_point: "main_app.py"

# Step 5: Process Control & Restart Rules
process_management:
  auto_restart_on_failure: true
  max_retry_attempts: 5
  graceful_shutdown_timeout: 30

# Step 6: Logging & Error Tracking System
logging_system:
  log_level: "INFO"
  log_to_file: true
  log_file_path: "logs/app.log"
  max_bytes: 10485760
  backup_count: 5

# Step 7: Environment Variables & Passwords Protection
security_env:
  FLASK_ENV: "production"
  PYTHONUNBUFFERED: "1"
  SESSION_COOKIE_SECURE: true
  SESSION_COOKIE_HTTPONLY: true

# Step 8: Database & Backup Management
backup_system:
  auto_backup_enabled: true
  export_formats: ["csv", "xlsx", "txt"]
  local_storage_folder: "backups/"

# Step 9: Maintenance & Health Verification
health_check:
  enabled: true
  check_interval: 60
  endpoint: "/"
