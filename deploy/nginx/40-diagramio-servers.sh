#!/bin/sh
# Chạy khi container Nginx khởi động (/docker-entrypoint.d/ của image nginx): chép danh sách instance mặc định sang chỗ
# ghi được, để deploy/rolling-restart.sh thay tạm mà không sửa file của repo. Container khởi động lại thì về mặc định.
set -eu
mkdir -p /etc/nginx/diagramio-runtime
cp /etc/nginx/diagramio/servers.conf /etc/nginx/diagramio-runtime/servers.conf
