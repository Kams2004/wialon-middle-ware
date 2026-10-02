#!/bin/sh
# certbot deploy hook: reload Apache after a certificate is renewed so the new
# certificate is served. Install:
#   sudo cp deploy/apache/certbot-reload-apache.sh /etc/letsencrypt/renewal-hooks/deploy/reload-apache.sh
#   sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-apache.sh
systemctl reload apache2
